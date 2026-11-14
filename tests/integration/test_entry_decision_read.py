"""Integration: the persisted entry-decision read and the default/refresh recheck."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from oft.application.entry_service import EntryRejected, EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerNotFound, TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import (
    LLMRequest,
    LLMResult,
    ProviderCapabilities,
)

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = datetime(2026, 10, 1, tzinfo=UTC).date()
END = datetime(2026, 10, 7, tzinfo=UTC).date()
NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)


class _CountingProvider:
    """Delegates to the stub provider and counts calls."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    def capabilities(self) -> Any:
        return self._inner.capabilities()

    def complete(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        return self._inner.complete(request)

    def estimate_cost(self, request: LLMRequest, model: str) -> Any:
        return self._inner.estimate_cost(request, model)


class _ExplodingProvider:
    """Proves no *completion* call happens on the persisted/default path.

    Constructing the orchestrator probes ``capabilities()``; only ``complete``
    raises, and the persisted read path never reaches it.
    """

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="stub",
            strict_schema=True,
            max_output_tokens=4096,
            notes="no-call guard",
        )

    def complete(self, request: LLMRequest) -> LLMResult:
        raise AssertionError("the provider must not be called for a persisted read")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _setup(tmp_path: Path) -> tuple[Settings, Any, TriggerService, str]:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    triggers = TriggerService(store=container.store, clock=container.clock)
    run = triggers.evaluate(opportunity.opportunity_id)
    assert run.trigger is not None
    return settings, container, triggers, run.trigger.trigger_id


def _entry(container: Any, provider: Any) -> EntryService:
    return EntryService(
        store=container.store,
        clock=container.clock,
        provider=provider,
        settings=container.settings,
        kill_switch=KillSwitch(
            settings=container.settings, store=container.store, clock=container.clock
        ),
    )


def test_the_persisted_entry_decision_is_read_with_provenance(tmp_path: Path) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    service = _entry(container, container.llm_provider)
    card = service.recheck(trigger_id)

    persisted = service.get_persisted_entry_decision(trigger_id)

    assert persisted["status"] == "FOUND"
    assert persisted["entry_decision_id"] == card["decision_id"]
    assert persisted["decision"]["decision_id"] == card["decision_id"]
    provenance = persisted["provenance"]
    assert provenance["provider"] == "stub"
    assert provenance["model"]
    assert provenance["prompt_id"] == "entry_recheck"
    assert provenance["prompt_version"] >= 1
    assert provenance["prompt_hash"]
    assert provenance["cost"]["currency"] == "USD"
    assert provenance["recorded_at"].endswith("Z")
    assert persisted["validation_status"] == "VALID"
    assert 0.0 <= persisted["confidence"] <= 1.0
    assert persisted["decision_type"] == "ENTRY_RECHECK"


def test_the_default_recheck_reuses_the_persisted_decision_without_a_call(
    tmp_path: Path,
) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    first = _entry(container, container.llm_provider).recheck(trigger_id)
    decisions_before = len(container.store.list_decisions())
    audits_before = len(container.store.list_audit_events(limit=1000))

    source = _entry(container, _ExplodingProvider())
    card = source.recheck(trigger_id)

    assert card["decision_id"] == first["decision_id"]
    assert card["refreshed"] is False
    assert card["llm"]["provenance"]["provider"] == "stub"
    assert card["entry_decision_id"] == first["decision_id"]
    assert len(container.store.list_decisions()) == decisions_before
    assert len(container.store.list_audit_events(limit=1000)) == audits_before


