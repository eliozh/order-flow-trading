"""Integration tests: backfill Parquet -> versioned metrics -> immutable parts."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import structlog

from oft.adapters.archive import FixtureArchiveSource
from oft.adapters.parquet_store import ParquetStore, metrics_parts, read_metrics
from oft.adapters.sqlite_store import SqliteStore
from oft.application.backfill_service import BackfillService
from oft.application.metric_service import MetricRequest, MetricService, load_klines
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.metrics.cvd import kline_delta
from oft.domain.metrics.types import contract_json, decimal_str
from oft.domain.metrics.version import build_metric_version, orderflow_metric_version

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)


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


def test_compute_from_backfill_partitions_is_complete_and_exact(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    document = harness.service.compute(_request())
    payload = document.to_dict()

    assert document.quality.data_quality.value == "PARTIAL"
    assert "open_interest not measured in this slice" in document.quality.quality_reasons
    assert "funding not measured in this slice" in document.quality.quality_reasons
    assert len(document.cvd.points) == 7
    assert document.cvd.summary.delta == Decimal("1.40")
    assert document.cvd.summary.cvd_change == Decimal("1.40")
    assert document.volume_profile.poc == Decimal("70002.00")
    assert document.volume_profile.vah == Decimal("70002.10")
    assert document.volume_profile.val == Decimal("70001.00")
    assert document.input_manifest_hash == payload["input_manifest_hash"]
    assert len(document.input_manifest_hash) == 64
    assert document.computed_at == "2026-11-06T12:00:00.000Z"
    assert payload["window"] == {
        "start": "2026-10-01T00:00:00.000Z",
        "end": "2026-10-08T00:00:00.000Z",
    }


def test_kline_cross_check_passes_exactly_on_the_one_week_fixture(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    document = harness.service.compute(_request())
    klines = load_klines(harness.settings, _request())

    assert len(klines) == 7
    assert document.quality.data_quality.value == "PARTIAL"
    assert not any("kline" in reason for reason in document.quality.quality_reasons)
    by_start = {kline.bar_start: kline for kline in klines}
    for point in document.cvd.points:
        kline = by_start[point.bar_start]
        # Independent fixture row, not values re-derived from the CVD bar.
        assert (
            kline_delta(volume=kline.volume, taker_buy_volume=kline.taker_buy_volume) == point.delta
        )


def test_tampered_kline_row_breaks_the_cross_check(tmp_path: Path) -> None:
    fixture = ROOT / "tests" / "fixtures" / "market" / "btcusdt_klines_4h.csv"
    lines = fixture.read_text(encoding="utf-8").splitlines()
    fields = lines[1].split(",")
    fields[7] = "0.61"  # volume no longer equals buy + sell
    lines[1] = ",".join(fields)
    tampered = tmp_path / "tampered_klines.csv"
    tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")

    harness = _harness(tmp_path)
    settings = harness.settings.model_copy(update={"klines_fixture": tampered})
    service = MetricService(
        settings=settings,
        store=harness.store,
        parquet=harness.parquet,
        clock=FixedClock(NOW),
    )

    with pytest.raises(ValueError, match="kline"):
        service.compute(_request())


def test_missing_kline_fixture_marks_quality_partial(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    settings = harness.settings.model_copy(
        update={"klines_fixture": tmp_path / "missing_klines.csv"}
    )
    service = MetricService(
        settings=settings,
        store=harness.store,
        parquet=harness.parquet,
        clock=FixedClock(NOW),
    )

    document = service.compute(_request())

    assert document.quality.data_quality.value == "PARTIAL"
    assert any("kline" in reason for reason in document.quality.quality_reasons)
    assert len(document.cvd.points) == 7


def test_persisted_rows_carry_metric_provenance(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    document = harness.service.compute(_request())

    persisted = harness.service.compute_and_persist(_request())

    entry = persisted.entry
    assert Path(entry.path).is_file()
    assert entry.data_type == "metrics"
    assert entry.source == f"metrics:{document.metric_version}"
    assert document.metric_version in entry.path
    assert "symbol=BTCUSDT" in entry.path
    assert "period=4h" in entry.path

    manifest = harness.store.list_manifest(symbol="BTCUSDT")
    assert any(row.path == entry.path for row in manifest)

    rows = read_metrics(
        harness.settings,
        symbol="BTCUSDT",
        period="4h",
        metric_version=document.metric_version,
    )
    assert len(rows) == 28  # 7 CVD bars + 21 volume-profile bins
    for row in rows:
        assert row["metric_version"] == document.metric_version
        assert row["input_manifest_hash"] == document.input_manifest_hash
        assert row["computed_at"] is not None
        assert row["data_quality"] == "PARTIAL"
        assert "open_interest not measured in this slice" in row["quality_reasons"]
        assert "funding not measured in this slice" in row["quality_reasons"]
    assert {row["record_type"] for row in rows} == {"CVD_BAR", "VOLUME_PROFILE_BIN"}
    cvd_rows = [row for row in rows if row["record_type"] == "CVD_BAR"]
    assert all(isinstance(row["buy_volume"], str) for row in cvd_rows)
    assert all(isinstance(row["delta"], str) for row in cvd_rows)
    assert all(isinstance(row["cvd"], str) for row in cvd_rows)


def test_identical_inputs_and_version_produce_identical_bytes(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    first = harness.service.compute_and_persist(_request())
    second = harness.service.compute_and_persist(_request())

    assert first.document.metric_version == second.document.metric_version
    # One logical document per (version, window): the same part is replaced.
    assert first.entry.path == second.entry.path
    first_bytes = Path(first.entry.path).read_bytes()
    second_bytes = Path(second.entry.path).read_bytes()
    assert hashlib.sha256(first_bytes).hexdigest() == hashlib.sha256(second_bytes).hexdigest()
    rows = read_metrics(
        harness.settings,
        symbol="BTCUSDT",
        period="4h",
        metric_version=first.document.metric_version,
    )
    assert len(rows) == 28


def test_a_new_metric_version_gets_a_new_path_and_old_versions_stay_readable(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    old = harness.service.compute_and_persist(_request())

    new_document = harness.service.compute(_request(bin_size_multiple=5))
    assert new_document.metric_version != old.document.metric_version
    new = harness.service.compute_and_persist(_request(bin_size_multiple=5))
    assert new.entry.path != old.entry.path
    assert new.document.metric_version in new.entry.path
    assert Path(old.entry.path).is_file()

    old_rows = read_metrics(
        harness.settings,
        symbol="BTCUSDT",
        period="4h",
        metric_version=old.document.metric_version,
    )
    new_rows = read_metrics(
        harness.settings,
        symbol="BTCUSDT",
        period="4h",
        metric_version=new.document.metric_version,
    )
    assert len(old_rows) == 28
    assert len(new_rows) == 12  # 0.50 bins leave two empty bins between prices
    assert {row["metric_version"] for row in old_rows} == {old.document.metric_version}
    assert {row["metric_version"] for row in new_rows} == {new.document.metric_version}
    assert Path(old.entry.path).is_file()


def test_manifest_insert_failure_removes_the_published_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    captured: dict[str, str] = {}
    original = harness.store.upsert_manifest_entry

    def fail(entry: object) -> None:
        captured["path"] = entry.path  # type: ignore[attr-defined]
        raise RuntimeError("simulated manifest insert failure")

    monkeypatch.setattr(harness.store, "upsert_manifest_entry", fail)

    with pytest.raises(RuntimeError, match="manifest"):
        harness.service.compute_and_persist(_request(value_area_pct=Decimal("68")))

    assert captured["path"]
    assert not Path(captured["path"]).exists()

    monkeypatch.setattr(harness.store, "upsert_manifest_entry", original)
    assert not metrics_parts(harness.settings, symbol="BTCUSDT", period="4h")


def test_metric_version_includes_algorithm_and_parameters() -> None:
    one = build_metric_version(
        algorithm="cvd",
        algorithm_version=1,
        parameters={"period": "4h", "ratio_precision": 8},
    )
    same = build_metric_version(
        algorithm="cvd",
        algorithm_version=1,
        parameters={"ratio_precision": 8, "period": "4h"},
    )
    changed_parameter = build_metric_version(
        algorithm="cvd",
        algorithm_version=1,
        parameters={"period": "1h", "ratio_precision": 8},
    )
    changed_algorithm = build_metric_version(
        algorithm="cvd",
        algorithm_version=2,
        parameters={"period": "4h", "ratio_precision": 8},
    )

    assert one.identifier == same.identifier
    assert one.identifier != changed_parameter.identifier
    assert one.identifier != changed_algorithm.identifier
    assert one.identifier.startswith("cvd.v1.")
    assert len(one.digest) == 64


def test_orderflow_version_changes_with_any_algorithm_or_parameter() -> None:
    base = orderflow_metric_version(
        period="4h",
        unit="BASE",
        tick_size=Decimal("0.10"),
        value_area_pct=Decimal("70"),
        bin_size_multiple=1,
    )
    same = orderflow_metric_version(
        period="4h",
        unit="BASE",
        tick_size=Decimal("0.10"),
        value_area_pct=Decimal("70"),
        bin_size_multiple=1,
    )
    assert base.identifier == same.identifier

    variants = {
        orderflow_metric_version(
            period="1h",
            unit="BASE",
            tick_size=Decimal("0.10"),
            value_area_pct=Decimal("70"),
            bin_size_multiple=1,
        ).identifier,
        orderflow_metric_version(
            period="4h",
            unit="QUOTE",
            tick_size=Decimal("0.10"),
            value_area_pct=Decimal("70"),
            bin_size_multiple=1,
        ).identifier,
        orderflow_metric_version(
            period="4h",
            unit="BASE",
            tick_size=Decimal("0.01"),
            value_area_pct=Decimal("70"),
            bin_size_multiple=1,
        ).identifier,
        orderflow_metric_version(
            period="4h",
            unit="BASE",
            tick_size=Decimal("0.10"),
            value_area_pct=Decimal("68"),
            bin_size_multiple=1,
        ).identifier,
        orderflow_metric_version(
            period="4h",
            unit="BASE",
            tick_size=Decimal("0.10"),
            value_area_pct=Decimal("70"),
            bin_size_multiple=5,
        ).identifier,
        orderflow_metric_version(
            period="4h",
            unit="BASE",
            tick_size=Decimal("0.10"),
            value_area_pct=Decimal("70"),
            bin_size_multiple=1,
            cvd_algorithm_version=2,
        ).identifier,
        orderflow_metric_version(
            period="4h",
            unit="BASE",
            tick_size=Decimal("0.10"),
            value_area_pct=Decimal("70"),
            bin_size_multiple=1,
            volume_profile_algorithm_version=2,
        ).identifier,
    }
    assert base.identifier not in variants
    assert len(variants) == 7
    assert base.identifier.startswith("orderflow_metrics.v1.")


@pytest.mark.parametrize(
    "overrides",
    [
        {"period": "3h"},
        {"unit": "USD"},
        {"tick_size": Decimal("0")},
        {"value_area_pct": Decimal("0")},
        {"value_area_pct": Decimal("101")},
        {"bin_size_multiple": 0},
    ],
)
def test_invalid_orderflow_version_parameters_are_rejected(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "period": "4h",
        "unit": "BASE",
        "tick_size": Decimal("0.10"),
        "value_area_pct": Decimal("70"),
        "bin_size_multiple": 1,
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        orderflow_metric_version(**values)  # type: ignore[arg-type]


def test_build_metric_version_rejects_invalid_algorithm_and_value_types() -> None:
    with pytest.raises(ValueError):
        build_metric_version(algorithm="CVD", algorithm_version=1, parameters={})
    with pytest.raises(ValueError):
        build_metric_version(algorithm="cvd", algorithm_version=0, parameters={})
    with pytest.raises(TypeError):
        build_metric_version(algorithm="cvd", algorithm_version=1, parameters={"x": 1.5})


def test_contract_json_rejects_binary_floats_and_non_finite_decimals() -> None:
    with pytest.raises(TypeError, match="float"):
        contract_json(0.5)
    with pytest.raises(ValueError, match="finite"):
        contract_json(Decimal("NaN"))


def test_missing_partition_marks_quality_partial(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    shutil.rmtree(
        harness.settings.data_dir
        / "market"
        / "um"
        / "agg_trades"
        / "symbol=BTCUSDT"
        / "date=2026-10-04"
    )

    document = harness.service.compute(_request())

    assert document.quality.data_quality.value == "PARTIAL"
    assert any("2026-10-04" in reason for reason in document.quality.quality_reasons)
    assert len(document.cvd.points) == 6


def test_no_input_rows_marks_quality_missing(tmp_path: Path) -> None:
    harness = _harness(tmp_path, backfill=False)

    document = harness.service.compute(_request())

    assert document.quality.data_quality.value == "MISSING"
    assert document.quality.quality_reasons == ("no aggTrade rows in the requested window",)
    assert document.cvd.points == ()
    assert document.volume_profile.notes == ("NO_TRADES",)
    assert len(document.input_manifest_hash) == 64


def test_quote_unit_is_rejected_instead_of_mislabelled(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    with pytest.raises(ValueError, match="BASE"):
        harness.service.compute(_request(unit="QUOTE"))


def test_metric_core_has_no_wall_clock_or_float_calls() -> None:
    files = (
        ROOT / "src" / "oft" / "domain" / "metrics" / "types.py",
        ROOT / "src" / "oft" / "domain" / "metrics" / "cvd.py",
        ROOT / "src" / "oft" / "domain" / "metrics" / "volume_profile.py",
        ROOT / "src" / "oft" / "domain" / "metrics" / "version.py",
        ROOT / "src" / "oft" / "application" / "metric_service.py",
    )

    for path in files:
        source = path.read_text(encoding="utf-8")
        assert "datetime.now(" not in source, path
        assert "float(" not in source, path


def test_orphan_metric_part_is_adopted(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    persisted = harness.service.compute_and_persist(_request())
    harness.store.delete_manifest_entry(persisted.entry.path)
    assert not any(entry.path == persisted.entry.path for entry in harness.store.list_manifest())

    harness.service.compute_and_persist(_request())

    fresh = MetricService(
        settings=harness.settings,
        store=harness.store,
        parquet=harness.parquet,
        clock=FixedClock(NOW),
    )
    fresh.compute_and_persist(_request())

    adopted = [
        entry
        for entry in harness.store.list_manifest()
        if entry.data_type == "metrics" and entry.path == persisted.entry.path
    ]
    assert len(adopted) == 1
    assert adopted[0].source.startswith("metrics:")
    assert (
        len(
            read_metrics(
                harness.settings,
                symbol="BTCUSDT",
                period="4h",
                metric_version=persisted.document.metric_version,
            )
        )
        == 28
    )


def test_duplicate_parts_are_deduped_with_a_warning(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    persisted = harness.service.compute_and_persist(_request())
    from dataclasses import replace

    duplicate = Path(persisted.entry.path).with_name("part-duplicate.parquet")
    shutil.copyfile(persisted.entry.path, duplicate)
    harness.store.upsert_manifest_entry(replace(persisted.entry, path=str(duplicate)))

    with structlog.testing.capture_logs() as logs:
        rows = read_metrics(
            harness.settings,
            symbol="BTCUSDT",
            period="4h",
            metric_version=persisted.document.metric_version,
        )

    assert len(rows) == 28
    assert any(entry["event"] == "metrics_run_selected" for entry in logs)


def test_metric_version_includes_the_algorithm_source_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import oft.domain.metrics.version as version_module

    kwargs = {
        "period": "4h",
        "unit": "BASE",
        "tick_size": Decimal("0.10"),
        "value_area_pct": Decimal("70"),
        "bin_size_multiple": 1,
    }
    base = orderflow_metric_version(**kwargs)
    assert dict(base.parameters)["algorithm_source_sha256"] == (
        version_module.ALGORITHM_SOURCE_DIGEST
    )

    monkeypatch.setattr(version_module, "ALGORITHM_SOURCE_DIGEST", "0" * 64)
    changed = orderflow_metric_version(**kwargs)
    assert changed.identifier != base.identifier


def test_full_window_is_part_of_the_part_identity(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    full = harness.service.compute_and_persist(_request())
    short = harness.service.compute_and_persist(
        _request(window_end=WINDOW_START + timedelta(days=3))
    )

    assert full.entry.path != short.entry.path
    assert Path(full.entry.path).is_file()
    assert Path(short.entry.path).is_file()
    rows = read_metrics(
        harness.settings,
        symbol="BTCUSDT",
        period="4h",
        metric_version=full.document.metric_version,
    )
    windows = {(row["window_start"], row["window_end"]) for row in rows}
    assert len(windows) == 2  # window-aware dedupe keeps both documents


def test_repeated_manifest_failure_keeps_the_published_part(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    first = harness.service.compute_and_persist(_request())
    first_bytes = Path(first.entry.path).read_bytes()

    def fail(_entry: object) -> None:
        raise RuntimeError("manifest unavailable")

    harness.store.upsert_manifest_entry = fail  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="manifest"):
        harness.service.compute_and_persist(_request())

    assert Path(first.entry.path).read_bytes() == first_bytes
    rows = read_metrics(
        harness.settings,
        symbol="BTCUSDT",
        period="4h",
        metric_version=first.document.metric_version,
    )
    assert len(rows) == 28


def test_different_content_never_overwrites_a_published_part(tmp_path: Path) -> None:
    from dataclasses import replace

    harness = _harness(tmp_path)
    first = harness.service.compute_and_persist(_request())
    first_bytes = Path(first.entry.path).read_bytes()

    rows = list(first.document.rows)
    rows[0] = replace(rows[0], delta=(rows[0].delta or Decimal("0")) + Decimal("1"))
    second = harness.parquet.write_metrics_part(
        rows,
        symbol="BTCUSDT",
        period="4h",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        metric_version=first.document.metric_version,
        input_manifest_hash=first.document.input_manifest_hash,
        computed_at="2026-11-06T13:00:00.000Z",
        data_quality="COMPLETE",
        quality_reasons=(),
    )

    assert second.path != first.entry.path
    assert Path(first.entry.path).read_bytes() == first_bytes
    assert Path(second.path).is_file()


def test_metrics_paths_reject_unsafe_symbol_and_period(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    document = harness.service.compute(_request())

    with pytest.raises(ValueError):
        read_metrics(
            harness.settings,
            symbol="../etc",
            period="4h",
            metric_version=document.metric_version,
        )
    with pytest.raises(ValueError):
        read_metrics(
            harness.settings,
            symbol="BTCUSDT",
            period="../../x",
            metric_version=document.metric_version,
        )


def test_metric_request_requires_decimal_parameters() -> None:
    with pytest.raises(TypeError):
        MetricRequest(
            symbol="BTCUSDT",
            period="4h",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            tick_size=0.1,  # type: ignore[arg-type]
        )


def test_read_selects_one_run_and_ignores_partial_for_the_same_window(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    document = harness.service.compute(_request())
    common = {
        "symbol": "BTCUSDT",
        "period": "4h",
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "metric_version": document.metric_version,
    }
    complete_entry = harness.parquet.write_metrics_part(
        document.rows,
        input_manifest_hash="c" * 64,
        computed_at="2026-11-06T12:00:00.000Z",
        data_quality="COMPLETE",
        quality_reasons=(),
        **common,
    )
    harness.store.upsert_manifest_entry(complete_entry)
    partial_rows = tuple(
        row
        for row in document.rows
        if not (row.record_type == "CVD_BAR" and row.bar_start == WINDOW_START)
    )
    partial_entry = harness.parquet.write_metrics_part(
        partial_rows,
        input_manifest_hash="d" * 64,
        computed_at="2026-11-06T13:00:00.000Z",  # later, but PARTIAL loses
        data_quality="PARTIAL",
        quality_reasons=("missing agg_trades part 2026-10-04",),
        **common,
    )
    harness.store.upsert_manifest_entry(partial_entry)

    with structlog.testing.capture_logs() as logs:
        rows = read_metrics(
            harness.settings,
            symbol="BTCUSDT",
            period="4h",
            metric_version=document.metric_version,
        )

    assert len(rows) == 28
    assert {row["input_manifest_hash"] for row in rows} == {"c" * 64}
    assert {row["data_quality"] for row in rows} == {"COMPLETE"}
    cvd_rows = [row for row in rows if row["record_type"] == "CVD_BAR"]
    assert [row["cvd"] for row in cvd_rows] == [
        decimal_str(point.cvd) for point in document.cvd.points
    ]
    selected = [event for event in logs if event["event"] == "metrics_run_selected"]
    assert selected
    assert selected[0]["selected_run"] == complete_entry.path
    assert partial_entry.path in selected[0]["discarded_runs"]


def test_read_prefers_the_latest_complete_run_on_a_provenance_tiebreak(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    document = harness.service.compute(_request())
    common = {
        "symbol": "BTCUSDT",
        "period": "4h",
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "metric_version": document.metric_version,
        "data_quality": "COMPLETE",
        "quality_reasons": (),
    }
    harness.store.upsert_manifest_entry(
        harness.parquet.write_metrics_part(
            document.rows,
            input_manifest_hash="a" * 64,
            computed_at="2026-11-06T12:00:00.000Z",
            **common,
        )
    )
    harness.store.upsert_manifest_entry(
        harness.parquet.write_metrics_part(
            document.rows,
            input_manifest_hash="b" * 64,
            computed_at="2026-11-06T13:00:00.000Z",
            **common,
        )
    )

    rows = read_metrics(
        harness.settings,
        symbol="BTCUSDT",
        period="4h",
        metric_version=document.metric_version,
    )

    assert len(rows) == 28
    assert {row["input_manifest_hash"] for row in rows} == {"b" * 64}
    assert {row["computed_at"] for row in rows} == {"2026-11-06T13:00:00.000Z"}


def test_real_clock_rerun_reuses_the_same_part(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    first = harness.service.compute_and_persist(_request())
    later = MetricService(
        settings=harness.settings,
        store=harness.store,
        parquet=harness.parquet,
        clock=FixedClock(NOW + timedelta(hours=2)),
    )
    second = later.compute_and_persist(_request())

    assert second.entry.path == first.entry.path
    assert Path(first.entry.path).read_bytes() == Path(second.entry.path).read_bytes()
    rows = read_metrics(
        harness.settings,
        symbol="BTCUSDT",
        period="4h",
        metric_version=first.document.metric_version,
    )
    assert len(rows) == 28
    manifest = [
        entry
        for entry in harness.store.list_manifest()
        if entry.data_type == "metrics" and entry.path == first.entry.path
    ]
    assert len(manifest) == 1


@pytest.mark.parametrize("period", ["1h", "8h", "1d"])
def test_kline_cross_check_is_period_keyed(tmp_path: Path, period: str) -> None:
    harness = _harness(tmp_path)

    document = harness.service.compute(_request(period=period))

    assert document.quality.data_quality.value == "PARTIAL"
    assert any(
        "missing kline" in reason and period in reason
        for reason in document.quality.quality_reasons
    )


def test_orphan_agg_trade_part_is_not_read_by_the_metric_core(tmp_path: Path) -> None:
    from oft.domain.events import AggTrade

    harness = _harness(tmp_path)
    orphan = AggTrade(
        symbol="BTCUSDT",
        agg_trade_id=1,  # duplicate of a manifest-registered row
        price=Decimal("70000.00"),
        quantity=Decimal("9.99"),
        first_trade_id=1,
        last_trade_id=1,
        transact_time=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        is_buyer_maker=False,
    )
    harness.parquet.write_agg_trades((orphan,), symbol="BTCUSDT", source="archive:orphan")

    document = harness.service.compute(_request())

    assert document.quality.data_quality.value == "PARTIAL"
    assert len(document.cvd.points) == 7
    assert document.cvd.summary.delta == Decimal("1.40")


def test_metrics_manifest_rows_carry_version_hash_and_window(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    persisted = harness.service.compute_and_persist(_request())

    entry = persisted.entry
    assert entry.metric_version == persisted.document.metric_version
    assert entry.input_manifest_hash == persisted.document.input_manifest_hash
    assert entry.window_start == "2026-10-01T00:00:00.000Z"
    assert entry.window_end == "2026-10-08T00:00:00.000Z"
    row = next(item for item in harness.store.list_manifest() if item.path == entry.path)
    assert row.metric_version == entry.metric_version
    assert row.input_manifest_hash == entry.input_manifest_hash
    assert row.window_start == entry.window_start
    assert row.window_end == entry.window_end


def test_read_metrics_pushes_the_window_filter_down(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    full = harness.service.compute_and_persist(_request())
    short_end = WINDOW_START + timedelta(days=3)
    harness.service.compute_and_persist(_request(window_end=short_end))

    rows = read_metrics(
        harness.settings,
        symbol="BTCUSDT",
        period="4h",
        metric_version=full.document.metric_version,
        window_start=WINDOW_START,
        window_end=short_end,
    )

    assert len(rows) == 24  # 3 CVD bars + 21 volume-profile bins
    assert {row["window_end"] for row in rows} == {datetime(2026, 10, 4, tzinfo=UTC)}


def test_conflicting_rows_in_one_run_raise_instead_of_silently_dropping(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    harness = _harness(tmp_path)
    first = harness.service.compute_and_persist(_request())
    rows = list(first.document.rows)
    rows[0] = replace(rows[0], delta=(rows[0].delta or Decimal("0")) + Decimal("1"))
    harness.store.upsert_manifest_entry(
        harness.parquet.write_metrics_part(
            rows,
            symbol="BTCUSDT",
            period="4h",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            metric_version=first.document.metric_version,
            input_manifest_hash=first.document.input_manifest_hash,
            computed_at=first.document.computed_at,
            data_quality=first.document.quality.data_quality.value,
            quality_reasons=(),
        )
    )

    with pytest.raises(ValueError, match="conflicting"):
        read_metrics(
            harness.settings,
            symbol="BTCUSDT",
            period="4h",
            metric_version=first.document.metric_version,
        )


def test_unavailable_metrics_root_does_not_erase_manifest_rows(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    first = harness.service.compute_and_persist(_request())
    root = harness.settings.data_dir / "metrics" / "orderflow"
    moved = harness.settings.data_dir / "metrics-away"
    root.rename(moved)
    try:
        fresh = MetricService(
            settings=harness.settings,
            store=harness.store,
            parquet=harness.parquet,
            clock=FixedClock(NOW),
        )
        fresh.compute_and_persist(_request())
        assert any(entry.path == first.entry.path for entry in harness.store.list_manifest())
    finally:
        if not root.exists() and moved.exists():
            moved.rename(root)


def test_unreadable_manifest_keeps_the_published_part(tmp_path: Path) -> None:
    from oft.ports.store import StoreUnavailableError

    harness = _harness(tmp_path)
    first = harness.service.compute_and_persist(_request())

    def fail_upsert(_entry: object) -> None:
        raise StoreUnavailableError("manifest db unavailable")

    def fail_get(_path: str) -> object:
        raise StoreUnavailableError("manifest db unavailable")

    harness.store.upsert_manifest_entry = fail_upsert  # type: ignore[method-assign]
    harness.store.get_manifest_entry = fail_get  # type: ignore[method-assign]
    later = MetricService(
        settings=harness.settings,
        store=harness.store,
        parquet=harness.parquet,
        clock=FixedClock(NOW + timedelta(hours=1)),
    )

    with pytest.raises(StoreUnavailableError):
        later.compute_and_persist(_request())

    assert Path(first.entry.path).is_file()


def test_missing_part_directory_is_drift_and_keeps_the_manifest_row(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    first = harness.service.compute_and_persist(_request())
    shutil.rmtree(Path(first.entry.path).parent)

    later = MetricService(
        settings=harness.settings,
        store=harness.store,
        parquet=harness.parquet,
        clock=FixedClock(NOW + timedelta(hours=1)),
    )
    with structlog.testing.capture_logs() as logs:
        later.compute_and_persist(_request())

    rows = [
        entry
        for entry in harness.store.list_manifest()
        if entry.data_type == "metrics" and entry.path == first.entry.path
    ]
    assert len(rows) == 1  # drift: manifest row preserved, never silently deleted
    assert any(event["event"] == "metrics_manifest_drift" for event in logs)


def test_missing_window_persists_a_zero_row_part(tmp_path: Path) -> None:
    harness = _harness(tmp_path, backfill=False)

    persisted = harness.service.compute_and_persist(_request())

    assert persisted.document.quality.data_quality.value == "MISSING"
    assert persisted.entry.rows == 0
    assert Path(persisted.entry.path).is_file()
    assert (
        read_metrics(
            harness.settings,
            symbol="BTCUSDT",
            period="4h",
            metric_version=persisted.document.metric_version,
        )
        == ()
    )


def test_kline_fixture_without_a_period_column_is_a_schema_error(
    tmp_path: Path,
) -> None:
    fixture = ROOT / "tests" / "fixtures" / "market" / "btcusdt_klines_4h.csv"
    lines = fixture.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    period_index = header.index("period")
    header.pop(period_index)
    rewritten = [",".join(header)]
    for line in lines[1:]:
        fields = line.split(",")
        fields.pop(period_index)
        rewritten.append(",".join(fields))
    broken = tmp_path / "klines_without_period.csv"
    broken.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    harness = _harness(tmp_path)
    settings = harness.settings.model_copy(update={"klines_fixture": broken})
    service = MetricService(
        settings=settings,
        store=harness.store,
        parquet=harness.parquet,
        clock=FixedClock(NOW),
    )

    with pytest.raises(ValueError, match="period"):
        service.compute(_request())


def test_sole_orphan_part_does_not_produce_a_false_complete_quality(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    for entry in harness.store.list_manifest(symbol="BTCUSDT", date="2026-10-02"):
        harness.store.delete_manifest_entry(entry.path)

    document = harness.service.compute(_request())

    assert document.quality.data_quality.value == "PARTIAL"
    assert any("2026-10-02" in reason for reason in document.quality.quality_reasons)
    assert len(document.cvd.points) == 6


def test_unmeasured_open_interest_and_funding_are_marked(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    document = harness.service.compute(_request())

    assert "open_interest not measured in this slice" in document.quality.quality_reasons
    assert "funding not measured in this slice" in document.quality.quality_reasons
    payload = document.to_dict()
    assert payload["open_interest"] is not None
    assert payload["funding"] is not None


def test_unregistered_part_does_not_shadow_a_published_run(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    published = harness.service.compute_and_persist(_request())
    # A later, unregistered part with a different provenance hash.
    harness.parquet.write_metrics_part(
        published.document.rows,
        symbol="BTCUSDT",
        period="4h",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        metric_version=published.document.metric_version,
        input_manifest_hash="e" * 64,
        computed_at="2026-11-07T12:00:00.000Z",
        data_quality="COMPLETE",
        quality_reasons=(),
    )

    rows = read_metrics(
        harness.settings,
        symbol="BTCUSDT",
        period="4h",
        metric_version=published.document.metric_version,
        store=harness.store,
    )

    assert len(rows) == 28
    assert {row["input_manifest_hash"] for row in rows} == {published.document.input_manifest_hash}


def test_same_input_hash_but_different_quality_inputs_does_not_conflict(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    document = harness.service.compute(_request())
    common = {
        "symbol": "BTCUSDT",
        "period": "4h",
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "metric_version": document.metric_version,
        "input_manifest_hash": document.input_manifest_hash,
        "data_quality": "PARTIAL",
    }
    harness.store.upsert_manifest_entry(
        harness.parquet.write_metrics_part(
            document.rows,
            computed_at="2026-11-06T12:00:00.000Z",
            quality_reasons=("kline fixture clean",),
            **common,
        )
    )
    harness.store.upsert_manifest_entry(
        harness.parquet.write_metrics_part(
            document.rows,
            computed_at="2026-11-06T13:00:00.000Z",
            quality_reasons=("missing kline cross-check input for period 4h",),
            **common,
        )
    )

    rows = read_metrics(
        harness.settings,
        symbol="BTCUSDT",
        period="4h",
        metric_version=document.metric_version,
        store=harness.store,
    )

    assert len(rows) == 28  # one part selected, no permanent conflict
    assert {row["quality_reasons"][0] for row in rows} == {
        "missing kline cross-check input for period 4h"
    }


def test_gaps_are_filtered_to_the_request_window(tmp_path: Path) -> None:
    from oft.domain.data_quality import DataGap, GapSeverity

    harness = _harness(tmp_path)
    harness.store.upsert_data_gap(
        DataGap(
            gap_id="GAP-BTCUSDT-aggTrades-2026-11-30",
            symbol="BTCUSDT",
            data_type="aggTrades",
            start="2026-11-30T00:00:00.000Z",
            end="2026-11-30T23:59:59.999Z",
            reason="ARCHIVE_FILE_MISSING",
            severity=GapSeverity.CRITICAL,
            detected_at="2026-11-06T12:00:00.000Z",
            source="BACKFILL",
        )
    )

    document = harness.service.compute(_request())

    assert document.to_dict()["gaps"] == []


def test_read_metrics_requires_a_readable_manifest_database(tmp_path: Path) -> None:
    from oft.config import Settings as _Settings
    from oft.ports.store import StoreUnavailableError

    settings = _Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)

    with pytest.raises(StoreUnavailableError):
        read_metrics(
            settings,
            symbol="BTCUSDT",
            period="4h",
            metric_version="orderflow_metrics.v1.801bf086c70213df",
        )
