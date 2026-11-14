"""E2E: the paper management request behind the position-detail action.

The daemon runs in-process on a ``FixedClock`` with the committed archive and
the deterministic ``stub`` provider. Only a validated management decision can
reach the single paper gateway; the same ``Idempotency-Key`` replays instead of
creating a second order, CSRF is required for browser requests, and no live
route exists.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

import oft.bootstrap as bootstrap
from oft.adapters.llm_stub import LLMStub
from oft.adapters.sqlite_store import SqliteStore
from oft.api.app import create_app
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.ports.llm import LLMPort, LLMRole

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
CONTRACTS = ROOT / "docs" / "contracts"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 10, 8, tzinfo=UTC)
SYMBOL = "BTCUSDT"
PERIOD = "4h"


def _registry() -> Registry:
    """Every committed schema, so the envelope's local refs resolve offline."""
    resources = []
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        resources.append(
            (document["$id"], Resource.from_contents(document, default_specification=DRAFT202012))
        )
    return Registry().with_resources(resources)


def _validate_envelope(envelope: dict[str, Any]) -> None:
    """The response satisfies the committed API envelope contract."""
    document = json.loads((CONTRACTS / "api-envelope.schema.json").read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(document, registry=_registry()).iter_errors(envelope),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


@dataclass
class _ManageHarness:
    settings: Settings
    client: TestClient
    auth: dict[str, str]


def _settings(tmp_path: Path, index: int) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / f"data-{index}",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


@pytest.fixture()
def manage_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str], _ManageHarness]]:
    """One in-process daemon per call, pinned to a recorded MANAGEMENT fixture."""
    original = bootstrap.build_llm_adapter
    started: list[TestClient] = []
    counter = {"value": 0}

    def start(management_fixture: str = "management_hold.json") -> _ManageHarness:
        settings = _settings(tmp_path, counter["value"])
        counter["value"] += 1

        def factory(settings_value: Settings, name: str | None) -> LLMPort | None:
            if (name or "").strip().lower() == "stub":
                return LLMStub(
                    fixtures_dir=FIXTURES,
                    overrides={LLMRole.MANAGEMENT: management_fixture},
                )
            return original(settings_value, name)

        monkeypatch.setattr(bootstrap, "build_llm_adapter", factory)
        app = create_app(settings, clock=FixedClock(NOW))
        container = app.state.container
        assert (
            container.backfill.run(
                symbol=SYMBOL, data_type="aggTrades", start=START, end=END
            ).run.status
            == "COMPLETE"
        )
        client = TestClient(app)
        started.append(client)
        return _ManageHarness(
            settings=settings,
            client=client,
            auth={"Authorization": f"Bearer {app.state.api_token}"},
        )

    try:
        yield start
    finally:
        for client in started:
            client.close()


def _open_position(harness: _ManageHarness) -> dict[str, Any]:
    scan = harness.client.post(
        "/api/v1/scans",
        json={
            "symbol": SYMBOL,
            "period": PERIOD,
            "source": "backfill",
            "metrics_only": False,
            "provider": "stub",
        },
        headers={**harness.auth, "Idempotency-Key": "manage-scan"},
    )
    assert scan.status_code == 201, scan.text
    report_id = scan.json()["data"]["report_id"]
    report = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth).json()["data"]
    opportunity_id = report["llm"]["opportunity_ids"][0]
    evaluated = harness.client.post(
        "/api/v1/triggers/evaluate",
        json={"opportunity_id": opportunity_id},
        headers={**harness.auth, "Idempotency-Key": "manage-evaluate"},
    )
    assert evaluated.status_code == 201, evaluated.text
    trigger_id = evaluated.json()["data"]["trigger"]["trigger_id"]
    recheck = harness.client.post(
        f"/api/v1/triggers/{trigger_id}/recheck",
        headers={**harness.auth, "Idempotency-Key": "manage-recheck"},
    )
    assert recheck.status_code == 200, recheck.text
    accepted = harness.client.post(
        f"/api/v1/triggers/{trigger_id}/accept",
        headers={**harness.auth, "Idempotency-Key": "manage-accept"},
    )
    assert accepted.status_code == 201, accepted.text
    return accepted.json()["data"]["position"]


