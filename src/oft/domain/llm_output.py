"""Pydantic mirror of the provider-facing schema plus semantic checks (ADR-0005).

JSON Schema cannot express confidence ranges, stop side, zone order, or the
decision-type/payload pairing. These checks run after schema validation and
before anything is persisted or turned into an opportunity.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_RATIONALE_CHARS = 4000
_UNSAFE_PATTERNS = (
    re.compile(r"```"),
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"^\s*system\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"<\s*script", re.IGNORECASE),
)

_ROLE_DECISION_TYPES: dict[str, frozenset[str]] = {
    "ANALYSIS": frozenset({"MARKET_ANALYSIS", "OPPORTUNITY_PROPOSAL"}),
    "ENTRY_RECHECK": frozenset({"ENTRY_RECHECK"}),
    "MANAGEMENT": frozenset({"POSITION_MANAGEMENT"}),
    "REVIEW": frozenset({"MARKET_ANALYSIS", "POSITION_MANAGEMENT"}),
}

_PAYLOAD_BY_DECISION = {
    "MARKET_ANALYSIS": "analysis",
    "OPPORTUNITY_PROPOSAL": "opportunity",
    "ENTRY_RECHECK": "entry_recheck",
    "POSITION_MANAGEMENT": "position_management",
}


@dataclass(frozen=True)
class SemanticError:
    field: str
    code: str
    message: str


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TriggerParams(_Model):
    price: str | None
    price_low: str | None
    price_high: str | None
    threshold: str | None
    unit: str | None
    comparator: str | None
    lookback_bars: int | None
    session_states: list[str]
    note: str | None


class Window(_Model):
    start: str
    end: str


class TriggerConditionDraft(_Model):
    draft_id: str
    type: str
    params: TriggerParams
    window: Window
    required_data: list[str]
    description: str


class InvalidationConditionDraft(_Model):
    type: str
    description: str
    price: str | None
    threshold: str | None
    window_end: str | None


class KeyZone(_Model):
    role: str
    price_low: str
    price_high: str
    note: str


class CitedMetric(_Model):
    metric: str
    value: str | None
    unit: str
    as_of: str | None
    ref: str | None
    note: str


class Analysis(_Model):
    market_structure: str
    regime: str
    absorption: str | None
    exhaustion: str | None
    key_zones: list[KeyZone]
    narrative: str
    metrics_cited: list[CitedMetric]


class RiskRequest(_Model):
    entry_price: str
    stop_price: str
    take_profits: list[str]
    risk_pct: float
    leverage: float
    max_notional_usd: str | None


class OpportunityDraft(_Model):
    side: Literal["LONG", "SHORT"]
    zone_low: str
    zone_high: str
    entry_preference: Literal["LIMIT", "MARKET", "STOP"]
    trigger_conditions: list[TriggerConditionDraft]
    invalidation_conditions: list[InvalidationConditionDraft]
    expires_at: str
    risk_request: RiskRequest
    thesis: str
    data_needs: list[str]


class EntryOrderIntent(_Model):
    entry_type: Literal["MARKET", "LIMIT"]
    entry_price: str | None
    stop_price: str | None
    take_profits: list[str]
    size_mode: Literal["RISK_PCT", "QUOTE_NOTIONAL"]
    risk_pct: float | None
    quote_notional_usd: str | None
    leverage: float | None


class RecheckCondition(_Model):
    condition_id: str
    met: bool
    observed: str | None
    note: str


class EntryRecheck(_Model):
    decision: Literal["ENTER_LONG", "ENTER_SHORT", "NO_ENTRY"]
    order_intent: EntryOrderIntent | None
    recheck_conditions: list[RecheckCondition]
    reasons: list[str]
    invalidation_conditions: list[InvalidationConditionDraft]
    management_plan: str


class PositionManagement(_Model):
    action: Literal["HOLD", "PARTIAL_CLOSE", "FULL_CLOSE", "ADD"]
    close_fraction: float | None
    add_risk_pct: float | None
    new_stop_price: str | None
    new_targets: list[str]
    rationale: str
    urgency: Literal["LOW", "MEDIUM", "HIGH"]


class Subject(_Model):
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,24}$")
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "8h", "1d"]
    as_of: str


class DecisionOutput(_Model):
    decision_type: Literal[
        "MARKET_ANALYSIS", "OPPORTUNITY_PROPOSAL", "ENTRY_RECHECK", "POSITION_MANAGEMENT"
    ]
    subject: Subject
    confidence: float
    rationale: str
    caveats: list[str]
    analysis: Analysis | None
    opportunity: OpportunityDraft | None
    entry_recheck: EntryRecheck | None
    position_management: PositionManagement | None


def _decimal(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unsafe(value: str) -> bool:
    return any(pattern.search(value) for pattern in _UNSAFE_PATTERNS)


def _text_errors(field: str, value: str) -> list[SemanticError]:
    errors: list[SemanticError] = []
    if len(value) > MAX_RATIONALE_CHARS:
        errors.append(
            SemanticError(field, "RATIONALE_TOO_LONG", f"{field} exceeds the bounded length")
        )
    if _unsafe(value):
        errors.append(
            SemanticError(
                field,
                "UNSAFE_TEXT",
                f"{field} contains a markdown fence or embedded instruction",
            )
        )
    return errors


def _stop_side_errors(
    side: str, entry: str | None, stop: str | None, field: str
) -> list[SemanticError]:
    entry_value = _decimal(entry)
    stop_value = _decimal(stop)
    if stop_value is None:
        return []
    if entry_value is None:
        return [SemanticError(field, "STOP_SIDE", "stop present without an entry price")]
    if side == "LONG" and stop_value >= entry_value:
        return [SemanticError(field, "STOP_SIDE", "a LONG stop must be below the entry")]
    if side == "SHORT" and stop_value <= entry_value:
        return [SemanticError(field, "STOP_SIDE", "a SHORT stop must be above the entry")]
    return []


def semantic_errors(output: DecisionOutput, *, role: str) -> tuple[SemanticError, ...]:
    """Return every semantic violation; an empty tuple means the output is usable."""
    errors: list[SemanticError] = []
    allowed = _ROLE_DECISION_TYPES.get(role, frozenset())
    if output.decision_type not in allowed:
        errors.append(
            SemanticError(
                "decision_type",
                "DECISION_TYPE_ROLE_MISMATCH",
                f"{output.decision_type} is not valid for role {role}",
            )
        )

    if not math.isfinite(output.confidence) or not 0.0 <= output.confidence <= 1.0:
        errors.append(
            SemanticError("confidence", "CONFIDENCE_RANGE", "confidence must be within [0, 1]")
        )

    payload_name = _PAYLOAD_BY_DECISION.get(output.decision_type)
    if payload_name is not None and getattr(output, payload_name) is None:
        errors.append(
            SemanticError(
                payload_name,
                "PAYLOAD_MISSING",
                f"{output.decision_type} requires the {payload_name} payload",
            )
        )

    errors.extend(_text_errors("rationale", output.rationale))
    for index, caveat in enumerate(output.caveats):
        errors.extend(_text_errors(f"caveats[{index}]", caveat))

    if output.analysis is not None:
        errors.extend(_text_errors("analysis.narrative", output.analysis.narrative))
        for index, zone in enumerate(output.analysis.key_zones):
            low = _decimal(zone.price_low)
            high = _decimal(zone.price_high)
            if low is not None and high is not None and low > high:
                errors.append(
                    SemanticError(
                        f"analysis.key_zones[{index}]",
                        "ZONE_ORDER",
                        "key zone low must not exceed high",
                    )
                )

    opportunity = output.opportunity
    if opportunity is not None:
        low = _decimal(opportunity.zone_low)
        high = _decimal(opportunity.zone_high)
        if low is not None and high is not None and low >= high:
            errors.append(
                SemanticError(
                    "opportunity.zone",
                    "ZONE_ORDER",
                    "zone_low must be strictly below zone_high",
                )
            )
        errors.extend(_text_errors("opportunity.thesis", opportunity.thesis))
        errors.extend(
            _stop_side_errors(
                opportunity.side,
                opportunity.risk_request.entry_price,
                opportunity.risk_request.stop_price,
                "opportunity.risk_request.stop_price",
            )
        )

    entry = output.entry_recheck
    if entry is not None:
        if entry.decision == "NO_ENTRY" and entry.order_intent is not None:
            errors.append(
                SemanticError(
                    "entry_recheck.order_intent",
                    "PAYLOAD_MISMATCH",
                    "NO_ENTRY must not carry an order intent",
                )
            )
        if entry.decision != "NO_ENTRY":
            if entry.order_intent is None:
                errors.append(
                    SemanticError(
                        "entry_recheck.order_intent",
                        "PAYLOAD_MISSING",
                        "an ENTER decision requires an order intent",
                    )
                )
            else:
                side = "LONG" if entry.decision == "ENTER_LONG" else "SHORT"
                errors.extend(
                    _stop_side_errors(
                        side,
                        entry.order_intent.entry_price,
                        entry.order_intent.stop_price,
                        "entry_recheck.order_intent.stop_price",
                    )
                )

    management = output.position_management
    if management is not None:
        errors.extend(_text_errors("position_management.rationale", management.rationale))

    return tuple(errors)
