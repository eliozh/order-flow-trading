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
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from typer.testing import CliRunner

from oft.adapters.parquet_store import ParquetStore
from oft.adapters.sqlite_store import SqliteStore
from oft.api.app import create_app
from oft.application.paper_broker import OrderIntent, PaperBroker
from oft.bootstrap import build_container
from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.main import app as cli_app
from oft.clock import SystemClock
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, PositionSide
from oft.domain.resampling import Bar
from oft.domain.time import utc_timestamp
from position_support import set_position_mark

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
    """The daemon settings for one test: archive fixtures and the stub LLM."""
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
    """Start the real loopback daemon and wait for its startup."""
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


def _export_cli_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", str(settings.port))
    monkeypatch.setenv("OFT_DATA", str(settings.data_dir))
    monkeypatch.setenv("OFT_ARCHIVE_FIXTURE_DIR", str(ARCHIVE))
    monkeypatch.setenv("OFT_LLM_PROVIDER", "stub")
    monkeypatch.setenv("OFT_LLM_FIXTURES_DIR", str(FIXTURES))


@pytest.fixture()
def live_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """A real loopback daemon with the archive and stub LLM wired in."""
    settings = _live_settings(tmp_path)
    _seed_archive(settings)
    server, thread = _start_daemon(settings)
    _export_cli_env(monkeypatch, settings)
    try:
        yield settings
    finally:
        _stop_daemon(server, thread)


@dataclass
class _RestartableDaemon:
    """A live daemon that can be stopped and started again on the same data."""

    settings: Settings
    server: uvicorn.Server
    thread: threading.Thread

    def restart(self) -> None:
        """Stop the process-equivalent daemon and start a fresh one."""
        _stop_daemon(self.server, self.thread)
        self.server, self.thread = _start_daemon(self.settings)


@pytest.fixture()
def restartable_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_RestartableDaemon]:
    """A real loopback daemon over one data dir that a test may restart."""
    settings = _live_settings(tmp_path)
    _seed_archive(settings)
    server, thread = _start_daemon(settings)
    _export_cli_env(monkeypatch, settings)
    daemon = _RestartableDaemon(settings=settings, server=server, thread=thread)
    try:
        yield daemon
    finally:
        _stop_daemon(daemon.server, daemon.thread)


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


def _now() -> str:
    return utc_timestamp(datetime.now(UTC))


def _touch_stop(settings: Settings, position: dict[str, Any]) -> None:
    set_position_mark(settings, position["position_id"], position["stop_price"], as_of=_now())


def _mark_away_from_stop(position: dict[str, Any]) -> str:
    """A mark on the non-triggering side of the stop for this position side."""
    stop = Decimal(position["stop_price"])
    offset = Decimal("1000")
    return str(stop + offset if position["side"] == "LONG" else stop - offset)


def _balance(settings: Settings) -> str:
    """The paper account balance over the loopback API, envelope-validated."""
    envelope = ApiClient.from_settings(settings)._request("GET", "/api/v1/paper/account")
    _validate_envelope(envelope)
    return str(envelope["data"]["balance"])


def _submit_partial_close(
    settings: Settings,
    position: dict[str, Any],
    *,
    quantity: Decimal,
    idempotency_key: str,
) -> Any:
    """Apply a reduce-only slice through the production broker.

    Slice 24 exposes no public partial-close route (the manual tick only does a
    protective full close). The broker is the one production write path, so the
    test drives it against the daemon's database and then reads the result back
    over the real loopback API and CLI.
    """
    position_side = PositionSide(position["side"])
    side = OrderSide.SELL if position_side is PositionSide.LONG else OrderSide.BUY
    store = SqliteStore(settings)
    entry_order = next(
        record for record in store.list_orders(limit=100) if record.purpose == "ENTRY"
    )
    entry_payload = json.loads(entry_order.payload_json)
    intent = OrderIntent(
        symbol=position["symbol"],
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        price=None,
        stop_price=Decimal(str(position["stop_price"])),
        take_profits=(),
        leverage=float(position["leverage"]),
        decision_id=f"DEC-{idempotency_key}",
        risk_verdict_id=entry_order.risk_verdict_id,
        approval_id=str(entry_payload.get("approval_id") or "APR-MGMT"),
        idempotency_key=idempotency_key,
        position_side=position_side,
        risk_amount_usd=Decimal("0"),
        risk_pct=0.0,
        purpose="EXIT",
        position_id=position["position_id"],
        trigger_id=position.get("source_trigger_id"),
    )
    broker = PaperBroker(settings=settings, store=store, clock=SystemClock())
    return broker.submit(
        intent,
        mark_price=Decimal(str(position["mark_price"])),
        mark_as_of=str(position["updated_at"]),
        now=_now(),
    )


