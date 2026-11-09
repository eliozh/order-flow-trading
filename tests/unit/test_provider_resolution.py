"""``provider_for`` resolves the requested name, never the configured one (F1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from oft import bootstrap
from oft.adapters.llm_openai import OpenAIAdapter
from oft.adapters.llm_stub import LLMStub
from oft.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        llm_provider="openai",
        openai_api_key="sk-SENTINEL-DO-NOT-LEAK",
        llm_fixtures_dir=Path(__file__).resolve().parents[1] / "fixtures" / "llm",
    )


def test_provider_for_stub_never_returns_the_configured_real_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    stub = LLMStub(fixtures_dir=settings.llm_fixtures_dir)
    real = OpenAIAdapter(api_key="sk-SENTINEL-DO-NOT-LEAK", base_url="https://api.openai.test")

    def fake_build(_settings: Settings, name: str | None) -> object | None:
        normalized = (name or "").strip().lower()
        if normalized == "stub":
            return stub
        if normalized == "openai":
            return real
        return None

    monkeypatch.setattr(bootstrap, "build_llm_adapter", fake_build)
    container = bootstrap.build_container(settings)

    assert container.llm_provider is real
    assert container.provider_for("stub") is stub
    assert container.provider_for("openai") is real
    assert container.provider_for() is real


def test_provider_for_without_a_configured_provider_still_resolves_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        llm_provider=None,
        llm_fixtures_dir=Path(__file__).resolve().parents[1] / "fixtures" / "llm",
    )
    stub = LLMStub(fixtures_dir=settings.llm_fixtures_dir)
    monkeypatch.setattr(
        bootstrap,
        "build_llm_adapter",
        lambda _settings, name: stub if (name or "").strip().lower() == "stub" else None,
    )
    container = bootstrap.build_container(settings)

    assert container.llm_provider is None
    assert container.provider_for("stub") is stub
    assert container.provider_for() is None
