"""E2E: ``oft log stats|compare|export`` and ``oft trigger show --decision``.

A real loopback daemon runs offline against the committed archive fixtures and
the deterministic ``stub`` provider. Every envelope is validated against
``api-envelope.schema.json``; the daemon is always stopped by the fixture.
"""

from __future__ import annotations

import csv
import io
import json
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
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

from oft.api.app import create_app
from oft.cli.api_client import ApiClient
from oft.cli.main import app as cli_app
from oft.config import Settings

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
    return Settings(
        env="test",
        port=_free_port(),
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _seed_archive(settings: Settings) -> None:
    from oft.bootstrap import build_container

    container = build_container(settings, initialize=True)
    assert (
        container.backfill.run(
            symbol="BTCUSDT", data_type="aggTrades", start=START, end=END
        ).run.status
        == "COMPLETE"
    )


def _start_daemon(settings: Settings) -> tuple[uvicorn.Server, threading.Thread]:
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
    settings = _live_settings(tmp_path)
    _seed_archive(settings)
    server, thread = _start_daemon(settings)
    _export_cli_env(monkeypatch, settings)
    try:
        yield settings
    finally:
        _stop_daemon(server, thread)


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


def _pending_trigger(settings: Settings, *, key: str) -> str:
    client = ApiClient.from_settings(settings)
    scan = client.create_scan(
        {
            "symbol": "BTCUSDT",
            "period": "4h",
            "source": "backfill",
            "metrics_only": False,
            "provider": "stub",
        },
        idempotency_key=f"journal-scan-{key}",
    )
    opportunity_id = scan["data"]["report"]["llm"]["opportunity_ids"][0]
    evaluated = client.evaluate_trigger(
        {"opportunity_id": opportunity_id}, idempotency_key=f"journal-trigger-{key}"
    )
    return str(evaluated["data"]["trigger"]["trigger_id"])


def _open_position(settings: Settings, *, key: str) -> dict[str, Any]:
    client = ApiClient.from_settings(settings)
    trigger_id = _pending_trigger(settings, key=key)
    client._request(
        "POST",
        f"/api/v1/triggers/{trigger_id}/recheck",
        {},
        extra_headers={"Idempotency-Key": f"journal-recheck-{key}"},
    )
    accepted = client.accept_trigger(trigger_id, idempotency_key=f"journal-accept-{key}")
    return accepted["data"]["position"]


def _touch_stop(settings: Settings, position: dict[str, Any]) -> None:
    from position_support import set_position_mark

    set_position_mark(
        settings,
        position["position_id"],
        position["stop_price"],
        as_of=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )


def _close_position(settings: Settings, position: dict[str, Any]) -> dict[str, Any]:
    _touch_stop(settings, position)
    envelope = ApiClient.from_settings(settings)._request(
        "POST", f"/api/v1/positions/{position['position_id']}/monitor", {}
    )
    _validate_envelope(envelope)
    assert envelope["data"]["status"] == "CLOSED"
    return envelope["data"]["trade_log"]


def _make_trade_complete(settings: Settings, trade_id: str) -> None:
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM trade_logs WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["data_quality"] = {
            "status": "COMPLETE",
            "reasons": [],
            "gaps": [],
            "last_event_at": payload["closed_at"],
            "coverage_start": payload["opened_at"],
        }
        payload["outcome_tags"] = ["LOSS", "STOPPED"]
        connection.execute(
            "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
            (json.dumps(payload, sort_keys=True), trade_id),
        )
        connection.commit()


def _llm_call_count(settings: Settings) -> int:
    """Rows in the LLM ledger; a provider completion always records one."""
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0])


def _audit_event_types(settings: Settings) -> list[str]:
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        return [
            str(row[0])
            for row in connection.execute("SELECT event_type FROM audit_events ORDER BY seq")
        ]


