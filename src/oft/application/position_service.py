"""Position management: read models, mark lookup, stop touch, and the journal.

The service owns the deterministic management paths: it resolves the position's
latest recorded mark from published bars (falling back to the mark persisted on
the position), refuses to act on a missing or stale mark, applies the
code-owned stop-touch rule, accrues recorded funding settlements, and computes
the complete trade-log facts. Partial closes and adds are submitted through the
same execution gateway; the full close persists a management decision, an exit
risk verdict, one reduce-only ``FULL_CLOSE``, and the single trade-log row the
broker committed. No route or CLI calls the broker directly.

Mark lookup prefers the latest recorded bar close for the position's symbol and
uses the bar's ``bar_end`` as the mark's as-of. When no bar series or usable row
exists, the position payload's recorded ``mark_price``/``updated_at`` is the
fallback. The newest recorded observation wins; the stale rule is applied
unchanged to the chosen as-of, and a bar whose ``bar_end`` is in the future is
never selected. A bar-derived mark is written to the position row immediately
before a close so the gateway's independent re-read sees the same recorded mark.

The read model keeps the PnL bases explicit. An OPEN position's
``realized_pnl_usd`` is ACCUMULATED GROSS; a CLOSED position's is NET. The
history payload names the basis and never substitutes the residual position
notional for a missing entry notional.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from oft.application import recorded_bars
from oft.application.execution_gateway import AuthContext, ExecutionGateway, GatewayError
from oft.application.kill_switch import KillSwitch, record_audit
from oft.application.paper_account import PaperAccountService
from oft.application.paper_broker import (
    BrokerError,
    BrokerResult,
    OrderIntent,
    PaperBroker,
    recorded_entry_notional_usd,
)
from oft.application.payloads import decode_payload_json
from oft.application.telegram_events import (
    NotificationSink,
    build_position_management_event,
)
from oft.config import Settings
from oft.domain.oms import OrderType, Position, deterministic_id
from oft.domain.pnl import (
    MAX_ABS_FUNDING_RATE,
    MAX_ABS_MONEY,
    MAX_ABS_PRICE,
    MAX_ABS_QUANTITY,
    ZERO,
    decimal_str,
    first_touch,
    funding_amount_usd,
    mae_mfe_from_sized_bars,
    plain_money_str,
    quantize_money,
    weighted_average_entry,
)
from oft.domain.positions import (
    ADD,
    FULL_CLOSE,
    MANUAL_CLOSE_RULE,
    PARTIAL_CLOSE,
    STOP_TOUCH_RULE,
    CloseOutcome,
    PositionError,
    TradeLogContext,
    evaluate_protective_exit,
    increasing_side,
    management_decision_id,
    payload_has_partial_close,
    position_from_contract,
    recorded_partial_close,
    reducing_side,
    validate_stop_side,
)
from oft.domain.risk import (
    RISK_VERDICT_TTL_SECONDS,
    BindingCap,
    ManagementAction,
    RiskVerdict,
    StopChange,
    Verdict,
    classify_management_intent,
    verdict_to_contract,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import (
    AuditEventDraft,
    DecisionRecord,
    FundingAccrualCommit,
    FundingAccrualRecord,
    ManagementUpdateRecord,
    OmsConflictError,
    OrderRecord,
    PositionEventRecord,
    PositionRecord,
    RiskVerdictRecord,
    StorePort,
    StoreUnavailableError,
    TradeLogRecord,
)

_LOGGER = logging.getLogger(__name__)

DATA_STALE = "DATA_STALE"
STOP_NOT_TOUCHED = "STOP_NOT_TOUCHED"
STOP_TOUCH = "STOP_TOUCH"
# The code-only protective-exit reason is single-sourced as
# ``oft.application.jobs.PROTECTIVE_STOP``; the journal's close reason stays the
# domain's ``STOP_TOUCH`` vocabulary (``CloseOutcome`` validates it).
CLOSED = "CLOSED"
NO_ACTION = "NO_ACTION"

_MARK_SOURCE_BARS = "BARS"
_MARK_SOURCE_POSITION = "POSITION"
_PROVIDER_VALUES = frozenset({"openai", "anthropic", "deepseek", "stub"})
_FUNDING_RETRY_LIMIT = 3
_MAX_FUNDING_INTERVAL_HOURS = Decimal("168")
_MAX_EXPECTED_SETTLEMENTS = 500
# The funding-history read is bounded to the newest reports since the position
# opened; the holding window cannot be shorter than the scan cadence, and 500
# reports is far more than any plausible position lifetime.
_MAX_FUNDING_REPORTS = 500


def _metric_version_from_report(manifest_json: str) -> str | None:
    """The first recorded calc metric version in a report's metrics manifest."""
    try:
        entries = json.loads(manifest_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict):
            value = entry.get("metric_version")
            if isinstance(value, str) and value:
                return value
    return None


def _apply_decision_provenance(
    payload_json: str,
    provenance: dict[str, Any],
    reasons: list[str],
) -> None:
    """Copy provider/model/prompt_version from the recorded entry decision."""
    decision_payload = decode_payload_json(payload_json, subject="entry decision")
    recorded = (decision_payload or {}).get("provenance") or {}
    if not isinstance(recorded, dict):
        reasons.append("entry provenance is partial: the decision provenance is corrupt")
        recorded = {}
    provider = recorded.get("provider")
    model = recorded.get("model")
    prompt_version = recorded.get("prompt_version")
    if not isinstance(provider, str) or not provider:
        reasons.append("entry provenance is partial: the decision has no provider")
        provider = "stub"
    elif provider not in _PROVIDER_VALUES:
        reasons.append(
            f"entry provenance is partial: the decision provider {provider!r} is not recognized"
        )
        provider = "stub"
    if not isinstance(model, str) or not model:
        reasons.append("entry provenance is partial: the decision has no model")
        model = "unknown"
    if not isinstance(prompt_version, int) or prompt_version < 1:
        reasons.append("entry provenance is partial: the decision has no prompt version")
        prompt_version = 1
    provenance.update(
        llm_provider=provider,
        llm_model=model,
        prompt_version=prompt_version,
    )


class PositionNotFound(Exception):
    """The requested position does not exist."""

    code = "POSITION_NOT_FOUND"

    def __init__(self, position_id: str) -> None:
        super().__init__(f"Position {position_id} does not exist.")
        self.position_id = position_id
        self.message = f"Position {position_id} does not exist."


