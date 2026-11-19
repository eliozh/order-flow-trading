"""The sole read/write path for persisted runtime settings (slices 32/34).

``scan.period`` is the first setting; slice 34 adds the risk-cap document under
``system_state.risk_caps``. The effective value is the persisted row when
present, otherwise the configured default (``OFT_SCAN_PERIOD`` or the
built-in); the source is reported, never guessed. A save writes its row and one
audit event atomically, and an identical effective value is a deterministic
no-op. Risk-cap loosening additionally requires an explicit confirmation.

Documented maintainability exception: this module is 800+ lines because the
slice-47 Telegram allowlist section and the slice-53 Telegram settings section
are tightly coupled to the config/audit transaction seam
(``save_telegram_allowlist_change`` / ``save_telegram_settings_change``).
Extracting them as mixins would only move the same code and duplicate the
validation boundary; the exception is recorded here per the repository's
soft-ceiling standard and should be revisited if the section grows further.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from oft.application.entry_service import (
    caps_from_settings,
    decode_persisted_caps,
    effective_caps,
)
from oft.application.llm_settings_service import (
    LLMSettings,
    LLMSettingsMixin,
    resolve_effective_llm_settings,
)
from oft.application.onboarding_service import (
    ACTIVE_ONBOARDING_STEPS,
    DEFERRED_ONBOARDING_STEPS,
    ONBOARDING_PROGRESS_KEY,
    ONBOARDING_STEPS,
    REQUIRED_ONBOARDING_STEPS,
    OnboardingProgress,
    OnboardingProgressMixin,
    OnboardingStep,
    validate_onboarding_step,
)
from oft.application.risk_utilization import (
    RiskUtilization,
    RiskUtilizationMetric,
    build_risk_utilization,
    unavailable_utilization,
)
from oft.application.scheduler import (
    ScheduleState,
    load_schedule_state,
    next_boundary,
    resolve_scan_jitter,
    resolve_schedule_settings,
    schedule_state_path,
)
from oft.config import ConfigError
from oft.domain.risk import (
    CAP_FIELDS,
    DAILY_LOSS_BELOW_PER_TRADE,
    RISK_CAPS_STATE_KEY,
    RiskCaps,
    caps_loosened,
    validate_caps,
)
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
from oft.domain.telegram import (
    BUDGET_PUSH_KEY,
    ENTRY_RECHECK_ALERT_KEY,
    MAX_TELEGRAM_ALLOWED_USER_IDS,
    MAX_TELEGRAM_CHAT_IDS,
    POSITION_PUSH_KEY,
    RISK_PUSH_KEY,
    SCAN_FAILED_ALERT_KEY,
    SCAN_REPORT_PUSH_KEY,
    TELEGRAM_ALLOWED_USER_IDS_KEY,
    TELEGRAM_CHAT_IDS_KEY,
    TRIGGER_ALERT_KEY,
    TelegramValidationError,
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

RISK_CAPS_UPDATED_EVENT = "RISK_CAPS_UPDATED"
RISK_SECTION = "risk"
TELEGRAM_ALLOWLIST_UPDATED = "TELEGRAM_ALLOWLIST_UPDATED"
TELEGRAM_SETTINGS_UPDATED = "TELEGRAM_SETTINGS_UPDATED"
TELEGRAM_ALLOWLIST_SECTION = "telegram"
# The aggregate settings section and the allowlist audit subject are the same
# domain concept; alias it so the ids cannot drift.
TELEGRAM_SECTION = TELEGRAM_ALLOWLIST_SECTION
# The six operator-facing push toggles and the config key each persists.
TELEGRAM_PUSH_TOGGLES: tuple[tuple[str, str, bool], ...] = (
    ("scan_report", SCAN_REPORT_PUSH_KEY, True),
    ("trigger", TRIGGER_ALERT_KEY, True),
    ("entry_recheck", ENTRY_RECHECK_ALERT_KEY, True),
    ("position", POSITION_PUSH_KEY, True),
    ("risk", RISK_PUSH_KEY, True),
    ("budget", BUDGET_PUSH_KEY, True),
)
TELEGRAM_PUSH_TOGGLE_NAMES: tuple[str, ...] = tuple(
    name for name, _key, _default in TELEGRAM_PUSH_TOGGLES
)
TELEGRAM_SETTINGS_KEYS: tuple[str, ...] = (
    TELEGRAM_CHAT_IDS_KEY,
    TELEGRAM_ALLOWED_USER_IDS_KEY,
    *(key for _name, key, _default in TELEGRAM_PUSH_TOGGLES),
)
_UNSET: Any = object()
_LOGGER = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class RiskSummary:
    """Effective caps and provenance only; no utilization work.

    The overview sections list and ``oft risk show|validate`` need the caps and
    their source, never the utilization snapshot, so they must not trigger the
    trade-log/position scans that snapshot costs.
    """

    caps: RiskCaps
    source: SettingSource
    defaults: RiskCaps
    updated_at: str | None


@dataclass(frozen=True)
class RiskSettings:
    """The effective risk caps plus provenance, defaults, and utilization."""

    caps: RiskCaps
    source: SettingSource
    defaults: RiskCaps
    utilization: RiskUtilization
    updated_at: str | None
    updated_by: str | None

    def to_contract(self) -> dict[str, Any]:
        return {
            "caps": self.caps.to_state(),
            "source": self.source.value,
            "defaults": self.defaults.to_state(),
            "utilization": self.utilization.to_contract(),
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


@dataclass(frozen=True)
class RiskCapsUpdateResult:
    """What one risk-cap save committed, shaped for the CLI and API."""

    settings: RiskSettings
    before: RiskCaps
    after: RiskCaps
    loosened: tuple[str, ...]
    changed: bool


@dataclass(frozen=True)
class TelegramAllowlistSettings:
    """The effective Telegram allowlists plus their last change."""

    chat_ids: tuple[int, ...]
    allowed_user_ids: tuple[int, ...]
    updated_at: str | None
    updated_by: str | None

    def to_contract(self) -> dict[str, Any]:
        return {
            "chat_ids": list(self.chat_ids),
            "allowed_user_ids": list(self.allowed_user_ids),
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


@dataclass(frozen=True)
class TelegramSettings:
    """The effective Telegram push configuration (never the token)."""

    chat_ids: tuple[int, ...]
    allowed_user_ids: tuple[int, ...]
    toggles: dict[str, bool]
    source: SettingSource
    updated_at: str | None
    updated_by: str | None

    def to_contract(self) -> dict[str, Any]:
        return {
            "chat_ids": list(self.chat_ids),
            "allowed_user_ids": list(self.allowed_user_ids),
            "toggles": dict(self.toggles),
            "source": self.source.value,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


class SettingsService(LLMSettingsMixin, OnboardingProgressMixin):
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
        settings: Settings | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._environ = environ
        self._settings = settings

    def _env_period(self) -> str:
        try:
            return resolve_schedule_settings(self._environ, settings=self._settings).period
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
        """Section summaries with source/last change only; never values.

        The risk section uses the metadata-only read, so the overview never
        pays for the utilization snapshot.
        """
        scan = self.read_scan_settings()
        risk = self.read_risk_summary()
        telegram = self.read_telegram_settings()
        return [
            {
                "section": "scan",
                "source": scan.source.value,
                "last_changed": scan.updated_at,
                "keys": [SCAN_PERIOD_KEY],
            },
            {
                "section": RISK_SECTION,
                "source": risk.source.value,
                "last_changed": risk.updated_at,
                "keys": list(CAP_FIELDS),
            },
            {
                "section": TELEGRAM_SECTION,
                "source": telegram.source.value,
                "last_changed": telegram.updated_at,
                "keys": list(TELEGRAM_SETTINGS_KEYS),
            },
        ]

    # -- risk caps -------------------------------------------------------

    def read_risk_summary(self) -> RiskSummary:
        """Effective caps plus provenance; never builds the utilization snapshot.

        ``updated_at`` comes from the committed ``system_state.risk_caps`` row
        (the same row the caps are read from), not from an audit scan, so a
        hand-seeded or legacy row can never report ``source: user`` with a null
        last change.
        """
        caps, raw_source = effective_caps(self._settings, self._store)
        source = SettingSource.USER if raw_source == "persisted" else SettingSource.DEFAULT
        return RiskSummary(
            caps=caps,
            source=source,
            defaults=caps_from_settings(self._settings),
            updated_at=(
                self._store.get_system_state_updated_at(RISK_CAPS_STATE_KEY)
                if raw_source == "persisted"
                else None
            ),
        )

    def read_risk_settings(self) -> RiskSettings:
        """Effective caps, their source/defaults, and a read-only snapshot.

        ``effective_caps`` merges the persisted ``system_state.risk_caps``
        document field by field over the ``OFT_RISK_*`` base, so a partial
        operator row can never silently reset an env-tightened cap. An
        unreadable persisted document raises :class:`RiskCapsError` (fail
        closed); nothing is written.
        """
        summary = self.read_risk_summary()
        audit = self._store.last_audit_event_of_type(RISK_CAPS_UPDATED_EVENT)
        updated_by = _audit_actor_id(audit.payload_json) if audit is not None else None
        return RiskSettings(
            caps=summary.caps,
            source=summary.source,
            defaults=summary.defaults,
            utilization=self._utilization(summary.caps),
            updated_at=summary.updated_at,
            updated_by=updated_by,
        )

    def update_risk_caps(
        self,
        changes: Mapping[str, Any],
        *,
        actor: str,
        reason: str = "",
        confirm: bool = False,
    ) -> RiskCapsUpdateResult:
        """Validate and persist a partial cap patch atomically.

        The compare and the write run inside one ``save_risk_caps_change``
        transaction, so a concurrent save cannot record a stale ``before`` or
        lose an update. A stricter patch writes directly; a looser patch without
        ``confirm=True`` raises :class:`SettingValidationError` with one
        ``confirmation_required`` detail per loosened cap and writes nothing. An
        equal effective value is a no-op (no row, no audit).
        """
        normalized = _normalize_patch(changes)
        timestamp = utc_timestamp(self._clock.now())
        captured: dict[str, Any] = {}

        def _build(current_value: str | None) -> tuple[str, AuditEventDraft] | None:
            current, source_before = self._effective_from_persisted(current_value)
            proposed = replace(current, **normalized)
            errors = list(validate_caps(proposed))
            details = _validation_details(errors)
            if details:
                message = "invalid risk caps: " + "; ".join(detail["message"] for detail in details)
                raise SettingValidationError(message, details=details)
            loosened = caps_loosened(current, proposed)
            if proposed == current:
                captured.update(before=current, after=current, loosened=(), changed=False)
                return None
            if loosened and not confirm:
                message = f"loosening {', '.join(loosened)} requires confirmation"
                raise SettingValidationError(
                    message,
                    details=[
                        {
                            "field": field,
                            "code": "confirmation_required",
                            "message": f"loosening {field} requires confirmation",
                            # The server's own diff, so a client never has to
                            # build a stale confirmation table from local state.
                            "before": float(getattr(current, field)),
                            "after": float(getattr(proposed, field)),
                        }
                        for field in loosened
                    ],
                )
            payload = {
                "before": current.to_state(),
                "after": proposed.to_state(),
                "loosened": list(loosened),
                "reason": reason or ("loosening confirmed by operator" if loosened else ""),
                "source_before": source_before.value,
                "source_after": SettingSource.USER.value,
            }
            audit = AuditEventDraft(
                event_type=RISK_CAPS_UPDATED_EVENT,
                actor={"kind": "USER", "id": actor},
                subject={"kind": "SYSTEM", "id": RISK_CAPS_STATE_KEY},
                payload=payload,
                created_at=timestamp,
            )
            captured.update(before=current, after=proposed, loosened=loosened, changed=True)
            return json.dumps(proposed.to_state(), sort_keys=True), audit

        self._store.save_risk_caps_change(_build)
        # Build the response from the values captured inside the write
        # transaction. A post-commit read could hit a corrupt position/account
        # row and turn a committed, audited write into a 500 for the caller.
        after: RiskCaps = captured["after"]
        if captured["changed"]:
            source = SettingSource.USER
            updated_at: str | None = timestamp
            updated_by: str | None = actor
        else:
            # A no-op commits nothing; the committed row (if any) and its newest
            # audit event supply the provenance of the effective caps.
            summary = self.read_risk_summary()
            source = summary.source
            updated_at = summary.updated_at
            audit = self._store.last_audit_event_of_type(RISK_CAPS_UPDATED_EVENT)
            updated_by = _audit_actor_id(audit.payload_json) if audit is not None else None
        # The caps row and its audit event are committed by now, so the
        # best-effort response snapshot must never turn a successful, audited
        # write into a 500. A DB-level or arithmetic failure degrades to an
        # explicitly unavailable utilization instead.
        try:
            utilization = self._utilization(after)
        except Exception:
            _LOGGER.exception("risk-cap utilization snapshot failed after a committed save")
            utilization = unavailable_utilization(after, "utilization snapshot is unavailable")
        settings = RiskSettings(
            caps=after,
            source=source,
            defaults=caps_from_settings(self._settings),
            utilization=utilization,
            updated_at=updated_at,
            updated_by=updated_by,
        )
        return RiskCapsUpdateResult(
            settings=settings,
            before=captured["before"],
            after=after,
            loosened=captured["loosened"],
            changed=captured["changed"],
        )

    def _effective_from_persisted(self, value: str | None) -> tuple[RiskCaps, SettingSource]:
        """Resolve the in-transaction current caps against the env base.

        Uses the same fail-closed structural/range decode as the read path, so
        the write transaction and :func:`effective_caps` can never disagree
        about what a persisted document means. The cross-field rule is
        deliberately not applied here: a legacy document that violates it must
        stay repairable, and the *proposed* document is checked with the full
        rule in ``update_risk_caps``.
        """
        base = caps_from_settings(self._settings)
        if value is None:
            return base, SettingSource.DEFAULT
        return decode_persisted_caps(base, value, cross_field=False), SettingSource.USER

    def _utilization(self, caps: RiskCaps) -> RiskUtilization:
        """Read-only utilization; never seeds or writes anything.

        The defensive read model (corrupt account/position payload policy)
        lives in :mod:`oft.application.risk_utilization`; this method only
        decides whether a snapshot is meaningful for this service.
        """
        if self._settings is None:
            return unavailable_utilization(caps, "paper account is not initialized")
        return build_risk_utilization(self._store, caps)

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

    # -- Telegram allowlists ---------------------------------------------

    def read_scan_report_push(self) -> bool:
        """The ``telegram.scan_report`` toggle; absent means enabled.

        A malformed persisted value raises :class:`ConfigError` so the caller
        (the notification service) can fail closed instead of guessing.
        """
        return self._decode_bool(SCAN_REPORT_PUSH_KEY, default=True)

    def read_failed_scan_alert(self) -> bool:
        """The ``telegram.scan_failed_alert`` toggle; absent means disabled."""
        return self._decode_bool(SCAN_FAILED_ALERT_KEY, default=False)

    def read_trigger_alert(self) -> bool:
        """The ``telegram.trigger_alert`` toggle; absent means enabled.

        Covers both TRIGGERED and INVALIDATED pushes. A malformed persisted
        value raises :class:`ConfigError` so the notification service fails
        closed rather than guessing.
        """
        return self._decode_bool(TRIGGER_ALERT_KEY, default=True)

    def read_entry_recheck_alert(self) -> bool:
        """The ``telegram.entry_recheck_alert`` toggle; absent means enabled."""
        return self._decode_bool(ENTRY_RECHECK_ALERT_KEY, default=True)

    def read_position_push(self) -> bool:
        """The ``telegram.position`` toggle; absent means enabled.

        Position-management executed/blocked outcomes are enabled by default;
        the F10f Telegram settings screen and ``oft telegram toggles`` write
        this toggle.
        """
        return self._decode_bool(POSITION_PUSH_KEY, default=True)

    def read_risk_push(self) -> bool:
        """The ``telegram.risk`` toggle; absent means enabled.

        Daily-loss/leverage/notional and data/provider degradation alerts share
        this toggle. The F10f screen and ``oft telegram toggles`` write it.
        """
        return self._decode_bool(RISK_PUSH_KEY, default=True)

    def read_budget_push(self) -> bool:
        """The ``telegram.budget`` toggle; absent means enabled.

        Budget-threshold alerts are time-sensitive; the F10f screen and
        ``oft telegram toggles`` write this toggle. They never stop a call, so a
        delivery failure only loses the push.
        """
        return self._decode_bool(BUDGET_PUSH_KEY, default=True)

    def _decode_bool(self, key: str, *, default: bool) -> bool:
        return self._decode_bool_record(self._store.get_config(key), key, default=default)

    @staticmethod
    def _decode_bool_record(record: ConfigRecord | None, key: str, *, default: bool) -> bool:
        if record is None:
            return default
        try:
            value = json.loads(record.value_json)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"persisted {key} is unreadable") from exc
        if not isinstance(value, bool):
            raise ConfigError(f"persisted {key} must be a boolean")
        return value

    def read_telegram_settings(self) -> TelegramSettings:
        """The allowlists, six push toggles, and their newest provenance.

        ``source`` is ``default`` only when none of the managed Telegram settings
        rows exists (poll offsets/heartbeats are runtime state, not settings). A
        malformed persisted toggle fails closed as :class:`ConfigError`, exactly
        like the individual enqueue-time reads.
        """
        allowlist = self.read_telegram_allowlist()
        toggles = {
            name: self._decode_bool(key, default=default)
            for name, key, default in TELEGRAM_PUSH_TOGGLES
        }
        records = [
            record
            for record in (self._store.get_config(key) for key in TELEGRAM_SETTINGS_KEYS)
            if record is not None
        ]
        if not records:
            return TelegramSettings(
                chat_ids=allowlist.chat_ids,
                allowed_user_ids=allowlist.allowed_user_ids,
                toggles=toggles,
                source=SettingSource.DEFAULT,
                updated_at=None,
                updated_by=None,
            )
        latest = max(records, key=lambda record: record.updated_at)
        return TelegramSettings(
            chat_ids=allowlist.chat_ids,
            allowed_user_ids=allowlist.allowed_user_ids,
            toggles=toggles,
            source=SettingSource.USER,
            updated_at=latest.updated_at,
            updated_by=latest.updated_by,
        )

    def update_telegram_settings(
        self,
        *,
        chat_ids: object = _UNSET,
        allowed_user_ids: object = _UNSET,
        toggles: object = _UNSET,
        actor: str,
        reason: str = "",
    ) -> TelegramSettings:
        """Validate and persist a partial Telegram settings patch atomically.

        Every changed config row and exactly one ``TELEGRAM_SETTINGS_UPDATED``
        audit event commit in one ``BEGIN IMMEDIATE`` transaction. An unchanged
        patch is a deterministic no-op (no row, no audit). If any toggle is
        enabled the proposed chat list must be non-empty; an all-off state with
        no chats is allowed.
        """
        proposed_chats: tuple[int, ...] | None = None
        if chat_ids is not _UNSET:
            proposed_chats = _normalize_telegram_ids(
                chat_ids,
                field="chat_ids",
                maximum=MAX_TELEGRAM_CHAT_IDS,
                allow_negative=True,
            )
        proposed_users: tuple[int, ...] | None = None
        if allowed_user_ids is not _UNSET:
            proposed_users = _normalize_telegram_ids(
                allowed_user_ids,
                field="allowed_user_ids",
                maximum=MAX_TELEGRAM_ALLOWED_USER_IDS,
            )
        toggle_patch: dict[str, bool] | None = None
        if toggles is not _UNSET:
            toggle_patch = _normalize_telegram_toggles(toggles)
        if proposed_chats is None and proposed_users is None and not toggle_patch:
            message = "no Telegram setting was provided to set"
            raise SettingValidationError(
                message,
                details=[{"field": None, "code": "empty", "message": message}],
            )
        timestamp = utc_timestamp(self._clock.now())

        def _build(
            rows: Mapping[str, ConfigRecord | None],
        ) -> tuple[list[ConfigRecord], AuditEventDraft] | None:
            before_chats = self._decode_allowlist(
                rows[TELEGRAM_CHAT_IDS_KEY],
                field="chat_ids",
                maximum=MAX_TELEGRAM_CHAT_IDS,
                allow_negative=True,
            )
            before_users = self._decode_allowlist(
                rows[TELEGRAM_ALLOWED_USER_IDS_KEY],
                field="allowed_user_ids",
                maximum=MAX_TELEGRAM_ALLOWED_USER_IDS,
            )
            before_toggles = {
                name: self._decode_bool_record(rows[key], key, default=default)
                for name, key, default in TELEGRAM_PUSH_TOGGLES
            }
            after_chats = before_chats if proposed_chats is None else proposed_chats
            after_users = before_users if proposed_users is None else proposed_users
            after_toggles = {**before_toggles, **(toggle_patch or {})}
            if any(after_toggles.values()) and not after_chats:
                message = (
                    "at least one Telegram chat id is required while any push toggle is enabled"
                )
                raise TelegramValidationError(
                    message,
                    details=[
                        {
                            "field": "chat_ids",
                            "code": "chat_required_for_pushes",
                            "message": message,
                        }
                    ],
                )
            records: list[ConfigRecord] = []
            if after_chats != before_chats:
                records.append(
                    ConfigRecord(
                        key=TELEGRAM_CHAT_IDS_KEY,
                        value_json=json.dumps(list(after_chats)),
                        updated_at=timestamp,
                        updated_by=actor,
                    )
                )
            if after_users != before_users:
                records.append(
                    ConfigRecord(
                        key=TELEGRAM_ALLOWED_USER_IDS_KEY,
                        value_json=json.dumps(list(after_users)),
                        updated_at=timestamp,
                        updated_by=actor,
                    )
                )
            for name, key, _default in TELEGRAM_PUSH_TOGGLES:
                if after_toggles[name] != before_toggles[name]:
                    records.append(
                        ConfigRecord(
                            key=key,
                            value_json=json.dumps(after_toggles[name]),
                            updated_at=timestamp,
                            updated_by=actor,
                        )
                    )
            if not records:
                return None
            audit = AuditEventDraft(
                event_type=TELEGRAM_SETTINGS_UPDATED,
                actor={"kind": "USER", "id": actor},
                subject={"kind": "SYSTEM", "id": TELEGRAM_ALLOWLIST_SECTION},
                payload={
                    "before": {
                        "chat_ids": list(before_chats),
                        "allowed_user_ids": list(before_users),
                        "toggles": before_toggles,
                    },
                    "after": {
                        "chat_ids": list(after_chats),
                        "allowed_user_ids": list(after_users),
                        "toggles": after_toggles,
                    },
                    "reason": reason,
                },
                created_at=timestamp,
            )
            return records, audit

        self._store.save_telegram_settings_change(TELEGRAM_SETTINGS_KEYS, _build)
        return self.read_telegram_settings()

    def read_telegram_allowlist(self) -> TelegramAllowlistSettings:
        """The two bounded id lists and their newest provenance.

        A missing key reads as an empty list. A malformed persisted value is a
        typed :class:`ConfigError` (never silently emptied), so an operator
        cannot lose an allowlist to a corrupt row.
        """
        chat_record = self._store.get_config(TELEGRAM_CHAT_IDS_KEY)
        user_record = self._store.get_config(TELEGRAM_ALLOWED_USER_IDS_KEY)
        chat_ids = self._decode_allowlist(
            chat_record,
            field="chat_ids",
            maximum=MAX_TELEGRAM_CHAT_IDS,
            allow_negative=True,
        )
        allowed_user_ids = self._decode_allowlist(
            user_record, field="allowed_user_ids", maximum=MAX_TELEGRAM_ALLOWED_USER_IDS
        )
        timestamps = [
            record.updated_at for record in (chat_record, user_record) if record is not None
        ]
        updated_at = max(timestamps) if timestamps else None
        updated_by = None
        for record in (chat_record, user_record):
            if record is not None:
                updated_by = record.updated_by
        return TelegramAllowlistSettings(
            chat_ids=chat_ids,
            allowed_user_ids=allowed_user_ids,
            updated_at=updated_at,
            updated_by=updated_by,
        )

    def _decode_allowlist(
        self,
        record: ConfigRecord | None,
        *,
        field: str,
        maximum: int,
        allow_negative: bool = False,
    ) -> tuple[int, ...]:
        if record is None:
            return ()
        try:
            value = json.loads(record.value_json)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"persisted telegram {field} is unreadable") from exc
        try:
            return _normalize_telegram_ids(
                value, field=field, maximum=maximum, allow_negative=allow_negative
            )
        except SettingValidationError as exc:
            raise ConfigError(f"persisted telegram {field} is invalid: {exc.message}") from exc

    def update_telegram_allowlist(
        self,
        chat_ids: object,
        allowed_user_ids: object,
        *,
        actor: str,
        reason: str = "",
    ) -> TelegramAllowlistSettings:
        """Validate and persist both allowlists atomically.

        Both ``config`` rows and exactly one ``TELEGRAM_ALLOWLIST_UPDATED``
        audit event commit in one ``BEGIN IMMEDIATE`` transaction. An unchanged
        pair is a deterministic no-op (no row, no audit).
        """
        proposed_chats = _normalize_telegram_ids(
            chat_ids,
            field="chat_ids",
            maximum=MAX_TELEGRAM_CHAT_IDS,
            allow_negative=True,
        )
        proposed_users = _normalize_telegram_ids(
            allowed_user_ids,
            field="allowed_user_ids",
            maximum=MAX_TELEGRAM_ALLOWED_USER_IDS,
        )
        timestamp = utc_timestamp(self._clock.now())

        def _build(
            chat_current: ConfigRecord | None,
            user_current: ConfigRecord | None,
        ) -> tuple[ConfigRecord, ConfigRecord, AuditEventDraft] | None:
            before_chats = self._decode_allowlist(
                chat_current,
                field="chat_ids",
                maximum=MAX_TELEGRAM_CHAT_IDS,
                allow_negative=True,
            )
            before_users = self._decode_allowlist(
                user_current,
                field="allowed_user_ids",
                maximum=MAX_TELEGRAM_ALLOWED_USER_IDS,
            )
            if before_chats == proposed_chats and before_users == proposed_users:
                return None
            chat_record = ConfigRecord(
                key=TELEGRAM_CHAT_IDS_KEY,
                value_json=json.dumps(list(proposed_chats)),
                updated_at=timestamp,
                updated_by=actor,
            )
            user_record = ConfigRecord(
                key=TELEGRAM_ALLOWED_USER_IDS_KEY,
                value_json=json.dumps(list(proposed_users)),
                updated_at=timestamp,
                updated_by=actor,
            )
            audit = AuditEventDraft(
                event_type=TELEGRAM_ALLOWLIST_UPDATED,
                actor={"kind": "USER", "id": actor},
                subject={"kind": "SYSTEM", "id": TELEGRAM_ALLOWLIST_SECTION},
                payload={
                    "before": {
                        "chat_ids": list(before_chats),
                        "allowed_user_ids": list(before_users),
                    },
                    "after": {
                        "chat_ids": list(proposed_chats),
                        "allowed_user_ids": list(proposed_users),
                    },
                    "reason": reason,
                },
                created_at=timestamp,
            )
            return chat_record, user_record, audit

        self._store.save_telegram_allowlist_change(_build)
        return self.read_telegram_allowlist()


def normalize_telegram_ids(
    value: object, *, field: str, maximum: int, allow_negative: bool = False
) -> tuple[int, ...]:
    """Validate one bounded allowlist of nonzero integer ids.

    Chat ids may be negative (Telegram group/supergroup chats); user ids are
    always positive. Bools (which are ``int`` subclasses), strings, floats, and
    zero are rejected with a field-scoped detail. The accepted values are sorted
    and deduplicated so the persisted document is canonical.

    This is the single allowlist validator; the API, the CLI parser, and every
    write path reuse it so the rules cannot drift.
    """
    kind = "nonzero integer" if allow_negative else "positive integer"
    if not isinstance(value, (list, tuple)):
        raise TelegramValidationError(
            f"{field} must be a list of {kind} ids",
            details=[
                {
                    "field": field,
                    "code": "not_a_list",
                    "message": f"{field} must be a list of {kind} ids",
                }
            ],
        )
    details: list[dict[str, Any]] = []
    ids: set[int] = set()
    for item in value:
        valid = (
            not isinstance(item, bool)
            and isinstance(item, int)
            and (item != 0 if allow_negative else item > 0)
        )
        if not valid:
            details.append(
                {
                    "field": field,
                    "code": "invalid_id",
                    "message": f"{field} must contain only {kind} ids",
                }
            )
            continue
        ids.add(item)
    if len(ids) > maximum:
        details.append(
            {
                "field": field,
                "code": "too_many",
                "message": f"{field} must contain at most {maximum} ids",
            }
        )
    if details:
        raise TelegramValidationError(f"invalid telegram {field}", details=details)
    return tuple(sorted(ids))


# Backwards-compatible private alias; new callers should use the public name.
_normalize_telegram_ids = normalize_telegram_ids


def _normalize_telegram_toggles(value: object) -> dict[str, bool]:
    """Validate a partial toggle patch; unknown names and non-bools are rejected."""
    if not isinstance(value, Mapping):
        message = "toggles must be an object of boolean switches"
        raise TelegramValidationError(
            message,
            details=[{"field": "toggles", "code": "not_an_object", "message": message}],
        )
    unknown = sorted(set(value) - set(TELEGRAM_PUSH_TOGGLE_NAMES))
    if unknown:
        raise TelegramValidationError(
            "unknown Telegram toggle(s): " + ", ".join(str(name) for name in unknown),
            details=[
                {
                    "field": "toggles",
                    "code": "unknown_toggle",
                    "message": f"unknown Telegram toggle {name}",
                }
                for name in unknown
            ],
        )
    details = [
        {
            "field": "toggles",
            "code": "invalid_toggle",
            "message": f"{name} must be a boolean",
        }
        for name, enabled in value.items()
        if not isinstance(enabled, bool)
    ]
    if details:
        raise TelegramValidationError("invalid Telegram toggles", details=details)
    return {str(name): bool(enabled) for name, enabled in value.items()}


def _audit_actor_id(payload_json: str) -> str | None:
    """The ``actor.id`` recorded in one audit event, when readable."""
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    actor = payload.get("actor")
    if not isinstance(actor, dict):
        return None
    actor_id = actor.get("id")
    return None if actor_id is None else str(actor_id)


def _normalize_patch(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a partial cap patch's types and return normalized values.

    Unknown fields, an empty patch, non-numeric/bool/non-finite values, and
    non-integral integer caps are rejected with field-scoped details before any
    write. Range and cross-field checks run against the committed caps inside
    the write transaction, because they depend on the current values.
    """
    if not isinstance(changes, Mapping):
        raise SettingValidationError(
            "risk caps must be an object",
            details=[
                {
                    "field": None,
                    "code": "not_an_object",
                    "message": "risk caps must be an object",
                }
            ],
        )
    if not changes:
        raise SettingValidationError(
            "no risk cap was provided to set",
            details=[
                {
                    "field": None,
                    "code": "empty",
                    "message": "no risk cap was provided to set",
                }
            ],
        )
    unknown = sorted(set(changes) - set(CAP_FIELDS))
    if unknown:
        details = [
            {"field": field, "code": "unknown_field", "message": f"unknown risk cap {field}"}
            for field in unknown
        ]
        raise SettingValidationError(
            "invalid risk caps: unknown field(s): " + ", ".join(unknown), details=details
        )
    normalized: dict[str, Any] = {}
    type_details: list[dict[str, Any]] = []
    for field, value in changes.items():
        try:
            parsed = RiskCaps.from_state({field: value})
        except ValueError as exc:
            type_details.append({"field": field, "code": "invalid_type", "message": str(exc)})
            continue
        normalized[field] = getattr(parsed, field)
    if type_details:
        raise SettingValidationError(
            "invalid risk caps: " + "; ".join(detail["message"] for detail in type_details),
            details=type_details,
        )
    return normalized


