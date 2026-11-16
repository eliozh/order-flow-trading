"""Paper-only onboarding progress (slice 44, F01).

Completed first-run steps are persisted under one config key
(``onboarding.progress``) as ``{"completed_steps": [ascending ints]}``. No secret,
token, or free-form request text can enter the row: the only persisted value is
the step integer. Steps 3 (Binance) and 4 (Telegram) are deferred to Milestones
5 and 6 and can never be completed here.

The logic lives in a mixin so :class:`oft.application.settings_service.SettingsService`
keeps its public surface (and its established ``LLMSettingsMixin`` precedent)
without growing past the repository's source-file ceiling.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from oft.config import ConfigError
from oft.domain.onboarding import OnboardingStepId
from oft.domain.settings import (
    CONFIG_SUBJECT_KIND,
    CONFIG_UPDATED_EVENT,
    SettingSource,
    SettingValidationError,
    scoped_diff,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import AuditEventDraft, ConfigRecord, StorePort

ONBOARDING_PROGRESS_KEY = "onboarding.progress"


@dataclass(frozen=True)
class OnboardingStep:
    """One first-run step and whether this slice can persist it."""

    step: int
    id: OnboardingStepId
    title: str
    deferred: bool
    deferred_milestone: int | None


# The five F01 steps. Steps 3 (Binance) and 4 (Telegram) are visibly deferred
# to their milestones and can never be completed or persisted here.
ONBOARDING_STEPS: tuple[OnboardingStep, ...] = (
    OnboardingStep(1, "environment", "环境与本地绑定", False, None),
    OnboardingStep(2, "llm", "LLM providers", False, None),
    OnboardingStep(3, "binance", "Binance 密钥与权限", True, 5),
    OnboardingStep(4, "telegram", "Telegram 只读通道", True, 6),
    OnboardingStep(5, "watchlist", "Watchlist、周期与预算", False, None),
)
ACTIVE_ONBOARDING_STEPS: tuple[int, ...] = tuple(
    step.step for step in ONBOARDING_STEPS if not step.deferred
)
DEFERRED_ONBOARDING_STEPS: tuple[int, ...] = tuple(
    step.step for step in ONBOARDING_STEPS if step.deferred
)
# Immutable so an accidental write fails loudly instead of changing validation.
_ONBOARDING_STEP_MILESTONES: Mapping[int, int] = MappingProxyType(
    {
        step.step: step.deferred_milestone
        for step in ONBOARDING_STEPS
        if step.deferred and step.deferred_milestone is not None
    }
)


@dataclass(frozen=True)
class OnboardingProgress:
    """Completed first-run steps plus the server-derived resume point."""

    steps: tuple[OnboardingStep, ...]
    completed_steps: tuple[int, ...]
    resumed_step: int
    all_complete: bool
    updated_at: str | None
    updated_by: str | None

    def to_contract(self) -> dict[str, Any]:
        completed = set(self.completed_steps)
        return {
            "steps": [
                {
                    "step": step.step,
                    "id": step.id,
                    "title": step.title,
                    "deferred": step.deferred,
                    "deferred_milestone": step.deferred_milestone,
                    "completed": step.step in completed,
                }
                for step in self.steps
            ],
            "completed_steps": list(self.completed_steps),
            "resumed_step": self.resumed_step,
            "all_complete": self.all_complete,
            # Onboarding never enables Live; the environment is always Paper.
            "environment": "paper",
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


class OnboardingProgressMixin:
    """Read/write the ``onboarding.progress`` document.

    The concrete service supplies ``_store`` and ``_clock`` (see
    :class:`oft.application.settings_service.SettingsService`). A real change
    writes one config row and one ``CONFIG_UPDATED`` audit atomically; an
    already-completed step is a deterministic no-op.
    """

    _store: StorePort
    _clock: ClockPort

    def _secrets(self) -> Any:  # pragma: no cover - supplied by LLMSettingsMixin
        """The secret reader supplied by ``LLMSettingsMixin`` on the host.

        The MRO puts ``LLMSettingsMixin`` first, so the concrete
        :class:`SettingsService` never executes this stub; it exists only so
        the type checker accepts the prerequisite check below.
        """
        raise NotImplementedError

    def read_onboarding_progress(self) -> OnboardingProgress:
        """Completed first-run steps; a fresh install needs no config row."""
        record = self._store.get_config(ONBOARDING_PROGRESS_KEY)
        if record is None:
            return _build_onboarding_progress((), None, None)
        completed = _decode_onboarding_progress(record)
        return _build_onboarding_progress(completed, record.updated_at, record.updated_by)

    def complete_onboarding_step(self, step: object, *, actor: str) -> OnboardingProgress:
        """Persist one active step, auditing only a real change.

        Deferred steps (3/4) and unknown steps are rejected with
        ``VALIDATION_FAILED`` before any write. The persisted document is read
        and decoded first, so a corrupt document fails closed as
        ``CONFIG_INVALID`` and is never overwritten. Completing an
        already-completed step is a deterministic no-op (no row, no audit, no
        prerequisite re-evaluation). Only a step about to be persisted must
        satisfy its server-side prerequisite (step 2: a configured LLM
        provider; step 5: at least one starred symbol), so progress cannot
        claim a state the underlying settings do not support; a failed
        prerequisite writes nothing.
        """
        normalized = validate_onboarding_step(step)
        # Read/decode before the prerequisite check so a corrupt document fails
        # as CONFIG_INVALID (never as a prerequisite refusal), and so a repeat
        # of an already-recorded step stays the documented no-op even if its
        # prerequisite has since lapsed.
        current = self._store.get_config(ONBOARDING_PROGRESS_KEY)
        completed = () if current is None else _decode_onboarding_progress(current)
        if normalized in completed:
            return self.read_onboarding_progress()
        self._check_step_prerequisites(normalized)
        timestamp = utc_timestamp(self._clock.now())

        def _build(
            current: ConfigRecord | None,
        ) -> tuple[ConfigRecord, AuditEventDraft] | None:
            before = () if current is None else _decode_onboarding_progress(current)
            if normalized in before:
                return None
            after = tuple(sorted({*before, normalized}))
            record = ConfigRecord(
                key=ONBOARDING_PROGRESS_KEY,
                value_json=json.dumps({"completed_steps": list(after)}),
                updated_at=timestamp,
                updated_by=actor,
            )
            source_before = SettingSource.DEFAULT if current is None else SettingSource.USER
            payload = {
                **scoped_diff(
                    key=ONBOARDING_PROGRESS_KEY,
                    before=list(before),
                    after=list(after),
                ),
                "source_before": source_before.value,
                "source_after": SettingSource.USER.value,
            }
            audit = AuditEventDraft(
                event_type=CONFIG_UPDATED_EVENT,
                actor={"kind": "USER", "id": actor},
                subject={"kind": CONFIG_SUBJECT_KIND, "id": ONBOARDING_PROGRESS_KEY},
                payload=payload,
                created_at=timestamp,
            )
            return record, audit

        self._store.save_config_change(ONBOARDING_PROGRESS_KEY, _build)
        # Read after the commit so the response reflects the committed row and
        # the server-derived resume point.
        return self.read_onboarding_progress()

    def _check_step_prerequisites(self, step: int) -> None:
        """Refuse a completion the underlying settings do not support.

        Progress is not a client-declared claim: step 2 needs a configured LLM
        provider (the same secret metadata the UI reads) and step 5 needs at
        least one starred symbol. A failure raises before the store is touched,
        so nothing is written and the idempotency reservation is released by
        the route helper. Step order is deliberately not enforced.
        """
        if step == 2:
            configured = any(status.configured for status in self._secrets().list_statuses())
            if not configured:
                message = (
                    "step 2 requires at least one configured LLM provider; "
                    "add a provider key before completing this step"
                )
                raise SettingValidationError(
                    message,
                    details=[
                        {
                            "field": "step",
                            "code": "step_prerequisite_missing",
                            "message": message,
                        }
                    ],
                )
        if step == 5 and self._store.watchlist_snapshot(limit=1).starred_count < 1:
            message = (
                "step 5 requires at least one starred watchlist symbol; "
                "star a symbol before completing this step"
            )
            raise SettingValidationError(
                message,
                details=[
                    {
                        "field": "step",
                        "code": "step_prerequisite_missing",
                        "message": message,
                    }
                ],
            )


def validate_onboarding_step(value: object) -> int:
    """Return a persistable step or raise a field-scoped validation error.

    Only steps 1, 2, and 5 are configurable in this slice. Steps 3 and 4 are
    deferred to Milestones 5 and 6 and must never be persisted; treating them
    as ordinary validation failures keeps the progress document honest.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        message = "onboarding step must be an integer 1..5"
        raise SettingValidationError(
            message,
            details=[{"field": "step", "code": "not_an_integer", "message": message}],
        )
    if value < 1 or value > 5:
        message = "onboarding step must be an integer 1..5"
        raise SettingValidationError(
            message,
            details=[{"field": "step", "code": "out_of_range", "message": message}],
        )
    if value in DEFERRED_ONBOARDING_STEPS:
        milestone = _ONBOARDING_STEP_MILESTONES[value]
        message = (
            f"onboarding step {value} is deferred to Milestone {milestone}; "
            "only steps 1, 2 and 5 are configurable in this slice"
        )
        raise SettingValidationError(
            message,
            details=[{"field": "step", "code": "step_deferred", "message": message}],
        )
    return value


