"""Deterministic money and PnL arithmetic for a full position close.

Pure standard-library functions only: no I/O, clock, or provider type enters
here. Every value is an exact ``Decimal`` quantized to 8 decimal places with
``ROUND_DOWN`` so it matches the paper broker's money quantization. Strings that
cross a contract boundary are produced by :func:`money_str` /
:func:`decimal_str`, never by Python float formatting.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

EIGHT_DP = Decimal("0.00000001")
ZERO = Decimal("0")


def quantize_money(value: Decimal) -> Decimal:
    """Quantize an amount to 8 decimal places, rounding down like the broker."""
    return Decimal(value).quantize(EIGHT_DP, rounding=ROUND_DOWN)


def money_str(value: Decimal) -> str:
    """An exact 8-dp money string accepted by ``common.schema.json#/$defs/money``."""
    return format(quantize_money(value), "f")


def decimal_str(value: Decimal) -> str:
    """An exact non-exponent decimal string for a price or quantity."""
    return format(Decimal(value), "f")


def plain_money_str(value: Decimal) -> str:
    """A plain decimal money string; an exact zero is ``"0"``, never ``"0E-8"``."""
    amount = Decimal(value)
    if amount == 0:
        return "0"
    return format(amount, "f")


def gross_pnl_usd(
    side: str,
    entry_price: Decimal,
    exit_price: Decimal,
    size: Decimal,
) -> Decimal:
    """Gross PnL: ``(exit - entry) * size`` for LONG, ``(entry - exit) * size`` for SHORT."""
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if size < 0:
        raise ValueError("size must be non-negative")
    raw = (exit_price - entry_price) * size if side == "LONG" else (entry_price - exit_price) * size
    return quantize_money(raw)


def fees_total(*fees: Decimal) -> Decimal:
    """Sum every fee term, quantized to 8 dp."""
    total = ZERO
    for fee in fees:
        total += Decimal(fee)
    return quantize_money(total)


def net_pnl_usd(gross_pnl: Decimal, fees: Decimal, funding: Decimal) -> Decimal:
    """``net = gross - fees + funding`` quantized to 8 dp."""
    return quantize_money(Decimal(gross_pnl) - Decimal(fees) + Decimal(funding))


__all__ = [
    "EIGHT_DP",
    "decimal_str",
    "fees_total",
    "gross_pnl_usd",
    "money_str",
    "net_pnl_usd",
    "plain_money_str",
    "quantize_money",
]