def _orders_total(harness: _ManageHarness) -> int:
    response = harness.client.get("/api/v1/paper/orders", headers=harness.auth)
    assert response.status_code == 200, response.text
    return int(response.json()["data"]["total"])


def _manage(
    harness: _ManageHarness,
    position_id: str,
    key: str,
    *headers: tuple[str, str],
    cookies: dict[str, str] | None = None,
) -> Any:
    extra = dict(headers)
    return harness.client.post(
        f"/api/v1/positions/{position_id}/manage",
        json={},
        headers={**harness.auth, "Idempotency-Key": key, **extra},
        cookies=cookies,
    )


def test_manage_requires_an_idempotency_key(
    manage_harness: Callable[[str], _ManageHarness],
) -> None:
    harness = manage_harness()
    position = _open_position(harness)
    before = _orders_total(harness)

    response = harness.client.post(
        f"/api/v1/positions/{position['position_id']}/manage",
        json={},
        headers=harness.auth,
    )

    assert response.status_code == 400, response.text
    error_envelope = response.json()
    _validate_envelope(error_envelope)
    assert error_envelope["ok"] is False
    assert error_envelope["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert _orders_total(harness) == before


def test_manage_requires_csrf_for_a_browser_request(
    manage_harness: Callable[[str], _ManageHarness],
) -> None:
    harness = manage_harness("management_hold.json")
    position = _open_position(harness)
    before = _orders_total(harness)

    blocked = _manage(
        harness,
        position["position_id"],
        "csrf-1",
        ("Sec-Fetch-Site", "same-origin"),
    )
    assert blocked.status_code == 403, blocked.text
    blocked_envelope = blocked.json()
    _validate_envelope(blocked_envelope)
    assert blocked_envelope["error"]["code"] == "CSRF_INVALID"
    assert _orders_total(harness) == before

    allowed = _manage(
        harness,
        position["position_id"],
        "csrf-2",
        ("Sec-Fetch-Site", "same-origin"),
        ("X-CSRF-Token", "csrf-token"),
        cookies={"oft_csrf": "csrf-token"},
    )
    assert allowed.status_code == 200, allowed.text
    allowed_envelope = allowed.json()
    _validate_envelope(allowed_envelope)
    assert allowed_envelope["data"]["action"] == "HOLD"


def test_manage_hold_returns_llm_calc_and_status_without_an_order(
    manage_harness: Callable[[str], _ManageHarness],
) -> None:
    harness = manage_harness("management_hold.json")
    position = _open_position(harness)
    before = _orders_total(harness)

    response = _manage(harness, position["position_id"], "hold-1")

    assert response.status_code == 200, response.text
    envelope = response.json()
    _validate_envelope(envelope)
    assert envelope["ok"] is True
    assert envelope["meta"]["provenance"] == "MIXED"
    assert envelope["meta"]["idempotent_replay"] is False
    data = envelope["data"]
    assert data["position_id"] == position["position_id"]
    assert data["decision_id"]
    assert data["action"] == "HOLD"
    assert data["status"] == "NO_ACTION"
    assert data["executed"] is False
    assert data["reason_code"] == "HOLD"
    assert data["reason"]
    assert data["order"] is None and data["fill"] is None
    assert data["refusals"] == []
    assert data["llm"]["decision_id"] == data["decision_id"]
    assert data["llm"]["validation_status"] in {"VALID", "REPAIRED"}
    assert data["llm"]["provenance"]["provider"] == "stub"
    assert data["llm"]["provenance"]["model"] == "fixture"
    assert data["llm"]["provenance"]["prompt_version"] >= 1
    assert data["risk"] is None
    # The HOLD persists in the timeline the detail renders; no position change.
    assert data["position"]["state"] == "OPEN"
    assert data["position"]["size"] == position["size"]
    # The managed position snapshot carries the additive resolved mark too.
    assert set(data["position"]) >= {
        "resolved_mark_price",
        "resolved_mark_as_of",
        "resolved_mark_source",
    }
    assert Decimal(data["position"]["resolved_mark_price"]) > 0
    resolved_as_of = datetime.fromisoformat(
        str(data["position"]["resolved_mark_as_of"]).replace("Z", "+00:00")
    )
    assert resolved_as_of.tzinfo is not None
    assert data["position"]["resolved_mark_source"] in {"BARS", "POSITION"}
    assert _orders_total(harness) == before

    shown = harness.client.get(f"/api/v1/positions/{position['position_id']}", headers=harness.auth)
    assert shown.status_code == 200, shown.text
    shown_envelope = shown.json()
    _validate_envelope(shown_envelope)
    history = shown_envelope["data"]["management_history"]
    assert history[-1]["action"] == "HOLD"
    assert history[-1]["decision_id"] == data["decision_id"]
    assert history[-1]["executed_order_id"] is None


def test_duplicate_key_replays_and_creates_no_second_order(
    manage_harness: Callable[[str], _ManageHarness],
) -> None:
    harness = manage_harness("management_partial_close.json")
    position = _open_position(harness)
    assert _orders_total(harness) == 1

    first = _manage(harness, position["position_id"], "once-1")
    assert first.status_code == 200, first.text
    first_envelope = first.json()
    _validate_envelope(first_envelope)
    assert first_envelope["meta"]["idempotent_replay"] is False
    first_data = first_envelope["data"]
    assert first_data["action"] == "PARTIAL_CLOSE"
    assert first_data["status"] == "EXECUTED"
    assert first_data["reason_code"] == "PARTIAL_CLOSE"
    assert first_data["executed"] is True
    assert first_data["decision_id"]
    assert first_data["llm"]["decision_id"] == first_data["decision_id"]
    assert first_data["llm"]["validation_status"] in {"VALID", "REPAIRED"}
    # An executed action carries its risk evidence and its single order/fill.
    assert first_data["risk"] is not None
    assert first_data["risk"]["verdict"]
    order = first_data["order"]
    fill = first_data["fill"]
    assert order is not None and fill is not None
    assert fill["order_id"] == order["order_id"]
    assert order["position_id"] == position["position_id"]
    assert Decimal(order["quantity"]) > 0
    assert Decimal(order["quantity"]) == Decimal(fill["quantity"])
    updated = first_data["position"]
    assert updated["state"] == "OPEN"
    assert set(updated) >= {
        "resolved_mark_price",
        "resolved_mark_as_of",
        "resolved_mark_source",
    }
    assert Decimal(updated["resolved_mark_price"]) > 0
    assert updated["resolved_mark_source"] in {"BARS", "POSITION"}
    assert Decimal(updated["size"]) == Decimal(position["size"]) - Decimal(fill["quantity"])
    entry = updated["management_history"][-1]
    assert entry["action"] == "PARTIAL_CLOSE"
    assert entry["decision_id"] == first_data["decision_id"]
    assert entry["executed_order_id"] == order["order_id"]
    assert _orders_total(harness) == 2

    # The accounting timeline the detail renders records the same execution.
    shown_history = harness.client.get(
        f"/api/v1/positions/{position['position_id']}/history", headers=harness.auth
    )
    assert shown_history.status_code == 200, shown_history.text
    history_envelope = shown_history.json()
    _validate_envelope(history_envelope)
    history_data = history_envelope["data"]
    assert history_data["state"] == "OPEN"
    assert history_data["size"] == updated["size"]
    assert history_data["pnl"]["basis"] == "ACCUMULATED_GROSS"
    assert history_data["pnl"]["gross_pnl_usd"] is not None
    executed = [event for event in history_data["events"] if event["action"] == "PARTIAL_CLOSE"]
    assert len(executed) == 1
    assert executed[0]["order_id"] == order["order_id"]

    second = _manage(harness, position["position_id"], "once-1")
    assert second.status_code == 200, second.text
    second_envelope = second.json()
    _validate_envelope(second_envelope)
    assert second_envelope["meta"]["idempotent_replay"] is True
    assert second_envelope["data"] == first_envelope["data"]
    assert _orders_total(harness) == 2


def test_blocked_add_names_the_binding_cap_and_creates_no_order(
    manage_harness: Callable[[str], _ManageHarness],
) -> None:
    harness = manage_harness("management_add.json")
    position = _open_position(harness)
    SqliteStore(harness.settings).set_system_state(
        "risk_caps",
        json.dumps({"max_concurrent_positions": 1}),
        updated_at=utc_timestamp(NOW),
    )
    before = _orders_total(harness)

    response = _manage(harness, position["position_id"], "blocked-add-1")

    assert response.status_code == 200, response.text
    envelope = response.json()
    _validate_envelope(envelope)
    data = envelope["data"]
    assert data["action"] == "ADD"
    assert data["status"] == "BLOCKED"
    assert data["executed"] is False
    assert data["reason_code"] == "RISK_BLOCKED"
    assert data["reason"]
    assert data["order"] is None and data["fill"] is None
    assert data["llm"]["validation_status"] in {"VALID", "REPAIRED"}
    assert data["risk"]["verdict"] == "BLOCK"
    assert data["risk"]["binding_cap"] == "MAX_POSITIONS"
    assert envelope["meta"]["data_quality"] == "PARTIAL"
    assert _orders_total(harness) == before

    shown = harness.client.get(f"/api/v1/positions/{position['position_id']}", headers=harness.auth)
    assert shown.status_code == 200, shown.text
    shown_envelope = shown.json()
    _validate_envelope(shown_envelope)
    payload = shown_envelope["data"]
    assert payload["state"] == "OPEN"
    assert payload["size"] == position["size"]
    # A blocked action appends no management-history entry.
    assert payload["management_history"] == position["management_history"]


def test_kill_switch_blocks_a_risk_increasing_management_request(
    manage_harness: Callable[[str], _ManageHarness],
) -> None:
    harness = manage_harness("management_add.json")
    position = _open_position(harness)
    before = _orders_total(harness)

    activated = harness.client.post("/api/v1/kill-switch", headers=harness.auth)
    assert activated.status_code == 200, activated.text
    assert activated.json()["data"]["active"] is True

    response = _manage(harness, position["position_id"], "kill-1")

    assert response.status_code == 423, response.text
    error_envelope = response.json()
    _validate_envelope(error_envelope)
    assert error_envelope["ok"] is False
    assert error_envelope["error"]["code"] == "KILL_SWITCH_ACTIVE"
    assert _orders_total(harness) == before

    shown = harness.client.get(f"/api/v1/positions/{position['position_id']}", headers=harness.auth)
    assert shown.status_code == 200, shown.text
    shown_envelope = shown.json()
    _validate_envelope(shown_envelope)
    assert shown_envelope["data"]["state"] == "OPEN"
    assert shown_envelope["data"]["size"] == position["size"]
    assert shown_envelope["data"]["management_history"] == position["management_history"]


def test_no_live_position_management_route_exists(
    manage_harness: Callable[[str], _ManageHarness],
) -> None:
    harness = manage_harness()
    position = _open_position(harness)
    position_id = position["position_id"]

    attempts = [
        harness.client.post(f"/api/v1/positions/{position_id}/live", headers=harness.auth),
        harness.client.post(
            f"/api/v1/live/positions/{position_id}/manage",
            json={},
            headers={**harness.auth, "Idempotency-Key": "live-1"},
        ),
        harness.client.get(f"/api/v1/positions/{position_id}/live", headers=harness.auth),
    ]
    for response in attempts:
        assert response.status_code in {404, 405}, response.text

    openapi = harness.client.get("/api/openapi.json")
    assert openapi.status_code == 200
    paths = set(openapi.json()["paths"])
    assert not any("/live" in path for path in paths)