def _bars_series(settings: Settings) -> tuple[str, str]:
    """The scanned bars series ``(period, metric_version)`` for BTCUSDT."""
    for entry in SqliteStore(settings).list_manifest():
        if entry.data_type != "bars" or entry.symbol != "BTCUSDT" or not entry.metric_version:
            continue
        marker = "/period="
        if marker in entry.path:
            return entry.path.split(marker, 1)[1].split("/", 1)[0], entry.metric_version
    raise AssertionError("the scan did not publish a bars series")


def _seed_bar(
    settings: Settings,
    *,
    close: Decimal,
    bar_end: datetime,
    period: str,
    metric_version: str,
    run_id: str,
) -> None:
    """Publish one recorded bar so funding and MAE/MFE have a recorded mark."""
    bar_start = bar_end - timedelta(hours=4)
    bar = Bar(
        symbol="BTCUSDT",
        period=period,
        bar_start=bar_start,
        bar_end=bar_end,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal("1"),
        quote_volume=close,
        trade_count=1,
        taker_buy_volume=Decimal("0"),
        source_mode="BACKFILL",
    )
    entry = ParquetStore(settings).write_bar_part(
        (bar,),
        symbol="BTCUSDT",
        period=period,
        window_start=bar_start,
        window_end=bar_end,
        metric_version=metric_version,
        input_manifest_hash="e2e-funding-input",
        source_mode="BACKFILL",
        source_digest="e2e-funding-digest",
        run_id=run_id,
        computed_at=utc_timestamp(bar_end),
        data_quality="COMPLETE",
        quality_reasons=(),
    )
    SqliteStore(settings).upsert_manifest_entry(entry)


def _source_report_id(settings: Settings, position: dict[str, Any]) -> str:
    """Walk trigger -> opportunity -> source report for the position."""
    store = SqliteStore(settings)
    trigger = store.get_trigger_event(str(position["source_trigger_id"]))
    assert trigger is not None
    opportunity = store.get_opportunity(trigger.opportunity_id)
    assert opportunity is not None
    source = json.loads(opportunity.payload_json)["source"]
    report_id = source.get("report_id")
    assert isinstance(report_id, str) and report_id
    return report_id


def _inject_funding_history(
    settings: Settings, report_id: str, points: list[dict[str, object]]
) -> None:
    """Record funding settlements in the source report's metrics document."""
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT snapshot_json FROM reports WHERE report_id = ?", (report_id,)
        ).fetchone()
        assert row is not None
        snapshot = json.loads(row[0])
        snapshot.setdefault("orderflow", {})["funding"] = {
            "current_rate": "0.0001",
            "next_funding_time": points[0]["ts"] if points else None,
            "interval_hours": 8,
            "mark_price": "71000",
            "index_price": "71000",
            "history": points,
        }
        connection.execute(
            "UPDATE reports SET snapshot_json = ? WHERE report_id = ?",
            (json.dumps(snapshot, sort_keys=True), report_id),
        )
        connection.commit()


def _slice_gross(fill: dict[str, Any], entry_avg: Decimal, long_side: bool) -> Decimal:
    """One realized slice, quantized the way the domain does it."""
    move = Decimal(str(fill["price"])) - entry_avg
    if not long_side:
        move = -move
    raw = move * Decimal(str(fill["quantity"]))
    return raw.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)


