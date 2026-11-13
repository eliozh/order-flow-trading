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
from oft.application.paper_broker import BrokerError, BrokerResult, OrderIntent, PaperBroker
from oft.application.payloads import decode_payload_json
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, Position, PositionSide, deterministic_id
from oft.domain.pnl import quantize_money
from oft.domain.positions import (
    PositionError,
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
    OrderRecord,
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
_PERIOD_TOKEN = re.compile(r"(?:^|/)period=([^/]+)(?:/|$)")


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
        return self._close(position, payload, observation.mark, observation.as_of, now)

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
        try:
            entries = self._store.list_manifest()
        except StoreUnavailableError as exc:
            _LOGGER.warning("cannot read the manifest for %s: %s", position.symbol, exc)
            return None
        # One read per logical series: manifest parts of the same
        # (period, metric_version) are merged by ``read_bars`` itself, so
        # deduping here keeps that merge once per series.
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
                _LOGGER.warning(
                    "skipping bars manifest entry without period/version: %s", entry.path
                )
                continue
            series.add((period, metric_version))
        observations: list[_MarkObservation] = []
        for period, metric_version in sorted(series):
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
                continue
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
            if not close.is_finite() or close <= 0:
                _LOGGER.warning("ignoring a non-finite/non-positive bar close for %s", symbol)
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
        return _MarkObservation(mark, as_of, _MARK_SOURCE_POSITION)

    def _is_stale(self, now: str, mark_as_of: str) -> bool:
        try:
            age = (_parse(now) - _parse(mark_as_of)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            # An unparseable mark timestamp is never treated as fresh.
            return True
        return age > self._stale_seconds

    # -- close -----------------------------------------------------------

    def _close(
        self,
        position: Position,
        payload: dict[str, Any],
        mark: Decimal,
        mark_as_of: str,
        now: str,
    ) -> PositionMonitorResult:
        seed = f"{position.position_id}:{mark}:{mark_as_of}"
        ids = _CloseIds(
            decision_id=management_decision_id(position.position_id, mark, mark_as_of),
            risk_verdict_id=deterministic_id("RISK-MGD", seed),
            approval_id=deterministic_id("APR", seed),
            idempotency_key=deterministic_id("IDEM-CLOSE", seed),
        )
        intent = self._exit_intent(position, ids)
        existing = self._store.get_order_by_idempotency(ids.idempotency_key)
        if existing is not None:
            return self._replay_existing(position, existing, intent)
        if position.source_trigger_id is None:
            raise PositionCloseError(
                "DATA_UNAVAILABLE",
                "the position has no source trigger; the exit verdict cannot be bound",
            )
        if not self._persist_mark(position.position_id, payload, mark, mark_as_of):
            raise PositionCloseError(
                "POSITION_STATE_INVALID",
                "the position is no longer OPEN; no order was created",
            )

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
        mark: Decimal,
        mark_as_of: str,
    ) -> bool:
        """Write the chosen recorded mark to the OPEN row before an exit submit."""
        updated = dict(payload)
        updated["mark_price"] = str(mark)
        updated["updated_at"] = mark_as_of
        updated["version"] = int(updated.get("version", 1)) + 1
        return self._store.update_open_position_payload(
            position_id, payload_json=json.dumps(updated, sort_keys=True)
        )

    def _exit_intent(self, position: Position, ids: _CloseIds) -> OrderIntent:
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
        notional = quantize_money(mark * position.size)
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
