"""Deterministic risk gate (ADR-0006).

The gate recomputes every cap it has an input for from code-owned inputs.
Percentage caps (per-trade risk, max notional, daily loss) are derived from
current equity; leverage, spread, slippage, and the stale-mark budget are fixed
bounds and are not equity-derived. The LLM's proposal is only one of the terms
in ``effective_size = min(proposal, per-trade risk, max notional, max
leverage)``; the verdict records which cap bound the size. Tick rounding happens
before sizing (the rounded entry drives the stop distance and the quantity
terms), while step rounding and the minimum-notional filter are applied to the
chosen size. The contract's ``EXCHANGE_BRACKET`` cap is unused because no
exchange bracket data is ingested. A missing daily-loss, spread, or slippage
measurement, and a degraded (cash-only) equity basis, are recorded in
``stale_inputs`` and never treated as an evaluated value or a silent zero.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any

RISK_VERDICT_TTL_SECONDS = 30
RISK_CAPS_STATE_KEY = "risk_caps"
MAX_LEVERAGE_CEILING = 125.0
MAX_BPS_CEILING = 10000.0
_INT_CAP_FIELDS = frozenset({"max_concurrent_positions", "stale_mark_seconds"})

# The complete cap set, in the order the contract and the persistence document
# expose it. Adding a cap here is the one edit point (D1).
CAP_FIELDS: tuple[str, ...] = (
    "per_trade_risk_pct",
    "max_concurrent_positions",
    "daily_loss_pct",
    "max_leverage",
    "max_notional_pct",
    "max_slippage_bps",
    "max_spread_bps",
    "stale_mark_seconds",
)


class Verdict(StrEnum):
    """The closed verdict vocabulary from ``common.schema.json``."""

    ALLOW = "ALLOW"
    REDUCE = "REDUCE"
    BLOCK = "BLOCK"


class BindingCap(StrEnum):
    """The closed binding-cap vocabulary from ``common.schema.json``."""

    NONE = "NONE"
    PER_TRADE_RISK = "PER_TRADE_RISK"
    MAX_POSITIONS = "MAX_POSITIONS"
    DAILY_LOSS = "DAILY_LOSS"
    MAX_LEVERAGE = "MAX_LEVERAGE"
    MAX_NOTIONAL = "MAX_NOTIONAL"
    EXCHANGE_BRACKET = "EXCHANGE_BRACKET"
    MIN_NOTIONAL = "MIN_NOTIONAL"
    STALE_MARK = "STALE_MARK"
    STALE_DATA = "STALE_DATA"
    MISSING_STOP = "MISSING_STOP"
    PRICE_MOVED = "PRICE_MOVED"
    SPREAD = "SPREAD"
    SLIPPAGE = "SLIPPAGE"


@dataclass(frozen=True)
class RiskCaps:
    """Equity-derived caps. Percentages are of current account equity (D1)."""

    per_trade_risk_pct: float = 0.8
    max_concurrent_positions: int = 3
    daily_loss_pct: float = 3.0
    max_leverage: float = 5.0
    max_notional_pct: float = 20.0
    max_slippage_bps: float = 25.0
    max_spread_bps: float = 15.0
    stale_mark_seconds: int = 60

    def to_state(self) -> dict[str, Any]:
        """The durable ``system_state.risk_caps`` document."""
        return {field: getattr(self, field) for field in CAP_FIELDS}

    @classmethod
    def from_state(cls, payload: Mapping[str, Any]) -> RiskCaps:
        """Build caps from a persisted partial document.

        Unknown keys are rejected: a drifted document (for example the legacy
        absolute-dollar ``max_notional_usd``) must fail closed rather than be
        silently ignored. Non-numeric, boolean, non-finite (NaN/Infinity), and
        non-integral integer caps are rejected here as well as by
        :func:`validate_caps`, so a persisted document can never smuggle a
        value that disables a comparison-based cap.
        """
        if not isinstance(payload, Mapping):
            raise ValueError("risk caps must be a JSON object")
        unknown = sorted(set(payload) - set(CAP_FIELDS))
        if unknown:
            raise ValueError("persisted risk caps contain unknown field(s): " + ", ".join(unknown))
        values: dict[str, Any] = {}
        for field in CAP_FIELDS:
            if field not in payload:
                continue
            raw = payload[field]
            if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                raise ValueError(f"risk cap {field} has an invalid type")
            try:
                if field in _INT_CAP_FIELDS:
                    numeric = float(raw)
                    if not math.isfinite(numeric) or not numeric.is_integer():
                        raise ValueError(f"risk cap {field} must be a finite integer")
                    values[field] = int(numeric)
                else:
                    numeric = float(raw)
                    if not math.isfinite(numeric):
                        raise ValueError(f"risk cap {field} must be a finite number")
                    values[field] = numeric
            except (TypeError, ValueError, OverflowError) as exc:
                if isinstance(exc, ValueError) and str(exc).startswith("risk cap "):
                    raise
                raise ValueError(f"risk cap {field} has an invalid type") from exc
        return cls(**values)

    @classmethod
    def from_state_json(cls, raw: str) -> RiskCaps:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("persisted risk caps are not valid JSON") from exc
        caps = cls.from_state(payload)
        errors = validate_caps(caps)
        if errors:
            raise ValueError("persisted risk caps are invalid: " + "; ".join(errors))
        return caps


DEFAULT_CAPS = RiskCaps()


def _finite_number(name: str, value: object) -> bool:
    """A cap must be a real, finite number; bools and NaN/Infinity fail closed."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def validate_caps(caps: RiskCaps) -> tuple[str, ...]:
    """Return invariant violations; an empty tuple means the caps are usable."""
    errors: list[str] = []

    def _pct(name: str, value: object) -> None:
        if not _finite_number(name, value):
            errors.append(f"{name} must be a finite number")
            return
        assert isinstance(value, (int, float))
        if value <= 0 or value > 100:
            errors.append(f"{name} must be greater than 0 and at most 100")

    def _bps(name: str, value: object) -> None:
        if not _finite_number(name, value):
            errors.append(f"{name} must be a finite number")
            return
        assert isinstance(value, (int, float))
        if value <= 0 or value > MAX_BPS_CEILING:
            errors.append(f"{name} must be greater than 0 and at most {MAX_BPS_CEILING:g}")

    _pct("per_trade_risk_pct", caps.per_trade_risk_pct)
    _pct("daily_loss_pct", caps.daily_loss_pct)
    _pct("max_notional_pct", caps.max_notional_pct)
    if isinstance(caps.max_concurrent_positions, bool) or not isinstance(
        caps.max_concurrent_positions, int
    ):
        errors.append("max_concurrent_positions must be a finite integer")
    elif caps.max_concurrent_positions < 1:
        errors.append("max_concurrent_positions must be at least 1")
    if not _finite_number("max_leverage", caps.max_leverage):
        errors.append("max_leverage must be a finite number")
    elif caps.max_leverage <= 0 or caps.max_leverage > MAX_LEVERAGE_CEILING:
        errors.append(f"max_leverage must be greater than 0 and at most {MAX_LEVERAGE_CEILING:g}")
    _bps("max_slippage_bps", caps.max_slippage_bps)
    _bps("max_spread_bps", caps.max_spread_bps)
    if isinstance(caps.stale_mark_seconds, bool) or not isinstance(caps.stale_mark_seconds, int):
        errors.append("stale_mark_seconds must be a finite integer")
    elif caps.stale_mark_seconds < 1:
        errors.append("stale_mark_seconds must be at least 1")
    return tuple(errors)