def _fills_by_purpose(settings: Settings) -> dict[str, list[dict[str, Any]]]:
    """Persisted fill payloads grouped by their order's purpose, oldest first."""
    store = SqliteStore(settings)
    purposes = {record.order_id: record.purpose for record in store.list_orders(limit=100)}
    grouped: dict[str, list[dict[str, Any]]] = {"ENTRY": [], "EXIT": [], "ADD": []}
    fills = sorted(
        (json.loads(record.payload_json) for record in store.list_fills(limit=100)),
        key=lambda fill: str(fill["filled_at"]),
    )
    for fill in fills:
        grouped.setdefault(purposes[fill["order_id"]], []).append(fill)
    return grouped


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
    set_position_mark(live_daemon, position_id, _mark_away_from_stop(position), as_of=_now())

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
    set_position_mark(live_daemon, position_id, position["stop_price"], as_of=stale_at)

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


def test_cli_position_history_and_log_show_expose_the_pnl_breakdown(
    live_daemon: Settings,
) -> None:
    runner = CliRunner()
    client = ApiClient.from_settings(live_daemon)
    position = _open_position(live_daemon)
    position_id = position["position_id"]
    _touch_stop(live_daemon, position)
    monitored = _json(runner.invoke(cli_app, ["pos", "monitor", position_id, "--json"]))
    _validate_envelope(monitored)
    trade_id = monitored["data"]["trade_log"]["trade_id"]
    trade = monitored["data"]["trade_log"]

    # The journal record is complete and satisfies the committed contract.
    _validate_document("trade-log.schema.json", trade)
    for field in ("gross_pnl_usd", "fees_usd", "funding_usd", "net_pnl_usd", "mae_usd", "mfe_usd"):
        assert isinstance(trade[field], str)
    assert isinstance(trade["r_multiple"], float)
    assert isinstance(trade["return_pct"], float)
    assert Decimal(trade["mae_usd"]) <= 0
    assert Decimal(trade["mfe_usd"]) >= 0
    assert Decimal(trade["net_pnl_usd"]) == (
        Decimal(trade["gross_pnl_usd"]) - Decimal(trade["fees_usd"]) + Decimal(trade["funding_usd"])
    ).quantize(Decimal("0.00000001"))
    assert trade["outcome_tags"]
    assert trade["data_quality"]["status"] in {"COMPLETE", "PARTIAL"}
    assert set(trade["decision_quality"]) == {"matched_rules", "confidence", "notes"}
    assert set(trade["provenance"]) == {
        "calc_metric_version",
        "prompt_version",
        "llm_provider",
        "llm_model",
    }
    assert trade["signal"]["trigger_id"] == position["source_trigger_id"]

    history = _json(runner.invoke(cli_app, ["pos", "history", position_id, "--json"]))
    _validate_envelope(history)
    assert history["data"]["position_id"] == position_id
    assert [event["action"] for event in history["data"]["events"]] == ["FULL_CLOSE"]
    assert history["data"]["funding"] == []
    pnl = history["data"]["pnl"]
    for field in (
        "net_pnl_usd",
        "gross_pnl_usd",
        "fees_usd",
        "funding_usd",
        "r_multiple",
        "return_pct",
        "mae_usd",
        "mfe_usd",
        "outcome_tags",
        "data_quality",
    ):
        assert pnl[field] == trade[field], field
    api_history = client._request("GET", f"/api/v1/positions/{position_id}/history")
    _validate_envelope(api_history)
    assert history["data"] == api_history["data"]

    rendered = runner.invoke(cli_app, ["pos", "history", position_id])
    assert rendered.exit_code == 0, rendered.output
    for label in (
        "TIMELINE",
        "FULL_CLOSE",
        "FUNDING",
        "gross",
        "fees",
        "net",
        "r_multiple",
        "mae",
        "mfe",
    ):
        assert label in rendered.stdout, label

    log_rendered = runner.invoke(cli_app, ["log", "show", trade_id])
    assert log_rendered.exit_code == 0, log_rendered.output
    for label in (
        "gross",
        "fees",
        "funding",
        "net",
        "r_multiple",
        "mae",
        "mfe",
        "return",
        "outcome",
        "data_quality",
        "provenance",
    ):
        assert label in log_rendered.stdout, label