def _rewrite_trade_signal(settings: Settings, trade_id: str, **updates: str) -> None:
    """Point a closed trade at unavailable evidence without touching money."""
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM trade_logs WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        signal = payload.setdefault("signal", {})
        signal.update(updates)
        connection.execute(
            "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
            (json.dumps(payload, sort_keys=True), trade_id),
        )
        connection.commit()


def _write_trade_symbol(settings: Settings, trade_id: str, symbol: str) -> None:
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM trade_logs WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["symbol"] = symbol
        connection.execute(
            "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
            (json.dumps(payload, sort_keys=True), trade_id),
        )
        connection.commit()


def test_log_stats_is_undefined_never_zero_on_an_empty_journal(
    live_daemon: Settings,
) -> None:
    runner = CliRunner()

    rendered = runner.invoke(cli_app, ["log", "stats", "--period", "8w"])
    assert rendered.exit_code == 0, rendered.output
    for label in ("metrics undefined", "win rate", "expectancy", "avg R", "definitions"):
        assert label in rendered.stdout, label
    assert "8w" in rendered.stdout

    envelope = _json(runner.invoke(cli_app, ["log", "stats", "--period", "8w", "--json"]))
    _validate_envelope(envelope)
    data = envelope["data"]
    assert data["status"] == "UNDEFINED"
    assert data["win_rate"] is None
    assert data["expectancy_usd"] is None
    assert data["avg_r"]["value"] is None
    assert data["totals"] == {
        "gross_pnl_usd": None,
        "fees_usd": None,
        "funding_usd": None,
        "net_pnl_usd": None,
    }
    assert data["definitions"]
    assert envelope["meta"]["data_quality"] == "MISSING"
    # Never a fabricated zero: the metrics are null and the sample is empty.
    assert data["sample_size"] == 0
    assert data["denominator"] == 0
    assert (data["wins"], data["losses"], data["ties"]) == (0, 0, 0)
    assert data["avg_r"] == {"value": None, "samples": 0, "excluded_null_r": 0}
    assert data["exclusions"] == {
        "open_positions": 0,
        "partial_trades": 0,
        "unreadable_trades": 0,
        "outside_period": 0,
    }
    assert set(data["definitions"]) >= {
        "net",
        "win_rate",
        "expectancy",
        "avg_r",
        "eligibility",
        "period",
    }
    assert data["reason"].startswith("metrics undefined")
    assert "excluded open positions 0" in rendered.stdout
    assert "outside period 0" in rendered.stdout
    for name in ("net", "win_rate", "expectancy", "avg_r", "eligibility", "period"):
        assert name in rendered.stdout, name


def test_a_huge_period_is_a_typed_cli_error(live_daemon: Settings) -> None:
    runner = CliRunner()

    stats = runner.invoke(cli_app, ["log", "stats", "--period", "999999d", "--json"])
    assert stats.exit_code != 0
    assert json.loads(stats.stdout)["error"]["code"] == "VALIDATION_FAILED"

    exported = runner.invoke(cli_app, ["log", "export", "--period", "999999999999999m", "--json"])
    assert exported.exit_code != 0
    assert json.loads(exported.stdout)["error"]["code"] == "VALIDATION_FAILED"

    rendered = runner.invoke(cli_app, ["log", "stats", "--period", "999999d"])
    assert rendered.exit_code != 0
    assert "exceeds" in rendered.output


