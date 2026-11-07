"""Live-source freshness: fresh data may drive the LLM; stale data never does."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from oft.application.freshness import (
    KLINE_1M_STREAM,
    MARK_STREAM,
    LiveSourceVerdict,
    evaluate_live_source,
)
from oft.application.scan_service import ScanRequest, ScanService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.time import utc_timestamp

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _record(*, deadline: datetime, observed_at: datetime, stale: bool = False) -> dict[str, Any]:
    return {
        "status": "STALE" if stale else "FRESH",
        "data_quality": "STALE" if stale else "COMPLETE",
        "stale": stale,
        "age_seconds": 0.0,
        "budget_seconds": 120.0,
        "last_event_time": utc_timestamp(observed_at),
        "deadline": utc_timestamp(deadline),
        "reason": None,
    }


def _freshness(*, deadline: datetime, observed_at: datetime) -> dict[str, dict[str, Any]]:
    return {
        KLINE_1M_STREAM: _record(deadline=deadline, observed_at=observed_at),
        MARK_STREAM: _record(deadline=deadline, observed_at=observed_at),
    }


def test_fresh_live_input_is_fresh() -> None:
    verdict = evaluate_live_source(
        _freshness(deadline=NOW + timedelta(minutes=2), observed_at=NOW),
        now=NOW,
        connection_state="SUBSCRIBED",
    )

    assert isinstance(verdict, LiveSourceVerdict)
    assert verdict.fresh is True
    assert verdict.reason is None
    assert set(verdict.streams) == {KLINE_1M_STREAM, MARK_STREAM}


def test_exact_deadline_is_still_fresh() -> None:
    verdict = evaluate_live_source(
        _freshness(deadline=NOW, observed_at=NOW - timedelta(minutes=2)),
        now=NOW,
        connection_state="SUBSCRIBED",
    )

    assert verdict.fresh is True


def test_stale_stream_is_not_fresh_and_names_the_stream() -> None:
    freshness = _freshness(
        deadline=NOW - timedelta(seconds=1), observed_at=NOW - timedelta(minutes=3)
    )
    freshness[MARK_STREAM] = _record(
        deadline=NOW + timedelta(minutes=2), observed_at=NOW, stale=False
    )

    verdict = evaluate_live_source(freshness, now=NOW, connection_state="SUBSCRIBED")

    assert verdict.fresh is False
    assert KLINE_1M_STREAM in (verdict.reason or "")


def test_missing_stream_is_not_fresh() -> None:
    verdict = evaluate_live_source(
        {MARK_STREAM: _record(deadline=NOW + timedelta(minutes=1), observed_at=NOW)},
        now=NOW,
        connection_state="SUBSCRIBED",
    )

    assert verdict.fresh is False
    assert KLINE_1M_STREAM in (verdict.reason or "")


def test_missing_state_is_not_fresh() -> None:
    verdict = evaluate_live_source(None, now=NOW, connection_state="SUBSCRIBED")

    assert verdict.fresh is False
    assert "unavailable" in (verdict.reason or "").lower()


def test_disconnected_feed_is_not_fresh_even_with_recent_stamps() -> None:
    verdict = evaluate_live_source(
        _freshness(deadline=NOW + timedelta(minutes=2), observed_at=NOW),
        now=NOW,
        connection_state="DISCONNECTED",
    )

    assert verdict.fresh is False
    assert "DISCONNECTED" in (verdict.reason or "")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _container(tmp_path: Path) -> Any:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True)
    result = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert result.run.status == "COMPLETE"
    return replace(container, clock=FixedClock(NOW))


def _write_ws_state(
    container: Any, *, deadline: datetime, connection_state: str = "SUBSCRIBED"
) -> None:
    run_dir = container.settings.data_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "connection_state": connection_state,
        "streams": {},
        "freshness": _freshness(deadline=deadline, observed_at=NOW - timedelta(seconds=5)),
        "stats": {},
        "updated_at": utc_timestamp(NOW),
        "stale": deadline < NOW,
    }
    (run_dir / "ws_state.json").write_text(json.dumps(document), encoding="utf-8")


def test_live_scan_with_fresh_input_runs_the_llm_and_is_labelled_live(tmp_path: Path) -> None:
    container = _container(tmp_path)
    _write_ws_state(container, deadline=NOW + timedelta(minutes=5))

    run = ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", source="live", metrics_only=False, provider="stub")
    )

    assert run.scan.status == "DONE"
    assert run.scan.source_mode == "LIVE"
    llm_state = json.loads(run.report.llm_json or "{}")
    assert llm_state["configured"] is True
    assert llm_state["validation_status"] in {"VALID", "REPAIRED"}
    assert container.store.list_llm_calls(role="ANALYSIS")
    snapshot = json.loads(run.report.snapshot_json)
    assert snapshot["source_mode"] == "LIVE"
    policy = json.loads(run.report.window_policy_json or "{}")
    assert policy["lookback_days"] == 7
    assert policy["main_window"]["period"] == "4h"
    assert policy["main_window_source"] == "MANIFEST"


def test_stale_live_input_skips_the_llm_with_an_explicit_reason(tmp_path: Path) -> None:
    container = _container(tmp_path)
    _write_ws_state(container, deadline=NOW - timedelta(minutes=5))

    run = ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", source="live", metrics_only=False, provider="stub")
    )

    assert run.scan.status == "DONE"
    llm_state = json.loads(run.report.llm_json or "{}")
    assert llm_state["configured"] is False
    assert llm_state["reason"].startswith("LIVE_DATA_STALE")
    assert container.store.list_llm_calls(role="ANALYSIS") == ()
    # The CALC-only report still exists; stale data never silently drives a narrative.
    assert container.store.get_report(run.report.report_id) is not None


def test_live_scan_without_ws_state_skips_the_llm_explicitly(tmp_path: Path) -> None:
    container = _container(tmp_path)

    run = ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", source="live", metrics_only=False, provider="stub")
    )

    llm_state = json.loads(run.report.llm_json or "{}")
    assert llm_state["configured"] is False
    assert llm_state["reason"].startswith("LIVE_DATA_STALE")
    assert "unavailable" in llm_state["reason"].lower()


def test_live_scan_rejects_a_disconnected_feed(tmp_path: Path) -> None:
    container = _container(tmp_path)
    _write_ws_state(container, deadline=NOW + timedelta(minutes=5), connection_state="DISCONNECTED")

    run = ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", source="live", metrics_only=False, provider="stub")
    )

    llm_state = json.loads(run.report.llm_json or "{}")
    assert llm_state["configured"] is False
    assert "DISCONNECTED" in llm_state["reason"]


@pytest.mark.parametrize("source", ["backfill", "live"])
def test_scan_request_accepts_the_two_supported_sources(source: str) -> None:
    request = ScanRequest(symbol="BTCUSDT", source=source)
    assert request.source == source