def _decode_onboarding_progress(record: ConfigRecord) -> tuple[int, ...]:
    """Decode ``{"completed_steps": [...]}``; never guess a corrupt document.

    A persisted deferred step (3/4) is dropped rather than rejected: this service
    can never write one, so a hand-seeded row must not resume or complete a
    deferred step. Every other malformed shape fails closed as ``CONFIG_INVALID``.
    """
    try:
        value = json.loads(record.value_json)
    except (TypeError, ValueError) as exc:
        raise ConfigError("persisted onboarding progress is unreadable") from exc
    if not isinstance(value, dict):
        raise ConfigError("persisted onboarding progress is unreadable")
    raw_steps = value.get("completed_steps")
    if not isinstance(raw_steps, list):
        raise ConfigError("persisted onboarding progress is unreadable")
    steps: set[int] = set()
    for item in raw_steps:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ConfigError("persisted onboarding progress is unreadable")
        if item < 1 or item > 5:
            raise ConfigError("persisted onboarding progress is unreadable")
        # A deferred step can never have been persisted by this service; drop
        # it defensively so a hand-seeded row cannot resume or complete one.
        if item in ACTIVE_ONBOARDING_STEPS:
            steps.add(item)
    return tuple(sorted(steps))


def _build_onboarding_progress(
    completed: tuple[int, ...],
    updated_at: str | None,
    updated_by: str | None,
) -> OnboardingProgress:
    completed_set = set(completed)
    incomplete = [step for step in ACTIVE_ONBOARDING_STEPS if step not in completed_set]
    all_complete = not incomplete
    # All active steps complete still resumes at 5 (the last active step) so a
    # resumed wizard lands on the review rather than a deferred step.
    resumed_step = incomplete[0] if incomplete else ONBOARDING_STEPS[-1].step
    return OnboardingProgress(
        steps=ONBOARDING_STEPS,
        completed_steps=completed,
        resumed_step=resumed_step,
        all_complete=all_complete,
        updated_at=updated_at,
        updated_by=updated_by,
    )


__all__ = [
    "ACTIVE_ONBOARDING_STEPS",
    "DEFERRED_ONBOARDING_STEPS",
    "ONBOARDING_PROGRESS_KEY",
    "ONBOARDING_STEPS",
    "OnboardingProgress",
    "OnboardingProgressMixin",
    "OnboardingStep",
    "validate_onboarding_step",
]
