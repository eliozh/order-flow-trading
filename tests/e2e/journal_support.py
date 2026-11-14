"""Shared harness and seeding for the slice-30 journal e2e tests.

A real loopback-free in-process daemon runs over a temporary data dir with the
committed archive fixtures and the deterministic ``stub`` provider. A closed
trade is seeded through the real scan -> trigger -> recheck -> accept ->
(touch stop) -> monitor path. This module is imported by
``test_journal_view.py`` and ``test_journal_review.py``; it is deliberately not
named ``test_*`` so pytest does not collect it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from oft.api.app import create_app
from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.position_service import PositionService
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.clock import FixedClock
from oft.config import Settings
from position_support import set_position_mark

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
CONTRACTS = ROOT / "docs" / "contracts"
WEB_SRC = ROOT / "web" / "src"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 10, 8, tzinfo=UTC)


@dataclass
class _Harness:
    app: Any
    client: TestClient
    auth: dict[str, str]
    settings: Settings
    container: Any


def _registry() -> Registry:
    """Every committed schema, so the envelope's local refs resolve offline."""
    resources = []
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        resources.append(
            (document["$id"], Resource.from_contents(document, default_specification=DRAFT202012))
        )
    return Registry().with_resources(resources)


def _validate_envelope(envelope: dict[str, Any]) -> None:
    document = json.loads((CONTRACTS / "api-envelope.schema.json").read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(document, registry=_registry()).iter_errors(envelope),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def _harness(tmp_path: Path, *, clock: Any | None = None) -> _Harness:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )
    app = create_app(settings, clock=clock or FixedClock(NOW))
    client = TestClient(app)
    container = app.state.container
    assert (
        container.backfill.run(
            symbol="BTCUSDT", data_type="aggTrades", start=START, end=END
        ).run.status
        == "COMPLETE"
    )
    return _Harness(
        app=app,
        client=client,
        auth={"Authorization": f"Bearer {app.state.api_token}"},
        settings=settings,
        container=container,
    )


def _open_position(
    harness: _Harness, *, period: str = "4h", key: str = "journal"
) -> dict[str, Any]:
    """Run the real scan -> trigger -> accept path and stop short of closing."""
    container = harness.container
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period=period, metrics_only=False, provider="stub")
    )
    opportunities = list(container.store.list_opportunities(state="OPEN"))
    assert opportunities, "the scan produced no open opportunity"
    opportunity = opportunities[-1]
    run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert run.trigger is not None
    kill = KillSwitch(settings=harness.settings, store=container.store, clock=container.clock)
    accepted = EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=harness.settings,
        kill_switch=kill,
    ).accept(run.trigger.trigger_id, idempotency_key=f"{key}-entry")
    assert accepted.position is not None
    return accepted.position


def _closed_trade(harness: _Harness) -> tuple[dict[str, Any], dict[str, Any]]:
    position = _open_position(harness, key="journal-close")
    set_position_mark(harness.settings, position["position_id"], position["stop_price"])
    closed = PositionService(
        store=harness.container.store,
        clock=harness.container.clock,
        settings=harness.settings,
    ).monitor(position["position_id"])
    assert closed.trade_log is not None
    return position, closed.trade_log
