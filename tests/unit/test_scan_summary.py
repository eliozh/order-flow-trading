"""Bounded cost formatting shared by the scan API and the Telegram push."""

from __future__ import annotations

from decimal import Decimal

from oft.application.scan_summary import format_cost_total


def test_a_zero_total_renders_plain_zero() -> None:
    assert format_cost_total(Decimal("0")) == "0"
    assert format_cost_total(Decimal("-0")) == "0"


def test_a_finite_total_renders_fixed_point() -> None:
    assert format_cost_total(Decimal("0.842")) == "0.842"
    assert format_cost_total(Decimal("0.0005")) == "0.0005"


def test_a_non_finite_total_renders_bounded_na() -> None:
    assert format_cost_total(Decimal("NaN")) == "N/A"
    assert format_cost_total(Decimal("sNaN")) == "N/A"
    assert format_cost_total(Decimal("Infinity")) == "N/A"
    assert format_cost_total(Decimal("-Infinity")) == "N/A"


def test_a_hostile_exponent_renders_na_without_materializing() -> None:
    # ``format(Decimal("1e100000000"), "f")`` allocates roughly 100 MB.
    assert format_cost_total(Decimal("1e100000000")) == "N/A"
    assert format_cost_total(Decimal("1e-100000000")) == "N/A"
