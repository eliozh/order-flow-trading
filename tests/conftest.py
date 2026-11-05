"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from oft.config import Settings


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")
