"""Status read model shared by the API and CLI."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from oft import __version__
from oft.config import Settings
from oft.domain.symbols import SymbolUniverseSummary, summarize_universe
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.market_data import MarketDataPort


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


def build_status(
    settings: Settings,
    market_data: MarketDataPort,
    clock: ClockPort,
) -> StatusReport:
    """Build the status report from typed settings and the market-data port."""
    summary = summarize_universe(market_data.load_universe())
    return StatusReport(
        env=settings.env.value,
        version=__version__,
        host=settings.host,
        port=settings.port,
        data_dir=str(settings.data_dir),
        log_level=settings.log_level,
        generated_at=utc_timestamp(clock.now()),
        universe=summary,
    )