class PositionCloseError(Exception):
    """A typed management failure; ``code`` maps to the API error catalog."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PositionMonitorResult:
    """The outcome of one manual ``oft pos monitor`` tick."""

    position_id: str
    status: str
    reason_code: str
    reason: str | None = None
    action: str | None = None
    order: dict[str, Any] | None = None
    fill: dict[str, Any] | None = None
    position: dict[str, Any] | None = None
    trade_log: dict[str, Any] | None = None
    account: dict[str, Any] | None = None
    idempotent_replay: bool = False
    funding: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status not in {NO_ACTION, CLOSED}:
            raise ValueError(f"unknown monitor status {self.status!r}")
        if self.status == NO_ACTION:
            if self.action is not None or self.trade_log is not None:
                raise ValueError("a NO_ACTION result carries no action or trade log")
        elif self.action != "FULL_CLOSE" or self.trade_log is None:
            raise ValueError("a CLOSED result requires action FULL_CLOSE and a trade log")

    def to_contract(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "action": self.action,
            "order": self.order,
            "fill": self.fill,
            "position": self.position,
            "trade_log": self.trade_log,
            "account": self.account,
            "idempotent_replay": self.idempotent_replay,
            "funding": self.funding,
        }


@dataclass(frozen=True)
class _MarkObservation:
    """One recorded mark and the timestamp it was observed at."""

    mark: Decimal
    as_of: str
    source: str


@dataclass(frozen=True)
class ManagementMark:
    """A fresh, recorded mark for one OPEN position and the row it came from.

    The management path resolves the mark and applies the same stale rule as
    the protective-exit tick, then hands this value to the exit/add helpers so
    the gateway's independent re-read sees the same recorded mark.
    """

    position: Position
    payload: dict[str, Any]
    payload_json: str
    mark: Decimal
    mark_as_of: str
    now: str


@dataclass(frozen=True)
class _FundingPoint:
    """One recorded funding settlement; ``rate`` is ``None`` when unmeasured."""

    ts: str
    rate: Decimal | None


@dataclass(frozen=True)
class _FundingHistory:
    """Every recorded settlement for a symbol, plus any source-quality reason."""

    points: tuple[_FundingPoint, ...]
    interval_hours: Decimal | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _TimelineStep:
    """The size and average entry in force after one adjustment event."""

    at: str
    as_of: datetime | None
    size: Decimal
    avg_entry: Decimal


@dataclass(frozen=True)
class _ManagementTimeline:
    """The position's size/average-entry history derived from recorded events.

    ``initial_*`` is the state at ``opened_at``; each step is the state after a
    partial close or add. When a step cannot be reconstructed the timeline is
    incomplete and callers must name the gap instead of guessing.
    """

    initial_size: Decimal
    initial_avg_entry: Decimal
    steps: tuple[_TimelineStep, ...]
    partial_quantity: Decimal
    reasons: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.reasons

    def state_at(self, ts: object) -> tuple[Decimal, Decimal] | None:
        """The ``(avg entry, size)`` in force at ``ts``, or ``None`` if unparseable."""
        target = _parse_timestamp(ts)
        if target is None:
            return None
        size = self.initial_size
        avg_entry = self.initial_avg_entry
        for step in self.steps:
            if step.as_of is None or step.as_of > target:
                break
            size = step.size
            avg_entry = step.avg_entry
        return avg_entry, size


@dataclass(frozen=True)
class _SourceFacts:
    signal: dict[str, Any]
    provenance: dict[str, Any]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _HistoryPnl:
    """A typed history PnL read model with an explicit basis.

    The same persisted field means different things by state: an OPEN position's
    ``realized_pnl_usd`` is ACCUMULATED GROSS, a CLOSED one's is NET. This model
    names the basis and exposes an explicit gross term so a reader never has to
    infer it. The two concrete subclasses are the OPEN and CLOSED projections.
    """

    basis: str
    gross_pnl_usd: str | None
    realized_gross_pnl_usd: str | None
    fees_usd: str | None
    funding_usd: str | None
    net_pnl_usd: str | None
    r_multiple: float | None
    return_pct: float | None
    mae_usd: str | None
    mfe_usd: str | None
    outcome_tags: tuple[str, ...]
    data_quality: dict[str, Any]

    def to_contract(self) -> dict[str, Any]:
        return {
            "basis": self.basis,
            "gross_pnl_usd": self.gross_pnl_usd,
            "realized_gross_pnl_usd": self.realized_gross_pnl_usd,
            "fees_usd": self.fees_usd,
            "funding_usd": self.funding_usd,
            "net_pnl_usd": self.net_pnl_usd,
            "r_multiple": self.r_multiple,
            "return_pct": self.return_pct,
            "mae_usd": self.mae_usd,
            "mfe_usd": self.mfe_usd,
            "outcome_tags": list(self.outcome_tags),
            "data_quality": self.data_quality,
        }


def _money_text(value: object) -> str | None:
    """Render a persisted money value, or ``None`` when it is unreadable.

    An already-rendered contract string is preserved verbatim so the history
    projection cannot disagree with the trade-log document it mirrors.
    """
    parsed = _decimal_or_none(value)
    if parsed is None or abs(parsed) > MAX_ABS_MONEY:
        return None
    if isinstance(value, str):
        return value
    return plain_money_str(parsed)


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _degraded_quality(quality: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    """Force a stored data-quality summary to PARTIAL and merge named reasons."""
    merged = dict(quality)
    existing = [str(reason) for reason in merged.get("reasons") or []]
    for reason in reasons:
        if reason not in existing:
            existing.append(reason)
    merged["status"] = "PARTIAL"
    merged["reasons"] = existing
    return merged


class _OpenPnl(_HistoryPnl):
    """The OPEN projection: the persisted value is ACCUMULATED GROSS."""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> _OpenPnl:
        reasons: list[str] = []
        raw = payload.get("realized_pnl_usd")
        if raw is None:
            if payload_has_partial_close(payload):
                # The stored total was lost; the recorded partial closes make it
                # unmeasured rather than a measured zero.
                gross = None
                reasons.append(
                    "accumulated gross is unmeasured: the recorded partial closes "
                    "have no stored total"
                )
            else:
                gross = plain_money_str(ZERO)
        else:
            parsed = _decimal_or_none(raw)
            if parsed is None or abs(parsed) > MAX_ABS_MONEY:
                gross = None
                reasons.append("realized PnL is unreadable; the accumulated gross is not measured")
            else:
                gross = plain_money_str(parsed)
        fees = _money_text(payload.get("fees_usd"))
        if payload.get("fees_usd") is not None and fees is None:
            reasons.append("fees_usd is unreadable; the accumulated fees are not measured")
        funding = _money_text(payload.get("funding_usd"))
        if payload.get("funding_usd") is not None and funding is None:
            reasons.append("funding_usd is unreadable; accrued funding is not measured")
        return cls(
            basis="ACCUMULATED_GROSS",
            gross_pnl_usd=gross,
            realized_gross_pnl_usd=gross,
            fees_usd=fees,
            funding_usd=funding,
            net_pnl_usd=None,
            r_multiple=None,
            return_pct=None,
            mae_usd=None,
            mfe_usd=None,
            outcome_tags=(),
            data_quality={"status": "PARTIAL" if reasons else "COMPLETE", "reasons": reasons},
        )


class _ClosedPnl(_HistoryPnl):
    """The CLOSED projection: the persisted value is NET."""

    @classmethod
    def from_trade(cls, payload: dict[str, Any]) -> _ClosedPnl:
        reasons: list[str] = []

        def money(key: str) -> str | None:
            value = _money_text(payload.get(key))
            if value is None:
                reasons.append(f"trade-log {key} is unreadable")
            return value

        gross = money("gross_pnl_usd")
        fees = money("fees_usd")
        funding = money("funding_usd")
        net = money("net_pnl_usd")
        mae = money("mae_usd")
        mfe = money("mfe_usd")
        r_raw = payload.get("r_multiple")
        computed_r = _number_or_none(r_raw)
        if r_raw is not None and computed_r is None:
            reasons.append("trade-log r_multiple is unreadable")
        return_raw = payload.get("return_pct")
        computed_return = _number_or_none(return_raw)
        if computed_return is None:
            reasons.append("trade-log return_pct is unreadable")
        data_quality = payload.get("data_quality")
        if isinstance(data_quality, dict):
            quality = dict(data_quality)
            stored_status = quality.get("status")
            stored_reasons = quality.get("reasons")
            if stored_status not in {"COMPLETE", "PARTIAL"} or not isinstance(stored_reasons, list):
                reasons.append("stored trade-log data quality is invalid")
        else:
            quality = {"status": "PARTIAL", "reasons": []}
            reasons.append("stored trade-log data quality is invalid")
        if reasons:
            existing = [str(reason) for reason in quality.get("reasons") or []]
            for reason in reasons:
                if reason not in existing:
                    existing.append(reason)
            quality["status"] = "PARTIAL"
            quality["reasons"] = existing
        tags = payload.get("outcome_tags")
        return cls(
            basis="NET",
            gross_pnl_usd=gross,
            realized_gross_pnl_usd=gross,
            fees_usd=fees,
            funding_usd=funding,
            net_pnl_usd=net,
            r_multiple=computed_r,
            return_pct=computed_return,
            mae_usd=mae,
            mfe_usd=mfe,
            outcome_tags=tuple(str(tag) for tag in tags) if isinstance(tags, list) else (),
            data_quality=quality,
        )

    @classmethod
    def from_position_payload(cls, payload: dict[str, Any]) -> _ClosedPnl:
        reasons = ["the trade log is missing; the net PnL is not measured"]
        net = _money_text(payload.get("realized_pnl_usd"))
        if net is None:
            reasons.append("realized PnL is unreadable")
        fees = _money_text(payload.get("fees_usd"))
        if payload.get("fees_usd") is not None and fees is None:
            reasons.append("fees_usd is unreadable")
        funding = _money_text(payload.get("funding_usd"))
        if payload.get("funding_usd") is not None and funding is None:
            reasons.append("funding_usd is unreadable")
        return cls(
            basis="NET",
            gross_pnl_usd=None,
            realized_gross_pnl_usd=None,
            fees_usd=fees,
            funding_usd=funding,
            net_pnl_usd=net,
            r_multiple=None,
            return_pct=None,
            mae_usd=None,
            mfe_usd=None,
            outcome_tags=(),
            data_quality={"status": "PARTIAL", "reasons": reasons},
        )


@dataclass(frozen=True)
class _CloseIds:
    """The deterministic identifiers for one close attempt."""

    decision_id: str
    risk_verdict_id: str
    approval_id: str
    idempotency_key: str


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_timestamp(value: object) -> datetime | None:
    """Parse a recorded timestamp to an aware UTC datetime, or ``None``."""
    if isinstance(value, datetime):
        parsed = value
    elif value is None:
        return None
    else:
        try:
            parsed = _parse(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except ArithmeticError:
        return None
    return parsed if parsed.is_finite() else None


def _decimal_text(value: object) -> str | None:
    """Render a persisted price/quantity/rate, or ``None`` when unreadable."""
    parsed = _decimal_or_none(value)
    return None if parsed is None else decimal_str(parsed)


def _inside_window(ts: str, opened_at: str, now: str) -> bool:
    """Whether a settlement falls in the half-open holding window ``(open, now]``."""
    ts_dt = _parse_timestamp(ts)
    opened_dt = _parse_timestamp(opened_at)
    now_dt = _parse_timestamp(now)
    if ts_dt is None or opened_dt is None or now_dt is None:
        return False
    return opened_dt < ts_dt <= now_dt


def _mark_at(bars: tuple[dict[str, object], ...], ts: str) -> Decimal | None:
    """The latest recorded bar close at or before a settlement timestamp."""
    target = _parse_timestamp(ts)
    if target is None:
        return None
    selected: tuple[datetime, Decimal] | None = None
    for row in bars:
        bar_end = _parse_timestamp(row.get("bar_end"))
        if bar_end is None or bar_end > target:
            continue
        close = _decimal_or_none(row.get("close"))
        if close is None or close <= 0 or close > MAX_ABS_PRICE:
            continue
        if selected is None or bar_end > selected[0]:
            selected = (bar_end, close)
    return None if selected is None else selected[1]


def _observation_rank(observation: _MarkObservation) -> tuple[bool, datetime, int]:
    """Newest first; a parseable as-of beats an unparseable one, bar on a tie."""
    parsed = _parse_timestamp(observation.as_of)
    return (
        parsed is not None,
        parsed or datetime.min.replace(tzinfo=UTC),
        1 if observation.source == _MARK_SOURCE_BARS else 0,
    )


class PositionService:
    def __init__(
        self,
        *,
        store: StorePort,
        clock: ClockPort,
        settings: Settings,
        notifications: NotificationSink | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._settings = settings
        self._notifications = notifications
        self._broker = PaperBroker(settings=settings, store=store, clock=clock)
        self._paper = PaperAccountService(settings=settings, store=store, clock=clock)
        self._kill_switch = KillSwitch(settings=settings, store=store, clock=clock)
        self._gateway = ExecutionGateway(
            store=store,
            broker=self._broker,
            kill_switch=self._kill_switch,
            clock=clock,
        )
        self._stale_seconds = int(settings.risk_stale_mark_seconds)
        # Per-tick bars snapshot so the mark lookup, funding accrual, and journal
        # facts share one read per logical series. The service is constructed per
        # request/tick, so this transient state never spans operations.
        self._tick_series: set[tuple[str, str]] | None = None
        self._tick_cache: recorded_bars.SeriesCache | None = None

    # -- read models -----------------------------------------------------

    def _decode(self, payload_json: str, *, subject: str) -> dict[str, Any]:
        payload = decode_payload_json(payload_json, subject=subject)
        if payload is None:
            raise PositionCloseError("DATA_UNAVAILABLE", f"{subject} payload is unreadable")
        return payload

    def list(
        self, *, state: str | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[dict[str, Any], ...]:
        items: list[dict[str, Any]] = []
        # Resolve the published series once per symbol and keep one series cache
        # per symbol for the whole page, so a multi-row list neither re-queries
        # the manifest per row nor discards another symbol's cached parquet
        # reads. ``SeriesCache`` is keyed by ``(period, metric_version)`` only,
        # so it must never be shared across symbols. The service is per request;
        # the previous state is restored in ``finally``.
        series_by_symbol: dict[str, set[tuple[str, str]]] = {}
        caches_by_symbol: dict[str, recorded_bars.SeriesCache] = {}
        previous_series = self._tick_series
        previous_cache = self._tick_cache
        try:
            for record in self._store.list_positions(state=state, limit=limit, offset=offset):
                payload = decode_payload_json(
                    record.payload_json, subject=f"position {record.position_id}"
                )
                if payload is None:
                    continue
                symbol = str(payload.get("symbol") or "")
                if symbol:
                    if symbol not in series_by_symbol:
                        series_by_symbol[symbol], _failed = recorded_bars.bar_series(
                            self._store, symbol
                        )
                        caches_by_symbol[symbol] = {}
                    self._tick_series = series_by_symbol[symbol]
                    self._tick_cache = caches_by_symbol[symbol]
                else:
                    # A payload without a symbol cannot be constructed below;
                    # leave the per-row fallback untouched for the null path.
                    self._tick_series = None
                    self._tick_cache = None
                items.append(self.with_resolved_mark(payload))
        finally:
            self._tick_series = previous_series
            self._tick_cache = previous_cache
        return tuple(items)

    def count(self, *, state: str | None = None) -> int:
        return self._store.count_positions(state=state)

    def show(self, position_id: str) -> dict[str, Any]:
        record = self._store.get_position(position_id)
        if record is None:
            raise PositionNotFound(position_id)
        payload = self._decode(record.payload_json, subject=f"position {position_id}")
        return self.with_resolved_mark(payload)

    def with_resolved_mark(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return a new payload carrying the server-resolved recorded mark.

        The UI cannot derive mark freshness from the stored ``mark_price`` and
        ``updated_at`` alone: the backend writes those only at entry/exit, while
        the mark resolver prefers a newer recorded bar close. This decorates the
        payload additively with the resolver's ``(price, as_of, source)`` using
        the same rule as the monitor/management paths and the injected clock.
        The stored payload is never mutated, and decoration never raises: an
        unreadable position or a resolution failure yields null fields.
        """
        decorated = dict(payload)
        decorated["resolved_mark_price"] = None
        decorated["resolved_mark_as_of"] = None
        decorated["resolved_mark_source"] = None
        try:
            position = position_from_contract(payload)
            now_dt = self._clock.now()
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=UTC)
            observation = self._resolve_mark(position, payload, now_dt.astimezone(UTC))
        except (PositionError, KeyError, TypeError, ValueError, ArithmeticError):
            return decorated
        if observation is None:
            return decorated
        decorated["resolved_mark_price"] = decimal_str(observation.mark)
        decorated["resolved_mark_as_of"] = observation.as_of
        decorated["resolved_mark_source"] = observation.source
        return decorated

    def history(self, position_id: str) -> dict[str, Any]:
        """The position's accounting timeline: events, funding, and PnL terms.

        The PnL basis is explicit: an OPEN position reports ACCUMULATED GROSS in
        ``gross_pnl_usd``/``realized_gross_pnl_usd``; a CLOSED one reports NET.
        Recorded funding settlements that were not accrued are listed with the
        reason, so "none recorded" and "recorded but rejected" are distinct.
        """
        record = self._store.get_position(position_id)
        if record is None:
            raise PositionNotFound(position_id)
        payload = self._decode(record.payload_json, subject=f"position {position_id}")
        events = self._store.list_position_events(position_id)
        accruals = self._store.list_funding_accruals(position_id)
        trade = self._store.get_trade_log_for_position(position_id)
        trade_payload: dict[str, Any] | None = None
        if trade is not None:
            trade_payload = self._decode(trade.payload_json, subject=f"trade log {trade.trade_id}")
        pnl: _HistoryPnl
        if trade_payload is not None:
            pnl = _ClosedPnl.from_trade(trade_payload)
        elif payload.get("state") == "OPEN":
            try:
                pnl = _OpenPnl.from_payload(payload)
            except PositionError as exc:
                raise PositionCloseError(exc.code, exc.message) from exc
        else:
            pnl = _ClosedPnl.from_position_payload(payload)
        funding_items, funding_reasons, funding_degraded = self._funding_history_items(
            position_id, payload, accruals
        )
        if funding_degraded:
            pnl = replace(pnl, data_quality=_degraded_quality(pnl.data_quality, funding_reasons))
        return {
            "position_id": position_id,
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
            "state": payload.get("state"),
            "size": payload.get("size"),
            "entry_price": payload.get("entry_price"),
            "avg_entry_price": payload.get("avg_entry_price"),
            "events": [
                {
                    "seq": event.seq,
                    "at": event.at,
                    "action": event.action,
                    "order_id": event.order_id,
                    "note": event.note,
                }
                for event in events
            ],
            "funding": funding_items,
            "funding_reasons": funding_reasons,
            "pnl": pnl.to_contract(),
        }

    def _funding_history_items(
        self,
        position_id: str,
        payload: dict[str, Any],
        accruals: tuple[FundingAccrualRecord, ...],
    ) -> tuple[builtins.list[dict[str, Any]], builtins.list[str], bool]:
        """Every in-window recorded settlement with its accrual status.

        Accrued settlements carry the persisted amount; a recorded settlement
        that was not accrued carries a named reason. A stored accrual whose
        fields cannot be parsed is named and marks the history degraded (so the
        envelope cannot stay COMPLETE while a value is missing). A window with
        no recorded settlement yields an empty list and no reasons, distinguishing
        it from a settlement that exists but was rejected.
        """
        try:
            position = position_from_contract(payload)
        except PositionError as exc:
            return [], [f"position accounting is unavailable: {exc.message}"], True
        now = str(payload.get("closed_at") or self._now())
        history = self._funding_history(position)
        bars = self._window_bars(position, position.opened_at, now)
        timeline = self._management_timeline(position)
        accrued = {accrual.settlement_time for accrual in accruals}
        reasons: list[str] = list(history.reasons)
        degraded = False
        items: list[dict[str, Any]] = []
        for accrual in accruals:
            rate_text = _decimal_text(accrual.rate)
            mark_text = _decimal_text(accrual.mark_price)
            amount_text = _money_text(accrual.amount_usd)
            size_text = _decimal_text(accrual.size)
            unreadable = [
                name
                for name, value in (
                    ("rate", rate_text),
                    ("mark_price", mark_text),
                    ("amount_usd", amount_text),
                    ("size", size_text),
                )
                if value is None
            ]
            item_reason: str | None = None
            if unreadable:
                degraded = True
                item_reason = (
                    f"funding accrual {accrual.accrual_id} has unreadable stored "
                    f"field(s): {', '.join(unreadable)}"
                )
                reasons.append(item_reason)
            items.append(
                {
                    "accrual_id": accrual.accrual_id,
                    "settlement_time": accrual.settlement_time,
                    "rate": rate_text,
                    "mark_price": mark_text,
                    "amount_usd": amount_text,
                    "side": accrual.side,
                    "size": size_text,
                    "created_at": accrual.created_at,
                    "accrued": True,
                    "reason": item_reason,
                }
            )
        recorded: set[str] = set()
        for point in history.points:
            if not _inside_window(point.ts, position.opened_at, now):
                continue
            recorded.add(point.ts)
            if point.ts in accrued:
                continue
            mark = _mark_at(bars, point.ts)
            if point.rate is None or mark is None:
                reason = (
                    f"funding settlement {point.ts} is missing a recorded rate or mark; not accrued"
                )
            else:
                state = timeline.state_at(point.ts)
                if state is None or not timeline.complete or state[1] <= 0:
                    reason = (
                        f"funding settlement {point.ts} has no recorded size in force; not accrued"
                    )
                else:
                    reason = f"funding settlement {point.ts} was recorded but not accrued"
            reasons.append(reason)
            items.append(
                {
                    "accrual_id": None,
                    "settlement_time": point.ts,
                    "rate": None if point.rate is None else decimal_str(point.rate),
                    "mark_price": None if mark is None else decimal_str(mark),
                    "amount_usd": None,
                    "side": position.side.value,
                    "size": None,
                    "created_at": None,
                    "accrued": False,
                    "reason": reason,
                }
            )
        reasons.extend(
            self._expected_funding_reasons(history, position.opened_at, now, recorded, accrued)
        )
        return items, reasons, degraded

    def _now(self) -> str:
        now_dt = self._clock.now()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        return utc_timestamp(now_dt.astimezone(UTC))

    # -- funding ---------------------------------------------------------

    def accrue_funding(self, position_id: str, *, now: str | None = None) -> dict[str, Any]:
        """Accrue recorded funding settlements inside the holding window.

        Settlement timestamps come from every recorded report for the position's
        symbol, so a report recorded after entry can contribute a settlement
        that the frozen entry report never saw. A settlement whose recorded rate
        or mark is missing or implausible is named in ``reasons`` and never
        accrued as a fabricated amount. The ``(position_id, settlement_time)``
        uniqueness makes every replay or restart a no-op.
        """
        return self._accrue_with_retry(position_id, now or self._now())

    def funding_settlement_in_window(self, position_id: str, start: str, end: str) -> str | None:
        """The first recorded funding settlement inside ``[start, end)``.

        Read-only: it never accrues, executes, or mutates state. A missing or
        non-OPEN position (and an unreadable window) yields ``None`` so the
        management due logic reports nothing rather than raising.
        """
        record = self._store.get_position(position_id)
        if record is None:
            return None
        payload = decode_payload_json(record.payload_json, subject=f"position {position_id}")
        if payload is None or payload.get("state") != "OPEN":
            return None
        try:
            position = position_from_contract(payload)
        except PositionError:
            return None
        start_dt = _parse_timestamp(start)
        end_dt = _parse_timestamp(end)
        if start_dt is None or end_dt is None or start_dt >= end_dt:
            return None
        try:
            history = self._funding_history(position)
        except (StoreUnavailableError, PositionError):
            return None
        for point in history.points:
            ts = _parse_timestamp(point.ts)
            if ts is not None and start_dt <= ts < end_dt:
                return point.ts
        return None

    def _accrue_with_retry(
        self,
        position_id: str,
        now_value: str,
        *,
        bars: tuple[dict[str, object], ...] | None = None,
        history: _FundingHistory | None = None,
    ) -> dict[str, Any]:
        """Retry a bounded number of times when a concurrent write wins first."""
        attempts = 0
        while True:
            try:
                return self._accrue_funding_once(position_id, now_value, bars=bars, history=history)
            except OmsConflictError as exc:
                attempts += 1
                if attempts > _FUNDING_RETRY_LIMIT:
                    raise PositionCloseError(
                        "CONFLICT",
                        "the funding accrual could not commit after "
                        f"{_FUNDING_RETRY_LIMIT + 1} attempts: {exc}",
                    ) from exc
                # The next attempt re-reads the payload (and recomputes the
                # funding total) before building its commit.

    def _accrue_funding_once(
        self,
        position_id: str,
        now_value: str,
        *,
        bars: tuple[dict[str, object], ...] | None = None,
        history: _FundingHistory | None = None,
    ) -> dict[str, Any]:
        record = self._store.get_position(position_id)
        if record is None:
            raise PositionNotFound(position_id)
        payload = self._decode(record.payload_json, subject=f"position {position_id}")
        result: dict[str, Any] = {"position_id": position_id, "accrued": 0, "reasons": ()}
        if payload.get("state") != "OPEN":
            return result
        try:
            position = position_from_contract(payload)
        except PositionError as exc:
            raise PositionCloseError(exc.code, exc.message) from exc
        history = history if history is not None else self._funding_history(position)
        reasons: list[str] = list(history.reasons)
        if not history.points:
            return {"position_id": position_id, "accrued": 0, "reasons": tuple(reasons)}
        bars = (
            bars if bars is not None else self._window_bars(position, position.opened_at, now_value)
        )
        timeline = self._management_timeline(position)
        reasons.extend(timeline.reasons)
        existing = {
            accrual.settlement_time for accrual in self._store.list_funding_accruals(position_id)
        }
        current_payload = payload
        current_payload_json = record.payload_json
        funding_total = position.funding_usd
        accrued = 0
        for point in history.points:
            if not _inside_window(point.ts, position.opened_at, now_value):
                continue
            if point.ts in existing:
                continue
            mark = _mark_at(bars, point.ts)
            if point.rate is None or mark is None:
                reasons.append(
                    f"funding settlement {point.ts} is missing a recorded rate or mark; not accrued"
                )
                continue
            state = timeline.state_at(point.ts)
            if state is None or not timeline.complete:
                reasons.append(
                    f"funding settlement {point.ts} has no recorded size in force; not accrued"
                )
                continue
            _avg_entry, size = state
            if size <= 0:
                reasons.append(
                    f"funding settlement {point.ts} has a non-positive size in force; not accrued"
                )
                continue
            try:
                amount = funding_amount_usd(position.side.value, point.rate, mark, size)
            except (ArithmeticError, ValueError):
                reasons.append(
                    f"funding settlement {point.ts} has implausible recorded data; not accrued"
                )
                continue
            try:
                new_total = quantize_money(funding_total + amount)
            except (ArithmeticError, ValueError):
                reasons.append(
                    f"funding settlement {point.ts} has an implausible accumulated total; "
                    "not accrued"
                )
                continue
            # Funding is a cash movement only. It is never written into
            # ``management_history`` (whose contract actions are management
            # decisions) and never advances ``updated_at``: the persisted mark
            # and its as-of must stay coherent, or the stale-mark rule would
            # mask a newer recorded bar. Only the optimistic version moves.
            updated = dict(current_payload)
            updated["funding_usd"] = plain_money_str(new_total)
            updated["version"] = int(updated.get("version", 1)) + 1
            updated_json = json.dumps(updated, sort_keys=True)
            accrual_id = deterministic_id("FND", f"{position_id}:{point.ts}")
            amount_text = plain_money_str(amount)
            # The FUNDING event is derived from the accrual it records: the same
            # accrual id and amount appear in the note, at the same settlement
            # timestamp, so the two rows are traceable to each other.
            commit = FundingAccrualCommit(
                accrual=FundingAccrualRecord(
                    accrual_id=accrual_id,
                    position_id=position_id,
                    settlement_time=point.ts,
                    rate=decimal_str(point.rate),
                    mark_price=decimal_str(mark),
                    amount_usd=amount_text,
                    created_at=now_value,
                    side=position.side.value,
                    size=decimal_str(size),
                ),
                position=PositionRecord(
                    position_id=position_id,
                    account_id=position.account_id,
                    environment=position.environment,
                    symbol=position.symbol,
                    state="OPEN",
                    opened_at=position.opened_at,
                    payload_json=updated_json,
                    expected_payload_json=current_payload_json,
                ),
                account=self._paper.ensure(),
                event=PositionEventRecord(
                    position_id=position_id,
                    seq=None,
                    at=point.ts,
                    action="FUNDING",
                    order_id=None,
                    note=(
                        f"funding accrual {accrual_id} amount {amount_text} "
                        f"rate {decimal_str(point.rate)}"
                    ),
                ),
            )
            if self._store.save_funding_accrual(commit):
                existing.add(point.ts)
                current_payload = updated
                current_payload_json = updated_json
                funding_total = new_total
                accrued += 1
        return {
            "position_id": position_id,
            "accrued": accrued,
            "reasons": tuple(reasons),
        }

    def _funding_history(self, position: Position) -> _FundingHistory:
        """Every recorded funding settlement for the position's symbol.

        Each recorded report contributes the ``orderflow.funding`` history it
        observed. Points are unioned by ``ts`` and the newest recorded report
        wins a duplicate. Absent or corrupt sources are named rather than
        silently treated as "no settlements".
        """
        try:
            reports = self._store.list_reports_for_symbol(
                position.symbol,
                since=position.opened_at,
                limit=_MAX_FUNDING_REPORTS,
            )
        except StoreUnavailableError as exc:
            _LOGGER.warning("cannot read reports for %s: %s", position.symbol, exc)
            return _FundingHistory((), None, (f"funding source is unavailable: {exc}",))
        if not reports:
            return _FundingHistory(
                (),
                None,
                (f"funding source is missing: no recorded report for {position.symbol}",),
            )
        by_ts: dict[str, tuple[str, Decimal | None]] = {}
        interval: Decimal | None = None
        reasons: list[str] = []
        saw_funding = False
        for report in sorted(reports, key=lambda item: (item.generated_at, item.report_id)):
            try:
                snapshot = json.loads(report.snapshot_json)
            except (json.JSONDecodeError, TypeError, ValueError):
                reasons.append(
                    f"funding source is corrupt: report {report.report_id} snapshot is unreadable"
                )
                continue
            orderflow = snapshot.get("orderflow") if isinstance(snapshot, dict) else None
            funding = orderflow.get("funding") if isinstance(orderflow, dict) else None
            if not isinstance(funding, dict):
                continue
            saw_funding = True
            maybe_interval = _decimal_or_none(funding.get("interval_hours"))
            if maybe_interval is not None and ZERO < maybe_interval <= _MAX_FUNDING_INTERVAL_HOURS:
                interval = maybe_interval
            history = funding.get("history")
            if not isinstance(history, list):
                reasons.append(
                    f"funding source is corrupt: report {report.report_id} history is not a list"
                )
                continue
            for item in history:
                if not isinstance(item, dict):
                    reasons.append("funding source is corrupt: a settlement is not an object")
                    continue
                ts = item.get("ts")
                if not isinstance(ts, str) or _parse_timestamp(ts) is None:
                    reasons.append(
                        "funding source is corrupt: a settlement has an invalid timestamp"
                    )
                    continue
                rate = _decimal_or_none(item.get("rate"))
                if rate is not None and abs(rate) > MAX_ABS_FUNDING_RATE:
                    reasons.append(f"funding settlement {ts} has an implausible rate; not accrued")
                    rate = None
                previous = by_ts.get(ts)
                if previous is None or report.generated_at >= previous[0]:
                    by_ts[ts] = (report.generated_at, rate)
        if not saw_funding:
            reasons.append(
                f"funding source is missing: no recorded funding document for {position.symbol}"
            )
        points = tuple(
            _FundingPoint(ts, rate)
            for ts, (_generated_at, rate) in sorted(
                by_ts.items(),
                key=lambda item: _parse_timestamp(item[0]) or datetime.min.replace(tzinfo=UTC),
            )
        )
        return _FundingHistory(points, interval, tuple(reasons))

    def _management_timeline(self, position: Position) -> _ManagementTimeline:
        """Reconstruct the size/average entry in force from the recorded events."""
        try:
            events = self._store.list_position_events(position.position_id)
        except StoreUnavailableError as exc:
            return _ManagementTimeline(
                position.size,
                position.entry_price,
                (),
                ZERO,
                (f"management timeline is unavailable: {exc}",),
            )
        reasons: list[str] = []
        adjustments: list[tuple[str, str, Decimal, Decimal | None]] = []
        for event in events:
            if event.action not in {PARTIAL_CLOSE, ADD}:
                continue
            if event.order_id is None:
                reasons.append(
                    f"management event {event.action} at {event.at} has no order; "
                    "the size timeline is incomplete"
                )
                continue
            fill = self._store.get_fill_by_order_id(event.order_id)
            if fill is None:
                reasons.append(
                    f"management fill {event.order_id} is missing; the size timeline is incomplete"
                )
                continue
            try:
                fill_payload = json.loads(fill.payload_json)
            except (json.JSONDecodeError, TypeError, ValueError):
                reasons.append(
                    f"management fill {event.order_id} is unreadable; "
                    "the size timeline is incomplete"
                )
                continue
            quantity = (
                _decimal_or_none(fill_payload.get("quantity"))
                if isinstance(fill_payload, dict)
                else None
            )
            price = (
                _decimal_or_none(fill_payload.get("price"))
                if isinstance(fill_payload, dict)
                else None
            )
            if quantity is None or quantity <= 0 or quantity > MAX_ABS_QUANTITY:
                reasons.append(
                    f"management fill {event.order_id} has no usable quantity; "
                    "the size timeline is incomplete"
                )
                continue
            if price is not None and (price <= 0 or price > MAX_ABS_PRICE):
                price = None
            adjustments.append((event.at, event.action, quantity, price))
        try:
            partial_quantity = quantize_money(
                sum(
                    (qty for _at, action, qty, _price in adjustments if action == PARTIAL_CLOSE),
                    ZERO,
                )
            )
            added_quantity = quantize_money(
                sum(
                    (qty for _at, action, qty, _price in adjustments if action == ADD),
                    ZERO,
                )
            )
        except (ArithmeticError, ValueError):
            reasons.append(
                "the size timeline carries implausible recorded quantities; "
                "the size timeline is incomplete"
            )
            return _ManagementTimeline(
                position.size,
                position.entry_price,
                (),
                ZERO,
                tuple(reasons),
            )
        initial_size = position.size + partial_quantity - added_quantity
        if initial_size <= 0:
            reasons.append(
                "the size timeline is inconsistent: the derived initial size is not positive"
            )
            return _ManagementTimeline(
                position.size,
                position.entry_price,
                (),
                partial_quantity,
                tuple(reasons),
            )
        steps: list[_TimelineStep] = []
        size = initial_size
        avg_entry = position.entry_price
        for at, action, quantity, price in sorted(
            adjustments,
            key=lambda item: _parse_timestamp(item[0]) or datetime.min.replace(tzinfo=UTC),
        ):
            as_of = _parse_timestamp(at)
            if action == ADD:
                if price is None or price <= 0:
                    reasons.append(
                        f"management add fill at {at} has no usable price; "
                        "the size timeline is incomplete"
                    )
                    continue
                try:
                    avg_entry = weighted_average_entry(avg_entry, size, price, quantity)
                except (ArithmeticError, ValueError):
                    reasons.append(
                        f"management add fill at {at} has implausible values; "
                        "the size timeline is incomplete"
                    )
                    continue
                size = size + quantity
            else:
                size = size - quantity
            steps.append(_TimelineStep(at, as_of, size, avg_entry))
        if size <= 0 and not reasons:
            reasons.append(
                "the size timeline is inconsistent: the reconstructed size is not positive"
            )
        return _ManagementTimeline(
            initial_size,
            position.entry_price,
            tuple(steps),
            partial_quantity,
            tuple(reasons),
        )

    def _read_series(
        self,
        position: Position,
        period: str,
        metric_version: str,
        cache: recorded_bars.SeriesCache | None,
    ) -> tuple[dict[str, object], ...]:
        """Read one logical bars series, reusing it within one operation."""
        return recorded_bars.read_series(
            self._settings,
            self._store,
            position.symbol,
            period,
            metric_version,
            cache,
        ).rows

    def _window_bars(
        self,
        position: Position,
        start: str,
        end: str,
        *,
        series: set[tuple[str, str]] | None = None,
        cache: recorded_bars.SeriesCache | None = None,
    ) -> tuple[dict[str, object], ...]:
        """The recorded bars whose ``bar_end`` falls inside the holding window."""
        return recorded_bars.window_bars(
            self._settings,
            self._store,
            position.symbol,
            start,
            end,
            series=series,
            cache=cache,
        ).rows

    def _bar_series(self, position: Position) -> set[tuple[str, str]]:
        series, _failed = recorded_bars.bar_series(self._store, position.symbol)
        return series

    # -- management tick -------------------------------------------------

    def monitor(self, position_id: str) -> PositionMonitorResult:
        """Accrue recorded funding, then run the stop-touch rule for one tick.

        Funding settles independently of the stop rule and is applied first, so
        any close in the same tick carries the recorded funding term. The result
        always reports the accrual outcome (``accrued`` and ``reasons``) alongside
        the stop-touch status.
        """
        record = self._store.get_position(position_id)
        if record is None:
            raise PositionNotFound(position_id)
        payload = self._decode(record.payload_json, subject=f"position {position_id}")
        if payload.get("state") != "OPEN":
            trade = self._store.get_trade_log_for_position(position_id)
            if trade is None:
                raise PositionCloseError(
                    "POSITION_STATE_INVALID",
                    f"position {position_id} is {payload.get('state')}, not OPEN",
                )
            return self._replay(record, trade)
        try:
            position = position_from_contract(payload)
        except PositionError as exc:
            raise PositionCloseError(exc.code, exc.message) from exc

        now_dt = self._clock.now()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        now_dt = now_dt.astimezone(UTC)
        now = utc_timestamp(now_dt)

        # Resolve the recorded bars and funding points once for the whole tick.
        # ``_read_series`` caches each logical series, so the mark lookup, the
        # funding accrual, and the journal facts share one ``read_bars`` sweep
        # per series instead of four.
        series = self._bar_series(position)
        cache: recorded_bars.SeriesCache = {}
        self._tick_series = series
        self._tick_cache = cache
        window_bars = self._window_bars(
            position, position.opened_at, now, series=series, cache=cache
        )
        history = self._funding_history(position)

        # Funding settles independently of the stop rule. Accruing first means a
        # close that follows in the same tick carries the recorded funding term
        # and its cash delta; the accrual is idempotent across restarts.
        funding = self._accrue_with_retry(position_id, now, bars=window_bars, history=history)
        if funding["accrued"]:
            record = self._store.get_position(position_id)
            if record is None:  # pragma: no cover - the row was just read
                raise PositionNotFound(position_id)
            payload = self._decode(record.payload_json, subject=f"position {position_id}")
            try:
                position = position_from_contract(payload)
            except PositionError as exc:
                raise PositionCloseError(exc.code, exc.message) from exc

        observation = self._resolve_mark(position, payload, now_dt)
        if observation is None:
            self._notify_stale(position_id=position_id, symbol=position.symbol, now=now)
            return PositionMonitorResult(
                position_id=position_id,
                status=NO_ACTION,
                reason_code=DATA_STALE,
                reason="the position has no recorded mark price",
                funding=funding,
            )
        if self._is_stale(now, observation.as_of):
            self._notify_stale(position_id=position_id, symbol=position.symbol, now=now)
            return PositionMonitorResult(
                position_id=position_id,
                status=NO_ACTION,
                reason_code=DATA_STALE,
                reason=(
                    f"the recorded mark is older than {self._stale_seconds}s; no order was created"
                ),
                funding=funding,
            )
        try:
            decision = evaluate_protective_exit(position, observation.mark, at=now)
        except PositionError as exc:
            raise PositionCloseError(exc.code, exc.message) from exc
        if decision is None:
            return PositionMonitorResult(
                position_id=position_id,
                status=NO_ACTION,
                reason_code=STOP_NOT_TOUCHED,
                reason="the mark did not touch the protective stop",
                funding=funding,
            )
        return self._close(
            position,
            payload,
            record.payload_json,
            observation.mark,
            observation.as_of,
            now,
            bars=window_bars,
            history=history,
            series=series,
            cache=cache,
            funding=funding,
        )

    # -- management planning (slice 25) ----------------------------------

    def management_mark(self, position_id: str) -> ManagementMark:
        """Resolve the fresh recorded mark the management decision acts on.

        Raises ``PositionNotFound`` for an unknown position,
        ``POSITION_STATE_INVALID`` for a non-OPEN one, and a typed
        ``DATA_UNAVAILABLE``/``DATA_STALE`` when no fresh mark exists. No order
        is created here; the caller validates the LLM proposal first.
        """
        record = self._store.get_position(position_id)
        if record is None:
            raise PositionNotFound(position_id)
        payload = self._decode(record.payload_json, subject=f"position {position_id}")
        if payload.get("state") != "OPEN":
            raise PositionCloseError(
                "POSITION_STATE_INVALID",
                f"position {position_id} is {payload.get('state')}, not OPEN",
            )
        try:
            position = position_from_contract(payload)
        except PositionError as exc:
            raise PositionCloseError(exc.code, exc.message) from exc
        if position.source_trigger_id is None:
            raise PositionCloseError(
                "DATA_UNAVAILABLE",
                "the position has no source trigger; the management decision cannot be bound",
            )
        now = self._now()
        now_dt = _parse_timestamp(now)
        if now_dt is None:  # pragma: no cover - ``_now`` always renders a timestamp
            raise PositionCloseError("DATA_UNAVAILABLE", "the service clock is unreadable")
        observation = self._resolve_mark(position, payload, now_dt)
        if observation is None:
            raise PositionCloseError("DATA_UNAVAILABLE", "the position has no recorded mark price")
        if self._is_stale(now, observation.as_of):
            raise PositionCloseError(
                "DATA_STALE",
                f"the recorded mark is older than {self._stale_seconds}s; "
                "no management decision was executed",
            )
        return ManagementMark(
            position=position,
            payload=payload,
            payload_json=record.payload_json,
            mark=observation.mark,
            mark_as_of=observation.as_of,
            now=now,
        )

    def recorded_mark(
        self,
        position_id: str,
        *,
        record: PositionRecord | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[Decimal, str] | None:
        """The position's newest recorded ``(mark, as_of)``, or ``None``.

        Uses the same resolver as the monitor/management paths (the newest
        published bar close, then the stored ``mark_price``/``updated_at``) so a
        loss measurement values the account at a real mark. Missing or corrupt
        rows return ``None`` instead of raising: the caller reports a degraded
        measurement, never a fabricated mark or price.

        A caller that already read the row (and optionally decoded its payload)
        passes them in so the window measurement does not read or decode the
        same position twice.
        """
        try:
            if record is None:
                record = self._store.get_position(position_id)
                if record is None:
                    return None
            if payload is None:
                payload = self._decode(record.payload_json, subject=f"position {position_id}")
            position = position_from_contract(payload)
            now_dt = self._clock.now()
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=UTC)
            observation = self._resolve_mark(position, payload, now_dt.astimezone(UTC))
        except (
            KeyError,
            TypeError,
            ValueError,
            ArithmeticError,
            PositionError,
            PositionCloseError,
        ):
            return None
        if observation is None:
            return None
        return observation.mark, observation.as_of

    def persist_management_mark(self, context: ManagementMark) -> ManagementMark:
        """Write the chosen recorded mark to the OPEN row and return a fresh context.

        The ADD risk verdict digest binds the account equity measured at the
        same recorded mark the gateway will re-read, so the mark is persisted
        before the gate is evaluated (a later exit path does the same).
        """
        self._persist_mark(
            context.position.position_id,
            context.payload,
            context.payload_json,
            context.mark,
            context.mark_as_of,
        )
        record = self._store.get_position(context.position.position_id)
        if record is None:  # pragma: no cover - the row was just updated
            raise PositionNotFound(context.position.position_id)
        payload = self._decode(
            record.payload_json, subject=f"position {context.position.position_id}"
        )
        return replace(context, payload=payload, payload_json=record.payload_json)

    def full_close_context(self, context: ManagementMark, *, close_reason: str) -> TradeLogContext:
        """The complete journal facts for a management full close.

        A management close is manual rather than a protective stop touch; the
        outcome authority (``close_reason``) drives the flags so the journal and
        its outcome tags cannot disagree.
        """
        built = self._trade_log_context(
            context.position, context.payload, context.now, close_reason=close_reason
        )
        return built

    def persist_management_exit_verdict(
        self,
        *,
        position: Position,
        decision_id: str,
        quantity: Decimal,
        mark: Decimal,
        now: str,
        purpose: str,
    ) -> str:
        """Persist the code-owned ALLOW verdict a management exit order binds to.

        An ``ADD`` uses its own risk verdict (:mod:`oft.application.management_add`)
        and never binds this one.
        """
        risk_verdict_id = deterministic_id("RISK-MGD", f"{decision_id}:{purpose}:{quantity}:{mark}")
        self._persist_verdict(
            risk_verdict_id=risk_verdict_id,
            decision_id=decision_id,
            position=position,
            mark=mark,
            now=now,
            quantity=quantity,
            purpose=purpose,
            reason="risk-reducing management exit",
        )
        return risk_verdict_id

    def prepare_management_exit(
        self,
        context: ManagementMark,
        *,
        decision_id: str,
        purpose: str,
        quantity: Decimal,
        idempotency_key: str,
        risk_verdict_id: str,
        approval_id: str,
        note: str,
    ) -> BrokerResult:
        """Submit one reduce-only management close through the gateway.

        A ``FULL_CLOSE`` requires (and commits exactly one) trade log; a
        ``PARTIAL_CLOSE`` passes no context and keeps the position OPEN. The
        chosen mark is persisted first so the gateway's re-read matches.
        """
        position = context.position
        if purpose not in {PARTIAL_CLOSE, FULL_CLOSE}:
            raise PositionCloseError(
                "POSITION_STATE_INVALID", f"{purpose} is not a management exit"
            )
        if quantity <= 0 or quantity > position.size:
            raise PositionCloseError(
                "POSITION_STATE_INVALID", "the management exit quantity is invalid"
            )
        close_reason = MANUAL_CLOSE_RULE
        trade_context: TradeLogContext | None = None
        if purpose == FULL_CLOSE:
            if quantity != position.size:
                raise PositionCloseError(
                    "POSITION_STATE_INVALID", "a full close must close the whole position"
                )
            trade_context = self.full_close_context(context, close_reason=close_reason)
        intent = OrderIntent(
            symbol=position.symbol,
            side=reducing_side(position.side),
            order_type=OrderType.MARKET,
            quantity=quantity,
            price=None,
            stop_price=position.stop_price,
            take_profits=(),
            leverage=position.leverage,
            decision_id=decision_id,
            risk_verdict_id=risk_verdict_id,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
            position_side=position.side,
            risk_amount_usd=Decimal("0"),
            risk_pct=0.0,
            purpose=purpose,
            position_id=position.position_id,
            trigger_id=position.source_trigger_id,
            close_reason=close_reason,
            trade_log=trade_context,
            note=note,
        )
        return self._submit_management_intent(context, intent)

    def _submit_management_intent(
        self,
        context: ManagementMark,
        intent: OrderIntent,
    ) -> BrokerResult:
        """Persist the decision mark, then submit one management intent.

        The persist-mark, ``AuthContext``, gateway submit, and typed error
        mapping are shared; each intent keeps its own risk amount, side,
        purpose, and trade context intact.
        """
        self._persist_mark(
            context.position.position_id,
            context.payload,
            context.payload_json,
            context.mark,
            context.mark_as_of,
        )
        try:
            return self._gateway.submit(
                intent,
                AuthContext(
                    actor_id="management_service",
                    approval_id=intent.approval_id,
                ),
                mark_price=context.mark,
                mark_as_of=context.mark_as_of,
            )
        except (GatewayError, BrokerError) as exc:
            raise PositionCloseError(exc.code, exc.message) from exc

    def prepare_management_add(
        self,
        context: ManagementMark,
        *,
        decision_id: str,
        quantity: Decimal,
        idempotency_key: str,
        risk_verdict_id: str,
        approval_id: str,
        stop_price: Decimal,
        leverage: float,
        note: str = "",
    ) -> BrokerResult:
        """Submit one position-increasing add through the gateway.

        The add never creates a second position; the broker updates the one OPEN
        row. The risk amount is recomputed in code from the submitted mark and
        the position's protective stop, never from the LLM payload.
        """
        position = context.position
        if quantity <= 0:
            raise PositionCloseError("POSITION_STATE_INVALID", "the add quantity is invalid")
        if stop_price <= 0:
            raise PositionCloseError("POSITION_STATE_INVALID", "the add requires a stop")
        try:
            risk_amount = quantize_money(abs(context.mark - stop_price) * quantity)
        except (ArithmeticError, ValueError) as exc:
            raise PositionCloseError(
                "DATA_UNAVAILABLE", "the add risk at the mark is implausible"
            ) from exc
        intent = OrderIntent(
            symbol=position.symbol,
            side=increasing_side(position.side),
            order_type=OrderType.MARKET,
            quantity=quantity,
            price=None,
            stop_price=stop_price,
            take_profits=(),
            leverage=leverage,
            decision_id=decision_id,
            risk_verdict_id=risk_verdict_id,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
            position_side=position.side,
            risk_amount_usd=risk_amount,
            risk_pct=0.0,
            purpose=ADD,
            position_id=position.position_id,
            trigger_id=position.source_trigger_id,
            note=note,
        )
        return self._submit_management_intent(context, intent)

    def record_management_hold(
        self, position_id: str, *, decision_id: str, note: str, now: str
    ) -> dict[str, Any]:
        """Persist a HOLD action in ``management_history`` plus its audit.

        HOLD creates no order, fill, or trade log, but it does persist the
        management-history entry and its audit event. The history action is one
        of the contract's management actions; the audit event carries the LLM
        decision provenance.
        """
        record = self._store.get_position(position_id)
        if record is None:
            raise PositionNotFound(position_id)
        payload = self._decode(record.payload_json, subject=f"position {position_id}")
        if payload.get("state") != "OPEN":
            raise PositionCloseError(
                "POSITION_STATE_INVALID",
                f"position {position_id} is {payload.get('state')}, not OPEN",
            )
        history = payload.get("management_history")
        if not isinstance(history, list):
            raise PositionCloseError(
                "DATA_UNAVAILABLE", "the position management history is corrupt"
            )
        updated = dict(payload)
        updated["management_history"] = [
            *history,
            {
                "decision_id": decision_id,
                "action": "HOLD",
                "at": now,
                "executed_order_id": None,
                "note": note,
            },
        ]
        return self._save_management_payload(
            record,
            updated,
            now=now,
            event_type="MANAGEMENT_HOLD",
            payload={"decision_id": decision_id, "action": "HOLD"},
        )

    def apply_management_stop(
        self,
        position_id: str,
        *,
        stop_price: Decimal | None,
        target_prices: tuple[Decimal, ...] | None,
        decision_id: str,
        now: str,
        reference: Decimal | None = None,
    ) -> dict[str, Any]:
        """Apply a validated stop and/or target change to the OPEN position row.

        ``stop_price=None`` means "leave the stop unchanged"; a target-only
        change on a stop-less position must never synthesize a placeholder stop
        (a persisted ``0`` is contract-invalid). A supplied stop is checked for
        positivity independently of :func:`validate_stop_side`, because a caller
        that bypasses the domain validation must still be refused. The stop's
        side is revalidated against ``reference`` -- the same recorded mark the
        LLM-facing validation used (falling back to the position's own recorded
        mark and then the average entry) -- so a profit-locking trailing stop is
        accepted here exactly when it was accepted upstream. A caller cannot
        persist a stop that would never protect the position, and it cannot
        loosen or remove the stop either: that would be a risk-increasing change
        the management path forbids (defense in depth for replay and any direct
        caller). No management history action is written (stop updates are not
        in the contract's action enum); the change is recorded in the audit chain
        as ``STOP_UPDATED``.
        """
        record = self._store.get_position(position_id)
        if record is None:
            raise PositionNotFound(position_id)
        payload = self._decode(record.payload_json, subject=f"position {position_id}")
        if payload.get("state") != "OPEN":
            raise PositionCloseError(
                "POSITION_STATE_INVALID",
                f"position {position_id} is {payload.get('state')}, not OPEN",
            )
        if stop_price is None and target_prices is None:
            return payload
        if stop_price is not None:
            try:
                position = position_from_contract(payload)
                if not stop_price.is_finite() or stop_price <= 0 or stop_price > MAX_ABS_PRICE:
                    raise PositionError(
                        "POSITION_STATE_INVALID",
                        "the management stop must be a positive price",
                    )
                side_reference = reference or position.mark_price or position.avg_entry_price
                validate_stop_side(position.side, side_reference, stop_price)
                current_stop = position.stop_price
                if current_stop is not None:
                    change = classify_management_intent(
                        action=ManagementAction.PARTIAL_CLOSE.value,
                        position_side=position.side,
                        current_stop=current_stop,
                        new_stop_price=stop_price,
                    ).stop_change
                    if change in (StopChange.LOOSEN, StopChange.REMOVE):
                        raise PositionError(
                            "RISK_BLOCKED",
                            "a management action may not loosen or remove the protective stop",
                        )
            except PositionError as exc:
                raise PositionCloseError(exc.code, exc.message) from exc
        updated = dict(payload)
        if stop_price is not None:
            updated["stop_price"] = str(stop_price)
        if target_prices is not None:
            updated["target_prices"] = [str(price) for price in target_prices]
        return self._save_management_payload(
            record,
            updated,
            now=now,
            event_type="STOP_UPDATED",
            payload={
                "decision_id": decision_id,
                "stop_price": None if stop_price is None else str(stop_price),
                "targets": (
                    None if target_prices is None else [str(price) for price in target_prices]
                ),
            },
        )

    def _save_management_payload(
        self,
        record: PositionRecord,
        updated: dict[str, Any],
        *,
        now: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Commit an order-less management payload change and its audit atomically.

        Like the funding path, this never advances ``updated_at``: that field is
        the recorded mark's as-of, and moving it without moving ``mark_price``
        would make an old stored price look like the newest, freshest
        observation. Only the optimistic version moves.
        """
        updated["version"] = int(updated.get("version", 1)) + 1
        try:
            self._store.save_management_update(
                ManagementUpdateRecord(
                    position=PositionRecord(
                        position_id=record.position_id,
                        account_id=record.account_id,
                        environment=record.environment,
                        symbol=record.symbol,
                        state="OPEN",
                        opened_at=str(updated.get("opened_at") or record.opened_at),
                        payload_json=json.dumps(updated, sort_keys=True),
                        expected_payload_json=record.payload_json,
                    ),
                    audits=(
                        AuditEventDraft(
                            event_type=event_type,
                            actor={
                                "kind": "LLM",
                                "id": str(payload.get("decision_id") or "management"),
                            },
                            subject={"kind": "POSITION", "id": record.position_id},
                            payload=payload,
                            created_at=now,
                        ),
                    ),
                )
            )
        except OmsConflictError as exc:
            raise PositionCloseError(
                "CONFLICT", "the position changed concurrently; retry the management action"
            ) from exc
        return updated

    def _resolve_mark(
        self,
        position: Position,
        payload: dict[str, Any],
        now_dt: datetime,
    ) -> _MarkObservation | None:
        """Pick the newest recorded mark: latest bar close first, then the row."""
        candidates = [
            observation
            for observation in (
                self._latest_bar_mark(position, now_dt),
                self._stored_mark(position, payload),
            )
            if observation is not None
        ]
        if not candidates:
            return None
        return max(candidates, key=_observation_rank)

    def _latest_bar_mark(self, position: Position, now_dt: datetime) -> _MarkObservation | None:
        """The newest usable published bar close across every published series."""
        series = self._tick_series if self._tick_series is not None else self._bar_series(position)
        observations: list[_MarkObservation] = []
        for period, metric_version in sorted(series):
            rows = self._read_series(position, period, metric_version, self._tick_cache)
            observation = self._latest_valid_bar_row(position.symbol, rows, now_dt)
            if observation is not None:
                observations.append(observation)
        if not observations:
            return None
        # The newest recorded observation wins, regardless of which series'
        # manifest row happened to be newest.
        return max(observations, key=_observation_rank)

    @staticmethod
    def _latest_valid_bar_row(
        symbol: str,
        rows: tuple[dict[str, object], ...],
        now_dt: datetime,
    ) -> _MarkObservation | None:
        """The newest row with a parseable past ``bar_end`` and a positive close."""
        for row in reversed(rows):
            bar_end = _parse_timestamp(row.get("bar_end"))
            if bar_end is None:
                _LOGGER.warning("ignoring a bar with an unparseable bar_end for %s", symbol)
                continue
            if bar_end > now_dt:
                # A future-dated bar is not a recorded observation yet.
                continue
            try:
                close = Decimal(str(row.get("close")))
            except ArithmeticError:
                _LOGGER.warning("ignoring a bar with an unreadable close for %s", symbol)
                continue
            if not close.is_finite() or close <= 0 or close > MAX_ABS_PRICE:
                _LOGGER.warning("ignoring an implausible bar close for %s", symbol)
                continue
            return _MarkObservation(close, utc_timestamp(bar_end), _MARK_SOURCE_BARS)
        return None

    @staticmethod
    def _stored_mark(position: Position, payload: dict[str, Any]) -> _MarkObservation | None:
        """The mark already recorded on the position row, with its ``updated_at``."""
        mark = position.mark_price
        as_of = str(payload.get("updated_at") or "")
        if mark is None or not as_of:
            return None
        if not mark.is_finite() or mark <= 0 or mark > MAX_ABS_PRICE:
            return None
        return _MarkObservation(mark, as_of, _MARK_SOURCE_POSITION)

    def _is_stale(self, now: str, mark_as_of: str) -> bool:
        try:
            age = (_parse(now) - _parse(mark_as_of)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            # An unparseable mark timestamp is never treated as fresh.
            return True
        return age > self._stale_seconds

    # -- journal facts ---------------------------------------------------

    def _trade_log_context(
        self,
        position: Position,
        payload: dict[str, Any],
        now: str,
        *,
        bars: tuple[dict[str, object], ...] | None = None,
        history: _FundingHistory | None = None,
        series: set[tuple[str, str]] | None = None,
        cache: recorded_bars.SeriesCache | None = None,
        close_reason: str = STOP_TOUCH_RULE,
    ) -> TradeLogContext:
        """Compute the complete journal facts for the upcoming full close."""
        outcome = CloseOutcome.from_reason(close_reason)
        source = self._source_facts(position)
        reasons = list(source.reasons)
        bars = (
            bars
            if bars is not None
            else self._window_bars(position, position.opened_at, now, series=series, cache=cache)
        )
        timeline = self._management_timeline(position)
        reasons.extend(timeline.reasons)
        if bars:
            states = self._bar_states(position, bars, timeline)
            try:
                excursion = mae_mfe_from_sized_bars(position.side.value, bars, states)
            except (ArithmeticError, ValueError):
                mae = mfe = ZERO
                reasons.append("MAE/MFE not measured: the recorded bars or size are implausible")
            else:
                mae, mfe = excursion.mae_usd, excursion.mfe_usd
                if excursion.measured_bars == 0:
                    reasons.append(
                        "MAE/MFE not measured: no usable recorded bar covers the holding window"
                    )
                elif excursion.skipped_bars:
                    reasons.append(
                        "MAE/MFE are partial: "
                        f"{excursion.skipped_bars} recorded bar(s) in the holding window "
                        "were unusable"
                    )
        else:
            mae = mfe = ZERO
            reasons.append("MAE/MFE not measured: no recorded bars cover the holding window")
        target = position.target_prices[0] if position.target_prices else None
        touch = first_touch(
            position.side.value,
            bars,
            stop_price=position.stop_price,
            target_price=target,
        )
        if touch.ambiguous:
            reasons.append(
                "AMBIGUOUS_BAR: one recorded bar contains both the stop and the target; "
                "the stop is assumed first"
            )
        reasons.extend(self._unaccrued_funding_reasons(position, now, bars=bars, history=history))
        # The R-multiple reason must exist before the status is finalized, or a
        # zero/missing frozen risk would journal COMPLETE with a stated gap.
        if position.risk_amount_usd <= 0:
            reasons.append("R multiple not measured: the frozen initial risk is zero or missing")
        status = "PARTIAL" if reasons else "COMPLETE"
        traded_quantity = (
            timeline.partial_quantity + position.size if timeline.complete else position.size
        )
        # ``return_pct`` is measured against the immutable entry notional, not
        # the residual exposure a partial close leaves behind. When that fill
        # cannot be resolved the residual notional is NOT substituted: the
        # journal names the gap and renders a contract-valid ``return_pct``.
        entry_notional = recorded_entry_notional_usd(self._store, position.source_trigger_id)
        if entry_notional is None:
            reasons.append("entry notional is unavailable: the recorded entry fill was not found")
        status = "PARTIAL" if reasons else "COMPLETE"
        try:
            had_partial_close = recorded_partial_close(position)
        except PositionError as exc:
            raise PositionCloseError(exc.code, exc.message) from exc
        return TradeLogContext(
            signal=source.signal,
            provenance=source.provenance,
            decision_quality={
                "matched_rules": None,
                "confidence": None,
                "notes": "deterministic protective-exit rule; no LLM review in this slice",
            },
            mae_usd=mae,
            mfe_usd=mfe,
            risk_amount_usd=position.risk_amount_usd,
            entry_notional_usd=entry_notional,
            data_quality_status=status,
            data_quality_reasons=tuple(reasons),
            close_reason=close_reason,
            stopped=outcome.stopped,
            target=outcome.target,
            manual=outcome.manual,
            had_partial_close=had_partial_close,
            traded_quantity=traded_quantity,
        )

    @staticmethod
    def _bar_states(
        position: Position,
        bars: tuple[dict[str, object], ...],
        timeline: _ManagementTimeline,
    ) -> tuple[tuple[Decimal, Decimal], ...]:
        """The ``(average entry, size)`` in force for each bar's own timestamp."""
        if not timeline.complete:
            # The timeline is already named as a quality gap; measure the window
            # on the final basis rather than invent per-bar states.
            return tuple((position.avg_entry_price, position.size) for _ in bars)
        states: tuple[tuple[Decimal, Decimal], ...] = ()
        for bar in bars:
            state = timeline.state_at(bar.get("bar_end"))
            states += (state if state is not None else (position.avg_entry_price, position.size),)
        return states

    def _unaccrued_funding_reasons(
        self,
        position: Position,
        now: str,
        *,
        bars: tuple[dict[str, object], ...] | None = None,
        history: _FundingHistory | None = None,
    ) -> tuple[str, ...]:
        """Name every in-window settlement that was not accrued and why."""
        history = history if history is not None else self._funding_history(position)
        reasons: list[str] = list(history.reasons)
        existing = {
            accrual.settlement_time
            for accrual in self._store.list_funding_accruals(position.position_id)
        }
        bars = bars if bars is not None else self._window_bars(position, position.opened_at, now)
        recorded: set[str] = set()
        for point in history.points:
            if not _inside_window(point.ts, position.opened_at, now):
                continue
            recorded.add(point.ts)
            if point.ts in existing:
                continue
            if point.rate is None or _mark_at(bars, point.ts) is None:
                reasons.append(
                    f"funding settlement {point.ts} is missing a recorded rate or mark; not accrued"
                )
        reasons.extend(
            self._expected_funding_reasons(history, position.opened_at, now, recorded, existing)
        )
        return tuple(reasons)

    @staticmethod
    def _expected_funding_reasons(
        history: _FundingHistory,
        opened_at: str,
        now: str,
        recorded: set[str],
        existing: set[str],
    ) -> tuple[str, ...]:
        """Name a settlement implied by the recorded interval but not recorded."""
        interval = history.interval_hours
        if interval is None or interval <= 0 or not history.points:
            return ()
        parsed = [
            value
            for value in (_parse_timestamp(point.ts) for point in history.points)
            if value is not None
        ]
        opened_dt = _parse_timestamp(opened_at)
        now_dt = _parse_timestamp(now)
        if not parsed or opened_dt is None or now_dt is None:
            return ()
        step = timedelta(hours=float(interval))
        expected = max(parsed) + step
        reasons: list[str] = []
        count = 0
        while expected <= now_dt and count < _MAX_EXPECTED_SETTLEMENTS:
            settlement = utc_timestamp(expected)
            if expected > opened_dt and settlement not in recorded and settlement not in existing:
                reasons.append(
                    f"funding settlement {settlement} is expected every {interval} hours "
                    "but was not recorded; not accrued"
                )
            expected += step
            count += 1
        return tuple(reasons)

    def _source_facts(self, position: Position) -> _SourceFacts:
        """Signal references and provenance copied from the recorded entry decision."""
        signal: dict[str, Any] = {
            "scan_id": None,
            "report_id": None,
            "opportunity_id": None,
            "trigger_id": position.source_trigger_id,
            "decision_id": None,
        }
        provenance: dict[str, Any] = {
            "calc_metric_version": "pnl-v1",
            "prompt_version": 1,
            "llm_provider": "stub",
            "llm_model": "deterministic-protective-exit",
        }
        reasons: list[str] = []
        if position.source_trigger_id is None:
            reasons.append("entry provenance is missing: the position has no source trigger")
            return _SourceFacts(signal, provenance, tuple(reasons))
        trigger = self._store.get_trigger_event(position.source_trigger_id)
        if trigger is None:
            reasons.append("entry provenance is missing: the source trigger was not found")
            return _SourceFacts(signal, provenance, tuple(reasons))
        signal["opportunity_id"] = trigger.opportunity_id
        opportunity = self._store.get_opportunity(trigger.opportunity_id)
        opportunity_payload = (
            None
            if opportunity is None
            else decode_payload_json(
                opportunity.payload_json,
                subject=f"opportunity {opportunity.opportunity_id}",
            )
        )
        source = (opportunity_payload or {}).get("source") or {}
        if not isinstance(source, dict):
            reasons.append("entry provenance is partial: the opportunity source is corrupt")
            source = {}
        report_id = source.get("report_id")
        signal["scan_id"] = source.get("scan_id")
        signal["report_id"] = report_id
        trigger_payload = decode_payload_json(
            trigger.payload_json, subject=f"trigger {trigger.trigger_id}"
        )
        entry_decision_id = (
            None if trigger_payload is None else trigger_payload.get("entry_decision_id")
        )
        signal["decision_id"] = entry_decision_id
        if opportunity is None:
            reasons.append("entry provenance is missing: the source opportunity was not found")
        if entry_decision_id is None:
            reasons.append("entry provenance is missing: the trigger recorded no entry decision")
        else:
            decision = self._store.get_decision(str(entry_decision_id))
            if decision is None:
                reasons.append(
                    f"entry provenance is missing: decision {entry_decision_id} was not found"
                )
            else:
                _apply_decision_provenance(decision.payload_json, provenance, reasons)
        if report_id is None:
            reasons.append("entry provenance is partial: no source report is recorded")
        else:
            report = self._store.get_report(str(report_id))
            if report is None:
                reasons.append(f"entry provenance is partial: report {report_id} was not found")
            else:
                metric_version = _metric_version_from_report(report.metrics_manifest_json)
                if metric_version is None:
                    reasons.append(
                        "entry provenance is partial: the report records no calc metric version"
                    )
                else:
                    provenance["calc_metric_version"] = metric_version
        return _SourceFacts(signal, provenance, tuple(reasons))

    # -- close -----------------------------------------------------------

    def _close(
        self,
        position: Position,
        payload: dict[str, Any],
        payload_json: str,
        mark: Decimal,
        mark_as_of: str,
        now: str,
        *,
        bars: tuple[dict[str, object], ...] | None = None,
        history: _FundingHistory | None = None,
        series: set[tuple[str, str]] | None = None,
        cache: recorded_bars.SeriesCache | None = None,
        funding: dict[str, Any] | None = None,
    ) -> PositionMonitorResult:
        seed = f"{position.position_id}:{mark}:{mark_as_of}"
        ids = _CloseIds(
            decision_id=management_decision_id(position.position_id, mark, mark_as_of),
            risk_verdict_id=deterministic_id("RISK-MGD", seed),
            approval_id=deterministic_id("APR", seed),
            idempotency_key=deterministic_id("IDEM-CLOSE", seed),
        )
        # The idempotency lookup needs no journal facts (they are excluded from
        # the intent hash), so a replay tick never pays for the full context.
        probe = self._exit_intent(position, ids, None)
        existing = self._store.get_order_by_idempotency(ids.idempotency_key)
        if existing is not None:
            replayed = self._replay_existing(position, existing, probe)
            self._notify_close(
                replayed, symbol=position.symbol, decision_id=ids.decision_id, now=now
            )
            return replayed
        context = self._trade_log_context(
            position,
            payload,
            now,
            bars=bars,
            history=history,
            series=series,
            cache=cache,
        )
        intent = self._exit_intent(position, ids, context)
        if position.source_trigger_id is None:
            raise PositionCloseError(
                "DATA_UNAVAILABLE",
                "the position has no source trigger; the exit verdict cannot be bound",
            )
        self._persist_mark(position.position_id, payload, payload_json, mark, mark_as_of)

        self._persist_decision(ids.decision_id, position, mark, mark_as_of, now)
        self._persist_verdict(
            risk_verdict_id=ids.risk_verdict_id,
            decision_id=ids.decision_id,
            position=position,
            mark=mark,
            now=now,
        )
        result = self._submit_close(intent, ids, mark, mark_as_of)
        closed = self._closed_result(position, result, funding=funding)
        self._notify_close(closed, symbol=position.symbol, decision_id=ids.decision_id, now=now)
        return closed

    def _persist_mark(
        self,
        position_id: str,
        payload: dict[str, Any],
        payload_json: str,
        mark: Decimal,
        mark_as_of: str,
    ) -> None:
        """Write the chosen recorded mark to the OPEN row before an exit submit.

        The update is optimistic against the payload this tick read. A
        concurrent funding accrual, partial close, or add therefore surfaces as
        a typed conflict instead of being silently overwritten by the stale
        mark payload (which would drop the other write's funding/size/realized
        PnL from the surviving row and corrupt the later trade log).
        """
        updated = dict(payload)
        updated["mark_price"] = str(mark)
        updated["updated_at"] = mark_as_of
        updated["version"] = int(updated.get("version", 1)) + 1
        if self._store.update_open_position_payload(
            position_id,
            payload_json=json.dumps(updated, sort_keys=True),
            expected_payload_json=payload_json,
        ):
            return
        record = self._store.get_position(position_id)
        if record is None:
            raise PositionNotFound(position_id)
        current = self._decode(record.payload_json, subject=f"position {position_id}")
        if current.get("state") != "OPEN":
            raise PositionCloseError(
                "POSITION_STATE_INVALID",
                "the position is no longer OPEN; no order was created",
            )
        raise PositionCloseError(
            "CONFLICT",
            "the position changed concurrently; retry the monitor tick",
        )

    def _exit_intent(
        self,
        position: Position,
        ids: _CloseIds,
        context: TradeLogContext | None,
    ) -> OrderIntent:
        side = reducing_side(position.side)
        return OrderIntent(
            symbol=position.symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=position.size,
            price=None,
            stop_price=position.stop_price,
            take_profits=(),
            leverage=position.leverage,
            decision_id=ids.decision_id,
            risk_verdict_id=ids.risk_verdict_id,
            approval_id=ids.approval_id,
            idempotency_key=ids.idempotency_key,
            position_side=position.side,
            risk_amount_usd=Decimal("0"),
            risk_pct=0.0,
            purpose="EXIT",
            position_id=position.position_id,
            reduce_only=True,
            trigger_id=position.source_trigger_id,
            trade_log=context,
        )

    def _replay_existing(
        self,
        position: Position,
        existing: OrderRecord,
        intent: OrderIntent,
    ) -> PositionMonitorResult:
        """Replay a committed close, or reject a reused key with a new payload."""
        existing_payload = decode_payload_json(
            existing.payload_json, subject=f"order {existing.order_id}"
        )
        if existing_payload is None:
            raise PositionCloseError(
                "DATA_UNAVAILABLE", "the existing close order payload is unreadable"
            )
        if str(existing_payload.get("intent_hash") or "") != intent.hash():
            raise PositionCloseError(
                "IDEMPOTENCY_CONFLICT",
                f"idempotency key {intent.idempotency_key} was used for a different close",
            )
        trade = self._store.get_trade_log_for_position(position.position_id)
        if trade is None:
            raise PositionCloseError(
                "CONFLICT",
                "the close order exists but its trade log is missing",
            )
        record = self._store.get_position(position.position_id)
        if record is None:  # pragma: no cover - the order references it
            raise PositionNotFound(position.position_id)
        return self._replay(record, trade)

    def _submit_close(
        self,
        intent: OrderIntent,
        ids: _CloseIds,
        mark: Decimal,
        mark_as_of: str,
    ) -> BrokerResult:
        try:
            return self._gateway.submit(
                intent,
                AuthContext(actor_id="position_service", approval_id=ids.approval_id),
                mark_price=mark,
                mark_as_of=mark_as_of,
            )
        except (GatewayError, BrokerError) as exc:
            raise PositionCloseError(exc.code, exc.message) from exc

    def _closed_result(
        self,
        position: Position,
        result: BrokerResult,
        *,
        funding: dict[str, Any] | None = None,
    ) -> PositionMonitorResult:
        trade = self._store.get_trade_log_for_position(position.position_id)
        if trade is None:
            raise PositionCloseError("CONFLICT", "the close committed but no trade log was written")
        record = self._store.get_position(position.position_id)
        if record is None:  # pragma: no cover - the close updates the row
            raise PositionNotFound(position.position_id)
        trade_payload = self._decode(trade.payload_json, subject=f"trade log {trade.trade_id}")
        position_payload = self._decode(
            record.payload_json, subject=f"position {position.position_id}"
        )
        return PositionMonitorResult(
            position_id=position.position_id,
            status=CLOSED,
            reason_code=STOP_TOUCH,
            reason="the mark touched the protective stop; the position was closed",
            action="FULL_CLOSE",
            order=result.order.to_contract(),
            fill=result.fill.to_contract(),
            position=position_payload,
            trade_log=trade_payload,
            account=self._paper.summary(),
            idempotent_replay=False,
            funding=funding,
        )

    def _notify_stale(self, *, position_id: str, symbol: str, now: str) -> None:
        """Push one read-only DATA_STALE event per position per UTC day.

        The marker is durable (``enqueue_cooled``), so repeated monitor ticks
        cannot become a message storm and a restart does not re-alert. The
        event carries no order or action affordance.
        """
        if self._notifications is None:
            return
        period = now[:10]
        try:
            self._notifications.enqueue_cooled(
                build_position_management_event(
                    position_id=position_id,
                    symbol=symbol,
                    action="MONITOR",
                    status="DEGRADED",
                    size=None,
                    price=None,
                    reason_code=DATA_STALE,
                    decision_id=f"monitor:{position_id}:{period}",
                    occurred_at=now,
                ),
                cooldown_key=f"position:stale:{position_id}",
                period=period,
            )
        except Exception as exc:  # noqa: BLE001 - a push never breaks the monitor
            _LOGGER.warning("position_stale_alert_failed for %s: %s", position_id, exc)

    def _notify_close(
        self,
        result: PositionMonitorResult,
        *,
        symbol: str,
        decision_id: str,
        now: str,
    ) -> None:
        """Push one executed protective-exit event, deduped by decision id.

        An idempotent replay carries the same decision id, so the outbox key is
        unchanged and no second message is enqueued. A failure is logged and
        swallowed: the ledger transaction has already committed.
        """
        if self._notifications is None:
            return
        fill = result.fill or {}
        order = result.order or {}
        size = fill.get("quantity")
        price = fill.get("price")
        if size is None:
            size = order.get("quantity")
        if price is None:
            price = order.get("price")
        try:
            self._notifications.enqueue_event(
                build_position_management_event(
                    position_id=result.position_id,
                    symbol=symbol,
                    action=result.action or "FULL_CLOSE",
                    status="EXECUTED",
                    size=size,
                    price=price,
                    reason_code=result.reason_code,
                    decision_id=decision_id,
                    occurred_at=now,
                )
            )
        except Exception as exc:  # noqa: BLE001 - a push never breaks the close
            _LOGGER.warning("position_close_alert_failed for %s: %s", result.position_id, exc)

    def _persist_decision(
        self,
        decision_id: str,
        position: Position,
        mark: Decimal,
        mark_as_of: str,
        now: str,
    ) -> None:
        self._store.save_decision(
            DecisionRecord(
                decision_id=decision_id,
                role="POSITION_MANAGEMENT",
                decision_type="POSITION_MANAGEMENT",
                validation_status="VALID",
                raw_output_hash=hashlib.sha256(
                    f"{decision_id}:{mark}:{mark_as_of}".encode()
                ).hexdigest(),
                persisted_at=now,
                payload_json=json.dumps(
                    {
                        "decision_id": decision_id,
                        "decision_type": "POSITION_MANAGEMENT",
                        "position_id": position.position_id,
                        "action": "FULL_CLOSE",
                        "rule": "STOP_TOUCH",
                        "mark_price": str(mark),
                        "mark_as_of": mark_as_of,
                        "decided_at": now,
                        "provenance": "CALC",
                    },
                    sort_keys=True,
                ),
            )
        )

    def _persist_verdict(
        self,
        *,
        risk_verdict_id: str,
        decision_id: str,
        position: Position,
        mark: Decimal,
        now: str,
        quantity: Decimal | None = None,
        purpose: str = "EXIT",
        reason: str = "risk-reducing protective exit",
    ) -> None:
        resolved_quantity = position.size if quantity is None else quantity
        expires_at = utc_timestamp(_parse(now) + timedelta(seconds=RISK_VERDICT_TTL_SECONDS))
        try:
            notional = quantize_money(mark * resolved_quantity)
        except (ArithmeticError, ValueError) as exc:
            raise PositionCloseError(
                "DATA_UNAVAILABLE",
                "the recorded mark and position size make the exit notional implausible",
            ) from exc
        verdict = RiskVerdict(
            verdict=Verdict.ALLOW,
            requested_quantity=resolved_quantity,
            allowed_quantity=resolved_quantity,
            allowed_notional_usd=notional,
            allowed_leverage=position.leverage,
            risk_amount_usd=Decimal("0"),
            binding_cap=BindingCap.NONE,
            reasons=(reason,),
            computed_at=now,
            expires_at=expires_at,
            inputs_hash=hashlib.sha256(
                f"{risk_verdict_id}:{mark}:{resolved_quantity}".encode()
            ).hexdigest(),
            stale_inputs=(),
            entry_price=mark,
            stop_price=position.stop_price,
        )
        requested_payload = {
            "entry_price": str(mark),
            # A stop-less position has no protective stop; persist the contract's
            # decimal "0" (as the ADD gate does) rather than the Python string
            # "None", which is not a valid decimal.
            "stop_price": "0" if position.stop_price is None else str(position.stop_price),
            "quantity": str(resolved_quantity),
            "risk_pct": 0.0,
            "leverage": position.leverage,
            "notional_usd": str(notional),
        }
        payload_json = json.dumps(
            verdict_to_contract(verdict, requested_payload=requested_payload),
            sort_keys=True,
        )
        # The risk_verdicts parent trigger is the position's real source trigger.
        assert position.source_trigger_id is not None
        self._store.save_risk_verdict(
            RiskVerdictRecord(
                risk_verdict_id=risk_verdict_id,
                trigger_id=position.source_trigger_id,
                decision_id=decision_id,
                verdict=Verdict.ALLOW.value,
                binding_cap=BindingCap.NONE.value,
                computed_at=now,
                expires_at=expires_at,
                payload_json=payload_json,
                spread_bps=None,
                expected_slippage_bps=None,
                daily_loss_usd=None,
            )
        )
        record_audit(
            self._store,
            event_type="RISK_VERDICT",
            actor={"kind": "SYSTEM", "id": "risk_gate"},
            subject={"kind": "RISK_VERDICT", "id": risk_verdict_id},
            payload={
                "verdict": Verdict.ALLOW.value,
                "binding_cap": BindingCap.NONE.value,
                "purpose": purpose,
            },
            at=now,
        )

    # -- replay ----------------------------------------------------------

    def _replay(self, record: PositionRecord, trade: TradeLogRecord) -> PositionMonitorResult:
        order = self._store.get_order(trade.order_id)
        if order is None:
            raise PositionCloseError(
                "DATA_UNAVAILABLE", f"the replayed exit order {trade.order_id} is missing"
            )
        fill = self._store.get_fill_by_order_id(trade.order_id)
        if fill is None:
            raise PositionCloseError(
                "DATA_UNAVAILABLE",
                f"the replayed exit fill for order {trade.order_id} is missing",
            )
        # A corrupt stored payload is never reported as a successful replay with
        # a ``None`` component; it degrades to the typed unavailable error.
        order_payload = self._decode(order.payload_json, subject=f"order {order.order_id}")
        fill_payload = self._decode(fill.payload_json, subject=f"fill {fill.fill_id}")
        trade_payload = self._decode(trade.payload_json, subject=f"trade log {trade.trade_id}")
        position_payload = self._decode(
            record.payload_json, subject=f"position {record.position_id}"
        )
        return PositionMonitorResult(
            position_id=record.position_id,
            status=CLOSED,
            reason_code=STOP_TOUCH,
            reason="the position was already closed; the original result was replayed",
            action="FULL_CLOSE",
            order=order_payload,
            fill=fill_payload,
            position=position_payload,
            trade_log=trade_payload,
            account=self._paper.summary(),
            idempotent_replay=True,
        )


__all__ = [
    "CLOSED",
    "DATA_STALE",
    "ManagementMark",
    "NO_ACTION",
    "STOP_NOT_TOUCHED",
    "STOP_TOUCH",
    "PositionCloseError",
    "PositionMonitorResult",
    "PositionNotFound",
    "PositionService",
]