def caps_loosened(before: RiskCaps, after: RiskCaps) -> tuple[str, ...]:
    """Name every cap that ``after`` makes more permissive (D4)."""
    checks: tuple[tuple[str, bool], ...] = (
        ("per_trade_risk_pct", after.per_trade_risk_pct > before.per_trade_risk_pct),
        (
            "max_concurrent_positions",
            after.max_concurrent_positions > before.max_concurrent_positions,
        ),
        ("daily_loss_pct", after.daily_loss_pct > before.daily_loss_pct),
        ("max_leverage", after.max_leverage > before.max_leverage),
        ("max_notional_pct", after.max_notional_pct > before.max_notional_pct),
        ("max_slippage_bps", after.max_slippage_bps > before.max_slippage_bps),
        ("max_spread_bps", after.max_spread_bps > before.max_spread_bps),
        ("stale_mark_seconds", after.stale_mark_seconds > before.stale_mark_seconds),
    )
    return tuple(name for name, loosened in checks if loosened)


def merge_caps(base: RiskCaps, override: RiskCaps) -> RiskCaps:
    """Apply a persisted document over the configured defaults."""
    return replace(base, **override.to_state())


def apply_persisted_caps(base: RiskCaps, payload: Mapping[str, Any]) -> RiskCaps:
    """Override only the fields a persisted document actually names (D1/D4).

    Missing fields keep the ``OFT_RISK_*`` value from ``base`` instead of
    falling back to the hardcoded dataclass defaults, so a partial operator row
    can never silently loosen an env-tightened cap.
    """
    partial = RiskCaps.from_state(payload)
    values = {field: getattr(partial, field) for field in CAP_FIELDS if field in payload}
    return replace(base, **values)


