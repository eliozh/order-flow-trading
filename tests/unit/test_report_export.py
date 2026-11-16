"""Pure renderer tests for the report export (slice 42).

The renderer is deterministic and allowlisted: only the known report sections
reach the Markdown, every dynamic value is escaped, and secrets/raw prompts are
removed before rendering.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import typer

from oft.api.envelope import success_envelope
from oft.application.report_export import (
    escape_markdown_text,
    provenance_counts,
    redact_report_payload,
    render_report_json,
    render_report_markdown,
)
from oft.cli.commands import report
from oft.cli.commands.report import export_exit_code


def _payload() -> dict[str, Any]:
    return {
        "report_id": "RPT-BTCUSDT-20261007-120000-ABCD1234",
        "scan_id": "SCAN-20261007-ABCD1234",
        "symbol": "BTCUSDT",
        "timeframe": "4h",
        "generated_at": "2026-10-07T12:00:00.000Z",
        "data_quality": "PARTIAL",
        "llm": {
            "configured": True,
            "provider": "stub",
            "model": "fixture",
            "prompt_id": "analysis",
            "prompt_version": 3,
            "validation_status": "VALID",
            "decision_type": "OPPORTUNITY_PROPOSAL",
            "confidence": 0.72,
            "narrative": "Price is rotating inside the range.",
            "opportunity_ids": ["OPP-111", "OPP-222"],
            "cost": {"amount_usd": "0.0042", "unpriced": False},
            "usage": {"total_tokens": 1234},
        },
        "llm_tokens": 1234,
        "cost_usd": "0.0042",
        "cost_unpriced": False,
        "snapshot": {
            "window": {
                "start": "2026-10-01T00:00:00.000Z",
                "end": "2026-10-08T00:00:00.000Z",
            },
            "data_quality": "PARTIAL",
            "quality_reasons": ["open_interest not measured in this slice"],
        },
        "metrics": {
            "data_quality": "PARTIAL",
            "quality_reasons": ["funding not measured in this slice"],
            "cvd": {
                "points": [
                    {"bar_start": "2026-10-07T08:00:00.000Z", "delta": "-12.5", "cvd": "900"}
                ],
                "summary": {
                    "cvd_change": "123.45",
                    "delta": "-12.5",
                    "buy_volume": "10",
                    "sell_volume": "5",
                },
            },
            "volume_profile": {
                "poc": "70000.00",
                "vah": "70500.00",
                "val": "69500.00",
                "bin_size": "50",
                "bins": [{"price": "70000.00"}, {"price": "70050.00"}, {"price": "70100.00"}],
            },
            "footprint": {"bars": [{"cells": [{}, {}]}, {"cells": [{}]}]},
            "open_interest": {
                "points": [],
                "delta_over_window": "0",
                "sampling": "REGULAR",
            },
            "funding": {"history": [], "current_rate": "0"},
            "liquidations": {"count": 7, "coverage": "SINCE_DEPLOYMENT"},
            "gaps": [{"gap_id": "GAP-1"}],
        },
    }


def _render(payload: dict[str, Any]) -> str:
    return render_report_markdown(payload)


def test_markdown_renders_the_heading_metadata_calc_and_llm_band() -> None:
    text = _render(_payload())

    assert text.startswith("# REPORT RPT-BTCUSDT-20261007-120000-ABCD1234 · BTCUSDT · 4h\n")
    assert "Window: 2026-10-01T00:00:00.000Z → 2026-10-08T00:00:00.000Z" in text
    assert "Generated: 2026-10-07T12:00:00.000Z" in text
    assert "Scan: SCAN-20261007-ABCD1234" in text
    assert "Data quality: PARTIAL" in text
    assert "Provenance: CALC (metrics) · LLM (narrative)" in text
    assert "Prompt: analysis v3" in text
    assert "Decision: OPPORTUNITY_PROPOSAL" in text
    assert "Confidence: 0.72" in text
    assert "Provider: stub/fixture" in text
    assert "Cost: 0.0042 USD (priced) · tokens 1234" in text
    assert "Opportunities: OPP-111, OPP-222" in text

    assert "## CALC metrics" in text
    assert "| CVD (window) | 123.45 |" in text
    assert "| Delta (latest) | -12.5 |" in text
    assert "| POC | 70000.00 |" in text
    assert "| VAH / VAL | 70500.00 / 69500.00 |" in text
    assert "| Volume Profile | 3 bins · 50 per bin |" in text
    assert "| Footprint | 2 bars · 3 cells |" in text
    assert "| Liquidations | 7 events · SINCE_DEPLOYMENT |" in text
    assert "| Gaps | 1 |" in text
    assert "LLM › Price is rotating inside the range." in text

    assert "## Data quality stamps" in text
    assert "| snapshot | PARTIAL |" in text
    assert "| quality_reasons | open_interest not measured in this slice |" in text
    assert "## Provenance" in text
    assert "CALC blocks: 6 · LLM blocks: 1" in text


def test_unmeasured_open_interest_and_funding_render_na_with_a_reason() -> None:
    text = _render(_payload())

    assert "| Open interest Δ | N/A — open interest not measured |" in text
    assert "| Funding | N/A — funding not measured |" in text
    assert "0 · 0" not in text


def test_data_quality_stamps_use_the_metrics_and_snapshot_levels() -> None:
    # The contract carries ``data_quality`` on the metrics and snapshot objects,
    # not on each metric family.
    text = _render(_payload())

    assert "| CVD (window) | 123.45 | PARTIAL |" in text
    assert "| Delta (latest) | -12.5 | PARTIAL |" in text
    assert "| Gaps | 1 | PARTIAL |" in text
    assert "| metrics | PARTIAL |" in text
    assert "| snapshot | PARTIAL |" in text
    assert "| quality_reasons | open_interest not measured in this slice |" in text


@pytest.mark.parametrize("stamp", ["STALE", "MISSING"])
def test_metrics_level_stamp_is_preserved_literally(stamp: str) -> None:
    payload = copy.deepcopy(_payload())
    payload["metrics"]["data_quality"] = stamp

    text = _render(payload)

    assert f"| metrics | {stamp} |" in text
    assert f"| CVD (window) | 123.45 | {stamp} |" in text
    assert f"| Gaps | 1 | {stamp} |" in text


def test_a_per_family_stamp_is_honored_defensively_when_present() -> None:
    # Forward compatibility only: the metrics contract has no per-family stamp,
    # but a future document that adds one must not be ignored.
    payload = copy.deepcopy(_payload())
    payload["metrics"]["cvd"]["data_quality"] = "STALE"

    text = _render(payload)

    assert "| cvd | STALE |" in text
    assert "| CVD (window) | 123.45 | STALE |" in text
    assert "| POC | 70000.00 | PARTIAL |" in text


def test_a_hostile_narrative_is_html_escaped_and_never_renders_a_tag() -> None:
    payload = copy.deepcopy(_payload())
    payload["llm"]["narrative"] = "<script>alert('x')</script><img src=x onerror=alert(1)>"

    text = _render(payload)

    assert "LLM › &lt;script&gt;alert('x')&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;" in text
    assert "<script" not in text
    assert "<img" not in text
    assert "&lt;script" in text
    assert "&lt;img" in text


def test_newlines_and_pipes_are_contained_in_the_narrative() -> None:
    payload = copy.deepcopy(_payload())
    payload["llm"]["narrative"] = "first line\n| injected | second line"

    text = _render(payload)

    assert "first line \\| injected \\| second line" in text
    assert "\n| injected" not in text


def _reversed_keys(value: Any) -> Any:
    """Recursively rebuild mappings with their keys inserted in reverse order."""
    if isinstance(value, dict):
        return {key: _reversed_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reversed_keys(item) for item in value]
    return value


def test_markdown_is_deterministic_across_insertion_order() -> None:
    payload = _payload()

    assert _render(payload) == _render(_reversed_keys(payload))


def test_markdown_ends_with_exactly_one_trailing_newline() -> None:
    text = _render(_payload())

    assert text.endswith("\n")
    assert not text.endswith("\n\n")


def test_json_rendering_round_trips_a_hostile_narrative_as_data() -> None:
    payload = copy.deepcopy(_payload())
    hostile = "</script><script>alert(1)</script>"
    payload["llm"]["narrative"] = hostile
    envelope = success_envelope(
        payload,
        request_id="REQ-TEST",
        generated_at="2026-10-07T12:00:00.000Z",
        provenance="CALC",
        data_quality="PARTIAL",
    )

    text = render_report_json(envelope)
    document = json.loads(text)

    assert text.endswith("\n")
    assert document["data"]["llm"]["narrative"] == hostile
    assert document["ok"] is True
    assert document["meta"]["provenance"] == "CALC"


def test_redaction_drops_secrets_and_prompts_but_keeps_prompt_metadata() -> None:
    payload: dict[str, Any] = {
        "api_key": "sk-super-secret",
        "prompt": "RAW-PROMPT-BODY",
        "authorization": "Bearer super-secret",
        "secret_key": "secret",
        "password": "hunter2",
        "headers": {"authorization": "x", "accept": "application/json"},
        "credentials": {"token": "t"},
        "prompt_id": "analysis",
        "prompt_version": 3,
        "llm_tokens": 10,
        "total_tokens": 20,
        "nested": [{"token": "t", "keep": 1, "prompt_text": "raw"}],
    }

    redacted = redact_report_payload(payload)

    for key in (
        "api_key",
        "prompt",
        "authorization",
        "secret_key",
        "password",
        "headers",
        "credentials",
    ):
        assert key not in redacted, key
    assert redacted["prompt_id"] == "analysis"
    assert redacted["prompt_version"] == 3
    assert redacted["llm_tokens"] == 10
    assert redacted["total_tokens"] == 20
    assert redacted["nested"] == [{"keep": 1}]
    # The source payload is never mutated.
    assert payload["api_key"] == "sk-super-secret"


def test_provenance_counts_calc_families_and_a_valid_llm_band() -> None:
    counts = provenance_counts(_payload())

    assert counts == {"calc": 6, "llm": 1}


def test_provenance_counts_do_not_credit_rejected_or_absent_llm_bands() -> None:
    rejected = copy.deepcopy(_payload())
    rejected["llm"]["validation_status"] = "REJECTED"
    assert provenance_counts(rejected)["llm"] == 0

    absent = copy.deepcopy(_payload())
    absent["llm"] = {"configured": False, "reason": "LLM not configured."}
    assert provenance_counts(absent)["llm"] == 0


def test_rejected_llm_band_is_explicit_and_creates_nothing() -> None:
    payload = copy.deepcopy(_payload())
    payload["llm"].update(
        {"validation_status": "REJECTED", "reason": "SCHEMA_INVALID: subject is required"}
    )

    text = _render(payload)

    assert "LLM rejected — SCHEMA_INVALID: subject is required" in text
    assert "No narrative, opportunity, or order was created from this output." in text
    assert "LLM ›" not in text
    # A rejected band contributes no LLM block, so the provenance header and
    # footer must agree.
    assert "Provenance: CALC (metrics) · LLM (narrative)" not in text
    assert "Provenance: CALC (metrics)\n" in text
    assert "CALC blocks: 6 · LLM blocks: 0" in text


def test_not_configured_llm_band_is_explicit_and_requires_no_credential() -> None:
    payload = copy.deepcopy(_payload())
    payload["llm"] = {
        "configured": False,
        "reason": "LLM not configured. This build produces metrics-only CALC reports.",
    }
    payload["cost_usd"] = "0"
    payload["cost_unpriced"] = True
    payload["llm_tokens"] = 0

    text = _render(payload)

    assert "LLM not configured. This build produces metrics-only CALC reports." in text
    assert "LLM not configured — LLM not configured" not in text
    assert "No LLM call, order, or credential was required or recorded." in text
    assert "Provenance: CALC (metrics) · LLM (narrative)" not in text
    assert "Cost: 0 USD (unpriced) · tokens 0" in text


def test_escape_markdown_text_handles_html_markdown_and_none() -> None:
    assert escape_markdown_text(None) == ""
    assert escape_markdown_text("<b>bold</b>") == "&lt;b&gt;bold&lt;/b&gt;"
    assert escape_markdown_text("a | b") == "a \\| b"
    assert escape_markdown_text("*not emphasis*") == "\\*not emphasis\\*"
    assert escape_markdown_text("line\r\nbreak") == "line break"
    assert escape_markdown_text("line\rbreak") == "line break"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a\\b", "a\\\\b"),
        ("`x`", "\\`x\\`"),
        ("[x]", "\\[x\\]"),
        ("{x}", "\\{x\\}"),
        ("# heading", "\\# heading"),
        ("~x~", "\\~x\\~"),
        ("a_b", "a_b"),
        ("_x_", "\\_x\\_"),
    ],
)
def test_escape_markdown_text_covers_each_mapped_character(value: str, expected: str) -> None:
    assert escape_markdown_text(value) == expected


def test_export_exit_code_maps_api_validation_to_usage() -> None:
    assert export_exit_code("VALIDATION_FAILED") == 2
    assert export_exit_code("NOT_FOUND") == 1
    assert export_exit_code("DAEMON_NOT_RUNNING") == 1


def test_write_export_reports_storage_unavailable_and_removes_the_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "report.md"

    def raise_oserror(_file_descriptor: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(report.os, "fsync", raise_oserror)

    with pytest.raises(typer.Exit) as excinfo:
        report._write_export(path, b"body", {})

    assert excinfo.value.exit_code == 1
    assert not path.exists()
    captured = capsys.readouterr()
    assert "STORAGE_UNAVAILABLE" in captured.err
    assert "REQ-" in captured.err


def test_write_export_refuses_a_file_created_after_the_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "report.md"
    path.write_bytes(b"original")
    # Simulate the TOCTOU window: the pre-check sees no file, but the exclusive
    # create still fails because the file exists.
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    monkeypatch.setattr(Path, "exists", lambda self: False)

    with pytest.raises(typer.Exit) as excinfo:
        report._write_export(path, b"new", {})

    assert excinfo.value.exit_code == 2
    assert path.read_bytes() == b"original"
    captured = capsys.readouterr()
    assert "VALIDATION_FAILED" in captured.err
