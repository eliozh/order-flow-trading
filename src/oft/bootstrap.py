"""Composition root: the one place adapters are wired to application services."""

from __future__ import annotations

from dataclasses import dataclass

from oft.adapters.fixture_market_data import FixtureMarketData
from oft.application.status import StatusReport, build_status
from oft.clock import SystemClock
from oft.config import Settings, load_settings
from oft.ports.clock import ClockPort
from oft.ports.market_data import MarketDataPort


@dataclass(frozen=True)
class AppContainer:
    settings: Settings
    clock: ClockPort
    market_data: MarketDataPort


def build_container(settings: Settings | None = None) -> AppContainer:
    resolved = settings or load_settings()
    return AppContainer(
        settings=resolved,
        clock=SystemClock(),
        market_data=FixtureMarketData(),
    )


def get_status(settings: Settings | None = None) -> StatusReport:
    """Build a status report through the same path for every client."""
    container = build_container(settings)
    return build_status(container.settings, container.market_data, container.clock)
