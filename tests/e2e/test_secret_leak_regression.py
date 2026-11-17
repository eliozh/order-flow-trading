"""Security regression: a stored provider key never reaches an observable surface.

The key is written through the real encrypted-file fallback (keyring backend
forced to ``fail`` plus a per-test ``OFT_MASTER_KEY``), then every surface the
milestone audits is checked for the key, a raw-prompt sentinel, and provider
header spellings: the settings API, the stored report detail, onboarding status,
both report-export formats, and the daemon log.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from oft.api.auth import read_api_token
from oft.application.secrets_service import SecretsService
from oft.bootstrap import build_container
from oft.cli.api_client import ApiClient
from oft.cli.main import app
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.store import ReportRecord

ROOT = Path(__file__).resolve().parents[2]
MARKET_FIXTURE = ROOT / "tests" / "fixtures" / "market" / "btcusdt_agg-trades_sample.csv"

SENTINEL = "sk-live-SENTINEL-DO-NOT-LEAK-9f3a"
RAW_PROMPT_SENTINEL = "RAW-PROMPT-SENTINEL-DO-NOT-LEAK-2b7c"
MASK = "****9f3a"
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def encrypted_file_fallback(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the real encrypted-file store and restore the keyring afterwards.

    A process that already imported ``keyring`` may have cached a secure
    backend, so the documented ``PYTHON_KEYRING_BACKEND`` selection is applied
    to both the environment and the in-process backend.
    """
    import keyring
    from keyring.backends.fail import Keyring as FailKeyring

    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("OFT_MASTER_KEY", "per-test-master-key-0123456789abcdef")
    previous = keyring.get_keyring()
    keyring.set_keyring(FailKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)


_CREDENTIAL_SPELLINGS = ("x-api-key", "authorization", "api_key")


def _assert_no_sentinel_material(text: str) -> None:
    """Assert absence without echoing the secret value or the surface body.

    Inline ``assert value not in text`` makes pytest's assertion rewriting print
    both operands on failure. ``pytest.fail`` avoids that, and deleting the
    audited surface keeps pytest's traceback from echoing its body through the
    frame's locals.
    """
    lowered = text.lower()
    leaked_value = SENTINEL in text or SENTINEL[:12] in text or RAW_PROMPT_SENTINEL in text
    leaked_header = any(spelling in lowered for spelling in _CREDENTIAL_SPELLINGS)
    del text, lowered
    if leaked_value:
        pytest.fail("a provider key or raw prompt reached an observable surface")
    if leaked_header:
        pytest.fail("a credential field name reached an observable surface")


def _api_get(settings: Settings, path: str) -> httpx.Response:
    token = read_api_token(settings)
    assert token
    with httpx.Client(
        base_url=f"http://127.0.0.1:{settings.port}", trust_env=False, timeout=5.0
    ) as client:
        return client.get(path, headers={"Authorization": f"Bearer {token}"})


def _seed_hostile_report(settings: Settings) -> str:
    """Create a real report row whose LLM block carries credential material."""
    client = ApiClient.from_settings(settings)
    scan = client.create_scan(
        {
            "fixture_path": str(MARKET_FIXTURE),
            "symbol": "BTCUSDT",
            "period": "4h",
            "metrics_only": True,
        },
        idempotency_key="secret-leak-report",
    )
    scan_id = str(scan["data"]["scan_id"])
    report_id = "RPT-SECRET-LEAK"

    hostile = {
        "configured": True,
        "provider": "openai",
        "model": "fixture",
        "prompt_id": "analysis",
        "prompt_version": 9,
        "validation_status": "VALID",
        "narrative": "A harmless stored narrative.",
        "opportunity_ids": [],
        "api_key": SENTINEL,
        "authorization": f"Bearer {SENTINEL}",
        "x-api-key": SENTINEL,
        "prompt": RAW_PROMPT_SENTINEL,
        "system_prompt": RAW_PROMPT_SENTINEL,
        "user_prompt": RAW_PROMPT_SENTINEL,
        "usage": {"total_tokens": 42},
        "cost": {"amount_usd": "0.001", "unpriced": False},
    }
    container = build_container(settings, initialize=True)
    container.store.save_report(
        ReportRecord(
            report_id=report_id,
            scan_id=scan_id,
            symbol="BTCUSDT",
            timeframe="4h",
            generated_at="2026-10-07T12:00:00.000Z",
            metrics_manifest_json="[]",
            llm_decision_id="DEC-SECRET-LEAK",
            narrative_ref="inline:DEC-SECRET-LEAK",
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
            llm_json=json.dumps(hostile),
        )
    )
    return report_id


