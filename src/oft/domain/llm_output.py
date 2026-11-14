"""Pydantic mirror of the provider-facing schema plus semantic checks (ADR-0005).

JSON Schema cannot express confidence ranges, stop side, zone order, or the
decision-type/payload pairing. These checks run after schema validation and
before anything is persisted or turned into an opportunity.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_RATIONALE_CHARS = 4000
_UNSAFE_PATTERNS = (
    re.compile(r"```"),
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"^\s*system\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"<\s*script", re.IGNORECASE),
)
# C0 controls (except tab/newline/carriage return) and C1 controls include
# terminal escape sequences (ESC = \x1b, CSI = \x9b). They are rejected in
# every model-controlled text field before anything is persisted or rendered.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_ROLE_DECISION_TYPES: dict[str, frozenset[str]] = {
    "ANALYSIS": frozenset({"MARKET_ANALYSIS", "OPPORTUNITY_PROPOSAL"}),
    "ENTRY_RECHECK": frozenset({"ENTRY_RECHECK"}),
    "MANAGEMENT": frozenset({"POSITION_MANAGEMENT"}),
    "REVIEW": frozenset({"REVIEW"}),
}

_PAYLOAD_BY_DECISION = {
    "MARKET_ANALYSIS": "analysis",
    "OPPORTUNITY_PROPOSAL": "opportunity",
    "ENTRY_RECHECK": "entry_recheck",
    "POSITION_MANAGEMENT": "position_management",
}

# OPPORTUNITY_PROPOSAL also carries its supporting analysis; everything else is
# closed so a non-null payload cannot ride along with the wrong decision type.
# REVIEW is advisory by construction: it may carry no payload at all.
_ALLOWED_PAYLOADS_BY_DECISION: dict[str, frozenset[str]] = {
    "MARKET_ANALYSIS": frozenset({"analysis"}),
    "OPPORTUNITY_PROPOSAL": frozenset({"analysis", "opportunity"}),
    "ENTRY_RECHECK": frozenset({"entry_recheck"}),
    "POSITION_MANAGEMENT": frozenset({"position_management"}),
    "REVIEW": frozenset(),
}

_PAYLOAD_FIELDS = ("analysis", "opportunity", "entry_recheck", "position_management")


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
    risk_pct: float | None = Field(default=None, gt=0)
    quote_notional_usd: str | None
    leverage: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _bind_size_mode(self) -> EntryOrderIntent:
        """Tie size_mode to exactly the field that governs the requested size."""
        if self.size_mode == "RISK_PCT":
            if self.risk_pct is None:
                raise ValueError("size_mode RISK_PCT requires a positive risk_pct")
            if self.quote_notional_usd is not None:
                raise ValueError("size_mode RISK_PCT must not carry quote_notional_usd")
            return self
        if self.quote_notional_usd is None:
            raise ValueError("size_mode QUOTE_NOTIONAL requires quote_notional_usd")
        if self.risk_pct is not None:
            raise ValueError("size_mode QUOTE_NOTIONAL must not carry risk_pct")
        notional = _decimal(self.quote_notional_usd)
        if notional is None or notional <= 0:
            raise ValueError("quote_notional_usd must be a positive decimal")
        return self


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

    @model_validator(mode="after")
    def _bind_action_fields(self) -> PositionManagement:
        """Tie each action to exactly the fields it consumes.

        ``FULL_CLOSE`` neither forbids nor applies the stop/targets (the
        position is leaving); every other action's fields are strict so a
        mismatched payload is a typed ``PYDANTIC_INVALID`` before it can reach
        execution.
        """
        action = self.action
        if action == "HOLD":
            if self.close_fraction is not None:
                raise ValueError("HOLD must not carry a close_fraction")
            if self.add_risk_pct is not None:
                raise ValueError("HOLD must not carry an add_risk_pct")
            if self.new_stop_price is not None:
                raise ValueError("HOLD must not carry a new_stop_price")
            if self.new_targets:
                raise ValueError("HOLD must not carry new_targets")
            return self
        if action == "PARTIAL_CLOSE":
            if self.close_fraction is None:
                raise ValueError("PARTIAL_CLOSE requires a close_fraction")
            if not math.isfinite(self.close_fraction) or not 0 < self.close_fraction < 1:
                raise ValueError(
                    "PARTIAL_CLOSE close_fraction must be finite and strictly between 0 and 1"
                )
            if self.add_risk_pct is not None:
                raise ValueError("PARTIAL_CLOSE must not carry an add_risk_pct")
            return self
        if action == "FULL_CLOSE":
            if self.close_fraction is not None:
                raise ValueError("FULL_CLOSE must not carry a close_fraction")
            if self.add_risk_pct is not None:
                raise ValueError("FULL_CLOSE must not carry an add_risk_pct")
            return self
        # ADD
        if self.add_risk_pct is None:
            raise ValueError("ADD requires an add_risk_pct")
        if not math.isfinite(self.add_risk_pct) or not 0 < self.add_risk_pct <= 100:
            raise ValueError("ADD add_risk_pct must be finite and greater than 0 and at most 100")
        if self.close_fraction is not None:
            raise ValueError("ADD must not carry a close_fraction")
        if self.new_stop_price is not None:
            raise ValueError("ADD must not carry a new_stop_price")
        return self


class Subject(_Model):
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,24}$")
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "8h", "1d"]
    as_of: str


class DecisionOutput(_Model):
    decision_type: Literal[
        "MARKET_ANALYSIS",
        "OPPORTUNITY_PROPOSAL",
        "ENTRY_RECHECK",
        "POSITION_MANAGEMENT",
        "REVIEW",
    ]
    subject: Subject
    confidence: float
    rationale: str
    caveats: list[str]
    analysis: Analysis | None
    opportunity: OpportunityDraft | None
    entry_recheck: EntryRecheck | None
    position_management: PositionManagement | None


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _number_errors(field: str, value: str | None) -> list[SemanticError]:
    """A present-but-unparseable numeric string is invalid, not absent."""
    if value is None or _decimal(value) is not None:
        return []
    return [SemanticError(field, "NUMBER_INVALID", f"{field} must be a decimal number")]


def _finite_number_errors(field: str, value: float | None) -> list[SemanticError]:
    """A present, non-finite (NaN/Infinity) JSON number is invalid.

    JSON Schema's ``"number"`` type and Python's ``json`` parser both admit the
    bare ``NaN``/``Infinity`` tokens, so a provider-supplied float must be
    finite-checked here before it reaches ``json.dumps(..., allow_nan=False)``.
    """
    if value is None or math.isfinite(value):
        return []
    return [SemanticError(field, "NUMBER_INVALID", f"{field} must be a finite number")]


def _numbers_errors(field: str, values: list[str]) -> list[SemanticError]:
    errors: list[SemanticError] = []
    for index, value in enumerate(values):
        errors.extend(_number_errors(f"{field}[{index}]", value))
    return errors


def _timestamp_errors(field: str, value: str | None) -> list[SemanticError]:
    if value is None:
        return []
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return [SemanticError(field, "TIMESTAMP_INVALID", f"{field} must be an ISO-8601 timestamp")]
    return []


def _unsafe(value: str) -> bool:
    return any(pattern.search(value) for pattern in _UNSAFE_PATTERNS)


def _text_errors(field: str, value: str) -> list[SemanticError]:
    errors: list[SemanticError] = []
    if len(value) > MAX_RATIONALE_CHARS:
        errors.append(
            SemanticError(field, "RATIONALE_TOO_LONG", f"{field} exceeds the bounded length")
        )
    if _CONTROL_CHARS.search(value):
        errors.append(SemanticError(field, "UNSAFE_TEXT", f"{field} contains a control character"))
    if _unsafe(value):
        errors.append(
            SemanticError(
                field,
                "UNSAFE_TEXT",
                f"{field} contains a markdown fence or embedded instruction",
            )
        )
    return errors


def _optional_text_errors(field: str, value: str | None) -> list[SemanticError]:
    return [] if value is None else _text_errors(field, value)


def _texts_errors(field: str, values: list[str]) -> list[SemanticError]:
    errors: list[SemanticError] = []
    for index, value in enumerate(values):
        errors.extend(_text_errors(f"{field}[{index}]", value))
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

    # The pairing is two-way: a payload that does not belong to this decision
    # type must be null, otherwise a MARKET_ANALYSIS could still create an
    # opportunity.
    allowed_payloads = _ALLOWED_PAYLOADS_BY_DECISION.get(output.decision_type, frozenset())
    for name in _PAYLOAD_FIELDS:
        if getattr(output, name) is not None and name not in allowed_payloads:
            errors.append(
                SemanticError(
                    name,
                    "PAYLOAD_MISMATCH",
                    f"{output.decision_type} must not carry the {name} payload",
                )
            )

    errors.extend(_text_errors("rationale", output.rationale))
    if output.decision_type == "REVIEW" and not output.rationale.strip():
        # A review is its rationale: an empty advisory decision is not usable.
        errors.append(
            SemanticError("rationale", "RATIONALE_REQUIRED", "a REVIEW requires a rationale")
        )
    for index, caveat in enumerate(output.caveats):
        errors.extend(_text_errors(f"caveats[{index}]", caveat))
    errors.extend(_timestamp_errors("subject.as_of", output.subject.as_of))

    if output.analysis is not None:
        errors.extend(_text_errors("analysis.market_structure", output.analysis.market_structure))
        errors.extend(_optional_text_errors("analysis.absorption", output.analysis.absorption))
        errors.extend(_optional_text_errors("analysis.exhaustion", output.analysis.exhaustion))
        errors.extend(_text_errors("analysis.narrative", output.analysis.narrative))
        for index, zone in enumerate(output.analysis.key_zones):
            errors.extend(_text_errors(f"analysis.key_zones[{index}].note", zone.note))
            errors.extend(_number_errors(f"analysis.key_zones[{index}].price_low", zone.price_low))
            errors.extend(
                _number_errors(f"analysis.key_zones[{index}].price_high", zone.price_high)
            )
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
        for index, cited in enumerate(output.analysis.metrics_cited):
            prefix = f"analysis.metrics_cited[{index}]"
            errors.extend(_optional_text_errors(f"{prefix}.value", cited.value))
            errors.extend(_text_errors(f"{prefix}.unit", cited.unit))
            errors.extend(_optional_text_errors(f"{prefix}.ref", cited.ref))
            errors.extend(_text_errors(f"{prefix}.note", cited.note))

    opportunity = output.opportunity
    if opportunity is not None:
        errors.extend(_number_errors("opportunity.zone_low", opportunity.zone_low))
        errors.extend(_number_errors("opportunity.zone_high", opportunity.zone_high))
        errors.extend(_timestamp_errors("opportunity.expires_at", opportunity.expires_at))
        errors.extend(
            _number_errors(
                "opportunity.risk_request.entry_price", opportunity.risk_request.entry_price
            )
        )
        errors.extend(
            _number_errors(
                "opportunity.risk_request.stop_price", opportunity.risk_request.stop_price
            )
        )
        errors.extend(
            _numbers_errors(
                "opportunity.risk_request.take_profits",
                opportunity.risk_request.take_profits,
            )
        )
        errors.extend(
            _finite_number_errors(
                "opportunity.risk_request.risk_pct", opportunity.risk_request.risk_pct
            )
        )
        errors.extend(
            _finite_number_errors(
                "opportunity.risk_request.leverage", opportunity.risk_request.leverage
            )
        )
        for index, trigger in enumerate(opportunity.trigger_conditions):
            params = trigger.params
            prefix = f"opportunity.trigger_conditions[{index}].params"
            errors.extend(
                _text_errors(
                    f"opportunity.trigger_conditions[{index}].description", trigger.description
                )
            )
            errors.extend(_optional_text_errors(f"{prefix}.threshold", params.threshold))
            errors.extend(_optional_text_errors(f"{prefix}.unit", params.unit))
            errors.extend(_optional_text_errors(f"{prefix}.comparator", params.comparator))
            errors.extend(_optional_text_errors(f"{prefix}.note", params.note))
            errors.extend(_texts_errors(f"{prefix}.session_states", params.session_states))
            errors.extend(_number_errors(f"{prefix}.price", params.price))
            errors.extend(_number_errors(f"{prefix}.price_low", params.price_low))
            errors.extend(_number_errors(f"{prefix}.price_high", params.price_high))
            errors.extend(_timestamp_errors(f"{prefix}.window.start", trigger.window.start))
            errors.extend(_timestamp_errors(f"{prefix}.window.end", trigger.window.end))
        for index, invalidation in enumerate(opportunity.invalidation_conditions):
            prefix = f"opportunity.invalidation_conditions[{index}]"
            errors.extend(_text_errors(f"{prefix}.description", invalidation.description))
            errors.extend(_optional_text_errors(f"{prefix}.threshold", invalidation.threshold))
            errors.extend(_number_errors(f"{prefix}.price", invalidation.price))
            errors.extend(_timestamp_errors(f"{prefix}.window_end", invalidation.window_end))
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
        errors.extend(_text_errors("entry_recheck.management_plan", entry.management_plan))
        errors.extend(_texts_errors("entry_recheck.reasons", entry.reasons))
        for index, condition in enumerate(entry.recheck_conditions):
            prefix = f"entry_recheck.recheck_conditions[{index}]"
            errors.extend(_optional_text_errors(f"{prefix}.observed", condition.observed))
            errors.extend(_text_errors(f"{prefix}.note", condition.note))
        for index, invalidation in enumerate(entry.invalidation_conditions):
            prefix = f"entry_recheck.invalidation_conditions[{index}]"
            errors.extend(_text_errors(f"{prefix}.description", invalidation.description))
            errors.extend(_optional_text_errors(f"{prefix}.threshold", invalidation.threshold))
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
                intent = entry.order_intent
                # The paper broker fills every accepted entry at the report mark;
                # any other entry type would be silently treated as a MARKET fill.
                if intent.entry_type != "MARKET":
                    errors.append(
                        SemanticError(
                            "entry_recheck.order_intent.entry_type",
                            "ENTRY_TYPE_UNSUPPORTED",
                            "only MARKET entries are supported; the broker fills at the mark",
                        )
                    )
                errors.extend(
                    _number_errors("entry_recheck.order_intent.entry_price", intent.entry_price)
                )
                errors.extend(
                    _number_errors("entry_recheck.order_intent.stop_price", intent.stop_price)
                )
                errors.extend(
                    _numbers_errors("entry_recheck.order_intent.take_profits", intent.take_profits)
                )
                errors.extend(
                    _number_errors(
                        "entry_recheck.order_intent.quote_notional_usd",
                        intent.quote_notional_usd,
                    )
                )
                errors.extend(
                    _finite_number_errors("entry_recheck.order_intent.risk_pct", intent.risk_pct)
                )
                errors.extend(
                    _finite_number_errors("entry_recheck.order_intent.leverage", intent.leverage)
                )
                side = "LONG" if entry.decision == "ENTER_LONG" else "SHORT"
                errors.extend(
                    _stop_side_errors(
                        side,
                        intent.entry_price,
                        intent.stop_price,
                        "entry_recheck.order_intent.stop_price",
                    )
                )

    management = output.position_management
    if management is not None:
        errors.extend(_text_errors("position_management.rationale", management.rationale))
        # A FULL_CLOSE neither forbids nor applies the stop/targets: validate
        # them only for actions that consume the fields, so an ignored field can
        # never reject the reduce-only close.
        if management.action != "FULL_CLOSE":
            errors.extend(
                _number_errors("position_management.new_stop_price", management.new_stop_price)
            )
            errors.extend(
                _numbers_errors("position_management.new_targets", management.new_targets)
            )
        # JSON Schema's "number" admits NaN/Infinity (Python's JSON parser
        # accepts the bare tokens), so the finite check belongs here: a
        # non-finite fraction or percentage must never reach persistence.
        for field, value in (
            ("close_fraction", management.close_fraction),
            ("add_risk_pct", management.add_risk_pct),
        ):
            if value is not None and not math.isfinite(value):
                errors.append(
                    SemanticError(
                        f"position_management.{field}",
                        "NUMBER_INVALID",
                        f"position_management.{field} must be a finite number",
                    )
                )

    return tuple(errors)
