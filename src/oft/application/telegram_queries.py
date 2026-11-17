"""Read-only Telegram query builders over the shared application read models.

Each builder constructs the same service the Web/CLI uses, reads, and returns a
rendered plain-text reply. Nothing here calls the Telegram port, resolves an LLM
provider, writes a row, transitions an opportunity, records a budget call, or
changes thresholds. A typed read failure becomes the honest error copy with a
reference id instead of a traceback.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC
from typing import Any

import structlog

from oft.application import telegram_messages as messages
from oft.application.budget_read_model import (
    BUDGET_READ_ERRORS,
    BudgetConfigError,
    BudgetThresholds,
)
from oft.application.budget_service import BudgetService
from oft.application.opportunity_service import (
    Opportunity,
    OpportunityContractError,
    OpportunityService,
    opportunity_summary,
)
from oft.application.position_service import PositionCloseError, PositionService
from oft.application.scan_service import report_payload
from oft.application.symbol_registry import SymbolRegistry
from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import ReportRecord, StorePort, StoreUnavailableError

LOGGER = structlog.get_logger(__name__)

POSITION_LIMIT = 100
OPPORTUNITY_LIMIT = 200

# Curated typed read/model failures. ``BUDGET_READ_ERRORS`` carries the exact
# degraded-ledger set the budget route uses (sqlite3/OSError/InvalidOperation);
# ``ValueError`` also covers a stored report whose JSON cannot be decoded.
# Programming errors (``TypeError``/``KeyError``/``IndexError``/…) are *not*
# expected failures: they reach the builder's broad liveness guard below, which
# logs the cause with a traceback and still returns the typed copy.
QUERY_READ_ERRORS: tuple[type[Exception], ...] = (
    StoreUnavailableError,
    PositionCloseError,
    OpportunityContractError,
    BudgetConfigError,
    *BUDGET_READ_ERRORS,
    ValueError,
)
MAX_LOGGED_ERROR_CHARS = 200


def render_positions_query(
    *,
    store: StorePort,
    clock: ClockPort,
    settings: Settings,
    reference_id: str,
) -> str:
    """Render the OPEN positions from ``PositionService.list(state="OPEN")``."""
    try:
        service = PositionService(store=store, clock=clock, settings=settings)
        items = service.list(state="OPEN", limit=POSITION_LIMIT)
        return messages.render_positions(
            items,
            now=_now_text(clock),
            stale_seconds=int(settings.risk_stale_mark_seconds),
        )
    except QUERY_READ_ERRORS as exc:
        return _unavailable(exc, reference_id)
    except Exception as exc:  # noqa: BLE001 - poison-message liveness guard
        return _unexpected(exc, reference_id)


def render_opportunities_query(*, store: StorePort, clock: ClockPort, reference_id: str) -> str:
    """Render OPEN + TRIGGERED opportunities newest first; no transition."""
    try:
        service = OpportunityService(store=store, clock=clock)
        items: Sequence[Opportunity] = (
            *service.list(state="OPEN", limit=OPPORTUNITY_LIMIT),
            *service.list(state="TRIGGERED", limit=OPPORTUNITY_LIMIT),
        )
        ordered = sorted(
            items,
            key=lambda item: (item.created_at, item.opportunity_id),
            reverse=True,
        )
        rows = [opportunity_summary(item) for item in ordered]
        return messages.render_opportunities(rows, now=_now_text(clock))
    except QUERY_READ_ERRORS as exc:
        return _unavailable(exc, reference_id)
    except Exception as exc:  # noqa: BLE001 - poison-message liveness guard
        return _unexpected(exc, reference_id)


def render_report_query(*, store: StorePort, symbol: str | None, reference_id: str) -> str:
    """Render the latest stored report for ``symbol`` (or the only symbol).

    With no symbol, the latest overall report is used only when every stored
    report belongs to one symbol (``count_reports == count_reports_for_symbol``);
    otherwise a mixed report history has no unambiguous "latest," so the exact
    empty copy is returned. The stored narrative is rendered as untrusted text;
    no provider is resolved and no analysis is re-run.
    """
    try:
        record = _latest_report(store, symbol)
        if record is None:
            return messages.REPORT_EMPTY_TEXT
        return messages.render_report(report_payload(record))
    except QUERY_READ_ERRORS as exc:
        return _unavailable(exc, reference_id)
    except Exception as exc:  # noqa: BLE001 - poison-message liveness guard
        return _unexpected(exc, reference_id)


def render_budget_query(
    *,
    store: StorePort,
    clock: ClockPort,
    settings: Settings,
    reference_id: str,
) -> str:
    """Render thresholds plus day/week/month spend; never write or re-threshold.

    The whole builder and render sit inside one guard, so a constructor or
    renderer defect cannot escape ``handle_update`` and strand the poller
    offset on the same poison update.
    """
    try:
        service = BudgetService(store=store, settings=settings)
        at = _now_text(clock)
        thresholds = service.thresholds()
        try:
            snapshot = service.snapshot(at, thresholds=thresholds)
        except QUERY_READ_ERRORS as exc:
            # A degraded ledger is a state, not a query failure: keep the
            # thresholds and label usage UNAVAILABLE.
            _log_failure("telegram_budget_usage_unavailable", exc, exc_info=False)
            snapshot = _degraded_budget(thresholds)
        return messages.render_budget(snapshot)
    except QUERY_READ_ERRORS as exc:
        return _unavailable(exc, reference_id)
    except Exception as exc:  # noqa: BLE001 - poison-message liveness guard
        return _unexpected(exc, reference_id)


def render_watchlist_query(*, store: StorePort, clock: ClockPort, reference_id: str) -> str:
    """Render starred symbols grouped by CRYPTO/TRADFI from the registry."""
    try:
        registry = SymbolRegistry(store, clock=clock)
        contracts = registry.list_symbols(starred=True)
        return messages.render_watchlist(contracts)
    except QUERY_READ_ERRORS as exc:
        return _unavailable(exc, reference_id)
    except Exception as exc:  # noqa: BLE001 - poison-message liveness guard
        return _unexpected(exc, reference_id)


def render_invalid_argument_error(reference_id: str) -> str:
    """The fixed invalid-argument copy (never echoes the rejected argument)."""
    return messages.render_query_error(messages.QUERY_ERROR_INVALID_ARGUMENT, reference_id)


def _latest_report(store: StorePort, symbol: str | None) -> ReportRecord | None:
    if symbol is not None:
        page = store.list_reports_for_symbol_page(symbol, limit=1)
        return page[0] if page else None
    latest = store.list_reports(limit=1)
    if not latest:
        return None
    candidate = latest[0]
    total = store.count_reports()
    if total > 0 and total == store.count_reports_for_symbol(candidate.symbol):
        return candidate
    return None


def _degraded_budget(thresholds: BudgetThresholds) -> dict[str, Any]:
    """The same degraded shape ``GET /api/v1/budget`` uses: usage is not zero."""
    return {
        "thresholds": thresholds.to_contract(),
        "usage_quality": "UNAVAILABLE",
        "budget_state": None,
        "periods": None,
        "projected_month_end_usd": None,
        "projection_basis": None,
        "last_alert": None,
    }


def _now_text(clock: ClockPort) -> str:
    now = clock.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return utc_timestamp(now.astimezone(UTC))


def _unavailable(exc: Exception, reference_id: str) -> str:
    # The chat text stays generic; server-side logging names the bounded cause.
    _log_failure("telegram_query_failed", exc, exc_info=False)
    return messages.render_query_error(messages.QUERY_ERROR_UNAVAILABLE, reference_id)


def _unexpected(exc: Exception, reference_id: str) -> str:
    # A defect outside the expected read/model set still returns the typed copy
    # so the poller can advance, but the traceback is retained server-side.
    _log_failure("telegram_query_unexpected_failure", exc, exc_info=True)
    return messages.render_query_error(messages.QUERY_ERROR_UNAVAILABLE, reference_id)


def _log_failure(event: str, exc: Exception, *, exc_info: bool) -> None:
    # Only a bounded message and the type: the raw query argument and any
    # secret never enter the log record through this path.
    LOGGER.warning(
        event,
        error_type=type(exc).__name__,
        error_message=str(exc)[:MAX_LOGGED_ERROR_CHARS],
        exc_info=exc_info,
    )


__all__ = [
    "MAX_LOGGED_ERROR_CHARS",
    "OPPORTUNITY_LIMIT",
    "POSITION_LIMIT",
    "QUERY_READ_ERRORS",
    "render_budget_query",
    "render_invalid_argument_error",
    "render_opportunities_query",
    "render_positions_query",
    "render_report_query",
    "render_watchlist_query",
]
