"""Unit: the CLI review renderer and its API-error path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from oft.cli.api_client import ApiClientError
from oft.cli.commands import log as log_module
from oft.cli.commands.log import _REVIEW_TIMEOUT_SECONDS, ADVISORY_LABEL, _render_review
from oft.cli.main import app as cli_app
from oft.config import Settings


def test_render_includes_replay_rationale_caveats_and_reason() -> None:
    rendered = _render_review(
        {
            "trade_id": "TRD-1",
            "status": "PARTIAL",
            "decision_id": "RVW-1",
            "validation_status": None,
            "confidence": None,
            "idempotent_replay": True,
            "rationale": "why it worked",
            "caveats": ["first", "second"],
            "reason": "the linked review was rejected",
        }
    )

    assert ADVISORY_LABEL in rendered
    assert "REVIEW TRD-1 · status PARTIAL · decision RVW-1" in rendered
    assert "validation -" in rendered
    assert "confidence -" in rendered
    assert "reused the linked review (no provider call)" in rendered
    assert "rationale: why it worked" in rendered
    assert "caveat: first" in rendered
    assert "caveat: second" in rendered
    assert "reason: the linked review was rejected" in rendered


def test_render_uses_placeholders_when_fields_are_missing() -> None:
    rendered = _render_review({"trade_id": "TRD-2", "status": "MISSING"})

    assert "decision -" in rendered
    assert "validation -" in rendered
    assert "confidence -" in rendered
    assert "rationale" not in rendered
    assert "caveat" not in rendered


def test_cli_surfaces_an_api_error_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")

    class _FailingClient:
        timeouts: list[float | None] = []

        @classmethod
        def from_settings(
            cls, _settings: Settings, *, timeout: float | None = 2.0
        ) -> _FailingClient:
            cls.timeouts.append(timeout)
            return cls()

        def journal_review(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise ApiClientError("LLM_REFUSED", "the model refused to review")

    monkeypatch.setattr(log_module, "load_settings", lambda: settings)
    monkeypatch.setattr(log_module, "ApiClient", _FailingClient)

    result = CliRunner().invoke(cli_app, ["log", "review", "TRD-1"])

    assert result.exit_code != 0
    assert "the model refused to review" in result.output
    # A review may block on the provider for up to two 120 s calls plus a
    # repair/backoff; the CLI must use its finite review deadline, never the
    # 2 s default socket timeout (H2).
    assert _REVIEW_TIMEOUT_SECONDS == 1200.0
    assert _FailingClient.timeouts == [_REVIEW_TIMEOUT_SECONDS]
