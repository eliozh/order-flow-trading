"""Integration: ``GET /api/v1/reports/{id}/export`` over a real scan."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.application.report_export import provenance_counts
from oft.bootstrap import build_container
from oft.config import Settings
from oft.ports.store import ReportRecord

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
PAYLOAD = {
    "symbol": "BTCUSDT",
    "period": "4h",
    "source": "backfill",
    "metrics_only": False,
    "provider": "stub",
}


@dataclass
class _Harness:
    settings: Settings
    app: Any
    client: TestClient
    auth: dict[str, str]


def _harness(tmp_path: Path) -> _Harness:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )
    container = build_container(settings, initialize=True)
    result = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert result.run.status == "COMPLETE"
    app = create_app(settings)
    return _Harness(
        settings=settings,
        app=app,
        client=TestClient(app),
        auth={"Authorization": f"Bearer {app.state.api_token}"},
    )


def _create_report(harness: _Harness) -> str:
    response = harness.client.post(
        "/api/v1/scans",
        json=PAYLOAD,
        headers={**harness.auth, "Idempotency-Key": "report-export-key"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["report_id"])


def _export(harness: _Harness, report_id: str, fmt: str = "md") -> Any:
    return harness.client.get(
        f"/api/v1/reports/{report_id}/export", params={"format": fmt}, headers=harness.auth
    )


def test_markdown_export_is_an_attachment_with_calc_llm_and_provenance(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _create_report(harness)
        report = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth).json()
        response = _export(harness, report_id, "md")
    finally:
        harness.client.close()

    assert response.status_code == 200, response.text
    content_type = response.headers["content-type"]
    assert content_type.startswith("text/markdown")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f"report-{report_id}.md" in disposition

    detail = report["data"]
    counts = provenance_counts(detail)
    assert response.headers["x-oft-calc-blocks"] == str(counts["calc"])
    assert response.headers["x-oft-llm-blocks"] == str(counts["llm"])
    assert counts["llm"] == 1

    text = response.text
    assert text.startswith(f"# REPORT {report_id} · BTCUSDT · 4h")
    assert "## CALC metrics" in text
    assert "LLM ›" in text
    assert "Prompt: analysis v" in text
    assert "Provider: stub/fixture" in text
    assert f"Cost: {detail['cost_usd']} USD (" in text
    assert f"tokens {detail['llm_tokens']}" in text
    llm = detail["llm"]
    assert llm["narrative"] in text
    for opportunity_id in llm["opportunity_ids"]:
        assert opportunity_id in text
    # The metrics stamp is the per-CALC-row fallback; the snapshot stamp is a
    # separate row in the stamps table.
    metrics_quality = detail["metrics"]["data_quality"]
    snapshot_quality = detail["snapshot"]["data_quality"]
    assert f"| metrics | {metrics_quality} |" in text
    assert f"| snapshot | {snapshot_quality} |" in text
    cvd_line = next(line for line in text.splitlines() if line.startswith("| CVD (window) |"))
    assert cvd_line.endswith(f"| {metrics_quality} |")
    assert "<script" not in text.lower()
    assert "&lt;script" not in text
    assert "api_key" not in text.lower()
    assert "authorization" not in text.lower()
    assert "system_prompt" not in text
    assert "RAW-PROMPT" not in text


def test_json_export_is_the_shared_envelope_as_an_attachment(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _create_report(harness)
        detail_envelope = harness.client.get(
            f"/api/v1/reports/{report_id}", headers=harness.auth
        ).json()
        detail = detail_envelope["data"]
        response = _export(harness, report_id, "json")
    finally:
        harness.client.close()

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f"report-{report_id}.json" in disposition

    body = json.loads(response.text)
    counts = provenance_counts(body["data"])
    assert response.headers["x-oft-calc-blocks"] == str(counts["calc"])
    assert response.headers["x-oft-llm-blocks"] == str(counts["llm"])
    assert counts["llm"] == 1
    assert body["ok"] is True
    # The stub LLM produced a narrative, so both views report mixed provenance.
    assert body["meta"]["provenance"] == "MIXED"
    assert detail_envelope["meta"]["provenance"] == "MIXED"
    data = body["data"]
    assert data["report_id"] == report_id
    assert data["snapshot"]["symbol"] == "BTCUSDT"
    assert data["metrics"]["cvd"]["points"]
    assert data["llm"]["configured"] is True
    assert data["llm"]["validation_status"] in {"VALID", "REPAIRED"}
    assert data["llm"]["prompt_version"] >= 1
    assert data["llm"]["opportunity_ids"]
    # The exported data-quality stamps equal the frozen report's stamps.
    assert data["metrics"]["data_quality"] == detail["metrics"]["data_quality"]
    assert data["snapshot"]["data_quality"] == detail["snapshot"]["data_quality"]
    assert "<script" not in json.dumps(body).lower()
    assert "api_key" not in json.dumps(body).lower()
    assert "authorization" not in json.dumps(body).lower()


def test_missing_report_is_not_found(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        response = _export(harness, "RPT-MISSING", "md")
    finally:
        harness.client.close()

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_invalid_format_is_validation_failed(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _create_report(harness)
        response = harness.client.get(
            f"/api/v1/reports/{report_id}/export",
            params={"format": "pdf"},
            headers=harness.auth,
        )
    finally:
        harness.client.close()

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["data"] is None


def test_export_defaults_to_markdown_when_format_is_omitted(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _create_report(harness)
        response = harness.client.get(f"/api/v1/reports/{report_id}/export", headers=harness.auth)
    finally:
        harness.client.close()

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text.startswith(f"# REPORT {report_id}")


def test_a_malformed_report_id_is_validation_failed(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        response = harness.client.get("/api/v1/reports/bad%20id/export", headers=harness.auth)
    finally:
        harness.client.close()

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["data"] is None


def test_a_colon_report_id_is_sanitized_in_the_attachment_filename(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        created_id = _create_report(harness)
        created = harness.client.get(f"/api/v1/reports/{created_id}", headers=harness.auth).json()[
            "data"
        ]
        harness.app.state.container.store.save_report(
            ReportRecord(
                report_id="RPT:COLON:1",
                scan_id=str(created["scan_id"]),
                symbol="BTCUSDT",
                timeframe="4h",
                generated_at="2026-10-07T12:00:00.000Z",
                metrics_manifest_json="[]",
                llm_decision_id=None,
                narrative_ref=None,
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
                metrics_json=json.dumps({"data_quality": "PARTIAL", "quality_reasons": []}),
            )
        )
        response = _export(harness, "RPT:COLON:1", "md")
    finally:
        harness.client.close()

    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    assert 'filename="report-RPT_COLON_1.md"' in disposition
    assert ":" not in disposition


def test_a_hostile_stored_report_is_redacted_and_escaped_in_both_formats(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    try:
        created_id = _create_report(harness)
        created = harness.client.get(f"/api/v1/reports/{created_id}", headers=harness.auth).json()[
            "data"
        ]
        record = ReportRecord(
            report_id="RPT-HOSTILE-1",
            scan_id=str(created["scan_id"]),
            symbol="BTCUSDT",
            timeframe="4h",
            generated_at="2026-10-07T12:00:00.000Z",
            metrics_manifest_json="[]",
            llm_decision_id="DEC-HOSTILE-1",
            narrative_ref="inline:DEC-HOSTILE-1",
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
                    "reason": "the price table /var/lib/oft/prices.json is not valid",
                    "api_key": "sk-super-secret",
                    "authorization": "Bearer super-secret",
                    "prompt": "RAW-PROMPT-BODY-DO-NOT-EXPORT",
                    "usage": {"total_tokens": 42},
                    "cost": {"amount_usd": "0.001", "unpriced": False},
                }
            ),
        )
        harness.app.state.container.store.save_report(record)

        markdown = _export(harness, "RPT-HOSTILE-1", "md")
        exported_json = _export(harness, "RPT-HOSTILE-1", "json")
    finally:
        harness.client.close()

    assert markdown.status_code == 200, markdown.text
    text = markdown.text
    assert "&lt;script" in text
    assert "&lt;img" in text
    assert "<script" not in text
    assert "<img" not in text
    assert "sk-super-secret" not in text
    assert "Bearer super-secret" not in text
    assert "RAW-PROMPT-BODY-DO-NOT-EXPORT" not in text
    assert "Prompt: analysis v9" in text
    assert "Opportunities: OPP-HOSTILE" in text

    assert exported_json.status_code == 200, exported_json.text
    body = json.loads(exported_json.text)
    serialized = json.dumps(body)
    assert "sk-super-secret" not in serialized
    assert "Bearer super-secret" not in serialized
    assert "RAW-PROMPT-BODY-DO-NOT-EXPORT" not in serialized
    assert "api_key" not in serialized
    assert "authorization" not in serialized
    assert body["data"]["llm"]["prompt_id"] == "analysis"
    assert body["data"]["llm"]["prompt_version"] == 9
    assert body["data"]["llm"]["usage"]["total_tokens"] == 42
    # Redaction must not over-redact: the hostile text survives as inert JSON
    # data and the opportunity id is preserved.
    assert (
        body["data"]["llm"]["narrative"]
        == "<script>alert('x')</script><img src=x onerror=alert(1)>"
    )
    assert body["data"]["llm"]["opportunity_ids"] == ["OPP-HOSTILE"]
    # Daemon-absolute paths in the reason are reduced to the basename.
    assert "/var/lib/oft" not in exported_json.text
    assert body["data"]["llm"]["reason"] == "the price table prices.json is not valid"
    assert body["meta"]["data_quality"] == "PARTIAL"
