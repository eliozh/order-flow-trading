"""Position management: read models, mark lookup, stop touch, and full close.

The service owns the one deterministic management path in this slice: it
resolves the position's latest recorded mark from published bars (falling back
to the mark persisted on the position), refuses to act on a missing or stale
mark, applies the code-owned stop-touch rule, persists a management decision and
an exit risk verdict, submits one reduce-only ``FULL_CLOSE`` through the
execution gateway, and surfaces the single trade-log row the broker committed.
No route or CLI calls the broker directly.

Mark lookup prefers the latest recorded bar close for the position's symbol and
uses the bar's ``bar_end`` as the mark's as-of. When no bar series or usable row
exists, the position payload's recorded ``mark_price``/``updated_at`` is the
fallback. The newest recorded observation wins; the stale rule is applied
unchanged to the chosen as-of, and a bar whose ``bar_end`` is in the future is
never selected. A bar-derived mark is written to the position row immediately
before a close so the gateway's independent re-read sees the same recorded mark.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from oft.adapters.parquet_store import read_bars
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
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, Position, PositionSide, deterministic_id
from oft.domain.pnl import (
    MAX_ABS_FUNDING_RATE,
    MAX_ABS_PRICE,
    MAX_ABS_QUANTITY,
    ZERO,
    first_touch,
    funding_amount_usd,
    mae_mfe_from_sized_bars,
    plain_money_str,
    quantize_money,
    weighted_average_entry,
)
from oft.domain.positions import (
    ADD,
    PARTIAL_CLOSE,
    STOP_TOUCH_RULE,
    PositionError,
    TradeLogContext,
    evaluate_protective_exit,
    management_decision_id,
    position_from_contract,
)
from oft.domain.risk import (
    RISK_VERDICT_TTL_SECONDS,
    BindingCap,
    RiskVerdict,
    Verdict,
    verdict_to_contract,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import (
    DecisionRecord,
    FundingAccrualCommit,
    FundingAccrualRecord,
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
CLOSED = "CLOSED"
NO_ACTION = "NO_ACTION"

_MARK_SOURCE_BARS = "BARS"
_MARK_SOURCE_POSITION = "POSITION"
_BARS_STATUSES = frozenset({"PUBLISHED", "COMPLETE"})
_PROVIDER_VALUES = frozenset({"openai", "anthropic", "deepseek", "stub"})
_PERIOD_TOKEN = re.compile(r"(?:^|/)period=([^/]+)(?:/|$)")
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
        }


@dataclass(frozen=True)
class _MarkObservation:
    """One recorded mark and the timestamp it was observed at."""

    mark: Decimal
    as_of: str
    source: str


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


def _decimal_or_zero(value: object) -> Decimal:
    parsed = _decimal_or_none(value)
    return parsed if parsed is not None else Decimal("0")


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except ArithmeticError:
        return None
    return parsed if parsed.is_finite() else None


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


def _period_from_path(path: str) -> str | None:
    match = _PERIOD_TOKEN.search(path)
    return match.group(1) if match is not None else None


def _observation_rank(observation: _MarkObservation) -> tuple[bool, datetime, int]:
    """Newest first; a parseable as-of beats an unparseable one, bar on a tie."""
    parsed = _parse_timestamp(observation.as_of)
    return (
        parsed is not None,
        parsed or datetime.min.replace(tzinfo=UTC),
        1 if observation.source == _MARK_SOURCE_BARS else 0,
    )


class PositionService:
    def __init__(self, *, store: StorePort, clock: ClockPort, settings: Settings) -> None:
        self._store = store
        self._clock = clock
        self._settings = settings
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
        self._tick_cache: dict[tuple[str, str], tuple[dict[str, object], ...]] | None = None

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
        for record in self._store.list_positions(state=state, limit=limit, offset=offset):
            payload = decode_payload_json(
                record.payload_json, subject=f"position {record.position_id}"
            )
            if payload is not None:
                items.append(payload)
        return tuple(items)

    def count(self, *, state: str | None = None) -> int:
        return self._store.count_positions(state=state)

    def show(self, position_id: str) -> dict[str, Any]:
        record = self._store.get_position(position_id)
        if record is None:
            raise PositionNotFound(position_id)
        return self._decode(record.payload_json, subject=f"position {position_id}")

    def history(self, position_id: str) -> dict[str, Any]:
        """The position's accounting timeline: events, funding, and PnL terms."""
        record = self._store.get_position(position_id)
        if record is None:
            raise PositionNotFound(position_id)
        payload = self._decode(record.payload_json, subject=f"position {position_id}")
        events = self._store.list_position_events(position_id)
        accruals = self._store.list_funding_accruals(position_id)
        trade = self._store.get_trade_log_for_position(position_id)
        pnl: dict[str, Any]
        trade_payload: dict[str, Any] | None = None
        if trade is not None:
            trade_payload = self._decode(trade.payload_json, subject=f"trade log {trade.trade_id}")
        if trade_payload is not None:
            pnl = {
                "gross_pnl_usd": trade_payload.get("gross_pnl_usd"),
                "fees_usd": trade_payload.get("fees_usd"),
                "funding_usd": trade_payload.get("funding_usd"),
                "net_pnl_usd": trade_payload.get("net_pnl_usd"),
                "r_multiple": trade_payload.get("r_multiple"),
                "return_pct": trade_payload.get("return_pct"),
                "mae_usd": trade_payload.get("mae_usd"),
                "mfe_usd": trade_payload.get("mfe_usd"),
                "outcome_tags": trade_payload.get("outcome_tags"),
                "data_quality": trade_payload.get("data_quality"),
            }
        else:
            pnl = {
                "gross_pnl_usd": plain_money_str(_decimal_or_zero(payload.get("realized_pnl_usd"))),
                "fees_usd": payload.get("fees_usd"),
                "funding_usd": payload.get("funding_usd"),
                "net_pnl_usd": None,
                "r_multiple": None,
                "return_pct": None,
                "mae_usd": None,
                "mfe_usd": None,
                "outcome_tags": [],
                "data_quality": payload.get("data_quality"),
            }
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
            "funding": [
                {
                    "accrual_id": accrual.accrual_id,
                    "settlement_time": accrual.settlement_time,
                    "rate": accrual.rate,
                    "mark_price": accrual.mark_price,
                    "amount_usd": accrual.amount_usd,
                    "created_at": accrual.created_at,
                }
                for accrual in accruals
            ],
            "pnl": pnl,
        }

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
            commit = FundingAccrualCommit(
                accrual=FundingAccrualRecord(
                    accrual_id=deterministic_id("FND", f"{position_id}:{point.ts}"),
                    position_id=position_id,
                    settlement_time=point.ts,
                    rate=str(point.rate),
                    mark_price=str(mark),
                    amount_usd=str(amount),
                    created_at=now_value,
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
                balance_delta=str(amount),
                event=PositionEventRecord(
                    position_id=position_id,
                    seq=None,
                    at=point.ts,
                    action="FUNDING",
                    order_id=None,
                    note=f"funding {point.rate}",
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
        cache: dict[tuple[str, str], tuple[dict[str, object], ...]] | None,
    ) -> tuple[dict[str, object], ...]:
        """Read one logical bars series, reusing it within one operation."""
        key = (period, metric_version)
        if cache is not None and key in cache:
            return cache[key]
        try:
            rows = read_bars(
                self._settings,
                symbol=position.symbol,
                period=period,
                metric_version=metric_version,
                store=self._store,
            )
        except (StoreUnavailableError, OSError, ValueError) as exc:
            _LOGGER.warning("cannot read bars for %s: %s", position.symbol, exc)
            rows = ()
        if cache is not None:
            cache[key] = rows
        return rows

    def _window_bars(
        self,
        position: Position,
        start: str,
        end: str,
        *,
        series: set[tuple[str, str]] | None = None,
        cache: dict[tuple[str, str], tuple[dict[str, object], ...]] | None = None,
    ) -> tuple[dict[str, object], ...]:
        """The recorded bars whose ``bar_end`` falls inside the holding window."""
        start_dt = _parse_timestamp(start)
        end_dt = _parse_timestamp(end)
        if start_dt is None or end_dt is None:
            return ()
        series = self._bar_series(position) if series is None else series
        best: tuple[dict[str, object], ...] = ()
        best_end: datetime | None = None
        for period, metric_version in sorted(series):
            rows = self._read_series(position, period, metric_version, cache)
            usable: list[dict[str, object]] = []
            newest: datetime | None = None
            for row in rows:
                bar_end = _parse_timestamp(row.get("bar_end"))
                if bar_end is None or not (start_dt <= bar_end <= end_dt):
                    continue
                usable.append(row)
                if newest is None or bar_end > newest:
                    newest = bar_end
            if newest is not None and (best_end is None or newest > best_end):
                best_end = newest
                best = tuple(usable)
        return best

    def _bar_series(self, position: Position) -> set[tuple[str, str]]:
        try:
            entries = self._store.list_manifest()
        except StoreUnavailableError as exc:
            _LOGGER.warning("cannot read the manifest for %s: %s", position.symbol, exc)
            return set()
        series: set[tuple[str, str]] = set()
        for entry in entries:
            if (
                entry.data_type != "bars"
                or entry.symbol != position.symbol
                or entry.status not in _BARS_STATUSES
            ):
                continue
            period = _period_from_path(entry.path)
            metric_version = entry.metric_version
            if period is None or metric_version is None:
                continue
            series.add((period, metric_version))
        return series

    # -- management tick -------------------------------------------------

    def monitor(self, position_id: str) -> PositionMonitorResult:
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
        cache: dict[tuple[str, str], tuple[dict[str, object], ...]] = {}
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
            return PositionMonitorResult(
                position_id=position_id,
                status=NO_ACTION,
                reason_code=DATA_STALE,
                reason="the position has no recorded mark price",
            )
        if self._is_stale(now, observation.as_of):
            return PositionMonitorResult(
                position_id=position_id,
                status=NO_ACTION,
                reason_code=DATA_STALE,
                reason=(
                    f"the recorded mark is older than {self._stale_seconds}s; no order was created"
                ),
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
        )

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
        cache: dict[tuple[str, str], tuple[dict[str, object], ...]] | None = None,
    ) -> TradeLogContext:
        """Compute the complete journal facts for the upcoming full close."""
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
                mae, mfe = mae_mfe_from_sized_bars(position.side.value, bars, states)
            except (ArithmeticError, ValueError):
                mae = mfe = Decimal("0")
                reasons.append("MAE/MFE not measured: the recorded bars or size are implausible")
        else:
            mae = mfe = Decimal("0")
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
        # the residual exposure a partial close leaves behind.
        entry_notional = recorded_entry_notional_usd(self._store, position.source_trigger_id)
        if entry_notional is None:
            reasons.append("entry notional is unavailable: the recorded entry fill was not found")
            entry_notional = position.notional_usd
            status = "PARTIAL"
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
            close_reason=STOP_TOUCH_RULE,
            stopped=True,
            had_partial_close=timeline.partial_quantity > 0,
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
        cache: dict[tuple[str, str], tuple[dict[str, object], ...]] | None = None,
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
            return self._replay_existing(position, existing, probe)
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
        return self._closed_result(position, result)

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
        side = OrderSide.SELL if position.side is PositionSide.LONG else OrderSide.BUY
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

    def _closed_result(self, position: Position, result: BrokerResult) -> PositionMonitorResult:
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
        )

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
    ) -> None:
        expires_at = utc_timestamp(_parse(now) + timedelta(seconds=RISK_VERDICT_TTL_SECONDS))
        try:
            notional = quantize_money(mark * position.size)
        except (ArithmeticError, ValueError) as exc:
            raise PositionCloseError(
                "DATA_UNAVAILABLE",
                "the recorded mark and position size make the exit notional implausible",
            ) from exc
        verdict = RiskVerdict(
            verdict=Verdict.ALLOW,
            requested_quantity=position.size,
            allowed_quantity=position.size,
            allowed_notional_usd=notional,
            allowed_leverage=position.leverage,
            risk_amount_usd=Decimal("0"),
            binding_cap=BindingCap.NONE,
            reasons=("risk-reducing protective exit",),
            computed_at=now,
            expires_at=expires_at,
            inputs_hash=hashlib.sha256(
                f"{risk_verdict_id}:{mark}:{position.size}".encode()
            ).hexdigest(),
            stale_inputs=(),
            entry_price=mark,
            stop_price=position.stop_price,
        )
        requested_payload = {
            "entry_price": str(mark),
            "stop_price": str(position.stop_price),
            "quantity": str(position.size),
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
                "purpose": "EXIT",
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
    "NO_ACTION",
    "STOP_NOT_TOUCHED",
    "STOP_TOUCH",
    "PositionCloseError",
    "PositionMonitorResult",
    "PositionNotFound",
    "PositionService",
]
