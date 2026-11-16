"""Pure renderer tests for the report export (slice 42).

The renderer is deterministic and allowlisted: only the known report sections
reach the Markdown, every dynamic value is escaped, and the caller
(``redact_report_payload`` on the export path) removes secrets/raw prompts
before rendering.
"""

from __future__ import annotations

import copy
import errno
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

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
from oft.cli.main import app as cli_app


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


@pytest.mark.parametrize("status", ["VALID", "REPAIRED"])
def test_a_valid_or_repaired_llm_band_counts_one_block_in_text_and_json(status: str) -> None:
    # VALID and REPAIRED are both LLM bands. A regression that only accepts
    # ``{"VALID"}`` would silently strip REPAIRED provenance from both formats.
    payload = copy.deepcopy(_payload())
    payload["llm"]["validation_status"] = status
    envelope = success_envelope(
        payload,
        request_id="REQ-TEST",
        generated_at="2026-10-07T12:00:00.000Z",
        provenance="MIXED",
        data_quality="PARTIAL",
    )

    text = _render(payload)
    document = json.loads(render_report_json(envelope))

    assert "LLM › Price is rotating inside the range." in text
    assert "LLM blocks: 1" in text
    assert provenance_counts(payload)["llm"] == 1
    assert document["data"]["llm"]["narrative"] == "Price is rotating inside the range."
    assert provenance_counts(document["data"])["llm"] == 1


def test_measured_open_interest_and_funding_render_their_measured_values() -> None:
    payload = copy.deepcopy(_payload())
    payload["metrics"]["open_interest"] = {
        "points": [{"ts": "2026-10-07T08:00:00.000Z", "value": "10"}],
        "delta_over_window": "42.5",
        "sampling": "REGULAR",
    }
    payload["metrics"]["funding"] = {
        "history": [{"ts": "2026-10-07T00:00:00.000Z", "rate": "0.0001"}],
        "current_rate": "0.00012",
    }

    text = _render(payload)

    assert "| Open interest Δ | 42.5 |" in text
    assert "| Funding | 0.00012 |" in text
    assert "N/A — open interest not measured" not in text
    assert "N/A — funding not measured" not in text
    # The base payload's 6 measured CALC blocks plus OI and funding.
    assert "CALC blocks: 8 · LLM blocks: 1" in text
    assert provenance_counts(payload) == {"calc": 8, "llm": 1}


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


def test_redaction_removes_the_named_secret_key_variants() -> None:
    payload: dict[str, Any] = {
        "apiKey": "secret-1",
        "apikey": "secret-2",
        "openai_api_key": "secret-3",
        "x-api-key": "secret-4",
        "x_api_key": "secret-5",
        "xapikey": "secret-6",
        "client_secret": "secret-7",
        "secret_value": "secret-8",
        "nested": {"apiKey": "secret-9", "keep": 1},
    }

    redacted = redact_report_payload(payload)

    assert redacted == {"nested": {"keep": 1}}
    assert "secret-" not in json.dumps(redacted)


def test_redaction_keeps_llm_usage_cost_and_prompt_metadata() -> None:
    payload: dict[str, Any] = {
        "llm_tokens": 10,
        "total_tokens": 20,
        "prompt_id": "analysis",
        "prompt_version": 3,
        "cost": {"amount_usd": "0.1"},
        "usage": {"total_tokens": 20},
    }

    assert redact_report_payload(payload) == payload


def test_redaction_basenames_absolute_paths_in_the_llm_reason_only() -> None:
    payload: dict[str, Any] = {
        "llm": {
            "configured": True,
            "validation_status": "REJECTED",
            "reason": "the price table /var/lib/oft/prices.json is not valid",
        },
        "other": "keep /var/lib/oft/prices.json intact",
    }

    redacted = redact_report_payload(payload)

    reason = redacted["llm"]["reason"]
    assert "/var/lib/oft" not in reason
    assert "prices.json" in reason
    assert reason == "the price table prices.json is not valid"
    # Only ``llm.reason`` is touched; unrelated payload strings survive.
    assert redacted["other"] == "keep /var/lib/oft/prices.json intact"