def test_a_persisted_card_is_complete_when_the_risk_verdict_is_stale_free(
    tmp_path: Path,
) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    first = _entry(container, container.llm_provider).recheck(trigger_id)
    with sqlite3.connect(container.settings.data_dir / "oft.db") as connection:
        row = connection.execute(
            "SELECT payload_json FROM trigger_events WHERE trigger_id = ?", (trigger_id,)
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["risk_verdict"] = {
            "verdict": "ALLOW",
            "binding_cap": "NONE",
            "reasons": [],
            "stale_inputs": [],
        }
        connection.execute(
            "UPDATE trigger_events SET payload_json = ? WHERE trigger_id = ?",
            (json.dumps(payload, sort_keys=True), trigger_id),
        )
        connection.commit()

    card = _entry(container, _ExplodingProvider()).recheck(trigger_id)

    assert card["persisted"] is True
    assert card["decision_id"] == first["decision_id"]
    assert card["risk"]["stale_inputs"] == []
    assert card["data_quality"] == "COMPLETE"
    assert card["reasons"] == []


def test_a_persisted_card_with_stale_risk_inputs_is_partial_and_names_them(
    tmp_path: Path,
) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    first = _entry(container, container.llm_provider).recheck(trigger_id)

    card = _entry(container, _ExplodingProvider()).recheck(trigger_id)

    assert card["persisted"] is True
    assert card["decision_id"] == first["decision_id"]
    assert card["data_quality"] == "PARTIAL"
    assert card["risk"]["stale_inputs"]
    assert "daily_loss_usd" in card["risk"]["stale_inputs"]


def test_refresh_reruns_the_provider_and_audits_the_previous_decision(
    tmp_path: Path,
) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    first = _entry(container, container.llm_provider).recheck(trigger_id)
    counting = _CountingProvider(container.llm_provider)

    refreshed = _entry(container, counting).recheck(trigger_id, refresh=True)

    assert counting.calls == 1
    assert refreshed["refreshed"] is True
    events = [
        event
        for event in container.store.list_audit_events(limit=1000)
        if event.event_type == "ENTRY_RECHECK_REFRESHED"
    ]
    assert len(events) == 1
    payload = json.loads(events[0].payload_json)["payload"]
    assert payload["previous_decision_id"] == first["decision_id"]
    assert payload["decision_id"] == refreshed["decision_id"]
    assert payload["status"] in {"RECHECKED", "REJECTED"}


def test_a_missing_persisted_decision_row_fails_closed_without_a_call(
    tmp_path: Path,
) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    first = _entry(container, container.llm_provider).recheck(trigger_id)
    with sqlite3.connect(container.settings.data_dir / "oft.db") as connection:
        connection.execute("DELETE FROM decisions WHERE decision_id = ?", (first["decision_id"],))
        connection.commit()

    service = _entry(container, container.llm_provider)
    persisted = service.get_persisted_entry_decision(trigger_id)
    assert persisted["status"] == "MISSING"
    assert persisted["data_quality"] == "MISSING"
    assert persisted["reasons"]

    source = _entry(container, _ExplodingProvider())
    with pytest.raises(EntryRejected) as excinfo:
        source.recheck(trigger_id)
    assert excinfo.value.code == "DATA_UNAVAILABLE"


def test_an_unknown_trigger_still_raises_trigger_not_found(tmp_path: Path) -> None:
    _settings_value, container, _triggers, _trigger_id = _setup(tmp_path)

    with pytest.raises(TriggerNotFound):
        _entry(container, container.llm_provider).get_persisted_entry_decision("TRG-MISSING")


def test_a_corrupt_trigger_payload_is_missing_not_a_crash(tmp_path: Path) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    with sqlite3.connect(container.settings.data_dir / "oft.db") as connection:
        connection.execute(
            "UPDATE trigger_events SET payload_json = ? WHERE trigger_id = ?",
            ("{not-json", trigger_id),
        )
        connection.commit()

    persisted = _entry(container, container.llm_provider).get_persisted_entry_decision(trigger_id)

    assert persisted["status"] == "MISSING"
    assert persisted["decision"] is None
    assert persisted["data_quality"] == "MISSING"
    assert persisted["reasons"] == ["the trigger payload is unreadable"]


def test_refresh_persists_a_new_decision_and_retains_the_previous(
    tmp_path: Path,
) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    service = _entry(container, container.llm_provider)

    default = service.recheck(trigger_id)
    refreshed = service.recheck(trigger_id, refresh=True)

    assert refreshed["refreshed"] is True
    assert refreshed["decision_id"] != default["decision_id"]
    persisted = service.get_persisted_entry_decision(trigger_id)
    assert persisted["entry_decision_id"] == refreshed["decision_id"]
    assert persisted["decision"]["decision_id"] == refreshed["decision_id"]
    new_row = container.store.get_decision(refreshed["decision_id"])
    assert new_row is not None
    assert json.loads(new_row.payload_json)["decision_id"] == refreshed["decision_id"]
    # The previous decision row is retained for the audit trail.
    assert container.store.get_decision(default["decision_id"]) is not None
    audits = [
        event
        for event in container.store.list_audit_events(limit=1000)
        if event.event_type == "ENTRY_RECHECK_REFRESHED"
    ]
    payload = json.loads(audits[-1].payload_json)["payload"]
    assert payload["previous_decision_id"] == default["decision_id"]
    assert payload["decision_id"] == refreshed["decision_id"]


def test_a_provenance_less_persisted_decision_is_partial_and_has_no_marker(
    tmp_path: Path,
) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    service = _entry(container, container.llm_provider)
    first = service.recheck(trigger_id)
    with sqlite3.connect(container.settings.data_dir / "oft.db") as connection:
        row = connection.execute(
            "SELECT payload_json FROM decisions WHERE decision_id = ?", (first["decision_id"],)
        ).fetchone()
        assert row is not None
        envelope = json.loads(row[0])
        envelope.pop("provenance", None)
        connection.execute(
            "UPDATE decisions SET payload_json = ? WHERE decision_id = ?",
            (json.dumps(envelope, sort_keys=True), first["decision_id"]),
        )
        connection.commit()

    persisted = service.get_persisted_entry_decision(trigger_id)
    assert persisted["status"] == "FOUND"
    assert persisted["data_quality"] == "PARTIAL"
    assert persisted["provenance"] is None
    assert any("no provenance" in reason for reason in persisted["reasons"])

    card = service.recheck(trigger_id)
    assert card["persisted"] is True
    assert card["data_quality"] == "PARTIAL"
    assert card["llm"]["provenance"] is None


def test_a_persisted_decision_without_entry_recheck_is_partial(
    tmp_path: Path,
) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    service = _entry(container, container.llm_provider)
    first = service.recheck(trigger_id)
    with sqlite3.connect(container.settings.data_dir / "oft.db") as connection:
        row = connection.execute(
            "SELECT payload_json FROM decisions WHERE decision_id = ?", (first["decision_id"],)
        ).fetchone()
        assert row is not None
        envelope = json.loads(row[0])
        envelope.pop("entry_recheck", None)
        connection.execute(
            "UPDATE decisions SET payload_json = ? WHERE decision_id = ?",
            (json.dumps(envelope, sort_keys=True), first["decision_id"]),
        )
        connection.commit()

    persisted = service.get_persisted_entry_decision(trigger_id)
    assert persisted["status"] == "FOUND"
    assert persisted["data_quality"] == "PARTIAL"
    assert any("no entry_recheck payload" in reason for reason in persisted["reasons"])

    card = service.recheck(trigger_id)
    assert card["persisted"] is True
    assert card["data_quality"] == "PARTIAL"
    assert card["llm"]["decision"] is None


def test_an_empty_persisted_entry_recheck_is_partial_and_has_no_decision(
    tmp_path: Path,
) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    service = _entry(container, container.llm_provider)
    first = service.recheck(trigger_id)
    with sqlite3.connect(container.settings.data_dir / "oft.db") as connection:
        row = connection.execute(
            "SELECT payload_json FROM decisions WHERE decision_id = ?", (first["decision_id"],)
        ).fetchone()
        assert row is not None
        envelope = json.loads(row[0])
        envelope["entry_recheck"] = {}
        connection.execute(
            "UPDATE decisions SET payload_json = ? WHERE decision_id = ?",
            (json.dumps(envelope, sort_keys=True), first["decision_id"]),
        )
        connection.commit()

    persisted = service.get_persisted_entry_decision(trigger_id)
    assert persisted["status"] == "FOUND"
    assert persisted["data_quality"] == "PARTIAL"
    assert any("no usable entry_recheck.decision" in reason for reason in persisted["reasons"])

    card = service.recheck(trigger_id)
    assert card["persisted"] is True
    assert card["data_quality"] == "PARTIAL"
    assert any("no usable entry_recheck.decision" in reason for reason in card["reasons"])
    assert card["llm"]["decision"] is None


def test_an_unknown_persisted_entry_decision_value_is_partial_and_hidden(
    tmp_path: Path,
) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    service = _entry(container, container.llm_provider)
    first = service.recheck(trigger_id)
    with sqlite3.connect(container.settings.data_dir / "oft.db") as connection:
        row = connection.execute(
            "SELECT payload_json FROM decisions WHERE decision_id = ?", (first["decision_id"],)
        ).fetchone()
        assert row is not None
        envelope = json.loads(row[0])
        assert isinstance(envelope["entry_recheck"], dict)
        envelope["entry_recheck"]["decision"] = "MAYBE"
        connection.execute(
            "UPDATE decisions SET payload_json = ? WHERE decision_id = ?",
            (json.dumps(envelope, sort_keys=True), first["decision_id"]),
        )
        connection.commit()

    persisted = service.get_persisted_entry_decision(trigger_id)
    assert persisted["status"] == "FOUND"
    assert persisted["data_quality"] == "PARTIAL"
    assert any("no usable entry_recheck.decision" in reason for reason in persisted["reasons"])

    card = service.recheck(trigger_id)
    assert card["persisted"] is True
    assert card["data_quality"] == "PARTIAL"
    assert any("no usable entry_recheck.decision" in reason for reason in card["reasons"])
    assert card["llm"]["decision"] is None


def test_a_trigger_without_a_persisted_risk_verdict_renders_not_evaluated(
    tmp_path: Path,
) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    service = _entry(container, container.llm_provider)
    service.recheck(trigger_id)
    with sqlite3.connect(container.settings.data_dir / "oft.db") as connection:
        row = connection.execute(
            "SELECT payload_json FROM trigger_events WHERE trigger_id = ?", (trigger_id,)
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload.pop("risk_verdict", None)
        connection.execute(
            "UPDATE trigger_events SET payload_json = ? WHERE trigger_id = ?",
            (json.dumps(payload, sort_keys=True), trigger_id),
        )
        connection.commit()

    card = service.recheck(trigger_id)

    assert card["persisted"] is True
    assert card["risk"]["verdict"] == "NOT_EVALUATED"
    assert card["data_quality"] == "PARTIAL"


def test_a_non_dict_persisted_decision_is_data_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    service = _entry(container, container.llm_provider)
    monkeypatch.setattr(
        service,
        "get_persisted_entry_decision",
        lambda _trigger_id: {
            "trigger_id": trigger_id,
            "entry_decision_id": "DEC-ENTRY-X",
            "status": "FOUND",
            "decision": [],
            "provenance": None,
            "validation_status": None,
            "confidence": None,
            "decision_type": None,
            "reasons": [],
            "data_quality": "COMPLETE",
        },
    )

    with pytest.raises(EntryRejected) as excinfo:
        service.recheck(trigger_id)

    assert excinfo.value.code == "DATA_UNAVAILABLE"


def test_an_unreadable_persisted_decision_payload_is_missing(tmp_path: Path) -> None:
    _settings_value, container, _triggers, trigger_id = _setup(tmp_path)
    service = _entry(container, container.llm_provider)
    first = service.recheck(trigger_id)
    with sqlite3.connect(container.settings.data_dir / "oft.db") as connection:
        connection.execute(
            "UPDATE decisions SET payload_json = ? WHERE decision_id = ?",
            ("{not-json", first["decision_id"]),
        )
        connection.commit()

    persisted = service.get_persisted_entry_decision(trigger_id)

    assert persisted["status"] == "MISSING"
    assert persisted["data_quality"] == "MISSING"
    assert any("unreadable" in reason for reason in persisted["reasons"])