@dataclass(frozen=True)
class RiskRequest:
    symbol: str
    side: str
    entry_price: Decimal
    stop_price: Decimal | None
    quantity: Decimal | None
    risk_pct: float
    leverage: float
    notional_usd: Decimal | None

    def __post_init__(self) -> None:
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        if self.entry_price <= 0:
            raise ValueError("entry_price must be positive")
        if self.quantity is not None and self.quantity < 0:
            raise ValueError("quantity must be non-negative")
        if self.leverage <= 0:
            raise ValueError("leverage must be positive")


@dataclass(frozen=True)
class RiskVerdict:
    verdict: Verdict
    requested_quantity: Decimal | None
    allowed_quantity: Decimal | None
    allowed_notional_usd: Decimal | None
    allowed_leverage: float
    risk_amount_usd: Decimal
    binding_cap: BindingCap
    reasons: tuple[str, ...]
    computed_at: str
    expires_at: str
    inputs_hash: str
    stale_inputs: tuple[str, ...]
    entry_price: Decimal
    stop_price: Decimal | None

    def __post_init__(self) -> None:
        if self.verdict is Verdict.BLOCK:
            if self.allowed_quantity is not None:
                raise ValueError("a BLOCK verdict must not carry an allowed quantity")
            return
        if self.allowed_quantity is None or self.allowed_quantity <= 0:
            raise ValueError(f"a {self.verdict.value} verdict requires a positive allowed quantity")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _inputs_hash(
    request: RiskRequest,
    *,
    equity: Decimal,
    open_positions: int,
    spread_bps: float | None = None,
    expected_slippage_bps: float | None = None,
    daily_loss_usd: Decimal | None = None,
) -> str:
    payload = {
        "symbol": request.symbol,
        "side": request.side,
        "entry_price": str(request.entry_price),
        "stop_price": None if request.stop_price is None else str(request.stop_price),
        "quantity": None if request.quantity is None else str(request.quantity),
        "risk_pct": request.risk_pct,
        "leverage": request.leverage,
        "notional_usd": None if request.notional_usd is None else str(request.notional_usd),
        "equity": str(equity),
        "open_positions": open_positions,
        # A missing (unmeasured) input is encoded as JSON null and is distinct
        # from a measured zero; the gateway can therefore detect that a verdict
        # was computed under different spread/slippage/daily-loss inputs (D1).
        "spread_bps": None if spread_bps is None else float(spread_bps),
        "expected_slippage_bps": (
            None if expected_slippage_bps is None else float(expected_slippage_bps)
        ),
        "daily_loss_usd": None if daily_loss_usd is None else str(daily_loss_usd),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def verdict_inputs_hash(
    request: RiskRequest,
    *,
    equity: Decimal,
    open_positions: int,
    spread_bps: float | None = None,
    expected_slippage_bps: float | None = None,
    daily_loss_usd: Decimal | None = None,
) -> str:
    """Recompute the input digest so callers can bind an intent to a verdict."""
    return _inputs_hash(
        request,
        equity=equity,
        open_positions=open_positions,
        spread_bps=spread_bps,
        expected_slippage_bps=expected_slippage_bps,
        daily_loss_usd=daily_loss_usd,
    )


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _round_tick(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return (value / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def _block(
    request: RiskRequest,
    *,
    binding_cap: BindingCap,
    reason: str,
    now: str,
    inputs_hash: str,
    allowed_leverage: float,
    stale_inputs: tuple[str, ...] = (),
) -> RiskVerdict:
    return RiskVerdict(
        verdict=Verdict.BLOCK,
        requested_quantity=request.quantity,
        allowed_quantity=None,
        allowed_notional_usd=None,
        allowed_leverage=allowed_leverage,
        risk_amount_usd=Decimal("0"),
        binding_cap=binding_cap,
        reasons=(reason,),
        computed_at=now,
        expires_at=_expires(now),
        inputs_hash=inputs_hash,
        stale_inputs=stale_inputs,
        entry_price=request.entry_price,
        stop_price=request.stop_price,
    )


def _expires(now: str) -> str:
    from datetime import timedelta

    return (_parse(now) + timedelta(seconds=RISK_VERDICT_TTL_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def evaluate_risk(
    *,
    request: RiskRequest,
    caps: RiskCaps,
    equity: Decimal,
    open_positions: int,
    daily_loss_usd: Decimal | None,
    mark_price: Decimal,
    mark_as_of: str,
    now: str,
    tick_size: Decimal,
    step_size: Decimal,
    min_notional: Decimal,
    spread_bps: float | None = None,
    expected_slippage_bps: float | None = None,
    equity_stale_inputs: tuple[str, ...] = (),
) -> RiskVerdict:
    """Recompute the caps and return ALLOW/REDUCE/BLOCK with the binding cap.

    ``daily_loss_usd``, ``spread_bps``, and ``expected_slippage_bps`` are
    ``None`` when the caller has no measurement. A missing input is named in
    ``stale_inputs`` and never treated as an evaluated zero (D1).
    ``equity_stale_inputs`` names a degraded equity basis (for example a missing
    position mark that forced ``equity = cash``) and is merged into the same
    stale list.
    """
    digest = _inputs_hash(
        request,
        equity=equity,
        open_positions=open_positions,
        spread_bps=spread_bps,
        expected_slippage_bps=expected_slippage_bps,
        daily_loss_usd=daily_loss_usd,
    )
    stale = _stale_inputs(
        daily_loss_usd=daily_loss_usd,
        spread_bps=spread_bps,
        expected_slippage_bps=expected_slippage_bps,
        equity_stale_inputs=equity_stale_inputs,
    )
    daily_loss_limit = equity * Decimal(str(caps.daily_loss_pct)) / Decimal("100")
    max_notional = equity * Decimal(str(caps.max_notional_pct)) / Decimal("100")
    allowed_leverage = min(request.leverage, caps.max_leverage)
    entry_price = _round_tick(request.entry_price, tick_size)
    stop_price = request.stop_price

    if stop_price is None or stop_price <= 0:
        return _block(
            request,
            binding_cap=BindingCap.MISSING_STOP,
            reason="a protective stop is required",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=stale,
        )
    if request.side == "LONG" and stop_price >= entry_price:
        return _block(
            request,
            binding_cap=BindingCap.MISSING_STOP,
            reason="a LONG stop must be below the entry",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=stale,
        )
    if request.side == "SHORT" and stop_price <= entry_price:
        return _block(
            request,
            binding_cap=BindingCap.MISSING_STOP,
            reason="a SHORT stop must be above the entry",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=stale,
        )

    if mark_price <= 0:
        return _block(
            request,
            binding_cap=BindingCap.STALE_MARK,
            reason="mark price is missing",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=_merge_stale(stale, "mark_price"),
        )
    mark_age = (_parse(now) - _parse(mark_as_of)).total_seconds()
    if mark_age > caps.stale_mark_seconds:
        return _block(
            request,
            binding_cap=BindingCap.STALE_MARK,
            reason=f"mark price is stale by {int(mark_age)}s",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=_merge_stale(stale, "mark_price"),
        )
    if open_positions >= caps.max_concurrent_positions:
        return _block(
            request,
            binding_cap=BindingCap.MAX_POSITIONS,
            reason="maximum concurrent positions reached",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=stale,
        )
    if daily_loss_usd is not None and daily_loss_usd >= daily_loss_limit:
        return _block(
            request,
            binding_cap=BindingCap.DAILY_LOSS,
            reason="daily loss limit reached",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=stale,
        )
    if spread_bps is not None and spread_bps < 0:
        return _block(
            request,
            binding_cap=BindingCap.SPREAD,
            reason="the measured spread is negative",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=stale,
        )
    if spread_bps is not None and spread_bps > caps.max_spread_bps:
        return _block(
            request,
            binding_cap=BindingCap.SPREAD,
            reason=f"spread {spread_bps:g} bps exceeds the {caps.max_spread_bps:g} bps cap",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=stale,
        )
    if expected_slippage_bps is not None and expected_slippage_bps < 0:
        return _block(
            request,
            binding_cap=BindingCap.SLIPPAGE,
            reason="the expected slippage is negative",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=stale,
        )
    if expected_slippage_bps is not None and expected_slippage_bps > caps.max_slippage_bps:
        return _block(
            request,
            binding_cap=BindingCap.SLIPPAGE,
            reason=(
                f"expected slippage {expected_slippage_bps:g} bps exceeds "
                f"the {caps.max_slippage_bps:g} bps cap"
            ),
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=stale,
        )

    distance = abs(entry_price - _round_tick(stop_price, tick_size))

    # The proposal is the LLM's own risk percentage; the per-trade cap is the
    # configured ceiling. They are distinct strictest-of terms (D1): a
    # conservative 0.4% proposal must size at 0.4%, not be upsized to 0.8%.
    risk_budget = equity * Decimal(str(caps.per_trade_risk_pct)) / Decimal("100")
    proposal_budget = equity * Decimal(str(request.risk_pct)) / Decimal("100")
    requested_quantity = request.quantity
    if requested_quantity is None:
        if request.notional_usd is not None and request.notional_usd > 0:
            requested_quantity = request.notional_usd / entry_price
        else:
            requested_quantity = proposal_budget / distance

    per_trade_quantity = risk_budget / distance if risk_budget > 0 else Decimal("0")
    notional_quantity = max_notional / entry_price
    leverage_quantity = (equity * Decimal(str(allowed_leverage))) / entry_price

    candidates: list[tuple[str, Decimal]] = [
        ("PROPOSAL", requested_quantity),
        ("PER_TRADE_RISK", per_trade_quantity),
        ("MAX_NOTIONAL", notional_quantity),
        ("MAX_LEVERAGE", leverage_quantity),
    ]
    binding_name, allowed_quantity = min(candidates, key=lambda item: item[1])
    if binding_name == "PROPOSAL":
        binding_name = "NONE"

    allowed_quantity = _round_down(allowed_quantity, step_size)
    notional = allowed_quantity * entry_price
    if allowed_quantity <= 0 or notional < min_notional:
        return _block(
            request,
            binding_cap=BindingCap.MIN_NOTIONAL,
            reason="effective size is below the exchange minimum notional",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=stale,
        )

    reasons: list[str] = []
    if _round_tick(request.entry_price, tick_size) != request.entry_price:
        reasons.append("entry price was rounded to the tick size")
    if _round_tick(stop_price, tick_size) != stop_price:
        reasons.append("stop price was rounded to the tick size")

    if binding_name == "NONE" and allowed_leverage < request.leverage:
        binding_name = "MAX_LEVERAGE"
        reasons.append("leverage clamped to the configured cap")
    if binding_name == "NONE":
        verdict = Verdict.ALLOW
    else:
        verdict = Verdict.REDUCE
        reasons.append(f"{binding_name.lower()} reduced the requested size")
    if daily_loss_usd is None:
        reasons.append("daily loss cap was not evaluated: no realized-PnL source in this slice")

    return RiskVerdict(
        verdict=verdict,
        requested_quantity=requested_quantity,
        allowed_quantity=allowed_quantity,
        allowed_notional_usd=notional,
        allowed_leverage=allowed_leverage,
        risk_amount_usd=allowed_quantity * distance,
        binding_cap=BindingCap(binding_name),
        reasons=tuple(reasons),
        computed_at=now,
        expires_at=_expires(now),
        inputs_hash=digest,
        stale_inputs=stale,
        entry_price=entry_price,
        stop_price=_round_tick(stop_price, tick_size),
    )


def _stale_inputs(
    *,
    daily_loss_usd: Decimal | None,
    spread_bps: float | None,
    expected_slippage_bps: float | None,
    equity_stale_inputs: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Name every input the caller could not measure; never a silent zero."""
    names: list[str] = []
    if daily_loss_usd is None:
        names.append("daily_loss_usd")
    if spread_bps is None:
        names.append("spread_bps")
    if expected_slippage_bps is None:
        names.append("expected_slippage_bps")
    for name in equity_stale_inputs:
        if name not in names:
            names.append(name)
    return tuple(names)


def _merge_stale(base: tuple[str, ...], name: str) -> tuple[str, ...]:
    """Add a stale-input name without duplicating it."""
    return base if name in base else (*base, name)


def verdict_to_contract(
    verdict: RiskVerdict,
    *,
    requested_payload: dict[str, Any],
) -> dict[str, Any]:
    """Render the persisted verdict as ``common.schema.json#/$defs/riskVerdict``."""
    return {
        "verdict": verdict.verdict.value,
        "requested": requested_payload,
        "allowed": {
            "quantity": (
                None if verdict.allowed_quantity is None else str(verdict.allowed_quantity)
            ),
            "notional_usd": (
                None if verdict.allowed_notional_usd is None else str(verdict.allowed_notional_usd)
            ),
            "leverage": verdict.allowed_leverage,
            "risk_amount_usd": str(verdict.risk_amount_usd),
        },
        "binding_cap": verdict.binding_cap.value,
        "reasons": list(verdict.reasons),
        "computed_at": verdict.computed_at,
        "expires_at": verdict.expires_at,
        "inputs_hash": verdict.inputs_hash,
        "stale_inputs": list(verdict.stale_inputs),
    }
