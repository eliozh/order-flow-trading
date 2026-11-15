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
from datetime import datetime
from typing import TYPE_CHECKING, Any

from oft.application.scheduler import (
    ScheduleState,
    load_schedule_state,
    next_boundary,
    resolve_scan_jitter,
    resolve_schedule_settings,
    schedule_state_path,
)
from oft.config import ConfigError
from oft.domain.settings import (
    CONFIG_SUBJECT_KIND,
    CONFIG_UPDATED_EVENT,
    SCAN_PERIOD_HOURS,
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

if TYPE_CHECKING:
    from oft.config import Settings

TIMEZONE = "UTC"
# The overlay jitter must be valid for *every* supported (and pending) period,
# so clamp to just under the smallest one. Clamping keeps an out-of-range
# environment value from turning a saved period into a scheduler traceback.
OVERLAY_MAX_JITTER_SECONDS = min(SCAN_PERIOD_HOURS.values()) * 3600 - 1


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
    """Read sections and persist a validated setting with one audit event.

    A save that *changes* the effective value writes one ``config`` row and one
    ``CONFIG_UPDATED`` event atomically (``save_config_change``); a save that
    repeats the effective value writes nothing. The store's ``set_config`` and
    ``save_config_with_audit`` are lower-level seams, not this service's path.
    """

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
        """Validate, persist, and audit one scan-period change atomically.

        Raises :class:`SettingValidationError` before any write when the value is
        invalid. The compare and the write run inside one store transaction, so
        two concurrent saves cannot both audit or record a stale ``before``.
        Saving the effective value is a no-op (no row, no audit).
        """
        value = validate_scan_period(period)
        now = self._clock.now()
        timestamp = utc_timestamp(now)

        def _build(
            current: ConfigRecord | None,
        ) -> tuple[ConfigRecord, AuditEventDraft] | None:
            if current is None:
                before = self._env_period()
                source_before = SettingSource.DEFAULT
            else:
                before = self._decode(current)
                source_before = SettingSource.USER
            if before == value:
                return None
            record = ConfigRecord(
                key=SCAN_PERIOD_KEY,
                value_json=json.dumps(value),
                updated_at=timestamp,
                updated_by=actor,
            )
            payload = {
                **scoped_diff(key=SCAN_PERIOD_KEY, before=before, after=value),
                "source_before": source_before.value,
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
            return record, audit

        self._store.save_config_change(SCAN_PERIOD_KEY, _build)
        # Read after the commit so the response reflects the committed row.
        return self.read_scan_settings()


def resolve_schedule_state_overlay(
    *,
    settings: Settings,
    store: StorePort | None,
    clock: ClockPort,
    now: datetime | None = None,
) -> ScheduleState:
    """The persisted schedule with the saved period overlaid, promoted.

    Resolves the persisted row *before* validating the environment period (M3),
    so a valid saved ``8h`` keeps diagnostics working even when
    ``OFT_SCAN_PERIOD`` is invalid. The jitter comes only from
    :func:`resolve_scan_jitter` and is clamped below the smallest supported
    period, so an out-of-range ``OFT_SCAN_JITTER_SECONDS`` cannot make a saved
    period raise from ``ScheduleState``. Shared by the daemon/CLI status paths
    and ``/api/v1/status`` (M14).
    """
    persisted: str | None = None
    if store is not None:
        persisted = SettingsService(store=store, clock=clock).persisted_scan_period()
    if persisted is not None:
        default_period = persisted
    else:
        try:
            default_period = resolve_schedule_settings().period
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    jitter = min(resolve_scan_jitter(), OVERLAY_MAX_JITTER_SECONDS)
    default = ScheduleState(period=default_period, jitter_seconds_max=jitter)
    state = load_schedule_state(schedule_state_path(settings), default=default)
    resolved_now = now or clock.now()
    promoted, _changed = state.apply_persisted(persisted, now=resolved_now)
    return promoted


__all__ = [
    "OVERLAY_MAX_JITTER_SECONDS",
    "ScanSettings",
    "SettingsService",
    "TIMEZONE",
    "resolve_schedule_state_overlay",
]
