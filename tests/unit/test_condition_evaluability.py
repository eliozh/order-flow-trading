"""Evaluability at creation, historical gaps, and invalidation draft mapping."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oft.application.opportunity_service import (
    _bar_series,
    _bar_timestamps,
    _footprint_imbalance,
    _liquidation_notional,
    _open_interest_delta_pct,
    _optional_decimal,
    _parse_timestamp,
    _session_state,
    available_data_from_snapshot,
    evaluation_context_from_snapshot,
)
from oft.domain.conditions import (
    ConditionParamsError,
    ConditionType,
    MappedCondition,
    available_data,
    creation_evaluable,
    evaluate_condition,
    map_invalidation_draft,
    validate_draft,
)
from oft.domain.opportunities import (
    Condition,
    ConditionDraft,
    ConditionDraftError,
    assign_conditions,
)

WINDOW = {"start": "2026-11-01T00:00:00.000Z", "end": "2026-12-01T00:00:00.000Z"}
CREATED_AT = "2026-11-06T12:00:00.000Z"
EXPIRES_AT = "2026-12-01T00:00:00.000Z"


def draft(
    condition_type: str,
    *,
    draft_id: str = "c1",
    **params: object,
) -> ConditionDraft:
    base: dict[str, object] = {
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
    base.update(params)
    return ConditionDraft(
        draft_id=draft_id,
        type=condition_type,
        params=base,
        window=WINDOW,
        required_data=(),
        description="test draft",
    )


def test_creation_is_evaluable_only_when_all_required_data_is_present() -> None:
    assert creation_evaluable(ConditionType.DELTA_ABOVE, {"METRICS_5M"}) is True
    assert creation_evaluable(ConditionType.DELTA_ABOVE, {"MARK_PRICE"}) is False
    assert creation_evaluable(ConditionType.TIME_WINDOW, set()) is True


def test_historical_liquidations_leave_the_condition_unevaluable() -> None:
    conditions = assign_conditions(
        (draft("LIQUIDATION_NOTIONAL_ABOVE", threshold="1000"),),
        available_data={"MARK_PRICE", "CONTRACT_SPECS", "METRICS_5M"},
    )

    condition = conditions[0]
    assert condition.evaluable is False
    assert "LIQUIDATIONS" in (condition.unavailable_reason or "")


def test_available_data_from_a_snapshot_like_mapping() -> None:
    snapshot = {
        "last_price": "100",
        "contract_specs": {"tick_size": "0.01"},
        "orderflow": {
            "cvd": {"summary": {"delta": "1"}, "points": [{"cvd": "2"}]},
            "open_interest": {
                "points": [
                    {"open_interest": "10"},
                    {"open_interest": "11"},
                ]
            },
            "funding": {
                "current_rate": "0.0001",
                "history": [{"rate": "0.0001"}],
            },
            "footprint": {"bars": []},
            "liquidations": {"coverage": "NONE"},
        },
        "session": {"underlying_session": "REGULAR", "is_no_trading": False},
    }

    present = available_data_from_snapshot(snapshot)

    assert "MARK_PRICE" in present
    assert "CONTRACT_SPECS" in present
    assert "METRICS_5M" in present
    assert "OPEN_INTEREST" in present
    assert "FUNDING" in present
    assert "SESSION" in present
    assert "LIQUIDATIONS" not in present


def test_empty_bars_do_not_become_zero_volume_or_empty_series() -> None:
    snapshot = {
        "last_price": "100",
        "contract_specs": {"tick_size": "0.01"},
        "bars": [],
        "orderflow": {"cvd": {"summary": {"delta": "1"}}},
    }

    context = evaluation_context_from_snapshot(snapshot, as_of=CREATED_AT)

    assert context.volume is None
    assert context.price_series is None
    assert context.cvd_series is None
    volume_condition = Condition(
        condition_id="C-1",
        draft_id="c1",
        type=ConditionType.VOLUME_ABOVE.value,
        params={"threshold": "100", "lookback_bars": 1},
        window_start=CREATED_AT,
        window_end=EXPIRES_AT,
        required_data=("METRICS_5M",),
        evaluable=True,
    )
    result = evaluate_condition(volume_condition, context)

    assert result.met is False
    assert result.evaluable is False
    assert result.observed_value is None
    assert "volume" in (result.unavailable_reason or "")


def test_invalidation_price_cross_maps_to_real_params() -> None:
    mapped = map_invalidation_draft(
        "PRICE_CROSS_BELOW",
        price="69000.00",
        threshold=None,
        window_end=None,
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
        zone_low="69000.00",
        zone_high="71000.00",
        available_data={"MARK_PRICE", "CONTRACT_SPECS"},
    )

    assert isinstance(mapped, MappedCondition)
    assert mapped.type is ConditionType.PRICE_CROSS_BELOW
    assert mapped.params == {"price": "69000.00"}
    assert mapped.window_start == CREATED_AT
    assert mapped.window_end == EXPIRES_AT
    assert mapped.evaluable is True


def test_invalidation_price_outside_zone_uses_the_opportunity_zone() -> None:
    mapped = map_invalidation_draft(
        "PRICE_OUTSIDE_ZONE",
        price=None,
        threshold=None,
        window_end=None,
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
        zone_low="69000.00",
        zone_high="71000.00",
        available_data={"MARK_PRICE", "CONTRACT_SPECS"},
    )

    assert mapped.type is ConditionType.PRICE_OUTSIDE_ZONE
    assert mapped.params == {"price_low": "69000.00", "price_high": "71000.00"}


def test_invalidation_time_expired_maps_to_time_window() -> None:
    mapped = map_invalidation_draft(
        "TIME_EXPIRED",
        price=None,
        threshold=None,
        window_end="2026-11-20T00:00:00.000Z",
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
        zone_low="69000.00",
        zone_high="71000.00",
        available_data=set(),
    )

    assert mapped.type is ConditionType.TIME_WINDOW
    # TIME_EXPIRED keeps its identity so the evaluator can invert the window
    # semantics (met after the validity window ends).
    assert mapped.params == {"original_type": "TIME_EXPIRED"}
    assert mapped.window_start == CREATED_AT
    assert mapped.window_end == "2026-11-20T00:00:00.000Z"
    assert mapped.evaluable is True


def test_invalidation_manual_is_code_only_and_never_met() -> None:
    mapped = map_invalidation_draft(
        "MANUAL",
        price=None,
        threshold=None,
        window_end=None,
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
        zone_low="69000.00",
        zone_high="71000.00",
        available_data={"MARK_PRICE", "CONTRACT_SPECS"},
    )

    assert mapped.type is ConditionType.TIME_WINDOW
    assert mapped.params["original_type"] == "MANUAL"
    assert mapped.evaluable is False
    assert mapped.unavailable_reason == "manual invalidation is code-only"


def test_invalidation_threshold_types_require_a_threshold() -> None:
    with pytest.raises(ConditionParamsError):
        map_invalidation_draft(
            "DELTA_ABOVE",
            price=None,
            threshold=None,
            window_end=None,
            created_at=CREATED_AT,
            expires_at=EXPIRES_AT,
            zone_low="69000.00",
            zone_high="71000.00",
            available_data={"METRICS_5M"},
        )


def test_invalidation_mapping_without_available_data_assumes_complete() -> None:
    mapped = map_invalidation_draft(
        "PRICE_CROSS_BELOW",
        price="69000.00",
        threshold=None,
        window_end=None,
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
        zone_low="69000.00",
        zone_high="71000.00",
        available_data=None,
    )

    assert mapped.evaluable is True


def test_mapped_invalidation_with_missing_data_records_the_same_reason_as_a_trigger() -> None:
    mapped = map_invalidation_draft(
        "PRICE_CROSS_BELOW",
        price="69000.00",
        threshold=None,
        window_end=None,
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
        zone_low="69000.00",
        zone_high="71000.00",
        # MARK_PRICE is present but the contract spec (tick size) is not.
        available_data={"MARK_PRICE"},
    )

    assert mapped.evaluable is False
    assert mapped.unavailable_reason == "missing required data: CONTRACT_SPECS"


def test_mapped_metric_invalidation_with_missing_data_records_the_reason() -> None:
    mapped = map_invalidation_draft(
        "DELTA_ABOVE",
        price=None,
        threshold="10",
        window_end=None,
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
        zone_low="69000.00",
        zone_high="71000.00",
        available_data={"MARK_PRICE"},
    )

    assert mapped.evaluable is False
    assert mapped.unavailable_reason == "missing required data: METRICS_5M"


def test_funding_invalidation_maps_a_threshold_only_condition() -> None:
    mapped = map_invalidation_draft(
        "FUNDING_ABOVE",
        price=None,
        threshold="0.0003",
        window_end=None,
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
        zone_low="69000.00",
        zone_high="71000.00",
        available_data={"FUNDING"},
    )

    assert mapped.type is ConditionType.FUNDING_ABOVE
    assert mapped.params == {"threshold": "0.0003"}
    assert mapped.evaluable is True


def test_invalidation_unknown_type_is_rejected() -> None:
    with pytest.raises(ConditionParamsError):
        map_invalidation_draft(
            "SOMETHING_ELSE",
            price=None,
            threshold=None,
            window_end=None,
            created_at=CREATED_AT,
            expires_at=EXPIRES_AT,
            zone_low="69000.00",
            zone_high="71000.00",
            available_data=set(),
        )


def test_assign_conditions_wraps_registry_errors_as_draft_errors() -> None:
    with pytest.raises(ConditionDraftError) as excinfo:
        assign_conditions((draft("DELTA_ABOVE", threshold="1"),))

    assert excinfo.value.field == "lookback_bars"


def test_assign_conditions_persists_canonical_params_without_nulls() -> None:
    conditions = assign_conditions(
        (
            draft(
                "DELTA_ABOVE",
                threshold="2.5",
                lookback_bars=3,
                comparator=">=",
                unit="BASE",
                note="momentum",
            ),
        ),
        available_data={"METRICS_5M"},
    )

    assert conditions[0].params == {
        "threshold": "2.5",
        "lookback_bars": 3,
        "comparator": ">=",
    }
    assert conditions[0].required_data == ("METRICS_5M",)
    assert conditions[0].evaluable is True


def test_validate_draft_is_the_single_registry_path() -> None:
    validated = validate_draft("PRICE_CROSS_ABOVE", {"price": "1.25"})
    assert validated.params == {"price": "1.25"}


def test_available_data_is_derived_from_the_context_fields() -> None:
    from oft.domain.conditions import EvaluationContext

    present = available_data(
        EvaluationContext(
            last_price=Decimal("1"),
            delta=Decimal("1"),
            session_state="UNKNOWN",
        )
    )
    assert present == frozenset({"MARK_PRICE", "METRICS_5M", "SESSION"})


def test_optional_decimal_accepts_only_exact_finite_strings_or_decimals() -> None:
    assert _optional_decimal(None) is None
    assert _optional_decimal(True) is None
    assert _optional_decimal(Decimal("1.5")) == Decimal("1.5")
    assert _optional_decimal(Decimal("Infinity")) is None
    assert _optional_decimal(5) is None
    assert _optional_decimal("not-a-number") is None
    assert _optional_decimal("1.5") == Decimal("1.5")


def test_bar_series_rejects_non_lists_missing_values_and_non_mappings() -> None:
    assert _bar_series(None, "close") is None
    assert _bar_series([{"close": "1"}, "oops"], "close") is None
    assert _bar_series([{"close": None}], "close") is None
    assert _bar_series([], "close") == ()
    assert _bar_series([{"close": "1"}], "close") == (Decimal("1"),)


def test_bar_timestamps_reject_missing_malformed_and_empty_values() -> None:
    assert _bar_timestamps(None) is None
    assert _bar_timestamps([]) is None
    assert _bar_timestamps([{"bar_start": "2026-11-06T12:00:00.000Z"}, "oops"]) is None
    assert _bar_timestamps([{"bar_start": None}]) is None
    assert _bar_timestamps([{"bar_start": ""}]) is None
    assert _bar_timestamps([{"bar_start": "2026-11-06T12:00:00.000Z"}]) == (
        "2026-11-06T12:00:00.000Z",
    )


def test_parse_timestamp_accepts_naive_and_rejects_malformed_values() -> None:
    from datetime import UTC

    assert _parse_timestamp(None) is None
    assert _parse_timestamp("") is None
    assert _parse_timestamp("not-a-timestamp") is None
    naive = _parse_timestamp("2026-11-06T12:00:00")
    assert naive is not None and naive.tzinfo is UTC
    assert naive == _parse_timestamp(CREATED_AT)


def test_footprint_imbalance_edge_cases() -> None:
    assert _footprint_imbalance({}) is None
    assert _footprint_imbalance({"bars": []}) is None
    assert _footprint_imbalance({"bars": [{}]}) is None
    assert _footprint_imbalance({"bars": [{"cells": [{"bid_volume": "1"}]}]}) is None
    assert (
        _footprint_imbalance({"bars": [{"cells": [{"bid_volume": "0", "ask_volume": "0"}]}]})
        is None
    )
    assert _footprint_imbalance(
        {"bars": [{"cells": [{"bid_volume": "3", "ask_volume": "1"}]}]}
    ) == Decimal("0.5")
    assert _footprint_imbalance(
        {
            "bars": [
                {
                    "cells": [
                        {"bid_volume": "3", "ask_volume": "1"},
                        {"bid_volume": "2", "ask_volume": "2"},
                    ]
                }
            ]
        }
    ) == Decimal("0.5")


def test_open_interest_delta_pct_edge_cases() -> None:
    assert _open_interest_delta_pct({}) is None
    assert _open_interest_delta_pct({"points": [{"open_interest": "10"}]}) is None
    assert (
        _open_interest_delta_pct({"points": [{"open_interest": "0"}, {"open_interest": "1"}]})
        is None
    )
    assert _open_interest_delta_pct(
        {"points": [{"open_interest": "10"}, {"open_interest": "11"}]}
    ) == Decimal("10")


def test_liquidation_notional_requires_coverage_and_totals() -> None:
    assert _liquidation_notional({}) is None
    assert _liquidation_notional({"coverage": "NONE"}) is None
    assert _liquidation_notional({"coverage": "LIVE"}) is None
    assert _liquidation_notional(
        {
            "coverage": "LIVE",
            "total_long_notional": "1",
            "total_short_notional": "2",
        }
    ) == Decimal("3")


def test_since_deployment_coverage_only_measures_its_own_window() -> None:
    covered = {
        "coverage": "SINCE_DEPLOYMENT",
        "coverage_start": "2026-11-01T00:00:00.000Z",
        "coverage_end": "2026-11-06T00:00:00.000Z",
        "total_long_notional": "1",
        "total_short_notional": "2",
    }
    assert _liquidation_notional(covered, window_start="2026-11-05T00:00:00.000Z") == Decimal("3")

    # The recorder started after the window: this is not a measured zero.
    started_late = {**covered, "coverage_start": "2026-11-07T00:00:00.000Z"}
    assert _liquidation_notional(started_late, window_start="2026-11-05T00:00:00.000Z") is None

    # No end bound means no events were recorded yet.
    assert _liquidation_notional({**covered, "coverage_end": None}) is None

    snapshot = {
        "window": {"start": "2026-11-05T00:00:00.000Z"},
        "orderflow": {"liquidations": started_late},
    }
    context = evaluation_context_from_snapshot(snapshot, as_of=CREATED_AT)
    condition = Condition(
        condition_id="C-1",
        draft_id="c1",
        type=ConditionType.LIQUIDATION_NOTIONAL_ABOVE.value,
        params={"threshold": "1"},
        window_start=CREATED_AT,
        window_end=EXPIRES_AT,
        required_data=("LIQUIDATIONS",),
        evaluable=True,
    )
    result = evaluate_condition(condition, context)

    assert context.liquidation_notional is None
    assert result.met is False
    assert result.evaluable is False


def test_session_state_edge_cases() -> None:
    assert _session_state({}) is None
    assert _session_state({"session": {}}) is None
    assert _session_state({"session": {"is_no_trading": True}}) == "NO_TRADING"
    # A raw label that is not a measured regime is absent, never coerced to the
    # measured value "UNKNOWN" (which could then satisfy SESSION_STATE).
    assert _session_state({"session": {"underlying_session": "WAT"}}) is None
    assert _session_state({"session": {"underlying_session": "unknown"}}) is None
    assert _session_state({"session": {"underlying_session": ""}}) is None
    assert _session_state({"session": {"underlying_session": "UNKNOWN"}}) is None
    assert _session_state({"session": {"underlying_session": 5}}) is None


def test_unrecognized_session_labels_never_satisfy_a_session_condition() -> None:
    condition = Condition(
        condition_id="C-1",
        draft_id="c1",
        type=ConditionType.SESSION_STATE.value,
        params={"session_states": ["UNKNOWN"]},
        window_start=CREATED_AT,
        window_end=EXPIRES_AT,
        required_data=("SESSION",),
        evaluable=True,
    )
    context = evaluation_context_from_snapshot(
        {"session": {"underlying_session": "WAT", "is_no_trading": False}},
        as_of=CREATED_AT,
    )
    result = evaluate_condition(condition, context)

    assert context.session_state is None
    assert result.met is False
    assert result.evaluable is False
    assert result.data_quality == "MISSING"


def test_placeholder_session_is_treated_as_absent() -> None:
    placeholder = {
        "session": {
            "underlying_session": "UNKNOWN",
            "is_no_trading": False,
            "note": "Session state is not measured in this metrics-only slice.",
        }
    }
    assert _session_state(placeholder) is None

    condition = Condition(
        condition_id="C-1",
        draft_id="c1",
        type=ConditionType.SESSION_STATE.value,
        params={"session_states": ["REGULAR"]},
        window_start=CREATED_AT,
        window_end=EXPIRES_AT,
        required_data=("SESSION",),
        evaluable=True,
    )
    context = evaluation_context_from_snapshot(placeholder, as_of=CREATED_AT)
    result = evaluate_condition(condition, context)

    assert result.met is False
    assert result.evaluable is False
    assert result.data_quality == "MISSING"


def test_cvd_points_with_a_hole_make_the_series_unavailable() -> None:
    snapshot = {
        "last_price": "100",
        "orderflow": {"cvd": {"summary": {"delta": "1"}, "points": [{"cvd": "10"}, {"cvd": None}]}},
    }
    context = evaluation_context_from_snapshot(snapshot, as_of=CREATED_AT)

    assert context.cvd_series is None
    assert context.cvd is None

    condition = Condition(
        condition_id="C-1",
        draft_id="c1",
        type=ConditionType.CVD_ABOVE.value,
        params={"threshold": "5", "lookback_bars": 2},
        window_start=CREATED_AT,
        window_end=EXPIRES_AT,
        required_data=("METRICS_5M",),
        evaluable=True,
    )
    result = evaluate_condition(condition, context)

    assert result.met is False
    assert result.evaluable is False


def test_open_interest_points_cannot_mask_a_per_bar_hole() -> None:
    snapshot = {
        "last_price": "100",
        "contract_specs": {"tick_size": "0.01"},
        "bars": [
            {"bar_start": "2026-11-06T11:55:00.000Z", "close": "99", "open_interest": "10"},
            {"bar_start": "2026-11-06T12:00:00.000Z", "close": "100", "open_interest": None},
        ],
        "orderflow": {
            "open_interest": {
                "points": [
                    {"open_interest": "10"},
                    {"open_interest": "11"},
                    {"open_interest": "20"},
                ]
            }
        },
    }
    context = evaluation_context_from_snapshot(snapshot, as_of=CREATED_AT)
    condition = Condition(
        condition_id="C-1",
        draft_id="c1",
        type=ConditionType.OI_DELTA_ABOVE.value,
        params={"threshold": "50", "lookback_bars": 1},
        window_start=WINDOW["start"],
        window_end=EXPIRES_AT,
        required_data=("OPEN_INTEREST",),
        evaluable=True,
    )

    result = evaluate_condition(condition, context)

    assert context.oi_series is None
    assert context.oi_delta_pct == Decimal("100")
    # A missing per-bar series is unevaluable; the +100% whole-window scalar
    # must never stand in for the requested one-bar lookback.
    assert result.met is False
    assert result.evaluable is False
    assert result.observed_value is None
    assert result.data_quality == "PARTIAL"


def test_snapshot_bars_build_the_per_bar_series() -> None:
    snapshot = {
        "last_price": "100",
        "contract_specs": {"tick_size": "0.01"},
        "bars": [
            {
                "bar_start": "2026-11-06T11:55:00.000Z",
                "close": "99",
                "cvd": "9",
                "volume": "10",
                "delta": "1",
                "open_interest": "10",
            },
            {
                "bar_start": "2026-11-06T12:00:00.000Z",
                "close": "100",
                "cvd": "10",
                "volume": "12",
                "delta": "2",
                "open_interest": "11",
            },
        ],
    }

    context = evaluation_context_from_snapshot(snapshot, as_of=CREATED_AT)

    assert context.bar_timestamps == (
        "2026-11-06T11:55:00.000Z",
        "2026-11-06T12:00:00.000Z",
    )
    assert context.delta_series == (Decimal("1"), Decimal("2"))
    assert context.volume_series == (Decimal("10"), Decimal("12"))
    assert context.cvd_series == (Decimal("9"), Decimal("10"))
    assert context.oi_series == (Decimal("10"), Decimal("11"))


def test_assigned_condition_windows_are_normalized_to_utc() -> None:
    conditions = assign_conditions(
        (
            ConditionDraft(
                draft_id="c1",
                type="TIME_WINDOW",
                params={},
                window={
                    "start": "2026-11-06T17:30:00+05:30",
                    "end": "2026-11-06T18:30:00+05:30",
                },
                required_data=(),
                description="utc window",
            ),
        )
    )

    assert conditions[0].window_start == "2026-11-06T12:00:00.000Z"
    assert conditions[0].window_end == "2026-11-06T13:00:00.000Z"


def test_context_falls_back_to_mark_price_and_last_close() -> None:
    marked = evaluation_context_from_snapshot(
        {
            "mark_price": "100",
            "contract_specs": {"tick_size": "0"},
            "bars": [{"close": "99"}],
        },
        as_of=CREATED_AT,
    )
    assert marked.last_price == Decimal("100")
    assert marked.tick_size is None
    assert marked.previous_price is None

    closed = evaluation_context_from_snapshot({"bars": [{"close": "99"}]}, as_of=CREATED_AT)
    assert closed.last_price == Decimal("99")
    assert closed.previous_price is None
