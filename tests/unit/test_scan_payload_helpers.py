"""Pure payload helpers: data quality, stage cells, and cost aggregation."""

from __future__ import annotations

from decimal import Decimal

from oft.api.routes.scans import (
    _batch_data_quality,
    _cost_totals,
    _symbol_row,
    _symbol_stage_flags,
)
from oft.api.schemas.report import ScanCreateRequest
from oft.application.scan_service import report_summary
from oft.ports.store import ReportRecord, ScanRecord, ScanSymbolRecord


def _child(symbol: str, *, quality: str = "COMPLETE") -> ScanRecord:
    return ScanRecord(
        scan_id=f"SCAN-{symbol}",
        symbol=symbol,
        period="4h",
        status="DONE",
        trigger_source="MANUAL",
        metrics_only=False,
        fixture_path="",
        started_at="t",
        finished_at="t",
        window_start="t",
        window_end="t",
        trade_count=1,
        buy_volume="1",
        sell_volume="1",
        delta="0",
        cvd="0",
        data_quality=quality,
        manifest_path="",
        parquet_rows=1,
        created_at="t",
        report_id=f"RPT-{symbol}",
        batch_id="SCAN-BATCH",
    )


def _row(symbol: str, **overrides: object) -> ScanSymbolRecord:
    values: dict[str, object] = {
        "scan_id": "SCAN-BATCH",
        "symbol": symbol,
        "stage": "DONE",
        "started_at": "t",
        "finished_at": "t",
        "error_code": None,
        "report_id": f"RPT-{symbol}",
        "data_quality": "COMPLETE",
        "llm_status": "VALID",
        "cost_usd": "0.10",
        "cost_unpriced": False,
    }
    values.update(overrides)
    return ScanSymbolRecord(**values)  # type: ignore[arg-type]


def test_llm_degradation_does_not_make_child_data_partial() -> None:
    children = (_child("BTCUSDT"), _child("ETHUSDT"))

    assert _batch_data_quality(children) == "COMPLETE"


def test_child_gaps_drive_batch_data_quality() -> None:
    assert _batch_data_quality((_child("BTCUSDT", quality="PARTIAL"),)) == "PARTIAL"
    assert _batch_data_quality((_child("BTCUSDT", quality="MISSING"),)) == "MISSING"
    assert _batch_data_quality(()) == "MISSING"


def test_failed_row_renders_the_persisted_failing_stage() -> None:
    metrics = _symbol_row(
        _row("ETHUSDT", stage="FAILED", failed_stage="METRICS", report_id=None, llm_status=None)
    )
    assert metrics["data_stage"] == "DONE"
    assert metrics["metrics_stage"] == "FAILED"

    report = _symbol_row(
        _row("ETHUSDT", stage="FAILED", failed_stage="REPORT", report_id=None, llm_status=None)
    )
    assert report["data_stage"] == "DONE"
    assert report["metrics_stage"] == "DONE"
    assert report["llm_stage"] == "SKIPPED"
    assert report["report_stage"] == "FAILED"


def test_legacy_failed_row_uses_the_error_code_stage() -> None:
    flags = _symbol_stage_flags(
        _row(
            "ETHUSDT",
            stage="FAILED",
            error_code="METRICS_INCOMPLETE",
            report_id=None,
            llm_status=None,
        )
    )
    assert flags["metrics_stage"] == "FAILED"


def test_unconfigured_failure_does_not_mark_the_batch_unpriced() -> None:
    rows = (
        _row("BTCUSDT", cost_usd="0.25", cost_unpriced=False),
        _row(
            "XAUUSDT",
            stage="FAILED",
            report_id=None,
            llm_status=None,
            cost_usd="0",
            cost_unpriced=True,
        ),
    )

    cost, unpriced = _cost_totals(rows)

    assert cost == Decimal("0.25")
    assert unpriced is False


def test_unparseable_cost_is_surfaced_as_unpriced() -> None:
    cost, unpriced = _cost_totals((_row("BTCUSDT", cost_usd="not-a-number"),))

    assert cost == Decimal("0")
    assert unpriced is True


def test_metrics_only_report_reports_llm_stage_skipped() -> None:
    report = ReportRecord(
        report_id="RPT-1",
        scan_id="SCAN-1",
        symbol="BTCUSDT",
        timeframe="4h",
        generated_at="t",
        metrics_manifest_json="[]",
        llm_decision_id=None,
        narrative_ref=None,
        data_quality="COMPLETE",
        snapshot_json="{}",
        metrics_json="{}",
        llm_json='{"configured": false, "reason": "LLM not configured"}',
    )

    assert report_summary(report)["llm_stage"] == "SKIPPED"


def test_scan_request_defaults_to_a_non_metrics_only_scan() -> None:
    assert ScanCreateRequest().metrics_only is False
