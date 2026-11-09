"""A corrupt operator price table degrades a scan to CALC, never fails it (M11)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from oft.application.scan_service import ScanRequest, ScanService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 9, tzinfo=UTC)


def test_a_price_table_that_becomes_corrupt_only_degrades_accounting(
    tmp_path: Path,
) -> None:
    """A corrupt price table degrades accounting, not the analysis (M11/F5).

    The scan still completes with the LLM result; the billed call is recorded
    unpriced with an explicit ``LLM_PRICE_TABLE_INVALID`` audit instead of
    abandoning the LLM stage as CALC-only.
    """
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"version": "v", "models": []}), encoding="utf-8")
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
        llm_price_table=prices,
    )
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"
    # Corrupt the table after the settings/container were validated.
    prices.write_text("{not json", encoding="utf-8")

    run = ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )

    assert run.scan.status == "DONE"
    assert run.report.metrics_json
    assert run.llm_state["configured"] is True
    assert run.llm_state["validation_status"] in {"VALID", "REPAIRED"}
    assert run.cost_unpriced is True
    budgets = container.store.list_budget_records()
    assert budgets and all(record.unpriced for record in budgets)
    events = [
        event
        for event in container.store.list_audit_events()
        if event.event_type == "LLM_PRICE_TABLE_INVALID"
    ]
    assert events, "a corrupt price table must surface an explicit audit event"
