"""Health, readiness, doctor checks, and daemon probing."""

from __future__ import annotations

import socket
from dataclasses import asdict, dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from oft import __version__
from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.market_data import MarketDataPort
from oft.ports.store import StorePort

CheckState = Literal["ok", "degraded", "offline"]


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
        data_dir=str(settings.data_dir),
        checked_at=utc_timestamp(now),
        checks=tuple(checks),
        latest_scan=asdict(latest) if latest is not None else None,
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
    checks = (
        *readiness.checks,
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
