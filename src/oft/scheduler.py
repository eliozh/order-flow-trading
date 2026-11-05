"""APScheduler placeholder.

The durable SQLite job store and UTC-anchored windows arrive with the
scheduler slice; the walking skeleton only exposes a constructible scheduler.
"""

from __future__ import annotations

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from oft.config import Settings


def create_scheduler(_settings: Settings) -> BackgroundScheduler:
    """Return a stopped scheduler; no jobs are registered yet."""
    return BackgroundScheduler(
        jobstores={"default": MemoryJobStore()},
        timezone="UTC",
    )
