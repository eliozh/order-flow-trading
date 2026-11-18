"""Health, readiness, doctor checks, and daemon probing.

Documented maintainability exception: this module is the single diagnostics
surface shared by `/ready`, `/api/v1/status`, `oft doctor`, and
`oft system info`. The Telegram observability projection lives here so one
implementation feeds every surface (and the file sits just over the 800-line
soft ceiling); extracting it would require splitting the readiness/doctor
contracts and their tests for no behavioral gain.
"""

from __future__ import annotations

import json
import shutil
import socket
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from oft import __version__
from oft.config import SECRET_FIELDS, Settings
from oft.domain.layout import PARTITION_ROOTS
from oft.domain.telegram import (
    POLLER_HEARTBEAT_KEY,
    POLLER_LAST_ERROR_KEY,
    POLLER_STALE_SECONDS,
    POLLER_STATE_CONFLICT,
    POLLER_STATE_DEGRADED,
    POLLER_STATE_KEY,
    POLLER_STATE_UNKNOWN,
    TELEGRAM_ALLOWED_USER_IDS_KEY,
    TELEGRAM_CHAT_IDS_KEY,
    TELEGRAM_LAST_SEND_AT_KEY,
    TELEGRAM_OUTBOX_OVERFLOW_KEY,
    TelegramValidationError,
    validate_telegram_token,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.market_data import MarketDataPort
from oft.ports.store import StorePort

CheckState = Literal["ok", "degraded", "offline", "unknown"]

MIN_FREE_DISK_BYTES = 1 * 1024**3
DISK_WARN_USED_PCT = 95.0
MANIFEST_SCAN_ROOTS = PARTITION_ROOTS


class HealthReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    version: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckState
    detail: str


@dataclass(frozen=True)
class DaemonProbe:
    state: Literal["UP", "OFFLINE"]
    detail: str


@dataclass(frozen=True)
class ReadinessReport:
    status: Literal["ready", "not_ready"]
    version: str
    env: str
    data_dir: str
    checked_at: str
    checks: tuple[CheckResult, ...]
    latest_scan: dict[str, Any] | None
    telegram: dict[str, Any] | None = None


@dataclass(frozen=True)
class DoctorReport:
    status: Literal["READY", "DEGRADED"]
    version: str
    checked_at: str
    checks: tuple[CheckResult, ...]
    latest_scan: dict[str, Any] | None
    telegram: dict[str, Any] | None = None


def build_health() -> HealthReport:
    return HealthReport(status="ok", version=__version__)


def probe_daemon(settings: Settings, timeout: float = 0.25) -> DaemonProbe:
    """Check whether something listens on the configured loopback address."""
    try:
        with socket.create_connection((settings.host, settings.port), timeout=timeout):
            return DaemonProbe(
                state="UP",
                detail=f"listening on {settings.host}:{settings.port}",
            )
    except OSError as exc:
        return DaemonProbe(
            state="OFFLINE",
            detail=f"no listener on {settings.host}:{settings.port} ({exc.__class__.__name__})",
        )


def _check(name: str, ok: bool, detail: str, failure: CheckState = "degraded") -> CheckResult:
    return CheckResult(name=name, status="ok" if ok else failure, detail=detail)


def build_readiness(
    settings: Settings,
    store: StorePort,
    market_data: MarketDataPort,
    clock: ClockPort,
) -> ReadinessReport:
    checks: list[CheckResult] = [_check("config", True, "settings loaded")]

    sqlite_ok = False
    try:
        sqlite_ok = store.integrity_check()
    except Exception:  # pragma: no cover - defensive boundary
        sqlite_ok = False
    checks.append(
        _check(
            "storage.sqlite",
            sqlite_ok,
            "integrity ok" if sqlite_ok else "database missing or integrity check failed",
        )
    )

    parquet_root = settings.data_dir / "market"
    checks.append(
        _check(
            "storage.parquet",
            parquet_root.is_dir(),
            str(parquet_root) if parquet_root.is_dir() else "market layout not initialized",
        )
    )

    now = clock.now()
    checks.append(_check("clock", now.tzinfo is not None, utc_timestamp(now)))

    universe_ok = False
    universe_detail = "symbol universe unavailable"
    try:
        universe = market_data.load_universe()
        universe_ok = True
        universe_detail = f"{len(universe.symbols)} symbols"
    except Exception as exc:  # pragma: no cover - defensive boundary
        universe_detail = str(exc)
    checks.append(_check("market_data", universe_ok, universe_detail))

    latest = store.latest_scan()
    ready = all(check.status == "ok" for check in checks)
    return ReadinessReport(
        status="ready" if ready else "not_ready",
        version=__version__,
        env=settings.env.value,
        data_dir=str(settings.data_dir),
        checked_at=utc_timestamp(now),
        checks=tuple(checks),
        latest_scan=asdict(latest) if latest is not None else None,
        # Readiness is unauthenticated, so it carries only the bounded state a
        # probe needs: configured, receipt totals, the failing count, and the
        # poller enum. Chat ids and recent event keys stay on authenticated
        # surfaces (`/api/v1/status`, `oft doctor`, `oft system info`).
        telegram=_telegram_readiness(settings, store, clock),
    )


def _telegram_readiness(settings: Settings, store: StorePort, clock: ClockPort) -> dict[str, Any]:
    """The minimal, non-identifying Telegram projection for `/ready`."""
    summary = telegram_observability(settings, store, clock, daemon_state="UP")
    receipts = summary.get("receipts")
    if not isinstance(receipts, dict):
        receipts = {"queued": 0, "sending": 0, "sent": 0, "failed": 0}
    failing = summary.get("failing_receipts")
    return {
        "configured": bool(summary.get("configured")),
        "failing_receipts": failing if isinstance(failing, int) else 0,
        "receipts": receipts,
        "poller_state": summary.get("poller_state") or POLLER_STATE_UNKNOWN,
    }


def _existing_ancestor(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def _disk_check(settings: Settings, *, disk_usage: Any = shutil.disk_usage) -> CheckResult:
    """Report free space without ever reading a file's contents or secrets."""
    try:
        usage = disk_usage(_existing_ancestor(settings.data_dir))
    except OSError as exc:  # pragma: no cover - defensive boundary
        return CheckResult("storage.disk", "degraded", f"disk usage unavailable ({exc})")
    free_gib = usage.free / 1024**3
    used_pct = (usage.used / usage.total * 100) if usage.total else 0.0
    detail = f"{free_gib:.1f} GiB free ({used_pct:.0f}% used)"
    healthy = usage.free >= MIN_FREE_DISK_BYTES and used_pct < DISK_WARN_USED_PCT
    return _check("storage.disk", healthy, detail)


def _manifest_orphans_check(settings: Settings, store: StorePort) -> CheckResult:
    """Find manifest rows with no file and Parquet parts with no manifest row."""
    try:
        entries = store.list_manifest()
    except Exception as exc:  # pragma: no cover - defensive boundary
        return CheckResult("manifest.orphans", "degraded", f"manifest unreadable ({exc})")
    data_root = settings.data_dir.expanduser().resolve()

    def _resolve(path: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = data_root / candidate
        return candidate.resolve()

    known = {str(_resolve(entry.path)) for entry in entries}
    missing = [entry for entry in entries if not _resolve(entry.path).is_file()]
    disk_parts: list[str] = []
    for name in MANIFEST_SCAN_ROOTS:
        root = data_root / name
        if root.is_dir():
            disk_parts.extend(str(path.resolve()) for path in root.rglob("*.parquet"))
    orphans = [path for path in disk_parts if path not in known]
    if missing or orphans:
        return CheckResult(
            "manifest.orphans",
            "degraded",
            f"{len(missing)} missing part(s), {len(orphans)} orphan part(s)",
        )
    return CheckResult(
        "manifest.orphans",
        "ok",
        f"{len(entries)} manifest row(s) consistent",
    )


def _stale_jobs_check(store: StorePort, clock: ClockPort) -> CheckResult:
    """Report every RUNNING job row whose lease already expired (no cap)."""
    now = utc_timestamp(clock.now())
    try:
        stale = store.list_stale_job_runs(now=now)
    except Exception as exc:  # pragma: no cover - defensive boundary
        return CheckResult("jobs.stale", "degraded", f"job ledger unreadable ({exc})")
    if stale:
        return CheckResult(
            "jobs.stale",
            "degraded",
            f"{len(stale)} stale RUNNING job run(s); recover or fail them",
        )
    return CheckResult("jobs.stale", "ok", "no stale RUNNING job runs")


def _database_unavailable(store: StorePort, check: str) -> CheckResult | None:
    """Return an ``unknown`` result when the store cannot confirm its database.

    The store port turns an absent database into empty results, so a check that
    only counts rows would report ``ok`` from nothing. Callers gate on this
    before reading, and a check that cannot verify reports ``unknown`` rather
    than false assurance (M1).
    """
    try:
        available = store.integrity_check()
    except Exception:  # pragma: no cover - defensive boundary
        available = False
    if available:
        return None
    return CheckResult(check, "unknown", f"database unavailable; {check} not verified")


def _management_jobs_check(store: StorePort, clock: ClockPort) -> CheckResult:
    """Report the aggregate result of the latest ``position_manage`` window.

    ``position_manage`` writes one row per ``"{window}#{position_id}"`` key, so
    the newest single row (ordered by heartbeat) is not the window status: a
    FAILED row for one position would be masked by a later DONE row for
    another. The aggregate ``latest_job_summary`` is the same read path the API
    and CLI use. Only the status, boundary window, and error code are rendered;
    a persisted message is never echoed (it may quote provider text).
    """
    from oft.application.jobs import POSITION_MANAGE_JOB_TYPE, latest_job_summary

    unavailable = _database_unavailable(store, "jobs.management")
    if unavailable is not None:
        return unavailable
    try:
        summary = latest_job_summary(store, POSITION_MANAGE_JOB_TYPE)
    except Exception as exc:
        return CheckResult(
            "jobs.management", "degraded", f"management job ledger unreadable ({exc})"
        )
    if summary is None:
        return CheckResult("jobs.management", "ok", "no management job run recorded yet")
    status = summary.status
    # ``latest_job_summary`` surfaces the boundary ISO timestamp, never the
    # internal ``"<ts>#<position_id>"`` ledger key.
    window = summary.window_start
    error_code = summary.error_code or ""
    if status in ("FAILED", "PARTIAL"):
        suffix = f" ({error_code})" if error_code else ""
        return CheckResult(
            "jobs.management",
            "degraded",
            f"last management run {status} for window {window}{suffix}",
        )
    if status == "RUNNING":
        if _window_lease_expired(store, POSITION_MANAGE_JOB_TYPE, window, clock):
            return CheckResult(
                "jobs.management",
                "degraded",
                f"management run for window {window} is RUNNING with an expired lease",
            )
        return CheckResult(
            "jobs.management", "ok", f"management run for window {window} is RUNNING"
        )
    return CheckResult("jobs.management", "ok", f"last management run {status} for window {window}")


def _window_lease_expired(store: StorePort, job_type: str, window: str, clock: ClockPort) -> bool:
    """True when any RUNNING row in the window has an expired/unparseable lease."""
    now = clock.now().astimezone(UTC)
    try:
        runs = store.list_job_runs(job_type)
    except Exception:
        return True
    for run in runs:
        if str(run.window_start).split("#", 1)[0] != window:
            continue
        if str(run.status) != "RUNNING":
            continue
        try:
            lease_expires = datetime.fromisoformat(str(run.lease_expires_at).replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return True
        if lease_expires < now:
            return True
    return False


def _funding_idempotency_check(store: StorePort) -> CheckResult:
    """Verify recorded funding accruals are unique and plausible.

    ``(position_id, settlement_time)`` is the idempotency key, so a duplicate
    settlement time for one position is a real double-accrual risk. Amount,
    mark, and size are re-parsed from the persisted rows; a non-finite or
    implausible value is a degraded read, never a silently accepted zero.

    Only OPEN positions are scanned, so the check is bounded by positions that
    can still accrue funding. Closed-position accruals are immutable history and
    cannot be double-settled; a read-only store aggregate over that history is a
    follow-up (the store port exposes no funding query).
    """
    from decimal import Decimal, InvalidOperation

    from oft.domain.pnl import MAX_ABS_MONEY, MAX_ABS_PRICE, MAX_ABS_QUANTITY

    unavailable = _database_unavailable(store, "funding.idempotency")
    if unavailable is not None:
        return unavailable
    try:
        positions = store.list_positions(state="OPEN", limit=None)
    except Exception as exc:
        return CheckResult("funding.idempotency", "degraded", f"position ledger unreadable ({exc})")
    checked = 0
    duplicates = 0
    invalid = 0
    for position in positions:
        try:
            accruals = store.list_funding_accruals(position.position_id)
        except Exception as exc:
            return CheckResult(
                "funding.idempotency", "degraded", f"funding accruals unreadable ({exc})"
            )
        seen: set[str] = set()
        for accrual in accruals:
            checked += 1
            settlement = str(accrual.settlement_time)
            if settlement in seen:
                duplicates += 1
            else:
                seen.add(settlement)
            try:
                amount = Decimal(str(accrual.amount_usd))
                mark = Decimal(str(accrual.mark_price))
                size = Decimal(str(accrual.size))
            except (InvalidOperation, TypeError, ValueError):
                invalid += 1
                continue
            plausible = (
                amount.is_finite()
                and abs(amount) <= MAX_ABS_MONEY
                and mark.is_finite()
                and 0 < mark <= MAX_ABS_PRICE
                and size.is_finite()
                and 0 <= size <= MAX_ABS_QUANTITY
            )
            if not plausible:
                invalid += 1
    if duplicates or invalid:
        return CheckResult(
            "funding.idempotency",
            "degraded",
            f"{duplicates} duplicate settlement(s), {invalid} invalid amount(s) "
            f"across {checked} funding accrual(s)",
        )
    return CheckResult(
        "funding.idempotency",
        "ok",
        f"{checked} funding accrual(s) unique and plausible",
    )


def _audit_chain_check(store: StorePort) -> CheckResult:
    """Re-derive the append-only hash chain and report the first broken link."""
    from oft.application.kill_switch import verify_audit_chain

    try:
        verification = verify_audit_chain(store)
    except Exception as exc:  # pragma: no cover - defensive boundary
        return CheckResult("audit.chain", "degraded", f"AUDIT_CHAIN_BROKEN: {exc}")
    return CheckResult(
        "audit.chain",
        "ok" if verification.ok else "degraded",
        verification.message,
    )


def _secrets_check(settings: Settings) -> CheckResult:
    """Report how many secrets are configured; never echo a value."""
    configured = [name for name in SECRET_FIELDS if getattr(settings, name, None) is not None]
    if configured:
        return CheckResult(
            "config.secrets",
            "ok",
            f"{len(configured)} secret(s) configured (values not displayed)",
        )
    return CheckResult("config.secrets", "ok", "no secrets configured")


def _provider_check(settings: Settings) -> CheckResult:
    """Report the configured LLM provider without ever printing or logging a key value."""
    from oft.application.llm_orchestrator import provider_configured

    name = (settings.llm_provider or "").strip().lower()
    if not name:
        return CheckResult(
            "config.provider",
            "ok",
            "no LLM provider configured (CALC-only)",
        )
    if name == "stub":
        return CheckResult(
            "config.provider",
            "ok",
            "provider=stub (deterministic fixture provider)",
        )
    if provider_configured(settings, name):
        return CheckResult(
            "config.provider",
            "ok",
            f"provider={name} configured (key present, value not displayed)",
        )
    return CheckResult(
        "config.provider",
        "degraded",
        f"provider={name} is not configured (missing API key)",
    )


def _budget_ledger_check(store: StorePort) -> CheckResult:
    """Verify the budget ledger is readable and every row carries a valid cost."""
    from decimal import Decimal, InvalidOperation

    try:
        records = store.list_budget_records()
    except Exception as exc:  # pragma: no cover - defensive boundary
        return CheckResult("budget.ledger", "degraded", f"budget ledger unreadable ({exc})")
    invalid = 0
    for record in records:
        try:
            cost = Decimal(str(record.cost_usd))
        except (InvalidOperation, TypeError, ValueError):
            invalid += 1
            continue
        if not cost.is_finite() or cost < 0:
            invalid += 1
    unpriced = sum(1 for record in records if record.unpriced)
    if invalid:
        return CheckResult(
            "budget.ledger",
            "degraded",
            f"{invalid} budget record(s) have an invalid cost",
        )
    return CheckResult(
        "budget.ledger",
        "ok",
        f"{len(records)} budget record(s), {unpriced} unpriced",
    )


# RECHECKED means the re-check finished and the trigger awaits a human
# decision, so it is not stale; only PENDING_RECHECK is waiting on a re-check.
_STALE_TRIGGER_STATUSES = ("PENDING_RECHECK",)
_TRIGGER_PAGE_SIZE = 200


def _stale_triggers_check(
    store: StorePort, clock: ClockPort, stale_after_seconds: int
) -> CheckResult:
    """Count triggers waiting for a re-check longer than the freshness budget."""
    now = clock.now().astimezone(UTC)
    stale = 0
    total = 0
    try:
        for status in _STALE_TRIGGER_STATUSES:
            offset = 0
            while True:
                page = store.list_trigger_events(
                    status=status, limit=_TRIGGER_PAGE_SIZE, offset=offset
                )
                if not page:
                    break
                for record in page:
                    total += 1
                    try:
                        triggered_at = datetime.fromisoformat(
                            record.triggered_at.replace("Z", "+00:00")
                        )
                    except (TypeError, ValueError):
                        stale += 1
                        continue
                    if (now - triggered_at.astimezone(UTC)).total_seconds() > stale_after_seconds:
                        stale += 1
                offset += len(page)
    except Exception as exc:  # pragma: no cover - defensive boundary
        return CheckResult("triggers.stale", "degraded", f"trigger ledger unreadable ({exc})")
    if stale:
        return CheckResult(
            "triggers.stale",
            "degraded",
            f"{stale} trigger(s) awaiting a re-check beyond {stale_after_seconds}s; "
            "re-check or reject them",
        )
    return CheckResult(
        "triggers.stale",
        "ok",
        f"no stale pending triggers ({total} waiting)",
    )


def _safe_read(read: Any, default: Any) -> Any:
    """Run one diagnostics read, falling back on any store/attribute failure."""
    try:
        return read()
    except Exception:  # noqa: BLE001 - diagnostics must stay readable
        return default


def _receipt_projection(record: Any) -> dict[str, Any]:
    """The bounded, non-secret receipt fields; never the payload text."""
    return {
        "event_key": str(record.event_key),
        "kind": str(record.kind),
        "chat_id": int(record.chat_id),
        "state": str(record.state),
        "attempts": int(record.attempts),
        "message_id": record.message_id,
        "last_error_code": record.last_error_code,
        "created_at": str(record.created_at),
        "sent_at": record.sent_at,
    }


def telegram_observability(
    settings: Settings,
    store: StorePort,
    clock: ClockPort,
    *,
    daemon_state: str = "UP",
) -> dict[str, Any]:
    """One stable, non-secret Telegram state document for every diagnostics surface.

    Exposes only booleans, counts, allowlisted chat/user counts, the stable
    poller state/reason, receipt timestamps, per-chat totals, and a bounded list
    of recent receipts (id/state/error/message id). It never resolves or renders
    the token, the outbound text, or a raw Bot API body.
    """
    meta = _safe_read(lambda: store.get_secret_meta("telegram", "bot_token"), None)
    configured = meta is not None or settings.telegram_bot_token is not None
    chat_count = _safe_read(
        lambda: _allowlist_count(store, TELEGRAM_CHAT_IDS_KEY, label="chat allowlist"), 0
    )
    user_count = _safe_read(
        lambda: _allowlist_count(store, TELEGRAM_ALLOWED_USER_IDS_KEY, label="user allowlist"), 0
    )
    counts = _safe_read(
        lambda: store.telegram_receipt_counts(),
        {"queued": 0, "sending": 0, "sent": 0, "failed": 0},
    )
    if not isinstance(counts, dict):
        counts = {"queued": 0, "sending": 0, "sent": 0, "failed": 0}
    safe_counts = {
        state: int(counts.get(state, 0) or 0) for state in ("queued", "sending", "sent", "failed")
    }
    per_chat = _safe_read(lambda: store.telegram_chat_statuses(), ())
    per_chat_payload: list[dict[str, Any]] = []
    for status in per_chat if isinstance(per_chat, (tuple, list)) else ():
        if isinstance(status, dict):
            entry = dict(status)
        else:
            entry = {
                "chat_id": int(getattr(status, "chat_id", 0)),
                "queued": int(getattr(status, "queued", 0)),
                "sending": int(getattr(status, "sending", 0)),
                "sent": int(getattr(status, "sent", 0)),
                "failed": int(getattr(status, "failed", 0)),
                "last_error_code": getattr(status, "last_error_code", None),
            }
        per_chat_payload.append(entry)
    recent = _safe_read(lambda: store.list_telegram_receipts(limit=5), ())
    recent_payload = [
        _receipt_projection(record)
        for record in (recent if isinstance(recent, (tuple, list)) else ())
        if hasattr(record, "event_key")
    ]
    last_send_at = _safe_read(
        lambda: store.get_system_state(TELEGRAM_LAST_SEND_AT_KEY), None
    ) or _safe_read(lambda: store.last_telegram_sent_at(), None)
    state = _safe_read(lambda: store.get_system_state(POLLER_STATE_KEY), None)
    last_error_code = _safe_read(lambda: store.get_system_state(POLLER_LAST_ERROR_KEY), None)
    return {
        "configured": bool(configured),
        "chat_count": chat_count if isinstance(chat_count, int) else 0,
        "allowed_user_count": user_count if isinstance(user_count, int) else 0,
        "poller_state": state or POLLER_STATE_UNKNOWN,
        "last_poll_at": _safe_read(lambda: store.get_system_state(POLLER_HEARTBEAT_KEY), None),
        "last_send_at": last_send_at or None,
        "failing_receipts": safe_counts["failed"],
        "receipts": safe_counts,
        "per_chat": per_chat_payload,
        "recent_receipts": recent_payload,
        "overflow_code": _safe_read(
            lambda: store.get_system_state(TELEGRAM_OUTBOX_OVERFLOW_KEY), None
        ),
        "last_error_code": last_error_code or None,
        "daemon_state": daemon_state,
    }


def _telegram_check(
    settings: Settings,
    store: StorePort,
    clock: ClockPort,
    *,
    daemon_state: str,
) -> CheckResult:
    """Report Telegram configuration/poller state without resolving the token.

    Unconfigured Telegram is ``ok`` because the onboarding step is optional.
    A configured channel with an empty chat or user allowlist, an unreadable
    persisted allowlist, or a stale/missing poller heartbeat while the daemon is
    up is ``degraded``. Both lists are required: a chat-only allowlist refuses
    every sender (fail-closed). Only ``secrets_meta`` metadata and
    ``system_state``/``config`` rows are read.
    """
    try:
        meta = store.get_secret_meta("telegram", "bot_token")
    except Exception:  # pragma: no cover - defensive boundary
        return CheckResult("telegram", "degraded", "telegram secret metadata unreadable")
    configured = meta is not None or settings.telegram_bot_token is not None
    if not configured:
        summary = telegram_observability(settings, store, clock, daemon_state=daemon_state)
        if summary["failing_receipts"] > 0:
            return CheckResult(
                "telegram",
                "degraded",
                f"{summary['failing_receipts']} failing telegram receipt(s); "
                "the channel is not configured",
            )
        return CheckResult("telegram", "ok", "not configured (optional)")
    if meta is None and settings.telegram_bot_token is not None:
        # The environment fallback is raw and never validated on write; an
        # invalid-shaped token makes every poll fail, so report it as degraded.
        # The value itself is never echoed.
        try:
            validate_telegram_token(settings.telegram_bot_token.get_secret_value())
        except TelegramValidationError:
            return CheckResult(
                "telegram", "degraded", "the environment Telegram bot token is invalid"
            )
    chat_count = _allowlist_count(store, TELEGRAM_CHAT_IDS_KEY, label="chat allowlist")
    if isinstance(chat_count, CheckResult):
        return chat_count
    user_count = _allowlist_count(store, TELEGRAM_ALLOWED_USER_IDS_KEY, label="user allowlist")
    if isinstance(user_count, CheckResult):
        return user_count
    if chat_count == 0:
        return CheckResult(
            "telegram",
            "degraded",
            "configured but no chat allowlist; the poller will not answer",
        )
    if user_count == 0:
        return CheckResult(
            "telegram",
            "degraded",
            "configured but no user allowlist; a chat-only allowlist refuses every sender",
        )
    summary = telegram_observability(settings, store, clock, daemon_state=daemon_state)
    poller_state = summary["poller_state"]
    reason = summary.get("last_error_code")
    suffix = f" ({reason})" if reason else ""
    if poller_state == POLLER_STATE_CONFLICT:
        return CheckResult(
            "telegram",
            "degraded",
            f"poller CONFLICT: another poller owns this token{suffix}",
        )
    if poller_state == POLLER_STATE_DEGRADED:
        return CheckResult("telegram", "degraded", f"poller DEGRADED{suffix}")
    if summary["failing_receipts"] > 0:
        return CheckResult(
            "telegram",
            "degraded",
            f"{summary['failing_receipts']} failing receipt(s); other chats continue",
        )
    if daemon_state != "UP":
        return CheckResult("telegram", "ok", f"configured; {chat_count} chat(s); daemon is offline")
    raw = store.get_system_state_updated_at(POLLER_HEARTBEAT_KEY)
    if raw is None:
        return CheckResult(
            "telegram", "degraded", "configured but the poller has not reported a heartbeat"
        )
    try:
        heartbeat = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return CheckResult("telegram", "degraded", "poller heartbeat is unreadable")
    age = (clock.now().astimezone(UTC) - heartbeat.astimezone(UTC)).total_seconds()
    if age > POLLER_STALE_SECONDS:
        return CheckResult("telegram", "degraded", f"stale poller heartbeat ({age:.0f}s old)")
    last_send = summary.get("last_send_at") or "never"
    return CheckResult(
        "telegram",
        "ok",
        f"configured; {chat_count} chat(s); poller {age:.0f}s old; last send {last_send}",
    )


def _allowlist_count(store: StorePort, key: str, *, label: str) -> int | CheckResult:
    """The bounded allowlist length, or a degraded check for a bad row."""
    try:
        record = store.get_config(key)
    except Exception:  # pragma: no cover - defensive boundary
        return CheckResult("telegram", "degraded", f"persisted Telegram {label} is unreadable")
    if record is None:
        return 0
    try:
        value = json.loads(record.value_json)
    except (TypeError, ValueError):
        return CheckResult("telegram", "degraded", f"persisted Telegram {label} is unreadable")
    return len(value) if isinstance(value, list) else 0


def _kill_switch_check(settings: Settings, store: StorePort, clock: ClockPort) -> CheckResult:
    """Report the kill-switch state; an active switch blocks new paper orders."""
    from oft.application.kill_switch import KillSwitch

    try:
        active = KillSwitch(settings=settings, store=store, clock=clock).is_active()
    except Exception as exc:  # pragma: no cover - defensive boundary
        return CheckResult("kill_switch", "degraded", f"kill switch state unreadable ({exc})")
    if active:
        return CheckResult(
            "kill_switch", "degraded", "kill switch is ACTIVE; new orders are blocked"
        )
    return CheckResult("kill_switch", "ok", "kill switch clear")


def build_doctor(
    settings: Settings,
    store: StorePort,
    market_data: MarketDataPort,
    clock: ClockPort,
    *,
    daemon: DaemonProbe | None = None,
) -> DoctorReport:
    readiness = build_readiness(settings, store, market_data, clock)
    probe = daemon or probe_daemon(settings)
    from oft.application.ingestion_supervisor import checkpoint_diagnostic, read_ws_state

    checkpoint = checkpoint_diagnostic(settings, clock)
    if not checkpoint.present:
        ingest_check = CheckResult(
            name="ingest.checkpoint",
            status="ok",
            detail="no ingestion checkpoint yet",
        )
    elif checkpoint.stale:
        ingest_check = CheckResult(
            name="ingest.checkpoint",
            status="degraded",
            detail=(f"stale checkpoint ({checkpoint.age_seconds:.0f}s old) at {checkpoint.path}"),
        )
    else:
        ingest_check = CheckResult(
            name="ingest.checkpoint",
            status="ok",
            detail=(f"{len(checkpoint.streams)} stream(s), {checkpoint.age_seconds:.0f}s old"),
        )
    ws_state = read_ws_state(settings)
    if ws_state is None:
        ws_check = CheckResult(name="ingest.ws", status="ok", detail="no live WS state yet")
    elif (
        ws_state.stale
        or ws_state.connection_state != "SUBSCRIBED"
        or not ws_state.streams
        or not ws_state.freshness
    ):
        ws_check = CheckResult(
            name="ingest.ws",
            status="degraded",
            detail=f"{ws_state.connection_state}; stale={ws_state.stale}",
        )
    else:
        ws_check = CheckResult(
            name="ingest.ws",
            status="ok",
            detail=(
                f"{ws_state.connection_state}; streams={len(ws_state.streams)}; "
                f"malformed={ws_state.stats.get('malformed', 0)}; "
                f"oversized={ws_state.stats.get('oversized', 0)}; "
                f"accepted={ws_state.stats.get('accepted', 0)}"
            ),
        )
    checks = (
        *readiness.checks,
        _disk_check(settings),
        _manifest_orphans_check(settings, store),
        _stale_jobs_check(store, clock),
        _management_jobs_check(store, clock),
        _funding_idempotency_check(store),
        _audit_chain_check(store),
        _provider_check(settings),
        _budget_ledger_check(store),
        _stale_triggers_check(store, clock, settings.trigger_stale_after_seconds),
        _kill_switch_check(settings, store, clock),
        _secrets_check(settings),
        _telegram_check(settings, store, clock, daemon_state=probe.state),
        ingest_check,
        ws_check,
        CheckResult(
            name="daemon",
            status="ok" if probe.state == "UP" else "offline",
            detail=probe.detail,
        ),
    )
    if probe.state == "OFFLINE":
        overall: Literal["READY", "DEGRADED"] = "DEGRADED"
    elif all(check.status in ("ok", "unknown") for check in checks):
        overall = "READY"
    else:
        overall = "DEGRADED"
    return DoctorReport(
        status=overall,
        version=__version__,
        checked_at=readiness.checked_at,
        checks=checks,
        latest_scan=readiness.latest_scan,
        telegram=telegram_observability(settings, store, clock, daemon_state=probe.state),
    )
