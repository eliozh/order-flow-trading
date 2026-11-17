"""The read-only query path never resolves a provider and records no LLM call."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import oft.bootstrap as bootstrap
from oft.application.settings_service import SettingsService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import TelegramUpdate

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
QUERIES = ("/status", "/positions", "/opportunities", "/report", "/budget", "/watchlist", "/help")


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _explode(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("the read-only query path resolved an LLM provider")


def test_each_query_runs_without_resolving_a_provider_or_recording_a_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    SettingsService(
        store=container.store, clock=container.clock, settings=container.settings
    ).update_telegram_allowlist([1001], [2002], actor="test")
    service = container.telegram_service
    assert service is not None

    # Every provider seam explodes after construction: none may be reached.
    monkeypatch.setattr(bootstrap, "resolve_llm_provider", _explode)
    monkeypatch.setattr(bootstrap, "build_llm_adapter", _explode)

    replies: list[str] = []
    for index, text in enumerate(QUERIES, start=1):
        record = service.handle_update(
            TelegramUpdate(
                update_id=index,
                chat_id=1001,
                user_id=2002,
                text=text,
                message_id=100 + index,
            )
        )
        assert record is not None, text
        replies.append(json.loads(record.payload_json)["text"])

    assert len(replies) == len(QUERIES)
    assert all(reply.strip() for reply in replies)
    assert container.store.list_llm_calls() == ()
    assert container.store.list_budget_records() == ()


def test_the_query_module_imports_no_llm_symbol() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "src" / "oft" / "application" / "telegram_queries.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
            imported.extend(alias.name for alias in node.names)

    assert not [name for name in imported if "llm" in name.lower()]
    assert not [name for name in imported if "provider" in name.lower()]
