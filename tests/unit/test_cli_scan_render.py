"""CLI scan rendering: single-symbol summaries vs batch summaries must not blur."""

from __future__ import annotations

from typing import Any

import pytest


def _scan_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "scan_id": "SCAN-20261106-040000-ABCD1234",
        "symbol": "BTCUSDT",
        "period": "4h",
        "status": "DONE",
        "trigger_source": "MANUAL",
        "metrics_only": False,
        "fixture_path": "",
        "started_at": "2026-11-06T04:00:00.000Z",
        "finished_at": "2026-11-06T04:02:00.000Z",
        "window_start": "2026-11-01T00:00:00.000Z",
        "window_end": "2026-11-06T00:00:00.000Z",
        "trade_count": 100,
        "buy_volume": "10",
        "sell_volume": "9",
        "delta": "1",
        "cvd": "1",
        "data_quality": "COMPLETE",
        "manifest_path": "part.parquet",
        "parquet_rows": 100,
        "created_at": "2026-11-06T04:02:00.000Z",
        "report_id": "RPT-BTCUSDT-1",
        "source_mode": "BACKFILL",
    }
    record.update(overrides)
    return record


def _single_metrics_payload() -> dict[str, Any]:
    """Shape returned by ``POST /api/v1/scans`` for an explicit ``--symbol``."""
    return {
        "scan": _scan_record(),
        "report": {
            "report_id": "RPT-BTCUSDT-1",
            "data_quality": "COMPLETE",
            "llm": {"configured": False},
        },
        "report_id": "RPT-BTCUSDT-1",
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "stage": "DONE",
                "report_id": "RPT-BTCUSDT-1",
                "data_quality": "COMPLETE",
                "error_code": None,
                "error_message": None,
                "llm_status": None,
                "llm_tokens": 0,
                "cost_usd": "0",
                "cost_unpriced": True,
            }
        ],
        "report_links": [
            {"symbol": "BTCUSDT", "report_id": "RPT-BTCUSDT-1", "href": "/reports/RPT-BTCUSDT-1"}
        ],
        "delivery": {"configured": False, "reason": "TELEGRAM_NOT_CONFIGURED"},
    }


def _batch_payload() -> dict[str, Any]:
    """Shape returned by ``POST /api/v1/scans`` for the starred-symbol batch."""
    return {
        "scan_id": "SCAN-BATCH-0001",
        "batch_id": "SCAN-BATCH-0001",
        "status": "PARTIAL",
        "trigger_source": "MANUAL",
        "period": "4h",
        "started_at": "2026-11-06T04:00:00.000Z",
        "finished_at": "2026-11-06T04:03:00.000Z",
        "symbols_total": 2,
        "symbols_completed": 1,
        "symbols_degraded": 0,
        "symbols_failed": 1,
        "total_tokens": 120,
        "total_cost_usd": "0",
        "cost_unpriced": True,
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "stage": "DONE",
                "report_id": "RPT-BTCUSDT-1",
                "llm_status": "VALID",
            },
            {
                "symbol": "XAUUSDT",
                "stage": "FAILED",
                "report_id": None,
                "error_message": "Data gap 2026-11-01 -> 2026-11-06",
            },
        ],
        "report_links": [],
        "delivery": {"configured": False, "reason": "TELEGRAM_NOT_CONFIGURED"},
    }


def test_render_run_keeps_the_single_symbol_summary() -> None:
    from oft.cli.commands.scan import _render_run

    output = _render_run(_single_metrics_payload())

    assert "Scan ID" in output
    assert "SCAN-20261106-040000-ABCD1234" in output
    assert "BTCUSDT" in output
    assert "Symbols          0/0" not in output
    assert "Status            -" not in output


def test_render_run_uses_the_batch_summary_for_batches() -> None:
    from oft.cli.commands.scan import _render_run

    output = _render_run(_batch_payload())

    assert "SCAN-BATCH-0001" in output
    assert "PARTIAL" in output
    assert "1/2" in output
    assert "1 failed" in output
    assert "XAUUSDT" in output


def test_render_detail_does_not_duplicate_the_single_scan_block() -> None:
    from oft.cli.commands.scan import _render_detail

    output = _render_detail(_single_metrics_payload())

    assert output.count("Stages") == 1
    assert output.count("Scan ID") == 1
    # The single-symbol summary is rendered instead of the batch 0/0 block.
    assert "Symbols          0/0" not in output


def test_render_detail_renders_a_batch_once() -> None:
    from oft.cli.commands.scan import _render_detail

    output = _render_detail(_batch_payload())

    assert output.count("SCAN-BATCH-0001") == 1
    assert "Symbols           " in output


@pytest.mark.parametrize(
    ("delivery", "expected"),
    [
        ({"configured": True, "state": "queued"}, "Telegram          queued"),
        ({"configured": True, "state": "sending"}, "Telegram          queued"),
        ({"configured": True, "state": "sent"}, "Telegram          sent"),
        ({"configured": True, "state": "failed"}, "Telegram          failed"),
        (
            {"configured": False, "reason": "TELEGRAM_NOT_CONFIGURED"},
            "Telegram          not configured",
        ),
        (None, "Telegram          not configured"),
    ],
)
def test_render_batch_labels_the_telegram_receipt_state(
    delivery: dict[str, Any] | None, expected: str
) -> None:
    # M-2: ``configured`` means "a receipt exists", not "delivered"; only
    # ``state == sent`` may read as sent, and queued/failed must not.
    from oft.cli.commands.scan import _render_batch

    payload = _batch_payload()
    if delivery is None:
        payload.pop("delivery")
    else:
        payload["delivery"] = delivery

    assert expected in _render_batch(payload)


def test_scan_history_labels_follow_the_receipt_state() -> None:
    from oft.cli.commands.scan import _telegram_label

    assert _telegram_label({"configured": False}) == "not configured"
    assert _telegram_label(None) == "not configured"
    assert _telegram_label({"configured": True, "state": "queued"}) == "queued"
    assert _telegram_label({"configured": True, "state": "sending"}) == "queued"
    assert _telegram_label({"configured": True, "state": "sent"}) == "sent"
    assert _telegram_label({"configured": True, "state": "failed"}) == "failed"
    # A configured receipt without a state never claims it was delivered.
    assert _telegram_label({"configured": True}) == "queued"
