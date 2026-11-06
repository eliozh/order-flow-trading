"""Status read model shared by the API and CLI."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from oft import __version__
from oft.application.health import DaemonProbe
from oft.config import Settings
from oft.domain.symbols import SymbolUniverseSummary, summarize_universe
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.market_data import MarketDataPort
from oft.ports.store import StorePort


class DaemonStatus(BaseModel):
    """Whether the local daemon answered a loopback probe."""

    model_config = ConfigDict(frozen=True)

    state: Literal["UP", "OFFLINE"]
    detail: str


class LastScanSummary(BaseModel):
    """Small scan summary carried by status and readiness surfaces."""

    model_config = ConfigDict(frozen=True)

    scan_id: str
    symbol: str
    period: str
    status: str
    delta: str
    cvd: str
    finished_at: str


class StatusReport(BaseModel):
    """Daemon-independent system status."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    env: str
    version: str
    host: str
    port: int
    data_dir: str
    log_level: str
    generated_at: str
    universe: SymbolUniverseSummary
    daemon: DaemonStatus
    last_scan: LastScanSummary | None = None


def _last_scan(store: StorePort | None) -> LastScanSummary | None:
    if store is None:
        return None
    record = store.latest_scan()
    if record is None:
        return None
    return LastScanSummary(
        scan_id=record.scan_id,
        symbol=record.symbol,
        period=record.period,
        status=record.status,
        delta=record.delta,
        cvd=record.cvd,
        finished_at=record.finished_at,
    )


def build_status(
    settings: Settings,
    market_data: MarketDataPort,
    clock: ClockPort,
    *,
    store: StorePort | None = None,
    daemon: DaemonProbe | None = None,
) -> StatusReport:
    """Build the status report from typed settings plus optional local state."""
    summary = summarize_universe(market_data.load_universe())
    probe = daemon or DaemonProbe(state="UP", detail="served by this process")
    return StatusReport(
        env=settings.env.value,
        version=__version__,
        host=settings.host,
        port=settings.port,
        data_dir=str(settings.data_dir),
        log_level=settings.log_level,
        generated_at=utc_timestamp(clock.now()),
        universe=summary,
        daemon=DaemonStatus(state=probe.state, detail=probe.detail),
        last_scan=_last_scan(store),
    )
