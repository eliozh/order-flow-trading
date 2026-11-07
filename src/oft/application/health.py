"""Health, readiness, doctor checks, and daemon probing."""

from __future__ import annotations

import shutil
import socket
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from oft import __version__
from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.market_data import MarketDataPort
from oft.ports.store import StorePort

CheckState = Literal["ok", "degraded", "offline"]

MIN_FREE_DISK_BYTES = 1 * 1024**3
DISK_WARN_USED_PCT = 95.0
MANIFEST_SCAN_ROOTS = ("market", "metrics", "footprint", "liquidations")
STALE_JOB_LIMIT = 200


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


def _disk_check(settings: Settings) -> CheckResult:
    """Report free space without ever reading a file's contents or secrets."""
    try:
        usage = shutil.disk_usage(_existing_ancestor(settings.data_dir))
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
    known = {entry.path for entry in entries}
    missing = [entry for entry in entries if not Path(entry.path).is_file()]
    disk_parts: list[str] = []
    for name in MANIFEST_SCAN_ROOTS:
        root = settings.data_dir / name
        if root.is_dir():
            disk_parts.extend(str(path) for path in root.rglob("*.parquet"))
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
    """Report RUNNING job rows whose lease already expired."""
    try:
        runs = store.list_job_runs(limit=STALE_JOB_LIMIT)
    except Exception as exc:  # pragma: no cover - defensive boundary
        return CheckResult("jobs.stale", "degraded", f"job ledger unreadable ({exc})")
    now = utc_timestamp(clock.now())
    stale = [run for run in runs if run.status == "RUNNING" and run.lease_expires_at <= now]
    if stale:
        return CheckResult(
            "jobs.stale",
            "degraded",
            f"{len(stale)} stale RUNNING job run(s); recover or fail them",
        )
    return CheckResult("jobs.stale", "ok", "no stale RUNNING job runs")


def _audit_chain_check() -> CheckResult:
    """Milestone-1 placeholder: the hash chain is not implemented yet."""
    return CheckResult(
        "audit.chain",
        "ok",
        "placeholder: audit_events hash chain is not implemented in milestone 1",
    )


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
        _audit_chain_check(),
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
    elif all(check.status == "ok" for check in checks):
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
