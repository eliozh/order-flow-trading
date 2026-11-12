"""Health, readiness, doctor checks, and daemon probing."""

from __future__ import annotations

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


@dataclass(frozen=True)
class DoctorReport:
    status: Literal["READY", "DEGRADED", "OFFLINE"]
    version: str
    checked_at: str
    checks: tuple[CheckResult, ...]
    latest_scan: dict[str, Any] | None


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
    )


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
    """Report the configured LLM provider without ever reading a key value."""
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


_STALE_TRIGGER_STATUSES = ("PENDING_RECHECK", "RECHECKED")
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
        _audit_chain_check(store),
        _provider_check(settings),
        _budget_ledger_check(store),
        _stale_triggers_check(store, clock, settings.trigger_stale_after_seconds),
        _kill_switch_check(settings, store, clock),
        _secrets_check(settings),
        ingest_check,
        ws_check,
        CheckResult(
            name="daemon",
            status="ok" if probe.state == "UP" else "offline",
            detail=probe.detail,
        ),
    )
    if probe.state == "OFFLINE":
        overall: Literal["READY", "DEGRADED", "OFFLINE"] = "DEGRADED"
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
    )
