"""The deterministic condition registry: validation and exact-decimal evaluation.

Every one of the 19 ``conditionType`` values is covered by a validation case, an
evaluation case, and an exact-decimal boundary case (ADR-0011).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from oft.domain.conditions import (
    _INVALIDATION_TYPES,
    ConditionEvaluation,
    ConditionParamsError,
    ConditionType,
    EvaluationContext,
    aggregate_data_quality,
    available_data,
    condition_spec,
    evaluate_condition,
    normalize_timestamp,
    validate_draft,
)
from oft.domain.opportunities import Condition

_ROOT = Path(__file__).resolve().parents[2]
_COMMON_SCHEMA = _ROOT / "docs" / "contracts" / "common.schema.json"
_LLM_SCHEMA = _ROOT / "docs" / "contracts" / "llm-output.schema.json"

WINDOW_START = "2026-11-01T00:00:00.000Z"
WINDOW_END = "2026-12-01T00:00:00.000Z"
AS_OF = "2026-11-06T12:00:00.000Z"
# Three bars inside WINDOW_START/WINDOW_END and at/behind AS_OF.
SERIES_BARS = (
    "2026-11-06T11:45:00.000Z",
    "2026-11-06T11:50:00.000Z",
    "2026-11-06T11:55:00.000Z",
)


def params(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "price": None,
        "price_low": None,
        "price_high": None,
        "threshold": None,
        "unit": None,
        "comparator": None,
        "lookback_bars": None,
        "session_states": [],
        "note": None,
    }
    base.update(overrides)
    return base


VALID_CASES: dict[ConditionType, dict[str, Any]] = {
    ConditionType.PRICE_CROSS_ABOVE: params(price="100.5"),
    ConditionType.PRICE_CROSS_BELOW: params(price="99.5"),
    ConditionType.PRICE_INSIDE_ZONE: params(price_low="100", price_high="200"),
    ConditionType.PRICE_TOUCH_ZONE: params(price_low="100", price_high="200"),
    ConditionType.PRICE_OUTSIDE_ZONE: params(price_low="100", price_high="200"),
    ConditionType.DELTA_ABOVE: params(threshold="10", lookback_bars=3),
    ConditionType.DELTA_BELOW: params(threshold="-10", lookback_bars=3),
    ConditionType.CVD_ABOVE: params(threshold="5", lookback_bars=3),
    ConditionType.CVD_BELOW: params(threshold="-5", lookback_bars=3),
    ConditionType.CVD_DIVERGENCE: params(lookback_bars=3),
    ConditionType.OI_DELTA_ABOVE: params(threshold="1.5", lookback_bars=3),
    ConditionType.OI_DELTA_BELOW: params(threshold="-1.5", lookback_bars=3),
    ConditionType.FUNDING_ABOVE: params(threshold="0.0001"),
    ConditionType.FUNDING_BELOW: params(threshold="-0.0001"),
    ConditionType.LIQUIDATION_NOTIONAL_ABOVE: params(threshold="1000"),
    ConditionType.VOLUME_ABOVE: params(threshold="100", lookback_bars=3),
    ConditionType.FOOTPRINT_IMBALANCE_ABOVE: params(threshold="0.5"),
    ConditionType.SESSION_STATE: params(session_states=["REGULAR"]),
    ConditionType.TIME_WINDOW: params(),
}

EXPECTED_DATA: dict[ConditionType, set[str]] = {
    ConditionType.PRICE_CROSS_ABOVE: {"MARK_PRICE", "CONTRACT_SPECS"},
    ConditionType.PRICE_CROSS_BELOW: {"MARK_PRICE", "CONTRACT_SPECS"},
    ConditionType.PRICE_INSIDE_ZONE: {"MARK_PRICE", "CONTRACT_SPECS"},
    ConditionType.PRICE_TOUCH_ZONE: {"MARK_PRICE", "CONTRACT_SPECS"},
    ConditionType.PRICE_OUTSIDE_ZONE: {"MARK_PRICE", "CONTRACT_SPECS"},
    ConditionType.DELTA_ABOVE: {"METRICS_5M"},
    ConditionType.DELTA_BELOW: {"METRICS_5M"},
    ConditionType.CVD_ABOVE: {"METRICS_5M"},
    ConditionType.CVD_BELOW: {"METRICS_5M"},
    ConditionType.CVD_DIVERGENCE: {"METRICS_5M"},
    ConditionType.OI_DELTA_ABOVE: {"OPEN_INTEREST"},
    ConditionType.OI_DELTA_BELOW: {"OPEN_INTEREST"},
    ConditionType.FUNDING_ABOVE: {"FUNDING"},
    ConditionType.FUNDING_BELOW: {"FUNDING"},
    ConditionType.LIQUIDATION_NOTIONAL_ABOVE: {"LIQUIDATIONS"},
    ConditionType.VOLUME_ABOVE: {"METRICS_5M"},
    ConditionType.FOOTPRINT_IMBALANCE_ABOVE: {"FOOTPRINT"},
    ConditionType.SESSION_STATE: {"SESSION"},
    ConditionType.TIME_WINDOW: set(),
}


def context(**overrides: Any) -> EvaluationContext:
    base: dict[str, Any] = {
        "as_of": AS_OF,
        "last_price": Decimal("100"),
        "previous_price": Decimal("99"),
        "tick_size": Decimal("0.01"),
        "delta": Decimal("10"),
        "cvd": Decimal("10"),
        "cvd_series": None,
        "price_series": None,
        "oi_delta_pct": Decimal("2"),
        "funding_rate": Decimal("0.0002"),
        "liquidation_notional": Decimal("2000"),
        "volume": Decimal("200"),
        "footprint_imbalance": Decimal("0.6"),
        "session_state": "REGULAR",
    }
    base.update(overrides)
    return EvaluationContext(**base)


def condition(condition_type: ConditionType, **overrides: Any) -> Condition:
    raw = dict(VALID_CASES[condition_type])
    raw.update(overrides)
    return Condition(
        condition_id="C-1",
        draft_id="c1",
        type=condition_type.value,
        params=raw,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        required_data=tuple(sorted(EXPECTED_DATA[condition_type])),
        evaluable=True,
    )


# -- registry ---------------------------------------------------------------


def _schema_enum(path: Path, *keys: str) -> set[str]:
    import json

    node: Any = json.loads(path.read_text(encoding="utf-8"))
    for key in keys:
        node = node[key]
    return set(node["enum"])


def test_registry_covers_exactly_the_contract_condition_types() -> None:
    assert {member.value for member in ConditionType} == _schema_enum(
        _COMMON_SCHEMA, "$defs", "conditionType"
    )
    assert set(ConditionType) == set(VALID_CASES)
    assert (
        _schema_enum(_LLM_SCHEMA, "$defs", "invalidationConditionDraft", "properties", "type")
        == _INVALIDATION_TYPES
    )


@pytest.mark.parametrize("condition_type", list(ConditionType))
def test_every_type_validates_with_its_required_parameters(
    condition_type: ConditionType,
) -> None:
    validated = validate_draft(condition_type.value, VALID_CASES[condition_type])

    assert validated.type is condition_type
    assert set(condition_spec(condition_type).required_data) == EXPECTED_DATA[condition_type]
    assert validated.required_data == tuple(sorted(EXPECTED_DATA[condition_type] or {"MARK_PRICE"}))


@pytest.mark.parametrize(
    ("condition_type", "field"),
    [
        (ConditionType.PRICE_CROSS_ABOVE, "price"),
        (ConditionType.PRICE_CROSS_BELOW, "price"),
        (ConditionType.PRICE_INSIDE_ZONE, "price_low"),
        (ConditionType.PRICE_TOUCH_ZONE, "price_high"),
        (ConditionType.PRICE_OUTSIDE_ZONE, "price_low"),
        (ConditionType.DELTA_ABOVE, "threshold"),
        (ConditionType.DELTA_BELOW, "lookback_bars"),
        (ConditionType.CVD_ABOVE, "threshold"),
        (ConditionType.CVD_BELOW, "lookback_bars"),
        (ConditionType.CVD_DIVERGENCE, "lookback_bars"),
        (ConditionType.OI_DELTA_ABOVE, "threshold"),
        (ConditionType.OI_DELTA_BELOW, "lookback_bars"),
        (ConditionType.FUNDING_ABOVE, "threshold"),
        (ConditionType.FUNDING_BELOW, "threshold"),
        (ConditionType.LIQUIDATION_NOTIONAL_ABOVE, "threshold"),
        (ConditionType.VOLUME_ABOVE, "threshold"),
        (ConditionType.FOOTPRINT_IMBALANCE_ABOVE, "threshold"),
        (ConditionType.SESSION_STATE, "session_states"),
    ],
)
def test_missing_required_parameter_rejects_the_draft(
    condition_type: ConditionType, field: str
) -> None:
    raw = dict(VALID_CASES[condition_type])
    raw[field] = [] if field == "session_states" else None

    with pytest.raises(ConditionParamsError) as excinfo:
        validate_draft(condition_type.value, raw)

    assert excinfo.value.field == field


def test_time_window_rejects_a_parameter_that_does_not_apply() -> None:
    with pytest.raises(ConditionParamsError) as excinfo:
        validate_draft("TIME_WINDOW", params(price="100"))

    assert excinfo.value.field == "price"


def test_unknown_parameter_key_is_rejected() -> None:
    raw = params(threshold="10", lookback_bars=3)
    raw["bogus"] = 1

    with pytest.raises(ConditionParamsError) as excinfo:
        validate_draft("DELTA_ABOVE", raw)

    assert excinfo.value.field == "bogus"


def test_unknown_condition_type_is_rejected() -> None:
    with pytest.raises(ConditionParamsError) as excinfo:
        validate_draft("NOT_A_CONDITION", params())

    assert excinfo.value.field == "type"


@pytest.mark.parametrize("value", ["1e3", "NaN", "Infinity", "", "not-a-number"])
def test_non_canonical_decimal_is_rejected(value: str) -> None:
    with pytest.raises(ConditionParamsError):
        validate_draft("DELTA_ABOVE", params(threshold=value, lookback_bars=3))


@pytest.mark.parametrize("value", ["0", "-1"])
def test_price_parameters_must_be_positive(value: str) -> None:
    with pytest.raises(ConditionParamsError):
        validate_draft("PRICE_CROSS_ABOVE", params(price=value))


def test_zone_parameters_must_be_ordered() -> None:
    with pytest.raises(ConditionParamsError) as excinfo:
        validate_draft("PRICE_INSIDE_ZONE", params(price_low="200", price_high="100"))

    assert excinfo.value.field == "price_low"


@pytest.mark.parametrize(
    ("condition_type", "threshold"),
    [
        (ConditionType.LIQUIDATION_NOTIONAL_ABOVE, "-1"),
        (ConditionType.LIQUIDATION_NOTIONAL_ABOVE, "0"),
        (ConditionType.VOLUME_ABOVE, "-1"),
        (ConditionType.VOLUME_ABOVE, "0"),
    ],
)
def test_positive_threshold_types_reject_non_positive_values(
    condition_type: ConditionType, threshold: str
) -> None:
    raw = dict(VALID_CASES[condition_type])
    raw["threshold"] = threshold

    with pytest.raises(ConditionParamsError):
        validate_draft(condition_type.value, raw)


def test_negative_threshold_is_valid_for_signed_metrics() -> None:
    validated = validate_draft("DELTA_BELOW", params(threshold="-42.5", lookback_bars=2))
    assert validated.params["threshold"] == "-42.5"


@pytest.mark.parametrize("lookback", [0, -1, True, 1.5, "3"])
def test_lookback_bars_must_be_an_integer_of_at_least_one(lookback: Any) -> None:
    with pytest.raises(ConditionParamsError):
        validate_draft("DELTA_ABOVE", params(threshold="1", lookback_bars=lookback))


def test_cvd_divergence_requires_at_least_three_bars() -> None:
    with pytest.raises(ConditionParamsError):
        validate_draft("CVD_DIVERGENCE", params(lookback_bars=2))


@pytest.mark.parametrize("value", ["-0.1", "1.1"])
def test_footprint_threshold_stays_inside_the_unit_interval(value: str) -> None:
    with pytest.raises(ConditionParamsError):
        validate_draft("FOOTPRINT_IMBALANCE_ABOVE", params(threshold=value))


def test_footprint_threshold_edges_are_inclusive() -> None:
    assert validate_draft("FOOTPRINT_IMBALANCE_ABOVE", params(threshold="0")).params
    assert validate_draft("FOOTPRINT_IMBALANCE_ABOVE", params(threshold="1")).params


def test_session_states_are_validated_against_the_closed_enum() -> None:
    with pytest.raises(ConditionParamsError) as excinfo:
        validate_draft("SESSION_STATE", params(session_states=["WEEKEND"]))

    assert excinfo.value.field == "session_states"


@pytest.mark.parametrize("comparator", [">", ">=", None])
def test_above_types_accept_only_above_comparators(comparator: str | None) -> None:
    validated = validate_draft(
        "DELTA_ABOVE", params(threshold="1", lookback_bars=1, comparator=comparator)
    )
    assert validated.params.get("comparator") == comparator


@pytest.mark.parametrize("comparator", ["<", "<=", "==", "ABOVE"])
def test_above_types_reject_inconsistent_comparators(comparator: str) -> None:
    with pytest.raises(ConditionParamsError) as excinfo:
        validate_draft(
            "DELTA_ABOVE",
            params(threshold="1", lookback_bars=1, comparator=comparator),
        )

    assert excinfo.value.field == "comparator"


@pytest.mark.parametrize("comparator", [">", ">=", "BETWEEN"])
def test_zone_types_reject_a_comparator(comparator: str) -> None:
    with pytest.raises(ConditionParamsError) as excinfo:
        validate_draft(
            "PRICE_TOUCH_ZONE",
            params(price_low="100", price_high="200", comparator=comparator),
        )

    assert excinfo.value.field == "comparator"


# -- evaluation -------------------------------------------------------------


def test_price_cross_above_met_at_the_upper_boundary() -> None:
    result = evaluate_condition(
        condition(ConditionType.PRICE_CROSS_ABOVE, price="100"),
        context(previous_price=Decimal("99"), last_price=Decimal("100")),
    )

    assert result.met is True
    assert result.evaluable is True
    assert result.data_quality == "COMPLETE"
    assert result.observed_value == "100.00"


def test_price_cross_above_is_strict_at_the_previous_price() -> None:
    result = evaluate_condition(
        condition(ConditionType.PRICE_CROSS_ABOVE, price="100"),
        context(previous_price=Decimal("100"), last_price=Decimal("101")),
    )

    assert result.met is False


def test_price_cross_below_met_at_the_lower_boundary() -> None:
    result = evaluate_condition(
        condition(ConditionType.PRICE_CROSS_BELOW, price="100"),
        context(previous_price=Decimal("101"), last_price=Decimal("100")),
    )

    assert result.met is True


def test_price_cross_below_is_strict_at_the_previous_price() -> None:
    result = evaluate_condition(
        condition(ConditionType.PRICE_CROSS_BELOW, price="100"),
        context(previous_price=Decimal("100"), last_price=Decimal("99")),
    )

    assert result.met is False


@pytest.mark.parametrize(
    "condition_type",
    [ConditionType.PRICE_INSIDE_ZONE, ConditionType.PRICE_TOUCH_ZONE],
)
def test_zone_conditions_include_both_edges(condition_type: ConditionType) -> None:
    zone = condition(condition_type, price_low="99", price_high="101")

    assert evaluate_condition(zone, context(last_price=Decimal("99"))).met is True
    assert evaluate_condition(zone, context(last_price=Decimal("101"))).met is True
    assert evaluate_condition(zone, context(last_price=Decimal("102"))).met is False


def test_price_outside_zone_excludes_the_edges() -> None:
    outside = condition(ConditionType.PRICE_OUTSIDE_ZONE, price_low="99", price_high="101")

    assert evaluate_condition(outside, context(last_price=Decimal("98.99"))).met is True
    assert evaluate_condition(outside, context(last_price=Decimal("101.01"))).met is True
    assert evaluate_condition(outside, context(last_price=Decimal("100"))).met is False
    assert evaluate_condition(outside, context(last_price=Decimal("99"))).met is False
    assert evaluate_condition(outside, context(last_price=Decimal("101"))).met is False


@pytest.mark.parametrize(
    ("condition_type", "overrides", "met"),
    [
        # The lookback metrics are sampled per bar; a scalar aggregate can never
        # stand in for a missing series (that fallback ignored lookback_bars).
        (
            ConditionType.DELTA_ABOVE,
            {
                "bar_timestamps": SERIES_BARS,
                "delta_series": (Decimal("0"), Decimal("0"), Decimal("10")),
            },
            False,
        ),
        (
            ConditionType.DELTA_ABOVE,
            {
                "bar_timestamps": SERIES_BARS,
                "delta_series": (
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("10.000000000000000001"),
                ),
            },
            True,
        ),
        (
            ConditionType.DELTA_BELOW,
            {
                "bar_timestamps": SERIES_BARS,
                "delta_series": (Decimal("0"), Decimal("0"), Decimal("-10")),
            },
            False,
        ),
        (
            ConditionType.DELTA_BELOW,
            {
                "bar_timestamps": SERIES_BARS,
                "delta_series": (
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("-10.000000000000000001"),
                ),
            },
            True,
        ),
        (
            ConditionType.CVD_ABOVE,
            {
                "bar_timestamps": SERIES_BARS,
                "cvd_series": (Decimal("0"), Decimal("0"), Decimal("5")),
            },
            False,
        ),
        (
            ConditionType.CVD_ABOVE,
            {
                "bar_timestamps": SERIES_BARS,
                "cvd_series": (
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("5.000000000000000001"),
                ),
            },
            True,
        ),
        (
            ConditionType.CVD_BELOW,
            {
                "bar_timestamps": SERIES_BARS,
                "cvd_series": (Decimal("0"), Decimal("0"), Decimal("-5")),
            },
            False,
        ),
        (
            ConditionType.CVD_BELOW,
            {
                "bar_timestamps": SERIES_BARS,
                "cvd_series": (
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("-5.000000000000000001"),
                ),
            },
            True,
        ),
        (
            ConditionType.OI_DELTA_ABOVE,
            {
                "bar_timestamps": SERIES_BARS,
                "oi_series": (Decimal("10"), Decimal("10"), Decimal("10.15")),
            },
            False,
        ),
        (
            ConditionType.OI_DELTA_ABOVE,
            {
                "bar_timestamps": SERIES_BARS,
                "oi_series": (Decimal("10"), Decimal("10"), Decimal("10.15000000001")),
            },
            True,
        ),
        (
            ConditionType.OI_DELTA_BELOW,
            {
                "bar_timestamps": SERIES_BARS,
                "oi_series": (Decimal("10"), Decimal("10"), Decimal("9.85")),
            },
            False,
        ),
        (
            ConditionType.OI_DELTA_BELOW,
            {
                "bar_timestamps": SERIES_BARS,
                "oi_series": (Decimal("10"), Decimal("10"), Decimal("9.84999999999")),
            },
            True,
        ),
        (ConditionType.FUNDING_ABOVE, {"funding_rate": Decimal("0.0001")}, False),
        (ConditionType.FUNDING_ABOVE, {"funding_rate": Decimal("0.0001000001")}, True),
        (ConditionType.FUNDING_BELOW, {"funding_rate": Decimal("-0.0001")}, False),
        (ConditionType.FUNDING_BELOW, {"funding_rate": Decimal("-0.0001000001")}, True),
        (
            ConditionType.LIQUIDATION_NOTIONAL_ABOVE,
            {"liquidation_notional": Decimal("1000")},
            False,
        ),
        (
            ConditionType.LIQUIDATION_NOTIONAL_ABOVE,
            {"liquidation_notional": Decimal("1000.00000001")},
            True,
        ),
        (
            ConditionType.VOLUME_ABOVE,
            {
                "bar_timestamps": SERIES_BARS,
                "volume_series": (Decimal("0"), Decimal("0"), Decimal("100")),
            },
            False,
        ),
        (
            ConditionType.VOLUME_ABOVE,
            {
                "bar_timestamps": SERIES_BARS,
                "volume_series": (Decimal("0"), Decimal("0"), Decimal("100.00000001")),
            },
            True,
        ),
        (ConditionType.FOOTPRINT_IMBALANCE_ABOVE, {"footprint_imbalance": Decimal("0.5")}, False),
        (
            ConditionType.FOOTPRINT_IMBALANCE_ABOVE,
            {"footprint_imbalance": Decimal("0.5000000001")},
            True,
        ),
    ],
)
def test_threshold_conditions_use_strict_exact_decimal_comparison(
    condition_type: ConditionType, overrides: dict[str, Any], met: bool
) -> None:
    result = evaluate_condition(condition(condition_type), context(**overrides))

    assert result.met is met
    assert result.evaluable is True


@pytest.mark.parametrize(
    ("condition_type", "comparator", "overrides", "met"),
    [
        (
            ConditionType.DELTA_ABOVE,
            ">=",
            {
                "bar_timestamps": SERIES_BARS,
                "delta_series": (Decimal("0"), Decimal("0"), Decimal("10")),
            },
            True,
        ),
        (
            ConditionType.DELTA_BELOW,
            "<=",
            {
                "bar_timestamps": SERIES_BARS,
                "delta_series": (Decimal("0"), Decimal("0"), Decimal("-10")),
            },
            True,
        ),
    ],
)
def test_inclusive_comparators_accept_the_boundary(
    condition_type: ConditionType,
    comparator: str,
    overrides: dict[str, Any],
    met: bool,
) -> None:
    result = evaluate_condition(
        condition(condition_type, comparator=comparator), context(**overrides)
    )

    assert result.met is met


def test_cvd_divergence_requires_opposite_directions() -> None:
    divergence = condition(ConditionType.CVD_DIVERGENCE, lookback_bars=3)

    met = evaluate_condition(
        divergence,
        context(
            bar_timestamps=SERIES_BARS,
            price_series=(Decimal("99"), Decimal("100"), Decimal("102")),
            cvd_series=(Decimal("10"), Decimal("9"), Decimal("8")),
        ),
    )
    same = evaluate_condition(
        divergence,
        context(
            bar_timestamps=SERIES_BARS,
            price_series=(Decimal("99"), Decimal("100"), Decimal("102")),
            cvd_series=(Decimal("8"), Decimal("9"), Decimal("10")),
        ),
    )

    assert met.met is True
    assert met.evaluable is True
    assert "price" in met.note
    assert same.met is False


def test_cvd_divergence_requires_equal_length_series() -> None:
    result = evaluate_condition(
        condition(ConditionType.CVD_DIVERGENCE, lookback_bars=3),
        context(
            bar_timestamps=SERIES_BARS,
            price_series=(Decimal("99"), Decimal("100"), Decimal("101")),
            cvd_series=(Decimal("9"), Decimal("8")),
        ),
    )

    assert result.met is False
    assert result.evaluable is False
    assert "series" in (result.unavailable_reason or "")
    assert result.data_quality == "PARTIAL"


def test_cvd_divergence_requires_enough_bars() -> None:
    result = evaluate_condition(
        condition(ConditionType.CVD_DIVERGENCE, lookback_bars=3),
        context(
            bar_timestamps=BARS_EARLY,
            price_series=(Decimal("99"), Decimal("100")),
            cvd_series=(Decimal("9"), Decimal("8")),
        ),
    )

    assert result.evaluable is False
    assert "lookback_bars" in (result.unavailable_reason or "")


def test_cvd_divergence_does_not_call_a_flat_series_divergent() -> None:
    result = evaluate_condition(
        condition(ConditionType.CVD_DIVERGENCE, lookback_bars=3),
        context(
            bar_timestamps=SERIES_BARS,
            price_series=(Decimal("100"), Decimal("100"), Decimal("100")),
            cvd_series=(Decimal("8"), Decimal("9"), Decimal("10")),
        ),
    )

    assert result.met is False
    assert result.evaluable is True


def test_cvd_divergence_never_uses_bars_after_as_of() -> None:
    # The four bars end at 12:05, after as_of 12:00. Slicing the raw tail would
    # include the future bar and read same-direction price and CVD; the
    # windowed tail (<= as_of) diverges.
    result = evaluate_condition(
        condition(ConditionType.CVD_DIVERGENCE, lookback_bars=3),
        context(
            bar_timestamps=(*SERIES_BARS, "2026-11-06T12:05:00.000Z"),
            price_series=(Decimal("99"), Decimal("100"), Decimal("102"), Decimal("50")),
            cvd_series=(Decimal("10"), Decimal("9"), Decimal("8"), Decimal("5")),
        ),
    )

    assert result.evaluable is True
    assert result.met is True


def test_cvd_divergence_never_uses_bars_outside_the_condition_window() -> None:
    # The 12:10 bar is before as_of but after the condition window end; it
    # would flip the last-3 comparison to a false divergence.
    date_window = Condition(
        condition_id="C-1",
        draft_id="c1",
        type=ConditionType.CVD_DIVERGENCE.value,
        params={"lookback_bars": 3},
        window_start="2026-11-06T11:50:00.000Z",
        window_end="2026-11-06T12:05:00.000Z",
        required_data=("METRICS_5M",),
        evaluable=True,
    )
    result = evaluate_condition(
        date_window,
        context(
            as_of="2026-11-06T12:20:00.000Z",
            bar_timestamps=(
                "2026-11-06T11:50:00.000Z",
                "2026-11-06T11:55:00.000Z",
                "2026-11-06T12:00:00.000Z",
                "2026-11-06T12:10:00.000Z",
            ),
            price_series=(Decimal("99"), Decimal("100"), Decimal("102"), Decimal("50")),
            cvd_series=(Decimal("8"), Decimal("9"), Decimal("10"), Decimal("20")),
        ),
    )

    assert result.evaluable is True
    assert result.met is False


def test_cvd_divergence_slices_surplus_bars_to_the_lookback() -> None:
    # The whole five-bar window trends down in both series; only the last three
    # bars (price up, CVD down) diverge.
    ctx = context(
        bar_timestamps=(
            "2026-11-06T11:30:00.000Z",
            "2026-11-06T11:40:00.000Z",
            "2026-11-06T11:45:00.000Z",
            "2026-11-06T11:50:00.000Z",
            "2026-11-06T11:55:00.000Z",
        ),
        price_series=(
            Decimal("200"),
            Decimal("150"),
            Decimal("99"),
            Decimal("100"),
            Decimal("102"),
        ),
        cvd_series=(Decimal("50"), Decimal("30"), Decimal("10"), Decimal("9"), Decimal("8")),
    )

    last_three = evaluate_condition(condition(ConditionType.CVD_DIVERGENCE, lookback_bars=3), ctx)
    whole_window = evaluate_condition(condition(ConditionType.CVD_DIVERGENCE, lookback_bars=5), ctx)

    assert last_three.evaluable is True
    assert last_three.met is True
    assert whole_window.evaluable is True
    assert whole_window.met is False


def test_cvd_divergence_without_bar_timestamps_is_unevaluable() -> None:
    result = evaluate_condition(
        condition(ConditionType.CVD_DIVERGENCE, lookback_bars=3),
        context(
            bar_timestamps=None,
            price_series=(Decimal("99"), Decimal("100"), Decimal("102")),
            cvd_series=(Decimal("10"), Decimal("9"), Decimal("8")),
        ),
    )

    assert result.met is False
    assert result.evaluable is False
    assert "timestamps" in (result.unavailable_reason or "")


def test_cvd_divergence_with_malformed_bar_timestamps_is_unevaluable() -> None:
    result = evaluate_condition(
        condition(ConditionType.CVD_DIVERGENCE, lookback_bars=3),
        context(
            bar_timestamps=("not-a-timestamp", "also-bad", "still-bad"),
            price_series=(Decimal("99"), Decimal("100"), Decimal("102")),
            cvd_series=(Decimal("10"), Decimal("9"), Decimal("8")),
        ),
    )

    assert result.met is False
    assert result.evaluable is False
    assert "malformed" in (result.unavailable_reason or "")


def test_session_state_matches_the_recorded_state() -> None:
    result = evaluate_condition(
        condition(ConditionType.SESSION_STATE, session_states=["REGULAR", "NO_TRADING"]),
        context(session_state="REGULAR"),
    )

    assert result.met is True
    assert result.observed_value == "REGULAR"


def test_no_trading_is_a_regime_flag_and_does_not_block_other_conditions() -> None:
    session = condition(ConditionType.SESSION_STATE, session_states=["NO_TRADING"])
    price = condition(ConditionType.PRICE_TOUCH_ZONE, price_low="99", price_high="101")
    no_trading = context(session_state="NO_TRADING", last_price=Decimal("100"))

    assert evaluate_condition(session, no_trading).met is True
    assert evaluate_condition(price, no_trading).met is True


def test_time_window_is_half_open_in_utc() -> None:
    window = Condition(
        condition_id="C-1",
        draft_id="c1",
        type="TIME_WINDOW",
        params={},
        window_start="2026-11-06T12:00:00.000Z",
        window_end="2026-11-06T13:00:00.000Z",
        required_data=("MARK_PRICE",),
        evaluable=True,
    )

    assert evaluate_condition(window, context(as_of="2026-11-06T12:00:00.000Z")).met is True
    assert evaluate_condition(window, context(as_of="2026-11-06T12:59:59.999Z")).met is True
    assert evaluate_condition(window, context(as_of="2026-11-06T13:00:00.000Z")).met is False
    assert evaluate_condition(window, context(as_of="2026-11-06T11:59:59.999Z")).met is False


def test_time_window_is_unevaluable_without_an_as_of() -> None:
    result = evaluate_condition(condition(ConditionType.TIME_WINDOW), context(as_of=None))

    assert result.met is False
    assert result.evaluable is False
    assert "as_of" in (result.unavailable_reason or "")


def test_time_window_is_evaluable_without_any_price_data() -> None:
    # The registry's empty required-data set is authoritative; the persisted
    # ``["MARK_PRICE"]`` value only satisfies the contract's minItems rule.
    result = evaluate_condition(
        condition(ConditionType.TIME_WINDOW),
        EvaluationContext(as_of=AS_OF),
    )

    assert result.evaluable is True
    assert result.met is True


def test_offset_less_time_window_bounds_are_treated_as_utc() -> None:
    window = Condition(
        condition_id="C-1",
        draft_id="c1",
        type="TIME_WINDOW",
        params={},
        window_start="2026-11-06T12:00:00",
        window_end="2026-11-06T13:00:00",
        required_data=("MARK_PRICE",),
        evaluable=True,
    )

    inside = evaluate_condition(window, context(as_of="2026-11-06T12:30:00.000Z"))
    naive_as_of = evaluate_condition(window, context(as_of="2026-11-06T12:30:00"))
    outside = evaluate_condition(window, context(as_of="2026-11-06T13:00:00.000Z"))

    assert inside.evaluable is True
    assert inside.met is True
    assert naive_as_of.evaluable is True
    assert naive_as_of.met is True
    assert outside.evaluable is True
    assert outside.met is False


# -- exact decimals and tick quantisation -----------------------------------


def test_prices_are_quantised_to_the_tick_before_comparison() -> None:
    quantised = evaluate_condition(
        condition(ConditionType.PRICE_CROSS_ABOVE, price="100"),
        context(
            tick_size=Decimal("1"),
            previous_price=Decimal("99.9"),
            last_price=Decimal("100.4"),
        ),
    )

    assert quantised.observed_value == "100"
    # Unquantised this would be 99.9 < 100 <= 100.4, i.e. met.
    assert quantised.met is False


def test_zone_thresholds_are_quantised_to_the_tick() -> None:
    result = evaluate_condition(
        condition(ConditionType.PRICE_INSIDE_ZONE, price_low="100.4", price_high="100.4"),
        context(tick_size=Decimal("1"), last_price=Decimal("101")),
    )

    # Both zone edges quantise to 100, so 101 is outside.
    assert result.met is False


@pytest.mark.parametrize("tick", [Decimal("0"), Decimal("-0.01")])
def test_non_positive_tick_is_unevaluable(tick: Decimal) -> None:
    # A malformed persisted context must not raise out of the read path.
    result = evaluate_condition(
        condition(ConditionType.PRICE_CROSS_ABOVE),
        context(tick_size=tick),
    )

    assert result.met is False
    assert result.evaluable is False
    assert result.unavailable_reason


def test_non_total_comparator_is_unevaluable_not_a_500() -> None:
    invalid = Condition(
        condition_id="C-1",
        draft_id="c1",
        type=ConditionType.DELTA_ABOVE.value,
        params={"threshold": "1", "lookback_bars": 1, "comparator": "=="},
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        required_data=("METRICS_5M",),
        evaluable=True,
    )

    result = evaluate_condition(
        invalid,
        context(
            bar_timestamps=SERIES_BARS,
            delta_series=(Decimal("0"), Decimal("0"), Decimal("10")),
        ),
    )

    assert result.met is False
    assert result.evaluable is False
    assert result.unavailable_reason


def test_out_of_range_quantisation_is_unevaluable_not_a_500() -> None:
    extreme = Condition(
        condition_id="C-1",
        draft_id="c1",
        type=ConditionType.PRICE_CROSS_ABOVE.value,
        params={"price": "9" * 64},
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        required_data=("MARK_PRICE", "CONTRACT_SPECS"),
        evaluable=True,
    )

    result = evaluate_condition(
        extreme,
        context(
            tick_size=Decimal("0.000000000000000000000000000001"),
            previous_price=Decimal("1"),
            last_price=Decimal("1"),
        ),
    )

    assert result.met is False
    assert result.evaluable is False
    assert result.unavailable_reason


def test_tick_half_tie_rounds_half_up_not_to_even() -> None:
    # 100.5 at tick 1 rounds to 101 under ROUND_HALF_UP; banker's rounding
    # would yield 100 and the cross would not be met.
    result = evaluate_condition(
        condition(ConditionType.PRICE_CROSS_ABOVE, price="101"),
        context(
            tick_size=Decimal("1"),
            previous_price=Decimal("100"),
            last_price=Decimal("100.5"),
        ),
    )

    assert result.observed_value == "101"
    assert result.met is True


def test_no_float_appears_in_a_result() -> None:
    result = evaluate_condition(
        condition(ConditionType.DELTA_ABOVE),
        context(
            bar_timestamps=SERIES_BARS,
            delta_series=(Decimal("0"), Decimal("0"), Decimal("10.123456789012345678")),
        ),
    )

    assert isinstance(result, ConditionEvaluation)
    assert result.observed_value == "10.123456789012345678"
    assert result.threshold == "10"


# -- non-evaluable paths ----------------------------------------------------


def test_missing_required_data_is_never_met() -> None:
    result = evaluate_condition(
        condition(ConditionType.LIQUIDATION_NOTIONAL_ABOVE),
        context(liquidation_notional=None),
    )

    assert result.met is False
    assert result.evaluable is False
    assert "LIQUIDATIONS" in (result.unavailable_reason or "")
    assert result.data_quality == "MISSING"


def test_partial_data_is_flagged_partial() -> None:
    result = evaluate_condition(
        condition(ConditionType.PRICE_CROSS_ABOVE),
        context(tick_size=None),
    )

    assert result.met is False
    assert result.evaluable is False
    assert "CONTRACT_SPECS" in (result.unavailable_reason or "")
    assert result.data_quality == "PARTIAL"


def test_missing_previous_price_is_unevaluable() -> None:
    result = evaluate_condition(
        condition(ConditionType.PRICE_CROSS_ABOVE),
        context(previous_price=None),
    )

    assert result.met is False
    assert result.evaluable is False
    assert "previous price" in (result.unavailable_reason or "")
    assert result.data_quality == "PARTIAL"


def test_a_condition_marked_unevaluable_is_never_met() -> None:
    unavailable = Condition(
        condition_id="C-1",
        draft_id="c1",
        type="LIQUIDATION_NOTIONAL_ABOVE",
        params={"threshold": "1"},
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        required_data=("LIQUIDATIONS",),
        evaluable=False,
        unavailable_reason="historical liquidations are unavailable",
    )

    result = evaluate_condition(unavailable, context())

    assert result.met is False
    assert result.evaluable is False
    assert result.unavailable_reason == "historical liquidations are unavailable"
    assert result.data_quality == "MISSING"


def test_unknown_condition_type_is_never_met() -> None:
    unknown = Condition(
        condition_id="C-9",
        draft_id="c9",
        type="NOT_A_CONDITION",
        params={},
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        required_data=("MARK_PRICE",),
        evaluable=True,
    )

    result = evaluate_condition(unknown, context())

    assert result.met is False
    assert result.evaluable is False
    assert "unknown" in (result.unavailable_reason or "").lower()


@pytest.mark.parametrize(
    ("condition_type", "absent"),
    [
        (ConditionType.PRICE_CROSS_ABOVE, {"last_price": None}),
        (ConditionType.DELTA_ABOVE, {"delta": None}),
        (ConditionType.CVD_ABOVE, {"cvd": None}),
        (ConditionType.OI_DELTA_ABOVE, {"oi_delta_pct": None}),
        (ConditionType.FUNDING_ABOVE, {"funding_rate": None}),
        (ConditionType.LIQUIDATION_NOTIONAL_ABOVE, {"liquidation_notional": None}),
        (ConditionType.VOLUME_ABOVE, {"volume": None}),
        (ConditionType.FOOTPRINT_IMBALANCE_ABOVE, {"footprint_imbalance": None}),
        (ConditionType.SESSION_STATE, {"session_state": None}),
    ],
)
def test_absent_input_makes_its_condition_unevaluable(
    condition_type: ConditionType, absent: dict[str, None]
) -> None:
    result = evaluate_condition(condition(condition_type), context(**absent))

    assert result.met is False
    assert result.evaluable is False
    assert result.unavailable_reason


def test_available_data_reports_only_present_inputs() -> None:
    assert available_data(context()) == frozenset(
        {
            "MARK_PRICE",
            "CONTRACT_SPECS",
            "METRICS_5M",
            "OPEN_INTEREST",
            "FUNDING",
            "LIQUIDATIONS",
            "FOOTPRINT",
            "SESSION",
        }
    )
    assert available_data(EvaluationContext()) == frozenset()


def test_cvd_divergence_requires_both_series() -> None:
    result = evaluate_condition(
        condition(ConditionType.CVD_DIVERGENCE, lookback_bars=3),
        context(price_series=None, cvd_series=None),
    )

    assert result.met is False
    assert result.evaluable is False
    assert "series" in (result.unavailable_reason or "")


def test_malformed_time_window_is_unevaluable() -> None:
    malformed = Condition(
        condition_id="C-1",
        draft_id="c1",
        type="TIME_WINDOW",
        params={},
        window_start="not-a-timestamp",
        window_end="also-not-a-timestamp",
        required_data=("MARK_PRICE",),
        evaluable=True,
    )

    result = evaluate_condition(malformed, context())

    assert result.met is False
    assert result.evaluable is False
    assert result.unavailable_reason == "the time window is malformed"


def test_invalid_persisted_parameters_are_unevaluable() -> None:
    invalid = Condition(
        condition_id="C-1",
        draft_id="c1",
        type="DELTA_ABOVE",
        params={},
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        required_data=("METRICS_5M",),
        evaluable=True,
    )

    result = evaluate_condition(invalid, context())

    assert result.met is False
    assert result.evaluable is False
    assert "invalid condition parameters" in (result.unavailable_reason or "")


def test_invalid_persisted_price_parameters_are_unevaluable() -> None:
    invalid = Condition(
        condition_id="C-1",
        draft_id="c1",
        type="PRICE_CROSS_ABOVE",
        params={},
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        required_data=("MARK_PRICE", "CONTRACT_SPECS"),
        evaluable=True,
    )

    result = evaluate_condition(invalid, context())

    assert result.evaluable is False
    assert "invalid condition parameters" in (result.unavailable_reason or "")


def test_aggregate_data_quality_reduces_the_worst_case() -> None:
    assert aggregate_data_quality(()) == "MISSING"
    assert aggregate_data_quality(("COMPLETE",)) == "COMPLETE"
    assert aggregate_data_quality(("MISSING",)) == "MISSING"
    assert aggregate_data_quality(("COMPLETE", "MISSING")) == "PARTIAL"
    assert aggregate_data_quality(("PARTIAL", "COMPLETE")) == "PARTIAL"


# -- lookback-bounded series -------------------------------------------------


BARS_EARLY = ("2026-11-06T11:50:00.000Z", "2026-11-06T11:55:00.000Z")


def _series_context(**overrides: Any) -> EvaluationContext:
    return context(bar_timestamps=BARS_EARLY, **overrides)


def test_volume_lookback_uses_only_the_requested_bars() -> None:
    ctx = _series_context(volume_series=(Decimal("10"), Decimal("12")))

    one = evaluate_condition(
        condition(ConditionType.VOLUME_ABOVE, threshold="15", lookback_bars=1), ctx
    )
    two = evaluate_condition(
        condition(ConditionType.VOLUME_ABOVE, threshold="15", lookback_bars=2), ctx
    )

    assert one.met is False
    assert one.observed_value == "12"
    assert two.met is True
    assert two.observed_value == "22"


def test_delta_lookback_sums_the_window() -> None:
    ctx = _series_context(delta_series=(Decimal("1"), Decimal("2")))

    one = evaluate_condition(
        condition(ConditionType.DELTA_ABOVE, threshold="2", lookback_bars=1), ctx
    )
    two = evaluate_condition(
        condition(ConditionType.DELTA_ABOVE, threshold="2", lookback_bars=2), ctx
    )

    assert one.met is False
    assert two.met is True
    assert two.observed_value == "3"


def test_cvd_lookback_uses_the_last_value_not_the_sum() -> None:
    ctx = _series_context(cvd_series=(Decimal("5"), Decimal("9")))

    two = evaluate_condition(
        condition(ConditionType.CVD_ABOVE, threshold="10", lookback_bars=2), ctx
    )
    one = evaluate_condition(
        condition(ConditionType.CVD_ABOVE, threshold="10", lookback_bars=1), ctx
    )

    assert two.met is False
    assert two.observed_value == "9"
    assert one.observed_value == "9"


def test_a_missing_per_bar_series_never_falls_back_to_the_scalar_aggregate() -> None:
    # bars[1] has no open_interest, so the builder hands over no series while
    # the summary still reports the whole-window +100%.
    result = evaluate_condition(
        condition(ConditionType.OI_DELTA_ABOVE, threshold="50", lookback_bars=1),
        _series_context(oi_series=None, oi_delta_pct=Decimal("100")),
    )

    assert result.met is False
    assert result.evaluable is False
    assert result.observed_value is None
    assert result.data_quality == "PARTIAL"
    assert "series" in (result.unavailable_reason or "")


def test_delta_without_a_per_bar_series_is_unevaluable() -> None:
    result = evaluate_condition(
        condition(ConditionType.DELTA_ABOVE, threshold="1", lookback_bars=1),
        _series_context(delta_series=None, delta=Decimal("100")),
    )

    assert result.met is False
    assert result.evaluable is False
    assert "series" in (result.unavailable_reason or "")


def test_a_none_point_in_the_lookback_series_is_unevaluable() -> None:
    result = evaluate_condition(
        condition(ConditionType.OI_DELTA_ABOVE, threshold="50", lookback_bars=1),
        context(
            bar_timestamps=SERIES_BARS,
            oi_series=(Decimal("10"), None, Decimal("20")),
        ),
    )

    assert result.met is False
    assert result.evaluable is False
    assert result.observed_value is None
    assert result.data_quality == "PARTIAL"


def test_oi_lookback_uses_percent_change_over_the_window() -> None:
    ctx = _series_context(oi_series=(Decimal("10"), Decimal("11")))

    two = evaluate_condition(
        condition(ConditionType.OI_DELTA_ABOVE, threshold="5", lookback_bars=2), ctx
    )
    one = evaluate_condition(
        condition(ConditionType.OI_DELTA_ABOVE, threshold="5", lookback_bars=1), ctx
    )

    assert two.met is True
    assert Decimal(two.observed_value or "0") == Decimal("10")
    assert one.met is False


def test_insufficient_lookback_bars_is_unevaluable() -> None:
    result = evaluate_condition(
        condition(ConditionType.VOLUME_ABOVE, threshold="1", lookback_bars=3),
        _series_context(volume_series=(Decimal("10"), Decimal("12"))),
    )

    assert result.met is False
    assert result.evaluable is False
    assert result.data_quality == "PARTIAL"
    assert "lookback_bars" in (result.unavailable_reason or "")


def test_future_bars_are_never_used_for_a_lookback() -> None:
    result = evaluate_condition(
        condition(ConditionType.VOLUME_ABOVE, threshold="1", lookback_bars=3),
        context(
            bar_timestamps=(
                *BARS_EARLY,
                "2026-11-06T12:05:00.000Z",
            ),
            volume_series=(Decimal("10"), Decimal("12"), Decimal("1000")),
        ),
    )

    assert result.evaluable is False
    assert "lookback_bars" in (result.unavailable_reason or "")


def test_bars_outside_the_condition_window_are_never_used() -> None:
    result = evaluate_condition(
        condition(ConditionType.VOLUME_ABOVE, threshold="1", lookback_bars=2),
        context(
            bar_timestamps=("2026-10-01T00:00:00.000Z", "2026-11-06T11:55:00.000Z"),
            volume_series=(Decimal("1000"), Decimal("12")),
        ),
    )

    assert result.evaluable is False
    assert "lookback_bars" in (result.unavailable_reason or "")


def test_a_lookback_series_without_bar_timestamps_is_unevaluable() -> None:
    result = evaluate_condition(
        condition(ConditionType.DELTA_ABOVE, threshold="1", lookback_bars=1),
        context(bar_timestamps=None, delta_series=(Decimal("10"),)),
    )

    assert result.met is False
    assert result.evaluable is False
    assert "timestamps" in (result.unavailable_reason or "")


def test_malformed_bar_timestamps_make_a_lookback_unevaluable() -> None:
    result = evaluate_condition(
        condition(ConditionType.DELTA_ABOVE, threshold="1", lookback_bars=1),
        context(bar_timestamps=("not-a-timestamp",), delta_series=(Decimal("10"),)),
    )

    assert result.met is False
    assert result.evaluable is False
    assert result.data_quality == "PARTIAL"


def test_a_zero_open_interest_baseline_is_unevaluable() -> None:
    result = evaluate_condition(
        condition(ConditionType.OI_DELTA_ABOVE, threshold="1", lookback_bars=2),
        context(
            bar_timestamps=BARS_EARLY,
            oi_series=(Decimal("0"), Decimal("10")),
        ),
    )

    assert result.met is False
    assert result.evaluable is False
    assert "open interest" in (result.unavailable_reason or "")


# -- timestamps, comparators, and inversion semantics -----------------------


def test_empty_as_of_is_unevaluable_without_an_empty_timestamp() -> None:
    result = evaluate_condition(condition(ConditionType.DELTA_ABOVE), context(as_of=""))

    assert result.evaluable is False
    assert result.met is False
    assert result.evaluated_at is None
    assert "as_of" in (result.unavailable_reason or "")


@pytest.mark.parametrize(
    ("condition_type", "comparator"),
    [
        (ConditionType.PRICE_CROSS_ABOVE, ">"),
        (ConditionType.PRICE_CROSS_ABOVE, ">="),
        (ConditionType.PRICE_CROSS_BELOW, "<"),
        (ConditionType.PRICE_CROSS_BELOW, "<="),
    ],
)
def test_price_cross_accepts_a_comparator_consistent_with_its_direction(
    condition_type: ConditionType, comparator: str
) -> None:
    validated = validate_draft(condition_type.value, params(price="100", comparator=comparator))

    assert validated.params["comparator"] == comparator


@pytest.mark.parametrize(
    ("condition_type", "comparator"),
    [
        (ConditionType.PRICE_CROSS_ABOVE, "<"),
        (ConditionType.PRICE_CROSS_BELOW, ">"),
    ],
)
def test_price_cross_rejects_an_opposite_comparator(
    condition_type: ConditionType, comparator: str
) -> None:
    with pytest.raises(ConditionParamsError) as excinfo:
        validate_draft(condition_type.value, params(price="100", comparator=comparator))

    assert excinfo.value.field == "comparator"


def test_time_expired_is_met_only_after_its_window() -> None:
    expired = Condition(
        condition_id="I-1",
        draft_id="i1",
        type="TIME_WINDOW",
        params={"original_type": "TIME_EXPIRED"},
        window_start=WINDOW_START,
        window_end="2026-11-10T00:00:00.000Z",
        required_data=("MARK_PRICE",),
        evaluable=True,
    )

    inside = evaluate_condition(expired, context(as_of="2026-11-06T12:00:00.000Z"))
    after = evaluate_condition(expired, context(as_of="2026-11-11T00:00:00.000Z"))

    assert inside.evaluable is True
    assert inside.met is False
    assert after.met is True


def test_normalize_timestamp_produces_utc_z_strings() -> None:
    assert normalize_timestamp("2026-11-06T17:30:00+05:30") == "2026-11-06T12:00:00.000Z"
    assert normalize_timestamp("2026-11-06T12:00:00") == "2026-11-06T12:00:00.000Z"
    assert normalize_timestamp("2026-11-06T12:00:00.123Z") == "2026-11-06T12:00:00.123Z"


def test_normalize_timestamp_truncates_sub_millisecond_input_to_millis() -> None:
    # TEXT expiry comparisons are lexicographic: a six-digit fraction would
    # sort after a three-digit instant that is actually later.
    assert normalize_timestamp("2026-11-06T12:00:00.123456Z") == "2026-11-06T12:00:00.123Z"
    assert normalize_timestamp("2026-11-06T12:00:00.123999Z") == "2026-11-06T12:00:00.123Z"
    assert normalize_timestamp("2026-11-06T12:00:00.9Z") == "2026-11-06T12:00:00.900Z"
