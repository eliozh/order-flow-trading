"""CLI parity: scan -> report -> opp -> trigger -> accept -> paper -> kill."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner

from oft.api.app import create_app
from oft.bootstrap import build_container
from oft.cli.api_client import ApiClient
from oft.cli.main import app as cli_app
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def llm_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
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
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"

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


def _json(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_cli_paper_loop_matches_the_api(llm_daemon: Settings) -> None:
    runner = CliRunner()

    scan = _json(
        runner.invoke(
            cli_app,
            [
                "scan",
                "run",
                "--symbol",
                "BTCUSDT",
                "--period",
                "4h",
                "--source",
                "backfill",
                "--provider",
                "stub",
                "--json",
            ],
        )
    )
    assert scan["ok"] is True
    report_id = scan["data"]["report_id"]
    assert scan["data"]["report"]["llm"]["configured"] is True
    opportunity_id = scan["data"]["report"]["llm"]["opportunity_ids"][0]

    report_text = runner.invoke(cli_app, ["report", "show", report_id])
    assert report_text.exit_code == 0, report_text.output
    assert report_text.output.index("CALC") < report_text.output.index("LLM ›")

    opp_list = _json(runner.invoke(cli_app, ["opp", "list", "--state", "OPEN", "--json"]))
    assert [item["opportunity_id"] for item in opp_list["data"]["items"]] == [opportunity_id]

    opp_show = _json(runner.invoke(cli_app, ["opp", "show", opportunity_id, "--json"]))
    assert opp_show["data"]["source"]["report_id"] == report_id

    evaluate = _json(runner.invoke(cli_app, ["trigger", "evaluate", opportunity_id, "--json"]))
    assert evaluate["data"]["triggered"] is True
    trigger_id = evaluate["data"]["trigger"]["trigger_id"]
    assert evaluate["data"]["trigger"]["trigger_source"] == "CODE"

    accepted = _json(
        runner.invoke(
            cli_app,
            [
                "trigger",
                "accept",
                trigger_id,
                "--idempotency-key",
                "cli-accept-1",
                "--json",
            ],
        )
    )
    order_id = accepted["data"]["order"]["order_id"]
    assert accepted["data"]["position"]["state"] == "OPEN"

    replay = _json(
        runner.invoke(
            cli_app,
            [
                "trigger",
                "accept",
                trigger_id,
                "--idempotency-key",
                "cli-accept-1",
                "--json",
            ],
        )
    )
    assert replay["meta"]["idempotent_replay"] is True
    assert replay["data"]["order"]["order_id"] == order_id

    api_order = ApiClient.from_settings(llm_daemon).paper_orders()["data"]["items"][0]
    assert api_order["order_id"] == order_id

    orders = _json(runner.invoke(cli_app, ["paper", "orders", "--json"]))
    fills = _json(runner.invoke(cli_app, ["paper", "fills", "--json"]))
    balance = _json(runner.invoke(cli_app, ["paper", "balance", "--json"]))
    equity = _json(runner.invoke(cli_app, ["paper", "equity", "--json"]))
    assert len(orders["data"]["items"]) == 1
    assert len(fills["data"]["items"]) == 1
    assert balance["data"]["balance"] != "10000.00"
    assert equity["data"]["open_positions"] == 1

    assert _json(runner.invoke(cli_app, ["kill", "on", "--json"]))["data"]["active"] is True
    assert _json(runner.invoke(cli_app, ["kill", "status", "--json"]))["data"]["active"] is True
    wrong = runner.invoke(cli_app, ["kill", "off", "--token", "NOPE", "--json"])
    assert wrong.exit_code != 0
    assert (
        _json(runner.invoke(cli_app, ["kill", "off", "--token", "RESUME", "--json"]))["data"][
            "active"
        ]
        is False
    )

    metrics_only = _json(
        runner.invoke(
            cli_app,
            [
                "scan",
                "run",
                "--symbol",
                "BTCUSDT",
                "--period",
                "4h",
                "--source",
                "backfill",
                "--metrics-only",
                "--json",
            ],
        )
    )
    assert metrics_only["data"]["report"]["llm"]["configured"] is False
