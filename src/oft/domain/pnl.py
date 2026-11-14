"""Deterministic money and PnL arithmetic for position accounting.

Covers the exact terms a full close journals (gross, fees, funding, net), the
gross PnL of a partial-close slice, an add's weighted average entry and frozen
risk, signed funding at a settlement, and the recorded-bar excursion measure.
Pure standard-library functions only: no I/O, clock, or provider type enters
here. Every value is an exact ``Decimal`` quantized to 8 decimal places with
``ROUND_DOWN`` so it matches the paper broker's money quantization. Strings that
cross a contract boundary are produced by :func:`money_str` /
:func:`decimal_str`, never by Python float formatting.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal

EIGHT_DP = Decimal("0.00000001")
ZERO = Decimal("0")
HUNDRED = Decimal("100")

# Plausibility bounds for recorded market data. They exist to keep a corrupt
# but finite value (for example ``1e100``) from reaching ``Decimal.quantize``
# and raising an unhandled ``InvalidOperation``; they are not trading limits.
MAX_ABS_PRICE = Decimal("1000000000")
MAX_ABS_QUANTITY = Decimal("1000000000000000")
MAX_ABS_FUNDING_RATE = Decimal("0.1")
# An upper bound on any persisted money amount. Together with the price and
# quantity bounds it keeps a corrupt recorded value from overflowing
# ``Decimal.quantize`` (a 28-digit context), which would otherwise escape as an
# unhandled ``InvalidOperation``.
MAX_ABS_MONEY = Decimal("1000000000000000")

STOP_FIRST: Literal["STOP"] = "STOP"
TARGET_FIRST: Literal["TARGET"] = "TARGET"
AMBIGUOUS_BAR = "AMBIGUOUS_BAR"


def quantize_money(value: Decimal) -> Decimal:
    """Quantize an amount to 8 decimal places, rounding down like the broker.

    A non-finite or implausibly large value is rejected with ``ValueError``
    instead of reaching ``Decimal.quantize`` and raising an unhandled
    ``InvalidOperation``. Callers turn that into a typed data-quality failure.
    """
    amount = Decimal(value)
    if not amount.is_finite() or abs(amount) > MAX_ABS_MONEY:
        raise ValueError("money value is outside the plausible range")
    return amount.quantize(EIGHT_DP, rounding=ROUND_DOWN)


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


def json_decimal(value: object) -> Decimal | None:
    """Parse one optional JSON-sourced decimal, or ``None`` when unusable.

    This is the single parser for values that arrive from an external document
    (an LLM payload or a stored contract row). ``None`` means "absent"; an
    unparseable or non-finite value is also ``None`` so a corrupt input never
    reaches money arithmetic. Callers must not treat ``None`` as a measured zero.
    """
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


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


def _validate_side(side: str) -> None:
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")


def realized_pnl_slice(
    side: str,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
) -> Decimal:
    """The exact gross PnL of one closed slice, before fees and funding."""
    return gross_pnl_usd(side, entry_price, exit_price, quantity)


def weighted_average_entry(
    old_entry: Decimal,
    old_size: Decimal,
    add_price: Decimal,
    add_quantity: Decimal,
) -> Decimal:
    """The size-weighted entry price after an add, quantized to 8 dp.

    The add is position-increasing, so both sizes must be positive. The result
    is a price rendered as an exact decimal string; it is never a float.
    """
    if old_size <= 0 or add_quantity <= 0:
        raise ValueError("sizes must be positive")
    if add_price <= 0:
        raise ValueError("add price must be positive")
    total = old_size + add_quantity
    raw = (old_entry * old_size + add_price * add_quantity) / total
    return quantize_money(raw)


def add_stop_risk(
    add_price: Decimal,
    stop_price: Decimal,
    add_quantity: Decimal,
) -> Decimal:
    """The incremental stop-based risk an add freezes: ``|price - stop| x qty``."""
    if add_quantity <= 0:
        raise ValueError("add quantity must be positive")
    return quantize_money(abs(add_price - stop_price) * add_quantity)


def funding_amount_usd(
    side: str,
    rate: Decimal,
    mark_price: Decimal,
    size: Decimal,
) -> Decimal:
    """The signed funding cash flow at one settlement.

    A LONG pays a positive rate and a SHORT receives it, so the trader's
    ``funding_usd`` term is ``-(rate x mark x size)`` for LONG and the positive
    product for SHORT. The amount is never fabricated: callers must supply a
    recorded rate and mark. Implausible but finite inputs fail closed instead of
    overflowing ``quantize``.
    """
    _validate_side(side)
    if size < 0:
        raise ValueError("size must be non-negative")
    if mark_price <= 0:
        raise ValueError("mark price must be positive")
    if not rate.is_finite() or abs(rate) > MAX_ABS_FUNDING_RATE:
        raise ValueError("funding rate is outside the plausible range")
    if not mark_price.is_finite() or mark_price > MAX_ABS_PRICE:
        raise ValueError("mark price is outside the plausible range")
    if not size.is_finite() or size > MAX_ABS_QUANTITY:
        raise ValueError("position size is outside the plausible range")
    magnitude = rate * mark_price * size
    signed = -magnitude if side == "LONG" else magnitude
    return quantize_money(signed)


def _bar_decimal(bar: Mapping[str, Any], key: str) -> Decimal | None:
    value = bar.get(key)
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except ArithmeticError:
        return None
    if not parsed.is_finite() or parsed <= 0 or parsed > MAX_ABS_PRICE:
        return None
    return parsed


def _bar_range(bar: Mapping[str, Any]) -> tuple[Decimal, Decimal] | None:
    """A usable ``(low, high)`` pair; inconsistent bars are skipped."""
    low = _bar_decimal(bar, "low")
    high = _bar_decimal(bar, "high")
    if low is None or high is None or low > high:
        return None
    return low, high


@dataclass(frozen=True)
class SizedExcursion:
    """A recorded-bar excursion plus how much of the window it actually measured.

    ``measured_bars`` counts the bars with a usable ``(low, high)`` pair;
    ``skipped_bars`` counts the unusable ones. A window with no usable bar is
    reported as unmeasured (``measured_bars == 0``) so a caller can name the
    data-quality gap instead of claiming a measured zero. ``complete`` is true
    only when the whole window was measurable.
    """

    mae_usd: Decimal
    mfe_usd: Decimal
    measured_bars: int
    skipped_bars: int

    @property
    def complete(self) -> bool:
        return self.measured_bars > 0 and self.skipped_bars == 0


def mae_mfe_from_bars(
    side: str,
    entry_price: Decimal,
    size: Decimal,
    bars: Sequence[Mapping[str, Any]],
) -> tuple[Decimal, Decimal]:
    """Maximum adverse / favorable excursion from recorded bars.

    The documented sign convention holds: MAE is ``<= 0`` and MFE is ``>= 0``.
    For a LONG the adverse extreme is the bar low and the favorable extreme the
    bar high; a SHORT mirrors both. A bar without a usable high/low is skipped
    rather than treated as a zero excursion.
    """
    _validate_side(side)
    if size < 0:
        raise ValueError("size must be non-negative")
    excursion = mae_mfe_from_sized_bars(side, bars, ((entry_price, size) for _ in bars))
    return excursion.mae_usd, excursion.mfe_usd


def mae_mfe_from_sized_bars(
    side: str,
    bars: Sequence[Mapping[str, Any]],
    states: Iterable[tuple[Decimal, Decimal]],
) -> SizedExcursion:
    """MAE/MFE using the entry price and size in force at each bar.

    ``states`` is aligned with ``bars``: ``states[i]`` is the
    ``(average entry price, open size)`` in force for ``bars[i]``. This keeps a
    pre-partial-close excursion measured on the pre-close quantity instead of
    silently rescaling the whole window to the final residual size. The typed
    result reports how many bars were measured and skipped, so an all-unusable
    window is never silently reported as a measured ``(0, 0)``.
    """
    _validate_side(side)
    states = tuple(states)
    if len(states) != len(bars):
        raise ValueError("each bar needs exactly one size/entry state")
    adverse = ZERO
    favorable = ZERO
    measured = 0
    skipped = 0
    for bar, (entry_price, size) in zip(bars, states, strict=True):
        if size < 0:
            raise ValueError("size must be non-negative")
        prices = _bar_range(bar)
        if prices is None:
            skipped += 1
            continue
        measured += 1
        low, high = prices
        if side == "LONG":
            adverse = min(adverse, (low - entry_price) * size)
            favorable = max(favorable, (high - entry_price) * size)
        else:
            adverse = min(adverse, (entry_price - high) * size)
            favorable = max(favorable, (entry_price - low) * size)
    return SizedExcursion(
        mae_usd=quantize_money(min(adverse, ZERO)),
        mfe_usd=quantize_money(max(favorable, ZERO)),
        measured_bars=measured,
        skipped_bars=skipped,
    )


@dataclass(frozen=True)
class FirstTouch:
    """The first protective/target touch found in a bar sequence.

    ``ambiguous`` is true when one bar contains both the stop and the target.
    The caller records ``AMBIGUOUS_BAR`` and assumes the conservative stop-first
    interpretation; the code never claims an intrabar sequence it cannot prove.
    """

    kind: Literal["STOP", "TARGET"] | None
    ambiguous: bool
    at: str | None


def first_touch(
    side: str,
    bars: Sequence[Mapping[str, Any]],
    *,
    stop_price: Decimal | None,
    target_price: Decimal | None,
) -> FirstTouch:
    """Find the first stop/target touch, treating a both-touch bar as stop-first."""
    _validate_side(side)
    if stop_price is None and target_price is None:
        raise ValueError("a first-touch evaluation needs a stop or a target")
    for bar in bars:
        prices = _bar_range(bar)
        if prices is None:
            continue
        low, high = prices
        at = bar.get("bar_end")
        at_value = str(at) if at is not None else None
        if side == "LONG":
            stop_hit = stop_price is not None and low <= stop_price
            target_hit = target_price is not None and high >= target_price
        else:
            stop_hit = stop_price is not None and high >= stop_price
            target_hit = target_price is not None and low <= target_price
        if stop_hit and target_hit:
            return FirstTouch(STOP_FIRST, True, at_value)
        if stop_hit:
            return FirstTouch(STOP_FIRST, False, at_value)
        if target_hit:
            return FirstTouch(TARGET_FIRST, False, at_value)
    return FirstTouch(None, False, None)


def r_multiple(net_pnl: Decimal, initial_risk: Decimal | None) -> float | None:
    """``R = net / frozen initial risk``; a zero, missing, or implausible risk is ``None``.

    A ratio outside the persisted money range is treated as unmeasured rather
    than allowed to escape ``quantize_money`` as an untyped ``ValueError`` after
    a partial commit.
    """
    if initial_risk is None or initial_risk <= 0:
        return None
    try:
        return float(quantize_money(Decimal(net_pnl) / Decimal(initial_risk)))
    except (ArithmeticError, ValueError):
        return None


def return_percent(net_pnl: Decimal, entry_notional: Decimal | None) -> float:
    """``return_pct = net / entry notional x 100``; an unmeasured or implausible basis is ``0.0``.

    The trade-log contract requires ``return_pct`` to be a number, so an
    implausible ratio degrades to a contract-valid ``0.0`` instead of raising an
    untyped error. Callers that can name the gap do so in ``data_quality``.
    """
    if entry_notional is None or entry_notional <= 0:
        return 0.0
    try:
        return float(quantize_money(Decimal(net_pnl) / Decimal(entry_notional) * HUNDRED))
    except (ArithmeticError, ValueError):
        return 0.0


def outcome_tags(
    net_pnl: Decimal,
    *,
    stopped: bool = False,
    target: bool = False,
    data_partial: bool = False,
    manual: bool = False,
    partial: bool = False,
) -> tuple[str, ...]:
    """The applicable outcome tags, in a deterministic order.

    ``WIN``/``LOSS``/``BE`` always describe the net result (a tie is neither a
    win nor a loss). ``DATA_PARTIAL`` comes first because it qualifies the
    measurement the other tags rest on.
    """
    tags: list[str] = []
    if data_partial:
        tags.append("DATA_PARTIAL")
    if net_pnl > 0:
        tags.append("WIN")
    elif net_pnl < 0:
        tags.append("LOSS")
    else:
        tags.append("BE")
    if stopped:
        tags.append("STOPPED")
    if target:
        tags.append("TARGET")
    if partial:
        tags.append("PARTIAL")
    if manual:
        tags.append("MANUAL_CLOSE")
    return tuple(tags)


__all__ = [
    "AMBIGUOUS_BAR",
    "EIGHT_DP",
    "MAX_ABS_FUNDING_RATE",
    "MAX_ABS_MONEY",
    "MAX_ABS_PRICE",
    "MAX_ABS_QUANTITY",
    "STOP_FIRST",
    "TARGET_FIRST",
    "FirstTouch",
    "SizedExcursion",
    "add_stop_risk",
    "decimal_str",
    "fees_total",
    "first_touch",
    "funding_amount_usd",
    "gross_pnl_usd",
    "json_decimal",
    "mae_mfe_from_bars",
    "mae_mfe_from_sized_bars",
    "money_str",
    "net_pnl_usd",
    "outcome_tags",
    "plain_money_str",
    "quantize_money",
    "r_multiple",
    "realized_pnl_slice",
    "return_percent",
    "weighted_average_entry",
]
