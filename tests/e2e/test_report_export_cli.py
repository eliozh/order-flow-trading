"""E2E: ``oft report export`` against a real loopback daemon.

The daemon runs offline against the committed archive fixtures and the
deterministic ``stub`` provider. Markdown/JSON exports are validated as
documents, ``--stdout`` carries only the export, and ``--output`` never
overwrites or follows a traversal path.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from typer.testing import CliRunner

from oft.api.app import create_app
from oft.bootstrap import build_container
from oft.cli.api_client import ApiClient
from oft.cli.main import app as cli_app
from oft.config import Settings
from oft.ports.store import ReportRecord

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
CONTRACTS = ROOT / "docs" / "contracts"
START = date(2026, 10, 1)
END = date(2026, 10, 7)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _live_settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=_free_port(),
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _seed_archive(settings: Settings) -> None:
    container = build_container(settings, initialize=True)
    assert (
        container.backfill.run(
            symbol="BTCUSDT", data_type="aggTrades", start=START, end=END
        ).run.status
        == "COMPLETE"
    )


def _start_daemon(settings: Settings) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host="127.0.0.1",
            port=settings.port,
            log_level="warning",
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("daemon thread exited before startup")
        if time.monotonic() > deadline:
            raise RuntimeError("daemon did not start within 10s")
        time.sleep(0.02)
    return server, thread


def _stop_daemon(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive(), "daemon thread did not stop after join"


def _export_cli_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", str(settings.port))
    monkeypatch.setenv("OFT_DATA", str(settings.data_dir))
    monkeypatch.setenv("OFT_ARCHIVE_FIXTURE_DIR", str(ARCHIVE))
    monkeypatch.setenv("OFT_LLM_PROVIDER", "stub")
    monkeypatch.setenv("OFT_LLM_FIXTURES_DIR", str(FIXTURES))
    monkeypatch.delenv("OFT_API_TOKEN", raising=False)


@pytest.fixture()
def live_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    settings = _live_settings(tmp_path)
    _seed_archive(settings)
    server, thread = _start_daemon(settings)
    _export_cli_env(monkeypatch, settings)
    try:
        yield settings
    finally:
        _stop_daemon(server, thread)


def _registry() -> Registry:
    resources = []
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        resources.append(
            (document["$id"], Resource.from_contents(document, default_specification=DRAFT202012))
        )
    return Registry().with_resources(resources)


def _validate_envelope(envelope: dict[str, Any]) -> None:
    document = json.loads((CONTRACTS / "api-envelope.schema.json").read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(document, registry=_registry()).iter_errors(envelope),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def _new_report(settings: Settings) -> str:
    client = ApiClient.from_settings(settings)
    scan = client.create_scan(
        {
            "symbol": "BTCUSDT",
            "period": "4h",
            "source": "backfill",
            "metrics_only": False,
            "provider": "stub",
        },
        idempotency_key=f"report-export-{uuid.uuid4().hex[:8]}",
    )
    return str(scan["data"]["report_id"])


def _save_hostile_report(settings: Settings) -> str:
    """Store a report whose payload carries secrets and a raw prompt body."""
    container = build_container(settings, initialize=True)
    # ``reports.scan_id`` has a foreign key, so anchor the hostile row to a real
    # scan created through the daemon.
    seeded_id = _new_report(settings)
    seeded = ApiClient.from_settings(settings).get_report(seeded_id)
    report_id = "RPT-HOSTILE-E2E"
    container.store.save_report(
        ReportRecord(
            report_id=report_id,
            scan_id=str(seeded["data"]["scan_id"]),
            symbol="BTCUSDT",
            timeframe="4h",
            generated_at="2026-10-07T12:00:00.000Z",
            metrics_manifest_json="[]",
            llm_decision_id="DEC-HOSTILE-E2E",
            narrative_ref="inline:DEC-HOSTILE-E2E",
            data_quality="PARTIAL",
            snapshot_json=json.dumps(
                {
                    "symbol": "BTCUSDT",
                    "window": {
                        "start": "2026-10-01T00:00:00.000Z",
                        "end": "2026-10-08T00:00:00.000Z",
                    },
                    "data_quality": "PARTIAL",
                    "quality_reasons": [],
                }
            ),
            metrics_json=json.dumps(
                {
                    "data_quality": "PARTIAL",
                    "quality_reasons": [],
                    "cvd": {"points": [], "summary": {}},
                    "volume_profile": {},
                    "footprint": {},
                    "open_interest": {"points": []},
                    "funding": {"history": []},
                    "liquidations": {"count": 0, "coverage": "NONE"},
                    "gaps": [],
                }
            ),
            llm_json=json.dumps(
                {
                    "configured": True,
                    "provider": "stub",
                    "model": "fixture",
                    "prompt_id": "analysis",
                    "prompt_version": 9,
                    "validation_status": "VALID",
                    "narrative": "<script>alert('x')</script><img src=x onerror=alert(1)>",
                    "opportunity_ids": ["OPP-HOSTILE"],
                    "api_key": "sk-super-secret",
                    "authorization": "Bearer super-secret",
                    "prompt": "RAW-PROMPT-BODY-DO-NOT-EXPORT",
                    "usage": {"total_tokens": 42},
                    "cost": {"amount_usd": "0.001", "unpriced": False},
                }
            ),
        )
    )
    return report_id


def _export(runner: CliRunner, *args: str) -> Any:
    return runner.invoke(cli_app, ["report", "export", *args])


def test_md_stdout_writes_only_the_export_document(live_daemon: Settings) -> None:
    runner = CliRunner()
    report_id = _new_report(live_daemon)

    result = _export(runner, report_id, "--format", "md", "--stdout")

    assert result.exit_code == 0, result.output
    stdout = result.stdout
    assert stdout.startswith(f"# REPORT {report_id}")
    assert stdout.endswith("\n")
    assert not stdout.endswith("\n\n")
    assert "## CALC metrics" in stdout
    assert "LLM ›" in stdout
    assert "Wrote " not in stdout
    assert result.stderr == ""


def test_json_stdout_is_the_shared_envelope(live_daemon: Settings) -> None:
    runner = CliRunner()
    report_id = _new_report(live_daemon)

    result = _export(runner, report_id, "--format", "json", "--stdout")

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    _validate_envelope(document)
    assert document["ok"] is True
    assert document["meta"]["provenance"] == "CALC"
    assert document["data"]["report_id"] == report_id
    assert result.stderr == ""


def test_output_writes_exact_bytes_names_the_path_and_leaves_no_temp(
    live_daemon: Settings, tmp_path: Path
) -> None:
    runner = CliRunner()
    report_id = _new_report(live_daemon)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    path = output_dir / "report.md"
    expected, _headers = ApiClient.from_settings(live_daemon).export_report(report_id, format="md")

    result = _export(runner, report_id, "--format", "md", "--output", str(path))

    assert result.exit_code == 0, result.output
    assert path.read_bytes() == expected
    assert str(path) in result.stdout
    assert f"{len(expected)} bytes" in result.stdout
    assert "CALC blocks:" in result.stdout
    assert "LLM blocks:" in result.stdout
    assert list(output_dir.glob("*.tmp")) == []
    assert list(output_dir.glob(".*")) == []


def test_overwrite_is_refused_and_the_existing_file_is_untouched(
    live_daemon: Settings, tmp_path: Path
) -> None:
    runner = CliRunner()
    report_id = _new_report(live_daemon)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    path = output_dir / "existing.md"
    path.write_bytes(b"sentinel")

    result = _export(runner, report_id, "--format", "md", "--output", str(path))

    assert result.exit_code == 2
    assert path.read_bytes() == b"sentinel"
    assert "VALIDATION_FAILED" in result.stderr
    assert "REQ-" in result.stderr
    assert str(path) in result.stderr
    assert list(output_dir.iterdir()) == [path]


def test_traversal_is_refused_and_writes_nothing_outside(
    live_daemon: Settings, tmp_path: Path
) -> None:
    runner = CliRunner()
    report_id = _new_report(live_daemon)
    nested = tmp_path / "nested"
    nested.mkdir()
    traversal = nested / ".." / "escape.md"

    result = _export(runner, report_id, "--format", "md", "--output", str(traversal))

    assert result.exit_code == 2
    assert "VALIDATION_FAILED" in result.stderr
    assert "REQ-" in result.stderr
    assert not (tmp_path / "escape.md").exists()
    assert list(nested.iterdir()) == []


def test_symlink_output_is_refused_and_the_target_is_untouched(
    live_daemon: Settings, tmp_path: Path
) -> None:
    runner = CliRunner()
    report_id = _new_report(live_daemon)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    target = output_dir / "target.md"
    target.write_bytes(b"sentinel")
    link = output_dir / "link.md"
    link.symlink_to(target)

    result = _export(runner, report_id, "--format", "md", "--output", str(link))

    assert result.exit_code == 2
    assert link.is_symlink()
    assert target.read_bytes() == b"sentinel"
    assert "VALIDATION_FAILED" in result.stderr
    assert "REQ-" in result.stderr


def test_a_missing_parent_directory_reports_storage_unavailable(
    live_daemon: Settings, tmp_path: Path
) -> None:
    runner = CliRunner()
    report_id = _new_report(live_daemon)
    path = tmp_path / "missing-parent" / "report.md"

    result = _export(runner, report_id, "--format", "md", "--output", str(path))

    assert result.exit_code == 1
    assert not path.exists()
    assert not path.parent.exists()
    assert "STORAGE_UNAVAILABLE" in result.stderr
    assert "REQ-" in result.stderr


def test_malformed_report_id_exits_two_with_validation_failed(live_daemon: Settings) -> None:
    runner = CliRunner()

    # A space is invalid under the route's report-id pattern; the API answers
    # 422 and the CLI maps that API-originated validation failure to exit 2.
    result = _export(runner, "RPT BAD", "--format", "md", "--stdout")

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "VALIDATION_FAILED" in result.stderr


def test_default_flags_and_format_normalization(live_daemon: Settings) -> None:
    runner = CliRunner()
    report_id = _new_report(live_daemon)

    default = _export(runner, report_id)
    assert default.exit_code == 0, default.output
    assert default.stdout.startswith("# REPORT")
    assert default.stderr == ""

    normalized = _export(runner, report_id, "--format", " JSON ")
    assert normalized.exit_code == 0, normalized.output
    document = json.loads(normalized.stdout)
    _validate_envelope(document)
    assert document["data"]["report_id"] == report_id
    assert normalized.stderr == ""


def test_a_hostile_stored_report_is_redacted_in_both_cli_formats(
    live_daemon: Settings,
) -> None:
    runner = CliRunner()
    report_id = _save_hostile_report(live_daemon)

    markdown = _export(runner, report_id, "--format", "md", "--stdout")
    assert markdown.exit_code == 0, markdown.output
    text = markdown.stdout
    assert "&lt;script" in text
    assert "&lt;img" in text
    assert "<script" not in text
    assert "<img" not in text
    assert "sk-super-secret" not in text
    assert "Bearer super-secret" not in text
    assert "RAW-PROMPT-BODY-DO-NOT-EXPORT" not in text
    assert "api_key" not in text.lower()
    assert "authorization" not in text.lower()
    assert "Prompt: analysis v9" in text
    assert "OPP-HOSTILE" in text

    exported_json = _export(runner, report_id, "--format", "json", "--stdout")
    assert exported_json.exit_code == 0, exported_json.output
    body = json.loads(exported_json.stdout)
    serialized = json.dumps(body)
    assert "sk-super-secret" not in serialized
    assert "Bearer super-secret" not in serialized
    assert "RAW-PROMPT-BODY-DO-NOT-EXPORT" not in serialized
    assert "api_key" not in serialized
    assert "authorization" not in serialized
    assert body["data"]["llm"]["prompt_id"] == "analysis"
    assert body["data"]["llm"]["prompt_version"] == 9
    assert body["data"]["llm"]["usage"]["total_tokens"] == 42


def test_missing_report_exits_one(live_daemon: Settings) -> None:
    runner = CliRunner()

    result = _export(runner, "RPT-MISSING", "--format", "md", "--stdout")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "does not exist" in result.stderr


def test_invalid_format_exits_two_with_validation_failed(live_daemon: Settings) -> None:
    runner = CliRunner()
    report_id = _new_report(live_daemon)

    result = _export(runner, report_id, "--format", "pdf", "--stdout")

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "VALIDATION_FAILED" in result.stderr


def test_stdout_and_output_together_are_refused(live_daemon: Settings, tmp_path: Path) -> None:
    runner = CliRunner()
    report_id = _new_report(live_daemon)
    path = tmp_path / "both.md"

    result = _export(runner, report_id, "--format", "md", "--output", str(path), "--stdout")

    assert result.exit_code == 2
    assert "VALIDATION_FAILED" in result.stderr
    assert not path.exists()


def test_no_secret_api_key_or_raw_prompt_reaches_either_stdout_format(
    live_daemon: Settings,
) -> None:
    runner = CliRunner()
    report_id = _new_report(live_daemon)

    for fmt in ("md", "json"):
        result = _export(runner, report_id, "--format", fmt, "--stdout")
        assert result.exit_code == 0, result.output
        body = result.stdout
        for forbidden in ("api_key", "authorization", "sk-", "raw-prompt"):
            assert forbidden not in body.lower(), (fmt, forbidden)
        # Raw prompt bodies are never exported; prompt id/version metadata may be.
        assert '"system_prompt"' not in body
        assert '"user_prompt"' not in body