def test_log_stats_compare_and_export_over_a_closed_trade(
    live_daemon: Settings,
) -> None:
    runner = CliRunner()
    position = _open_position(live_daemon, key="stats")
    trade = _close_position(live_daemon, position)
    trade_id = trade["trade_id"]
    _make_trade_complete(live_daemon, trade_id)

    stats = _json(runner.invoke(cli_app, ["log", "stats", "--period", "8w", "--json"]))
    _validate_envelope(stats)
    assert stats["data"]["status"] == "DEFINED"
    assert stats["data"]["sample_size"] == 1
    assert stats["data"]["denominator"] == 1
    assert stats["data"]["totals"]["net_pnl_usd"] == trade["net_pnl_usd"]
    assert stats["data"]["expectancy_usd"] == trade["net_pnl_usd"]
    assert stats["data"]["wins"] + stats["data"]["losses"] + stats["data"]["ties"] == 1
    assert stats["data"]["win_rate"] == stats["data"]["wins"] / stats["data"]["sample_size"]
    assert stats["data"]["avg_r"]["samples"] + stats["data"]["avg_r"]["excluded_null_r"] == 1
    # The only closed position is now CLOSED, so every exclusion counter is zero
    # and the sample denominator is exactly one.
    assert stats["data"]["exclusions"] == {
        "open_positions": 0,
        "partial_trades": 0,
        "unreadable_trades": 0,
        "outside_period": 0,
    }
    rendered_stats = runner.invoke(cli_app, ["log", "stats", "--period", "8w"])
    assert rendered_stats.exit_code == 0, rendered_stats.output
    for label in ("sample size", "denominator", "wins", "expectancy", "definitions"):
        assert label in rendered_stats.stdout, label
    assert "sample size 1" in rendered_stats.stdout
    assert "denominator 1" in rendered_stats.stdout
    for label in (
        "excluded open positions 0",
        "partial trades 0",
        "unreadable 0",
        "outside period 0",
    ):
        assert label in rendered_stats.stdout, label

    comparison = _json(runner.invoke(cli_app, ["log", "compare", trade_id, "--json"]))
    _validate_envelope(comparison)
    data = comparison["data"]
    assert data["trade_id"] == trade_id
    assert data["zone"]["status"] in {"INSIDE", "ABOVE_ZONE", "BELOW_ZONE"}
    # Proposed zone vs the actual fill: the fill price is the trade's entry.
    assert Decimal(data["zone"]["entry"]) == Decimal(trade["entry_avg_price"])
    assert data["zone"]["zone_low"] is not None
    assert data["zone"]["zone_high"] is not None
    assert data["slippage"]["status"] == "OK"
    assert isinstance(data["slippage"]["bps"], float)
    assert data["slippage"]["reference_price"] is not None
    assert "positive = adverse" in data["slippage"]["formula"]
    assert data["first_touch"]["status"] in {"N/A", "OK", "PARTIAL"}
    assert data["decision"]["status"] == "OK"
    assert data["decision"]["decision"] in {"ENTER_LONG", "ENTER_SHORT", "NO_ENTRY"}
    assert data["decision"]["net_pnl_usd"] == trade["net_pnl_usd"]
    assert data["decision"]["matched"] == (
        data["decision"]["direction_agrees"] and Decimal(trade["net_pnl_usd"]) > 0
    )
    rendered_compare = runner.invoke(cli_app, ["log", "compare", trade_id])
    assert rendered_compare.exit_code == 0, rendered_compare.output
    for label in ("zone", "slippage", "first_touch", "decision"):
        assert label in rendered_compare.stdout, label
    assert "bps" in rendered_compare.stdout

    export = _json(runner.invoke(cli_app, ["log", "export", "--format", "csv", "--json"]))
    _validate_envelope(export)
    path = Path(export["data"]["path"])
    assert path.is_file()
    assert export["data"]["rows"] == 1
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("trade_id,position_id")
    assert "system_prompt" not in text
    assert "user_prompt" not in text
    assert "raw_output" not in text
    assert "sk-" not in text
    rows = list(csv.reader(io.StringIO(text)))
    assert len(rows) == 2
    assert all(len(row) == len(rows[0]) for row in rows)

    json_export = _json(runner.invoke(cli_app, ["log", "export", "--format", "json", "--json"]))
    _validate_envelope(json_export)
    json_text = Path(json_export["data"]["path"]).read_text(encoding="utf-8")
    document = json.loads(json_text)
    assert document["schema_version"] == "1.0.0"
    assert [item["trade_id"] for item in document["trades"]] == [trade_id]
    # No secret or raw prompt text leaves the export in either format.
    for forbidden in ("system_prompt", "user_prompt", "raw_output", "sk-"):
        assert forbidden not in json_text, forbidden

    rendered_export = runner.invoke(cli_app, ["log", "export", "--format", "csv"])
    assert rendered_export.exit_code == 0, rendered_export.output
    assert "EXPORTED" in rendered_export.stdout