def test_a_partial_close_then_full_close_writes_exactly_one_journal_row(
    live_daemon: Settings,
) -> None:
    runner = CliRunner()
    client = ApiClient.from_settings(live_daemon)
    position = _open_position(live_daemon)
    position_id = position["position_id"]
    original_size = Decimal(position["size"])
    half = original_size / Decimal("2")

    partial = _submit_partial_close(
        live_daemon, position, quantity=half, idempotency_key="e2e-partial"
    )
    assert partial.position.state == "OPEN"
    assert partial.position.size == original_size - half

    shown = _json(runner.invoke(cli_app, ["pos", "show", position_id, "--json"]))
    _validate_envelope(shown)
    assert shown["data"]["state"] == "OPEN"
    assert Decimal(shown["data"]["size"]) == original_size - half
    assert shown["data"]["closed_at"] is None
    assert shown["data"]["exit_price"] is None
    assert Decimal(shown["data"]["realized_pnl_usd"]) != 0
    assert _row_counts(live_daemon) == {
        "positions": 1,
        "orders": 2,
        "fills": 2,
        "trade_logs": 0,
    }
    journal_before = _json(runner.invoke(cli_app, ["log", "list", "--json"]))
    _validate_envelope(journal_before)
    assert journal_before["data"]["items"] == []

    open_history = _json(runner.invoke(cli_app, ["pos", "history", position_id, "--json"]))
    _validate_envelope(open_history)
    assert [event["action"] for event in open_history["data"]["events"]] == ["PARTIAL_CLOSE"]
    assert open_history["data"]["state"] == "OPEN"
    assert open_history["data"]["pnl"]["net_pnl_usd"] is None
    assert open_history["data"]["pnl"]["r_multiple"] is None
    assert open_history["data"]["funding"] == []

    _touch_stop(live_daemon, position)
    monitored = _json(runner.invoke(cli_app, ["pos", "monitor", position_id, "--json"]))
    _validate_envelope(monitored)
    assert monitored["data"]["status"] == "CLOSED"
    assert monitored["data"]["action"] == "FULL_CLOSE"
    trade = monitored["data"]["trade_log"]
    _validate_document("trade-log.schema.json", trade)

    # Exactly one position row, one journal row, and one order/fill per slice.
    assert _row_counts(live_daemon) == {
        "positions": 1,
        "orders": 3,
        "fills": 3,
        "trade_logs": 1,
    }
    journal = _json(runner.invoke(cli_app, ["log", "list", "--json"]))
    _validate_envelope(journal)
    assert [item["trade_id"] for item in journal["data"]["items"]] == [trade["trade_id"]]

    # The journal's gross is the sum of every realized slice, and its fees are
    # the sum of every fill fee (entry plus both exits).
    grouped = _fills_by_purpose(live_daemon)
    entry_fill = grouped["ENTRY"][0]
    exits = grouped["EXIT"]
    assert len(entry_fill["fill_id"]) > 0 and len(exits) == 2
    entry_avg = Decimal(position["avg_entry_price"])
    long_side = position["side"] == "LONG"
    expected_gross = sum((_slice_gross(fill, entry_avg, long_side) for fill in exits), Decimal("0"))
    expected_fees = sum(
        (Decimal(str(fill["fee"])) for fill in grouped["ENTRY"] + exits), Decimal("0")
    ).quantize(Decimal("0.00000001"))
    assert Decimal(trade["gross_pnl_usd"]) == expected_gross
    assert Decimal(trade["fees_usd"]) == expected_fees
    assert Decimal(trade["funding_usd"]) == Decimal("0")
    assert Decimal(trade["net_pnl_usd"]) == (expected_gross - expected_fees).quantize(
        Decimal("0.00000001")
    )

    history = _json(runner.invoke(cli_app, ["pos", "history", position_id, "--json"]))
    _validate_envelope(history)
    assert [event["action"] for event in history["data"]["events"]] == [
        "PARTIAL_CLOSE",
        "FULL_CLOSE",
    ]
    pnl = history["data"]["pnl"]
    assert pnl["gross_pnl_usd"] == trade["gross_pnl_usd"]
    assert pnl["fees_usd"] == trade["fees_usd"]
    assert pnl["net_pnl_usd"] == trade["net_pnl_usd"]

    cli_trade = _json(runner.invoke(cli_app, ["log", "show", trade["trade_id"], "--json"]))
    _validate_envelope(cli_trade)
    assert cli_trade["data"] == client.get_trade_log(trade["trade_id"])["data"]
    _validate_document("trade-log.schema.json", cli_trade["data"])


