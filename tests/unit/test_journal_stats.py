"""Deterministic journal statistics: eligibility, wins, expectancy, avg R, periods."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

import pytest

from oft.domain.journal import (
    DEFINITIONS,
    JournalAvgR,
    JournalError,
    JournalStats,
    JournalTotals,
    PeriodWindow,
    compute_stats,
    parse_period,
    resolve_period,
)

EIGHT_DP = Decimal("0.00000001")
NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)


def _trade(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "trade_id": "TRD-1",
        "position_id": "POS-1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "opened_at": "2026-11-13T12:00:00.000Z",
        "closed_at": "2026-11-14T11:00:00.000Z",
        "gross_pnl_usd": "100.00000000",
        "fees_usd": "1.00000000",
        "funding_usd": "0",
        "net_pnl_usd": "99.00000000",
        "r_multiple": 1.5,
        "return_pct": 1.0,
        "outcome_tags": ["WIN"],
        "data_quality": {"status": "COMPLETE", "reasons": []},
    }
    payload.update(overrides)
    return payload


def _expect(value: str) -> Decimal:
    return Decimal(value).quantize(EIGHT_DP, rounding=ROUND_DOWN)


def _stats(trades: Any, **kwargs: Any) -> dict[str, Any]:
    return compute_stats(trades, **kwargs).to_contract()


# -- periods ---------------------------------------------------------------


def test_periods_accept_days_weeks_and_months() -> None:
    assert parse_period("3d").days == 3
    assert parse_period("8w").days == 56
    # A month is documented as a fixed 30 days, never a calendar month.
    assert parse_period("2m").days == 60


def test_period_window_is_the_utc_clock_window_on_closed_at() -> None:
    window = resolve_period("8w", NOW)
    assert window is not None
    assert window.label == "8w"
    assert window.basis == "closed_at"
    assert window.end == "2026-11-14T12:00:00.000000Z"
    assert window.start == "2026-09-19T12:00:00.000000Z"


def test_an_omitted_period_means_all_trades() -> None:
    assert resolve_period(None, NOW) is None


@pytest.mark.parametrize("label", ["", "0d", "8x", "-1d", "d", "1.5d", "w"])
def test_invalid_periods_fail_with_a_typed_validation_error(label: str) -> None:
    with pytest.raises(JournalError) as excinfo:
        parse_period(label)
    assert excinfo.value.code == "VALIDATION_FAILED"


@pytest.mark.parametrize("label", ["999999d", "999999999999999m"])
def test_a_huge_but_shape_valid_period_is_a_typed_validation_error(label: str) -> None:
    # 999999999999999m is 16 characters: the API's max_length cannot be relied
    # on to keep the duration inside the calendar.
    with pytest.raises(JournalError) as excinfo:
        parse_period(label)
    assert excinfo.value.code == "VALIDATION_FAILED"
    assert "exceeds" in excinfo.value.message


def test_a_window_that_does_not_fit_the_calendar_is_a_typed_validation_error() -> None:
    # The maximum documented period is accepted, but a clock near datetime.min
    # cannot subtract it without overflowing.
    with pytest.raises(JournalError) as excinfo:
        resolve_period("365000d", datetime(1, 1, 1, tzinfo=UTC))
    assert excinfo.value.code == "VALIDATION_FAILED"
    assert "calendar" in excinfo.value.message


# -- undefined -------------------------------------------------------------


def test_no_closed_trades_are_undefined_never_zero() -> None:
    stats = _stats([], open_positions=0)

    assert stats["status"] == "UNDEFINED"
    assert stats["data_quality"] == "MISSING"
    assert str(stats["reason"]).startswith("metrics undefined")
    assert stats["sample_size"] == 0
    assert stats["denominator"] == 0
    assert stats["win_rate"] is None
    assert stats["expectancy_usd"] is None
    assert stats["avg_r"]["value"] is None
    assert stats["totals"] == {
        "gross_pnl_usd": None,
        "fees_usd": None,
        "funding_usd": None,
        "net_pnl_usd": None,
    }
    # The definitions are always present so a reader can see what is missing.
    assert stats["definitions"] == DEFINITIONS
    assert stats["exclusions"]["open_positions"] == 0


def test_open_positions_are_excluded_and_counted_but_do_not_define_metrics() -> None:
    stats = _stats([], open_positions=3)

    assert stats["status"] == "UNDEFINED"
    assert stats["exclusions"]["open_positions"] == 3
    assert stats["win_rate"] is None


# -- wins and ties ---------------------------------------------------------


def test_win_rate_is_net_positive_over_eligible_and_ties_are_not_wins() -> None:
    stats = _stats(
        [
            _trade(trade_id="TRD-W", net_pnl_usd="10.00000000"),
            _trade(trade_id="TRD-L", net_pnl_usd="-5.00000000", r_multiple=-0.5),
            _trade(trade_id="TRD-T", net_pnl_usd="0", r_multiple=0),
        ]
    )

    assert stats["status"] == "DEFINED"
    assert stats["sample_size"] == 3
    assert stats["denominator"] == 3
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["ties"] == 1
    assert stats["win_rate"] == pytest.approx(1 / 3)
    assert Decimal(stats["expectancy_usd"]) == _expect("1.666666666666666666666666666")


def test_expectancy_is_the_mean_net_and_totals_sum_every_term() -> None:
    stats = _stats(
        [
            _trade(
                trade_id="TRD-A",
                gross_pnl_usd="20.00000000",
                fees_usd="1.00000000",
                funding_usd="-2.00000000",
                net_pnl_usd="17.00000000",
            ),
            _trade(
                trade_id="TRD-B",
                gross_pnl_usd="-10.00000000",
                fees_usd="2.00000000",
                funding_usd="1.00000000",
                net_pnl_usd="-11.00000000",
                r_multiple=-1.0,
            ),
        ]
    )

    assert Decimal(stats["expectancy_usd"]) == _expect("3.00000000")
    assert Decimal(stats["totals"]["gross_pnl_usd"]) == _expect("10")
    assert Decimal(stats["totals"]["fees_usd"]) == _expect("3")
    assert Decimal(stats["totals"]["funding_usd"]) == _expect("-1")
    assert Decimal(stats["totals"]["net_pnl_usd"]) == _expect("6")
    assert stats["data_quality"] == "COMPLETE"


# -- exclusions ------------------------------------------------------------


def test_partial_quality_and_partial_tags_are_excluded_and_counted() -> None:
    stats = _stats(
        [
            _trade(trade_id="TRD-OK"),
            _trade(
                trade_id="TRD-DQ",
                data_quality={"status": "PARTIAL", "reasons": ["MAE not measured"]},
            ),
            _trade(trade_id="TRD-TAG", outcome_tags=["LOSS", "PARTIAL"]),
            _trade(trade_id="TRD-DTAG", outcome_tags=["LOSS", "DATA_PARTIAL"]),
        ],
        open_positions=2,
    )

    assert stats["sample_size"] == 1
    assert stats["exclusions"]["partial_trades"] == 3
    assert stats["exclusions"]["open_positions"] == 2
    assert stats["data_quality"] == "COMPLETE"


def test_null_r_trades_are_excluded_from_avg_r_and_counted() -> None:
    stats = _stats(
        [
            _trade(trade_id="TRD-R1", r_multiple=1.0),
            _trade(trade_id="TRD-R2", r_multiple=3.0),
            _trade(trade_id="TRD-RN", r_multiple=None),
        ]
    )

    assert stats["avg_r"]["samples"] == 2
    assert stats["avg_r"]["excluded_null_r"] == 1
    assert stats["avg_r"]["value"] == pytest.approx(2.0)
    assert stats["sample_size"] == 3


def test_no_r_samples_leaves_avg_r_null_but_the_metrics_defined() -> None:
    stats = _stats([_trade(r_multiple=None)])

    assert stats["status"] == "DEFINED"
    assert stats["avg_r"]["value"] is None
    assert stats["avg_r"]["samples"] == 0
    assert stats["avg_r"]["excluded_null_r"] == 1


def test_a_period_excludes_out_of_window_trades_and_counts_them() -> None:
    window = resolve_period("1d", NOW)
    assert window is not None
    inside = _trade(trade_id="TRD-IN", closed_at="2026-11-14T11:00:00.000Z")
    outside = _trade(trade_id="TRD-OUT", closed_at="2026-11-01T11:00:00.000Z")

    stats = _stats([inside, outside], window=window)

    assert stats["sample_size"] == 1
    assert stats["exclusions"]["outside_period"] == 1
    assert stats["period"] == window.to_contract()


def test_period_start_boundary_is_inclusive() -> None:
    window = resolve_period("1d", NOW)
    assert window is not None
    trade = _trade(closed_at=window.start)

    stats = _stats([trade], window=window)

    assert stats["sample_size"] == 1
    assert stats["exclusions"]["outside_period"] == 0


def test_unreadable_rows_are_excluded_and_counted() -> None:
    stats = _stats(
        [
            None,
            _trade(trade_id="TRD-BAD-NET", net_pnl_usd="not-money"),
            _trade(trade_id="TRD-BAD-TS", closed_at="not-a-timestamp"),
            _trade(trade_id="TRD-OK"),
        ]
    )

    assert stats["sample_size"] == 1
    assert stats["exclusions"]["unreadable_trades"] == 3
    assert stats["data_quality"] == "PARTIAL"


def test_week_period_is_deterministic_under_a_fixed_clock() -> None:
    first = resolve_period("8w", NOW)
    second = resolve_period("8w", NOW)

    assert first is not None and second is not None
    assert first == second
    assert first.start == "2026-09-19T12:00:00.000000Z"
    assert datetime.fromisoformat(first.start.replace("Z", "+00:00")) == NOW - timedelta(days=56)


def test_a_non_string_period_is_rejected() -> None:
    with pytest.raises(JournalError):
        parse_period(8)  # type: ignore[arg-type]


def test_a_naive_clock_is_treated_as_utc() -> None:
    window = resolve_period("1d", datetime(2026, 11, 14, 12, 0, 0))

    assert window is not None
    assert window.end == "2026-11-14T12:00:00.000000Z"
    assert window.start == "2026-11-13T12:00:00.000000Z"


def test_datetime_and_missing_closed_at_are_classified_defensively() -> None:
    aware = datetime(2026, 11, 14, 11, 0, 0, tzinfo=UTC)
    stats = _stats(
        [
            _trade(closed_at=aware),
            _trade(trade_id="TRD-NONE", closed_at=None),
        ]
    )

    assert stats["sample_size"] == 1
    assert stats["exclusions"]["unreadable_trades"] == 1


def test_non_numeric_r_and_non_list_tags_are_handled() -> None:
    stats = _stats(
        [
            _trade(
                r_multiple="not-a-number",
                outcome_tags=None,
                data_quality={"status": "COMPLETE", "reasons": []},
            )
        ]
    )

    assert stats["status"] == "DEFINED"
    assert stats["avg_r"]["samples"] == 0
    assert stats["avg_r"]["excluded_null_r"] == 1


def test_missing_quality_non_decimal_net_rows_are_unreadable() -> None:
    stats = _stats(
        [
            _trade(data_quality=None),
            _trade(trade_id="TRD-BOOL", net_pnl_usd=True),
            _trade(trade_id="TRD-NONET", net_pnl_usd=None),
        ]
    )

    assert stats["sample_size"] == 0
    assert stats["exclusions"]["unreadable_trades"] == 3


def test_an_unmeasurable_component_degrades_the_totals() -> None:
    stats = _stats([_trade(gross_pnl_usd=None, fees_usd="1e20")])

    assert stats["status"] == "DEFINED"
    assert stats["totals"]["gross_pnl_usd"] is None
    assert stats["totals"]["fees_usd"] is None
    assert stats["data_quality"] == "PARTIAL"


def test_an_out_of_range_net_total_leaves_the_total_empty_and_degraded() -> None:
    stats = _stats(
        [
            _trade(trade_id="TRD-MAX-A", net_pnl_usd="1000000000000000"),
            _trade(trade_id="TRD-MAX-B", net_pnl_usd="1000000000000000"),
        ]
    )

    assert stats["status"] == "DEFINED"
    # The mean is bounded by the per-trade bound and is still rendered.
    assert stats["expectancy_usd"] == "1000000000000000.00000000"
    # The sum is outside the plausible money range, so it is not fabricated.
    assert stats["totals"]["net_pnl_usd"] is None
    assert stats["data_quality"] == "PARTIAL"


def test_a_zero_net_tie_renders_a_plain_zero_money_string() -> None:
    stats = _stats([_trade(net_pnl_usd="0")])

    assert stats["ties"] == 1
    assert stats["win_rate"] == 0.0
    assert stats["totals"]["net_pnl_usd"] == "0"
    assert stats["expectancy_usd"] == "0"


def test_an_out_of_range_gross_total_degrades_quality_symmetrically() -> None:
    stats = _stats(
        [
            _trade(trade_id="TRD-GROSS-A", gross_pnl_usd="1000000000000000"),
            _trade(trade_id="TRD-GROSS-B", gross_pnl_usd="1000000000000000"),
        ]
    )

    assert stats["status"] == "DEFINED"
    # Two per-trade values at the plausible bound sum beyond it: the total is
    # not fabricated and the quality degrades just like an unreadable term.
    assert stats["totals"]["gross_pnl_usd"] is None
    assert stats["data_quality"] == "PARTIAL"


# -- typed invariants ------------------------------------------------------


def _valid_defined() -> JournalStats:
    return JournalStats(
        status="DEFINED",
        reason=None,
        period=None,
        sample_size=2,
        denominator=2,
        exclusions={
            "open_positions": 0,
            "partial_trades": 0,
            "unreadable_trades": 0,
            "outside_period": 0,
        },
        definitions=dict(DEFINITIONS),
        wins=1,
        losses=1,
        ties=0,
        win_rate=0.5,
        expectancy_usd="1",
        avg_r=JournalAvgR(value=1.0, samples=2, excluded_null_r=0),
        totals=JournalTotals(gross_pnl_usd="1", fees_usd="0", funding_usd="0", net_pnl_usd="1"),
        data_quality="COMPLETE",
    )


def test_a_valid_defined_stats_object_renders_the_contract_shape() -> None:
    contract = _valid_defined().to_contract()

    assert contract["status"] == "DEFINED"
    assert contract["denominator"] == 2
    assert contract["avg_r"] == {"value": 1.0, "samples": 2, "excluded_null_r": 0}
    assert contract["definitions"] == DEFINITIONS


def test_defined_stats_require_buckets_to_sum_to_the_sample() -> None:
    with pytest.raises(ValueError, match="wins \\+ losses \\+ ties"):
        replace_defined(wins=2)


def test_defined_stats_require_a_positive_sample() -> None:
    with pytest.raises(ValueError, match="positive sample size"):
        replace_defined(sample_size=0)


def test_defined_stats_require_the_avg_r_split_to_account_for_every_trade() -> None:
    with pytest.raises(ValueError, match="average-R samples"):
        replace_defined(avg_r=JournalAvgR(value=1.0, samples=1, excluded_null_r=0))


def test_defined_stats_require_metrics() -> None:
    with pytest.raises(ValueError, match="win rate and expectancy"):
        replace_defined(win_rate=None)


def test_undefined_stats_must_not_carry_metrics() -> None:
    with pytest.raises(ValueError, match="must not fabricate"):
        replace_defined(status="UNDEFINED", sample_size=0, wins=0, losses=0, win_rate=0.5)


def test_undefined_stats_must_have_empty_buckets() -> None:
    with pytest.raises(ValueError, match="zero sample and empty buckets"):
        replace_defined(status="UNDEFINED", sample_size=0, wins=1, losses=0, win_rate=None)


def test_undefined_stats_must_have_empty_totals() -> None:
    with pytest.raises(ValueError, match="empty totals"):
        replace_defined(
            status="UNDEFINED",
            sample_size=0,
            wins=0,
            losses=0,
            win_rate=None,
            expectancy_usd=None,
            avg_r=JournalAvgR(value=None, samples=0, excluded_null_r=0),
        )


def test_avg_r_requires_a_value_iff_there_are_samples() -> None:
    with pytest.raises(ValueError, match="present iff samples"):
        JournalAvgR(value=None, samples=1, excluded_null_r=0)
    with pytest.raises(ValueError, match="present iff samples"):
        JournalAvgR(value=1.0, samples=0, excluded_null_r=0)


def replace_defined(**overrides: Any) -> JournalStats:
    from dataclasses import replace

    return replace(_valid_defined(), **overrides)


# -- period window validation ---------------------------------------------


@pytest.mark.parametrize(
    "start,end,basis",
    [
        ("not-a-timestamp", "2026-11-14T12:00:00Z", "closed_at"),
        ("2026-11-13T12:00:00Z", "not-a-timestamp", "closed_at"),
        ("2026-11-15T12:00:00Z", "2026-11-14T12:00:00Z", "closed_at"),
        ("2026-11-13T12:00:00Z", "2026-11-14T12:00:00Z", "opened_at"),
    ],
)
def test_an_invalid_period_window_is_a_typed_validation_error(
    start: str, end: str, basis: str
) -> None:
    with pytest.raises(JournalError) as excinfo:
        PeriodWindow(label="1d", start=start, end=end, basis=basis)
    assert excinfo.value.code == "VALIDATION_FAILED"


def test_an_empty_period_label_is_rejected() -> None:
    with pytest.raises(JournalError) as excinfo:
        PeriodWindow(label=" ", start="2026-11-13T12:00:00Z", end="2026-11-14T12:00:00Z")
    assert excinfo.value.code == "VALIDATION_FAILED"


def test_compute_stats_rejects_a_window_with_unparseable_bounds() -> None:
    # Bypass the constructor guard (e.g. a future serialization path) to prove
    # the computation itself fails closed instead of classifying every row as
    # outside the period.
    window = object.__new__(PeriodWindow)
    object.__setattr__(window, "label", "1d")
    object.__setattr__(window, "start", "not-a-timestamp")
    object.__setattr__(window, "end", "2026-11-14T12:00:00Z")
    object.__setattr__(window, "basis", "closed_at")

    with pytest.raises(JournalError) as excinfo:
        compute_stats([_trade()], window=window)

    assert excinfo.value.code == "VALIDATION_FAILED"


def test_an_unknown_status_or_quality_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid statistics status"):
        replace_defined(status="MAYBE")
    with pytest.raises(ValueError, match="invalid data quality"):
        replace_defined(data_quality="STALE")


def test_undefined_stats_must_have_an_empty_avg_r_result() -> None:
    with pytest.raises(ValueError, match="empty average-R"):
        replace_defined(
            status="UNDEFINED",
            sample_size=0,
            wins=0,
            losses=0,
            ties=0,
            win_rate=None,
            expectancy_usd=None,
            avg_r=JournalAvgR(value=1.0, samples=1, excluded_null_r=0),
        )


@pytest.mark.parametrize("field", ["fees_usd", "funding_usd"])
def test_an_out_of_range_fee_or_funding_total_degrades_quality(field: str) -> None:
    stats = _stats(
        [
            _trade(trade_id="TRD-TOTAL-A", **{field: "1000000000000000"}),
            _trade(trade_id="TRD-TOTAL-B", **{field: "1000000000000000"}),
        ]
    )

    assert stats["status"] == "DEFINED"
    assert stats["totals"][field] is None
    assert stats["data_quality"] == "PARTIAL"


def test_a_negative_average_r_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        JournalAvgR(value=None, samples=0, excluded_null_r=-1)