def test_log_export_neutralizes_a_formula_cell(live_daemon: Settings) -> None:
    runner = CliRunner()
    position = _open_position(live_daemon, key="neutralize")
    trade = _close_position(live_daemon, position)
    trade_id = trade["trade_id"]

    # Every spreadsheet formula prefix is neutralized, not just "=".
    for prefix in ("=", "+", "-", "@"):
        _write_trade_symbol(live_daemon, trade_id, f"{prefix}BTCUSDT")
        export = _json(runner.invoke(cli_app, ["log", "export", "--format", "csv", "--json"]))
        _validate_envelope(export)
        path = Path(export["data"]["path"])
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert f"'{prefix}BTCUSDT" in text, prefix
        assert export["data"]["neutralized"] >= 1
        assert "system_prompt" not in text
        assert "raw_output" not in text

    # JSON is a data format, not a spreadsheet: it must not be rewritten.
    json_export = _json(runner.invoke(cli_app, ["log", "export", "--format", "json", "--json"]))
    _validate_envelope(json_export)
    document = json.loads(Path(json_export["data"]["path"]).read_text(encoding="utf-8"))
    assert document["trades"][0]["symbol"] == "@BTCUSDT"


def test_log_compare_marks_unavailable_sides_na_and_partial(live_daemon: Settings) -> None:
    runner = CliRunner()
    position = _open_position(live_daemon, key="compare-na")
    trade = _close_position(live_daemon, position)
    trade_id = trade["trade_id"]

    # The source opportunity is unavailable: the proposed zone and the
    # trigger-time reference price degrade to N/A while the persisted entry
    # decision and realized outcome are still measurable -> PARTIAL, never
    # a fabricated comparison.
    _rewrite_trade_signal(live_daemon, trade_id, opportunity_id="OPP-MISSING")
    partial = _json(runner.invoke(cli_app, ["log", "compare", trade_id, "--json"]))
    _validate_envelope(partial)
    data = partial["data"]
    assert data["zone"]["status"] == "N/A"
    assert data["zone"]["inside"] is None
    assert data["slippage"]["status"] == "N/A"
    assert data["slippage"]["bps"] is None
    assert data["decision"]["status"] == "OK"
    assert data["data_quality"] == "PARTIAL"
    rendered = runner.invoke(cli_app, ["log", "compare", trade_id])
    assert rendered.exit_code == 0, rendered.output
    assert "N/A" in rendered.stdout
    assert "PARTIAL" in rendered.stdout

    # The source trigger is unavailable too: decision-vs-outcome is N/A with
    # null match fields rather than an inferred result.
    _rewrite_trade_signal(live_daemon, trade_id, trigger_id="TRG-MISSING")
    missing = _json(runner.invoke(cli_app, ["log", "compare", trade_id, "--json"]))
    _validate_envelope(missing)
    decision = missing["data"]["decision"]
    assert decision["status"] == "N/A"
    assert decision["decision"] is None
    assert decision["direction_agrees"] is None
    assert decision["matched"] is None
    assert missing["data"]["data_quality"] in {"MISSING", "PARTIAL"}


