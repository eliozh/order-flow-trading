"""The execution path must not reach an adapter, even transitively (F4/F10)."""

from __future__ import annotations

import pytest


def test_entry_service_cannot_reach_an_adapter() -> None:
    grimp = pytest.importorskip("grimp")
    graph = grimp.build_graph("oft")

    assert not graph.chain_exists(
        importer="oft.application.entry_service",
        imported="oft.adapters",
    )


def test_llm_orchestrator_cannot_reach_an_adapter() -> None:
    grimp = pytest.importorskip("grimp")
    graph = grimp.build_graph("oft")

    assert not graph.chain_exists(
        importer="oft.application.llm_orchestrator",
        imported="oft.adapters",
    )
