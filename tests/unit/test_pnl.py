"""Pure PnL arithmetic: exact decimals, 8-dp quantization, LONG/SHORT symmetry."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oft.domain.pnl import (
    add_stop_risk,
    decimal_str,
    fees_total,
    funding_amount_usd,
    gross_pnl_usd,
    mae_mfe_from_bars,
    mae_mfe_from_sized_bars,
    money_str,
    net_pnl_usd,
    outcome_tags,
    quantize_money,
    r_multiple,
    realized_pnl_slice,
    return_percent,
    weighted_average_entry,
)


def test_quantize_money_rounds_down_to_eight_decimal_places() -> None:
    assert quantize_money(Decimal("1.123456789")) == Decimal("1.12345678")
    assert quantize_money(Decimal("-1.123456789")) == Decimal("-1.12345678")
    assert quantize_money(Decimal("0")) == Decimal("0.00000000")


def test_money_and_decimal_strings_are_exact_and_non_exponent() -> None:
    assert money_str(Decimal("10.5")) == "10.50000000"
    assert money_str(Decimal("-0.00000001")) == "-0.00000001"
    assert decimal_str(Decimal("70000.10")) == "70000.10"
    assert decimal_str(Decimal("1E+3")) == "1000"


def test_quantize_money_rejects_an_implausible_magnitude() -> None:
    with pytest.raises(ValueError):
        quantize_money(Decimal("1e100"))
    with pytest.raises(ValueError):
        quantize_money(Decimal("NaN"))
    with pytest.raises(ValueError):
        quantize_money(Decimal("Infinity"))


def test_long_gross_pnl_is_exit_minus_entry_times_size() -> None:
    assert gross_pnl_usd("LONG", Decimal("70000"), Decimal("71000"), Decimal("0.5")) == Decimal(
        "500.00000000"
    )
    assert gross_pnl_usd("LONG", Decimal("70000"), Decimal("69000"), Decimal("0.5")) == Decimal(
        "-500.00000000"
    )


def test_short_gross_pnl_is_entry_minus_exit_times_size() -> None:
    assert gross_pnl_usd("SHORT", Decimal("70000"), Decimal("69000"), Decimal("0.5")) == Decimal(
        "500.00000000"
    )
    assert gross_pnl_usd("SHORT", Decimal("70000"), Decimal("71000"), Decimal("0.5")) == Decimal(
        "-500.00000000"
    )


def test_gross_pnl_is_symmetric_for_long_and_short_at_the_same_distance() -> None:
    long_pnl = gross_pnl_usd("LONG", Decimal("100"), Decimal("90"), Decimal("2"))
    short_pnl = gross_pnl_usd("SHORT", Decimal("100"), Decimal("110"), Decimal("2"))
    assert long_pnl == short_pnl == Decimal("-20.00000000")


def test_fees_total_sums_and_quantizes() -> None:
    assert fees_total(Decimal("0.35"), Decimal("0.345"), Decimal("0.000000001")) == Decimal(
        "0.69500000"
    )
    assert fees_total() == Decimal("0.00000000")


def test_net_pnl_is_gross_minus_fees_plus_funding() -> None:
    assert net_pnl_usd(Decimal("-10.2"), Decimal("0.695"), Decimal("0")) == Decimal("-10.89500000")
    assert net_pnl_usd(Decimal("5"), Decimal("0.25"), Decimal("-0.05")) == Decimal("4.70000000")


def test_invalid_side_and_negative_size_fail_closed() -> None:
    with pytest.raises(ValueError):
        gross_pnl_usd("FLAT", Decimal("1"), Decimal("2"), Decimal("1"))
    with pytest.raises(ValueError):
        gross_pnl_usd("LONG", Decimal("1"), Decimal("2"), Decimal("-1"))


def test_realized_slice_matches_gross_pnl() -> None:
    assert realized_pnl_slice("LONG", Decimal("70000"), Decimal("70500"), Decimal("0.5")) == (
        Decimal("250.00000000")
    )
    assert realized_pnl_slice("SHORT", Decimal("70000"), Decimal("70500"), Decimal("0.5")) == (
        Decimal("-250.00000000")
    )


def test_weighted_average_entry_is_size_weighted_and_exact() -> None:
    avg = weighted_average_entry(Decimal("70000"), Decimal("1"), Decimal("71000"), Decimal("1"))
    assert avg == Decimal("70500.00000000")
    avg = weighted_average_entry(Decimal("70000"), Decimal("3"), Decimal("72000"), Decimal("1"))
    assert avg == Decimal("70500.00000000")
    with pytest.raises(ValueError):
        weighted_average_entry(Decimal("70000"), Decimal("0"), Decimal("71000"), Decimal("1"))
    with pytest.raises(ValueError):
        weighted_average_entry(Decimal("70000"), Decimal("1"), Decimal("0"), Decimal("1"))


def test_add_stop_risk_is_absolute_distance_times_quantity() -> None:
    assert add_stop_risk(Decimal("70500"), Decimal("69000"), Decimal("0.5")) == (
        Decimal("750.00000000")
    )
    with pytest.raises(ValueError):
        add_stop_risk(Decimal("70500"), Decimal("69000"), Decimal("0"))


def test_funding_signs_by_side() -> None:
    # A positive rate means the LONG pays and the SHORT receives it.
    assert funding_amount_usd("LONG", Decimal("0.0001"), Decimal("70000"), Decimal("0.01")) == (
        Decimal("-0.07000000")
    )
    assert funding_amount_usd("SHORT", Decimal("0.0001"), Decimal("70000"), Decimal("0.01")) == (
        Decimal("0.07000000")
    )
    assert funding_amount_usd("LONG", Decimal("-0.0001"), Decimal("70000"), Decimal("0.01")) == (
        Decimal("0.07000000")
    )
    with pytest.raises(ValueError):
        funding_amount_usd("FLAT", Decimal("0.0001"), Decimal("70000"), Decimal("0.01"))
    with pytest.raises(ValueError):
        funding_amount_usd("LONG", Decimal("0.0001"), Decimal("0"), Decimal("0.01"))


def test_mae_mfe_use_recorded_highs_and_lows_with_the_documented_signs() -> None:
    bars = (
        {"low": "69000", "high": "71200"},
        {"low": "68500", "high": "71500"},
    )
    mae, mfe = mae_mfe_from_bars("LONG", Decimal("70000"), Decimal("1"), bars)
    assert mae == Decimal("-1500.00000000")
    assert mfe == Decimal("1500.00000000")


def test_mae_mfe_are_symmetric_for_a_short() -> None:
    bars = (
        {"low": "69000", "high": "71200"},
        {"low": "68500", "high": "71500"},
    )
    mae, mfe = mae_mfe_from_bars("SHORT", Decimal("70000"), Decimal("1"), bars)
    assert mae == Decimal("-1500.00000000")
    assert mfe == Decimal("1500.00000000")


def test_mae_mfe_never_cross_zero() -> None:
    bars = ({"low": "70100", "high": "71500"},)
    mae, mfe = mae_mfe_from_bars("LONG", Decimal("70000"), Decimal("1"), bars)
    assert mae == Decimal("0.00000000")
    assert mfe == Decimal("1500.00000000")
    bars = ({"low": "68500", "high": "69900"},)
    mae, mfe = mae_mfe_from_bars("LONG", Decimal("70000"), Decimal("1"), bars)
    assert mae == Decimal("-1500.00000000")
    assert mfe == Decimal("0.00000000")


def test_mae_mfe_skip_unusable_bars() -> None:
    bars = ({"low": None, "high": "1"}, {"low": "69000", "high": "71000"})
    mae, mfe = mae_mfe_from_bars("LONG", Decimal("70000"), Decimal("1"), bars)
    assert mae == Decimal("-1000.00000000")
    assert mfe == Decimal("1000.00000000")
    with pytest.raises(ValueError):
        mae_mfe_from_bars("FLAT", Decimal("70000"), Decimal("1"), bars)


def test_r_multiple_uses_frozen_risk_and_is_null_without_risk() -> None:
    assert r_multiple(Decimal("20"), Decimal("10")) == 2.0
    assert r_multiple(Decimal("-5"), Decimal("10")) == -0.5
    assert r_multiple(Decimal("20"), None) is None
    assert r_multiple(Decimal("20"), Decimal("0")) is None
    assert r_multiple(Decimal("20"), Decimal("-1")) is None


def test_return_percent_uses_entry_notional() -> None:
    assert return_percent(Decimal("100"), Decimal("10000")) == 1.0
    assert return_percent(Decimal("-50"), Decimal("10000")) == -0.5
    assert return_percent(Decimal("100"), Decimal("0")) == 0.0
    assert return_percent(Decimal("100"), None) == 0.0


def test_outcome_tags_are_deterministic_and_cover_every_result() -> None:
    assert outcome_tags(Decimal("1")) == ("WIN",)
    assert outcome_tags(Decimal("-1")) == ("LOSS",)
    assert outcome_tags(Decimal("0")) == ("BE",)
    assert outcome_tags(
        Decimal("-1"),
        stopped=True,
        data_partial=True,
        manual=True,
        partial=True,
    ) == ("DATA_PARTIAL", "LOSS", "STOPPED", "PARTIAL", "MANUAL_CLOSE")
    assert outcome_tags(Decimal("1"), target=True) == ("WIN", "TARGET")


def test_mae_mfe_use_the_size_in_force_at_each_bar() -> None:
    bars = (
        {"low": "69000", "high": "70200"},
        {"low": "69900", "high": "70100"},
    )
    # Before a 50% partial the excursion is measured on the full size; after it
    # the same price move counts half.
    mae, mfe = mae_mfe_from_sized_bars(
        "LONG",
        bars,
        ((Decimal("70000"), Decimal("1")), (Decimal("70000"), Decimal("0.5"))),
    )
    assert mae == Decimal("-1000.00000000")
    assert mfe == Decimal("200.00000000")
    with pytest.raises(ValueError):
        mae_mfe_from_sized_bars("LONG", bars, ((Decimal("70000"), Decimal("1")),))


def test_mae_mfe_skip_an_inconsistent_bar() -> None:
    # low > high and an absurd finite price are both corrupt; neither may raise
    # or be measured as an excursion.
    bars = (
        {"low": "71000", "high": "69000"},
        {"low": "69000", "high": "1e100"},
        {"low": "69500", "high": "70500"},
    )
    mae, mfe = mae_mfe_from_bars("LONG", Decimal("70000"), Decimal("1"), bars)
    assert mae == Decimal("-500.00000000")
    assert mfe == Decimal("500.00000000")


def test_an_implausible_funding_rate_fails_closed() -> None:
    with pytest.raises(ValueError):
        funding_amount_usd("LONG", Decimal("1e100"), Decimal("70000"), Decimal("1"))
    with pytest.raises(ValueError):
        funding_amount_usd("LONG", Decimal("0.0001"), Decimal("1e100"), Decimal("1"))
