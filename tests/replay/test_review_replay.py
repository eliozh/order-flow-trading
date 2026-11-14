"""Replay: fixed clock and inputs reproduce the same advisory review decision."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.position_service import PositionService
from oft.application.prompt_service import PromptService
from oft.application.review_service import ReviewService
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from position_support import set_position_mark

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _review_once(tmp_path: Path) -> dict[str, Any]:
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
    accepted = EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=settings,
        kill_switch=KillSwitch(settings=settings, store=container.store, clock=container.clock),
    ).accept(run.trigger.trigger_id, idempotency_key="replay-entry")
    assert accepted.position is not None
    position = accepted.position
    set_position_mark(settings, position["position_id"], position["stop_price"])
    closed = PositionService(
        store=container.store, clock=container.clock, settings=settings
    ).monitor(position["position_id"])
    assert closed.trade_log is not None
    trade_id = closed.trade_log["trade_id"]
    service = ReviewService(
        store=container.store,
        clock=container.clock,
        settings=settings,
        provider=container.llm_provider,
        runtime=container.llm_runtime,
    )
    result = service.review(trade_id, idempotency_key="replay-review")
    decision = container.store.get_decision(result.decision_id)
    assert decision is not None
    envelope = json.loads(decision.payload_json)
    replay = service.review(trade_id, idempotency_key="replay-review")
    provenance = PromptService(store=container.store, clock=container.clock).resolve_decision(
        envelope
    )
    return {
        "trade_id": trade_id,
        "result": result,
        "envelope": envelope,
        "replay": replay,
        "provenance": provenance,
    }


def _deterministic_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Drop the run-generated scan/report ids from the comparison.

    Those two ids embed a per-backfill-run suffix (the backfill run id is
    random), so they differ across two independent runs even though every input
    the review consumes is identical. The decision id, prompt, input hash, and
    validation result are the replay-deterministic parts.
    """
    stable = json.loads(json.dumps(envelope))
    stable["subject"].pop("scan_id", None)
    stable["subject"].pop("report_id", None)
    return stable


def test_the_same_inputs_produce_the_same_review_decision_id(tmp_path: Path) -> None:
    first = _review_once(tmp_path / "first")
    second = _review_once(tmp_path / "second")

    assert first["trade_id"] == second["trade_id"]
    assert first["result"].decision_id == second["result"].decision_id
    assert _deterministic_envelope(first["envelope"]) == _deterministic_envelope(second["envelope"])
    assert first["envelope"]["provenance"]["input_hash"]
    assert first["envelope"]["raw_output_hash"]


def test_a_replayed_review_is_the_persisted_decision_with_exact_replay(
    tmp_path: Path,
) -> None:
    run = _review_once(tmp_path)

    replay = run["replay"]
    assert replay.decision_id == run["result"].decision_id
    assert replay.idempotent_replay is True
    assert replay.to_contract()["rationale"] == run["envelope"]["rationale"]
    assert replay.to_contract()["provenance"] == run["envelope"]["provenance"]
    provenance = run["provenance"]
    assert provenance.status == "available"
    # The replay fixture has no knowledge artifact, so the advisory review was
    # rendered with KNOWLEDGE_STATE: ABSENT. It cannot be replayed exactly even
    # though the prompt version and rendered hash still exist.
    assert provenance.knowledge_status == "ABSENT"
    assert provenance.exact_replay is False
    assert provenance.prompt_id == "review"
    assert provenance.version == 1
    assert provenance.knowledge_status is not None


def test_missing_knowledge_disables_exact_replay_for_the_review(
    tmp_path: Path,
) -> None:
    run = _review_once(tmp_path)

    provenance = run["provenance"]
    recorded = run["envelope"]["provenance"]
    assert recorded["knowledge_status"] == "ABSENT"
    assert recorded["knowledge_reason"] == "KNOWLEDGE_ABSENT"
    assert provenance.status == "available"
    assert provenance.exact_replay is False


# -- position-management scenario (slice 31) --------------------------------


def _scenario(tmp_path: Path, recordings: Path, run_id: str = "review") -> dict[str, Any]:
    from oft.replay.position_management import (
        PositionManagementConfig,
        PositionManagementReplay,
    )

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


def test_the_scenario_review_is_the_same_advisory_decision_across_runs(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    first = _scenario(tmp_path, position_management_recordings, "review-a")
    second = _scenario(tmp_path, position_management_recordings, "review-b")

    assert first["outcome"]["review"] == second["outcome"]["review"]
    assert first["result"].review_decision_id == second["result"].review_decision_id
    assert first["outcome"]["review"]["status"] == "VALID"
    # The review is linked to the closed trade, never to a position or order.
    assert first["outcome"]["trade_log"]["review_decision_id"] == first["result"].review_decision_id
    assert first["result"].trade_id.startswith("TRD-")


def test_a_missing_review_recording_fails_with_the_role_and_input_hash(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    import shutil

    from oft.replay.paper_loop import ReplayRecordingMissingError

    recordings = tmp_path / "recordings"
    shutil.copytree(position_management_recordings, recordings)
    shutil.rmtree(recordings / "REVIEW")

    with pytest.raises(ReplayRecordingMissingError) as raised:
        _scenario(tmp_path, recordings, "missing-review")

    message = str(raised.value)
    assert "role=REVIEW" in message
    assert "input_hash=" in message
    assert "--live-llm" in message
