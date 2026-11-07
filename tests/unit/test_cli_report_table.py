"""CLI report table parity: every API section is rendered, unmeasured stays N/A."""

from __future__ import annotations

from typing import Any

from oft.cli.commands.report import render_report_table


def _payload(*, measured: bool) -> dict[str, Any]:
    return {
        "report_id": "RPT-BTCUSDT-20261106-120000-ABCD1234",
        "scan_id": "SCAN-20261106-ABCD1234",
        "symbol": "BTCUSDT",
        "timeframe": "4h",
        "generated_at": "2026-11-06T12:00:00.000Z",
        "data_quality": "MISSING",
        "snapshot": {
            "window": {
                "start": "2026-10-01T00:00:00.000Z",
                "end": "2026-10-02T00:00:00.000Z",
            }
        },
        "metrics": {
            "cvd": {
                "points": [{"delta": "0.20"}],
                "summary": {"cvd_change": "0.20", "delta": "0.20"},
            },
            "volume_profile": {
                "poc": "70000.00",
                "vah": "70001.00",
                "val": "69999.00",
                "bins": [{"price_low": "70000.00"}],
                "bin_size": "0.10",
            },
            "footprint": {"bars": [{"cells": [{"price": "70000.00"}]}]},
            "open_interest": {
                "points": [{"ts": "2026-10-01T00:00:00.000Z"}] if measured else [],
                "delta_over_window": "2" if measured else "0",
                "sampling": "REGULAR",
            },
            "funding": {
                "current_rate": "0.0001" if measured else "0",
                "next_funding_time": "2026-10-01T08:00:00.000Z",
                "history": [{"ts": "2026-10-01T00:00:00.000Z"}] if measured else [],
            },
            "liquidations": {"count": 3, "coverage": "SINCE_DEPLOYMENT"},
            "gaps": [],
        },
        "llm": {"configured": False, "reason": "LLM not configured."},
    }


def test_unmeasured_sections_render_not_measured_not_zero() -> None:
    output = render_report_table(_payload(measured=False))

    assert "OI Δ              not measured" in output
    assert "Funding           not measured" in output
    assert "next 2026-10-01T08:00:00.000Z" not in output
    assert "OI Δ              0" not in output
    assert "Funding           0\n" not in output


def test_measured_sections_render_the_api_values() -> None:
    data = _payload(measured=True)
    output = render_report_table(data)
    metrics = data["metrics"]

    assert f"OI Δ              {metrics['open_interest']['delta_over_window']}" in output
    assert f"Funding           {metrics['funding']['current_rate']}" in output
    assert f"next {metrics['funding']['next_funding_time']}" in output
    assert metrics["volume_profile"]["poc"] in output
    assert f"{metrics['volume_profile']['vah']} / {metrics['volume_profile']['val']}" in output
    assert f"{len(metrics['footprint']['bars'])} bars" in output
    assert f"{metrics['liquidations']['count']} events" in output
    assert metrics["liquidations"]["coverage"] in output


def test_configured_llm_renders_calc_first_then_the_llm_band() -> None:
    data = _payload(measured=True)
    data["llm"] = {
        "configured": True,
        "provider": "stub",
        "model": "fixture",
        "validation_status": "VALID",
        "decision_type": "OPPORTUNITY_PROPOSAL",
        "confidence": 0.72,
        "narrative": "Range high retest with positive delta.",
        "reason": None,
        "opportunity_ids": ["OPP-BTCUSDT-1"],
    }

    output = render_report_table(data)

    assert "CALC" in output
    assert output.index("CALC") < output.index("LLM ›")
    assert "Range high retest with positive delta." in output
    assert "OPP-BTCUSDT-1" in output
    assert "confidence 0.72" in output


def test_rejected_narrative_renders_the_explicit_rejection_without_prose() -> None:
    data = _payload(measured=True)
    data["llm"] = {
        "configured": True,
        "validation_status": "REJECTED",
        "reason": "STOP_SIDE: a LONG stop must be below the entry",
        "narrative": None,
        "opportunity_ids": [],
    }

    output = render_report_table(data)

    assert "LLM rejected — STOP_SIDE" in output
    assert "No narrative, opportunity, or order was created" in output
    assert "LLM ›" not in output