def _export(report_id: str, export_format: str) -> Any:
    return CliRunner().invoke(
        app, ["report", "export", report_id, "--format", export_format, "--stdout"]
    )


def test_a_stored_key_never_reaches_api_onboarding_export_or_logs(
    live_daemon: Settings, encrypted_file_fallback: None
) -> None:
    # Arrange: the fixture forces the encrypted-file fallback; store the
    # sentinel through that real backend.
    container = build_container(live_daemon, initialize=True)
    status = SecretsService(
        store=container.store, settings=live_daemon, clock=FixedClock(NOW)
    ).set_key("openai", SENTINEL, actor="test")
    assert status.key_source == "encrypted_file"
    assert status.masked == MASK
    report_id = _seed_hostile_report(live_daemon)

    # Act: read every audited surface.
    llm_response = _api_get(live_daemon, "/api/v1/settings/llm")
    onboarding_response = _api_get(live_daemon, "/api/v1/settings/onboarding")
    detail_response = _api_get(live_daemon, f"/api/v1/reports/{report_id}")
    markdown = _export(report_id, "md")
    exported_json = _export(report_id, "json")
    log_path = live_daemon.data_dir / "logs" / "daemon.log"
    log_text = log_path.read_text(encoding="utf-8")

    # Assert: the settings contract keeps the fixed mask and no value.
    if llm_response.status_code != 200:
        pytest.fail("the settings LLM surface did not return 200")
    _assert_no_sentinel_material(llm_response.text)
    llm_body = llm_response.json()
    openai_row = next(row for row in llm_body["data"]["providers"] if row["provider"] == "openai")
    assert openai_row["configured"] is True
    assert openai_row["masked"] == MASK
    assert openai_row["key_source"] == "encrypted_file"

    # Assert: the report detail redacts stored credential/prompt keys while
    # keeping the non-secret LLM evidence.
    if detail_response.status_code != 200:
        pytest.fail("the report detail surface did not return 200")
    _assert_no_sentinel_material(detail_response.text)
    detail = detail_response.json()["data"]
    assert detail["llm"]["usage"]["total_tokens"] == 42
    assert detail["llm"]["prompt_id"] == "analysis"
    assert detail["llm"]["narrative"] == "A harmless stored narrative."

    # Assert: onboarding is secret-free and its payload was actually read.
    if onboarding_response.status_code != 200:
        pytest.fail("the onboarding surface did not return 200")
    _assert_no_sentinel_material(onboarding_response.text)
    onboarding = onboarding_response.json()["data"]
    assert onboarding["environment"] == "paper"
    assert onboarding["resumed_step"] >= 1
    assert onboarding["steps"]

    # Assert: both export formats drop the key, raw prompts, and header names,
    # while keeping the non-secret usage metadata.
    if markdown.exit_code != 0:
        pytest.fail("the markdown export failed")
    _assert_no_sentinel_material(markdown.stdout)
    if exported_json.exit_code != 0:
        pytest.fail("the JSON export failed")
    _assert_no_sentinel_material(exported_json.stdout)
    exported = json.loads(exported_json.stdout)
    assert exported["data"]["llm"]["usage"]["total_tokens"] == 42
    assert exported["data"]["llm"]["prompt_id"] == "analysis"

    # Assert: the daemon's configured log captured the key lifecycle but no
    # material. Naming the event proves the log was not merely empty or stale.
    assert log_path.is_file()
    assert "secret_stored" in log_text
    _assert_no_sentinel_material(log_text)
    # The raw key file is ciphertext; a plaintext sentinel never reaches disk.
    if SENTINEL.encode() in (live_daemon.data_dir / "secrets.enc").read_bytes():
        pytest.fail("the provider key reached the encrypted secret file in plaintext")
    # The SQLite secret metadata carries only non-secret provenance.
    meta = container.store.get_secret_meta("openai", "api_key")
    assert meta is not None
    if SENTINEL in json.dumps(meta.__dict__):
        pytest.fail("the provider key reached the secret metadata row")
