"""The served HTML declares a favicon so browsers do not log a 404."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_index_declares_an_embedded_favicon() -> None:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'rel="icon"' in html
    assert "data:image/svg+xml" in html
