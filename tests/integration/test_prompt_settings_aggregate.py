"""``GET /api/v1/settings/prompts``: the list-only F10b read aggregate.

Writes stay on ``/api/v1/prompts``; this surface never accepts a mutation and
never returns a body, hash, or secret.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.prompts import REQUIRED_VARIABLES, PromptRole

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
SECRET_MARKERS = (
    "api_key",
    "api_secret",
    "password",
    "openai_api_key",
    "anthropic_api_key",
    "deepseek_api_key",
    "telegram_bot_token",
)

_CUSTOM_ANALYSIS_BODY = """\
Symbol: {{symbol}}
Timeframe: {{timeframe}}

<market_data>
{{metrics}}
</market_data>

<knowledge>
{{knowledge}}
</knowledge>

AGGREGATE CUSTOM MARKER
Respond with JSON matching this schema:
{{schema}}
"""

_ROLE_ORDER = (
    PromptRole.ANALYSIS,
    PromptRole.ENTRY_RECHECK,
    PromptRole.MANAGEMENT,
    PromptRole.REVIEW,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def _aggregate(client: TestClient, auth: dict[str, str]) -> dict[str, Any]:
    response = client.get("/api/v1/settings/prompts", headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["data"] is not None
    return body


def _create_analysis_version(
    client: TestClient, auth: dict[str, str], *, key: str, author: str = "tester"
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/prompts/analysis/versions",
        json={"body": _CUSTOM_ANALYSIS_BODY, "author": author, "reason": "aggregate"},
        headers={**auth, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_default_aggregate_lists_the_four_user_roles_as_built_in_v1(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        body = _aggregate(client, _auth(app))

    data = body["data"]
    assert data["total"] == 4
    assert [item["role"] for item in data["items"]] == [role.value for role in _ROLE_ORDER]
    assert [item["prompt_id"] for item in data["items"]] == [
        "analysis",
        "entry_recheck",
        "management",
        "review",
    ]
    for item, role in zip(data["items"], _ROLE_ORDER, strict=True):
        assert item["active_version"] == 1
        assert item["author"] == "system"
        assert item["updated_at"].endswith("Z")
        assert item["version_count"] == 1
        assert item["latest_version"] == 1
        assert item["required_variables"] == list(REQUIRED_VARIABLES[role])
        assert item["is_builtin_default"] is True
        assert set(item) == {
            "prompt_id",
            "role",
            "active_version",
            "author",
            "updated_at",
            "version_count",
            "latest_version",
            "required_variables",
            "is_builtin_default",
        }

    assert body["meta"]["provenance"] == "CALC"
    assert body["meta"]["data_quality"] == "COMPLETE"
    payload = json.dumps(data)
    # Metadata only: no template body, hash, or credential marker crosses this
    # read surface. ``AGGREGATE CUSTOM MARKER`` is not seeded yet in this test.
    assert "body" not in payload
    assert "body_hash" not in payload
    assert not any(marker in payload for marker in SECRET_MARKERS)


def test_a_custom_active_version_surfaces_its_author_and_clears_built_in_default(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        created = _create_analysis_version(client, auth, key="aggregate-create-1")
        # A draft does not change the active fields, but the family is no longer
        # a pure built-in default.
        after_draft = _aggregate(client, auth)["data"]["items"][0]
        assert after_draft["active_version"] == 1
        assert after_draft["author"] == "system"
        assert after_draft["version_count"] == 2
        assert after_draft["latest_version"] == created["version"]
        assert after_draft["is_builtin_default"] is False

        activated = client.post(
            "/api/v1/prompts/analysis/activate",
            json={"version": created["version"], "actor": "tester", "reason": "approved"},
            headers={**auth, "Idempotency-Key": "aggregate-activate-1"},
        )
        assert activated.status_code == 200, activated.text

        item = _aggregate(client, auth)["data"]["items"][0]
        assert item["active_version"] == created["version"]
        assert item["author"] == "tester"
        assert item["updated_at"].endswith("Z")
        assert item["version_count"] == 2
        assert item["latest_version"] == created["version"]
        assert item["required_variables"] == list(REQUIRED_VARIABLES[PromptRole.ANALYSIS])
        assert item["is_builtin_default"] is False


def test_restore_creates_a_new_version_but_only_activation_changes_the_aggregate(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        created = _create_analysis_version(client, auth, key="aggregate-create-2")
        client.post(
            "/api/v1/prompts/analysis/activate",
            json={"version": created["version"]},
            headers={**auth, "Idempotency-Key": "aggregate-activate-2"},
        )

        restored = client.post(
            "/api/v1/prompts/analysis/restore",
            json={"author": "tester", "reason": "restore"},
            headers={**auth, "Idempotency-Key": "aggregate-restore-2"},
        )
        assert restored.status_code == 201, restored.text
        restored_data = restored.json()["data"]
        assert restored_data["is_default"] is True
        assert restored_data["status"] == "DRAFT"

        after_restore = _aggregate(client, auth)["data"]["items"][0]
        assert after_restore["active_version"] == created["version"]
        assert after_restore["author"] == "tester"
        assert after_restore["version_count"] == 3
        assert after_restore["latest_version"] == restored_data["version"]
        # A restored *draft* is not the active built-in v1.
        assert after_restore["is_builtin_default"] is False

        client.post(
            "/api/v1/prompts/analysis/activate",
            json={"version": restored_data["version"]},
            headers={**auth, "Idempotency-Key": "aggregate-activate-restored"},
        )
        final = _aggregate(client, auth)["data"]["items"][0]
        assert final["active_version"] == restored_data["version"]
        # is_default alone is not enough: history has more than one version.
        assert final["is_builtin_default"] is False


def test_the_existing_prompts_api_response_shape_is_unchanged(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        listed = client.get("/api/v1/prompts", headers=auth)
        assert listed.status_code == 200, listed.text
        items = listed.json()["data"]["items"]
        analysis = next(item for item in items if item["prompt_id"] == "analysis")
        # Additive read fields added for the aggregate must not leak into the
        # existing catalog response.
        assert set(analysis) == {
            "prompt_id",
            "role",
            "kind",
            "active_version",
            "version_count",
            "latest_version",
            "updated_at",
        }
        assert any(item["prompt_id"] == "analysis:system" for item in items)

        # The write path is unchanged and still lives on /api/v1/prompts.
        created = client.post(
            "/api/v1/prompts/analysis/versions",
            json={"body": _CUSTOM_ANALYSIS_BODY, "author": "tester"},
            headers={**auth, "Idempotency-Key": "aggregate-write-path"},
        )
        assert created.status_code == 201, created.text
        missing_key = client.post(
            "/api/v1/prompts/analysis/versions",
            json={"body": _CUSTOM_ANALYSIS_BODY},
            headers=auth,
        )
        assert missing_key.status_code == 400
        assert missing_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_the_aggregate_route_rejects_writes(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        put = client.put(
            "/api/v1/settings/prompts",
            json={"prompt_id": "analysis", "body": "x"},
            headers={**auth, "Idempotency-Key": "aggregate-put"},
        )
        post = client.post(
            "/api/v1/settings/prompts",
            json={},
            headers={**auth, "Idempotency-Key": "aggregate-post"},
        )
        delete = client.delete(
            "/api/v1/settings/prompts",
            headers={**auth, "Idempotency-Key": "aggregate-delete"},
        )

    assert put.status_code == 405
    assert post.status_code == 405
    assert delete.status_code == 405


def test_the_aggregate_requires_authentication(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.get("/api/v1/settings/prompts")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_a_missing_user_role_is_omitted_and_reported_partial(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    store_path = app.state.container.store.path
    # Simulate seeded-template data loss for one USER role, then read the
    # aggregate: the missing role is omitted (never fabricated) and the
    # remaining three keep their stable order.
    connection = sqlite3.connect(store_path)
    try:
        connection.execute("DELETE FROM prompt_versions WHERE prompt_id = 'management'")
        connection.execute("DELETE FROM prompt_templates WHERE prompt_id = 'management'")
        connection.commit()
    finally:
        connection.close()

    with TestClient(app) as client:
        body = _aggregate(client, _auth(app))

    data = body["data"]
    assert data["total"] == 3
    assert [item["role"] for item in data["items"]] == [
        PromptRole.ANALYSIS.value,
        PromptRole.ENTRY_RECHECK.value,
        PromptRole.REVIEW.value,
    ]
    assert [item["prompt_id"] for item in data["items"]] == [
        "analysis",
        "entry_recheck",
        "review",
    ]
    assert all(item["prompt_id"] != "management" for item in data["items"])
    assert body["meta"]["data_quality"] == "PARTIAL"


def test_the_aggregate_never_includes_a_persisted_template_secret(
    tmp_path: Path,
) -> None:
    api_secret = "api-token-super-secret-value"
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        api_token=api_secret,
    )
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        body = _CUSTOM_ANALYSIS_BODY + f"\napi_token={api_secret}\n"
        created = client.post(
            "/api/v1/prompts/analysis/versions",
            json={"body": body, "author": api_secret, "reason": "sk-live-abcdefghijklmnop"},
            headers={**auth, "Idempotency-Key": "aggregate-secret"},
        )
        assert created.status_code == 201, created.text
        client.post(
            "/api/v1/prompts/analysis/activate",
            json={"version": created.json()["data"]["version"], "actor": "tester"},
            headers={**auth, "Idempotency-Key": "aggregate-secret-activate"},
        )
        response = client.get("/api/v1/settings/prompts", headers=auth)

    text = response.text
    assert api_secret not in text
    assert "sk-live-abcdefghijklmnop" not in text
    assert "api_token" not in text
    # The active author was itself a secret and is surfaced only redacted.
    assert "[REDACTED]" in text
