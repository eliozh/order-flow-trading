"""A clean data dir with no provider still runs a CALC-only scan (D8 acceptance)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from oft.application.scan_service import (
    LLM_PROVIDER_NOT_CONFIGURED_REASON,
    ScanRequest,
    ScanService,
)
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 9, tzinfo=UTC)


def test_a_named_provider_without_a_key_makes_no_call_and_reports_the_reason(
    tmp_path: Path,
) -> None:
    """A named keyless provider fails closed before any provider call (M16/F12)."""
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="openai",
        llm_model_analysis="gpt-fixture",
    )
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"

    run = ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="openai")
    )

    assert run.llm_state["configured"] is False
    assert "LLM_PROVIDER_NOT_CONFIGURED" in run.llm_state["reason"]
    assert container.store.list_llm_calls() == ()
    assert container.store.list_budget_records() == ()


def test_no_provider_runs_a_calc_only_scan_with_an_explicit_reason(tmp_path: Path) -> None:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
    )
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"

    run = ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False)
    )

    assert run.llm_state["configured"] is False
    assert run.llm_state["reason"] == LLM_PROVIDER_NOT_CONFIGURED_REASON
    assert "LLM_PROVIDER_NOT_CONFIGURED" in run.llm_state["reason"]
    report = json.loads(run.report.metrics_json or "{}")
    assert report
    assert container.store.list_opportunities() == ()
    assert container.store.list_orders() == ()
    assert container.store.list_llm_calls() == ()


def test_a_configured_key_never_reaches_reports_decisions_or_logs(tmp_path: Path) -> None:
    from oft.adapters.sqlite_store import database_path
    from oft.logging import configure_logging

    sentinel = "sk-SENTINEL-DO-NOT-LEAK"
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=ROOT / "tests" / "fixtures" / "llm",
        openai_api_key=sentinel,
        api_token=sentinel + "-api",
    )
    configure_logging(settings)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"

    run = ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )

    assert run.llm_state["validation_status"] in {"VALID", "REPAIRED"}
    report_text = " ".join(
        str(value)
        for value in (
            run.report.metrics_json,
            run.report.snapshot_json,
            run.report.llm_json,
        )
    )
    assert sentinel not in report_text
    assert sentinel not in json.dumps(run.llm_state)
    for decision in container.store.list_decisions():
        assert sentinel not in decision.payload_json
    assert sentinel.encode() not in database_path(settings).read_bytes()
    for call in container.store.list_llm_calls():
        assert sentinel not in call.usage_json
    log_path = settings.data_dir / "logs" / "daemon.log"
    assert log_path.is_file()
    assert sentinel not in log_path.read_text(encoding="utf-8")
