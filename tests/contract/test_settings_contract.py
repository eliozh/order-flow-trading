"""Settings payloads validate the shared envelope and use catalogued errors."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from typer.testing import CliRunner

from oft.api.app import create_app
from oft.cli.main import app as cli_app
from oft.clock import FixedClock
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"
NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_scan_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OFT_SCAN_PERIOD", raising=False)
    monkeypatch.delenv("OFT_SCAN_JITTER_SECONDS", raising=False)


def _load(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    common = _load("common.schema.json")
    envelope = _load("api-envelope.schema.json")
    resource = Resource.from_contents(common, default_specification=DRAFT202012)
    registry = Registry().with_resource(common["$id"], resource)
    return Draft202012Validator(envelope, registry=registry)


def _validate(validator: Draft202012Validator, payload: dict[str, Any]) -> None:
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def test_every_settings_response_validates_the_envelope(
    tmp_path: Path, validator: Draft202012Validator
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        responses = [
            client.get("/api/v1/settings/scan", headers=auth),
            client.get("/api/v1/settings", headers=auth),
            client.get("/api/v1/settings/risk", headers=auth),
            client.put(
                "/api/v1/settings/scan",
                json={"period": "8h"},
                headers={**auth, "Idempotency-Key": "contract-1"},
            ),
            client.get("/api/v1/settings/scan", headers=auth),
            client.put(
                "/api/v1/settings/scan",
                json={"period": "1h"},
                headers={**auth, "Idempotency-Key": "contract-2"},
            ),
            client.put(
                "/api/v1/settings/scan",
                json={"period": "8h"},
                headers=auth,
            ),
            client.put(
                "/api/v1/settings/risk",
                json={"caps": {"per_trade_risk_pct": 0.5}},
                headers={**auth, "Idempotency-Key": "contract-risk-1"},
            ),
            client.put(
                "/api/v1/settings/risk",
                json={"caps": {"max_leverage": 6.0}},
                headers={**auth, "Idempotency-Key": "contract-risk-2"},
            ),
            client.put(
                "/api/v1/settings/risk",
                json={"caps": {"per_trade_risk_pct": -1}},
                headers={**auth, "Idempotency-Key": "contract-risk-3"},
            ),
            client.put(
                "/api/v1/settings/risk",
                json={"caps": {"per_trade_risk_pct": 3.0}, "confirm": True},
                headers={**auth, "Idempotency-Key": "contract-risk-4"},
            ),
        ]

    for response in responses:
        _validate(validator, response.json())


def test_settings_payload_uses_only_catalogued_error_codes(
    tmp_path: Path, validator: Draft202012Validator
) -> None:
    catalog_codes = {entry["code"] for entry in _load("error-catalog.json")["codes"]}
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        ok = client.get("/api/v1/settings/scan", headers=auth)
        invalid = client.put(
            "/api/v1/settings/scan",
            json={"period": "3h"},
            headers={**auth, "Idempotency-Key": "contract-3"},
        )
        missing_key = client.put(
            "/api/v1/settings/scan",
            json={"period": "4h"},
            headers=auth,
        )

    for response in (ok, invalid, missing_key):
        _validate(validator, response.json())

    for response, expected in (
        (invalid, "VALIDATION_FAILED"),
        (missing_key, "IDEMPOTENCY_KEY_REQUIRED"),
    ):
        code = response.json()["error"]["code"]
        assert code == expected
        assert code in catalog_codes


def test_scan_settings_payload_has_no_secret_or_extra_fields(
    tmp_path: Path, validator: Draft202012Validator
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings, clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.get("/api/v1/settings/scan", headers=_auth(app))

    _validate(validator, response.json())
    data = response.json()["data"]
    assert set(data) == {
        "key",
        "value",
        "source",
        "supported",
        "next_run",
        "timezone",
        "updated_at",
        "updated_by",
    }
    assert data["supported"] == ["4h", "8h"]
    assert data["next_run"].endswith("Z")
    serialized = json.dumps(data).lower()
    for marker in ("secret", "api_key", "token", "password"):
        assert marker not in serialized


def test_risk_settings_payload_has_exact_keys_and_no_secrets(
    tmp_path: Path, validator: Draft202012Validator
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        get_response = client.get("/api/v1/settings/risk", headers=auth)
        put_response = client.put(
            "/api/v1/settings/risk",
            json={"caps": {"per_trade_risk_pct": 0.5}, "reason": "contract"},
            headers={**auth, "Idempotency-Key": "contract-risk-keys"},
        )

    for response in (get_response, put_response):
        _validate(validator, response.json())
        data = response.json()["data"]
        assert set(data) == {
            "caps",
            "source",
            "defaults",
            "utilization",
            "updated_at",
            "updated_by",
        }
        assert set(data["caps"]) == {
            "per_trade_risk_pct",
            "max_concurrent_positions",
            "daily_loss_pct",
            "max_leverage",
            "max_notional_pct",
            "max_slippage_bps",
            "max_spread_bps",
            "stale_mark_seconds",
        }
        assert set(data["defaults"]) == set(data["caps"])
        utilization = data["utilization"]
        assert set(utilization) == {"source", "quality", "stale_inputs", "metrics"}
        assert set(utilization["metrics"]) == set(data["caps"])
        for metric in utilization["metrics"].values():
            assert set(metric) == {"used", "cap", "unit", "source", "quality", "note"}
        serialized = json.dumps(data).lower()
        for marker in ("secret", "api_key", "token", "password"):
            assert marker not in serialized


def test_risk_settings_errors_use_catalogued_codes_and_the_envelope(
    tmp_path: Path, validator: Draft202012Validator
) -> None:
    catalog_codes = {entry["code"] for entry in _load("error-catalog.json")["codes"]}
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        no_key = client.put(
            "/api/v1/settings/risk",
            json={"caps": {"per_trade_risk_pct": 0.5}},
            headers=auth,
        )
        loosening = client.put(
            "/api/v1/settings/risk",
            json={"caps": {"max_leverage": 6.0}},
            headers={**auth, "Idempotency-Key": "contract-risk-loosen"},
        )
        extra = client.put(
            "/api/v1/settings/risk",
            json={"caps": {"per_trade_risk_pct": 0.5}, "unknown": True},
            headers={**auth, "Idempotency-Key": "contract-risk-extra"},
        )

    for response in (no_key, loosening, extra):
        _validate(validator, response.json())

    assert no_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert loosening.json()["error"]["code"] == "VALIDATION_FAILED"
    assert extra.json()["error"]["code"] == "VALIDATION_FAILED"
    for response in (no_key, loosening, extra):
        assert response.json()["error"]["code"] in catalog_codes


def test_settings_sections_include_the_risk_section(
    tmp_path: Path, validator: Draft202012Validator
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings, clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.get("/api/v1/settings", headers=_auth(app))

    _validate(validator, response.json())
    sections = {item["section"]: item for item in response.json()["data"]["sections"]}
    assert set(sections) == {"scan", "risk"}
    assert sections["risk"]["source"] == "default"
    assert sections["risk"]["last_changed"] is None
    assert sections["risk"]["keys"] == [
        "per_trade_risk_pct",
        "max_concurrent_positions",
        "daily_loss_pct",
        "max_leverage",
        "max_notional_pct",
        "max_slippage_bps",
        "max_spread_bps",
        "stale_mark_seconds",
    ]


def test_llm_settings_payloads_validate_and_never_include_a_key(
    tmp_path: Path, validator: Draft202012Validator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.application import secrets_service as secrets_module
    from oft.application.secrets_service import SecretBackend

    class _Memory(SecretBackend):
        kind = "encrypted_file"

        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def available(self) -> bool:
            return True

        def get(self, reference: str) -> str | None:
            return self.values.get(reference)

        def set(self, reference: str, value: str) -> None:
            self.values[reference] = value

        def delete(self, reference: str) -> None:
            self.values.pop(reference, None)

    memory = _Memory()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    key = "sk-contract-SENTINEL-7c1d"
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        responses = [
            client.get("/api/v1/settings/llm", headers=auth),
            client.put(
                "/api/v1/settings/llm",
                json={"provider": "openai", "roles": {"ANALYSIS": "gpt-contract"}},
                headers={**auth, "Idempotency-Key": "contract-llm-1"},
            ),
            client.put(
                "/api/v1/settings/llm",
                json={"base_urls": {"openai": "https://api.openai.com.evil.example"}},
                headers={**auth, "Idempotency-Key": "contract-llm-2"},
            ),
            client.put(
                "/api/v1/settings/llm/openai/key",
                json={"api_key": key},
                headers={**auth, "Idempotency-Key": "contract-llm-key"},
            ),
            client.get("/api/v1/settings/llm", headers=auth),
            client.delete(
                "/api/v1/settings/llm/openai/key",
                headers={**auth, "Idempotency-Key": "contract-llm-delete"},
            ),
        ]

    catalog_codes = {entry["code"] for entry in _load("error-catalog.json")["codes"]}
    for response in responses:
        _validate(validator, response.json())
        assert key not in response.text
        assert key[:12] not in response.text

    get_data = responses[0].json()["data"]
    assert set(get_data) == {
        "provider",
        "providers",
        "roles",
        "base_urls",
        "allow_custom_base_url",
        "concurrency",
        "source",
        "updated_at",
        "updated_by",
    }
    for row in get_data["providers"]:
        assert set(row) == {
            "provider",
            "configured",
            "key_source",
            "masked",
            "fingerprint",
            "updated_at",
            "updated_by",
            "base_url",
            "capability",
        }
    key_data = responses[3].json()["data"]
    assert set(key_data) == {
        "provider",
        "configured",
        "key_source",
        "masked",
        "fingerprint",
        "updated_at",
        "updated_by",
    }
    assert key_data["masked"] == "****7c1d"
    invalid = responses[2].json()["error"]
    assert invalid["code"] == "VALIDATION_FAILED"
    assert invalid["code"] in catalog_codes
    assert invalid["details"][0]["field"] == "base_urls.openai"
    assert invalid["details"][0]["code"] == "unofficial_host"
    serialized = json.dumps([response.json() for response in responses]).lower()
    for marker in ("api_key", "secret_value", "password"):
        assert marker not in serialized


def test_cli_config_envelopes_validate_the_shared_api_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator: Draft202012Validator,
) -> None:
    """``oft config show|set --json`` emits the same contract as the API."""
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    runner = CliRunner()

    shown = runner.invoke(cli_app, ["config", "show", "--json"])
    saved = runner.invoke(cli_app, ["config", "set", "scan.period", "8h", "--json"])

    for result in (shown, saved):
        assert result.exit_code == 0, result.output
        _validate(validator, json.loads(result.stdout))
