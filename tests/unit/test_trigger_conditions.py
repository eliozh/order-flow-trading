"""The trigger evaluator runs the full slice-16 condition registry, deterministically."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

import pytest

from oft.domain.conditions import ConditionType
from oft.domain.opportunities import Condition
from oft.domain.trigger import (
    TriggerSnapshot,
    contract_data_quality,
    evaluate_trigger,
    evaluation_context,
    snapshot_hash,
)

NOW = "2026-11-06T12:00:00.000Z"
WINDOW_START = "2026-11-01T00:00:00.000Z"
WINDOW_END = "2026-12-01T00:00:00.000Z"
BAR_ONE = "2026-11-05T00:00:00.000Z"
BAR_TWO = "2026-11-05T00:05:00.000Z"
BAR_THREE = "2026-11-05T00:10:00.000Z"
BARS = (BAR_ONE, BAR_TWO)


def condition(
    condition_type: str | ConditionType,
    params: dict[str, object],
    *,
    condition_id: str = "C-1",
    evaluable: bool = True,
    window_start: str = WINDOW_START,
    window_end: str = WINDOW_END,
) -> Condition:
    return Condition(
        condition_id=condition_id,
        draft_id="c1",
        type=str(condition_type),
        params=params,
        window_start=window_start,
        window_end=window_end,
        required_data=("MARK_PRICE",),
        evaluable=evaluable,
    )


def snapshot(**overrides: object) -> TriggerSnapshot:
    base: dict[str, object] = {
        "symbol": "BTCUSDT",
        "as_of": NOW,
        "last_price": Decimal("70000.00"),
        "previous_price": Decimal("69900.00"),
        "tick_size": Decimal("0.10"),
        "bar_timestamps": BARS,
        "price_series": (Decimal("69950.00"), Decimal("70000.00")),
        "delta_series": (Decimal("5"), Decimal("6")),
        "cvd_series": (Decimal("60"), Decimal("100")),
        "volume_series": (Decimal("10"), Decimal("20")),
        "oi_series": (Decimal("100"), Decimal("102")),
        "delta": Decimal("11"),
        "cvd": Decimal("100"),
        "oi_delta_pct": Decimal("2"),
        "funding_rate": Decimal("0.0001"),
        "liquidation_notional": Decimal("1000"),
        "volume": Decimal("30"),
        "footprint_imbalance": Decimal("0.7"),
        "session_state": "REGULAR",
        "data_quality": "COMPLETE",
        "snapshot_id": "SNAP-1",
    }
    base.update(overrides)
    return TriggerSnapshot(**base)  # type: ignore[arg-type]


CASES: list[tuple[str, dict[str, object], dict[str, object], bool]] = [
    ("PRICE_CROSS_ABOVE", {"price": "70000.00"}, {}, True),
    (
        "PRICE_CROSS_BELOW",
        {"price": "69950.00"},
        {"previous_price": Decimal("70000.00"), "last_price": Decimal("69900.00")},
        True,
    ),
    ("PRICE_INSIDE_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}, {}, True),
    ("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}, {}, True),
    ("PRICE_OUTSIDE_ZONE", {"price_low": "71000.00", "price_high": "72000.00"}, {}, True),
    (
        "DELTA_ABOVE",
        {"threshold": "10", "lookback_bars": 2},
        {"delta_series": (Decimal("5"), Decimal("6"))},
        True,
    ),
    (
        "DELTA_BELOW",
        {"threshold": "10", "lookback_bars": 2},
        {"delta_series": (Decimal("1"), Decimal("2"))},
        True,
    ),
    (
        "CVD_ABOVE",
        {"threshold": "50", "lookback_bars": 2},
        {"cvd_series": (Decimal("60"), Decimal("100"))},
        True,
    ),
    (
        "CVD_BELOW",
        {"threshold": "50", "lookback_bars": 2},
        {"cvd_series": (Decimal("60"), Decimal("40"))},
        True,
    ),
    (
        "CVD_DIVERGENCE",
        {"lookback_bars": 3},
        {
            "bar_timestamps": (BAR_ONE, BAR_TWO, BAR_THREE),
            "price_series": (Decimal("100"), Decimal("95"), Decimal("90")),
            "cvd_series": (Decimal("10"), Decimal("15"), Decimal("20")),
        },
        True,
    ),
    (
        "OI_DELTA_ABOVE",
        {"threshold": "1", "lookback_bars": 2},
        {"oi_series": (Decimal("100"), Decimal("102"))},
        True,
    ),
    (
        "OI_DELTA_BELOW",
        {"threshold": "0", "lookback_bars": 2},
        {"oi_series": (Decimal("100"), Decimal("99"))},
        True,
    ),
    (
        "FUNDING_ABOVE",
        {"threshold": "0.0005"},
        {"funding_rate": Decimal("0.001")},
        True,
    ),
    ("FUNDING_BELOW", {"threshold": "0.0005"}, {}, True),
    ("LIQUIDATION_NOTIONAL_ABOVE", {"threshold": "500"}, {}, True),
    (
        "VOLUME_ABOVE",
        {"threshold": "25", "lookback_bars": 2},
        {"volume_series": (Decimal("10"), Decimal("20"))},
        True,
    ),
    ("FOOTPRINT_IMBALANCE_ABOVE", {"threshold": "0.5"}, {}, True),
    ("SESSION_STATE", {"session_states": ["REGULAR"]}, {}, True),
    (
        "TIME_WINDOW",
        {},
        {},
        True,
    ),
]


@pytest.mark.parametrize(("condition_type", "params", "overrides", "expected"), CASES)
def test_every_contract_condition_type_evaluates_through_the_shared_registry(
    condition_type: str,
    params: dict[str, object],
    overrides: dict[str, object],
    expected: bool,
) -> None:
    window_start, window_end = WINDOW_START, WINDOW_END
    if condition_type == "TIME_WINDOW":
        window_start, window_end = "2026-11-06T11:00:00.000Z", "2026-11-06T13:00:00.000Z"

    evaluation = evaluate_trigger(
        (condition(condition_type, params, window_start=window_start, window_end=window_end),),
        snapshot(**overrides),
        now=NOW,
        snapshot_id="SNAP-1",
    )

    assert len(evaluation.results) == 1
    result = evaluation.results[0]
    assert result.type == condition_type
    assert result.evaluable is True
    assert result.met is expected
    assert evaluation.met is expected
    assert result.evaluated_at == NOW


def test_all_conditions_must_be_evaluable_and_met_to_trigger() -> None:
    snapshot_value = snapshot()
    conditions = (
        condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),
        condition("SESSION_STATE", {"session_states": ["REGULAR"]}, condition_id="C-2"),
        condition(
            "VOLUME_ABOVE",
            {"threshold": "25", "lookback_bars": 2},
            condition_id="C-3",
        ),
    )

    evaluation = evaluate_trigger(conditions, snapshot_value, now=NOW, snapshot_id="SNAP-1")

    assert evaluation.met is True
    assert [result.met for result in evaluation.results] == [True, True, True]
    assert evaluation.reasons == ()


def test_a_single_unmet_condition_blocks_the_trigger_and_names_itself() -> None:
    evaluation = evaluate_trigger(
        (
            condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),
            condition(
                "PRICE_OUTSIDE_ZONE",
                {"price_low": "69000.00", "price_high": "71000.00"},
                condition_id="C-2",
            ),
        ),
        snapshot(),
        now=NOW,
        snapshot_id="SNAP-1",
    )

    assert evaluation.met is False
    assert len(evaluation.reasons) == 1
    assert "C-2" in evaluation.reasons[0]


def test_evaluable_false_is_never_met_even_with_a_met_price_condition() -> None:
    evaluation = evaluate_trigger(
        (
            condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),
            condition(
                "CVD_ABOVE",
                {"threshold": "50", "lookback_bars": 2},
                condition_id="C-2",
                evaluable=False,
            ),
        ),
        snapshot(),
        now=NOW,
        snapshot_id="SNAP-1",
    )

    assert evaluation.met is False
    assert evaluation.results[1].evaluable is False
    assert evaluation.results[1].met is False
    assert any("C-2" in reason for reason in evaluation.reasons)


def test_missing_required_data_is_never_met() -> None:
    evaluation = evaluate_trigger(
        (condition("VOLUME_ABOVE", {"threshold": "25", "lookback_bars": 2}),),
        snapshot(volume_series=None),
        now=NOW,
        snapshot_id="SNAP-1",
    )

    assert evaluation.met is False
    assert evaluation.results[0].evaluable is False
    assert evaluation.results[0].met is False


def test_empty_condition_set_never_triggers() -> None:
    evaluation = evaluate_trigger((), snapshot(), now=NOW, snapshot_id="SNAP-1")

    assert evaluation.met is False
    assert evaluation.reasons == ("no trigger conditions are defined",)


def test_stale_mark_never_triggers_and_reports_stale() -> None:
    evaluation = evaluate_trigger(
        (condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),),
        snapshot(as_of="2026-11-06T11:58:00.000Z"),
        now=NOW,
        stale_after_seconds=60,
        snapshot_id="SNAP-1",
    )

    assert evaluation.met is False
    assert evaluation.stale is True
    assert evaluation.data_quality == "STALE"
    assert evaluation.results[0].evaluable is False
    assert evaluation.results[0].evaluated_at == NOW
    assert "stale snapshot" in evaluation.reasons[0]


def test_missing_last_price_is_stale_and_never_triggers() -> None:
    evaluation = evaluate_trigger(
        (condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),),
        snapshot(last_price=None),
        now=NOW,
        snapshot_id="SNAP-1",
    )

    assert evaluation.met is False
    assert evaluation.stale is True
    assert evaluation.data_quality == "STALE"
    assert "last_price" in evaluation.reasons[0]


def test_a_snapshot_marked_stale_never_triggers() -> None:
    evaluation = evaluate_trigger(
        (condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),),
        snapshot(data_quality="STALE", quality_reasons=("metrics older than budget",)),
        now=NOW,
        snapshot_id="SNAP-1",
    )

    assert evaluation.met is False
    assert evaluation.stale is True
    assert "metrics older than budget" in evaluation.reasons[0]


def test_a_stale_quality_without_reasons_still_blocks() -> None:
    evaluation = evaluate_trigger(
        (condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),),
        snapshot(data_quality="STALE", quality_reasons=()),
        now=NOW,
        snapshot_id="SNAP-1",
    )

    assert evaluation.stale is True
    assert evaluation.reasons == ("stale snapshot: data quality is STALE",)


def test_evaluate_trigger_requires_a_snapshot_or_a_price() -> None:
    with pytest.raises(ValueError):
        evaluate_trigger((), now=NOW, snapshot_id="SNAP-1")


def test_naive_timestamps_are_treated_as_utc() -> None:
    evaluation = evaluate_trigger(
        (condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),),
        last_price=Decimal("70000.00"),
        snapshot_as_of="2026-11-06T11:59:30",
        now="2026-11-06T12:00:00",
        snapshot_id="SNAP-NAIVE",
    )

    assert evaluation.stale is False
    assert evaluation.met is True


def test_malformed_as_of_is_stale() -> None:
    evaluation = evaluate_trigger(
        (condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),),
        snapshot(as_of="not-a-timestamp"),
        now=NOW,
        snapshot_id="SNAP-1",
    )

    assert evaluation.stale is True
    assert evaluation.met is False
    assert "malformed" in evaluation.reasons[0]


def test_snapshot_hash_is_deterministic_for_the_same_snapshot() -> None:
    conditions = (
        condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),
    )
    first = evaluate_trigger(conditions, snapshot(), now=NOW, snapshot_id="SNAP-1")
    second = evaluate_trigger(conditions, snapshot(), now=NOW, snapshot_id="SNAP-1")

    assert first.snapshot_hash == second.snapshot_hash
    assert first.results == second.results


def test_snapshot_hash_changes_with_prices_series_and_condition_params() -> None:
    conditions = (
        condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),
    )
    base = evaluate_trigger(conditions, snapshot(), now=NOW, snapshot_id="SNAP-1")
    price = evaluate_trigger(
        conditions,
        snapshot(last_price=Decimal("70001.00")),
        now=NOW,
        snapshot_id="SNAP-1",
    )
    interior = evaluate_trigger(
        conditions,
        snapshot(delta_series=(Decimal("5"), Decimal("7"))),
        now=NOW,
        snapshot_id="SNAP-1",
    )
    params = evaluate_trigger(
        (condition("PRICE_TOUCH_ZONE", {"price_low": "69800.00", "price_high": "70100.00"}),),
        snapshot(),
        now=NOW,
        snapshot_id="SNAP-1",
    )

    assert base.snapshot_hash != price.snapshot_hash
    assert base.snapshot_hash != interior.snapshot_hash
    assert base.snapshot_hash != params.snapshot_hash


def test_snapshot_hash_commits_to_the_symbol() -> None:
    conditions = (
        condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),
    )
    btc = evaluate_trigger(conditions, snapshot(symbol="BTCUSDT"), now=NOW, snapshot_id="S-1")
    eth = evaluate_trigger(conditions, snapshot(symbol="ETHUSDT"), now=NOW, snapshot_id="S-1")

    assert btc.snapshot_hash != eth.snapshot_hash


def test_snapshot_hash_changes_when_only_an_interior_series_point_changes() -> None:
    conditions = (
        condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),
    )
    base = evaluate_trigger(
        conditions,
        snapshot(
            bar_timestamps=(BAR_ONE, BAR_TWO, BAR_THREE),
            price_series=(Decimal("69950.00"), Decimal("70000.00"), Decimal("70010.00")),
        ),
        now=NOW,
        snapshot_id="SNAP-1",
    )
    interior = evaluate_trigger(
        conditions,
        snapshot(
            bar_timestamps=(BAR_ONE, BAR_TWO, BAR_THREE),
            price_series=(Decimal("69950.00"), Decimal("70500.00"), Decimal("70010.00")),
        ),
        now=NOW,
        snapshot_id="SNAP-1",
    )

    # Equal endpoints, different interior: the full series is hashed.
    assert base.snapshot_hash != interior.snapshot_hash


def test_stale_boundary_is_exact_and_a_future_as_of_is_stale() -> None:
    conditions = (
        condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),
    )
    at_budget = evaluate_trigger(
        conditions,
        snapshot(as_of="2026-11-06T11:59:00.000Z"),  # exactly 60 s old
        now=NOW,
        stale_after_seconds=60,
        snapshot_id="SNAP-1",
    )
    beyond_budget = evaluate_trigger(
        conditions,
        snapshot(as_of="2026-11-06T11:58:59.999Z"),  # 60 s + epsilon
        now=NOW,
        stale_after_seconds=60,
        snapshot_id="SNAP-1",
    )
    future = evaluate_trigger(
        conditions,
        snapshot(as_of="2026-11-06T12:00:01.000Z"),  # negative age
        now=NOW,
        stale_after_seconds=60,
        snapshot_id="SNAP-1",
    )

    assert at_budget.stale is False
    assert at_budget.met is True
    assert beyond_budget.stale is True
    assert future.stale is True
    assert "future" in future.reasons[0]


def test_condition_results_carry_every_contract_field() -> None:
    evaluation = evaluate_trigger(
        (condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),),
        snapshot(),
        now=NOW,
        snapshot_id="SNAP-1",
    )

    payload = asdict(evaluation.results[0])
    assert set(payload) == {
        "condition_id",
        "type",
        "met",
        "observed_value",
        "threshold",
        "evaluated_at",
        "data_quality",
        "evaluable",
        "note",
    }
    assert payload["evaluated_at"]
    assert payload["data_quality"] in {"COMPLETE", "PARTIAL", "STALE", "MISSING", "ESTIMATED"}


def test_evaluation_context_maps_every_snapshot_field() -> None:
    context = evaluation_context(snapshot())

    assert context.as_of == NOW
    assert context.last_price == Decimal("70000.00")
    assert context.previous_price == Decimal("69900.00")
    assert context.tick_size == Decimal("0.10")
    assert context.delta_series == (Decimal("5"), Decimal("6"))
    assert context.cvd_series == (Decimal("60"), Decimal("100"))
    assert context.volume_series == (Decimal("10"), Decimal("20"))
    assert context.oi_series == (Decimal("100"), Decimal("102"))
    assert context.price_series == (Decimal("69950.00"), Decimal("70000.00"))
    assert context.session_state == "REGULAR"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("COMPLETE", "COMPLETE"),
        ("PARTIAL", "PARTIAL"),
        ("STALE", "STALE"),
        ("MISSING", "MISSING"),
        ("ESTIMATED", "ESTIMATED"),
        ("garbage", "MISSING"),
        (None, "MISSING"),
    ],
)
def test_contract_data_quality_is_restricted_to_the_enum(raw: str | None, expected: str) -> None:
    assert contract_data_quality(raw) == expected


def test_legacy_price_only_wrapper_still_evaluates_price_conditions() -> None:
    evaluation = evaluate_trigger(
        (condition("PRICE_TOUCH_ZONE", {"price_low": "69900.00", "price_high": "70100.00"}),),
        last_price=Decimal("70000.00"),
        snapshot_as_of=NOW,
        now=NOW,
        data_quality="COMPLETE",
        snapshot_id="SNAP-LEGACY",
    )

    assert evaluation.met is True
    assert evaluation.snapshot_id == "SNAP-LEGACY"


def test_snapshot_hash_helper_is_canonical() -> None:
    payload = {"snapshot_id": "SNAP-1", "last_price": "70000.00", "as_of": NOW}
    reordered = {"as_of": NOW, "last_price": "70000.00", "snapshot_id": "SNAP-1"}

    assert snapshot_hash(payload) == snapshot_hash(reordered)
    assert snapshot_hash({**payload, "last_price": "70001.00"}) != snapshot_hash(payload)


def test_an_empty_snapshot_id_is_derived_deterministically_from_the_snapshot_hash() -> None:
    # A custom provider may leave ``snapshot_id`` empty; the contract requires a
    # non-empty ``market_snapshot_id``, so the evaluator derives one.
    frozen = snapshot(snapshot_id="")

    first = evaluate_trigger((), frozen, now=NOW)
    second = evaluate_trigger((), frozen, now=NOW)

    assert first.snapshot_id
    assert first.snapshot_id.startswith("SNAP-")
    assert first.snapshot_id == f"SNAP-{first.snapshot_hash[:16].upper()}"
    assert second.snapshot_id == first.snapshot_id