def test_funding_accrues_once_and_is_not_double_applied_across_a_restart(
    restartable_daemon: _RestartableDaemon,
) -> None:
    runner = CliRunner()
    settings = restartable_daemon.settings
    position = _open_position(settings)
    position_id = position["position_id"]
    opened_at = position["opened_at"]
    period, metric_version = _bars_series(settings)
    settlement_at = datetime.now(UTC)
    settlement = utc_timestamp(settlement_at)
    assert settlement > opened_at
    entry = Decimal(position["avg_entry_price"])
    rate = Decimal("0.0001")
    _seed_bar(
        settings,
        close=entry,
        bar_end=settlement_at,
        period=period,
        metric_version=metric_version,
        run_id="e2e-funding-bar",
    )
    _inject_funding_history(
        settings,
        _source_report_id(settings, position),
        [{"ts": settlement, "rate": "0.0001", "interval_hours": 8}],
    )
    expected_amount = (Decimal("-1") * rate * entry * Decimal(position["size"])).quantize(
        Decimal("0.00000001")
    )

    first = _json(runner.invoke(cli_app, ["pos", "monitor", position_id, "--json"]))
    _validate_envelope(first)
    assert first["data"]["status"] == "NO_ACTION"
    assert first["data"]["reason_code"] == "STOP_NOT_TOUCHED"
    assert _row_counts(settings)["trade_logs"] == 0

    history = _json(runner.invoke(cli_app, ["pos", "history", position_id, "--json"]))
    _validate_envelope(history)
    assert history["data"]["state"] == "OPEN"
    assert [event["action"] for event in history["data"]["events"]] == ["FUNDING"]
    accruals = history["data"]["funding"]
    assert len(accruals) == 1
    assert accruals[0]["settlement_time"] == settlement
    assert Decimal(accruals[0]["amount_usd"]) == expected_amount
    assert Decimal(accruals[0]["mark_price"]) == entry
    assert Decimal(history["data"]["pnl"]["funding_usd"]) == expected_amount
    assert history["data"]["pnl"]["net_pnl_usd"] is None
    balance_after = _balance(settings)

    # Stop and start the daemon over the same data dir: the unique
    # ``(position_id, settlement_time)`` key must make the replay a no-op.
    restartable_daemon.restart()
    set_position_mark(settings, position_id, str(entry), as_of=_now())

    second = _json(runner.invoke(cli_app, ["pos", "monitor", position_id, "--json"]))
    _validate_envelope(second)
    assert second["data"]["status"] == "NO_ACTION"
    assert second["data"]["reason_code"] == "STOP_NOT_TOUCHED"

    replayed = _json(runner.invoke(cli_app, ["pos", "history", position_id, "--json"]))
    _validate_envelope(replayed)
    assert len(replayed["data"]["funding"]) == 1
    assert replayed["data"]["funding"][0] == accruals[0]
    assert Decimal(replayed["data"]["pnl"]["funding_usd"]) == expected_amount
    assert _balance(settings) == balance_after
    assert _row_counts(settings)["trade_logs"] == 0


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


