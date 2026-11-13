"""Pure PnL arithmetic: exact decimals, 8-dp quantization, LONG/SHORT symmetry."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oft.domain.pnl import (
    decimal_str,
    fees_total,
    gross_pnl_usd,
    money_str,
    net_pnl_usd,
    quantize_money,
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
