"""Report-backed snapshot provider: parsing, quality, and unavailable cases."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from oft.application.trigger_snapshot import ReportSnapshotProvider, visible_bars
from oft.domain.opportunities import Condition
from oft.domain.time import utc_timestamp
from oft.domain.trigger import evaluate_trigger

AS_OF = "2026-11-06T12:00:00.000Z"
AS_OF_INSTANT = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
BEFORE = "2026-11-06T11:00:00.000Z"
BEFORE_END = "2026-11-06T11:05:00.000Z"
AFTER = "2026-11-06T11:55:00.000Z"
AFTER_END = "2026-11-06T12:05:00.000Z"


def _opportunity(report_id: str | None = "RPT-1") -> Any:
    source = {} if report_id is None else {"report_id": report_id}
    return SimpleNamespace(
        opportunity_id="OPP-1",
        symbol="BTCUSDT",
        source=source,
    )


def _store(snapshot: Any) -> Any:
    report = (
        None
        if snapshot is None
        else SimpleNamespace(
            snapshot_json=(snapshot if isinstance(snapshot, str) else json.dumps(snapshot))
        )
    )
    return SimpleNamespace(get_report=lambda _report_id: report)


def _snapshot(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "scheme_version": "1.0.0",
        "snapshot_id": "SNAP-REPORT",
        "symbol": "BTCUSDT",
        "as_of": AS_OF,
        "data_quality": "COMPLETE",
        "quality_reasons": [],
        "last_price": "70000.00",
        "bars": [
            {
                "bar_start": BEFORE,
                "bar_end": BEFORE_END,
                "close": "69900.00",
                "volume": "10",
                "delta": "1",
                "cvd": "10",
                "open_interest": "100",
            }
        ],
        "contract_specs": {"tick_size": "0.10"},
        "orderflow": {},
        "session": {"underlying_session": "REGULAR", "is_no_trading": False},
    }
    base.update(overrides)
    return base


def _provider(snapshot: Any) -> ReportSnapshotProvider:
    return ReportSnapshotProvider(store=_store(snapshot))


def test_provider_preserves_as_of_quality_and_snapshot_id() -> None:
    frozen = _provider(_snapshot()).snapshot_for(_opportunity())

    assert frozen is not None
    assert frozen.symbol == "BTCUSDT"
    assert frozen.as_of == AS_OF
    assert frozen.data_quality == "COMPLETE"
    assert frozen.snapshot_id == "SNAP-REPORT"
    assert frozen.last_price is not None and str(frozen.last_price) == "70000.00"
    assert frozen.tick_size is not None


def test_provider_maps_an_unknown_quality_to_missing() -> None:
    frozen = _provider(_snapshot(data_quality="WEIRD")).snapshot_for(_opportunity())

    assert frozen is not None
    assert frozen.data_quality == "MISSING"


def test_provider_keeps_quality_reasons() -> None:
    frozen = _provider(
        _snapshot(data_quality="STALE", quality_reasons=["metrics older than budget"])
    ).snapshot_for(_opportunity())

    assert frozen is not None
    assert frozen.quality_reasons == ("metrics older than budget",)


def test_provider_leaves_an_absent_snapshot_id_for_the_evaluator_to_derive() -> None:
    first = _provider(_snapshot(snapshot_id=None)).snapshot_for(_opportunity())
    second = _provider(_snapshot(snapshot_id=None)).snapshot_for(_opportunity())
    conditions = (
        Condition(
            condition_id="C-1",
            draft_id="c1",
            type="PRICE_TOUCH_ZONE",
            params={"price_low": "69900.00", "price_high": "70100.00"},
            window_start="2026-11-01T00:00:00.000Z",
            window_end="2026-12-01T00:00:00.000Z",
            required_data=("MARK_PRICE",),
            evaluable=True,
        ),
    )

    assert first is not None and second is not None
    # One hash-derived id scheme, shared by provider/manual/legacy paths.
    assert first.snapshot_id == second.snapshot_id == ""
    one = evaluate_trigger(conditions, first, now=AS_OF)
    two = evaluate_trigger(conditions, second, now=AS_OF)
    assert one.snapshot_id == two.snapshot_id
    assert one.snapshot_id.startswith("SNAP-")
    assert one.snapshot_id == f"SNAP-{one.snapshot_hash[:16].upper()}"


def test_provider_returns_none_when_the_report_id_is_missing() -> None:
    assert _provider(_snapshot()).snapshot_for(_opportunity(None)) is None


def test_provider_returns_none_when_the_report_is_missing() -> None:
    assert _provider(None).snapshot_for(_opportunity()) is None


def test_provider_returns_none_for_malformed_json() -> None:
    assert _provider("{not-json").snapshot_for(_opportunity()) is None


def test_provider_returns_none_for_a_non_object_snapshot() -> None:
    assert _provider(json.dumps([1, 2, 3])).snapshot_for(_opportunity()) is None


def test_provider_returns_none_for_a_missing_as_of() -> None:
    snapshot = _snapshot()
    snapshot.pop("as_of")

    assert _provider(snapshot).snapshot_for(_opportunity()) is None


def test_provider_returns_none_for_a_naive_as_of() -> None:
    assert _provider(_snapshot(as_of="2026-11-06T12:00:00")).snapshot_for(_opportunity()) is None


def test_provider_accepts_an_instant_at_the_ceiling() -> None:
    frozen = _provider(
        _snapshot(
            bars=[
                {
                    "bar_start": BEFORE,
                    "bar_end": AS_OF,
                    "close": "69900.00",
                    "volume": "10",
                    "delta": "1",
                    "cvd": "10",
                    "open_interest": "100",
                }
            ]
        )
    ).snapshot_for(_opportunity())

    assert frozen is not None
    assert frozen.bar_timestamps == (BEFORE,)


def test_visible_bars_drops_bars_after_the_ceiling() -> None:
    bars = [
        {"bar_start": BEFORE, "bar_end": BEFORE_END},
        {"bar_start": AFTER, "bar_end": AFTER_END},
        {"bar_start": "2026-11-06T11:30:00.000Z", "bar_end": None},
    ]

    visible = visible_bars(bars, AS_OF_INSTANT)

    assert visible == [{"bar_start": BEFORE, "bar_end": BEFORE_END}]


def test_visible_bars_fails_closed_for_non_list_input() -> None:
    assert visible_bars("not-a-list", AS_OF_INSTANT) == []
    assert visible_bars([{"bar_start": BEFORE}], AS_OF_INSTANT) == []


def test_visible_bars_fails_closed_when_a_bar_is_not_an_object() -> None:
    bars = [{"bar_end": BEFORE_END}, "not-a-bar"]

    assert visible_bars(bars, AS_OF_INSTANT) == []


def test_a_non_mapping_element_never_leaks_a_future_bar() -> None:
    # The future bar must not survive merely because another element is
    # malformed: fail closed with an empty list.
    bars = [
        {"bar_start": AFTER, "bar_end": AFTER_END},
        "not-a-bar",
    ]

    assert visible_bars(bars, AS_OF_INSTANT) == []


def test_a_snapshot_with_only_the_visible_bar_cannot_satisfy_a_two_bar_lookback() -> None:
    frozen = _provider(
        _snapshot(
            bars=[
                {
                    "bar_start": BEFORE,
                    "bar_end": BEFORE_END,
                    "close": "69900.00",
                    "volume": "10",
                    "delta": "1",
                    "cvd": "10",
                    "open_interest": "100",
                },
                {
                    "bar_start": AFTER,
                    "bar_end": AFTER_END,
                    "close": "70100.00",
                    "volume": "99",
                    "delta": "2",
                    "cvd": "20",
                    "open_interest": "102",
                },
            ]
        )
    ).snapshot_for(_opportunity())
    assert frozen is not None
    assert frozen.bar_timestamps == (BEFORE,)

    condition = Condition(
        condition_id="C-1",
        draft_id="c1",
        type="VOLUME_ABOVE",
        params={"threshold": "25", "lookback_bars": 2},
        window_start="2026-11-01T00:00:00.000Z",
        window_end="2026-12-01T00:00:00.000Z",
        required_data=("METRICS_5M",),
        evaluable=True,
    )
    evaluation = evaluate_trigger(
        (condition,),
        frozen,
        now=utc_timestamp(AS_OF_INSTANT),
        snapshot_id=frozen.snapshot_id,
    )

    assert evaluation.met is False
    assert evaluation.results[0].evaluable is False
