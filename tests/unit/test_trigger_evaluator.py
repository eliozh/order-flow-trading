"""Deterministic PRICE_TOUCH_ZONE evaluator and no-stale rule."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

import pytest

from oft.domain.opportunities import Condition
from oft.domain.trigger import (
    evaluate_condition,
    evaluate_trigger,
    snapshot_hash,
)

WINDOW = {"start": "2026-11-01T00:00:00.000Z", "end": "2026-12-01T00:00:00.000Z"}


def _condition(
    *,
    low: str = "69900.00",
    high: str = "70100.00",
    condition_type: str = "PRICE_TOUCH_ZONE",
    evaluable: bool = True,
) -> Condition:
    return Condition(
        condition_id="C-1",
        draft_id="c1",
        type=condition_type,
        params={
            "price": None,
            "price_low": low,
            "price_high": high,
            "threshold": None,
            "unit": "PRICE",
            "comparator": "BETWEEN",
            "lookback_bars": None,
            "session_states": [],
            "note": "",
        },
        window_start=WINDOW["start"],
        window_end=WINDOW["end"],
        required_data=("MARK_PRICE",),
        evaluable=evaluable,
    )


def test_price_touch_zone_met_at_both_boundaries() -> None:
    for price in ("69900.00", "70000.00", "70100.00"):
        result = evaluate_condition(
            _condition(),
            last_price=Decimal(price),
            evaluated_at="2026-11-06T12:00:00.000Z",
            data_quality="COMPLETE",
        )
        assert result.met is True
        assert result.observed_value == price
        assert result.threshold == "69900.00-70100.00"
        assert result.evaluable is True


def test_price_touch_zone_not_met_outside_the_zone() -> None:
    below = evaluate_condition(
        _condition(),
        last_price=Decimal("69899.99"),
        evaluated_at="t",
        data_quality="COMPLETE",
    )
    above = evaluate_condition(
        _condition(),
        last_price=Decimal("70100.01"),
        evaluated_at="t",
        data_quality="COMPLETE",
    )

    assert below.met is False
    assert above.met is False


def test_non_evaluable_condition_is_never_met() -> None:
    result = evaluate_condition(
        _condition(evaluable=False),
        last_price=Decimal("70000.00"),
        evaluated_at="t",
        data_quality="MISSING",
    )

    assert result.met is False
    assert result.evaluable is False
    assert "not evaluable" in result.note


def test_unsupported_condition_is_not_evaluable() -> None:
    result = evaluate_condition(
        _condition(condition_type="CVD_ABOVE"),
        last_price=Decimal("70000.00"),
        evaluated_at="t",
        data_quality="COMPLETE",
    )

    assert result.evaluable is False
    assert result.met is False
    assert "unsupported" in result.note


@pytest.mark.parametrize("low", [None, "", "not-a-number", "NaN"])
def test_missing_or_invalid_zone_parameters_are_not_evaluable(low: str | None) -> None:
    condition = _condition()
    object.__setattr__(
        condition,
        "params",
        {**condition.params, "price_low": low},
    )

    result = evaluate_condition(
        condition,
        last_price=Decimal("70000.00"),
        evaluated_at="t",
        data_quality="COMPLETE",
    )

    assert result.evaluable is False
    assert result.met is False


def test_stale_snapshot_produces_no_trigger() -> None:
    evaluation = evaluate_trigger(
        (_condition(),),
        last_price=Decimal("70000.00"),
        snapshot_as_of="2026-11-06T11:58:00.000Z",
        now="2026-11-06T12:00:00.000Z",
        stale_after_seconds=60,
        data_quality="STALE",
        snapshot_id="SNAP-1",
    )

    assert evaluation.met is False
    assert evaluation.stale is True
    assert evaluation.data_quality == "STALE"
    assert evaluation.results[0].met is False
    assert "stale" in evaluation.reasons[0]


def test_fresh_snapshot_triggers_only_when_every_condition_is_met() -> None:
    met = evaluate_trigger(
        (_condition(),),
        last_price=Decimal("70000.00"),
        snapshot_as_of="2026-11-06T11:59:30.000Z",
        now="2026-11-06T12:00:00.000Z",
        stale_after_seconds=60,
        data_quality="COMPLETE",
        snapshot_id="SNAP-1",
    )
    not_met = evaluate_trigger(
        (_condition(),),
        last_price=Decimal("65000.00"),
        snapshot_as_of="2026-11-06T11:59:30.000Z",
        now="2026-11-06T12:00:00.000Z",
        stale_after_seconds=60,
        data_quality="COMPLETE",
        snapshot_id="SNAP-1",
    )

    assert met.met is True
    assert met.stale is False
    assert not_met.met is False
    assert not_met.stale is False


def test_empty_condition_set_never_triggers() -> None:
    evaluation = evaluate_trigger(
        (),
        last_price=Decimal("70000.00"),
        snapshot_as_of="2026-11-06T11:59:30.000Z",
        now="2026-11-06T12:00:00.000Z",
        stale_after_seconds=60,
        data_quality="COMPLETE",
        snapshot_id="SNAP-1",
    )

    assert evaluation.met is False


def test_snapshot_hash_is_canonical_and_sensitive() -> None:
    payload = {"snapshot_id": "SNAP-1", "last_price": "70000.00", "as_of": "t"}
    reordered = {"as_of": "t", "last_price": "70000.00", "snapshot_id": "SNAP-1"}

    assert snapshot_hash(payload) == snapshot_hash(reordered)
    assert snapshot_hash({**payload, "last_price": "70001.00"}) != snapshot_hash(payload)


def test_condition_result_serializes_for_the_contract() -> None:
    result = evaluate_condition(
        _condition(),
        last_price=Decimal("70000.00"),
        evaluated_at="t",
        data_quality="COMPLETE",
    )
    payload = asdict(result)

    assert payload["condition_id"] == "C-1"
    assert payload["type"] == "PRICE_TOUCH_ZONE"
    assert payload["met"] is True


def test_snapshot_hash_commits_to_the_symbol() -> None:
    args = {
        "conditions": (_condition(),),
        "last_price": Decimal("70000.00"),
        "snapshot_as_of": "2026-11-06T12:00:00.000Z",
        "now": "2026-11-06T12:00:00.000Z",
        "data_quality": "COMPLETE",
        "snapshot_id": "SNAP-1",
    }

    btc = evaluate_trigger(**args, symbol="BTCUSDT")
    eth = evaluate_trigger(**args, symbol="ETHUSDT")

    assert btc.snapshot_hash != eth.snapshot_hash
