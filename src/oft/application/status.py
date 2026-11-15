"""Status read model shared by the API and CLI."""

from __future__ import annotations

from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from oft import __version__
from oft.application.health import DaemonProbe
from oft.config import ConfigError, Settings
from oft.domain.symbols import SymbolUniverseSummary, summarize_universe
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.market_data import MarketDataPort
from oft.ports.store import StorePort

_LOGGER = structlog.get_logger(__name__)


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


class MarketDataStreamState(BaseModel):
    """Last event position for one live stream."""

    model_config = ConfigDict(frozen=True)

    stream: str
    symbol: str | None = None
    last_event_time: str | None = None
    last_event_id: int | None = None
    stale: bool = False


class MarketDataStatus(BaseModel):
    """WS connection state and last event times (daemon-independent)."""

    model_config = ConfigDict(frozen=True)

    connection_state: str
    updated_at: str
    stale: bool
    streams: tuple[MarketDataStreamState, ...]
    counters: dict[str, int] = Field(default_factory=dict)


class ScheduleStatus(BaseModel):
    """Persisted scan cadence surfaced to the web schedule card."""

    model_config = ConfigDict(frozen=True)

    timezone: str = "UTC"
    period: str
    pending_period: str | None = None
    next_run_at: str


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
    market_data: MarketDataStatus | None = None
    schedule: ScheduleStatus | None = None


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


def _freshness_key(stream: str) -> str | None:
    if "@markPrice" in stream:
        return "mark"
    if "@kline_" in stream:
        return "kline_1m"
    return None


def _market_data_status(settings: Settings) -> MarketDataStatus | None:
    from oft.application.ingestion_supervisor import read_ws_state

    state = read_ws_state(settings)
    if state is None:
        return None
    streams: list[MarketDataStreamState] = []
    for stream in sorted(state.streams):
        payload = state.streams[stream]
        freshness_key = _freshness_key(stream)
        fresh = state.freshness.get(freshness_key, {}) if freshness_key else {}
        raw_id = payload.get("last_event_id")
        streams.append(
            MarketDataStreamState(
                stream=stream,
                symbol=payload.get("symbol") if isinstance(payload.get("symbol"), str) else None,
                last_event_time=(
                    payload.get("last_event_time")
                    if isinstance(payload.get("last_event_time"), str)
                    else None
                ),
                last_event_id=raw_id if isinstance(raw_id, int) else None,
                stale=bool(fresh.get("stale", False)),
            )
        )
    return MarketDataStatus(
        connection_state=state.connection_state,
        updated_at=state.updated_at,
        stale=state.stale,
        streams=tuple(streams),
        counters=dict(state.stats),
    )


def _schedule_status(
    settings: Settings, clock: ClockPort, store: StorePort | None
) -> ScheduleStatus | None:
    """Read the persisted cadence, overlaying the saved period (M14)."""
    from oft.application.settings_service import resolve_schedule_state_overlay

    try:
        state = resolve_schedule_state_overlay(
            settings=settings,
            store=store,
            clock=clock,
        )
    except (ConfigError, ValueError) as exc:
        # A corrupt persisted row (or an incompatible env jitter) still returns
        # HTTP 200, but the dropped schedule must be diagnosable, not silent.
        _LOGGER.warning("schedule_status_unavailable", error=str(exc))
        return None
    payload = state.next_run_payload(clock.now())
    return ScheduleStatus(
        timezone=payload["timezone"],
        period=payload["period"],
        pending_period=payload["pending_period"] or None,
        next_run_at=payload["next_run_at"],
    )


def build_status(
    settings: Settings,
    market_data: MarketDataPort,
    clock: ClockPort,
    *,
    store: StorePort | None = None,
    daemon: DaemonProbe | None = None,
    market_data_status: MarketDataStatus | None = None,
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
        market_data=market_data_status or _market_data_status(settings),
        schedule=_schedule_status(settings, clock, store),
    )