def test_a_mark_inside_the_stale_boundary_is_fresh(live_daemon: Settings) -> None:
    runner = CliRunner()
    position = _open_position(live_daemon)
    ttl = int(live_daemon.risk_stale_mark_seconds)
    fresh_at = utc_timestamp(datetime.now(UTC) - timedelta(seconds=ttl - 2))
    set_position_mark(
        live_daemon,
        position["position_id"],
        _mark_away_from_stop(position),
        as_of=fresh_at,
    )

    result = _json(runner.invoke(cli_app, ["pos", "monitor", position["position_id"], "--json"]))

    _validate_envelope(result)
    assert result["data"]["reason_code"] == "STOP_NOT_TOUCHED"


def test_a_mark_one_second_past_the_stale_boundary_is_stale(live_daemon: Settings) -> None:
    runner = CliRunner()
    position = _open_position(live_daemon)
    ttl = int(live_daemon.risk_stale_mark_seconds)
    stale_at = utc_timestamp(datetime.now(UTC) - timedelta(seconds=ttl + 1))
    set_position_mark(
        live_daemon,
        position["position_id"],
        position["stop_price"],
        as_of=stale_at,
    )

    result = _json(runner.invoke(cli_app, ["pos", "monitor", position["position_id"], "--json"]))

    _validate_envelope(result)
    assert result["data"]["reason_code"] == "DATA_STALE"


def test_an_unparseable_mark_timestamp_fails_closed(live_daemon: Settings) -> None:
    runner = CliRunner()
    position = _open_position(live_daemon)
    # Publish no usable bars so the stored mark is the chosen observation and
    # the unparseable as-of is decided by ``_is_stale``'s parse guard.
    store = SqliteStore(live_daemon)
    for entry in store.list_manifest():
        if entry.data_type == "bars":
            store.delete_manifest_entry(entry.path)
    set_position_mark(
        live_daemon,
        position["position_id"],
        position["stop_price"],
        as_of="not-a-timestamp",
    )

    result = _json(runner.invoke(cli_app, ["pos", "monitor", position["position_id"], "--json"]))

    _validate_envelope(result)
    assert result["data"]["status"] == "NO_ACTION"
    assert result["data"]["reason_code"] == "DATA_STALE"
    assert "older than" in result["data"]["reason"]
    assert _row_counts(live_daemon) == {
        "positions": 1,
        "orders": 1,
        "fills": 1,
        "trade_logs": 0,
    }


def test_list_offsets_are_bounded(live_daemon: Settings) -> None:
    client = ApiClient.from_settings(live_daemon)

    with pytest.raises(ApiClientError) as positions:
        client._request("GET", "/api/v1/positions?offset=1000001")
    assert positions.value.code == "VALIDATION_FAILED"
    with pytest.raises(ApiClientError) as journal:
        client._request("GET", "/api/v1/journal?offset=1000001")
    assert journal.value.code == "VALIDATION_FAILED"


def test_a_corrupt_position_row_does_not_break_the_read_routes(live_daemon: Settings) -> None:
    client = ApiClient.from_settings(live_daemon)
    position = _open_position(live_daemon)
    database = live_daemon.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ?",
            ("not json", position["position_id"]),
        )
        connection.commit()

    listed = client._request("GET", "/api/v1/positions")
    _validate_envelope(listed)
    assert listed["data"]["items"] == []
    with pytest.raises(ApiClientError) as excinfo:
        client.get_position(position["position_id"])
    assert excinfo.value.code == "DATA_UNAVAILABLE"


def test_a_corrupt_trade_log_does_not_break_the_read_routes(live_daemon: Settings) -> None:
    client = ApiClient.from_settings(live_daemon)
    position = _open_position(live_daemon)
    _touch_stop(live_daemon, position)
    monitored = client.monitor_position(position["position_id"])
    trade_id = monitored["data"]["trade_log"]["trade_id"]
    database = live_daemon.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
            ("not json", trade_id),
        )
        connection.commit()

    listed = client._request("GET", "/api/v1/journal")
    _validate_envelope(listed)
    assert listed["data"]["items"] == []
    with pytest.raises(ApiClientError) as excinfo:
        client.get_trade_log(trade_id)
    assert excinfo.value.code == "DATA_UNAVAILABLE"