def test_trigger_show_decision_and_recheck_refresh(live_daemon: Settings) -> None:
    runner = CliRunner()
    trigger_id = _pending_trigger(live_daemon, key="decision")

    missing = runner.invoke(cli_app, ["trigger", "show", trigger_id, "--decision"])
    assert missing.exit_code == 0, missing.output
    assert "N/A: no persisted entry decision" in missing.stdout
    missing_json = _json(
        runner.invoke(cli_app, ["trigger", "show", trigger_id, "--decision", "--json"])
    )
    _validate_envelope(missing_json)
    assert missing_json["data"]["status"] == "MISSING"
    assert missing_json["data"]["decision"] is None
    assert missing_json["data"]["provenance"] is None
    assert missing_json["data"]["reasons"]
    assert missing_json["meta"]["data_quality"] == "MISSING"

    first = _json(runner.invoke(cli_app, ["trigger", "recheck", trigger_id, "--json"]))
    _validate_envelope(first)
    card = first["data"]
    assert card["refreshed"] is False
    assert card["llm"]["provenance"]["provider"] == "stub"
    assert card["llm"]["provenance"]["prompt_id"] == "entry_recheck"

    calls_after_first = _llm_call_count(live_daemon)
    audits_after_first = _audit_event_types(live_daemon)
    assert calls_after_first >= 1

    shown = runner.invoke(cli_app, ["trigger", "show", trigger_id, "--decision"])
    assert shown.exit_code == 0, shown.output
    for label in ("provenance", "stub", "prompt", "cost", "validation"):
        assert label in shown.stdout, label

    # The same persisted decision, read back over the real loopback API: the
    # provenance is the decision's own, and no raw prompt is exposed.
    shown_json = _json(
        runner.invoke(cli_app, ["trigger", "show", trigger_id, "--decision", "--json"])
    )
    _validate_envelope(shown_json)
    shown_data = shown_json["data"]
    assert shown_data["status"] == "FOUND"
    assert shown_data["entry_decision_id"] == card["decision_id"]
    provenance = shown_data["provenance"]
    assert provenance["provider"] == "stub"
    assert provenance["model"]
    assert provenance["prompt_id"] == "entry_recheck"
    assert provenance["prompt_version"] >= 1
    assert provenance["prompt_hash"]
    assert provenance["cost"]["currency"] == "USD"
    assert provenance["recorded_at"].endswith("Z")
    assert shown_data["validation_status"] == "VALID"
    assert 0.0 <= shown_data["confidence"] <= 1.0
    assert shown_data["decision_type"] == "ENTRY_RECHECK"
    assert shown_data["decision"]["entry_recheck"]["decision"] in {
        "ENTER_LONG",
        "ENTER_SHORT",
        "NO_ENTRY",
    }
    rendered_decision = json.dumps(shown_json)
    # Only hashes/provenance leave the ledger: no raw prompt or raw output key.
    assert "system_prompt" not in rendered_decision
    assert "user_prompt" not in rendered_decision
    assert '"raw_output"' not in rendered_decision
    assert '"raw_output_hash"' in rendered_decision

    reused = _json(runner.invoke(cli_app, ["trigger", "recheck", trigger_id, "--json"]))
    _validate_envelope(reused)
    assert reused["data"]["refreshed"] is False
    assert reused["data"]["decision_id"] == card["decision_id"]
    assert reused["data"]["persisted"] is True
    # The default path is a persisted read: no provider call (no new LLM
    # ledger row) and no re-check audit event is written.
    assert _llm_call_count(live_daemon) == calls_after_first
    assert _audit_event_types(live_daemon) == audits_after_first

    refreshed = _json(
        runner.invoke(cli_app, ["trigger", "recheck", trigger_id, "--refresh", "--json"])
    )
    _validate_envelope(refreshed)
    assert refreshed["data"]["refreshed"] is True
    assert refreshed["data"]["llm"]["provenance"]["provider"] == "stub"
    # Only the explicit refresh reaches the provider and audits the replacement.
    assert _llm_call_count(live_daemon) > calls_after_first
    assert "ENTRY_RECHECK_REFRESHED" in _audit_event_types(live_daemon)
