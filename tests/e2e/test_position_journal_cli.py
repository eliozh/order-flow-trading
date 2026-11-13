"""E2E: ``oft pos list|show|monitor`` and ``oft log list|show`` over the real API.

The daemon runs offline against the committed archive Fixtures and the
deterministic ``stub`` provider. The position is closed by the manual tick and
every CLI value must match what the API returns, with every envelope validated.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
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
from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.main import app as cli_app
from oft.config import Settings
from oft.domain.time import utc_timestamp

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


@pytest.fixture()
def live_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """A real loopback daemon with the archive and stub LLM wired in."""
    port = _free_port()
    data_dir = tmp_path / "data"
    settings = Settings(
        env="test",
        port=port,
        data_dir=data_dir,
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )
    container = build_container(settings, initialize=True)
    assert (
        container.backfill.run(
            symbol="BTCUSDT", data_type="aggTrades", start=START, end=END
        ).run.status
        == "COMPLETE"
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host="127.0.0.1",
            port=port,
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
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", str(port))
    monkeypatch.setenv("OFT_DATA", str(data_dir))
    monkeypatch.setenv("OFT_ARCHIVE_FIXTURE_DIR", str(ARCHIVE))
    monkeypatch.setenv("OFT_LLM_PROVIDER", "stub")
    monkeypatch.setenv("OFT_LLM_FIXTURES_DIR", str(FIXTURES))
    try:
        yield settings
    finally:
        server.should_exit = True
        thread.join(timeout=5)


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


def _validate_document(name: str, payload: dict[str, Any]) -> None:
    """Validate a record against one committed contract (local refs only)."""
    document = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(document, registry=_registry()).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def _row_counts(settings: Settings) -> dict[str, int]:
    """Raw row counts, independent of any service projection."""
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
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
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT purpose, COUNT(*) FROM orders GROUP BY purpose"
        ).fetchall()
    return {str(purpose): int(count) for purpose, count in rows}


def _exit_order_payload(settings: Settings) -> dict[str, Any]:
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM orders WHERE purpose = 'EXIT'"
        ).fetchone()
    assert row is not None
    return json.loads(row[0])


def _json(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _open_position(settings: Settings) -> dict[str, Any]:
    client = ApiClient.from_settings(settings)
    scan = client.create_scan(
        {
            "symbol": "BTCUSDT",
            "period": "4h",
            "source": "backfill",
            "metrics_only": False,
            "provider": "stub",
        },
        idempotency_key="journal-scan",
    )
    opportunity_id = scan["data"]["report"]["llm"]["opportunity_ids"][0]
    evaluated = client.evaluate_trigger(
        {"opportunity_id": opportunity_id}, idempotency_key="journal-trigger"
    )
    trigger_id = evaluated["data"]["trigger"]["trigger_id"]
    client._request(
        "POST",
        f"/api/v1/triggers/{trigger_id}/recheck",
        {},
        extra_headers={"Idempotency-Key": "journal-recheck"},
    )
    accepted = client.accept_trigger(trigger_id, idempotency_key="journal-accept")
    return accepted["data"]["position"]


def _set_mark(settings: Settings, position_id: str, mark: str, *, as_of: str) -> None:
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM positions WHERE position_id = ?",
            (position_id,),
        ).fetchone()
        payload = json.loads(row[0])
        payload["mark_price"] = mark
        payload["updated_at"] = as_of
        connection.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ?",
            (json.dumps(payload, sort_keys=True), position_id),
        )
        connection.commit()


def _now() -> str:
    return utc_timestamp(datetime.now(UTC))


def _touch_stop(settings: Settings, position: dict[str, Any]) -> None:
    _set_mark(settings, position["position_id"], position["stop_price"], as_of=_now())


def _mark_away_from_stop(position: dict[str, Any]) -> str:
    """A mark on the non-triggering side of the stop for this position side."""
    stop = Decimal(position["stop_price"])
    offset = Decimal("1000")
    return str(stop + offset if position["side"] == "LONG" else stop - offset)


def test_cli_position_and_journal_surfaces_match_the_api(live_daemon: Settings) -> None:
    runner = CliRunner()
    position = _open_position(live_daemon)
    position_id = position["position_id"]
    _touch_stop(live_daemon, position)

    monitored = _json(runner.invoke(cli_app, ["pos", "monitor", position_id, "--json"]))
    _validate_envelope(monitored)
    assert monitored["data"]["status"] == "CLOSED"
    assert monitored["data"]["reason_code"] == "STOP_TOUCH"
    assert monitored["data"]["trade_log"]["position_id"] == position_id

    replay = _json(runner.invoke(cli_app, ["pos", "monitor", position_id, "--json"]))
    _validate_envelope(replay)
    assert replay["meta"]["idempotent_replay"] is True
    assert replay["data"]["trade_log"]["trade_id"] == monitored["data"]["trade_log"]["trade_id"]

    # Exactly one exit order and fill, one CLOSED update, one trade log, and no
    # second position row for the exit.
    assert _row_counts(live_daemon) == {
        "positions": 1,
        "orders": 2,
        "fills": 2,
        "trade_logs": 1,
    }
    assert _order_counts_by_purpose(live_daemon) == {"ENTRY": 1, "EXIT": 1}
    exit_order = _exit_order_payload(live_daemon)
    assert exit_order["reduce_only"] is True
    assert exit_order["position_id"] == position_id
    assert exit_order["status"] == "FILLED"

    client = ApiClient.from_settings(live_daemon)
    api_position = client.get_position(position_id)
    _validate_envelope(api_position)
    cli_position = _json(runner.invoke(cli_app, ["pos", "show", position_id, "--json"]))
    _validate_envelope(cli_position)
    assert cli_position["data"] == api_position["data"]
    assert cli_position["data"]["state"] == "CLOSED"
    assert cli_position["data"]["closed_at"] is not None
    assert cli_position["data"]["exit_price"] is not None
    assert cli_position["data"]["realized_pnl_usd"] is not None

    listed = _json(runner.invoke(cli_app, ["pos", "list", "--json"]))
    _validate_envelope(listed)
    assert [item["position_id"] for item in listed["data"]["items"]] == [position_id]

    trade_id = monitored["data"]["trade_log"]["trade_id"]
    api_trade = client.get_trade_log(trade_id)
    _validate_envelope(api_trade)
    cli_trade = _json(runner.invoke(cli_app, ["log", "show", trade_id, "--json"]))
    _validate_envelope(cli_trade)
    assert cli_trade["data"] == api_trade["data"]

    # The journal record satisfies the committed trade-log contract, and the
    # monitor payload carries the same validated record.
    _validate_document("trade-log.schema.json", api_trade["data"])
    _validate_document("trade-log.schema.json", cli_trade["data"])
    _validate_document("trade-log.schema.json", monitored["data"]["trade_log"])
    assert api_trade["data"]["funding_usd"] == "0"

    journal = _json(runner.invoke(cli_app, ["log", "list", "--json"]))
    _validate_envelope(journal)
    assert [item["trade_id"] for item in journal["data"]["items"]] == [trade_id]
    assert journal["data"]["items"][0]["net_pnl_usd"] == api_trade["data"]["net_pnl_usd"]

    equity = _json(runner.invoke(cli_app, ["paper", "equity", "--json"]))
    _validate_envelope(equity)
    assert equity["data"]["realized_pnl_usd"] == api_trade["data"]["net_pnl_usd"]
    assert equity["data"]["closed_trades"] == 1


def _pending_trigger(settings: Settings) -> str:
    """Create a second opportunity/trigger that is still eligible for entry."""
    client = ApiClient.from_settings(settings)
    scan = client.create_scan(
        {
            "symbol": "BTCUSDT",
            "period": "4h",
            "source": "backfill",
            "metrics_only": False,
            "provider": "stub",
        },
        idempotency_key="second-scan",
    )
    opportunity_id = scan["data"]["report"]["llm"]["opportunity_ids"][0]
    evaluated = client.evaluate_trigger(
        {"opportunity_id": opportunity_id}, idempotency_key="second-trigger"
    )
    trigger_id = str(evaluated["data"]["trigger"]["trigger_id"])
    client._request(
        "POST",
        f"/api/v1/triggers/{trigger_id}/recheck",
        {},
        extra_headers={"Idempotency-Key": "second-recheck"},
    )
    return trigger_id


def test_a_mark_that_does_not_touch_the_stop_creates_no_order(
    live_daemon: Settings,
) -> None:
    runner = CliRunner()
    position = _open_position(live_daemon)
    position_id = position["position_id"]
    _set_mark(live_daemon, position_id, _mark_away_from_stop(position), as_of=_now())

    result = _json(runner.invoke(cli_app, ["pos", "monitor", position_id, "--json"]))

    _validate_envelope(result)
    assert result["data"]["status"] == "NO_ACTION"
    assert result["data"]["reason_code"] == "STOP_NOT_TOUCHED"
    assert result["meta"]["data_quality"] == "COMPLETE"
    assert _row_counts(live_daemon) == {
        "positions": 1,
        "orders": 1,
        "fills": 1,
        "trade_logs": 0,
    }
    shown = _json(runner.invoke(cli_app, ["pos", "show", position_id, "--json"]))
    _validate_envelope(shown)
    assert shown["data"]["state"] == "OPEN"
    journal = _json(runner.invoke(cli_app, ["log", "list", "--json"]))
    assert journal["data"]["items"] == []


def test_a_stale_mark_creates_no_order_and_reports_data_stale(
    live_daemon: Settings,
) -> None:
    runner = CliRunner()
    position = _open_position(live_daemon)
    position_id = position["position_id"]
    stale_seconds = int(live_daemon.risk_stale_mark_seconds) + 60
    stale_at = utc_timestamp(datetime.now(UTC) - timedelta(seconds=stale_seconds))
    _set_mark(live_daemon, position_id, position["stop_price"], as_of=stale_at)

    result = _json(runner.invoke(cli_app, ["pos", "monitor", position_id, "--json"]))

    _validate_envelope(result)
    assert result["data"]["status"] == "NO_ACTION"
    assert result["data"]["reason_code"] == "DATA_STALE"
    assert "older than" in result["data"]["reason"]
    assert result["meta"]["data_quality"] == "PARTIAL"
    assert _row_counts(live_daemon) == {
        "positions": 1,
        "orders": 1,
        "fills": 1,
        "trade_logs": 0,
    }
    rendered = runner.invoke(cli_app, ["pos", "monitor", position_id])
    assert rendered.exit_code == 0, rendered.output
    assert "DATA_STALE" in rendered.stdout
    assert "reason:" in rendered.stdout
    journal = _json(runner.invoke(cli_app, ["log", "list", "--json"]))
    assert journal["data"]["items"] == []


def test_kill_switch_permits_the_reduce_only_close_and_blocks_a_new_entry(
    live_daemon: Settings,
) -> None:
    runner = CliRunner()
    client = ApiClient.from_settings(live_daemon)
    position = _open_position(live_daemon)
    position_id = position["position_id"]
    pending_trigger = _pending_trigger(live_daemon)
    _touch_stop(live_daemon, position)

    activated = client.set_kill_switch(active=True)
    _validate_envelope(activated)
    assert activated["data"]["active"] is True

    # The reduce-only protective exit is risk-reducing and must still go through.
    monitored = _json(runner.invoke(cli_app, ["pos", "monitor", position_id, "--json"]))
    _validate_envelope(monitored)
    assert monitored["data"]["status"] == "CLOSED"
    assert monitored["data"]["action"] == "FULL_CLOSE"
    assert monitored["data"]["trade_log"]["position_id"] == position_id

    # A new entry is risk-increasing and must be blocked by the switch.
    with pytest.raises(ApiClientError) as blocked:
        client.accept_trigger(pending_trigger, idempotency_key="blocked-entry")
    assert blocked.value.code == "KILL_SWITCH_ACTIVE"
    assert _order_counts_by_purpose(live_daemon) == {"ENTRY": 1, "EXIT": 1}
    assert _row_counts(live_daemon)["trade_logs"] == 1


def test_cli_show_reports_a_missing_position_with_the_catalog_error(
    live_daemon: Settings,
) -> None:
    result = CliRunner().invoke(cli_app, ["pos", "show", "POS-MISSING", "--json"])

    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    _validate_envelope(payload)
    assert payload["error"]["code"] == "NOT_FOUND"


def test_api_maps_missing_and_unbound_positions_to_catalog_errors(
    live_daemon: Settings,
) -> None:
    client = ApiClient.from_settings(live_daemon)
    with pytest.raises(ApiClientError) as monitor_missing:
        client.monitor_position("POS-MISSING")
    assert monitor_missing.value.code == "NOT_FOUND"
    with pytest.raises(ApiClientError) as show_missing:
        client.get_position("POS-MISSING")
    assert show_missing.value.code == "NOT_FOUND"

    position = _open_position(live_daemon)
    database = live_daemon.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM positions WHERE position_id = ?",
            (position["position_id"],),
        ).fetchone()
        payload = json.loads(row[0])
        payload["source_trigger_id"] = None
        payload["mark_price"] = position["stop_price"]
        payload["updated_at"] = utc_timestamp(datetime.now(UTC))
        connection.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ?",
            (json.dumps(payload, sort_keys=True), position["position_id"]),
        )
        connection.commit()

    with pytest.raises(ApiClientError) as unbound:
        client.monitor_position(position["position_id"])
    assert unbound.value.code == "DATA_UNAVAILABLE"
