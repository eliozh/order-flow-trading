"""Pure helpers behind the F10b prompt settings aggregate (slice 36).

The aggregate is list-only and reads the active version through its own
``PromptService.prompt_settings_rows`` path. These tests pin the role order,
the built-in-default detection, the active-version read fields, and that the
legacy ``PromptSummary`` / CLI JSON key set never drifts.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.prompt_service import (
    PromptService,
    is_builtin_default,
)
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.prompts import (
    REQUIRED_VARIABLES,
    PromptRole,
)

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)

LEGACY_PROMPT_SUMMARY_KEYS = {
    "prompt_id",
    "role",
    "kind",
    "active_version",
    "version_count",
    "latest_version",
    "updated_at",
}

_CUSTOM_ANALYSIS_BODY = """\
Symbol: {{symbol}}
Timeframe: {{timeframe}}

<market_data>
{{metrics}}
</market_data>

<knowledge>
{{knowledge}}
</knowledge>

CUSTOM EDITOR BODY
Respond with JSON matching this schema:
{{schema}}
"""


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _service(tmp_path: Path) -> tuple[PromptService, SqliteStore]:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    return PromptService(store=store, clock=FixedClock(NOW)), store


def test_prompt_settings_rows_returns_the_four_user_roles_in_order(
    tmp_path: Path,
) -> None:
    service, _store = _service(tmp_path)

    rows = service.prompt_settings_rows()

    assert [row.role for row in rows] == [
        PromptRole.ANALYSIS,
        PromptRole.ENTRY_RECHECK,
        PromptRole.MANAGEMENT,
        PromptRole.REVIEW,
    ]
    assert [row.prompt_id for row in rows] == [
        "analysis",
        "entry_recheck",
        "management",
        "review",
    ]
    # System prompts are versioned separately and never enter the aggregate.
    assert all(row.prompt_id.endswith(":system") is False for row in rows)


def test_prompt_settings_rows_maps_active_read_fields(tmp_path: Path) -> None:
    service, _store = _service(tmp_path)
    created = service.edit("analysis", _CUSTOM_ANALYSIS_BODY, author="operator")
    service.activate("analysis", created.version, actor="operator")

    row = next(item for item in service.prompt_settings_rows() if item.prompt_id == "analysis")

    assert row.active_version == created.version
    assert row.author == "operator"
    assert row.updated_at.endswith("Z")
    assert row.version_count == 2
    assert row.latest_version == created.version
    assert row.required_variables == REQUIRED_VARIABLES[PromptRole.ANALYSIS]
    assert row.is_builtin_default is False


@pytest.mark.parametrize(
    ("active_is_default", "version_count", "expected"),
    [
        (True, 1, True),
        (True, 2, False),
        (False, 1, False),
        (False, 5, False),
    ],
)
def test_is_builtin_default_requires_a_seeded_active_version_and_one_version(
    active_is_default: bool, version_count: int, expected: bool
) -> None:
    assert (
        is_builtin_default(
            active_is_default=active_is_default,
            version_count=version_count,
        )
        is expected
    )


def test_list_templates_returns_the_legacy_prompt_summary_shape(
    tmp_path: Path,
) -> None:
    service, _store = _service(tmp_path)
    service.edit("analysis", _CUSTOM_ANALYSIS_BODY, author="tester")

    summaries = service.list_templates()

    assert summaries
    for summary in summaries:
        # The F10b read fields live only on the aggregate path; this legacy
        # shape is what `/api/v1/prompts` and `oft prompt list --json` expose.
        assert set(asdict(summary)) == LEGACY_PROMPT_SUMMARY_KEYS
        assert not hasattr(summary, "active_author")
        assert not hasattr(summary, "active_is_default")
        assert not hasattr(summary, "active_required_variables")


def test_cli_prompt_list_json_keys_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from oft.cli.main import app as cli_app

    # Initialize and seed the same data directory the CLI will open.
    SqliteStore(_settings(tmp_path)).initialize()
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))

    result = CliRunner().invoke(cli_app, ["prompt", "list", "--json"])

    assert result.exit_code == 0, result.output
    items = json.loads(result.stdout)["data"]["items"]
    assert items
    for item in items:
        assert set(item) == LEGACY_PROMPT_SUMMARY_KEYS


def test_prompt_settings_rows_track_the_active_version_not_a_draft(
    tmp_path: Path,
) -> None:
    service, _store = _service(tmp_path)

    created = service.edit("analysis", _CUSTOM_ANALYSIS_BODY, author="tester", reason="wording")

    (draft_row,) = [row for row in service.prompt_settings_rows() if row.prompt_id == "analysis"]
    # The custom body is a draft: the active read fields still describe v1, but
    # the presence of a custom version means the family is no longer a pure
    # built-in default.
    assert draft_row.active_version == 1
    assert draft_row.author == "system"
    assert draft_row.version_count == 2
    assert draft_row.latest_version == created.version
    assert draft_row.is_builtin_default is False

    service.activate("analysis", created.version, actor="tester")
    activated = next(row for row in service.prompt_settings_rows() if row.prompt_id == "analysis")
    assert activated.active_version == created.version
    assert activated.author == "tester"
    assert activated.version_count == 2
    assert activated.latest_version == created.version
    assert activated.is_builtin_default is False


def test_prompt_settings_rows_survive_a_missing_active_version_row(
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

    analysis = next(row for row in service.prompt_settings_rows() if row.prompt_id == "analysis")

    # The aggregate is a read surface: a damaged row must not crash it and must
    # not invent an author or a default marker.
    assert analysis.active_version == 1
    assert analysis.author == ""
    assert analysis.required_variables == REQUIRED_VARIABLES[PromptRole.ANALYSIS]
    assert analysis.is_builtin_default is False


def test_prompt_settings_rows_never_include_the_persisted_body(tmp_path: Path) -> None:
    service, _store = _service(tmp_path)
    body = "api_token=super-secret-token-value"
    service.edit("analysis", _CUSTOM_ANALYSIS_BODY + f"\n{body}\n", author="tester")

    rows = service.prompt_settings_rows()

    assert len(rows) == 4
    payload = repr(rows)
    # The aggregate carries metadata only: no body, hash, or secret marker.
    for row in rows:
        assert not hasattr(row, "body")
    assert "super-secret-token-value" not in payload
    assert body not in payload
