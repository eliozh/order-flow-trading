"""The Makefile serve target must use the settings-aware CLI entry point."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_make_serve_uses_oft_serve() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    serve_block = makefile.split("serve:", 1)[1].split("\n\n", 1)[0]

    assert "uv run oft serve" in serve_block
    # A raw uvicorn --factory invocation reloads env Settings and desynchronises
    # the app port from the bound port (HOST_FORBIDDEN on every request).
    assert "uvicorn" not in serve_block
