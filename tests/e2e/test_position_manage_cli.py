"""E2E: the manual paper management journey behind ``oft pos manage``.

An OPEN paper position is created through the real loopback daemon (fixture
archive and the deterministic ``stub`` LLM). ``oft pos manage`` then asks the
``MANAGEMENT`` role for a decision, the code validates it, and only a validated
risk-reducing/within-cap action reaches the single paper gateway. Every CLI
envelope is validated against the committed API contract and cross-checked
against the API and the SQLite rows.

The daemon is in-process over a temporary data dir with a ``FixedClock``; the
market data comes from the committed archive fixtures, and the stub returns a
recorded management fixture (selected through the documented provider seam).
No network, provider, credential, or live path is touched.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from typer.testing import CliRunner

import oft.bootstrap as bootstrap
from oft.adapters.llm_stub import LLMStub
from oft.adapters.sqlite_store import SqliteStore
from oft.api.app import create_app
from oft.bootstrap import build_container
from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.main import app as cli_app
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=_free_port(),
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _seed_archive(settings: Settings) -> None:
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    result = container.backfill.run(symbol=SYMBOL, data_type="aggTrades", start=START, end=END)
    assert result.run.status == "COMPLETE"


def _start_daemon(settings: Settings) -> tuple[uvicorn.Server, threading.Thread]:
    """Start the real loopback daemon on a fixed clock and wait for startup."""
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, clock=FixedClock(NOW)),
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


def _export_cli_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", str(settings.port))
    monkeypatch.setenv("OFT_DATA", str(settings.data_dir))
    monkeypatch.setenv("OFT_ARCHIVE_FIXTURE_DIR", str(ARCHIVE))
    monkeypatch.setenv("OFT_LLM_PROVIDER", "stub")
    monkeypatch.setenv("OFT_LLM_FIXTURES_DIR", str(FIXTURES))


@dataclass
class _DaemonHandle:
    settings: Settings
    server: uvicorn.Server
    thread: threading.Thread


@pytest.fixture()
def manage_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str], Settings]]:
    """Start one daemon per call, each pinned to a recorded management fixture.

    The stub provider chooses its fixture by role, so the documented
    ``build_llm_adapter`` seam is used to select the recorded MANAGEMENT
    response without touching the committed fixtures.
    """
    original = bootstrap.build_llm_adapter
    started: list[_DaemonHandle] = []

    def start(management_fixture: str = "management_hold.json") -> Settings:
        settings = _settings(tmp_path / f"data-{len(started)}")

        def factory(settings_value: Settings, name: str | None) -> LLMPort | None:
            if (name or "").strip().lower() == "stub":
                return LLMStub(
                    fixtures_dir=FIXTURES,
                    overrides={LLMRole.MANAGEMENT: management_fixture},
                )
            return original(settings_value, name)

        monkeypatch.setattr(bootstrap, "build_llm_adapter", factory)
        _seed_archive(settings)
        server, thread = _start_daemon(settings)
        started.append(_DaemonHandle(settings=settings, server=server, thread=thread))
        _export_cli_env(monkeypatch, settings)
        return settings

    try:
        yield start
    finally:
        for handle in started:
            _stop_daemon(handle.server, handle.thread)


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


def _json(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _open_position(settings: Settings) -> dict[str, Any]:
    """Create one OPEN paper position through the real scan/trigger/accept path."""
    client = ApiClient.from_settings(settings)
    scan = client.create_scan(
        {
            "symbol": SYMBOL,
            "period": PERIOD,
            "source": "backfill",
            "metrics_only": False,
            "provider": "stub",
        },
        idempotency_key="manage-scan",
    )
    opportunity_id = scan["data"]["report"]["llm"]["opportunity_ids"][0]
    evaluated = client.evaluate_trigger(
        {"opportunity_id": opportunity_id}, idempotency_key="manage-trigger"
    )
    trigger_id = evaluated["data"]["trigger"]["trigger_id"]
    client._request(
        "POST",
        f"/api/v1/triggers/{trigger_id}/recheck",
        {},
        extra_headers={"Idempotency-Key": "manage-recheck"},
    )
    accepted = client.accept_trigger(trigger_id, idempotency_key="manage-accept")
    return accepted["data"]["position"]


def _database(settings: Settings) -> Path:
    return settings.data_dir / "oft.db"


def _row_counts(settings: Settings) -> dict[str, int]:
    """Raw row counts, independent of any service projection."""
    with sqlite3.connect(_database(settings)) as connection:
        positions = connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        orders = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        fills = connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        trade_logs = connection.execute("SELECT COUNT(*) FROM trade_logs").fetchone()[0]
    return {
        "positions": int(positions),
        "orders": int(orders),
        "fills": int(fills),
        "trade_logs": int(trade_logs),
    }


def _order_counts_by_purpose(settings: Settings) -> dict[str, int]:
    with sqlite3.connect(_database(settings)) as connection:
        rows = connection.execute(
            "SELECT purpose, COUNT(*) FROM orders GROUP BY purpose"
        ).fetchall()
    return {str(purpose): int(count) for purpose, count in rows}


def _stored_order(settings: Settings, purpose: str) -> dict[str, Any]:
    with sqlite3.connect(_database(settings)) as connection:
        row = connection.execute(
            "SELECT payload_json FROM orders WHERE purpose = ?", (purpose,)
        ).fetchone()
    assert row is not None, f"no {purpose} order was persisted"
    return json.loads(row[0])


def _fills_for_order(settings: Settings, order_id: str) -> int:
    with sqlite3.connect(_database(settings)) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM fills WHERE order_id = ?", (order_id,)
        ).fetchone()
    return int(row[0])


def _decision_row(settings: Settings, decision_id: str) -> tuple[str, str, str]:
    with sqlite3.connect(_database(settings)) as connection:
        row = connection.execute(
            "SELECT role, decision_type, validation_status FROM decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
    assert row is not None, f"decision {decision_id} was not persisted"
    return (str(row[0]), str(row[1]), str(row[2]))


def _persisted_decision(settings: Settings, decision_id: str) -> dict[str, Any]:
    with sqlite3.connect(_database(settings)) as connection:
        row = connection.execute(
            "SELECT payload_json FROM decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
    assert row is not None, f"decision {decision_id} was not persisted"
    return json.loads(row[0])


def _assert_management_provenance(llm: dict[str, Any], decision: dict[str, Any]) -> None:
    """A stub decision records provenance but no raw prompt or secret."""
    assert llm["validation_status"] in {"VALID", "REPAIRED"}
    provenance = llm["provenance"]
    assert set(provenance) == {
        "provider",
        "model",
        "prompt_version",
        "knowledge_artifact_hash",
        "cost",
    }
    assert provenance["provider"] == "stub"
    assert provenance["model"] == "fixture"
    assert provenance["prompt_version"] >= 1
    assert provenance["cost"]["amount_usd"] == "0"

    # The persisted decision carries the full provenance (hash) and never the
    # raw prompt body.
    persisted = decision["provenance"]
    assert persisted["prompt_hash"]
    assert persisted["input_hash"]
    assert persisted["provider"] == "stub"
    assert "system_prompt" not in decision
    assert "user_prompt" not in decision
    assert "raw_prompt" not in decision


def test_cli_manage_hold_returns_a_validated_decision_and_creates_no_order(
    manage_daemon: Callable[[str], Settings],
) -> None:
    settings = manage_daemon("management_hold.json")
    runner = CliRunner()
    position = _open_position(settings)
    position_id = position["position_id"]
    before = _row_counts(settings)

    result = _json(
        runner.invoke(
            cli_app,
            ["pos", "manage", position_id, "--idempotency-key", "manage-hold", "--json"],
        )
    )

    _validate_envelope(result)
    data = result["data"]
    assert result["meta"]["idempotent_replay"] is False
    assert data["position_id"] == position_id
    assert data["action"] == "HOLD"
    assert data["status"] == "NO_ACTION"
    assert data["executed"] is False
    assert data["reason_code"] == "HOLD"
    assert data["order"] is None
    assert data["fill"] is None
    assert data["trade_log"] is None
    _assert_management_provenance(data["llm"], _persisted_decision(settings, data["decision_id"]))

    # The decision is persisted as a validated MANAGEMENT record.
    assert _decision_row(settings, data["decision_id"]) == (
        "MANAGEMENT",
        "POSITION_MANAGEMENT",
        "VALID",
    )

    # HOLD creates no order, fill, or trade log and leaves the position OPEN.
    assert _row_counts(settings) == before
    assert _order_counts_by_purpose(settings) == {"ENTRY": 1}
    api_shown = ApiClient.from_settings(settings).get_position(position_id)
    _validate_envelope(api_shown)
    cli_shown = _json(runner.invoke(cli_app, ["pos", "show", position_id, "--json"]))
    _validate_envelope(cli_shown)
    assert cli_shown["data"] == api_shown["data"] == data["position"]
    assert cli_shown["data"]["state"] == "OPEN"
    history = cli_shown["data"]["management_history"]
    assert history[-1]["action"] == "HOLD"
    assert history[-1]["decision_id"] == data["decision_id"]
    assert history[-1]["executed_order_id"] is None


def test_cli_manage_partial_close_executes_one_order_fill_and_position(
    manage_daemon: Callable[[str], Settings],
) -> None:
    settings = manage_daemon("management_partial_close.json")
    runner = CliRunner()
    position = _open_position(settings)
    position_id = position["position_id"]
    original_size = Decimal(position["size"])

    result = _json(
        runner.invoke(
            cli_app,
            ["pos", "manage", position_id, "--idempotency-key", "manage-partial", "--json"],
        )
    )

    _validate_envelope(result)
    data = result["data"]
    assert data["action"] == "PARTIAL_CLOSE"
    assert data["status"] == "EXECUTED"
    assert data["executed"] is True
    assert data["reason_code"] == "PARTIAL_CLOSE"
    _assert_management_provenance(data["llm"], _persisted_decision(settings, data["decision_id"]))
    assert _decision_row(settings, data["decision_id"]) == (
        "MANAGEMENT",
        "POSITION_MANAGEMENT",
        "VALID",
    )

    order = data["order"]
    fill = data["fill"]
    updated = data["position"]
    assert order["reduce_only"] is True
    assert order["position_id"] == position_id
    assert order["status"] == "FILLED"
    assert order["client_order_id"].startswith("oft-paper-BTCUSDT-partial_close-")
    assert order["risk_verdict_id"]
    assert fill["order_id"] == order["order_id"]
    assert fill["source"] == "PAPER"
    assert updated["state"] == "OPEN"
    assert Decimal(updated["size"]) < original_size
    assert Decimal(updated["realized_pnl_usd"]) != 0

    # Exactly one reduce-only order and fill; a partial close writes no journal row.
    assert _row_counts(settings) == {
        "positions": 1,
        "orders": 2,
        "fills": 2,
        "trade_logs": 0,
    }
    assert _order_counts_by_purpose(settings) == {"ENTRY": 1, "PARTIAL_CLOSE": 1}
    stored = _stored_order(settings, "PARTIAL_CLOSE")
    assert stored["order_id"] == order["order_id"]
    assert stored["reduce_only"] is True
    assert stored["position_id"] == position_id
    assert _fills_for_order(settings, order["order_id"]) == 1

    # The updated position is identical across the manage envelope, the CLI,
    # and the API read model.
    api_shown = ApiClient.from_settings(settings).get_position(position_id)
    _validate_envelope(api_shown)
    cli_shown = _json(runner.invoke(cli_app, ["pos", "show", position_id, "--json"]))
    _validate_envelope(cli_shown)
    assert cli_shown["data"] == api_shown["data"] == updated
    journal = _json(runner.invoke(cli_app, ["log", "list", "--json"]))
    _validate_envelope(journal)
    assert journal["data"]["items"] == []


def test_duplicate_manage_with_the_same_idempotency_key_creates_no_second_order(
    manage_daemon: Callable[[str], Settings],
) -> None:
    settings = manage_daemon("management_partial_close.json")
    runner = CliRunner()
    position = _open_position(settings)
    position_id = position["position_id"]

    first = _json(
        runner.invoke(
            cli_app,
            ["pos", "manage", position_id, "--idempotency-key", "manage-once", "--json"],
        )
    )
    _validate_envelope(first)
    assert first["meta"]["idempotent_replay"] is False
    assert first["data"]["status"] == "EXECUTED"
    counts_after_first = _row_counts(settings)
    purposes_after_first = _order_counts_by_purpose(settings)
    assert counts_after_first["orders"] == 2
    assert purposes_after_first == {"ENTRY": 1, "PARTIAL_CLOSE": 1}

    second = _json(
        runner.invoke(
            cli_app,
            ["pos", "manage", position_id, "--idempotency-key", "manage-once", "--json"],
        )
    )
    _validate_envelope(second)
    assert second["meta"]["idempotent_replay"] is True
    assert second["data"] == first["data"]
    assert _row_counts(settings) == counts_after_first
    assert _order_counts_by_purpose(settings) == purposes_after_first

    # A different payload under the same key is rejected before any execution.
    client = ApiClient.from_settings(settings)
    with pytest.raises(ApiClientError) as conflict:
        client.manage_position(position_id, {"provider": "openai"}, idempotency_key="manage-once")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    assert _row_counts(settings) == counts_after_first


def test_a_blocked_add_decision_creates_no_order_and_names_the_binding_cap(
    manage_daemon: Callable[[str], Settings],
) -> None:
    settings = manage_daemon("management_add.json")
    runner = CliRunner()
    position = _open_position(settings)
    position_id = position["position_id"]
    SqliteStore(settings).set_system_state(
        "risk_caps",
        json.dumps({"max_concurrent_positions": 1}),
        updated_at=utc_timestamp(NOW),
    )
    before = _row_counts(settings)

    result = _json(
        runner.invoke(
            cli_app,
            ["pos", "manage", position_id, "--idempotency-key", "manage-add", "--json"],
        )
    )

    _validate_envelope(result)
    data = result["data"]
    assert data["action"] == "ADD"
    assert data["status"] == "BLOCKED"
    assert data["executed"] is False
    assert data["reason_code"] == "RISK_BLOCKED"
    assert data["order"] is None
    assert data["fill"] is None
    assert data["trade_log"] is None
    assert data["risk"]["verdict"] == "BLOCK"
    assert data["risk"]["binding_cap"] == "MAX_POSITIONS"
    assert "daily_loss_usd" in data["risk"]["stale_inputs"]
    assert result["meta"]["data_quality"] == "PARTIAL"

    # A blocked management action writes no order/fill/journal row.
    assert _row_counts(settings) == before
    assert _order_counts_by_purpose(settings) == {"ENTRY": 1}
    shown = _json(runner.invoke(cli_app, ["pos", "show", position_id, "--json"]))
    _validate_envelope(shown)
    assert shown["data"]["state"] == "OPEN"
    assert Decimal(shown["data"]["size"]) == Decimal(position["size"])
