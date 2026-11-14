"""Replay: the same fixed clock and inputs produce the same management result."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oft.adapters.llm_stub import LLMStub
from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import ManagementService
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import LLMRole
from oft.replay.paper_loop import (
    ReplayRecordingInvalidError,
    ReplayRecordingMissingError,
)
from oft.replay.position_management import (
    PositionManagementConfig,
    PositionManagementReplay,
    PositionManagementReplayError,
)

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _run(tmp_path: Path) -> dict[str, Any]:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )
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
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    entry = EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=kill,
    )
    accepted = entry.accept(run.trigger.trigger_id, idempotency_key="replay-entry")
    assert accepted.position is not None
    service = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=LLMStub(
            fixtures_dir=FIXTURES,
            overrides={LLMRole.MANAGEMENT: "management_partial_close.json"},
        ),
        settings=container.settings,
        kill_switch=kill,
    )
    result = service.manage(accepted.position["position_id"], idempotency_key="replay-manage")
    record = container.store.get_position(accepted.position["position_id"])
    assert record is not None
    return {
        "container": container,
        "result": result,
        "position": json.loads(record.payload_json),
    }


@pytest.mark.replay
def test_replaying_the_same_management_inputs_is_byte_identical(tmp_path: Path) -> None:
    first = _run(tmp_path / "first")
    second = _run(tmp_path / "second")

    assert first["result"].decision_id == second["result"].decision_id
    assert first["result"].order is not None and second["result"].order is not None
    assert first["result"].order["order_id"] == second["result"].order["order_id"]
    assert first["result"].fill is not None and second["result"].fill is not None
    assert first["result"].fill["fill_id"] == second["result"].fill["fill_id"]
    assert first["position"]["size"] == second["position"]["size"]
    assert (
        first["position"]["management_history"][-1]["decision_id"]
        == second["position"]["management_history"][-1]["decision_id"]
    )
    assert first["container"].store.list_trade_logs(limit=None) == ()


@pytest.mark.replay
def test_a_replayed_management_call_creates_no_new_rows(tmp_path: Path) -> None:
    state = _run(tmp_path)
    container = state["container"]
    orders = container.store.count_orders()
    fills = container.store.count_fills()

    again = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=LLMStub(
            fixtures_dir=FIXTURES,
            overrides={LLMRole.MANAGEMENT: "management_partial_close.json"},
        ),
        settings=container.settings,
        kill_switch=KillSwitch(
            settings=container.settings, store=container.store, clock=container.clock
        ),
    ).manage(state["position"]["position_id"], idempotency_key="replay-manage")

    assert again.idempotent_replay is True
    assert container.store.count_orders() == orders
    assert container.store.count_fills() == fills


# -- position-management scenario (slice 31) --------------------------------


def _scenario_replay(
    tmp_path: Path,
    *,
    live_llm: bool = False,
    provider: str | None = None,
    period: str = "4h",
    run_id: str = "management",
) -> PositionManagementReplay:
    config = PositionManagementConfig(
        symbol="BTCUSDT",
        archive_fixture_dir=ARCHIVE,
        backfill_start=START,
        backfill_end=END,
        recordings_dir=tmp_path / "recordings",
        period=period,
        live_llm=live_llm,
        provider=provider,
        run_id=run_id,
    )
    return PositionManagementReplay(data_dir=tmp_path / "data", config=config)


def _scenario(tmp_path: Path, recordings: Path, run_id: str = "management") -> dict[str, Any]:
    config = PositionManagementConfig(
        symbol="BTCUSDT",
        archive_fixture_dir=ARCHIVE,
        backfill_start=START,
        backfill_end=END,
        recordings_dir=recordings,
        run_id=run_id,
    )
    result = PositionManagementReplay(data_dir=tmp_path / "data", config=config).run()
    summary = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    return {"result": result, "outcome": summary["outcome"]}


@pytest.mark.replay
def test_the_scenario_executes_every_management_action_from_recorded_outputs(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    run = _scenario(tmp_path, position_management_recordings)

    steps = run["outcome"]["management"]
    assert [step["action"] for step in steps] == [
        "PARTIAL_CLOSE",
        "ADD",
        "HOLD",
        "FULL_CLOSE",
    ]
    assert [step["status"] for step in steps] == [
        "EXECUTED",
        "EXECUTED",
        "NO_ACTION",
        "EXECUTED",
    ]
    assert [step["executed"] for step in steps] == [True, True, False, True]
    # HOLD creates no order/fill; it must not invent an execution.
    assert steps[2]["order_id"] is None
    assert steps[2]["fill_id"] is None
    assert all(step["decision_id"].startswith("MGD-") for step in steps)


@pytest.mark.replay
def test_the_scenario_management_ids_are_identical_across_two_fresh_stores(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    first = _scenario(tmp_path, position_management_recordings, "management-a")
    second = _scenario(tmp_path, position_management_recordings, "management-b")

    left = first["outcome"]["management"]
    right = second["outcome"]["management"]
    assert left == right
    for step in left:
        assert step["decision_id"] == step["decision_id"].upper()
    assert first["outcome"]["position_id"] == second["outcome"]["position_id"]


@pytest.mark.replay
def test_a_missing_management_recording_fails_with_the_role_and_input_hash(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    recordings = tmp_path / "recordings"
    shutil.copytree(position_management_recordings, recordings)
    shutil.rmtree(recordings / "MANAGEMENT")

    with pytest.raises(ReplayRecordingMissingError) as raised:
        _scenario(tmp_path, recordings, "missing-management")

    message = str(raised.value)
    assert "role=MANAGEMENT" in message
    assert "input_hash=" in message
    assert "--live-llm" in message
    summary = tmp_path / "data" / "replay" / "position-management" / "missing-management"
    assert not (summary / "summary.json").exists()


@pytest.mark.replay
def test_a_tampered_management_recording_fails_as_invalid(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    recordings = tmp_path / "recordings"
    shutil.copytree(position_management_recordings, recordings)
    recording = sorted((recordings / "MANAGEMENT").glob("*.json"))[0]
    payload = json.loads(recording.read_text(encoding="utf-8"))
    payload["raw_text"] = payload["raw_text"] + "tampered"
    recording.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReplayRecordingInvalidError):
        _scenario(tmp_path, recordings, "tampered-management")


@pytest.mark.replay
def test_live_llm_resolves_the_environment_configured_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``--live-llm`` without ``--provider`` must honour OFT_LLM_PROVIDER, as the
    # entry scenario does. Resolving the settings never calls the provider.
    monkeypatch.setenv("OFT_LLM_PROVIDER", "deepseek")
    replay = _scenario_replay(tmp_path, live_llm=True)

    settings = replay._settings(tmp_path / "attempt-1")

    assert settings.llm_provider == "deepseek"


@pytest.mark.replay
def test_an_explicit_provider_wins_over_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OFT_LLM_PROVIDER", "deepseek")
    replay = _scenario_replay(tmp_path, live_llm=True, provider="openai")

    assert replay._settings(tmp_path / "attempt-1").llm_provider == "openai"


@pytest.mark.replay
def test_a_read_only_scenario_stays_pinned_to_the_offline_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A read-only/capture replay never inherits an ambient live provider.
    monkeypatch.setenv("OFT_LLM_PROVIDER", "deepseek")
    replay = _scenario_replay(tmp_path)

    assert replay._settings(tmp_path / "attempt-1").llm_provider == "stub"


@pytest.mark.replay
@pytest.mark.parametrize("period", ["1h", "2h", "8h", "4H"])
def test_the_scenario_refuses_a_period_other_than_4h(tmp_path: Path, period: str) -> None:
    # The seeded bars, funding offsets, and step clock are a fixed 4h cadence;
    # any other label would publish a bar whose span contradicts its period.
    with pytest.raises(PositionManagementReplayError, match="4h"):
        _scenario_replay(tmp_path, period=period)


class _PositionStore:
    def __init__(self, *position_ids: str) -> None:
        self._position_ids = position_ids

    def list_positions(
        self, *, state: str | None = None, limit: int | None = 100
    ) -> tuple[Any, ...]:
        return tuple(SimpleNamespace(position_id=pid) for pid in self._position_ids)


def _position_container(*position_ids: str) -> Any:
    return SimpleNamespace(store=_PositionStore(*position_ids))


@pytest.mark.replay
def test_position_id_requires_exactly_one_position(tmp_path: Path) -> None:
    replay = _scenario_replay(tmp_path)

    assert replay._position_id(_position_container("POS-ONE")) == "POS-ONE"
    for produced in ((), ("POS-ONE", "POS-TWO")):
        with pytest.raises(PositionManagementReplayError, match="exactly one"):
            replay._position_id(_position_container(*produced))
