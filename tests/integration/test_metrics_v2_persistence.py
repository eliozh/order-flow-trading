"""Integration tests: metric engine v2 (footprint, OI, funding, liquidations).

The complete ``orderflow-metrics`` document is validated for BTCUSDT and
XAUUSDT. Liquidation fixtures are recorded/live-shaped public WS events, never
historical USD-M liquidation data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from oft.adapters.archive import FixtureArchiveSource
from oft.adapters.parquet_store import (
    ParquetStore,
    liquidation_parts,
    read_footprint,
    read_liquidations,
)
from oft.adapters.sqlite_store import SqliteStore
from oft.application.backfill_service import BackfillService
from oft.application.metric_service import MetricInputs, MetricRequest, MetricService
from oft.application.oi_funding_service import OiFundingService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.events import AggTrade
from oft.domain.metrics.funding import FundingSample, compute_funding
from oft.domain.metrics.liquidations import (
    LiquidationEvent,
    LiquidationRecorder,
    compute_liquidations,
    load_liquidation_events,
)
from oft.domain.metrics.open_interest import OpenInterestSample, compute_open_interest
from oft.domain.metrics.version import orderflow_metric_version

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"
GOLDEN = ROOT / "tests" / "fixtures" / "golden"
MARKET = ROOT / "tests" / "fixtures" / "market"
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
DEPLOYED_AT = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
BTC_AS_OF = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
XAU_WINDOW_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
XAU_WINDOW_END = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
XAU_AS_OF = datetime(2026, 10, 1, 0, 30, tzinfo=UTC)


def _load(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    common = _load("common.schema.json")
    metrics = _load("orderflow-metrics.schema.json")
    resource = Resource.from_contents(common, default_specification=DRAFT202012)
    registry = Registry().with_resource(common["$id"], resource)
    return Draft202012Validator(metrics, registry=registry)


def _validate(validator: Draft202012Validator, payload: dict[str, Any]) -> None:
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)


@dataclass
class _Harness:
    settings: Settings
    store: SqliteStore
    parquet: ParquetStore
    service: MetricService


def _harness(tmp_path: Path, *, backfill: bool = True) -> _Harness:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    parquet = ParquetStore(settings)
    parquet.ensure_layout()
    if backfill:
        BackfillService(
            settings=settings,
            archive=FixtureArchiveSource(ARCHIVE),
            parquet=parquet,
            store=store,
            clock=FixedClock(NOW),
        ).run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    service = MetricService(
        settings=settings,
        store=store,
        parquet=parquet,
        clock=FixedClock(NOW),
    )
    return _Harness(settings=settings, store=store, parquet=parquet, service=service)


def _request(**overrides: object) -> MetricRequest:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "period": "4h",
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "tick_size": Decimal("0.10"),
    }
    values.update(overrides)
    return MetricRequest(**values)  # type: ignore[arg-type]


def _xau_request() -> MetricRequest:
    return MetricRequest(
        symbol="XAUUSDT",
        period="4h",
        window_start=XAU_WINDOW_START,
        window_end=XAU_WINDOW_END,
        tick_size=Decimal("0.01"),
    )


def _xau_trades() -> tuple[AggTrade, ...]:
    rows = (
        (1, "2650.00", "1.00", False, 1),
        (2, "2650.01", "2.00", True, 2),
        (3, "2650.02", "1.50", False, 3),
        (4, "2650.01", "0.50", False, 4),
        (5, "2649.99", "1.00", True, 5),
        (6, "2650.00", "0.50", True, 6),
    )
    return tuple(
        AggTrade(
            symbol="XAUUSDT",
            agg_trade_id=trade_id,
            price=Decimal(price),
            quantity=Decimal(quantity),
            first_trade_id=trade_id * 10,
            last_trade_id=trade_id * 10,
            transact_time=XAU_WINDOW_START + timedelta(seconds=second),
            is_buyer_maker=maker,
        )
        for trade_id, price, quantity, maker, second in rows
    )


def _write_xau_trades(harness: _Harness) -> None:
    for entry in harness.parquet.write_agg_trades_batch(
        _xau_trades(), symbol="XAUUSDT", source="fixture:xauusdt"
    ):
        harness.store.upsert_manifest_entry(entry)


def _btc_oi_samples() -> tuple[OpenInterestSample, ...]:
    return tuple(
        OpenInterestSample(
            ts=WINDOW_START + timedelta(minutes=minute),
            open_interest=Decimal(open_interest),
            open_interest_value=Decimal(value),
            long_short_ratio=Decimal(ratio),
            taker_ratio=Decimal(taker),
        )
        for minute, open_interest, value, ratio, taker in (
            (0, "100.0", "7000000.00", "1.10", "0.50"),
            (5, "105.0", "7350000.00", "1.20", "0.51"),
            (10, "120.0", "8400000.00", "1.30", "0.52"),
        )
    )


def _btc_funding_samples() -> tuple[FundingSample, ...]:
    return (
        FundingSample(
            ts=WINDOW_START,
            rate=Decimal("0.00010000"),
            mark_price=Decimal("70000.00"),
            index_price=Decimal("70001.00"),
            interval_hours=8,
        ),
        FundingSample(
            ts=WINDOW_START + timedelta(hours=8),
            rate=Decimal("0.00030000"),
            mark_price=Decimal("70100.00"),
            index_price=Decimal("70101.00"),
            interval_hours=8,
        ),
    )


def _btc_oi() -> object:
    return compute_open_interest(
        _btc_oi_samples(), window_start=WINDOW_START, window_end=WINDOW_END
    )


def _btc_funding() -> object:
    return compute_funding(_btc_funding_samples(), as_of=BTC_AS_OF)


def _btc_liquidations() -> object:
    recorder = LiquidationRecorder.from_jsonl(
        MARKET / "liquidations_btcusdt.jsonl", clock=FixedClock(DEPLOYED_AT)
    )
    return recorder.summary()


def _btc_inputs() -> MetricInputs:
    return MetricInputs(
        open_interest=_btc_oi(),  # type: ignore[arg-type]
        funding=_btc_funding(),  # type: ignore[arg-type]
        liquidations=_btc_liquidations(),  # type: ignore[arg-type]
    )


def _xau_service() -> OiFundingService:
    return OiFundingService.from_csv(MARKET / "xauusdt_metrics_sample.csv")


def _xau_inputs() -> MetricInputs:
    service = _xau_service()
    return MetricInputs(
        open_interest=service.open_interest(
            window_start=XAU_WINDOW_START, window_end=XAU_WINDOW_END
        ),
        funding=service.funding(as_of=XAU_AS_OF),
        liquidations=compute_liquidations((), symbol="XAUUSDT", coverage_start=DEPLOYED_AT),
    )


def test_btcusdt_complete_document_has_all_six_groups_and_validates(
    tmp_path: Path, validator: Draft202012Validator
) -> None:
    harness = _harness(tmp_path)

    document = harness.service.compute(_request(), inputs=_btc_inputs())
    payload = document.to_dict()

    _validate(validator, payload)
    assert payload["symbol"] == "BTCUSDT"
    assert payload["data_quality"] == "PARTIAL"
    assert "span" in " ".join(payload["quality_reasons"])
    assert payload["open_interest"]["sampling"] == "REGULAR"
    assert payload["open_interest"]["delta_over_window"] == "20.0"
    assert payload["funding"]["current_rate"] == "0.00030000"
    assert payload["funding"]["next_funding_time"] == "2026-10-01T16:00:00.000Z"
    assert payload["liquidations"]["coverage"] == "SINCE_DEPLOYMENT"
    assert payload["footprint"]["bars"]
    assert payload["volume_profile"]["bins"]
    assert payload["cvd"]["points"]


def test_xauusdt_irregular_open_interest_is_estimated_and_validates(
    tmp_path: Path, validator: Draft202012Validator
) -> None:
    harness = _harness(tmp_path)
    _write_xau_trades(harness)
    service = _xau_service()
    oi = service.open_interest(window_start=XAU_WINDOW_START, window_end=XAU_WINDOW_END)

    document = harness.service.compute(_xau_request(), inputs=_xau_inputs())
    payload = document.to_dict()

    _validate(validator, payload)
    assert payload["open_interest"]["sampling"] == "IRREGULAR_RESAMPLED"
    assert payload["data_quality"] == "ESTIMATED"
    reasons = " ".join(payload["quality_reasons"])
    assert "resampled" in reasons.lower()
    assert "ESTIMATED" in reasons
    assert payload["funding"]["interval_hours"] == 8
    assert payload["liquidations"]["coverage"] == "SINCE_DEPLOYMENT"
    assert payload["footprint"]["bars"]
    # The resampled points are estimates; original samples stay measured.
    assert oi.delta_over_window == Decimal("15.0")
    assert oi.points[0].ts == XAU_WINDOW_START
    assert oi.points[-1].open_interest == Decimal("1015.0")
    original = {XAU_WINDOW_START + timedelta(minutes=minute) for minute in (0, 5, 11, 15, 22)}
    assert all(not point.estimated for point in oi.points if point.ts in original)
    assert all(point.estimated for point in oi.points if point.ts not in original)


def test_footprint_golden_matches_every_cell_and_stacked_flag(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    golden = json.loads((GOLDEN / "footprint_btcusdt.json").read_text(encoding="utf-8"))

    document = harness.service.compute(_request())
    payload = document.to_dict()

    assert document.metric_version == golden["metric_version"]
    assert payload["footprint"] == golden["footprint"]


def test_open_interest_golden_matches_irregular_xauusdt(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _write_xau_trades(harness)
    golden = json.loads((GOLDEN / "open_interest_xauusdt.json").read_text(encoding="utf-8"))

    document = harness.service.compute(_xau_request(), inputs=_xau_inputs())
    payload = document.to_dict()

    assert document.metric_version == golden["metric_version"]
    assert payload["open_interest"] == golden["open_interest"]


def test_funding_golden_matches_btcusdt(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    golden = json.loads((GOLDEN / "funding_btcusdt.json").read_text(encoding="utf-8"))

    document = harness.service.compute(_request(), inputs=_btc_inputs())
    payload = document.to_dict()

    assert document.metric_version == golden["metric_version"]
    assert payload["funding"] == golden["funding"]


def test_liquidations_golden_matches_and_never_claims_history(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    golden = json.loads((GOLDEN / "liquidations_btcusdt.json").read_text(encoding="utf-8"))

    document = harness.service.compute(_request(), inputs=_btc_inputs())
    payload = document.to_dict()

    assert document.metric_version == golden["metric_version"]
    assert payload["liquidations"] == golden["liquidations"]
    limitations = " ".join(payload["liquidations"]["limitations"])
    assert "largest liquidation per symbol per second" in limitations
    assert "No historical USD-M liquidation backfill exists" in limitations
    assert payload["liquidations"]["coverage"] == "SINCE_DEPLOYMENT"
    assert payload["liquidations"]["coverage_start"] == "2026-10-01T00:00:00.000Z"


def test_liquidations_are_none_before_recording(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    document = harness.service.compute(_request())
    payload = document.to_dict()

    assert payload["liquidations"]["coverage"] == "NONE"
    assert payload["liquidations"]["coverage_start"] is None
    limitations = " ".join(payload["liquidations"]["limitations"])
    assert "largest liquidation per symbol per second" in limitations
    assert "No historical USD-M liquidation backfill exists" in limitations


def test_persisted_footprint_follows_adr_0002_layout_and_carries_metric_version(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    persisted = harness.service.compute_and_persist(_request(), inputs=_btc_inputs())

    assert persisted.footprint_entry is not None
    entry = persisted.footprint_entry
    version = persisted.document.metric_version
    assert Path(entry.path).is_file()
    assert f"footprint/symbol=BTCUSDT/period=4h/metric_version={version}/" in entry.path
    assert entry.data_type == "footprint"
    assert entry.source == f"footprint:{version}"
    assert entry.metric_version == version

    rows = read_footprint(harness.settings, symbol="BTCUSDT", period="4h", metric_version=version)
    assert len(rows) == 21  # 7 bars x 3 occupied price levels
    assert {row["metric_version"] for row in rows} == {version}
    assert {row["symbol"] for row in rows} == {"BTCUSDT"}
    assert {row["record_type"] for row in rows} == {"FOOTPRINT_CELL"}
    assert {row["is_stacked_imbalance"] for row in rows} == {False}
    first = rows[0]
    assert first["price"] == "70000.00"
    assert first["bid_volume"] == "0"
    assert first["ask_volume"] == "0.10"
    assert first["delta"] == "0.10"
    assert first["total_volume"] == "0.10"

    manifest = [
        row
        for row in harness.store.list_manifest()
        if row.path == entry.path and row.data_type == "footprint"
    ]
    assert len(manifest) == 1


def test_persisted_liquidations_follow_adr_0002_layout_and_carry_metric_version(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    persisted = harness.service.compute_and_persist(_request(), inputs=_btc_inputs())
    version = persisted.document.metric_version
    events = load_liquidation_events(MARKET / "liquidations_btcusdt.jsonl")

    entry = harness.service.persist_liquidation_events(
        events,
        symbol="BTCUSDT",
        window_start=DEPLOYED_AT,
        window_end=DEPLOYED_AT + timedelta(minutes=5),
        metric_version=version,
    )

    assert Path(entry.path).is_file()
    assert f"liquidations/symbol=BTCUSDT/metric_version={version}/" in entry.path
    assert entry.data_type == "liquidations"
    assert entry.source == f"liquidations:{version}"
    assert entry.metric_version == version
    rows = read_liquidations(harness.settings, symbol="BTCUSDT", metric_version=version)
    assert len(rows) == 4
    assert {row["metric_version"] for row in rows} == {version}
    assert {row["side"] for row in rows} == {"SELL", "BUY"}
    assert {row["symbol"] for row in rows} == {"BTCUSDT"}
    long_rows = [row for row in rows if row["side"] == "SELL"]
    assert len(long_rows) == 3


def test_footprint_and_liquidation_parts_are_content_addressed(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    first = harness.service.compute_and_persist(_request(), inputs=_btc_inputs())
    second = harness.service.compute_and_persist(_request(), inputs=_btc_inputs())
    assert first.footprint_entry is not None
    assert second.footprint_entry is not None
    assert first.footprint_entry.path == second.footprint_entry.path
    assert (
        Path(first.footprint_entry.path).read_bytes()
        == Path(second.footprint_entry.path).read_bytes()
    )

    events = load_liquidation_events(MARKET / "liquidations_btcusdt.jsonl")
    kwargs = {
        "symbol": "BTCUSDT",
        "window_start": DEPLOYED_AT,
        "window_end": DEPLOYED_AT + timedelta(minutes=5),
        "metric_version": first.document.metric_version,
    }
    one = harness.service.persist_liquidation_events(events, **kwargs)
    two = harness.service.persist_liquidation_events(events, **kwargs)
    assert one.path == two.path
    assert Path(one.path).read_bytes() == Path(two.path).read_bytes()
    assert (
        len(
            liquidation_parts(
                harness.settings, symbol="BTCUSDT", metric_version=first.document.metric_version
            )
        )
        == 1
    )

    changed = (
        events[0].__class__(
            received_at=events[0].received_at,
            trade_time=events[0].trade_time,
            symbol=events[0].symbol,
            side=events[0].side,
            price=events[0].price,
            average_price=events[0].average_price,
            quantity=Decimal("9.99"),
            source_seq=events[0].source_seq,
        ),
    )
    three = harness.service.persist_liquidation_events(changed, **kwargs)
    assert three.path != one.path


def test_metric_version_covers_every_v2_algorithm_and_parameter() -> None:
    base = orderflow_metric_version(
        period="4h",
        unit="BASE",
        tick_size=Decimal("0.10"),
        value_area_pct=Decimal("70"),
        bin_size_multiple=1,
    )
    variants = {
        orderflow_metric_version(
            period="4h",
            unit="BASE",
            tick_size=Decimal("0.10"),
            value_area_pct=Decimal("70"),
            bin_size_multiple=1,
            footprint_imbalance_threshold=Decimal("4"),
        ).identifier,
        orderflow_metric_version(
            period="4h",
            unit="BASE",
            tick_size=Decimal("0.10"),
            value_area_pct=Decimal("70"),
            bin_size_multiple=1,
            footprint_stacked_min_levels=4,
        ).identifier,
        orderflow_metric_version(
            period="4h",
            unit="BASE",
            tick_size=Decimal("0.10"),
            value_area_pct=Decimal("70"),
            bin_size_multiple=1,
            footprint_algorithm_version=2,
        ).identifier,
        orderflow_metric_version(
            period="4h",
            unit="BASE",
            tick_size=Decimal("0.10"),
            value_area_pct=Decimal("70"),
            bin_size_multiple=1,
            open_interest_algorithm_version=2,
        ).identifier,
        orderflow_metric_version(
            period="4h",
            unit="BASE",
            tick_size=Decimal("0.10"),
            value_area_pct=Decimal("70"),
            bin_size_multiple=1,
            funding_algorithm_version=2,
        ).identifier,
        orderflow_metric_version(
            period="4h",
            unit="BASE",
            tick_size=Decimal("0.10"),
            value_area_pct=Decimal("70"),
            bin_size_multiple=1,
            liquidation_algorithm_version=2,
        ).identifier,
        orderflow_metric_version(
            period="4h",
            unit="BASE",
            tick_size=Decimal("0.10"),
            value_area_pct=Decimal("70"),
            bin_size_multiple=1,
            liquidation_bucket_seconds=30,
        ).identifier,
    }
    assert base.identifier not in variants
    assert len(variants) == 7


def test_v2_metric_path_has_no_wall_clock_or_float_calls() -> None:
    files = (
        ROOT / "src" / "oft" / "domain" / "metrics" / "footprint.py",
        ROOT / "src" / "oft" / "domain" / "metrics" / "open_interest.py",
        ROOT / "src" / "oft" / "domain" / "metrics" / "funding.py",
        ROOT / "src" / "oft" / "domain" / "metrics" / "liquidations.py",
        ROOT / "src" / "oft" / "domain" / "metrics" / "version.py",
        ROOT / "src" / "oft" / "application" / "metric_service.py",
        ROOT / "src" / "oft" / "application" / "oi_funding_service.py",
        ROOT / "src" / "oft" / "application" / "liquidation_recorder.py",
    )

    for path in files:
        source = path.read_text(encoding="utf-8")
        assert "datetime.now(" not in source, path
        assert "float(" not in source, path


def test_xauusdt_funding_comes_from_measured_rows(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _write_xau_trades(harness)
    service = _xau_service()

    funding = service.funding(as_of=XAU_AS_OF)
    document = harness.service.compute(_xau_request(), inputs=_xau_inputs())
    payload = document.to_dict()

    assert funding.current_rate == Decimal("0.00010000")
    assert funding.next_funding_time == XAU_WINDOW_START + timedelta(hours=8)
    assert funding.interval_hours == 8
    assert payload["funding"]["history"][0]["rate"] == "0.00010000"
    assert payload["open_interest"]["unit"] == "BASE"


def test_missing_funding_period_is_reported_not_zeroed(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    samples = (
        FundingSample(
            ts=WINDOW_START,
            rate=Decimal("0.0001"),
            mark_price=Decimal("70000.00"),
            index_price=Decimal("70001.00"),
            interval_hours=8,
        ),
        FundingSample(
            ts=WINDOW_START + timedelta(hours=24),
            rate=Decimal("0.0002"),
            mark_price=Decimal("70200.00"),
            index_price=Decimal("70201.00"),
            interval_hours=8,
        ),
    )
    funding = compute_funding(samples, as_of=WINDOW_START + timedelta(hours=25))

    document = harness.service.compute(
        _request(),
        inputs=MetricInputs(open_interest=_btc_oi(), funding=funding),  # type: ignore[arg-type]
    )
    payload = document.to_dict()

    assert funding.missing_periods
    assert payload["data_quality"] == "MISSING"
    assert any(
        "MISSING" in reason and "not treated as zero" in reason
        for reason in payload["quality_reasons"]
    )
    assert all(item["rate"] != "0" for item in payload["funding"]["history"])


def test_persisted_part_bytes_are_stable(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    persisted = harness.service.compute_and_persist(_request(), inputs=_btc_inputs())
    checksum = hashlib.sha256(Path(persisted.entry.path).read_bytes()).hexdigest()
    assert persisted.entry.checksum == checksum
    assert persisted.footprint_entry is not None
    assert (
        persisted.footprint_entry.checksum
        == hashlib.sha256(Path(persisted.footprint_entry.path).read_bytes()).hexdigest()
    )


def test_metric_request_rejects_naive_or_inverted_windows(tmp_path: Path) -> None:
    from oft.application.metric_service import MetricRequest

    with pytest.raises(ValueError, match="timezone"):
        MetricRequest(
            symbol="BTCUSDT",
            period="4h",
            window_start=datetime(2026, 10, 1),
            window_end=datetime(2026, 10, 2),
            tick_size=Decimal("0.10"),
        )
    with pytest.raises(ValueError, match="after"):
        MetricRequest(
            symbol="BTCUSDT",
            period="4h",
            window_start=WINDOW_START,
            window_end=WINDOW_START,
            tick_size=Decimal("0.10"),
        )


def _liq_event(quantity: str, *, seq: int | None = None) -> LiquidationEvent:
    received = NOW + timedelta(minutes=1)
    return LiquidationEvent(
        received_at=received,
        trade_time=received,
        symbol="BTCUSDT",
        side="SELL",
        price=Decimal("100.00"),
        average_price=Decimal("100.00"),
        quantity=Decimal(quantity),
        source_seq=seq,
    )


def test_read_liquidations_dedupes_on_original_quantity_column(tmp_path: Path) -> None:
    harness = _harness(tmp_path, backfill=False)
    version = orderflow_metric_version(
        period="4h",
        unit="BASE",
        tick_size=Decimal("0.10"),
        value_area_pct=Decimal("70"),
        bin_size_multiple=1,
    ).identifier
    events = (_liq_event("1"), _liq_event("5"))  # both source_seq=None

    harness.service.persist_liquidation_events(
        events,
        symbol="BTCUSDT",
        window_start=NOW,
        window_end=NOW + timedelta(hours=1),
        metric_version=version,
    )
    rows = read_liquidations(
        harness.settings, symbol="BTCUSDT", metric_version=version, store=harness.store
    )

    assert len(rows) == 2
    assert {row["original_quantity"] for row in rows} == {"1", "5"}


def test_liquidation_part_identity_is_order_independent(tmp_path: Path) -> None:
    harness = _harness(tmp_path, backfill=False)
    version = orderflow_metric_version(
        period="4h",
        unit="BASE",
        tick_size=Decimal("0.10"),
        value_area_pct=Decimal("70"),
        bin_size_multiple=1,
    ).identifier
    events = [_liq_event("1", seq=2), _liq_event("5", seq=1)]
    common = {
        "symbol": "BTCUSDT",
        "window_start": NOW,
        "window_end": NOW + timedelta(hours=1),
        "metric_version": version,
        "input_manifest_hash": "a" * 64,
        "computed_at": "2026-11-06T12:00:00.000Z",
        "data_quality": "COMPLETE",
        "quality_reasons": (),
    }
    first = harness.parquet.write_liquidation_part(events, **common)
    second = harness.parquet.write_liquidation_part(list(reversed(events)), **common)

    assert first.path == second.path


def test_input_hash_treats_empty_metric_inputs_like_none(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    without = harness.service.compute(_request())
    with_empty = harness.service.compute(_request(), inputs=MetricInputs())

    assert without.input_manifest_hash == with_empty.input_manifest_hash


def test_write_liquidation_part_validates_before_creating_directories(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, backfill=False)
    event = _liq_event("1")

    with pytest.raises(ValueError, match="timezone"):
        harness.parquet.write_liquidation_part(
            (event,),
            symbol="BTCUSDT",
            window_start=datetime(2026, 10, 1),
            window_end=datetime(2026, 10, 2),
            metric_version="x",
            input_manifest_hash="a" * 64,
            computed_at="2026-11-06T12:00:00.000Z",
            data_quality="COMPLETE",
            quality_reasons=(),
        )
    assert not list((harness.settings.data_dir / "liquidations").rglob("*.parquet"))

    with pytest.raises(ValueError, match="symbol"):
        harness.parquet.write_liquidation_part(
            (event,),
            symbol="ETHUSDT",
            window_start=NOW,
            window_end=NOW + timedelta(hours=1),
            metric_version="x",
            input_manifest_hash="a" * 64,
            computed_at="2026-11-06T12:00:00.000Z",
            data_quality="COMPLETE",
            quality_reasons=(),
        )
