"""Deterministic risk gate (ADR-0006).

The gate recomputes every cap it has an input for from code-owned inputs. The
LLM's proposal is only one of the terms in ``effective_size = min(proposal,
per-trade risk, max notional, max leverage)``; the verdict records which cap
bound the size. Exchange tick/step rounding and the minimum-notional filter are
post-hoc adjustments and blocks applied to the chosen size, not terms in the
minimum; the contract's ``EXCHANGE_BRACKET`` cap is unused because no exchange
bracket data is ingested. The daily-loss cap is not evaluated in this slice:
there is no realized-PnL source yet, so a missing input is recorded in
``stale_inputs`` rather than silently treated as zero (slice-18 follow-up).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any

RISK_VERDICT_TTL_SECONDS = 30


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
    per_trade_risk_pct: float = 0.8
    max_concurrent_positions: int = 3
    daily_loss_limit_usd: Decimal = Decimal("300.00")
    max_leverage: float = 5.0
    max_notional_usd: Decimal = Decimal("2000.00")
    max_slippage_bps: float = 25.0
    max_spread_bps: float = 15.0
    stale_mark_seconds: int = 60


DEFAULT_CAPS = RiskCaps()


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


def _inputs_hash(request: RiskRequest, *, equity: Decimal, open_positions: int) -> str:
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
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def verdict_inputs_hash(request: RiskRequest, *, equity: Decimal, open_positions: int) -> str:
    """Recompute the input digest so callers can bind an intent to a verdict."""
    return _inputs_hash(request, equity=equity, open_positions=open_positions)


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
) -> RiskVerdict:
    """Recompute the caps and return ALLOW/REDUCE/BLOCK with the binding cap.

    ``daily_loss_usd`` is ``None`` when the caller has no realized-PnL source;
    the cap is then not evaluated and the verdict names it in ``stale_inputs``.
    """
    digest = _inputs_hash(request, equity=equity, open_positions=open_positions)
    # No PnL engine exists in this slice, so callers pass None. Never treat a
    # missing daily-loss input as an evaluated zero (slice-18 follow-up).
    unevaluated: tuple[str, ...] = () if daily_loss_usd is not None else ("daily_loss_usd",)
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
            stale_inputs=unevaluated,
        )
    if request.side == "LONG" and stop_price >= entry_price:
        return _block(
            request,
            binding_cap=BindingCap.MISSING_STOP,
            reason="a LONG stop must be below the entry",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=unevaluated,
        )
    if request.side == "SHORT" and stop_price <= entry_price:
        return _block(
            request,
            binding_cap=BindingCap.MISSING_STOP,
            reason="a SHORT stop must be above the entry",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=unevaluated,
        )

    if mark_price <= 0:
        return _block(
            request,
            binding_cap=BindingCap.STALE_MARK,
            reason="mark price is missing",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=_merge_stale(unevaluated, "mark_price"),
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
            stale_inputs=_merge_stale(unevaluated, "mark_price"),
        )
    if open_positions >= caps.max_concurrent_positions:
        return _block(
            request,
            binding_cap=BindingCap.MAX_POSITIONS,
            reason="maximum concurrent positions reached",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=unevaluated,
        )
    if daily_loss_usd is not None and daily_loss_usd >= caps.daily_loss_limit_usd:
        return _block(
            request,
            binding_cap=BindingCap.DAILY_LOSS,
            reason="daily loss limit reached",
            now=now,
            inputs_hash=digest,
            allowed_leverage=allowed_leverage,
            stale_inputs=unevaluated,
        )

    distance = abs(entry_price - _round_tick(stop_price, tick_size))

    risk_budget = equity * Decimal(str(caps.per_trade_risk_pct)) / Decimal("100")
    requested_quantity = request.quantity
    if requested_quantity is None:
        if request.notional_usd is not None and request.notional_usd > 0:
            requested_quantity = request.notional_usd / entry_price
        else:
            requested_quantity = risk_budget / distance

    per_trade_quantity = risk_budget / distance if risk_budget > 0 else Decimal("0")
    notional_quantity = caps.max_notional_usd / entry_price
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
            stale_inputs=unevaluated,
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
    if unevaluated:
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
        stale_inputs=unevaluated,
        entry_price=entry_price,
        stop_price=_round_tick(stop_price, tick_size),
    )


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