def test_redaction_basenames_windows_drive_paths_in_the_llm_reason() -> None:
    payload: dict[str, Any] = {"llm": {"reason": r"cannot read C:\oft\data\prices.json now"}}

    reason = redact_report_payload(payload)["llm"]["reason"]

    assert "C:\\oft" not in reason
    assert reason == "cannot read prices.json now"


def test_redaction_leaves_a_non_string_llm_reason_untouched() -> None:
    payload: dict[str, Any] = {"llm": {"configured": False, "reason": ["LIVE_DATA_STALE"]}}

    assert redact_report_payload(payload)["llm"]["reason"] == ["LIVE_DATA_STALE"]


def test_non_string_untrusted_values_do_not_raise_and_do_not_count() -> None:
    payload = copy.deepcopy(_payload())
    payload["metrics"]["liquidations"] = {"count": 0, "coverage": ["SINCE_DEPLOYMENT"]}
    payload["llm"]["validation_status"] = {"VALID": True}

    counts = provenance_counts(payload)
    text = _render(payload)

    assert counts["calc"] == 5
    assert counts["llm"] == 0
    assert "LLM rejected" in text


def test_a_non_configuration_skip_renders_with_a_neutral_prefix() -> None:
    payload = copy.deepcopy(_payload())
    payload["llm"] = {
        "configured": False,
        "reason": "LIVE_DATA_STALE: the live snapshot is older than the allowed window",
    }

    text = _render(payload)

    assert (
        "LLM skipped — LIVE_DATA_STALE: the live snapshot is older than the allowed window" in text
    )
    assert "LLM not configured" not in text


def test_escape_markdown_text_strips_dangerous_controls_but_keeps_tabs() -> None:
    text = escape_markdown_text("a\x1b[31mred\x1b[0m\x00\x07\x7f\x9f")

    assert "\x1b" not in text
    assert "\x00" not in text
    assert "\x07" not in text
    assert "\x7f" not in text
    assert "\x9f" not in text
    # The printable remainder stays; ``[`` is still escaped for Markdown.
    assert "a\\[31mred\\[0m" in text
    # A tab is left intact: it is inert inline and cannot start an escape.
    assert escape_markdown_text("a\tb") == "a\tb"


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
    assert list(tmp_path.glob(".*")) == []
    captured = capsys.readouterr()
    assert "STORAGE_UNAVAILABLE" in captured.err
    assert "REQ-" in captured.err
    # The bounded cause is retained instead of being discarded.
    assert "OSError" in captured.err
    assert "disk full" in captured.err


def test_write_export_never_overwrites_an_existing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "report.md"
    path.write_bytes(b"original")

    with pytest.raises(typer.Exit) as excinfo:
        report._write_export(path, b"new", {})

    assert excinfo.value.exit_code == 2
    assert path.read_bytes() == b"original"
    # The same-directory temporary file is cleaned up on the refusal path.
    assert list(tmp_path.glob(".*")) == []
    captured = capsys.readouterr()
    assert "VALIDATION_FAILED" in captured.err
    assert "REQ-" in captured.err


def test_write_export_falls_back_to_exclusive_create_without_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "report.md"

    def no_links(_source: object, _destination: object) -> None:
        raise OSError(errno.EPERM, "hard links are not supported")

    monkeypatch.setattr(report.os, "link", no_links)

    report._write_export(path, b"body", {})

    assert path.read_bytes() == b"body"
    # The staged temporary file is removed after the fallback publish.
    assert list(tmp_path.glob(".*")) == []


def test_fallback_refuses_an_existing_destination_and_leaves_it_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "report.md"
    path.write_bytes(b"original")

    def no_links(_source: object, _destination: object) -> None:
        raise OSError(errno.EPERM, "hard links are not supported")

    monkeypatch.setattr(report.os, "link", no_links)

    with pytest.raises(typer.Exit) as excinfo:
        report._write_export(path, b"new", {})

    # The fallback still uses an exclusive create: no overwrite, exit 2.
    assert excinfo.value.exit_code == 2
    assert path.read_bytes() == b"original"
    assert list(tmp_path.glob(".*")) == []
    assert "VALIDATION_FAILED" in capsys.readouterr().err


