"""Funding accrual arithmetic and the position payload's funding accumulator."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oft.domain.pnl import funding_amount_usd, plain_money_str, quantize_money


def test_long_pays_a_positive_rate_and_short_receives_it() -> None:
    long_amount = funding_amount_usd("LONG", Decimal("0.0001"), Decimal("70000"), Decimal("0.01"))
    short_amount = funding_amount_usd("SHORT", Decimal("0.0001"), Decimal("70000"), Decimal("0.01"))
    assert long_amount == Decimal("-0.07000000")
    assert short_amount == Decimal("0.07000000")
    assert long_amount == -short_amount


def test_a_negative_rate_reverses_the_sign() -> None:
    assert funding_amount_usd("LONG", Decimal("-0.0001"), Decimal("70000"), Decimal("0.01")) == (
        Decimal("0.07000000")
    )


def test_funding_is_exact_and_quantized_once() -> None:
    amount = funding_amount_usd("LONG", Decimal("0.00012345"), Decimal("70000.1"), Decimal("0.333"))
    assert amount == quantize_money(
        Decimal("0.00012345") * Decimal("70000.1") * Decimal("0.333") * -1
    )


def test_the_accumulated_funding_term_adds_exactly() -> None:
    first = funding_amount_usd("LONG", Decimal("0.0001"), Decimal("70000"), Decimal("0.01"))
    second = funding_amount_usd("LONG", Decimal("0.0002"), Decimal("71000"), Decimal("0.01"))
    total = quantize_money(first + second)
    assert total == Decimal("-0.21200000")
    assert plain_money_str(total) == "-0.21200000"


def test_a_missing_mark_is_never_treated_as_zero() -> None:
    with pytest.raises(ValueError):
        funding_amount_usd("LONG", Decimal("0.0001"), Decimal("0"), Decimal("0.01"))
    with pytest.raises(ValueError):
        funding_amount_usd("LONG", Decimal("0.0001"), Decimal("70000"), Decimal("-1"))
