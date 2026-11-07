"""E2E journey: stub scan -> CALC/LLM report -> trigger -> paper entry.

One symbol, one fixture archive, and the deterministic ``stub`` LLM provider are
driven through the real ``oft`` CLI against a real loopback daemon. The order,
fill, and position are then read back independently over HTTP and must match the
CLI values exactly.

The test is offline and clock-independent: market data comes from the committed
archive fixtures, the LLM is the fixture-backed stub, and no value asserted here
depends on the current wall clock. No exchange, provider, or live credential is
touched (PAPER only).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from typer.testing import CliRunner

from oft.api.app import create_app
from oft.bootstrap import build_container
from oft.cli.main import app as cli_app
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
SYMBOL = "BTCUSDT"
PERIOD = "4h"


@dataclass(frozen=True)
class _Daemon:
    settings: Settings
    base_url: str
    api_token: str


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed_archive(settings: Settings) -> None:
    container = build_container(settings, initialize=True)
    result = container.backfill.run(symbol=SYMBOL, data_type="aggTrades", start=START, end=END)
    assert result.run.status == "COMPLETE"


@pytest.fixture()
def paper_loop_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Daemon]:
    """Run the real daemon and point the CLI at it via the OFT_* env contract."""
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
    _seed_archive(settings)

    api = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(
            api,
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
        yield _Daemon(
            settings=settings,
            base_url=f"http://127.0.0.1:{port}",
            api_token=api.state.api_token,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _json(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_stub_scan_to_paper_entry_is_identical_across_cli_and_api(
    paper_loop_daemon: _Daemon,
) -> None:
    runner = CliRunner()
    auth = {"Authorization": f"Bearer {paper_loop_daemon.api_token}"}

    with httpx.Client(
        base_url=paper_loop_daemon.base_url,
        headers=auth,
        timeout=10.0,
        trust_env=False,
    ) as http:
        # 1. One-symbol stub scan through the CLI.
        scan = _json(
            runner.invoke(
                cli_app,
                [
                    "scan",
                    "run",
                    "--symbol",
                    SYMBOL,
                    "--period",
                    PERIOD,
                    "--source",
                    "backfill",
                    "--provider",
                    "stub",
                    "--idempotency-key",
                    "e2e-scan-1",
                    "--json",
                ],
            )
        )
        assert scan["ok"] is True
        report_id = scan["data"]["report_id"]
        llm = scan["data"]["report"]["llm"]
        assert llm["configured"] is True
        assert llm["validation_status"] in {"VALID", "REPAIRED"}
        opportunity_ids = llm["opportunity_ids"]
        assert len(opportunity_ids) == 1
        opportunity_id = opportunity_ids[0]

        # 2. The report carries CALC metrics and the LLM band + opportunity.
        report_text = runner.invoke(cli_app, ["report", "show", report_id])
        assert report_text.exit_code == 0, report_text.output
        assert report_text.output.index("CALC") < report_text.output.index("LLM ›")
        assert opportunity_id in report_text.output

        api_report = http.get(f"/api/v1/reports/{report_id}").json()["data"]
        assert api_report["report_id"] == report_id
        assert api_report["symbol"] == SYMBOL
        assert api_report["metrics"]["cvd"]["points"]
        assert api_report["metrics"]["volume_profile"]["bins"]
        assert api_report["llm"]["configured"] is True
        assert api_report["llm"]["opportunity_ids"] == opportunity_ids

        cli_opps = _json(runner.invoke(cli_app, ["opp", "list", "--state", "OPEN", "--json"]))
        assert [item["opportunity_id"] for item in cli_opps["data"]["items"]] == (opportunity_ids)
        api_opps = http.get("/api/v1/opportunities", params={"state": "open"}).json()["data"][
            "items"
        ]
        assert [item["opportunity_id"] for item in api_opps] == opportunity_ids

        cli_opp = _json(runner.invoke(cli_app, ["opp", "show", opportunity_id, "--json"]))
        api_opp = http.get(f"/api/v1/opportunities/{opportunity_id}").json()["data"]
        assert cli_opp["data"] == api_opp
        assert api_opp["source"]["report_id"] == report_id
        assert api_opp["trigger_conditions"][0]["condition_id"] == "C-1"

        # 3. `oft trigger evaluate` decides the trigger in code.
        evaluation = _json(
            runner.invoke(cli_app, ["trigger", "evaluate", opportunity_id, "--json"])
        )
        assert evaluation["data"]["triggered"] is True
        trigger = evaluation["data"]["trigger"]
        trigger_id = trigger["trigger_id"]
        assert trigger["trigger_source"] == "CODE"
        assert trigger["condition_results"][0]["met"] is True
        assert trigger["snapshot"]["snapshot_hash"]
        assert trigger["idempotency_key"]
        assert (
            trigger["snapshot"]["snapshot_hash"]
            == (evaluation["data"]["evaluation"]["snapshot_hash"])
        )

        api_trigger = http.get(f"/api/v1/triggers/{trigger_id}").json()["data"]
        assert api_trigger == trigger

        # 4. `oft trigger accept` creates exactly one paper order/fill/position.
        accept_key = "e2e-accept-1"
        accepted = _json(
            runner.invoke(
                cli_app,
                [
                    "trigger",
                    "accept",
                    trigger_id,
                    "--idempotency-key",
                    accept_key,
                    "--json",
                ],
            )
        )
        assert accepted["meta"]["idempotent_replay"] is False
        entry = accepted["data"]
        order = entry["order"]
        fill = entry["fill"]
        position = entry["position"]
        assert entry["status"] == "ACCEPTED"
        assert order["status"] == "FILLED"
        assert fill["source"] == "PAPER"
        assert fill["order_id"] == order["order_id"]
        assert position["state"] == "OPEN"
        assert position["symbol"] == SYMBOL

        # The API returns byte-for-byte the same order/fill/position on replay.
        replay = http.post(
            f"/api/v1/triggers/{trigger_id}/accept",
            headers={"Idempotency-Key": accept_key},
        )
        assert replay.status_code == 200, replay.text
        replayed = replay.json()["data"]
        assert replay.json()["meta"]["idempotent_replay"] is True
        assert replayed["order"] == order
        assert replayed["fill"] == fill
        assert replayed["position"] == position
        assert replayed["account"] == entry["account"]

        # 5. Read models agree exactly between CLI and API, with one row each.
        cli_orders = _json(runner.invoke(cli_app, ["paper", "orders", "--json"]))
        api_orders = http.get("/api/v1/paper/orders").json()
        assert cli_orders["data"] == api_orders["data"]
        assert [item["order_id"] for item in api_orders["data"]["items"]] == [order["order_id"]]

        cli_fills = _json(runner.invoke(cli_app, ["paper", "fills", "--json"]))
        api_fills = http.get("/api/v1/paper/fills").json()
        assert cli_fills["data"] == api_fills["data"]
        assert [item["fill_id"] for item in api_fills["data"]["items"]] == [fill["fill_id"]]

        cli_balance = _json(runner.invoke(cli_app, ["paper", "balance", "--json"]))
        api_balance = http.get("/api/v1/paper/balance").json()
        assert cli_balance["data"] == api_balance["data"]
        assert api_balance["data"]["balance"] != api_balance["data"]["initial_balance"]

        cli_equity = _json(runner.invoke(cli_app, ["paper", "equity", "--json"]))
        api_equity = http.get("/api/v1/paper/equity").json()
        assert cli_equity["data"] == api_equity["data"]
        assert api_equity["data"]["open_positions"] == 1

        # The human-readable CLI surface shows the same position identity.
        rendered = runner.invoke(
            cli_app,
            [
                "trigger",
                "accept",
                trigger_id,
                "--idempotency-key",
                accept_key,
            ],
        )
        assert rendered.exit_code == 0, rendered.output
        assert order["order_id"] in rendered.output
        assert position["position_id"] in rendered.output
        assert "OPEN" in rendered.output