def test_fallback_write_failure_removes_the_partial_final_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "report.md"
    real_fsync = report.os.fsync
    calls = {"count": 0}

    def fail_second_fsync(file_descriptor: int) -> None:
        calls["count"] += 1
        # Call 1 fsyncs the staged temp; call 2 fsyncs the fallback's final file.
        if calls["count"] == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        real_fsync(file_descriptor)

    def no_links(_source: object, _destination: object) -> None:
        raise OSError(errno.EPERM, "hard links are not supported")

    monkeypatch.setattr(report.os, "link", no_links)
    monkeypatch.setattr(report.os, "fsync", fail_second_fsync)

    with pytest.raises(typer.Exit) as excinfo:
        report._write_export(path, b"body", {})

    assert excinfo.value.exit_code == 1
    assert not path.exists()
    assert list(tmp_path.glob(".*")) == []
    captured = capsys.readouterr()
    assert "STORAGE_UNAVAILABLE" in captured.err
    assert "ENOSPC" in captured.err


def test_staging_temp_is_created_beside_the_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "report.md"
    seen: list[Path] = []
    real_link = report.os.link

    def capture_link(source: Any, destination: Any) -> None:
        seen.append(Path(str(source)))
        real_link(source, destination)

    monkeypatch.setattr(report.os, "link", capture_link)

    report._write_export(path, b"body", {})

    assert seen and seen[0].parent == path.parent


def test_bounded_cause_strips_controls_and_is_capped() -> None:
    cause = report._bounded_cause(OSError(errno.ENOSPC, "\x1b[31m" + "x" * 400))

    assert "ENOSPC" in cause
    assert "\x1b" not in cause
    assert len(cause) <= 200


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_export_mode_is_owner_only_on_both_publish_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = tmp_path / "primary.md"
    report._write_export(primary, b"body", {})
    assert stat.S_IMODE(primary.stat().st_mode) == 0o600

    fallback = tmp_path / "fallback.md"

    def no_links(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.EPERM, "hard links are not supported")

    monkeypatch.setattr(report.os, "link", no_links)
    report._write_export(fallback, b"body", {})
    assert stat.S_IMODE(fallback.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_fallback_creates_the_file_owner_only_before_restrict_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback's create syscall itself must be 0600, not 0666-then-chmod."""
    path = tmp_path / "report.md"
    previous_umask = os.umask(0)
    try:

        def no_links(*_args: Any, **_kwargs: Any) -> None:
            raise OSError(errno.EPERM, "hard links are not supported")

        monkeypatch.setattr(report.os, "link", no_links)
        # Disable normalization so the assertion observes the create mode.
        monkeypatch.setattr(report, "_restrict_mode", lambda _file_descriptor: None)
        report._write_export(path, b"body", {})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        os.umask(previous_umask)


def test_write_export_names_a_leftover_temp_file_on_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "report.md"

    def refuse_unlink(self: Path, missing_ok: bool = False) -> None:
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(Path, "unlink", refuse_unlink)

    report._write_export(path, b"body", {})

    assert path.read_bytes() == b"body"
    captured = capsys.readouterr()
    assert "could not remove the temporary export file" in captured.err
    assert ".report.md." in captured.err
    leftovers = list(tmp_path.glob(".report.md.*.tmp"))
    assert len(leftovers) == 1
    # Restore the real ``unlink`` and remove what the simulated failure left.
    monkeypatch.undo()
    leftovers[0].unlink()


def test_write_export_warns_but_keeps_the_file_when_parent_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "report.md"

    def fail_sync(_parent: Path) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(report, "_fsync_parent", fail_sync)

    report._write_export(path, b"body", {})

    assert path.read_bytes() == b"body"
    captured = capsys.readouterr()
    assert "Wrote" in captured.out
    assert "could not be fsynced" in captured.err


def test_an_invalid_output_path_is_refused_before_the_api_client_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the API client must not be built for an invalid output path")

    monkeypatch.setattr(report.ApiClient, "from_settings", fail)
    existing = tmp_path / "existing.md"
    existing.write_bytes(b"sentinel")
    runner = CliRunner()

    result = runner.invoke(
        cli_app,
        ["report", "export", "RPT-ANY", "--output", str(existing)],
    )

    assert result.exit_code == 2
    assert "VALIDATION_FAILED" in result.stderr
    assert existing.read_bytes() == b"sentinel"
