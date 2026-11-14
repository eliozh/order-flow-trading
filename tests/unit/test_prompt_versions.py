"""Prompt templates are immutable, explicitly activated, and restore keeps history."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore, database_path
from oft.application.prompt_service import (
    PromptConflictError,
    PromptNotFoundError,
    PromptService,
    PromptVersionMissingError,
    secret_values_from_settings,
)
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.prompts import (
    DEFAULT_USER_BODIES,
    PromptRole,
    PromptStatus,
    content_hash,
)

_COLLISION = "UNIQUE constraint failed: prompt_versions.prompt_id, prompt_versions.version"

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _service(tmp_path: Path) -> tuple[PromptService, SqliteStore]:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    return PromptService(store=store, clock=FixedClock(NOW)), store


def test_default_templates_are_seeded_as_active_v1(tmp_path: Path) -> None:
    service, _store = _service(tmp_path)

    summaries = {summary.prompt_id: summary for summary in service.list_templates()}

    for prompt_id in ("analysis", "entry_recheck", "analysis:system", "entry_recheck:system"):
        assert prompt_id in summaries
        assert summaries[prompt_id].active_version == 1

    analysis = service.active_for(PromptRole.ANALYSIS)
    assert analysis.prompt_id == "analysis"
    assert analysis.version == 1
    assert analysis.status == PromptStatus.ACTIVE.value
    assert "{{symbol}}" in analysis.body
    assert analysis.system_prompt_id == "analysis:system"
    assert analysis.system_version == 1
    assert analysis.system_body

    entry = service.active_for(PromptRole.ENTRY_RECHECK)
    assert entry.prompt_id == "entry_recheck"
    assert entry.version == 1
    assert "{{trigger}}" in entry.body


def test_edit_creates_a_new_immutable_version_and_keeps_the_old_readable(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    original = store.get_prompt_version("analysis", 1)
    assert original is not None
    new_body = (
        "Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n<market_data>{{metrics}}</market_data>\n"
        "<knowledge>{{knowledge}}</knowledge>\n<schema>{{schema}}</schema>\nNEW WORDING\n"
    )

    created = service.edit("analysis", new_body, author="tester", reason="tighten wording")

    assert created.version == 2
    assert created.status == PromptStatus.DRAFT.value
    assert created.is_active is False
    assert created.body_hash == content_hash(new_body)
    # The old version is still readable and unchanged.
    assert store.get_prompt_version("analysis", 1) is not None
    assert store.get_prompt_version("analysis", 1).body == original.body  # type: ignore[union-attr]
    assert len(service.history("analysis")) == 2
    # Activation was not implied by an edit.
    assert service.active_for(PromptRole.ANALYSIS).version == 1


def test_a_draft_is_never_used_by_a_scan_until_explicitly_activated(
    tmp_path: Path,
) -> None:
    service, _store = _service(tmp_path)
    body = (
        "Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n"
        "<market_data>{{metrics}}</market_data>\n<knowledge>{{knowledge}}</knowledge>\n"
        "<schema>{{schema}}</schema>\nDRAFT MARKER\n"
    )
    service.edit("analysis", body)

    active = service.active_for(PromptRole.ANALYSIS)
    assert active.version == 1
    assert "DRAFT MARKER" not in active.body

    service.activate("analysis", 2)
    activated = service.active_for(PromptRole.ANALYSIS)
    assert activated.version == 2
    assert "DRAFT MARKER" in activated.body


def test_restore_default_creates_a_new_version_instead_of_deleting_history(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    custom = (
        "Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n"
        "<market_data>{{metrics}}</market_data>\n<knowledge>{{knowledge}}</knowledge>\n"
        "<schema>{{schema}}</schema>\nCUSTOM\n"
    )
    service.edit("analysis", custom)
    service.activate("analysis", 2)

    restored = service.restore_default("analysis", author="tester")

    assert restored.version == 3
    assert restored.status == PromptStatus.DRAFT.value
    assert restored.body == DEFAULT_USER_BODIES[PromptRole.ANALYSIS]
    assert store.get_prompt_version("analysis", 2) is not None
    assert [view.version for view in service.history("analysis")] == [1, 2, 3]

    service.activate("analysis", 3)
    assert service.active_for(PromptRole.ANALYSIS).body == DEFAULT_USER_BODIES[PromptRole.ANALYSIS]


def test_changing_the_body_changes_the_content_and_rendered_hashes(tmp_path: Path) -> None:
    service, _store = _service(tmp_path)
    values = {
        "symbol": "BTCUSDT",
        "timeframe": "4h",
        "as_of": "2026-11-08T12:00:00.000Z",
        "metrics": "{}",
        "snapshot": "{}",
        "knowledge": "",
        "schema": "{}",
    }
    first = service.render_active(PromptRole.ANALYSIS, values)
    original_hash = first.prompt_hash

    body = (
        "Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n"
        "<market_data>{{metrics}}</market_data>\n<knowledge>{{knowledge}}</knowledge>\n"
        "<schema>{{schema}}</schema>\nCHANGED BODY\n"
    )
    created = service.edit("analysis", body)
    service.activate("analysis", created.version)
    second = service.render_active(PromptRole.ANALYSIS, values)

    assert created.body_hash != content_hash(DEFAULT_USER_BODIES[PromptRole.ANALYSIS])
    assert second.prompt_hash != original_hash


def test_a_missing_historical_version_reports_missing_and_never_falls_back(
    tmp_path: Path,
) -> None:
    service, _store = _service(tmp_path)
    decision = {
        "provenance": {"prompt_id": "analysis", "prompt_version": 99, "prompt_hash": "abc"},
    }

    resolution = service.resolve_decision(decision)

    assert resolution.status == "missing"
    assert resolution.exact_replay is False
    assert resolution.code == "PROMPT_VERSION_MISSING"
    # It must not return the current active body as a substitute.
    assert resolution.prompt_hash is None

    available = service.resolve_decision(
        {"provenance": {"prompt_id": "analysis", "prompt_version": 1}}
    )
    assert available.status == "available"
    # Without the decision's recorded rendered hash, an exact replay cannot be
    # claimed even though the template version still exists.
    assert available.exact_replay is False


def test_activating_an_unknown_version_fails_loudly(tmp_path: Path) -> None:
    service, _store = _service(tmp_path)

    with pytest.raises(PromptVersionMissingError):
        service.activate("analysis", 99)


def test_system_prompt_is_versioned_separately_from_the_user_template(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    system_before = service.active_for(PromptRole.ANALYSIS).system_version

    service.edit(
        "analysis",
        "Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n"
        "<market_data>{{metrics}}</market_data>\n<knowledge>{{knowledge}}</knowledge>\n"
        "<schema>{{schema}}</schema>\nUSER ONLY\n",
    )

    assert service.active_for(PromptRole.ANALYSIS).system_version == system_before
    system_versions = store.list_prompt_versions("analysis:system")
    assert len(system_versions) == 1
    assert system_versions[0].is_active is True


def test_missing_pinned_system_version_disables_exact_replay(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    service = PromptService(store=store, clock=FixedClock(NOW))
    decision = {
        "provenance": {"prompt_id": "analysis", "prompt_version": 1, "prompt_hash": "recorded"},
    }
    assert service.resolve_decision(decision).exact_replay is True

    connection = sqlite3.connect(database_path(settings))
    try:
        connection.execute(
            "DELETE FROM prompt_versions WHERE prompt_id = 'analysis:system' AND version = 1"
        )
        connection.commit()
    finally:
        connection.close()

    resolution = service.resolve_decision(decision)

    assert resolution.status == "missing"
    assert resolution.exact_replay is False
    assert resolution.code == "PROMPT_VERSION_MISSING"


def test_a_concurrent_version_allocation_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store = _service(tmp_path)
    original = store.save_prompt_version
    calls = {"count": 0}

    def flaky(record):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] == 1:
            # Simulate another writer committing this version between our read
            # of the latest version and our INSERT.
            original(record)
            raise sqlite3.IntegrityError(_COLLISION)
        return original(record)

    monkeypatch.setattr(store, "save_prompt_version", flaky)
    body = (
        "Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n"
        "<market_data>{{metrics}}</market_data>\n<knowledge>{{knowledge}}</knowledge>\n"
        "<schema>{{schema}}</schema>\nRETRIED\n"
    )

    created = service.edit("analysis", body)

    assert created.version == 3
    assert calls["count"] == 2


def test_a_persistent_version_conflict_is_a_clean_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store = _service(tmp_path)
    body = (
        "Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n"
        "<market_data>{{metrics}}</market_data>\n<knowledge>{{knowledge}}</knowledge>\n"
        "<schema>{{schema}}</schema>\nCONFLICT\n"
    )

    def always_conflict(record):  # type: ignore[no-untyped-def]
        raise sqlite3.IntegrityError(_COLLISION)

    monkeypatch.setattr(store, "save_prompt_version", always_conflict)

    with pytest.raises(PromptConflictError) as excinfo:
        service.edit("analysis", body)

    assert excinfo.value.code == "CONFLICT"


def test_a_secret_in_the_body_is_redacted_before_persisting(tmp_path: Path) -> None:
    api_secret = "api-token-super-secret-value"
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data", api_token=api_secret)
    store = SqliteStore(settings)
    store.initialize()
    service = PromptService(
        store=store,
        clock=FixedClock(NOW),
        secret_values=secret_values_from_settings(settings),
    )
    body = (
        "Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n"
        "<market_data>{{metrics}}</market_data>\n<knowledge>{{knowledge}}</knowledge>\n"
        "<schema>{{schema}}</schema>\n"
        f"api_token={api_secret}\nkey: sk-live-abcdefghijklmnop\n"
    )

    created = service.edit(
        "analysis",
        body,
        author=api_secret,
        reason="sk-live-abcdefghijklmnop",
    )

    stored = store.get_prompt_version("analysis", created.version)
    assert stored is not None
    assert api_secret not in stored.body
    assert "sk-live-abcdefghijklmnop" not in stored.body
    assert api_secret not in stored.author
    assert "sk-live-abcdefghijklmnop" not in stored.reason
    assert "[REDACTED]" in stored.body
    # The persisted hash must describe the persisted (redacted) body.
    assert stored.body_hash == content_hash(stored.body)
    assert api_secret not in created.body


def test_a_non_collision_integrity_error_is_not_retried_or_relabelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store = _service(tmp_path)
    calls = {"count": 0}

    def check_failure(record):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        raise sqlite3.IntegrityError("CHECK constraint failed: prompt_versions")

    monkeypatch.setattr(store, "save_prompt_version", check_failure)
    body = (
        "Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n"
        "<market_data>{{metrics}}</market_data>\n<knowledge>{{knowledge}}</knowledge>\n"
        "<schema>{{schema}}</schema>\nCHECK FAILURE\n"
    )

    # A CHECK/NOT NULL/trigger failure is a real bug, not a version race: it is
    # neither retried nor rewritten as a 409 CONFLICT.
    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        service.edit("analysis", body)

    assert "CHECK constraint failed" in str(excinfo.value)
    assert calls["count"] == 1


def test_active_for_fails_closed_when_a_seeded_template_row_is_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service, store = _service(tmp_path)

    with caplog.at_level(logging.ERROR, logger="oft.application.prompt_service"):
        monkeypatch_target = store.get_prompt_template
        store.get_prompt_template = lambda prompt_id: None  # type: ignore[method-assign]
        try:
            with pytest.raises(PromptVersionMissingError):
                service.active_for(PromptRole.ANALYSIS)
            with pytest.raises(PromptVersionMissingError):
                service.active_for(PromptRole.ENTRY_RECHECK)
        finally:
            store.get_prompt_template = monkeypatch_target  # type: ignore[method-assign]

    assert any("seeded" in record.message for record in caplog.records)


def test_active_for_fails_closed_when_the_active_version_row_is_missing(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            "DELETE FROM prompt_versions WHERE prompt_id = 'analysis' AND version = 1"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(PromptVersionMissingError):
        service.active_for(PromptRole.ANALYSIS)


def test_active_for_uses_the_built_in_default_for_a_never_seeded_role_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service, _store = _service(tmp_path)

    with caplog.at_level(logging.INFO, logger="oft.application.prompt_service"):
        active = service.active_for(PromptRole.REVIEW)

    assert active.prompt_id == "review"
    assert active.version == 1
    assert any("built-in" in record.message for record in caplog.records)


def test_active_for_rejects_an_unknown_role_with_a_prompt_error(tmp_path: Path) -> None:
    service, _store = _service(tmp_path)

    with pytest.raises(PromptNotFoundError):
        service.active_for("not_a_role")
    with pytest.raises(PromptNotFoundError):
        service.render_active("not_a_role", {})


def test_activate_redacts_actor_and_reason_in_the_audit_chain(tmp_path: Path) -> None:
    api_secret = "api-token-super-secret-value"
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data", api_token=api_secret)
    store = SqliteStore(settings)
    store.initialize()
    service = PromptService(
        store=store,
        clock=FixedClock(NOW),
        secret_values=secret_values_from_settings(settings),
    )
    body = (
        "Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n"
        "<market_data>{{metrics}}</market_data>\n<knowledge>{{knowledge}}</knowledge>\n"
        "<schema>{{schema}}</schema>\nAUDIT REDACTION\n"
    )
    created = service.edit("analysis", body, author="operator")
    service.activate("analysis", created.version, actor=api_secret, reason=f"token {api_secret}")

    events = store.list_audit_events(limit=10)
    event_types = {event.event_type for event in events}
    assert {"PROMPT_VERSION_CREATED", "PROMPT_VERSION_ACTIVATED"} <= event_types
    audit_text = "\n".join(event.payload_json for event in events)
    assert api_secret not in audit_text
    assert "[REDACTED]" in audit_text


def test_system_prompt_lifecycle_pins_the_active_version_onto_user_versions(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    system_body = "You are a stricter order-flow system prompt."

    system_draft = service.edit("analysis:system", system_body, author="ops")
    assert system_draft.status == PromptStatus.DRAFT.value
    service.activate("analysis:system", system_draft.version)

    user_draft = service.edit(
        "analysis",
        "Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n"
        "<market_data>{{metrics}}</market_data>\n<knowledge>{{knowledge}}</knowledge>\n"
        "<schema>{{schema}}</schema>\nPINNED\n",
    )
    assert user_draft.system_prompt_id == "analysis:system"
    assert user_draft.system_prompt_version == 2
    assert user_draft.system_prompt_hash == content_hash(system_body)

    service.activate("analysis", user_draft.version)
    rendered = service.render_active(
        PromptRole.ANALYSIS,
        {
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "as_of": "2026-11-08T12:00:00.000Z",
            "metrics": "{}",
            "snapshot": "{}",
            "knowledge": "",
            "schema": "{}",
        },
    )
    assert rendered.system == system_body
    assert rendered.system_version == 2

    restored = service.restore_default("analysis:system", author="ops")
    assert restored.version == 3
    assert restored.status == PromptStatus.DRAFT.value
    # Restoring does not rewrite the already pinned user version.
    pinned = store.get_prompt_version("analysis", user_draft.version)
    assert pinned is not None
    assert pinned.system_prompt_version == 2


def test_pre_v16_decision_without_a_pinned_system_version_is_missing(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            """
            INSERT INTO prompt_versions (
                prompt_id, version, body, body_hash, is_active, created_at,
                status, author, reason, is_default
            ) VALUES ('analysis', 9, ?, ?, 0, 't', 'DRAFT', 'raw', 'pre-v16', 0)
            """,
            (
                DEFAULT_USER_BODIES[PromptRole.ANALYSIS],
                content_hash(DEFAULT_USER_BODIES[PromptRole.ANALYSIS]),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    resolution = service.resolve_decision(
        {"provenance": {"prompt_id": "analysis", "prompt_version": 9, "prompt_hash": "abc"}}
    )

    assert resolution.status == "missing"
    assert resolution.exact_replay is False
    assert resolution.code == "PROMPT_VERSION_MISSING"


def test_malformed_required_variables_json_is_logged_not_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service, store = _service(tmp_path)
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            """
            INSERT INTO prompt_versions (
                prompt_id, version, body, body_hash, is_active, created_at,
                required_variables_json, status, author, reason, is_default
            ) VALUES ('analysis', 8, ?, ?, 0, 't', 'not-json', 'DRAFT', 'raw', 'bad', 0)
            """,
            (
                DEFAULT_USER_BODIES[PromptRole.ANALYSIS],
                content_hash(DEFAULT_USER_BODIES[PromptRole.ANALYSIS]),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with caplog.at_level(logging.WARNING, logger="oft.application.prompt_service"):
        detail = service.show("analysis")

    assert any(version.version == 8 for version in detail.versions)
    assert any("required_variables_json" in record.message for record in caplog.records)
