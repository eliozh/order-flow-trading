"""Full paper loop: stub scan -> report -> opportunity -> trigger -> accept -> paper."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.application.entry_service import EntryRejected, EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.config import Settings
from oft.ports.llm import LLMRole

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _seed(settings: Settings) -> None:
    container = build_container(settings, initialize=True)
    result = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert result.run.status == "COMPLETE"


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def test_api_scan_report_opportunity_trigger_accept_paper_loop(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _seed(settings)
    app = create_app(settings)
    auth = _auth(app)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/scans",
            json={
                "symbol": "BTCUSDT",
                "period": "4h",
                "source": "backfill",
                "metrics_only": False,
                "provider": "stub",
            },
            headers={**auth, "Idempotency-Key": "scan-llm-1"},
        )
        assert created.status_code == 201, created.text
        scan_data = created.json()["data"]
        report_id = scan_data["report_id"]
        assert scan_data["report"]["llm"]["configured"] is True

        report = client.get(f"/api/v1/reports/{report_id}", headers=auth).json()["data"]
        assert report["llm"]["configured"] is True
        assert report["llm"]["validation_status"] == "VALID"
        opportunity_ids = report["llm"]["opportunity_ids"]
        assert len(opportunity_ids) == 1
        opportunity_id = opportunity_ids[0]

        listed = client.get("/api/v1/opportunities?state=open", headers=auth).json()
        assert [item["opportunity_id"] for item in listed["data"]["items"]] == [opportunity_id]

        shown = client.get(f"/api/v1/opportunities/{opportunity_id}", headers=auth).json()["data"]
        assert shown["source"]["report_id"] == report_id
        assert shown["trigger_conditions"][0]["condition_id"] == "C-1"
        assert shown["state_history"][0]["to_state"] == "OPEN"

        evaluated = client.post(
            "/api/v1/triggers/evaluate",
            json={"opportunity_id": opportunity_id},
            headers={**auth, "Idempotency-Key": "trigger-1"},
        )
        assert evaluated.status_code == 200, evaluated.text
        evaluation = evaluated.json()["data"]
        assert evaluation["triggered"] is True
        trigger = evaluation["trigger"]
        assert trigger["trigger_source"] == "CODE"
        assert trigger["snapshot"]["snapshot_hash"]
        assert trigger["condition_results"][0]["met"] is True
        assert trigger["idempotency_key"].startswith("TRG-")
        trigger_id = trigger["trigger_id"]

        accepted = client.post(
            f"/api/v1/triggers/{trigger_id}/accept",
            headers={**auth, "Idempotency-Key": "accept-1"},
        )
        assert accepted.status_code == 200, accepted.text
        entry = accepted.json()["data"]
        assert entry["status"] == "ACCEPTED"
        assert entry["order"]["status"] == "FILLED"
        assert entry["fill"]["source"] == "PAPER"
        assert entry["position"]["state"] == "OPEN"
        order_id = entry["order"]["order_id"]

        replayed = client.post(
            f"/api/v1/triggers/{trigger_id}/accept",
            headers={**auth, "Idempotency-Key": "accept-1"},
        )
        assert replayed.status_code == 200, replayed.text
        replayed_body = replayed.json()
        assert replayed_body["meta"]["idempotent_replay"] is True
        assert replayed_body["data"]["order"]["order_id"] == order_id

        orders = client.get("/api/v1/paper/orders", headers=auth).json()["data"]
        fills = client.get("/api/v1/paper/fills", headers=auth).json()["data"]
        balance = client.get("/api/v1/paper/balance", headers=auth).json()["data"]
        equity = client.get("/api/v1/paper/equity", headers=auth).json()["data"]
        assert len(orders["items"]) == 1
        assert len(fills["items"]) == 1
        assert balance["initial_balance"] == "10000.00"
        assert balance["balance"] != balance["initial_balance"]
        assert equity["open_positions"] == 1

    # The trigger, order, fill, and position are also reachable through the store.
    container = app.state.container
    assert container.store.get_order(order_id) is not None
    assert container.store.list_positions(state="OPEN")
    audit_events = container.store.list_audit_events()
    assert {event.event_type for event in audit_events} >= {
        "TRIGGER_CREATED",
        "RISK_VERDICT",
        "ORDER_FILLED",
        "TRIGGER_ACCEPTED",
    }
    assert audit_events[0].previous_hash


def _container_with_trigger(tmp_path: Path, *, entry_fixture: str | None = None):
    settings = _settings(tmp_path)
    _seed(settings)
    container = build_container(settings, initialize=True)
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunities = container.store.list_opportunities(state="OPEN")
    assert opportunities
    trigger_service = TriggerService(store=container.store, clock=container.clock)
    run = trigger_service.evaluate(opportunities[0].opportunity_id)
    assert run.trigger is not None
    provider = container.llm_provider
    if entry_fixture is not None:
        from oft.adapters.llm_stub import LLMStub

        provider = LLMStub(
            fixtures_dir=FIXTURES,
            overrides={LLMRole.ENTRY_RECHECK: entry_fixture},
        )
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    entry = EntryService(
        store=container.store,
        clock=container.clock,
        provider=provider,
        settings=container.settings,
        kill_switch=kill,
    )
    return container, entry, kill, run.trigger.trigger_id, opportunities[0].opportunity_id


def test_rejected_narrative_produces_a_report_and_no_opportunity(tmp_path: Path) -> None:
    from dataclasses import replace

    from oft.adapters.llm_stub import LLMStub

    settings = _settings(tmp_path)
    _seed(settings)
    container = build_container(settings, initialize=True)
    broken = replace(
        container,
        llm_provider=LLMStub(
            fixtures_dir=FIXTURES,
            overrides={LLMRole.ANALYSIS: "invalid_schema.json"},
        ),
    )

    run = ScanService(container=broken, lock=broken.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )

    assert run.scan.status == "DONE"
    llm_state = json.loads(run.report.llm_json or "{}")
    assert llm_state["configured"] is True
    assert llm_state["validation_status"] == "REJECTED"
    assert llm_state["reason"].startswith("SCHEMA_INVALID")
    assert container.store.list_opportunities() == ()
    assert container.store.list_orders() == ()


def test_kill_switch_blocks_a_new_paper_order(tmp_path: Path) -> None:
    container, entry, kill, trigger_id, _opportunity_id = _container_with_trigger(tmp_path)
    kill.activate()

    with pytest.raises(EntryRejected) as excinfo:
        entry.accept(trigger_id, idempotency_key="accept-killed")

    assert excinfo.value.code == "KILL_SWITCH_ACTIVE"
    assert container.store.list_orders() == ()
    assert container.store.list_positions() == ()


def test_stopless_entry_is_blocked_and_the_verdict_is_persisted(tmp_path: Path) -> None:
    container, entry, _kill, trigger_id, _opportunity_id = _container_with_trigger(
        tmp_path, entry_fixture="entry_recheck_stopless.json"
    )

    with pytest.raises(EntryRejected) as excinfo:
        entry.accept(trigger_id, idempotency_key="accept-stopless")

    assert excinfo.value.code == "RISK_BLOCKED"
    assert container.store.list_orders() == ()
    # The risk verdict and the trigger's BLOCKED status are persisted.
    triggers = container.store.list_trigger_events()
    assert triggers and triggers[0].status == "BLOCKED"
    risk = json.loads(triggers[0].payload_json)["risk_verdict"]
    assert risk is not None
    assert risk["verdict"] == "BLOCK"
    assert risk["binding_cap"] == "MISSING_STOP"


def test_oversized_entry_is_reduced_to_the_stricter_cap(tmp_path: Path) -> None:
    container, entry, _kill, trigger_id, _opportunity_id = _container_with_trigger(
        tmp_path, entry_fixture="entry_recheck_oversized.json"
    )

    result = entry.accept(trigger_id, idempotency_key="accept-oversized")

    assert result.status == "ACCEPTED"
    assert result.risk_verdict["verdict"] == "REDUCE"
    assert result.risk_verdict["binding_cap"] == "MAX_NOTIONAL"
    order = container.store.list_orders()
    assert len(order) == 1
    assert order[0].payload_json
    payload = json.loads(order[0].payload_json)
    assert payload["quantity"] == "0.028"
