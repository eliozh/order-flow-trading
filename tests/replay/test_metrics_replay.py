"""Deterministic replay: same inputs/version -> same hash; new version -> new path.

The replay harness is the regression tool for metric changes. These tests pin
the three invariants from ADR-0011 and slice 10:

1. The same fixture window and ``metric_version`` produce an identical metric
   hash, and re-running is idempotent.
2. A different metric version writes a new output path and never edits the old
   document.
3. Replay writes only its own namespace and fails loudly on non-determinism.

The bottom section is the CI branch gate: every pure metric primitive stays at
100% branch coverage.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from oft.domain.events import AggTrade
from oft.domain.metrics.delta import compute_delta
from oft.domain.metrics.footprint import compute_footprint
from oft.domain.metrics.types import contract_json
from oft.domain.metrics.volume_profile import (
    _better_value_area,
    _value_area,
    compute_volume_profile,
)
from oft.replay.clock import ReplayClock
from oft.replay.event_source import (
    EventSourceError,
    FixtureEventSource,
    MemoryEventSource,
    ReplayWindow,
)
from oft.replay.harness import (
    ReplayConfig,
    ReplayHarness,
    ReplayNonDeterministicError,
)

pytestmark = pytest.mark.replay

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"
TICK_SIZE = Decimal("0.10")


def _load(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _orderflow_validator() -> Draft202012Validator:
    common = _load("common.schema.json")
    metrics = _load("orderflow-metrics.schema.json")
    resource = Resource.from_contents(common, default_specification=DRAFT202012)
    registry = Registry().with_resource(common["$id"], resource)
    return Draft202012Validator(metrics, registry=registry)


def _assert_valid(validator: Draft202012Validator, payload: dict[str, Any]) -> None:
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)


def _window(
    symbol: str = "BTCUSDT",
    start: datetime | None = None,
    end: datetime | None = None,
) -> ReplayWindow:
    return ReplayWindow(
        symbol=symbol,
        start=start or datetime(2025, 11, 5, tzinfo=UTC),
        end=end or datetime(2025, 11, 5, 4, tzinfo=UTC),
    )


def _config(*, cvd_algorithm_version: int = 1, window: ReplayWindow | None = None) -> ReplayConfig:
    resolved = window or _window()
    return ReplayConfig(
        symbol=resolved.symbol,
        period="4h",
        window_start=resolved.start,
        window_end=resolved.end,
        tick_size=TICK_SIZE,
        cvd_algorithm_version=cvd_algorithm_version,
    )


def _harness(
    tmp_path: Path,
    *,
    source: Any | None = None,
    config: ReplayConfig | None = None,
) -> ReplayHarness:
    resolved_config = config or _config()
    resolved_source = source or FixtureEventSource.from_csv(
        ROOT / "tests" / "fixtures" / "market" / "btcusdt_agg-trades_sample.csv",
        window=_window(),
    )
    return ReplayHarness(
        data_dir=tmp_path / "data",
        source=resolved_source,
        config=resolved_config,
    )


def _trade(
    trade_id: int,
    *,
    second: int = 0,
    symbol: str = "BTCUSDT",
    quantity: Decimal = Decimal("1"),
    is_buyer_maker: bool = False,
) -> AggTrade:
    return AggTrade(
        symbol=symbol,
        agg_trade_id=trade_id,
        price=Decimal("100"),
        quantity=quantity,
        first_trade_id=trade_id,
        last_trade_id=trade_id,
        transact_time=datetime(2025, 11, 5, tzinfo=UTC) + timedelta(seconds=second),
        is_buyer_maker=is_buyer_maker,
    )


def test_same_inputs_and_version_produce_an_identical_metric_hash(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    first = harness.run()
    second = harness.run()

    assert first.metric_hash == second.metric_hash
    assert first.input_manifest_hash == second.input_manifest_hash
    assert first.output_path == second.output_path
    assert second.idempotent is True
    assert Path(first.output_path).is_file()
    # Running twice does not create a second document.
    assert list((tmp_path / "data" / "replay").rglob("*.json")) == [Path(first.output_path)]


def test_different_metric_version_writes_a_new_path_and_never_edits(
    tmp_path: Path,
) -> None:
    first = _harness(tmp_path, config=_config(cvd_algorithm_version=1)).run()
    first_bytes = Path(first.output_path).read_bytes()
    first_record = json.loads(first_bytes)
    _assert_valid(_orderflow_validator(), first_record["document"])

    second = _harness(tmp_path, config=_config(cvd_algorithm_version=2)).run()

    assert second.output_path != first.output_path
    assert second.metric_version != first.metric_version
    # The old version is untouched: metrics are immutable.
    assert Path(first.output_path).read_bytes() == first_bytes
    assert Path(second.output_path).is_file()
    assert json.loads(Path(second.output_path).read_text())["document"]["metric_version"] == (
        second.metric_version
    )


def test_different_input_manifest_writes_a_new_path(tmp_path: Path) -> None:
    full = _harness(tmp_path).run()
    shorter_source = MemoryEventSource(window=_window(), events=(_trade(1),))
    shorter = _harness(tmp_path, source=shorter_source).run()

    assert shorter.input_manifest_hash != full.input_manifest_hash
    assert shorter.output_path != full.output_path


def test_replay_writes_only_its_namespace_and_leaves_live_state_untouched(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    live_db = data_dir / "oft.db"
    live_db.parent.mkdir(parents=True)
    live_db.write_bytes(b"live-daemon-state")
    live_part = data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT" / "part-0.parquet"
    live_part.parent.mkdir(parents=True)
    live_part.write_bytes(b"live-part")
    api_token = data_dir / "run" / "api-token"
    api_token.parent.mkdir(parents=True)
    api_token.write_text("live-token", encoding="utf-8")
    before = {path: path.read_bytes() for path in (live_db, live_part, api_token)}

    result = _harness(tmp_path).run()

    replay_root = (data_dir / "replay").resolve()
    produced = sorted(path for path in data_dir.rglob("*") if path.is_file() and path != live_db)
    new_files = [path for path in produced if path not in before]
    assert new_files, "replay must write its output"
    assert Path(result.output_path).resolve().is_relative_to(replay_root)
    for path in new_files:
        assert path.resolve().is_relative_to(replay_root), path
    for path, content in before.items():
        assert path.read_bytes() == content


def test_replay_document_validates_against_the_orderflow_schema(tmp_path: Path) -> None:
    result = _harness(tmp_path).run()
    record = json.loads(Path(result.output_path).read_text(encoding="utf-8"))
    document = record["document"]

    _assert_valid(_orderflow_validator(), document)
    assert record["metric_hash"] == result.metric_hash
    assert document["metric_version"] == result.metric_version
    assert document["input_manifest_hash"] == result.input_manifest_hash
    assert document["sources"][0]["kind"] == "REPLAY"
    assert document["symbol"] == "BTCUSDT"
    assert document["period"] == "4h"
    assert document["data_quality"] == "PARTIAL"
    assert "open_interest not measured in this slice" in document["quality_reasons"]


def test_replay_uses_event_time_not_the_wall_clock(tmp_path: Path) -> None:
    result = _harness(tmp_path).run()

    # The sample's last event is 2025-11-05T00:00:05Z; no lookahead is allowed.
    assert result.computed_at == "2025-11-05T00:00:05.000Z"
    assert result.event_count == 6


def test_replay_fails_loudly_on_nondeterministic_computation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.replay import harness as harness_module

    original = harness_module.compute_cvd
    calls = {"count": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] % 2 == 0:
            return replace(
                result,
                summary=replace(result.summary, delta=result.summary.delta + Decimal("1")),
            )
        return result

    monkeypatch.setattr(harness_module, "compute_cvd", flaky)

    with pytest.raises(ReplayNonDeterministicError):
        _harness(tmp_path).run()


def test_replay_refuses_to_overwrite_a_conflicting_document(tmp_path: Path) -> None:
    result = _harness(tmp_path).run()
    path = Path(result.output_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["metric_hash"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(ReplayNonDeterministicError):
        _harness(tmp_path).run()

    assert path.read_bytes() == tampered


def test_replay_empty_window_reports_missing_quality(tmp_path: Path) -> None:
    window = _window(end=datetime(2025, 11, 6, tzinfo=UTC))
    config = _config(window=window)
    source = MemoryEventSource(window=window, events=())
    result = _harness(tmp_path, source=source, config=config).run()
    document = json.loads(Path(result.output_path).read_text(encoding="utf-8"))["document"]

    assert document["data_quality"] == "MISSING"
    assert document["cvd"]["points"] == []
    assert result.event_count == 0


def test_fixture_event_source_rejects_duplicate_and_out_of_window_events(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.csv"
    duplicate.write_text(
        "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
        "1,100,1,1,1,1762300800000,false\n"
        "1,100,1,1,1,1762300801000,true\n",
        encoding="utf-8",
    )
    with pytest.raises(EventSourceError):
        tuple(FixtureEventSource.from_csv(duplicate, window=_window()).iter_events())

    outside = tmp_path / "outside.csv"
    outside.write_text(
        "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
        "1,100,1,1,1,1762300800000,false\n",
        encoding="utf-8",
    )
    with pytest.raises(EventSourceError):
        tuple(
            FixtureEventSource.from_csv(
                outside,
                window=_window(
                    start=datetime(2025, 11, 5, 1, tzinfo=UTC),
                    end=datetime(2025, 11, 5, 4, tzinfo=UTC),
                ),
            ).iter_events()
        )


def test_replay_clock_never_moves_backwards() -> None:
    clock = ReplayClock(datetime(2025, 11, 5, tzinfo=UTC))
    later = datetime(2025, 11, 5, 0, 0, 5, tzinfo=UTC)

    assert clock.advance_to(later) == later
    assert clock.now() == later
    with pytest.raises(ValueError):
        clock.advance_to(datetime(2025, 11, 5, tzinfo=UTC) - timedelta(seconds=1))
    with pytest.raises(ValueError):
        ReplayClock(datetime(2025, 11, 5))


# -- CI branch gate: pure metric primitives ---------------------------------


def test_delta_rejects_naive_and_inverted_windows() -> None:
    trade = _trade(1)
    with pytest.raises(ValueError):
        compute_delta(
            (trade,),
            symbol="BTCUSDT",
            period="4h",
            window_start=datetime(2025, 11, 5),
            window_end=datetime(2025, 11, 5, 4),
        )
    with pytest.raises(ValueError):
        compute_delta(
            (trade,),
            symbol="BTCUSDT",
            period="4h",
            window_start=datetime(2025, 11, 5, 4, tzinfo=UTC),
            window_end=datetime(2025, 11, 5, tzinfo=UTC),
        )


@pytest.mark.parametrize("value", [True, "3"])
def test_footprint_rejects_non_integer_top_imbalances(value: object) -> None:
    with pytest.raises(ValueError):
        compute_footprint(
            (_trade(1),),
            symbol="BTCUSDT",
            period="4h",
            window_start=datetime(2025, 11, 5, tzinfo=UTC),
            window_end=datetime(2025, 11, 5, 4, tzinfo=UTC),
            tick_size=TICK_SIZE,
            top_imbalances=value,  # type: ignore[arg-type]
        )


def test_volume_profile_value_area_tie_breaks_are_deterministic() -> None:
    # Equal-length ranges: the one containing the POC wins.
    assert _better_value_area((0, 1), (2, 3), poc_index=0) is True
    assert _better_value_area((2, 3), (0, 1), poc_index=0) is False
    # Zero total volume: the value area collapses onto the POC.
    assert _value_area((), 0, Decimal("70")) == (0, 0)

    trades = (_trade(1, quantity=Decimal("2")),)
    profile = compute_volume_profile(
        trades,
        symbol="BTCUSDT",
        window_start=datetime(2025, 11, 5, tzinfo=UTC),
        window_end=datetime(2025, 11, 5, 4, tzinfo=UTC),
        tick_size=TICK_SIZE,
    )
    assert profile.poc == Decimal("100.00")


def test_contract_json_rejects_unsupported_types() -> None:
    with pytest.raises(TypeError):
        contract_json(object())


def test_concurrent_replays_of_the_same_output_are_safe(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    harnesses = [_harness(tmp_path) for _ in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda item: item.run(), harnesses))

    assert len({result.metric_hash for result in results}) == 1
    output = Path(results[0].output_path)
    assert output.is_file()
    assert not list(output.parent.glob("*.tmp"))
    assert not list(output.parent.glob(".*.tmp"))


def test_tampered_document_is_not_treated_as_idempotent(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    result = harness.run()
    output = Path(result.output_path)
    text = output.read_text(encoding="utf-8")
    output.write_text(
        text.replace('"symbol":"BTCUSDT"', '"symbol":"TAMPERED"', 1), encoding="utf-8"
    )

    with pytest.raises(ReplayNonDeterministicError, match="tampered|different metric hash"):
        harness.run()


def test_existing_non_object_replay_document_fails_cleanly(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    result = harness.run()
    Path(result.output_path).write_text("[]", encoding="utf-8")

    with pytest.raises(ReplayNonDeterministicError, match="not a JSON object"):
        harness.run()
