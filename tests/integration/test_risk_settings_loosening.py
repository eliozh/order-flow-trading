"""Every loosening direction requires confirmation; tightening never does."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.risk import CAP_FIELDS, DEFAULT_CAPS, RISK_CAPS_STATE_KEY

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
RISK_PATH = "/api/v1/settings/risk"

# Every cap has a direction that is more permissive than its default.
LOOSER: dict[str, float | int] = {
    "per_trade_risk_pct": 1.0,
    "max_concurrent_positions": 4,
    "daily_loss_pct": 4.0,
    "max_leverage": 6.0,
    "max_notional_pct": 21.0,
    "max_slippage_bps": 26.0,
    "max_spread_bps": 16.0,
    "stale_mark_seconds": 61,
}

# Every cap has a direction that is stricter than its default.
TIGHTER: dict[str, float | int] = {
    "per_trade_risk_pct": 0.5,
    "max_concurrent_positions": 2,
    "daily_loss_pct": 2.0,
    "max_leverage": 3.0,
    "max_notional_pct": 10.0,
    "max_slippage_bps": 10.0,
    "max_spread_bps": 10.0,
    "stale_mark_seconds": 30,
}


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def _put(
    client: TestClient,
    auth: dict[str, str],
    caps: dict[str, Any],
    *,
    key: str,
    confirm: bool = False,
) -> Any:
    return client.put(
        RISK_PATH,
        json={"caps": caps, "confirm": confirm, "reason": ""},
        headers={**auth, "Idempotency-Key": key},
    )


@pytest.fixture(autouse=True)
def _clear_risk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OFT_RISK_PER_TRADE_PCT",
        "OFT_RISK_MAX_CONCURRENT_POSITIONS",
        "OFT_RISK_DAILY_LOSS_PCT",
        "OFT_RISK_MAX_LEVERAGE",
        "OFT_RISK_MAX_NOTIONAL_PCT",
        "OFT_RISK_MAX_SLIPPAGE_BPS",
        "OFT_RISK_MAX_SPREAD_BPS",
        "OFT_RISK_STALE_MARK_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("field", list(CAP_FIELDS))
def test_every_loosening_direction_requires_confirmation(tmp_path: Path, field: str) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        refused = _put(client, auth, {field: LOOSER[field]}, key=f"loosen-{field}")
        store = app.state.container.store

        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "VALIDATION_FAILED"
        detail = next(item for item in error["details"] if item["field"] == field)
        assert detail["code"] == "confirmation_required"
        assert detail["before"] == getattr(DEFAULT_CAPS, field)
        assert detail["after"] == LOOSER[field]
        assert all(detail["code"] == "confirmation_required" for detail in error["details"])
        assert store.get_system_state(RISK_CAPS_STATE_KEY) is None
        assert store.list_audit_events() == ()

        confirmed = _put(client, auth, {field: LOOSER[field]}, key=f"confirm-{field}", confirm=True)

    assert confirmed.status_code == 200, confirmed.text
    row = app.state.container.store.get_system_state(RISK_CAPS_STATE_KEY)
    assert row is not None
    assert json.loads(row)[field] == LOOSER[field]
    audits = app.state.container.store.list_audit_events()
    assert len(audits) == 1
    payload = json.loads(audits[0].payload_json)["payload"]
    assert payload["loosened"] == [field]
    assert payload["before"][field] == getattr(DEFAULT_CAPS, field)
    assert payload["after"][field] == LOOSER[field]


@pytest.mark.parametrize("field", list(CAP_FIELDS))
def test_every_tightening_direction_saves_without_confirmation(tmp_path: Path, field: str) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put(client, _auth(app), {field: TIGHTER[field]}, key=f"tighten-{field}")

    assert response.status_code == 200, response.text
    row = app.state.container.store.get_system_state(RISK_CAPS_STATE_KEY)
    assert row is not None
    assert json.loads(row)[field] == TIGHTER[field]
    audits = app.state.container.store.list_audit_events()
    assert len(audits) == 1
    payload = json.loads(audits[0].payload_json)["payload"]
    assert payload["loosened"] == []
