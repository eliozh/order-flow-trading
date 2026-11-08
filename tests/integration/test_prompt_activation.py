"""Prompt activation through the API, scans, and the CLI (draft -> active)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from oft.adapters.llm_stub import LLMStub
from oft.adapters.sqlite_store import database_path
from oft.api.app import create_app
from oft.application.prompt_service import PromptService
from oft.application.scan_service import ScanRequest, ScanService
from oft.bootstrap import build_container
from oft.cli.main import app as cli_app
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import CostEstimate, LLMRequest, LLMResult, ProviderCapabilities

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 10, 8, tzinfo=UTC)

_NEW_ANALYSIS_BODY = """\
Symbol: {{symbol}}
Timeframe: {{timeframe}}

<market_data>
{{metrics}}
</market_data>

<knowledge>
{{knowledge}}
</knowledge>

ACTIVE MARKER: review the auction state.
Respond with JSON matching this schema:
{{schema}}
"""


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


class _CapturingProvider:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []
        self._stub = LLMStub(fixtures_dir=FIXTURES)

    def capabilities(self) -> ProviderCapabilities:
        return self._stub.capabilities()

    def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        return self._stub.complete(request)

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate:
        return self._stub.estimate_cost(request, model)


def test_api_lists_shows_and_activates_prompt_versions(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        listed = client.get("/api/v1/prompts", headers=auth)
        assert listed.status_code == 200, listed.text
        items = listed.json()["data"]["items"]
        assert {item["prompt_id"] for item in items} >= {
            "analysis",
            "entry_recheck",
            "analysis:system",
            "entry_recheck:system",
        }
        analysis = next(item for item in items if item["prompt_id"] == "analysis")
        assert analysis["role"] == "ANALYSIS"
        assert analysis["active_version"] == 1

        created = client.post(
            "/api/v1/prompts/analysis/versions",
            json={"body": _NEW_ANALYSIS_BODY, "author": "tester", "reason": "wording"},
            headers={**auth, "Idempotency-Key": "prompt-edit-1"},
        )
        assert created.status_code == 201, created.text
        created_data = created.json()["data"]
        assert created_data["version"] == 2
        assert created_data["status"] == "DRAFT"
        assert created_data["is_active"] is False

        # A draft is not active and is not used.
        shown = client.get("/api/v1/prompts/analysis", headers=auth).json()["data"]
        assert shown["active_version"] == 1
        assert len(shown["versions"]) == 2

        activated = client.post(
            "/api/v1/prompts/analysis/activate",
            json={"version": 2, "reason": "approved"},
            headers={**auth, "Idempotency-Key": "prompt-activate-1"},
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["data"]["version"] == 2
        assert activated.json()["data"]["status"] == "ACTIVE"

        shown = client.get("/api/v1/prompts/analysis", headers=auth).json()["data"]
        assert shown["active_version"] == 2

        history = client.get("/api/v1/prompts/analysis/history", headers=auth).json()["data"]
        assert [item["version"] for item in history["items"]] == [1, 2]

        diff = client.get("/api/v1/prompts/analysis/diff?from_version=1&to_version=2", headers=auth)
        assert diff.status_code == 200, diff.text
        assert "ACTIVE MARKER" in diff.json()["data"]["diff"]

        restored = client.post(
            "/api/v1/prompts/analysis/restore",
            json={"author": "tester"},
            headers={**auth, "Idempotency-Key": "prompt-restore-1"},
        )
        assert restored.status_code == 201, restored.text
        assert restored.json()["data"]["version"] == 3
        assert restored.json()["data"]["is_default"] is True


def test_api_rejects_a_missing_required_variable_at_save_and_activation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        rejected = client.post(
            "/api/v1/prompts/analysis/versions",
            json={"body": "Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n"},
            headers={**auth, "Idempotency-Key": "prompt-bad-1"},
        )
        assert rejected.status_code == 422, rejected.text
        error = rejected.json()["error"]
        assert error["code"] == "PROMPT_VALIDATION_FAILED"
        assert error["details"][0]["field"] == "metrics"

        # An invalid version inserted behind the service must still be blocked
        # at activation; activation re-validates the stored body.
        connection = sqlite3.connect(database_path(settings))
        try:
            connection.execute(
                """
                INSERT INTO prompt_versions (
                    prompt_id, version, body, body_hash, is_active, created_at,
                    system_prompt_id, system_prompt_version, system_body,
                    system_body_hash, required_variables_json, knowledge_artifact_hash,
                    status, author, reason, is_default
                ) VALUES ('analysis', 2, 'Symbol: {{symbol}}', 'bad', 0, ?, '', 1, '', '', '[]',
                          NULL, 'DRAFT', 'raw', 'bypass', 0)
                """,
                ("2026-11-08T12:00:00.000Z",),
            )
            connection.commit()
        finally:
            connection.close()

        blocked = client.post(
            "/api/v1/prompts/analysis/activate",
            json={"version": 2},
            headers={**auth, "Idempotency-Key": "prompt-activate-bad-1"},
        )
        assert blocked.status_code == 422, blocked.text
        assert blocked.json()["error"]["code"] == "PROMPT_VALIDATION_FAILED"


def test_a_scan_uses_the_active_version_and_records_its_provenance(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"

    service = PromptService(store=container.store, clock=container.clock)
    created = service.edit("analysis", _NEW_ANALYSIS_BODY, author="tester")
    service.activate("analysis", created.version)

    provider = _CapturingProvider()
    active_container = replace(container, llm_provider=provider)
    run = ScanService(container=active_container, lock=active_container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )

    assert run.report is not None
    assert provider.requests
    assert "ACTIVE MARKER" in provider.requests[0].user_prompt
    assert provider.requests[0].prompt_id == "analysis"
    assert provider.requests[0].prompt_version == 2
    assert provider.requests[0].prompt_hash

    decisions = container.store.list_decisions(role="ANALYSIS")
    assert decisions
    payload = json.loads(decisions[-1].payload_json)
    provenance = payload["provenance"]
    assert provenance["prompt_id"] == "analysis"
    assert provenance["prompt_version"] == 2
    assert provenance["prompt_hash"] == provider.requests[0].prompt_hash

    calls = container.store.list_llm_calls(role="ANALYSIS")
    assert calls
    assert calls[0].system_prompt_id == "analysis:system"
    assert calls[0].system_prompt_version == 1
    assert calls[0].system_prompt_hash


def test_cli_prompt_lifecycle(tmp_path: Path, live_daemon: Settings) -> None:
    runner = CliRunner()

    listed = runner.invoke(cli_app, ["prompt", "list"])
    assert listed.exit_code == 0, listed.output
    assert "analysis" in listed.output

    shown = runner.invoke(cli_app, ["prompt", "show", "analysis"])
    assert shown.exit_code == 0, shown.output
    assert "v1" in shown.output

    edited = runner.invoke(cli_app, ["prompt", "edit", "analysis", "--body", _NEW_ANALYSIS_BODY])
    assert edited.exit_code == 0, edited.output
    assert "v2" in edited.output

    activated = runner.invoke(cli_app, ["prompt", "activate", "analysis", "--version", "2"])
    assert activated.exit_code == 0, activated.output

    history = runner.invoke(cli_app, ["prompt", "history", "analysis"])
    assert history.exit_code == 0, history.output
    assert "v2" in history.output

    diffed = runner.invoke(cli_app, ["prompt", "diff", "analysis", "--from", "1", "--to", "2"])
    assert diffed.exit_code == 0, diffed.output
    assert "ACTIVE MARKER" in diffed.output

    restored = runner.invoke(cli_app, ["prompt", "restore", "analysis"])
    assert restored.exit_code == 0, restored.output
    assert "v3" in restored.output
