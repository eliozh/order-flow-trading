"""The 15 s trigger job: claim, batch evaluation, overlap skip, no catch-up."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from oft.adapters.sqlite_store import SqliteStore
from oft.application.jobs import (
    DONE,
    FAILED,
    TRIGGER_JOB_TYPE,
    JobClaim,
    JobLedger,
    TriggerJobRunner,
    align_interval,
)
from oft.application.opportunity_service import OpportunityService
from oft.application.scheduler import TRIGGER_JOB_ID, TriggerDaemonScheduler
from oft.application.trigger_service import TriggerService
from oft.clock import FixedClock
from oft.config import ConfigError, Settings, load_settings
from oft.domain.time import utc_timestamp
from oft.domain.trigger import TriggerSnapshot, evaluate_trigger
from oft.ports.store import (
    JobRunRecord,
    OpportunityEventRecord,
    OpportunityRecord,
    TriggerEventRecord,
)

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
NOW_TEXT = utc_timestamp(NOW)
WINDOW = {"start": "2026-11-01T00:00:00.000Z", "end": "2026-12-01T00:00:00.000Z"}


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(Settings(env="test", port=8765, data_dir=tmp_path / "data"))
    store.initialize()
    return store


def _snapshot() -> TriggerSnapshot:
    return TriggerSnapshot(
        symbol="BTCUSDT",
        as_of=NOW_TEXT,
        last_price=Decimal("70000.00"),
        previous_price=Decimal("69900.00"),
        tick_size=Decimal("0.10"),
        data_quality="COMPLETE",
        snapshot_id="SNAP-TEST",
    )


class FixedProvider:
    def __init__(self, snapshot: TriggerSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot_for(self, opportunity: object) -> TriggerSnapshot:
        return self._snapshot


def _insert_opportunity(store: SqliteStore, opportunity_id: str = "OPP-1") -> str:
    payload = {
        "opportunity_id": opportunity_id,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "state": "OPEN",
        "created_at": NOW_TEXT,
        "updated_at": NOW_TEXT,
        "zone": {"low": "69000.00", "high": "71000.00"},
        "entry_preference": "MARKET",
        "trigger_conditions": [
            {
                "condition_id": "C-1",
                "draft_id": "c1",
                "type": "PRICE_TOUCH_ZONE",
                "params": {"price_low": "69900.00", "price_high": "70100.00"},
                "window": WINDOW,
                "required_data": ["CONTRACT_SPECS", "MARK_PRICE"],
                "evaluable": True,
            }
        ],
        "invalidation_conditions": [],
        "expires_at": "2099-01-01T00:00:00.000Z",
        "thesis": "test",
        "confidence": 0.7,
        "risk_request": {},
        "source": {"report_id": "RPT-1"},
        "provenance": {},
        "data_quality": {},
        "state_history": [
            {
                "from_state": None,
                "to_state": "OPEN",
                "at": NOW_TEXT,
                "reason_code": "CREATED",
                "note": "test",
            }
        ],
    }
    store.save_opportunity(
        OpportunityRecord(
            opportunity_id=opportunity_id,
            symbol="BTCUSDT",
            state="OPEN",
            created_at=NOW_TEXT,
            updated_at=NOW_TEXT,
            payload_json=json.dumps(payload, sort_keys=True),
        ),
        OpportunityEventRecord(
            opportunity_id=opportunity_id,
            seq=1,
            at=NOW_TEXT,
            from_state=None,
            to_state="OPEN",
            reason_code="CREATED",
            note="test",
        ),
    )
    return opportunity_id


def _runner(
    store: SqliteStore,
    *,
    now: datetime = NOW,
    interval_seconds: int = 15,
    service: object | None = None,
) -> TriggerJobRunner:
    resolved_service = service or TriggerService(
        store=store,
        clock=FixedClock(now),
        provider=FixedProvider(_snapshot()),
    )
    return TriggerJobRunner(
        service=resolved_service,  # type: ignore[arg-type]
        ledger=JobLedger(store=store, clock=FixedClock(now)),
        clock=FixedClock(now),
        interval_seconds=interval_seconds,
    )


def test_align_interval_floors_to_the_utc_boundary() -> None:
    assert align_interval(NOW + timedelta(seconds=7), 15) == NOW
    assert align_interval(NOW + timedelta(seconds=15), 15) == NOW + timedelta(seconds=15)
    with pytest.raises(ValueError):
        align_interval(NOW, 0)


def test_a_tick_claims_the_window_and_records_done_with_counts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_opportunity(store)

    outcome = _runner(store).run_tick(NOW)

    expected_window = utc_timestamp(align_interval(NOW, 15))
    assert outcome.claimed is True
    assert outcome.run is not None
    assert outcome.run.status == DONE
    assert outcome.run.window_start == expected_window
    assert outcome.run.source == "trigger"
    assert outcome.run.trigger_source == "SCHEDULED"
    assert outcome.run.message is not None
    assert "triggered=1" in outcome.run.message
    assert outcome.counts.triggered == 1
    assert len(store.list_trigger_events()) == 1
    opportunity = OpportunityService(store=store, clock=FixedClock(NOW)).show("OPP-1")
    assert opportunity.state.value == "TRIGGERED"
    timeline = OpportunityService(store=store, clock=FixedClock(NOW)).timeline("OPP-1")
    assert [event["to_state"] for event in timeline] == ["OPEN", "TRIGGERED"]
    trigger_id = store.list_trigger_events()[0].trigger_id
    assert timeline[-1]["reason_code"] == "TRIGGER_MET"
    assert timeline[-1]["note"] == trigger_id
    assert store.count_orders() == 0
    assert store.count_fills() == 0
    assert store.count_positions() == 0


def test_an_overlapping_tick_is_skipped_and_records_nothing_new(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_opportunity(store)
    runner = _runner(store)

    first = runner.run_tick(NOW)
    second = runner.run_tick(NOW)

    assert first.claimed is True
    assert second.claimed is False
    assert second.reason == "duplicate"
    assert len(store.list_job_runs(TRIGGER_JOB_TYPE)) == 1
    assert len(store.list_trigger_events()) == 1


def test_no_catch_up_and_restart_reconstructs_open_opportunities(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_opportunity(store, "OPP-1")
    _runner(store).run_tick(NOW)

    # A later tick is a new process tick: only the current window is claimed.
    later = NOW + timedelta(seconds=45)
    _insert_opportunity(store, "OPP-2")
    outcome = _runner(store, now=later).run_tick(later)

    assert outcome.claimed is True
    assert outcome.counts.triggered == 1
    windows = {run.window_start for run in store.list_job_runs(TRIGGER_JOB_TYPE)}
    assert windows == {
        utc_timestamp(align_interval(NOW, 15)),
        utc_timestamp(align_interval(later, 15)),
    }
    # The intermediate windows were never claimed or replayed.
    assert len(windows) == 2
    assert len(store.list_trigger_events()) == 2


def test_the_job_reconciles_an_open_opportunity_with_an_existing_pending_trigger(
    tmp_path: Path,
) -> None:
    """The crash window: a PENDING_RECHECK trigger committed, OPEN did not."""
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    opportunity = OpportunityService(store=store, clock=FixedClock(NOW)).show(opportunity_id)
    snapshot = _snapshot()
    evaluation = evaluate_trigger(
        opportunity.trigger_conditions,
        snapshot,
        symbol=opportunity.symbol,
        now=NOW_TEXT,
        snapshot_id=snapshot.snapshot_id,
    )
    key = f"TRG-{opportunity_id}-{evaluation.snapshot_hash[:16].upper()}"
    trigger_id = "TRG-PENDING"
    assert store.save_trigger_event(
        TriggerEventRecord(
            trigger_id=trigger_id,
            opportunity_id=opportunity_id,
            symbol=opportunity.symbol,
            status="PENDING_RECHECK",
            idempotency_key=key,
            triggered_at=NOW_TEXT,
            payload_json=json.dumps({"trigger_id": trigger_id}),
            snapshot_hash=evaluation.snapshot_hash,
            as_of=snapshot.as_of,
            data_quality="COMPLETE",
            condition_count=1,
        )
    )
    assert (
        OpportunityService(store=store, clock=FixedClock(NOW)).show(opportunity_id).state.value
        == "OPEN"
    )

    outcome = _runner(store).run_tick(NOW)

    assert outcome.claimed is True
    assert outcome.counts.triggered == 0
    assert outcome.counts.skipped == 1
    assert len(store.list_trigger_events()) == 1
    stored = OpportunityService(store=store, clock=FixedClock(NOW)).show(opportunity_id)
    assert stored.state.value == "TRIGGERED"
    timeline = OpportunityService(store=store, clock=FixedClock(NOW)).timeline(opportunity_id)
    assert [event["to_state"] for event in timeline] == ["OPEN", "TRIGGERED"]
    assert timeline[-1]["reason_code"] == "TRIGGER_MET"
    assert timeline[-1]["note"] == trigger_id


def test_a_failed_tick_records_a_failed_run(tmp_path: Path) -> None:
    store = _store(tmp_path)

    class FailingService:
        def evaluate_all(self, now: str) -> object:
            raise RuntimeError("boom")

    outcome = _runner(store, service=FailingService()).run_tick(NOW)

    assert outcome.claimed is True
    assert outcome.reason == "failed"
    assert outcome.error == "boom"
    assert outcome.run is not None
    assert outcome.run.status == FAILED
    assert outcome.run.error_code == "JOB_FAILED"


def test_the_trigger_scheduler_is_single_instance_with_coalescing() -> None:
    scheduler = TriggerDaemonScheduler(
        interval_seconds=15,
        job=lambda: None,
    )

    job = scheduler.scheduler.get_job(TRIGGER_JOB_ID)
    assert job is not None
    assert job.max_instances == 1
    assert job.coalesce is True
    assert job.trigger.interval.total_seconds() == 15
    scheduler.shutdown()  # no-op when not running


def test_trigger_interval_config_is_validated() -> None:
    from oft.config import CONFIG_INVALID

    with pytest.raises(ValidationError):
        Settings(trigger_interval_seconds=4)
    with pytest.raises(ValidationError):
        Settings(trigger_interval_seconds=61)
    with pytest.raises(ValidationError):
        Settings(trigger_stale_after_seconds=0)
    with pytest.raises(ConfigError) as interval_error:
        load_settings(trigger_interval_seconds=0)
    with pytest.raises(ConfigError) as stale_error:
        load_settings(trigger_stale_after_seconds=0)
    assert interval_error.value.code == CONFIG_INVALID
    assert stale_error.value.code == CONFIG_INVALID
    assert Settings().trigger_interval_seconds == 15
    assert Settings().trigger_stale_after_seconds == 60


def test_api_evaluate_all_returns_counts_and_list_filters_by_status(tmp_path: Path) -> None:
    from datetime import date

    from fastapi.testclient import TestClient

    from oft.api.app import create_app
    from oft.application.scan_service import ScanRequest, ScanService
    from oft.bootstrap import build_container

    root = Path(__file__).resolve().parents[2]
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=root / "tests" / "fixtures" / "archive",
        llm_provider="stub",
        llm_fixtures_dir=root / "tests" / "fixtures" / "llm",
    )
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    container.backfill.run(
        symbol="BTCUSDT",
        data_type="aggTrades",
        start=date(2026, 10, 1),
        end=date(2026, 10, 7),
    )
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    app = create_app(settings, clock=FixedClock(NOW))
    auth = {"Authorization": f"Bearer {app.state.api_token}"}

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/triggers/evaluate-all",
            headers={**auth, "Idempotency-Key": "evaluate-all-1"},
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["triggered"] == 1
        trigger_id = data["trigger_ids"][0]

        listed = client.get("/api/v1/triggers?status=PENDING_RECHECK", headers=auth).json()
        assert listed["meta"]["pagination"]["total"] == 1
        empty = client.get("/api/v1/triggers?status=ACCEPTED", headers=auth).json()
        assert empty["meta"]["pagination"]["total"] == 0

        shown = client.get(f"/api/v1/triggers/{trigger_id}", headers=auth).json()["data"]
        assert shown["trigger_source"] == "CODE"
        assert shown["condition_results"][0]["met"] is True


def _cli_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OFT_API_TOKEN", raising=False)


class _RecordingClient:
    """A fake ApiClient that records the exact request the CLI makes."""

    def __init__(self, envelope: dict[str, object]) -> None:
        self.envelope = envelope
        self.calls: list[dict[str, object]] = []

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "payload": payload,
                "extra_headers": extra_headers,
            }
        )
        return self.envelope

    def list_triggers(
        self,
        *,
        opportunity_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        from urllib.parse import quote

        query = f"?limit={limit}"
        if opportunity_id:
            query += f"&opportunity_id={quote(opportunity_id, safe='')}"
        if status:
            query += f"&status={quote(status, safe='')}"
        return self._request("GET", f"/api/v1/triggers{query}")

    def evaluate_all_triggers(self, *, idempotency_key: str | None = None) -> dict[str, object]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", "/api/v1/triggers/evaluate-all", {}, extra_headers=headers)

    def get_trigger(self, trigger_id: str) -> dict[str, object]:
        from urllib.parse import quote

        return self._request("GET", f"/api/v1/triggers/{quote(trigger_id, safe='')}")


def test_cli_trigger_list_sends_the_status_filter_and_renders_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from oft.cli.commands import trigger as trigger_command
    from oft.cli.main import app as cli_app

    _cli_environment(monkeypatch, tmp_path)
    client = _RecordingClient(
        {
            "data": {
                "items": [
                    {
                        "trigger_id": "TRG-1",
                        "symbol": "BTCUSDT",
                        "side": "LONG",
                        "status": "PENDING_RECHECK",
                        "created_order_id": None,
                    }
                ],
                "total": 1,
            }
        }
    )
    monkeypatch.setattr(
        trigger_command.ApiClient, "from_settings", lambda settings, **kwargs: client
    )

    result = CliRunner().invoke(
        cli_app, ["trigger", "list", "--status", "PENDING_RECHECK", "--limit", "5"]
    )

    assert result.exit_code == 0, result.output
    assert client.calls[0]["method"] == "GET"
    assert client.calls[0]["path"] == "/api/v1/triggers?limit=5&status=PENDING_RECHECK"
    assert "TRG-1" in result.stdout
    assert "PENDING_RECHECK" in result.stdout


def test_cli_trigger_list_json_keeps_the_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from oft.cli.commands import trigger as trigger_command
    from oft.cli.main import app as cli_app

    _cli_environment(monkeypatch, tmp_path)
    envelope = {"ok": True, "data": {"items": [], "total": 0}, "meta": {}}
    client = _RecordingClient(envelope)
    monkeypatch.setattr(
        trigger_command.ApiClient, "from_settings", lambda settings, **kwargs: client
    )

    result = CliRunner().invoke(cli_app, ["trigger", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == envelope


def test_cli_trigger_evaluate_all_calls_the_batch_path_and_renders_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from oft.cli.commands import trigger as trigger_command
    from oft.cli.main import app as cli_app

    _cli_environment(monkeypatch, tmp_path)
    client = _RecordingClient(
        {
            "data": {
                "open": 3,
                "evaluated": 2,
                "triggered": 1,
                "skipped": 1,
                "stale": 0,
                "unavailable": 1,
                "trigger_ids": ["TRG-1"],
            }
        }
    )
    monkeypatch.setattr(
        trigger_command.ApiClient, "from_settings", lambda settings, **kwargs: client
    )

    result = CliRunner().invoke(cli_app, ["trigger", "evaluate", "--all"])

    assert result.exit_code == 0, result.output
    assert client.calls[0]["method"] == "POST"
    assert client.calls[0]["path"] == "/api/v1/triggers/evaluate-all"
    assert client.calls[0]["payload"] == {}
    headers = client.calls[0]["extra_headers"]
    assert isinstance(headers, dict) and headers.get("Idempotency-Key", "").startswith("cli-all-")
    assert "open=3" in result.stdout
    assert "evaluated=2" in result.stdout
    assert "triggered=1" in result.stdout
    assert "skipped=1" in result.stdout
    assert "unavailable=1" in result.stdout
    assert "triggers TRG-1" in result.stdout


def test_cli_trigger_evaluate_all_json_keeps_the_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from oft.cli.commands import trigger as trigger_command
    from oft.cli.main import app as cli_app

    _cli_environment(monkeypatch, tmp_path)
    envelope = {
        "ok": True,
        "data": {
            "open": 1,
            "evaluated": 1,
            "triggered": 1,
            "skipped": 0,
            "stale": 0,
            "unavailable": 0,
            "trigger_ids": ["TRG-1"],
        },
        "meta": {},
    }
    client = _RecordingClient(envelope)
    monkeypatch.setattr(
        trigger_command.ApiClient, "from_settings", lambda settings, **kwargs: client
    )

    result = CliRunner().invoke(cli_app, ["trigger", "evaluate", "--all", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == envelope


def test_cli_trigger_show_renders_and_emits_the_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from oft.cli.commands import trigger as trigger_command
    from oft.cli.main import app as cli_app

    _cli_environment(monkeypatch, tmp_path)
    payload = {
        "trigger_id": "TRG-1",
        "opportunity_id": "OPP-1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "status": "PENDING_RECHECK",
        "trigger_source": "CODE",
        "triggered_at": NOW_TEXT,
        "idempotency_key": "TRG-OPP-1-KEY",
        "created_order_id": None,
        "rejection_reason": None,
        "condition_results": [
            {
                "condition_id": "C-1",
                "type": "PRICE_TOUCH_ZONE",
                "met": True,
                "observed_value": "70000.00",
                "threshold": "69900.00-70100.00",
            }
        ],
        "snapshot": {
            "market_snapshot_id": "SNAP-TEST",
            "snapshot_hash": "HASH-TEST",
            "as_of": NOW_TEXT,
            "data_quality": "COMPLETE",
        },
    }
    envelope = {"ok": True, "data": payload, "meta": {}}
    client = _RecordingClient(envelope)
    monkeypatch.setattr(
        trigger_command.ApiClient, "from_settings", lambda settings, **kwargs: client
    )

    shown = CliRunner().invoke(cli_app, ["trigger", "show", "TRG-1"])

    assert shown.exit_code == 0, shown.output
    assert "TRIGGER TRG-1" in shown.stdout
    assert "snapshot SNAP-TEST · hash HASH-TEST" in shown.stdout
    assert "C-1 PRICE_TOUCH_ZONE met=True" in shown.stdout
    assert client.calls[0]["path"] == "/api/v1/triggers/TRG-1"

    as_json = CliRunner().invoke(cli_app, ["trigger", "show", "TRG-1", "--json"])

    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.stdout) == envelope


def test_cli_trigger_evaluate_all_rejects_override_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from oft.cli.commands import trigger as trigger_command
    from oft.cli.main import app as cli_app

    _cli_environment(monkeypatch, tmp_path)
    client = _RecordingClient({"data": {}})
    monkeypatch.setattr(
        trigger_command.ApiClient, "from_settings", lambda settings, **kwargs: client
    )

    result = CliRunner().invoke(cli_app, ["trigger", "evaluate", "--all", "--price", "70000.00"])

    assert result.exit_code == 1
    assert "--all cannot be combined" in result.output
    assert client.calls == []


def test_cli_trigger_evaluate_all_rejects_a_positional_opportunity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from oft.cli.commands import trigger as trigger_command
    from oft.cli.main import app as cli_app

    _cli_environment(monkeypatch, tmp_path)
    client = _RecordingClient({"data": {}})
    monkeypatch.setattr(
        trigger_command.ApiClient, "from_settings", lambda settings, **kwargs: client
    )

    result = CliRunner().invoke(cli_app, ["trigger", "evaluate", "OPP-1", "--all"])

    assert result.exit_code == 1
    assert "omit the positional opportunity_id" in result.output
    assert client.calls == []


def test_align_interval_rejects_a_naive_instant() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        align_interval(datetime(2026, 11, 6, 12, 0, 0), 15)


def _running_job() -> JobRunRecord:
    return JobRunRecord(
        run_id="RUN-1",
        job_type=TRIGGER_JOB_TYPE,
        window_start=utc_timestamp(align_interval(NOW, 15)),
        status="RUNNING",
        trigger_source="SCHEDULED",
        started_at=NOW_TEXT,
        finished_at=None,
        error_code=None,
        message=None,
        lease_owner="owner-1",
        lease_expires_at=NOW_TEXT,
        heartbeat_at=NOW_TEXT,
        source="trigger",
    )


class _RejectingLedger:
    """A ledger whose finish write is rejected or raises."""

    def __init__(self, store: SqliteStore, *, raises: bool) -> None:
        self.store = store
        self.raises = raises
        self.finish_calls: list[str] = []
        self.finished = False

    def claim(self, *args: object, **kwargs: object) -> JobClaim:
        return JobClaim(claimed=True, run=_running_job(), reclaimed=False, reason="claimed")

    def finish(self, run_id: str, status: str, **kwargs: object) -> bool:
        self.finish_calls.append(status)
        self.finished = True
        if self.raises:
            raise OSError("ledger down")
        return False


class _BoomService:
    def evaluate_all(self, now: str) -> object:
        raise RuntimeError("boom")


def test_failed_tick_logs_before_finishing_and_preserves_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.application import jobs as jobs_module

    class _RecordingLogger:
        def __init__(self) -> None:
            self.events: list[str] = []

        def warning(self, event: str, **kwargs: object) -> None:
            self.events.append(event)

    logger = _RecordingLogger()
    monkeypatch.setattr(jobs_module, "_LOGGER", logger)
    ledger = _RejectingLedger(_store(tmp_path), raises=True)
    runner = TriggerJobRunner(
        service=_BoomService(),  # type: ignore[arg-type]
        ledger=ledger,  # type: ignore[arg-type]
        clock=FixedClock(NOW),
        interval_seconds=15,
    )

    outcome = runner.run_tick(NOW)

    assert outcome.reason == "failed"
    assert outcome.error == "boom"  # the finish error never masks the tick error
    assert ledger.finish_calls == [FAILED]
    assert logger.events.index("trigger_job_failed") < logger.events.index(
        "trigger_job_finish_failed"
    )


def test_a_done_tick_reports_a_rejected_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.application import jobs as jobs_module

    class _RecordingLogger:
        def __init__(self) -> None:
            self.events: list[str] = []

        def warning(self, event: str, **kwargs: object) -> None:
            self.events.append(event)

    logger = _RecordingLogger()
    monkeypatch.setattr(jobs_module, "_LOGGER", logger)
    store = _store(tmp_path)
    _insert_opportunity(store)
    ledger = _RejectingLedger(store, raises=False)
    runner = TriggerJobRunner(
        service=TriggerService(
            store=store, clock=FixedClock(NOW), provider=FixedProvider(_snapshot())
        ),
        ledger=ledger,  # type: ignore[arg-type]
        clock=FixedClock(NOW),
        interval_seconds=15,
    )

    outcome = runner.run_tick(NOW)

    assert outcome.reason == "finish_rejected"
    assert outcome.counts.triggered == 1
    assert "trigger_job_finish_rejected" in logger.events


def test_evaluate_all_envelope_is_degraded_for_a_stale_or_partial_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    import oft.api.routes.triggers as route_module
    from oft.api.app import create_app
    from oft.application.trigger_service import TriggerBatchOutcome

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings, clock=FixedClock(NOW))
    auth = {"Authorization": f"Bearer {app.state.api_token}"}
    outcomes = [
        TriggerBatchOutcome(open=2, evaluated=1, triggered=0, stale=1, open_total=2),
        TriggerBatchOutcome(open=1, evaluated=1, triggered=1, open_total=1),
        TriggerBatchOutcome(open=1, evaluated=1, triggered=0, open_total=3, capped=True),
    ]

    class _StubService:
        def evaluate_all(self) -> TriggerBatchOutcome:
            return outcomes.pop(0)

    monkeypatch.setattr(route_module, "_trigger_service", lambda request: _StubService())

    with TestClient(app) as client:
        degraded = client.post(
            "/api/v1/triggers/evaluate-all",
            headers={**auth, "Idempotency-Key": "evaluate-all-stale"},
        ).json()
        complete = client.post(
            "/api/v1/triggers/evaluate-all",
            headers={**auth, "Idempotency-Key": "evaluate-all-complete"},
        ).json()
        capped = client.post(
            "/api/v1/triggers/evaluate-all",
            headers={**auth, "Idempotency-Key": "evaluate-all-capped"},
        ).json()

    assert degraded["meta"]["data_quality"] == "PARTIAL"
    assert degraded["data"]["stale"] == 1
    assert degraded["data"]["open_total"] == 2
    assert complete["meta"]["data_quality"] == "COMPLETE"
    # Every loaded row was fresh, but rows were skipped: still DEGRADED.
    assert capped["data"]["capped"] is True
    assert capped["meta"]["data_quality"] == "PARTIAL"


def test_daemon_start_reaps_stale_trigger_runs_and_logs_the_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from oft.cli.commands import daemon as daemon_module

    class _RecordingLogger:
        def __init__(self) -> None:
            self.warnings: list[tuple[str, dict[str, object]]] = []

        def warning(self, event: str, **kwargs: object) -> None:
            self.warnings.append((event, kwargs))

    class _StubScheduler:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.started = False

        def start(self) -> None:
            self.started = True

        def shutdown(self, **kwargs: object) -> None:
            pass

    logger = _RecordingLogger()
    monkeypatch.setattr(daemon_module, "_LOGGER", logger)
    monkeypatch.setattr(daemon_module, "TriggerDaemonScheduler", _StubScheduler)
    store = _store(tmp_path)
    past = NOW - timedelta(minutes=30)
    claim = JobLedger(store=store, clock=FixedClock(past)).claim(
        TRIGGER_JOB_TYPE,
        utc_timestamp(align_interval(past, 15)),
        source="trigger",
    )
    assert claim.claimed is True
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = SimpleNamespace(store=store, clock=FixedClock(NOW), settings=settings)

    scheduler = daemon_module._start_trigger_scheduler(settings, container=container)

    assert scheduler.started is True
    assert scheduler.kwargs["interval_seconds"] == settings.trigger_interval_seconds
    runs = store.list_job_runs(TRIGGER_JOB_TYPE)
    assert [(run.status, run.error_code) for run in runs] == [("FAILED", "LEASE_EXPIRED")]
    assert logger.warnings == [("trigger_stale_runs_reaped", {"count": 1})]