def _validation_details(errors: list[str]) -> list[dict[str, Any]]:
    """Map ``validate_caps`` messages to the cap field they name."""
    details: list[dict[str, Any]] = []
    for message in errors:
        field = next((name for name in CAP_FIELDS if message.startswith(name)), None)
        code = (
            "daily_loss_below_per_trade"
            if message == DAILY_LOSS_BELOW_PER_TRADE
            else "invalid_value"
        )
        details.append({"field": field, "code": code, "message": message})
    return details


def resolve_schedule_state_overlay(
    *,
    settings: Settings,
    store: StorePort | None,
    clock: ClockPort,
    now: datetime | None = None,
) -> ScheduleState:
    """The persisted schedule with the saved period overlaid, promoted.

    Resolves the persisted row *before* validating the configured period (M3),
    so a valid saved ``8h`` keeps diagnostics working even when
    ``OFT_SCAN_PERIOD`` is invalid. A non-persisted period comes from the
    supplied ``settings`` object, so a cadence set only in ``.env`` is honored.
    The jitter comes from the same configured values (``OFT_SCAN_JITTER_SECONDS``
    via the environment or ``.env``) and is clamped below the smallest supported
    period, so an out-of-range jitter cannot make a saved period raise from
    ``ScheduleState``. Shared by the daemon/CLI status paths and
    ``/api/v1/status`` (M14).
    """
    persisted: str | None = None
    if store is not None:
        persisted = SettingsService(store=store, clock=clock).persisted_scan_period()
    if persisted is not None:
        default_period = persisted
    else:
        try:
            default_period = resolve_schedule_settings(settings=settings).period
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    jitter = min(resolve_scan_jitter(settings=settings), OVERLAY_MAX_JITTER_SECONDS)
    default = ScheduleState(period=default_period, jitter_seconds_max=jitter)
    state = load_schedule_state(schedule_state_path(settings), default=default)
    resolved_now = now or clock.now()
    promoted, _changed = state.apply_persisted(persisted, now=resolved_now)
    return promoted


__all__ = [
    "ACTIVE_ONBOARDING_STEPS",
    "DEFERRED_ONBOARDING_STEPS",
    "ONBOARDING_PROGRESS_KEY",
    "ONBOARDING_STEPS",
    "OVERLAY_MAX_JITTER_SECONDS",
    "REQUIRED_ONBOARDING_STEPS",
    "RISK_CAPS_UPDATED_EVENT",
    "RISK_SECTION",
    "TELEGRAM_PUSH_TOGGLES",
    "TELEGRAM_PUSH_TOGGLE_NAMES",
    "TELEGRAM_SETTINGS_KEYS",
    "TELEGRAM_SETTINGS_UPDATED",
    "LLMSettings",
    "OnboardingProgress",
    "OnboardingStep",
    "RiskCapsUpdateResult",
    "RiskSettings",
    "RiskSummary",
    "RiskUtilization",
    "RiskUtilizationMetric",
    "ScanSettings",
    "SettingsService",
    "TIMEZONE",
    "TelegramSettings",
    "resolve_effective_llm_settings",
    "resolve_schedule_state_overlay",
    "validate_onboarding_step",
]
