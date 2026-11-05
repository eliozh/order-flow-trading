"""Health report used by the unauthenticated health route."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from oft import __version__


class HealthReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    version: str


def build_health() -> HealthReport:
    return HealthReport(status="ok", version=__version__)
