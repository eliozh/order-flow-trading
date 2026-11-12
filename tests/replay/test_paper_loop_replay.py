"""Initial deterministic replay check for the paper loop (full harness in slice 21)."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oft.adapters.llm_stub import LLMStub
from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import LLMRequest, LLMResult
from oft.replay.paper_loop import (
    PaperFillSummary,
    PaperLoopConfig,
    PaperLoopOutcome,
    PaperLoopReplay,
    PaperOrderSummary,
    PaperPositionSummary,
    ReplayError,
    _decision_payload,
    _fixture_content_digest,
    _trigger_data_quality,
)

pytestmark = pytest.mark.replay

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


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
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"
    container = replace(container, clock=FixedClock(NOW))
    scan = ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    trigger_run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert trigger_run.trigger is not None
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    entry = EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=kill,
    )
    result = entry.accept(trigger_run.trigger.trigger_id, idempotency_key="replay-accept")
    stored_trigger = container.store.get_trigger_event(trigger_run.trigger.trigger_id)
    assert stored_trigger is not None
    trigger_payload = json.loads(stored_trigger.payload_json)
    decision = container.store.get_decision(str(trigger_payload["entry_decision_id"]))
    return {
        "report_id": scan.report.report_id,
        "decision": None if decision is None else json.loads(decision.payload_json),
        "opportunity": json.loads(opportunity.payload_json),
        "trigger": trigger_payload,
        "order": result.order,
        "fill": result.fill,
        "position": result.position,
        "account": result.account,
    }


def _stable(payload: dict[str, Any]) -> dict[str, Any]:
    opportunity = json.loads(json.dumps(payload["opportunity"]))
    opportunity["source"] = {
        key: value
        for key, value in opportunity["source"].items()
        if key not in {"scan_id", "report_id"}
    }
    return {
        "decision_id": payload["decision"]["decision_id"],
        "decision_validation": payload["decision"]["validation_status"],
        "opportunity": opportunity,
        "trigger": payload["trigger"],
        "order": payload["order"],
        "fill": payload["fill"],
        "position": payload["position"],
        "account": payload["account"],
    }


def test_same_fixture_and_fixed_clock_produce_identical_paper_loop_values(
    tmp_path: Path,
) -> None:
    first = _run(tmp_path / "first")
    second = _run(tmp_path / "second")

    left = _stable(first)
    right = _stable(second)

    assert left == right
    assert left["order"]["client_order_id"].startswith("oft-paper-BTCUSDT-entry-")
    assert left["order"]["order_id"] == right["order"]["order_id"]
    assert left["fill"]["fill_id"] == right["fill"]["fill_id"]
    assert left["position"]["position_id"] == right["position"]["position_id"]
    assert (
        left["trigger"]["snapshot"]["snapshot_hash"]
        == right["trigger"]["snapshot"]["snapshot_hash"]
    )


def test_replay_does_not_invent_a_provider_or_credential(tmp_path: Path) -> None:
    payload = _run(tmp_path)

    assert payload["decision"]["provenance"]["provider"] == "stub"
    assert payload["decision"]["provenance"]["model"] == "fixture"
    assert payload["decision"]["provenance"]["cost"]["unpriced"] is True


# -- full paper-loop replay (slice 21) --------------------------------------


def _paper_loop(tmp_path: Path, recordings: Path) -> Any:
    config = PaperLoopConfig(
        symbol="BTCUSDT",
        archive_fixture_dir=ARCHIVE,
        backfill_start=START,
        backfill_end=END,
        recordings_dir=recordings,
        run_id="paper-loop",
    )
    return PaperLoopReplay(data_dir=tmp_path / "replay-data", config=config).run()


def _outcome(result: Any) -> dict[str, Any]:
    return json.loads(Path(result.summary_path).read_text(encoding="utf-8"))["outcome"]


def _summary(result: Any) -> dict[str, Any]:
    return json.loads(Path(result.summary_path).read_text(encoding="utf-8"))


def test_the_full_loop_replays_decisions_triggers_orders_fills_and_positions(
    tmp_path: Path, replay_recordings: Path
) -> None:
    result = _paper_loop(tmp_path, replay_recordings)
    outcome = _outcome(result)

    # ``deterministic`` is Literal[True]; verify the persisted signal instead of
    # restating a constructor default (A2).
    assert _summary(result)["deterministic"] is True
    assert result.data_quality == outcome["data_quality"] == "PARTIAL"
    assert outcome["trigger"]["snapshot"]["data_quality"] == "PARTIAL"
    assert outcome["decision_id"] == result.decision_id
    assert outcome["decision_validation"] == "VALID"
    assert outcome["metric_version"].startswith("orderflow_metrics.v1.")
    assert outcome["prompt_version"] == 1
    # The recorded source preserves the original provenance; replay invents none.
    assert (outcome["llm_provider"], outcome["llm_model"]) == ("stub", "fixture")

    trigger = outcome["trigger"]
    assert trigger["trigger_id"] == result.trigger_id
    assert trigger["snapshot"]["snapshot_hash"]

    order = outcome["order"]
    assert order["order_id"] == result.order.order_id
    assert order["status"] == result.order.status == "FILLED"
    assert order["client_order_id"].startswith("oft-paper-BTCUSDT-entry-")

    fill = outcome["fill"]
    assert fill["fill_id"] == result.fill.fill_id
    assert fill["source"] == result.fill.source == "PAPER"
    assert fill["price"] == result.fill.price
    assert fill["fee"] == result.fill.fee

    position = outcome["position"]
    assert position["position_id"] == result.position.position_id
    assert position["entry_price"] == fill["price"]
    assert position["avg_entry_price"] == fill["price"]
    assert position["size"] == result.position.size
    assert position["notional_usd"] == result.position.notional_usd
    assert position["fees_usd"] == fill["fee"]
    # This milestone records no PnL; a replay must not fabricate one.
    assert position["unrealized_pnl_usd"] is None
    assert outcome["account"]["balance"] != "10000.00"


def test_the_result_nests_required_order_fill_and_position_value_objects(
    tmp_path: Path, replay_recordings: Path
) -> None:
    result = _paper_loop(tmp_path, replay_recordings)

    assert isinstance(result.order, PaperOrderSummary)
    assert isinstance(result.fill, PaperFillSummary)
    assert isinstance(result.position, PaperPositionSummary)
    assert result.order.order_id and result.fill.fill_id and result.position.position_id


@pytest.mark.parametrize(
    ("build", "field"),
    [
        (lambda: PaperOrderSummary(order_id="", status="FILLED", client_order_id="c"), "order_id"),
        (lambda: PaperFillSummary(fill_id=" ", price="1", fee="0", source="PAPER"), "fill_id"),
        (lambda: PaperPositionSummary(position_id="", size="1", notional_usd="1"), "position_id"),
    ],
)
def test_a_result_value_object_refuses_an_empty_id(build: Any, field: str) -> None:
    with pytest.raises(ReplayError, match=field):
        build()


def test_a_missing_accepted_decision_fails_loudly() -> None:
    with pytest.raises(ReplayError, match="disappeared"):
        _decision_payload(None)


def test_an_empty_validation_status_fails_loudly() -> None:
    record = SimpleNamespace(payload_json='{"validation_status": ""}')

    with pytest.raises(ReplayError, match="validation_status"):
        _decision_payload(record)  # type: ignore[arg-type]


def test_the_trigger_data_quality_is_read_from_the_snapshot() -> None:
    payload = {"snapshot": {"data_quality": "PARTIAL"}}

    assert _trigger_data_quality(payload) == "PARTIAL"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data_quality": "PARTIAL"},
        {"snapshot": {}},
        {"snapshot": {"data_quality": ""}},
        {"snapshot": {"data_quality": "UNKNOWN"}},
        {"snapshot": "not-an-object"},
    ],
)
def test_a_missing_or_invalid_trigger_data_quality_fails_loudly(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ReplayError, match="snapshot|data_quality"):
        _trigger_data_quality(payload)


def test_the_fixture_content_digest_ignores_the_checkout_path(tmp_path: Path) -> None:
    first = tmp_path / "checkout-a"
    second = tmp_path / "checkout-b"
    first.mkdir()
    second.mkdir()
    (first / "sample.csv").write_text("symbol,volume\nBTCUSDT,1\n", encoding="utf-8")
    (second / "sample.csv").write_text("symbol,volume\nBTCUSDT,1\n", encoding="utf-8")

    assert _fixture_content_digest(first) == _fixture_content_digest(second)

    (second / "sample.csv").write_text("symbol,volume\nBTCUSDT,2\n", encoding="utf-8")

    assert _fixture_content_digest(first) != _fixture_content_digest(second)


def test_replaying_the_same_scenario_twice_is_identical(
    tmp_path: Path, replay_recordings: Path
) -> None:
    first = _paper_loop(tmp_path / "first", replay_recordings)
    second = _paper_loop(tmp_path / "second", replay_recordings)

    assert _outcome(first) == _outcome(second)
    assert first.order.order_id == second.order.order_id
    assert first.fill.fill_id == second.fill.fill_id
    assert first.position.position_id == second.position.position_id
    assert first.fill.price == second.fill.price
    assert first.fill.fee == second.fill.fee
    assert first.position.notional_usd == second.position.notional_usd


def test_rerunning_an_existing_run_fails_before_writing_anything(
    tmp_path: Path, replay_recordings: Path
) -> None:
    first = _paper_loop(tmp_path, replay_recordings)
    root = tmp_path / "replay-data"
    before_files = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    before_dirs = {path.relative_to(root) for path in root.rglob("*") if path.is_dir()}

    with pytest.raises(ReplayError, match="already exists.*--run-id"):
        _paper_loop(tmp_path, replay_recordings)

    after_files = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    after_dirs = {path.relative_to(root) for path in root.rglob("*") if path.is_dir()}
    # Not just summary.json: no attempt directory may be created or touched.
    assert after_files == before_files
    assert after_dirs == before_dirs
    assert (
        Path(first.summary_path).read_bytes()
        == before_files[Path(first.summary_path).relative_to(root)]
    )


def _complete_outcome(**overrides: Any) -> PaperLoopOutcome:
    """A structurally valid outcome so only the overridden field can fail."""
    base = PaperLoopOutcome(
        decision_id="decision-1",
        decision_validation="VALID",
        metric_version="orderflow_metrics.v1.0.0",
        prompt_version=1,
        knowledge_artifact_hash="hash",
        llm_provider="stub",
        llm_model="fixture",
        data_quality="PARTIAL",
        opportunity={"source": {"scan_id": "scan-1", "report_id": "report-1"}},
        trigger={"trigger_id": "trigger-1"},
        order={"order_id": "order-1", "status": "FILLED", "client_order_id": "client-1"},
        fill={"fill_id": "fill-1", "price": "100", "fee": "0.1", "source": "PAPER"},
        position={"position_id": "position-1", "size": "1", "notional_usd": "100"},
        account={"balance": "9999.9"},
        risk_verdict={"verdict": "PASS"},
    )
    return replace(base, **overrides)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"order": None}, "no paper order"),
        ({"fill": None}, "no paper fill"),
        ({"position": None}, "no paper position"),
    ],
)
def test_a_missing_paper_artifact_fails_before_the_summary_is_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, Any],
    message: str,
) -> None:
    config = PaperLoopConfig(
        symbol="BTCUSDT",
        archive_fixture_dir=ARCHIVE,
        backfill_start=START,
        backfill_end=END,
        recordings_dir=tmp_path / "recordings",
        run_id="missing-artifact",
    )
    replay = PaperLoopReplay(data_dir=tmp_path / "replay-data", config=config)
    outcome = _complete_outcome(**override)
    # Both attempts return the same outcome, so determinism passes and only the
    # missing artifact can stop the run.
    monkeypatch.setattr(PaperLoopReplay, "_run_once", lambda self, attempt: outcome)

    with pytest.raises(ReplayError, match=message):
        replay.run()

    # A failed validation must not leave a success-looking deterministic summary.
    assert not (replay.run_root / "summary.json").exists()
    assert not replay.run_root.exists()


def test_ambient_paper_and_risk_settings_cannot_change_a_replay(
    tmp_path: Path, replay_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _paper_loop(tmp_path / "baseline", replay_recordings)
    for name, value in {
        "OFT_PAPER_INITIAL_BALANCE": "1.00",
        "OFT_PAPER_SLIPPAGE_BPS": "9000",
        "OFT_PAPER_TAKER_FEE_BPS": "9000",
        "OFT_RISK_PER_TRADE_PCT": "100",
        "OFT_RISK_MAX_CONCURRENT_POSITIONS": "1",
        "OFT_RISK_DAILY_LOSS_PCT": "0.1",
        "OFT_RISK_MAX_LEVERAGE": "125",
        "OFT_RISK_MAX_NOTIONAL_PCT": "100",
        "OFT_RISK_MAX_SLIPPAGE_BPS": "10000",
        "OFT_RISK_MAX_SPREAD_BPS": "10000",
        "OFT_RISK_STALE_MARK_SECONDS": "1",
        # Ambient fixture wiring must not reach sizing/risk/data quality either.
        "OFT_FIXTURES_DIR": str(tmp_path / "ambient-market"),
        "OFT_KLINES_FIXTURE": str(tmp_path / "ambient-klines.csv"),
        "OFT_EXCHANGE_INFO_FIXTURE": str(tmp_path / "ambient-exchange-info.json"),
    }.items():
        monkeypatch.setenv(name, value)

    perturbed = _paper_loop(tmp_path / "perturbed", replay_recordings)

    assert _outcome(perturbed) == _outcome(baseline)
    assert _summary(perturbed)["settings_digest"] == _summary(baseline)["settings_digest"]


def test_the_settings_digest_is_independent_of_the_checkout_fixture_path(
    tmp_path: Path, replay_recordings: Path
) -> None:
    first_archive = tmp_path / "checkout-a" / "archive"
    second_archive = tmp_path / "checkout-b" / "archive"
    shutil.copytree(ARCHIVE, first_archive)
    shutil.copytree(ARCHIVE, second_archive)

    def run(data_root: Path, archive: Path) -> dict[str, Any]:
        config = PaperLoopConfig(
            symbol="BTCUSDT",
            archive_fixture_dir=archive,
            backfill_start=START,
            backfill_end=END,
            recordings_dir=replay_recordings,
            run_id="digest",
        )
        return _summary(PaperLoopReplay(data_dir=data_root, config=config).run())

    first = run(tmp_path / "first", first_archive)
    second = run(tmp_path / "second", second_archive)

    # Byte-identical fixtures in different checkout directories share a digest.
    assert first["settings_digest"] == second["settings_digest"]
    assert first["outcome"] == second["outcome"]


def test_live_llm_calls_the_provider_once_per_recorded_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"count": 0}
    original = LLMStub.complete

    def counting(self: LLMStub, request: LLMRequest) -> LLMResult:
        calls["count"] += 1
        return original(self, request)

    monkeypatch.setattr(LLMStub, "complete", counting)
    recordings = tmp_path / "recordings"
    config = PaperLoopConfig(
        symbol="BTCUSDT",
        archive_fixture_dir=ARCHIVE,
        backfill_start=START,
        backfill_end=END,
        recordings_dir=recordings,
        live_llm=True,
        provider="stub",
        run_id="live",
    )

    result = PaperLoopReplay(data_dir=tmp_path / "data", config=config).run()

    recorded = sorted(recordings.rglob("*.json"))
    assert _summary(result)["deterministic"] is True
    assert recorded
    # One provider call per request; attempt-2 read the recordings instead of
    # calling the provider again (which would double the count).
    assert calls["count"] == len(recorded)
