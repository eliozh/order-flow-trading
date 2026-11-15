"""The sole read/write path for persisted runtime settings (slice 32).

``scan.period`` is the first (and only) setting in this slice. The effective
value is the persisted ``config`` row when present, otherwise ``OFT_SCAN_PERIOD``
(or the built-in ``4h``); the source is reported, never guessed. A save writes
one ``config`` row and one ``CONFIG_UPDATED`` audit event atomically, and an
identical effective value is a deterministic no-op.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from oft.application.scheduler import next_boundary, resolve_schedule_settings
from oft.config import ConfigError
from oft.domain.settings import (
    CONFIG_SUBJECT_KIND,
    CONFIG_UPDATED_EVENT,
    SCAN_PERIOD_KEY,
    SUPPORTED_SCAN_PERIODS,
    SettingSource,
    SettingValidationError,
    scoped_diff,
    validate_scan_period,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import AuditEventDraft, ConfigRecord, StorePort

TIMEZONE = "UTC"


@dataclass(frozen=True)
class ScanSettings:
    """The effective ``scan.period`` plus its provenance and next boundary."""

    key: str
    value: str
    source: SettingSource
    supported: tuple[str, ...]
    next_run: str
    timezone: str
    updated_at: str | None
    updated_by: str | None

    def to_contract(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source.value,
            "supported": list(self.supported),
            "next_run": self.next_run,
            "timezone": self.timezone,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


class SettingsService:
    """Read sections and persist validated settings with one audit event."""

    def __init__(
        self,
        *,
        store: StorePort,
        clock: ClockPort,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._environ = environ

    def _env_period(self) -> str:
        try:
            return resolve_schedule_settings(self._environ).period
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

    def _decode(self, record: ConfigRecord) -> str:
        try:
            value = json.loads(record.value_json)
        except (TypeError, ValueError) as exc:
            raise ConfigError("persisted scan.period is unreadable") from exc
        try:
            return validate_scan_period(value)
        except SettingValidationError as exc:
            raise ConfigError(f"persisted scan.period is invalid: {exc.message}") from exc

    def persisted_scan_period(self) -> str | None:
        """The persisted period, or ``None`` when no operator row exists."""
        record = self._store.get_config(SCAN_PERIOD_KEY)
        return None if record is None else self._decode(record)

    def read_scan_settings(self) -> ScanSettings:
        """The effective value plus ``next_run`` for that effective period.

        ``next_run`` is the next UTC boundary of the *effective* period. A saved
        change is applied by the daemon at the next boundary of the schedule
        that was active when the change was saved, so the two can differ while a
        change is pending.
        """
        record = self._store.get_config(SCAN_PERIOD_KEY)
        if record is None:
            value = self._env_period()
            source = SettingSource.DEFAULT
            updated_at: str | None = None
            updated_by: str | None = None
        else:
            value = self._decode(record)
            source = SettingSource.USER
            updated_at = record.updated_at
            updated_by = record.updated_by
        now = self._clock.now()
        return ScanSettings(
            key=SCAN_PERIOD_KEY,
            value=value,
            source=source,
            supported=SUPPORTED_SCAN_PERIODS,
            next_run=utc_timestamp(next_boundary(now, value)),
            timezone=TIMEZONE,
            updated_at=updated_at,
            updated_by=updated_by,
        )

    def read_sections(self) -> list[dict[str, Any]]:
        """Section summaries with source/last change only; never values."""
        scan = self.read_scan_settings()
        return [
            {
                "section": "scan",
                "source": scan.source.value,
                "last_changed": scan.updated_at,
                "keys": [SCAN_PERIOD_KEY],
            }
        ]

    def validate_settings(self) -> dict[str, Any]:
        """Validate the effective setting without writing anything."""
        scan = self.read_scan_settings()
        return {
            "status": "PASS",
            "key": scan.key,
            "value": scan.value,
            "source": scan.source.value,
            "supported": list(scan.supported),
            "errors": [],
        }

    def update_scan_period(self, period: object, *, actor: str, reason: str = "") -> ScanSettings:
        """Validate, persist, and audit one scan-period change.

        Raises :class:`SettingValidationError` before any write when the value is
        invalid. Saving the effective value is a no-op (no row, no audit).
        """
        value = validate_scan_period(period)
        current = self.read_scan_settings()
        if current.value == value:
            return current

        now = self._clock.now()
        timestamp = utc_timestamp(now)
        record = ConfigRecord(
            key=SCAN_PERIOD_KEY,
            value_json=json.dumps(value),
            updated_at=timestamp,
            updated_by=actor,
        )
        payload = {
            **scoped_diff(key=SCAN_PERIOD_KEY, before=current.value, after=value),
            "source_before": current.source.value,
            "source_after": SettingSource.USER.value,
            "reason": reason,
        }
        audit = AuditEventDraft(
            event_type=CONFIG_UPDATED_EVENT,
            actor={"kind": "USER", "id": actor},
            subject={"kind": CONFIG_SUBJECT_KIND, "id": SCAN_PERIOD_KEY},
            payload=payload,
            created_at=timestamp,
        )
        self._store.save_config_with_audit(record, audit)
        return self.read_scan_settings()


__all__ = ["ScanSettings", "SettingsService", "TIMEZONE"]
