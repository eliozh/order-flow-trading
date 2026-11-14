"""Replay: the same fixed clock and inputs produce the same management result."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import position_management_config, run_position_management
from oft.adapters.llm_stub import LLMStub
from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import ManagementService
from oft.application.position_service import PositionCloseError, PositionService
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.risk import ManagementAction, ManagementStatus
from oft.ports.llm import LLMRole
from oft.replay.harness import ReplayError
from oft.replay.paper_loop import (
    ReplayRecordingInvalidError,
    ReplayRecordingMissingError,
)
from oft.replay.position_management import (
    ManagementStepExecution,
    ManagementStepOutcome,
    PositionManagementOutcome,
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
    config = replace(
        position_management_config(tmp_path / "recordings", run_id=run_id),
        live_llm=live_llm,
        provider=provider,
        period=period,
    )
    return PositionManagementReplay(data_dir=tmp_path / "data", config=config)


def _scenario(tmp_path: Path, recordings: Path, run_id: str = "management") -> dict[str, Any]:
    return run_position_management(tmp_path, recordings, run_id)


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
def test_a_missing_analysis_recording_raises_the_typed_recording_error(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    recordings = tmp_path / "recordings"
    shutil.copytree(position_management_recordings, recordings)
    shutil.rmtree(recordings / "ANALYSIS")

    with pytest.raises(ReplayRecordingMissingError, match="role=ANALYSIS"):
        _scenario(tmp_path, recordings, "missing-analysis")

    summary = tmp_path / "data" / "replay" / "position-management" / "missing-analysis"
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


# -- H5: funding accrual is validated, never silently zero -------------------


@pytest.mark.replay
def test_the_scenario_accrues_every_seeded_funding_settlement(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    run = _scenario(tmp_path, position_management_recordings, "funding-success")
    funding = run["outcome"]["funding"]

    assert funding["accrued"] == 2
    assert funding["reasons"] == []
    assert Decimal(run["outcome"]["trade_log"]["funding_usd"]) != 0


@pytest.mark.replay
def test_a_degraded_funding_accrual_fails_typed_without_a_summary(
    tmp_path: Path, position_management_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        PositionService,
        "accrue_funding",
        lambda self, position_id, *, now=None: {
            "position_id": position_id,
            "accrued": 0,
            "reasons": ("funding settlement missing",),
        },
    )

    with pytest.raises(PositionManagementReplayError, match="funding accrual is degraded"):
        _scenario(tmp_path, position_management_recordings, "funding-degraded")

    summary = tmp_path / "data" / "replay" / "position-management" / "funding-degraded"
    assert not (summary / "summary.json").exists()


@pytest.mark.replay
def test_the_trade_funding_term_is_required_to_parse_and_be_non_zero() -> None:
    with pytest.raises(PositionManagementReplayError, match="unreadable"):
        PositionManagementReplay._validate_trade_funding({"funding_usd": "not-money"})
    with pytest.raises(PositionManagementReplayError, match="zero"):
        PositionManagementReplay._validate_trade_funding({"funding_usd": "0"})
    with pytest.raises(PositionManagementReplayError, match="non-finite"):
        PositionManagementReplay._validate_trade_funding({"funding_usd": "NaN"})

    # A parsed, non-zero term is accepted (the scenario's real success path).
    PositionManagementReplay._validate_trade_funding({"funding_usd": "-0.07"})


# -- M2: a typed position-close failure is translated ------------------------


@pytest.mark.replay
def test_a_position_close_error_is_raised_as_a_typed_replay_error(
    tmp_path: Path, position_management_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def escalate(self: Any, position_id: str, *, idempotency_key: str, **_: Any) -> Any:
        raise PositionCloseError(
            "POSITION_STATE_INVALID", "the partial close escalated to a full close"
        )

    monkeypatch.setattr(ManagementService, "manage", escalate)

    with pytest.raises(PositionManagementReplayError, match="POSITION_STATE_INVALID"):
        _scenario(tmp_path, position_management_recordings, "close-escalated")

    summary = tmp_path / "data" / "replay" / "position-management" / "close-escalated"
    assert not (summary / "summary.json").exists()


# -- M8: a mismatched management result fails loudly -------------------------


@pytest.mark.replay
def test_a_management_step_that_does_not_match_the_spec_fails_typed(
    tmp_path: Path, position_management_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mismatched(self: Any, position_id: str, *, idempotency_key: str, **_: Any) -> Any:
        return SimpleNamespace(
            action=ManagementAction.HOLD,
            status=ManagementStatus.NO_ACTION,
            executed=False,
            decision_id="MGD-MISMATCH",
            order=None,
            fill=None,
        )

    monkeypatch.setattr(ManagementService, "manage", mismatched)

    with pytest.raises(PositionManagementReplayError, match="returned action HOLD"):
        _scenario(tmp_path, position_management_recordings, "step-mismatch")

    summary = tmp_path / "data" / "replay" / "position-management" / "step-mismatch"
    assert not (summary / "summary.json").exists()


# -- M7: executed/not-executed is a union, not a pair of loose fields --------


@pytest.mark.replay
def test_an_executed_step_requires_an_execution() -> None:
    with pytest.raises(PositionManagementReplayError, match="execution"):
        ManagementStepOutcome(
            action=ManagementAction.PARTIAL_CLOSE,
            decision_id="MGD-1",
            status=ManagementStatus.EXECUTED,
            execution=None,
            size_after="0.01",
        )


@pytest.mark.replay
def test_a_non_executed_step_forbids_an_execution() -> None:
    with pytest.raises(PositionManagementReplayError, match="execution"):
        ManagementStepOutcome(
            action=ManagementAction.HOLD,
            decision_id="MGD-1",
            status=ManagementStatus.NO_ACTION,
            execution=ManagementStepExecution(order_id="ORD-1", fill_id="FIL-1"),
            size_after="0.01",
        )


@pytest.mark.replay
@pytest.mark.parametrize(
    ("field", "build"),
    [
        (
            "decision_id",
            lambda: ManagementStepOutcome(
                ManagementAction.HOLD, "", ManagementStatus.NO_ACTION, None, "0.01"
            ),
        ),
        (
            "size_after",
            lambda: ManagementStepOutcome(
                ManagementAction.HOLD, "MGD-1", ManagementStatus.NO_ACTION, None, " "
            ),
        ),
        ("order_id", lambda: ManagementStepExecution(order_id="", fill_id="FIL-1")),
        ("fill_id", lambda: ManagementStepExecution(order_id="ORD-1", fill_id="")),
    ],
)
def test_a_management_step_requires_non_empty_ids(field: str, build: Any) -> None:
    with pytest.raises(PositionManagementReplayError, match=field):
        build()


@pytest.mark.replay
def test_the_management_step_payload_keeps_the_flat_json_shape() -> None:
    step = ManagementStepOutcome(
        action=ManagementAction.PARTIAL_CLOSE,
        decision_id="MGD-1",
        status=ManagementStatus.EXECUTED,
        execution=ManagementStepExecution(order_id="ORD-1", fill_id="FIL-1"),
        size_after="0.01",
    )

    assert step.to_payload() == {
        "action": "PARTIAL_CLOSE",
        "decision_id": "MGD-1",
        "status": "EXECUTED",
        "executed": True,
        "size_after": "0.01",
        "order_id": "ORD-1",
        "fill_id": "FIL-1",
    }


# -- H3/M9: success artifacts require validated identity fields -------------


def _source_outcome(tmp_path: Path, recordings: Path) -> PositionManagementOutcome:
    replay = PositionManagementReplay(
        data_dir=tmp_path / "source-data",
        config=position_management_config(recordings, run_id="source-outcome"),
    )
    return replay._run_once("attempt-1")


@pytest.mark.replay
def test_an_empty_review_decision_id_leaves_no_summary(
    tmp_path: Path, position_management_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _source_outcome(tmp_path, position_management_recordings)
    broken = replace(outcome, review={"decision_id": "", "status": "VALID", "envelope": {}})
    replay = PositionManagementReplay(
        data_dir=tmp_path / "broken-data",
        config=position_management_config(position_management_recordings, run_id="no-review"),
    )
    monkeypatch.setattr(PositionManagementReplay, "_run_once", lambda self, attempt: broken)

    with pytest.raises(PositionManagementReplayError, match="review_decision_id"):
        replay.run()

    assert not (replay.run_root / "summary.json").exists()


@pytest.mark.replay
def test_a_missing_trade_log_data_quality_leaves_no_summary(
    tmp_path: Path, position_management_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _source_outcome(tmp_path, position_management_recordings)
    broken_trade = {key: value for key, value in outcome.trade_log.items() if key != "data_quality"}
    broken = replace(outcome, trade_log=broken_trade)
    replay = PositionManagementReplay(
        data_dir=tmp_path / "broken-data",
        config=position_management_config(position_management_recordings, run_id="no-quality"),
    )
    monkeypatch.setattr(PositionManagementReplay, "_run_once", lambda self, attempt: broken)

    with pytest.raises(PositionManagementReplayError, match="data_quality"):
        replay.run()

    assert not (replay.run_root / "summary.json").exists()


# -- M12: the explicit outcome projection carries no generated ids ----------


@pytest.mark.replay
def test_the_scenario_outcome_carries_no_generated_scan_or_report_ids(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    run = _scenario(tmp_path, position_management_recordings, "no-generated-ids")

    text = json.dumps(run["outcome"])
    assert "scan_id" not in text
    assert "report_id" not in text


# -- M15: --live-llm is exercised for the management scenario ---------------


@pytest.mark.replay
def test_live_llm_calls_the_provider_once_per_recorded_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.ports.llm import LLMRequest, LLMResult

    calls = {"count": 0}
    original = LLMStub.complete

    def counting(self: LLMStub, request: LLMRequest) -> LLMResult:
        calls["count"] += 1
        return original(self, request)

    monkeypatch.setattr(LLMStub, "complete", counting)
    replay = _scenario_replay(tmp_path, live_llm=True, provider="stub", run_id="live")

    result = replay.run()

    recorded = sorted((tmp_path / "recordings").rglob("*.json"))
    summary = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    assert summary["deterministic"] is True
    assert recorded
    assert calls["count"] == len(recorded)


@pytest.mark.replay
def test_live_llm_without_a_configured_provider_fails_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OFT_LLM_PROVIDER", raising=False)
    replay = _scenario_replay(tmp_path, live_llm=True, run_id="no-provider")

    with pytest.raises(ReplayError, match="no live LLM provider"):
        replay.run()


# -- M11: the summary carries a fixture identity digest ---------------------


@pytest.mark.replay
def test_the_summary_carries_a_fixture_identity_digest(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    run = _scenario(tmp_path, position_management_recordings, "identity")

    digest = run["summary"]["identity_digest"]
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


@pytest.mark.replay
def test_the_identity_digest_is_checkout_independent_and_content_sensitive(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    first = tmp_path / "recordings-a"
    second = tmp_path / "recordings-b"
    shutil.copytree(position_management_recordings, first)
    shutil.copytree(position_management_recordings, second)

    left = _scenario(tmp_path / "left", first, "identity-a")
    right = _scenario(tmp_path / "right", second, "identity-b")
    assert left["summary"]["identity_digest"] == right["summary"]["identity_digest"]

    (second / "EXTRA.txt").write_text("different input", encoding="utf-8")
    changed = _scenario(tmp_path / "changed", second, "identity-c")
    assert changed["summary"]["identity_digest"] != left["summary"]["identity_digest"]
