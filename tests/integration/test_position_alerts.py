"""Integration: executed and blocked management outcomes push one read-only message."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from test_position_monitor_job import (
    NOW as MONITOR_NOW,
)
from test_position_monitor_job import (
    NOW_TEXT as MONITOR_NOW_TEXT,
)
from test_position_monitor_job import (
    _container,
    _open_long,
)

from oft.adapters.llm_stub import LLMStub
from oft.adapters.sqlite_store import SqliteStore
from oft.api.app import create_app
from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import ManagementService
from oft.application.position_service import DATA_STALE, PositionService
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import POSITION_PUSH_KEY
from oft.domain.time import utc_timestamp
from oft.ports.llm import LLMRole
from oft.ports.store import ConfigRecord
from position_support import set_position_mark

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 10, 8, tzinfo=UTC)
NOW_TEXT = "2026-10-08T00:00:00.000Z"
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"


def _setup(tmp_path: Path, *, fixture: str = "management_partial_close.json") -> dict[str, Any]:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    assert (
        container.backfill.run(
            symbol="BTCUSDT", data_type="aggTrades", start=START, end=END
        ).run.status
        == "COMPLETE"
    )
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert run.trigger is not None
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    entry = EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=kill,
    )
    accepted = entry.accept(run.trigger.trigger_id, idempotency_key="position-alert-entry")
    assert accepted.position is not None
    container.telegram_service.set_token(SENTINEL, actor="test")
    container.telegram_service.update_allowlist([1001], [2002], actor="test")
    provider = LLMStub(fixtures_dir=FIXTURES, overrides={LLMRole.MANAGEMENT: fixture})
    service = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=provider,
        settings=container.settings,
        kill_switch=kill,
        runtime=container.llm_runtime,
        notifications=container.notifications,
    )
    return {
        "container": container,
        "service": service,
        "position": accepted.position,
        "kill": kill,
    }


def _receipts(container: Any) -> list[Any]:
    return list(container.store.list_telegram_receipts())


def _text(receipt: Any) -> str:
    return json.loads(receipt.payload_json)["text"]


def test_an_executed_management_outcome_pushes_one_read_only_message(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position_id = setup["position"]["position_id"]

    result = setup["service"].manage(position_id, idempotency_key="mgmt-partial-alert")

    assert result.status == "EXECUTED"
    receipts = _receipts(container)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.kind == "position_management"
    assert receipt.event_key == f"position:{result.decision_id}:EXECUTED:PARTIAL_CLOSE"
    assert receipt.chat_id == 1001
    text = _text(receipt)
    lines = text.splitlines()
    assert lines[0] == "持仓管理 · BTCUSDT · PARTIAL_CLOSE"
    assert f"position: {position_id}" in lines
    assert "action: PARTIAL_CLOSE" in lines
    assert "status: EXECUTED" in lines
    assert "size: " in text and "size: N/A" not in text
    assert "price: " in text and "price: N/A" not in text
    assert "reason_code: PARTIAL_CLOSE" in lines
    assert SENTINEL not in text
    for forbidden in ("/approve", "/accept", "/reject", "http://", "https://", "inline_keyboard"):
        assert forbidden not in text


def test_a_blocked_management_outcome_pushes_one_message_and_writes_no_order(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, fixture="management_add.json")
    container = setup["container"]
    position_id = setup["position"]["position_id"]
    container.store.set_system_state(
        "risk_caps",
        json.dumps({"max_concurrent_positions": 1}),
        updated_at=NOW_TEXT,
    )
    orders_before = container.store.count_orders()

    result = setup["service"].manage(position_id, idempotency_key="mgmt-add-alert-blocked")

    assert result.status == "BLOCKED"
    assert container.store.count_orders() == orders_before
    receipts = _receipts(container)
    assert len(receipts) == 1
    text = _text(receipts[0])
    assert text.splitlines()[0] == "持仓管理 · BTCUSDT · ADD"
    assert "status: BLOCKED" in text.splitlines()
    assert "reason_code: RISK_BLOCKED" in text.splitlines()
    assert "size: N/A" in text.splitlines()
    assert "price: N/A" in text.splitlines()
    assert SENTINEL not in text


def test_a_replayed_manage_call_does_not_push_a_second_message(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position_id = setup["position"]["position_id"]

    first = setup["service"].manage(position_id, idempotency_key="mgmt-replay-alert")
    second = setup["service"].manage(position_id, idempotency_key="mgmt-replay-alert")

    assert first.decision_id == second.decision_id
    assert len(_receipts(container)) == 1


def test_a_toggled_off_position_push_writes_nothing(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    container.store.set_config(
        ConfigRecord(
            key=POSITION_PUSH_KEY,
            value_json="false",
            updated_at=NOW_TEXT,
            updated_by="test",
        )
    )

    setup["service"].manage(setup["position"]["position_id"], idempotency_key="mgmt-toggled-off")

    assert _receipts(container) == []


def test_an_enqueue_failure_never_changes_the_management_outcome(
    tmp_path: Path, monkeypatch: Any
) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]

    def _boom(self: SqliteStore, record: object) -> bool:
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(SqliteStore, "append_telegram_outbox", _boom)

    result = setup["service"].manage(
        setup["position"]["position_id"], idempotency_key="mgmt-enqueue-fails"
    )

    assert result.status == "EXECUTED"
    assert _receipts(container) == []


def _configured_container(tmp_path: Path) -> Any:
    container = _container(tmp_path)
    container.telegram_service.set_token(SENTINEL, actor="test")
    container.telegram_service.update_allowlist([1001], [2002], actor="test")
    return container


def _position_receipts(container: Any) -> list[Any]:
    return [receipt for receipt in _receipts(container) if receipt.kind == "position_management"]


def test_a_protective_exit_pushes_one_executed_message_and_replays_silently(
    tmp_path: Path,
) -> None:
    container = _configured_container(tmp_path)
    position_id = _open_long(container, "ALERTSTOP")
    set_position_mark(container, position_id, "69000", as_of=MONITOR_NOW_TEXT)
    service = PositionService(
        store=container.store,
        clock=FixedClock(MONITOR_NOW),
        settings=container.settings,
        notifications=container.notifications,
    )

    result = service.monitor(position_id)
    assert result.status == "CLOSED"
    assert result.reason_code == "STOP_TOUCH"

    receipts = _position_receipts(container)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.event_key.endswith(":EXECUTED:FULL_CLOSE")
    text = json.loads(receipt.payload_json)["text"]
    assert "action: FULL_CLOSE" in text.splitlines()
    assert "status: EXECUTED" in text.splitlines()
    assert "reason_code: STOP_TOUCH" in text.splitlines()
    assert "size: N/A" not in text
    assert SENTINEL not in text

    # A second monitor tick replays the closed position without a second push.
    replay = service.monitor(position_id)
    assert replay.status == "CLOSED"
    assert len(_position_receipts(container)) == 1


def test_a_stale_monitor_tick_pushes_one_degraded_message(tmp_path: Path) -> None:
    container = _configured_container(tmp_path)
    position_id = _open_long(container, "ALERTSTALE")
    set_position_mark(
        container,
        position_id,
        "70000",
        as_of=utc_timestamp(MONITOR_NOW - timedelta(seconds=120)),
    )
    service = PositionService(
        store=container.store,
        clock=FixedClock(MONITOR_NOW),
        settings=container.settings,
        notifications=container.notifications,
    )

    first = service.monitor(position_id)
    second = service.monitor(position_id)

    assert first.reason_code == DATA_STALE
    assert second.reason_code == DATA_STALE
    receipts = _position_receipts(container)
    assert len(receipts) == 1
    text = json.loads(receipts[0].payload_json)["text"]
    assert "status: DEGRADED" in text.splitlines()
    assert "reason_code: DATA_STALE" in text.splitlines()


def test_the_api_management_site_enqueues_a_position_message(
    tmp_path: Path, monkeypatch: Any
) -> None:
    setup = _setup(tmp_path)
    position_id = setup["position"]["position_id"]
    monkeypatch.setattr(
        "oft.bootstrap.build_llm_adapter",
        lambda *_: LLMStub(
            fixtures_dir=FIXTURES,
            overrides={LLMRole.MANAGEMENT: "management_partial_close.json"},
        ),
    )
    app = create_app(setup["container"].settings, clock=FixedClock(NOW))
    headers = {"Authorization": f"Bearer {app.state.api_token}"}

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/positions/{position_id}/manage",
            json={"provider": "stub"},
            headers={**headers, "Idempotency-Key": "api-position-alert"},
        )
        assert response.status_code == 200, response.text

    receipts = _position_receipts(app.state.container)
    assert len(receipts) == 1
    assert receipts[0].event_key.endswith(":EXECUTED:PARTIAL_CLOSE")
