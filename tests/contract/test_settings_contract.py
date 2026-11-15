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

from oft.api.app import create_app
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
