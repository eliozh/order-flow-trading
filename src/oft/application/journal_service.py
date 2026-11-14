"""Journal read models: list/show, statistics, signal-vs-outcome, and export.

The service performs the I/O (trade logs, opportunity/trigger/report snapshots,
recorded bars, the persisted entry decision) and delegates every formula to
:mod:`oft.domain.journal`. Money is never SQL-aggregated: the store returns the
persisted trade-log rows and the domain sums exact ``Decimal`` values. The same
module renders the CSV/JSON export, and the CSV path neutralizes spreadsheet
formula prefixes before writing. No provider is called and no execution state is
mutated here.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from oft.application import recorded_bars
from oft.application.payloads import decode_payload_json
from oft.config import Settings
from oft.domain.journal import (
    JournalError,
    compute_stats,
    csv_document,
    decimal_text,
    decision_vs_outcome,
    resolve_period,
    trigger_to_fill_bps,
    zone_vs_fill,
)
from oft.domain.pnl import first_touch, json_decimal
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import StorePort

_JSON_SCHEMA_VERSION = "1.0.0"
_EXPORT_FORMATS = frozenset({"csv", "json"})
_EXPORT_FILE_MODE = 0o600
_QUALITY_RANK = {"COMPLETE": 0, "PARTIAL": 1, "MISSING": 2}
_RESOLVED_STATUSES = frozenset({"OK", "INSIDE", "ABOVE_ZONE", "BELOW_ZONE"})
_STOP_UNREADABLE = "the position stop price is unreadable"
_TARGETS_UNREADABLE = "the position target prices are unreadable"
_TARGETS_PARTIAL = "the position has unreadable target prices; the first parseable target was used"
_LOGGER = logging.getLogger(__name__)
_PayloadRows = list[tuple[Any, dict[str, Any] | None]]


class JournalNotFound(JournalError):
    """The requested closed trade does not exist."""

    def __init__(self, trade_id: str) -> None:
        super().__init__("NOT_FOUND", f"Trade {trade_id} does not exist.")
        self.trade_id = trade_id


def _snapshot_price(snapshot: Mapping[str, Any]) -> tuple[object, object]:
    value = snapshot.get("last_price")
    if value is None:
        value = snapshot.get("mark_price")
    return value, snapshot.get("as_of")


class JournalService:
    def __init__(self, store: StorePort, clock: ClockPort, settings: Settings) -> None:
        self._store = store
        self._clock = clock
        self._settings = settings

    # -- reads -----------------------------------------------------------

    def list(self, symbol: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        resolved = symbol.upper() if symbol else None
        items: list[dict[str, Any]] = []
        for record in self._store.list_trade_logs(symbol=resolved, limit=limit, offset=offset):
            payload = decode_payload_json(
                record.payload_json, subject=f"trade log {record.trade_id}"
            )
            if payload is not None:
                items.append(payload)
        return {"items": items, "total": self._store.count_trade_logs(symbol=resolved)}

    def show(self, trade_id: str) -> dict[str, Any]:
        record = self._store.get_trade_log(trade_id)
        if record is None:
            raise JournalNotFound(trade_id)
        payload = decode_payload_json(record.payload_json, subject=f"trade log {trade_id}")
        if payload is None:
            raise JournalError("DATA_UNAVAILABLE", f"Trade {trade_id} payload is unreadable.")
        return payload

    # -- statistics ------------------------------------------------------

    def stats(self, period: str | None = None, symbol: str | None = None) -> dict[str, Any]:
        now = self._now()
        window = resolve_period(period, now)
        resolved = symbol.upper() if symbol else None
        payloads = [payload for _record, payload in self._all_payloads(resolved)]
        return compute_stats(
            payloads,
            open_positions=self._store.count_positions(state="OPEN", symbol=resolved),
            window=window,
        ).to_contract()

    # -- signal vs outcome ----------------------------------------------

    def compare(self, trade_id: str) -> dict[str, Any]:
        trade = self.show(trade_id)
        signal = trade.get("signal") or {}
        side = str(trade.get("side") or "")
        entry = trade.get("entry_avg_price")
        position = self._position_for(trade)
        opportunity = self._opportunity_at(signal.get("opportunity_id"))
        trigger = self._trigger_at(signal.get("trigger_id"))
        report_snapshot = self._source_snapshot(opportunity)

        zone = self._zone(entry, opportunity)
        slippage = self._slippage(side, entry, report_snapshot)
        touch = self._first_touch(trade, position, side)
        decision = self._decision(trigger, side, trade.get("net_pnl_usd"))

        sides = (zone, slippage, touch, decision)
        if all(side_result["status"] in _RESOLVED_STATUSES for side_result in sides):
            data_quality = "COMPLETE"
        elif all(side_result["status"] == "N/A" for side_result in sides):
            data_quality = "MISSING"
        else:
            data_quality = "PARTIAL"
        reasons: list[str] = []
        for result in sides:
            reasons.extend(str(reason) for reason in result.get("reasons") or [])
        return {
            "trade_id": trade.get("trade_id", trade_id),
            "position_id": trade.get("position_id"),
            "symbol": trade.get("symbol"),
            "side": trade.get("side"),
            "opened_at": trade.get("opened_at"),
            "closed_at": trade.get("closed_at"),
            "net_pnl_usd": trade.get("net_pnl_usd"),
            "zone": zone,
            "slippage": slippage,
            "first_touch": touch,
            "decision": decision,
            "reasons": reasons,
            "data_quality": data_quality,
        }

    def _position_for(self, trade: Mapping[str, Any]) -> dict[str, Any] | None:
        position_id = str(trade.get("position_id") or "")
        if not position_id:
            return None
        record = self._store.get_position(position_id)
        if record is None:
            return None
        return decode_payload_json(record.payload_json, subject=f"position {position_id}")

    def _opportunity_at(self, opportunity_id: object) -> dict[str, Any] | None:
        if not isinstance(opportunity_id, str) or not opportunity_id:
            return None
        record = self._store.get_opportunity(opportunity_id)
        if record is None:
            return None
        return decode_payload_json(record.payload_json, subject=f"opportunity {opportunity_id}")

    def _trigger_at(self, trigger_id: object) -> dict[str, Any] | None:
        if not isinstance(trigger_id, str) or not trigger_id:
            return None
        record = self._store.get_trigger_event(trigger_id)
        if record is None:
            return None
        return decode_payload_json(record.payload_json, subject=f"trigger {trigger_id}")

    def _source_snapshot(self, opportunity: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if opportunity is None:
            return None
        source = opportunity.get("source") or {}
        if not isinstance(source, Mapping):
            return None
        report_id = source.get("report_id")
        if not isinstance(report_id, str) or not report_id:
            return None
        report = self._store.get_report(report_id)
        if report is None:
            return None
        try:
            snapshot = json.loads(report.snapshot_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return snapshot if isinstance(snapshot, dict) else None

    def _zone(self, entry: object, opportunity: Mapping[str, Any] | None) -> dict[str, Any]:
        if opportunity is None:
            result = zone_vs_fill(entry, None, None)
            result["reasons"] = [
                "the source opportunity was not found; the proposed zone is unavailable"
            ]
            return result
        zone = opportunity.get("zone") or {}
        if not isinstance(zone, Mapping):
            zone = {}
        return zone_vs_fill(entry, zone.get("low"), zone.get("high"))

    def _slippage(
        self,
        side: str,
        entry: object,
        snapshot: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if snapshot is None:
            return {
                "status": "N/A",
                "entry_avg_price": decimal_text(entry),
                "reference_price": None,
                "reference_as_of": None,
                "bps": None,
                "formula": _SLIPPAGE_FORMULA,
                "reasons": ["the trigger-time source report snapshot is unavailable"],
            }
        reference, as_of = _snapshot_price(snapshot)
        bps = trigger_to_fill_bps(side, entry, reference)
        rendered_as_of = _iso_or_none(as_of)
        if bps is None:
            return {
                "status": "N/A",
                "entry_avg_price": decimal_text(entry),
                "reference_price": decimal_text(reference),
                "reference_as_of": rendered_as_of,
                "bps": None,
                "formula": _SLIPPAGE_FORMULA,
                "reasons": [
                    "the source snapshot has no usable reference price for the slippage measure"
                ],
            }
        return {
            "status": "OK",
            "entry_avg_price": decimal_text(entry),
            "reference_price": decimal_text(reference),
            "reference_as_of": rendered_as_of,
            "bps": bps,
            "formula": _SLIPPAGE_FORMULA,
            "reasons": [],
        }

    def _first_touch(
        self,
        trade: Mapping[str, Any],
        position: Mapping[str, Any] | None,
        side: str,
    ) -> dict[str, Any]:
        if position is None:
            return _touch_unavailable("the closed position payload is unavailable")
        stop_raw = position.get("stop_price")
        stop = json_decimal(stop_raw)
        stop_unreadable = stop_raw is not None and stop is None
        targets = position.get("target_prices")
        target: Decimal | None = None
        targets_unreadable = False
        if isinstance(targets, list):
            for value in targets:
                parsed = json_decimal(value)
                if parsed is not None:
                    if target is None:
                        target = parsed
                elif value is not None:
                    targets_unreadable = True
        elif targets is not None:
            targets_unreadable = True
        reasons: list[str] = []
        if stop_unreadable:
            reasons.append(_STOP_UNREADABLE)
        if targets_unreadable:
            reasons.append(_TARGETS_UNREADABLE if target is None else _TARGETS_PARTIAL)
        if stop is None and target is None:
            # Distinguish "absent" from "present but unreadable": a corrupt value
            # must never be reported as a missing one.
            return {
                "kind": None,
                "at": None,
                "ambiguous": False,
                "status": "N/A",
                "reasons": reasons or ["the position records neither a stop nor a target"],
            }
        bars = self._window_bars(
            str(trade.get("symbol") or ""),
            trade.get("opened_at"),
            trade.get("closed_at"),
        )
        if bars.unavailable:
            return _touch_unavailable("recorded bars are unavailable")
        if not bars:
            return _touch_unavailable("no recorded bars cover the holding window")
        try:
            touch = first_touch(side, bars.rows, stop_price=stop, target_price=target)
        except ValueError as exc:
            return {
                "kind": None,
                "at": None,
                "ambiguous": False,
                "status": "N/A",
                "reasons": [f"first-touch evaluation is unavailable: {exc}"],
            }
        status = "PARTIAL" if reasons else "OK"
        if touch.ambiguous:
            status = "PARTIAL"
            reasons.append(
                "AMBIGUOUS_BAR: one recorded bar contains both the stop and the target; "
                "the stop is assumed first"
            )
        return {
            "kind": touch.kind,
            "at": touch.at,
            "ambiguous": touch.ambiguous,
            "status": status,
            "reasons": reasons,
        }

    def _decision(
        self,
        trigger: Mapping[str, Any] | None,
        side: str,
        net_pnl: object,
    ) -> dict[str, Any]:
        if trigger is None:
            result = decision_vs_outcome(None, side, net_pnl)
            result["reasons"] = [
                "the source trigger was not found; the entry decision is unavailable"
            ]
            return result
        decision_id = trigger.get("entry_decision_id")
        if not isinstance(decision_id, str) or not decision_id:
            return decision_vs_outcome(None, side, net_pnl)
        record = self._store.get_decision(decision_id)
        if record is None:
            result = decision_vs_outcome(None, side, net_pnl)
            result["reasons"] = [f"the persisted entry decision {decision_id} was not found"]
            return result
        envelope = decode_payload_json(record.payload_json, subject=f"entry decision {decision_id}")
        if envelope is None:
            result = decision_vs_outcome(None, side, net_pnl)
            result["reasons"] = [
                f"the persisted entry decision {decision_id} payload is unreadable"
            ]
            return result
        entry_recheck = envelope.get("entry_recheck") or {}
        value = entry_recheck.get("decision") if isinstance(entry_recheck, Mapping) else None
        result = decision_vs_outcome(value, side, net_pnl)
        result["decision_id"] = decision_id
        return result

    # -- recorded bars ---------------------------------------------------

    def _window_bars(self, symbol: str, start: object, end: object) -> recorded_bars.WindowBars:
        """The recorded bars inside the holding window, with failure state."""
        return recorded_bars.window_bars(self._settings, self._store, symbol, start, end)

    # -- export ----------------------------------------------------------

    def export(
        self,
        fmt: str = "csv",
        period: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        resolved_format = str(fmt).strip().lower()
        if resolved_format not in _EXPORT_FORMATS:
            raise JournalError(
                "VALIDATION_FAILED", f"unknown export format {fmt!r}; use csv or json"
            )
        now = self._now()
        window = resolve_period(period, now)
        resolved_symbol = symbol.upper() if symbol else None
        all_payloads: list[dict[str, Any] | None] = []
        export_trades: list[dict[str, Any]] = []
        skipped = 0
        start_dt = recorded_bars.parse_timestamp(window.start) if window is not None else None
        end_dt = recorded_bars.parse_timestamp(window.end) if window is not None else None
        for record, payload in self._all_payloads(resolved_symbol):
            all_payloads.append(payload)
            if payload is None:
                skipped += 1
                continue
            closed_at = recorded_bars.parse_timestamp(payload.get("closed_at"))
            if closed_at is None:
                # A readable row with an unparseable closed_at can never be
                # placed in (or out of) the period; count and log it instead of
                # silently dropping it. ``stats`` counts the same row as
                # unreadable, so the two counters agree.
                skipped += 1
                _LOGGER.warning(
                    "journal export skipped trade %s: closed_at is unreadable",
                    record.trade_id,
                )
                continue
            if window is not None:
                if start_dt is None or end_dt is None:
                    continue
                if not (start_dt <= closed_at <= end_dt):
                    continue
            export_trades.append(payload)
        stats = compute_stats(
            all_payloads,
            open_positions=self._store.count_positions(state="OPEN", symbol=resolved_symbol),
            window=window,
        )
        stats_contract = stats.to_contract()
        exports_dir = self._settings.data_dir / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        path = exports_dir / f"journal-{stamp}.{resolved_format}"
        neutralized_cells = 0
        if resolved_format == "csv":
            rendered = csv_document(export_trades)
            text = rendered.text
            neutralized_cells = rendered.neutralized_cells
        else:
            text = json.dumps(
                {
                    "schema_version": _JSON_SCHEMA_VERSION,
                    "generated_at": utc_timestamp(now),
                    "period": None if window is None else window.to_contract(),
                    "trades": export_trades,
                    "stats": stats_contract,
                },
                sort_keys=True,
            )
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _EXPORT_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        return {
            "format": resolved_format,
            "path": str(path),
            "rows": len(export_trades),
            "bytes": len(text.encode("utf-8")),
            "neutralized_cells": neutralized_cells,
            "skipped_unreadable": skipped,
            "data_quality": _worst_quality(
                "PARTIAL" if skipped else "COMPLETE",
                str(stats.data_quality),
            ),
        }

    # -- helpers ---------------------------------------------------------

    def _all_payloads(self, symbol: str | None) -> _PayloadRows:
        """Every trade-log row with its decoded payload, or ``None`` if unreadable."""
        records = self._store.list_trade_logs(symbol=symbol, limit=None)
        return [
            (
                record,
                decode_payload_json(record.payload_json, subject=f"trade log {record.trade_id}"),
            )
            for record in records
        ]

    def _now(self) -> datetime:
        now = self._clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(UTC)


def _touch_unavailable(reason: str) -> dict[str, Any]:
    return {
        "kind": None,
        "at": None,
        "ambiguous": False,
        "status": "N/A",
        "reasons": [reason],
    }


def _worst_quality(*values: str) -> str:
    """The least trustworthy of the supplied quality labels."""
    return max(values, key=lambda value: _QUALITY_RANK.get(value, 2))


def _iso_or_none(value: object) -> str | None:
    """Render a persisted timestamp as an ISO-8601 UTC string, or ``None``."""
    parsed = recorded_bars.parse_timestamp(value)
    if parsed is None:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_SLIPPAGE_FORMULA = (
    "trigger_to_fill_bps = (entry - reference)/reference*10000 for LONG and "
    "(reference - entry)/reference*10000 for SHORT; positive = adverse"
)


__all__ = ["JournalNotFound", "JournalService"]
