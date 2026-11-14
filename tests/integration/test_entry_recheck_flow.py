"""Entry re-check -> ordered decision card -> risk gate (slice 18, D2/D5)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oft.adapters.llm_stub import LLMStub
from oft.api.app import create_app
from oft.application.entry_service import EntryRejected, EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.cli.commands.trigger import _render_decision_card
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import LLMRole

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _setup(
    tmp_path: Path, *, entry_fixture: str | None = None
) -> tuple[Settings, Any, EntryService, str]:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert run.trigger is not None
    provider = container.llm_provider
    if entry_fixture is not None:
        provider = LLMStub(
            fixtures_dir=FIXTURES,
            overrides={LLMRole.ENTRY_RECHECK: entry_fixture},
        )
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    service = EntryService(
        store=container.store,
        clock=container.clock,
        provider=provider,
        settings=container.settings,
        kill_switch=kill,
    )
    return settings, container, service, run.trigger.trigger_id


def test_recheck_returns_an_ordered_decision_card(tmp_path: Path) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path)

    card = service.recheck(trigger_id)

    assert card["stage_order"] == ["TRIGGER", "LLM", "RISK", "HUMAN"]
    assert card["trigger_id"] == trigger_id
    assert card["trigger"]["provenance"] == "CALC"
    # The card exposes the entry decision's own provenance, not a bare marker.
    assert card["llm"]["provenance"]["provider"] == "stub"
    assert card["llm"]["provenance"]["model"]
    assert card["llm"]["provenance"]["prompt_version"] >= 1
    assert card["llm"]["provenance"]["cost"]["currency"] == "USD"
    assert card["llm"]["decision"] == "ENTER_LONG"
    assert card["risk"]["provenance"] == "CALC"
    assert card["risk"]["verdict"] in {"ALLOW", "REDUCE"}
    assert card["human_action"] is None
    # Re-check alone never places an order.
    assert container.store.list_orders() == ()


def test_recheck_persists_the_decision_and_verdict(tmp_path: Path) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path)

    card = service.recheck(trigger_id)

    record = container.store.get_trigger_event(trigger_id)
    assert record is not None
    stored = json.loads(record.payload_json)
    assert stored["entry_decision_id"] == card["decision_id"]
    assert stored["risk_verdict"]["verdict"] == card["risk"]["verdict"]
    assert stored["risk_verdict"]["binding_cap"] == card["risk"]["binding_cap"]
    # trigger-event.schema.json forbids extra properties, so the card is
    # rendered on demand from the persisted decision + verdict.
    assert "decision_card" not in stored
    assert record.status == "RECHECKED"


def test_no_entry_card_has_no_risk_stage_and_creates_no_order(tmp_path: Path) -> None:
    _settings, container, service, trigger_id = _setup(
        tmp_path, entry_fixture="entry_recheck_no_entry.json"
    )

    card = service.recheck(trigger_id)

    assert card["llm"]["decision"] == "NO_ENTRY"
    assert card["risk"]["verdict"] == "NOT_EVALUATED"
    # The card must surface why no entry was produced.
    assert "NO_ENTRY" in " ".join(card["risk"]["reasons"])
    assert container.store.list_orders() == ()
    with pytest.raises(EntryRejected):
        service.accept(trigger_id, idempotency_key="accept-no-entry-card")
    assert container.store.list_orders() == ()


def test_missing_spread_is_named_as_a_stale_input(tmp_path: Path) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path)

    card = service.recheck(trigger_id)

    stale = card["risk"]["stale_inputs"]
    assert "spread_bps" in stale
    assert "daily_loss_usd" in stale
    assert container.store.list_orders() == ()


def test_missing_spread_marks_the_card_partial_and_renders_it(tmp_path: Path) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path)

    card = service.recheck(trigger_id)

    # The frozen trigger snapshot carries no bid/ask, so spread is unmeasured.
    # The card must say so instead of implying a fully evaluated gate.
    assert "spread_bps" in card["risk"]["stale_inputs"]
    assert card["data_quality"] == "PARTIAL"
    rendered = _render_decision_card(card)
    assert "data_quality PARTIAL" in rendered
    assert "spread_bps" in rendered
    assert container.store.list_orders() == ()


def test_cli_renderer_keeps_the_stage_order_and_provenance() -> None:
    card = {
        "trigger_id": "TRG-1",
        "trigger": {
            "trigger_id": "TRG-1",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "status": "RECHECKED",
        },
        "llm": {"decision": "ENTER_LONG", "validation_status": "VALID", "confidence": 0.6},
        "risk": {"verdict": "ALLOW", "binding_cap": "NONE", "reasons": []},
        "human_action": None,
        "stage_order": ["TRIGGER", "LLM", "RISK", "HUMAN"],
    }

    rendered = _render_decision_card(card)

    assert rendered.index("TRIGGER") < rendered.index("LLM") < rendered.index("RISK")
    assert rendered.index("RISK") < rendered.index("HUMAN")
    assert "CALC" in rendered.splitlines()[0]
    assert "LLM" in rendered.splitlines()[1]
    assert "oft trigger accept TRG-1" in rendered


def test_api_recheck_returns_the_ordered_card(tmp_path: Path) -> None:
    settings, _container, _service, trigger_id = _setup(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    auth = {"Authorization": f"Bearer {app.state.api_token}"}

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/triggers/{trigger_id}/recheck",
            headers={**auth, "Idempotency-Key": "api-recheck-1"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"]["data_quality"] == "PARTIAL"
    assert body["data"]["data_quality"] == "PARTIAL"
    card = body["data"]
    assert card["stage_order"] == ["TRIGGER", "LLM", "RISK", "HUMAN"]
    assert card["trigger"]["provenance"] == "CALC"
    assert card["llm"]["provenance"]["provider"] == "stub"
    assert card["risk"]["provenance"] == "CALC"


def test_api_accept_envelope_is_partial_when_the_verdict_is_partial(
    tmp_path: Path,
) -> None:
    settings, _container, _service, trigger_id = _setup(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    auth = {"Authorization": f"Bearer {app.state.api_token}"}

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/triggers/{trigger_id}/accept",
            headers={**auth, "Idempotency-Key": "api-accept-partial"},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["data"]["risk_verdict"]["stale_inputs"]
    assert body["meta"]["data_quality"] == "PARTIAL"


def test_api_reject_envelope_is_partial_when_the_verdict_is_partial(
    tmp_path: Path,
) -> None:
    settings, _container, _service, trigger_id = _setup(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    auth = {"Authorization": f"Bearer {app.state.api_token}"}

    with TestClient(app) as client:
        recheck = client.post(
            f"/api/v1/triggers/{trigger_id}/recheck",
            headers={**auth, "Idempotency-Key": "api-recheck-before-reject"},
        )
        assert recheck.status_code == 200, recheck.text
        response = client.post(
            f"/api/v1/triggers/{trigger_id}/reject",
            headers={**auth, "Idempotency-Key": "api-reject-partial"},
            json={"reason": "setup no longer valid"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["risk_verdict"]["stale_inputs"]
    assert body["meta"]["data_quality"] == "PARTIAL"


def test_paper_equity_api_is_partial_for_a_missing_mark(tmp_path: Path) -> None:
    settings, _container, _service, _trigger_id = _setup(tmp_path)
    with closing(sqlite3.connect(settings.data_dir / "oft.db")) as connection:
        connection.execute(
            "INSERT INTO positions "
            "(position_id, account_id, environment, symbol, state, opened_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "POS-NO-MARK",
                "ACC-PAPER",
                "PAPER",
                "BTCUSDT",
                "OPEN",
                "2026-10-08T00:00:00.000Z",
                json.dumps({"size": "0.01", "mark_price": None}),
            ),
        )
        connection.commit()

    app = create_app(settings, clock=FixedClock(NOW))
    auth = {"Authorization": f"Bearer {app.state.api_token}"}
    with TestClient(app) as client:
        response = client.get("/api/v1/paper/equity", headers=auth)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"]["data_quality"] == "PARTIAL"
    assert body["data"]["stale_inputs"] == ["mark"]
    # Equity fell back to cash; no mark was invented.
    assert body["data"]["equity"] == body["data"]["cash_balance"]


def test_a_missing_mark_is_named_in_the_verdict_stale_inputs(tmp_path: Path) -> None:
    settings, container, service, trigger_id = _setup(tmp_path)
    with closing(sqlite3.connect(settings.data_dir / "oft.db")) as connection:
        connection.execute(
            "INSERT INTO positions "
            "(position_id, account_id, environment, symbol, state, opened_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "POS-NO-MARK",
                "ACC-PAPER",
                "PAPER",
                "BTCUSDT",
                "OPEN",
                "2026-10-08T00:00:00.000Z",
                json.dumps({"size": "0.01", "mark_price": None}),
            ),
        )
        connection.commit()

    card = service.recheck(trigger_id)

    # A degraded (cash-only) equity must be named, never silently used.
    assert "mark" in card["risk"]["stale_inputs"]
    assert "equity" in card["risk"]["stale_inputs"]
    assert card["data_quality"] == "PARTIAL"
    assert container.store.list_orders() == ()


# -- full-invalid re-check: no verdict, no order, no accept guidance --------


@pytest.mark.parametrize("fixture", ["refusal.json", "truncated.json", "invalid_schema.json"])
def test_a_fully_invalid_entry_recheck_creates_no_verdict_and_no_order(
    tmp_path: Path, fixture: str
) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path, entry_fixture=fixture)

    card = service.recheck(trigger_id)

    assert card["status"] == "REJECTED"
    assert card["llm"]["validation_status"] == "REJECTED"
    assert card["risk"]["verdict"] == "NOT_EVALUATED"
    assert card["rejection_reason"]
    assert card["data_quality"] == "PARTIAL"
    assert container.store.list_orders() == ()
    assert container.store.list_fills() == ()
    assert container.store.list_positions(state="OPEN") == ()
    with closing(sqlite3.connect(_settings.data_dir / "oft.db")) as connection:
        verdicts = connection.execute("SELECT COUNT(*) FROM risk_verdicts").fetchone()[0]
    assert verdicts == 0
    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-invalid-recheck")
    assert excinfo.value.code in {"ORDER_REJECTED", "CONFLICT"}


def test_a_gate_input_failure_is_not_reported_as_a_risk_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, container, service, trigger_id = _setup(tmp_path)
    monkeypatch.setattr(container.store, "get_symbol", lambda symbol: None)

    card = service.recheck(trigger_id)

    assert card["status"] == "REJECTED"
    assert card["error_code"] == "DATA_UNAVAILABLE"
    assert card["risk"]["verdict"] == "NOT_EVALUATED"
    assert card["risk"].get("error_code") == "DATA_UNAVAILABLE"
    assert card["rejection_reason"].startswith("DATA_UNAVAILABLE")
    assert card["data_quality"] == "PARTIAL"
    assert container.store.list_orders() == ()
    with closing(sqlite3.connect(settings.data_dir / "oft.db")) as connection:
        verdicts = connection.execute("SELECT COUNT(*) FROM risk_verdicts").fetchone()[0]
    assert verdicts == 0
    audit = [
        event
        for event in container.store.list_audit_events(limit=20)
        if event.event_type == "ENTRY_RECHECK"
    ]
    payload = json.loads(audit[0].payload_json)["payload"]
    assert payload["error_code"] == "DATA_UNAVAILABLE"
    assert payload["risk_verdict"] != "BLOCK"
    rendered = _render_decision_card(card)
    assert "oft trigger accept" not in rendered
    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-after-data-failure")
    assert excinfo.value.code == "CONFLICT"


def test_repeat_recheck_refreshes_the_verdict_metadata(tmp_path: Path) -> None:
    settings, container, service, trigger_id = _setup(tmp_path)
    first = service.recheck(trigger_id)
    later = EntryService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(seconds=10)),
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=KillSwitch(
            settings=container.settings,
            store=container.store,
            clock=FixedClock(NOW + timedelta(seconds=10)),
        ),
    )

    second = later.recheck(trigger_id, refresh=True)

    with closing(sqlite3.connect(settings.data_dir / "oft.db")) as connection:
        rows = connection.execute(
            "SELECT risk_verdict_id, computed_at FROM risk_verdicts ORDER BY computed_at"
        ).fetchall()
    # A refresh persists a NEW decision (and therefore a new deterministic
    # verdict id) while retaining the previous rows; the latest verdict metadata
    # must describe the latest re-check, not the first (DB-3).
    assert len(rows) == 2
    assert rows[-1][1] == second["risk"]["computed_at"]
    assert rows[-1][1] != first["risk"]["computed_at"]
    audits = [
        event
        for event in container.store.list_audit_events_ascending()
        if event.event_type == "RISK_VERDICT"
    ]
    assert len(audits) == 2


def test_accept_submits_only_the_approved_recheck_size_after_a_mark_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path)
    # Make the per-trade risk the binding term (notional cap widened) so a
    # fresh recompute after a mark drift would size much larger.
    container.store.set_system_state(
        "risk_caps",
        json.dumps({"max_notional_pct": 99.0}),
        updated_at="2026-10-08T00:00:00.000Z",
    )
    card = service.recheck(trigger_id)
    approved = Decimal(str(card["risk"]["allowed"]["quantity"]))
    # Per-trade risk binds at the re-check mark (~0.08 units); the drifted mark
    # would let a fresh recompute size several times larger.
    assert Decimal("0.07") < approved < Decimal("0.09")

    record = container.store.get_trigger_event(trigger_id)
    assert record is not None
    payload = json.loads(record.payload_json)
    opportunity = container.store.get_opportunity(payload["opportunity_id"])
    assert opportunity is not None
    report_id = str(json.loads(opportunity.payload_json)["source"]["report_id"])
    report = container.store.get_report(report_id)
    assert report is not None
    snapshot = json.loads(report.snapshot_json)
    snapshot["last_price"] = "69001.00"
    drifted = replace(report, snapshot_json=json.dumps(snapshot))
    original_get_report = container.store.get_report
    monkeypatch.setattr(
        container.store,
        "get_report",
        lambda rid: drifted if rid == report_id else original_get_report(rid),
    )

    result = service.accept(trigger_id, idempotency_key="accept-after-drift")

    # The human approved 0.08 units on the card. A fresh recompute at the
    # drifted mark would allow ~0.143; accept must submit within the verdict.
    assert Decimal(result.order["quantity"]) == approved
    assert len(container.store.list_orders()) == 1


def test_the_renderer_shows_the_status_and_never_offers_accept_after_a_block() -> None:
    card = {
        "trigger_id": "TRG-BLOCK",
        "status": "BLOCKED",
        "rejection_reason": "risk blocked: SPREAD",
        "data_quality": "PARTIAL",
        "trigger": {
            "trigger_id": "TRG-BLOCK",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "status": "BLOCKED",
        },
        "llm": {"decision": "ENTER_LONG", "validation_status": "VALID", "confidence": 0.6},
        "risk": {"verdict": "BLOCK", "binding_cap": "SPREAD", "reasons": []},
        "human_action": None,
        "stage_order": ["TRIGGER", "LLM", "RISK", "HUMAN"],
    }

    rendered = _render_decision_card(card)

    assert "STATUS BLOCKED" in rendered
    assert "SPREAD" in rendered
    assert "oft trigger accept" not in rendered
    assert "HUMAN not available" in rendered


def test_the_renderer_shows_the_rejection_reason_without_accept_guidance() -> None:
    card = {
        "trigger_id": "TRG-NOENTRY",
        "status": "REJECTED",
        "rejection_reason": "entry re-check returned NO_ENTRY",
        "data_quality": "PARTIAL",
        "trigger": {
            "trigger_id": "TRG-NOENTRY",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "status": "REJECTED",
        },
        "llm": {"decision": "NO_ENTRY", "validation_status": "VALID", "confidence": 0.2},
        "risk": {"verdict": "NOT_EVALUATED", "binding_cap": "NONE", "reasons": []},
        "human_action": None,
        "stage_order": ["TRIGGER", "LLM", "RISK", "HUMAN"],
    }

    rendered = _render_decision_card(card)

    assert "STATUS REJECTED" in rendered
    assert "NO_ENTRY" in rendered
    assert "oft trigger accept" not in rendered


# -- F1: a terminal trigger can never produce a second paper entry ---------


def _crash_accept_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, EntryService, str, str]:
    """Accept, crash after the OMS commit, and leave the trigger update lost."""
    _settings, container, service, trigger_id = _setup(tmp_path)
    service.recheck(trigger_id)
    original_update = container.store.update_trigger_event

    def _crash(record, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated crash after the OMS commit")

    monkeypatch.setattr(container.store, "update_trigger_event", _crash)
    with pytest.raises(RuntimeError):
        service.accept(trigger_id, idempotency_key="crash-first")
    monkeypatch.setattr(container.store, "update_trigger_event", original_update)
    committed = container.store.list_orders()
    assert len(committed) == 1
    return container, service, trigger_id, committed[0].order_id


def test_recheck_reconciles_a_committed_order_whose_trigger_update_was_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, service, trigger_id, order_id = _crash_accept_after_commit(tmp_path, monkeypatch)
    calls = {"count": 0}
    original_complete = service._provider.complete  # type: ignore[union-attr]

    def _counting_complete(request):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        return original_complete(request)

    monkeypatch.setattr(service._provider, "complete", _counting_complete)

    card = service.recheck(trigger_id)

    # Recheck must adopt the committed order without another LLM call or a
    # decision/verdict rewrite.
    assert calls["count"] == 0
    assert card["status"] == "ACCEPTED"
    assert card["reconciled"] is True
    # The reconciled card reads the persisted decision's structured provenance,
    # never a bare "LLM" stage marker.
    assert card["llm"]["provenance"]["provider"] == "stub"
    assert card["llm"]["provenance"]["prompt_id"] == "entry_recheck"
    assert card["order"]["order_id"] == order_id
    assert card["position"] is not None
    healed = container.store.get_trigger_event(trigger_id)
    assert healed is not None
    assert healed.status == "ACCEPTED"
    stored = json.loads(healed.payload_json)
    assert stored["created_order_id"] == order_id
    assert stored["human_action"]["action"] == "ACCEPT"
    assert len(container.store.list_orders()) == 1
    assert len(container.store.list_fills()) == 1
    assert len(container.store.list_positions(state="OPEN")) == 1


def test_reject_refuses_a_committed_order_whose_trigger_update_was_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, service, trigger_id, order_id = _crash_accept_after_commit(tmp_path, monkeypatch)
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None
    opportunity = container.store.get_opportunity(trigger.opportunity_id)
    assert opportunity is not None

    with pytest.raises(EntryRejected) as excinfo:
        service.reject(trigger_id, reason="human says no")

    assert excinfo.value.code == "CONFLICT"
    # The rejection must not invalidate the opportunity or hide the exposure.
    still = container.store.get_trigger_event(trigger_id)
    assert still is not None
    assert still.status == "RECHECKED"
    assert json.loads(still.payload_json)["created_order_id"] is None
    unchanged_opportunity = container.store.get_opportunity(trigger.opportunity_id)
    assert unchanged_opportunity is not None
    assert unchanged_opportunity.state == "TRIGGERED"
    assert [order.order_id for order in container.store.list_orders()] == [order_id]
    assert len(container.store.list_positions(state="OPEN")) == 1
    assert not any(
        event.event_type == "TRIGGER_REJECTED"
        for event in container.store.list_audit_events(limit=50)
    )


def test_recheck_after_accept_is_a_conflict_and_changes_nothing(tmp_path: Path) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path)
    service.accept(trigger_id, idempotency_key="accept-first")
    accepted = container.store.get_trigger_event(trigger_id)
    assert accepted is not None
    assert accepted.status == "ACCEPTED"
    verdict_before = json.loads(accepted.payload_json)["risk_verdict"]
    orders_before = container.store.list_orders()

    with pytest.raises(EntryRejected) as excinfo:
        service.recheck(trigger_id)

    assert excinfo.value.code == "CONFLICT"
    after = container.store.get_trigger_event(trigger_id)
    assert after is not None
    assert after.status == "ACCEPTED"
    stored = json.loads(after.payload_json)
    assert stored["risk_verdict"] == verdict_before
    assert stored["created_order_id"] == json.loads(accepted.payload_json)["created_order_id"]
    assert container.store.list_orders() == orders_before
    assert len(orders_before) == 1


def test_reject_after_accept_is_a_conflict_and_keeps_the_exposure(
    tmp_path: Path,
) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path)
    accepted = service.accept(trigger_id, idempotency_key="accept-then-reject")
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None
    opportunity_id = trigger.opportunity_id

    with pytest.raises(EntryRejected) as excinfo:
        service.reject(trigger_id, reason="too late")

    assert excinfo.value.code == "CONFLICT"
    after = container.store.get_trigger_event(trigger_id)
    assert after is not None
    assert after.status == "ACCEPTED"
    stored = json.loads(after.payload_json)
    assert stored["created_order_id"] == accepted.order["order_id"]
    assert stored["human_action"]["action"] == "ACCEPT"
    opportunity = container.store.get_opportunity(opportunity_id)
    assert opportunity is not None
    assert opportunity.state == "TRIGGERED"
    assert len(container.store.list_orders()) == 1
    assert len(container.store.list_positions(state="OPEN")) == 1
    assert not any(
        event.event_type == "TRIGGER_REJECTED"
        for event in container.store.list_audit_events(limit=50)
    )


def test_accept_then_accept_with_a_new_key_never_creates_a_second_order(tmp_path: Path) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path)
    first = service.accept(trigger_id, idempotency_key="accept-one")

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-two")

    assert excinfo.value.code == "CONFLICT"
    orders = container.store.list_orders()
    assert len(orders) == 1
    assert orders[0].order_id == first.order["order_id"]
    assert len(container.store.list_fills()) == 1
    assert len(container.store.list_positions(state="OPEN")) == 1


def test_the_same_key_still_replays_the_original_order(tmp_path: Path) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path)
    first = service.accept(trigger_id, idempotency_key="accept-replay")

    second = service.accept(trigger_id, idempotency_key="accept-replay")

    assert second.idempotent_replay is True
    assert second.order is not None
    assert second.order["order_id"] == first.order["order_id"]
    assert len(container.store.list_orders()) == 1


def test_recheck_then_accept_still_creates_at_most_one_order(tmp_path: Path) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path)
    service.recheck(trigger_id)
    rechecked = container.store.get_trigger_event(trigger_id)
    assert rechecked is not None
    assert rechecked.status == "RECHECKED"

    result = service.accept(trigger_id, idempotency_key="accept-after-recheck")

    assert result.order is not None
    assert len(container.store.list_orders()) == 1
    assert len(container.store.list_fills()) == 1


def test_accept_after_the_recheck_verdict_expired_is_a_conflict(tmp_path: Path) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path)
    card = service.recheck(trigger_id)
    assert card["risk"]["expires_at"]
    late = EntryService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(seconds=60)),
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=KillSwitch(
            settings=container.settings,
            store=container.store,
            clock=FixedClock(NOW + timedelta(seconds=60)),
        ),
    )

    with pytest.raises(EntryRejected) as excinfo:
        late.accept(trigger_id, idempotency_key="accept-expired-verdict")

    assert excinfo.value.code == "CONFLICT"
    assert container.store.list_orders() == ()


# -- F4/F5/F7: spread source, corrupt caps, and contract drift --------------


def test_a_report_snapshot_spread_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path)
    record = container.store.get_trigger_event(trigger_id)
    assert record is not None
    payload = json.loads(record.payload_json)
    opportunity = container.store.get_opportunity(payload["opportunity_id"])
    assert opportunity is not None
    report_id = str(json.loads(opportunity.payload_json)["source"]["report_id"])
    report = container.store.get_report(report_id)
    assert report is not None
    snapshot = json.loads(report.snapshot_json)
    snapshot["spread_bps"] = 500.0
    poisoned = replace(report, snapshot_json=json.dumps(snapshot))
    original_get_report = container.store.get_report
    monkeypatch.setattr(
        container.store,
        "get_report",
        lambda rid: poisoned if rid == report_id else original_get_report(rid),
    )

    card = service.recheck(trigger_id)

    # D2: only the trigger snapshot measures spread; a report-only value is
    # ignored and named stale, never silently trusted.
    assert "spread_bps" in card["risk"]["stale_inputs"]
    assert card["risk"]["binding_cap"] != "SPREAD"


def test_corrupt_persisted_risk_caps_map_to_validation_failed(tmp_path: Path) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path)
    container.store.set_system_state(
        "risk_caps", "{not-json", updated_at="2026-10-08T00:00:00.000Z"
    )

    with pytest.raises(EntryRejected) as recheck_error:
        service.recheck(trigger_id)

    assert recheck_error.value.code == "VALIDATION_FAILED"
    with pytest.raises(EntryRejected) as accept_error:
        service.accept(trigger_id, idempotency_key="accept-corrupt-caps")

    assert accept_error.value.code == "VALIDATION_FAILED"
    assert container.store.list_orders() == ()
    assert container.store.list_fills() == ()
    record = container.store.get_trigger_event(trigger_id)
    assert record is not None
    assert record.status == "PENDING_RECHECK"


def test_legacy_persisted_risk_caps_fail_closed_with_a_typed_error(tmp_path: Path) -> None:
    _settings, container, service, trigger_id = _setup(tmp_path)
    container.store.set_system_state(
        "risk_caps",
        json.dumps({"max_notional_usd": "2000", "daily_loss_limit_usd": "300"}),
        updated_at="2026-10-08T00:00:00.000Z",
    )

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-legacy-caps")

    assert excinfo.value.code == "VALIDATION_FAILED"
    assert container.store.list_orders() == ()
    assert container.store.list_fills() == ()
    assert container.store.list_positions(state="OPEN") == ()


# -- _spread_bps: pure extraction from the trigger snapshot (D2) -----------
#
# The frozen trigger-event contract carries no bid/ask/spread today, so these
# exercise the extraction helper directly with in-memory mappings instead of
# injecting contract-illegal fields into a persisted trigger payload.


def _spread_parser(tmp_path: Path) -> EntryService:
    _settings, _container, service, _trigger_id = _setup(tmp_path)
    return service


def test_a_measured_spread_bps_is_returned_verbatim(tmp_path: Path) -> None:
    service = _spread_parser(tmp_path)

    assert service._spread_bps({"snapshot": {"spread_bps": 12.5}}) == 12.5
    assert service._spread_bps({"snapshot": {"spread_bps": "12.5"}}) == 12.5
    assert service._spread_bps({"snapshot": {"spread_bps": 0.0}}) == 0.0


def test_bid_ask_are_converted_to_the_exact_spread_bps(tmp_path: Path) -> None:
    service = _spread_parser(tmp_path)

    # (ask - bid) / mid * 10000 with mid = (99 + 101) / 2 = 100 -> 200 bps.
    assert service._spread_bps({"snapshot": {"bid": 99, "ask": 101}}) == 200.0
    assert service._spread_bps({"snapshot": {"bid": "99", "ask": "101"}}) == 200.0
    # A non-round quote pins the Decimal formula, not a float shortcut.
    expected = float(
        (Decimal("100.01") - Decimal("100"))
        / ((Decimal("100.01") + Decimal("100")) / Decimal("2"))
        * Decimal("10000")
    )
    assert service._spread_bps({"snapshot": {"bid": "100", "ask": "100.01"}}) == expected
    assert round(service._spread_bps({"snapshot": {"bid": 100, "ask": 100.01}}), 12) == round(
        expected, 12
    )


def test_a_measured_spread_takes_precedence_over_bid_and_ask(tmp_path: Path) -> None:
    service = _spread_parser(tmp_path)

    payload = {"snapshot": {"spread_bps": 3.0, "bid": 99, "ask": 101}}

    assert service._spread_bps(payload) == 3.0


def test_absent_or_invalid_spread_inputs_are_none(tmp_path: Path) -> None:
    service = _spread_parser(tmp_path)

    invalid: list[dict[str, Any]] = [
        {},
        {"snapshot": None},
        {"snapshot": []},
        {"snapshot": {}},
        {"snapshot": {"spread_bps": "wide"}},
        {"snapshot": {"spread_bps": None}},
        {"snapshot": {"bid": 99}},
        {"snapshot": {"ask": 101}},
        {"snapshot": {"bid": "oops", "ask": 101}},
        {"snapshot": {"bid": 101, "ask": 99}},
        {"snapshot": {"bid": 0, "ask": 101}},
        {"snapshot": {"bid": -1, "ask": 101}},
        {"snapshot": {"bid": 99, "ask": 0}},
    ]

    for payload in invalid:
        assert service._spread_bps(payload) is None, payload

    # A missing declared spread still falls back to a valid bid/ask pair.
    fallback = {"snapshot": {"spread_bps": None, "bid": 99, "ask": 101}}
    assert service._spread_bps(fallback) == 200.0
