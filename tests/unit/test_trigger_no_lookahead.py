"""No-lookahead: the trigger path can only see bars that closed at or before as_of."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from oft.application.trigger_snapshot import ReportSnapshotProvider
from oft.domain.opportunities import Condition
from oft.domain.time import utc_timestamp
from oft.domain.trigger import evaluate_trigger

AS_OF_INSTANT = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
AS_OF = utc_timestamp(AS_OF_INSTANT)
BEFORE = "2026-11-06T11:00:00.000Z"
BEFORE_END = "2026-11-06T11:05:00.000Z"
AFTER = "2026-11-06T11:55:00.000Z"
AFTER_END = "2026-11-06T12:05:00.000Z"


def _provider(snapshot: dict[str, Any]) -> ReportSnapshotProvider:
    report = SimpleNamespace(snapshot_json=json.dumps(snapshot))
    store = SimpleNamespace(get_report=lambda _report_id: report)
    return ReportSnapshotProvider(store=store)


def _opportunity() -> Any:
    return SimpleNamespace(opportunity_id="OPP-1", symbol="BTCUSDT", source={"report_id": "RPT-1"})


def _snapshot() -> dict[str, Any]:
    return {
        "snapshot_id": "SNAP-REPORT",
        "as_of": AS_OF,
        "data_quality": "COMPLETE",
        "last_price": "69900.00",
        "bars": [
            {
                "bar_start": BEFORE,
                "bar_end": BEFORE_END,
                "close": "69900.00",
                "volume": "10",
                "delta": "100",
                "cvd": "10",
                "open_interest": "100",
            },
            {
                "bar_start": AFTER,
                "bar_end": AFTER_END,
                "close": "68000.00",
                "volume": "5000",
                "delta": "-1000",
                "cvd": "20",
                "open_interest": "50",
            },
        ],
        "contract_specs": {"tick_size": "0.10"},
        "orderflow": {},
        "session": {"underlying_session": "REGULAR", "is_no_trading": False},
    }


def _condition(condition_type: str, params: dict[str, object]) -> Condition:
    return Condition(
        condition_id="C-1",
        draft_id="c1",
        type=condition_type,
        params=params,
        window_start="2026-11-01T00:00:00.000Z",
        window_end="2026-12-01T00:00:00.000Z",
        required_data=("METRICS_5M",),
        evaluable=True,
    )


def test_future_bar_is_never_visible_to_the_evaluator() -> None:
    frozen = _provider(_snapshot()).snapshot_for(_opportunity())
    assert frozen is not None

    assert frozen.bar_timestamps == (BEFORE,)
    assert frozen.volume_series == (10,)
    assert frozen.delta_series == (100,)


def test_a_future_bar_cannot_make_a_two_bar_condition_met() -> None:
    frozen = _provider(_snapshot()).snapshot_for(_opportunity())
    assert frozen is not None

    evaluation = evaluate_trigger(
        (_condition("DELTA_BELOW", {"threshold": "0", "lookback_bars": 2}),),
        frozen,
        now=AS_OF,
        snapshot_id=frozen.snapshot_id,
    )

    # With the future bar the summed delta would be -900 and "met"; because the
    # bar is dropped the lookback is short and the condition is unevaluable.
    assert evaluation.met is False
    assert evaluation.results[0].evaluable is False


def test_a_bar_that_closes_after_as_of_is_excluded_even_when_it_starts_before() -> None:
    snapshot = _snapshot()
    snapshot["bars"][1]["bar_start"] = "2026-11-06T11:59:00.000Z"
    snapshot["bars"][1]["bar_end"] = "2026-11-06T12:01:00.000Z"

    frozen = _provider(snapshot).snapshot_for(_opportunity())

    assert frozen is not None
    assert frozen.bar_timestamps == (BEFORE,)


def test_a_malformed_bar_end_is_excluded_rather_than_assumed_future() -> None:
    snapshot = _snapshot()
    snapshot["bars"][1]["bar_end"] = "not-a-timestamp"

    frozen = _provider(snapshot).snapshot_for(_opportunity())

    assert frozen is not None
    assert frozen.bar_timestamps == (BEFORE,)
