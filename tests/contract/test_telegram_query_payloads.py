"""Contract: Telegram renders carry the same values as the API read models."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.application import telegram_queries
from oft.application.paper_account import PaperAccountService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.symbols import SymbolContract
from oft.ports.store import ReportRecord, ScanRecord

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
NOW_TEXT = "2026-11-17T09:30:00.000Z"
OLDER_TEXT = "2026-11-17T08:30:00.000Z"


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _num(value: object) -> str:
    parsed = Decimal(str(value))
    if parsed == 0:
        return "0"
    return format(parsed.normalize(), "f")


def _contract(symbol: str, *, tradifi: bool = False) -> SymbolContract:
    return SymbolContract(
        symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        contract_type="TRADIFI_PERPETUAL" if tradifi else "PERPETUAL",
        underlying_type=None,
        status="TRADING",
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
        price_precision=2,
        quantity_precision=3,
        onboard_date=1_600_000_000_000,
        tradifi=tradifi,
    )


def _position_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "position_id": "POS-1",
        "account_id": "ACC-PAPER",
        "environment": "PAPER",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "state": "OPEN",
        "size": "0.01",
        "entry_price": "70000",
        "avg_entry_price": "70000",
        "mark_price": "71000",
        "notional_usd": "700",
        "unrealized_pnl_usd": None,
        "unrealized_pnl_pct": None,
        "stop_price": "69000",
        "target_prices": [],
        "leverage": 2.0,
        "margin_mode": "ISOLATED",
        "margin_usd": "0",
        "risk_amount_usd": "10",
        "risk_pct": 0.1,
        "fees_usd": "0",
        "funding_usd": "0",
        "realized_pnl_usd": None,
        "opened_at": OLDER_TEXT,
        "updated_at": NOW_TEXT,
        "closed_at": None,
        "exit_price": None,
        "source_trigger_id": None,
        "source_decision_id": None,
        "management_history": [],
        "data_quality": {"status": "COMPLETE", "reasons": [], "gaps": []},
        "version": 1,
    }


def _opportunity_contract(opportunity_id: str, *, state: str, created_at: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "opportunity_id": opportunity_id,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "state": state,
        "created_at": created_at,
        "updated_at": created_at,
        "zone": {"low": "69000", "high": "70000"},
        "entry_preference": "RETEST",
        "trigger_conditions": [],
        "invalidation_conditions": [],
        "expires_at": "2026-11-18T00:00:00.000Z",
        "thesis": "Range high retest",
        "confidence": 0.72,
        "risk_request": {
            "entry_price": "70000",
            "stop_price": "69000",
            "quantity": None,
            "risk_pct": 0.1,
            "leverage": 2.0,
            "notional_usd": "100",
        },
        "source": {"scan_id": "SCAN-1", "report_id": "RPT-1", "decision_id": "DEC-1"},
        "provenance": {"provider": "stub", "model": "fixture", "prompt_version": 1},
        "data_quality": {"status": "COMPLETE", "reasons": []},
        "state_history": [
            {
                "from_state": None,
                "to_state": state,
                "at": created_at,
                "reason_code": "TRIGGER_MET" if state == "TRIGGERED" else "CREATED",
                "note": "",
            }
        ],
    }


def _seed(settings: Settings) -> None:
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    store = container.store
    PaperAccountService(settings=settings, store=store, clock=container.clock).ensure()
    database = settings.data_dir / "oft.db"
    position = _position_payload()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO positions "
            "(position_id, account_id, environment, symbol, state, opened_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                position["position_id"],
                position["account_id"],
                position["environment"],
                position["symbol"],
                position["state"],
                position["opened_at"],
                json.dumps(position, sort_keys=True),
            ),
        )
        for opportunity_id, state, created_at in (
            ("OPP-OPEN", "OPEN", OLDER_TEXT),
            ("OPP-TRIG", "TRIGGERED", NOW_TEXT),
        ):
            contract = _opportunity_contract(opportunity_id, state=state, created_at=created_at)
            connection.execute(
                "INSERT INTO opportunities "
                "(opportunity_id, symbol, state, created_at, updated_at, payload_json, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    opportunity_id,
                    contract["symbol"],
                    state,
                    created_at,
                    created_at,
                    json.dumps(contract, sort_keys=True),
                ),
            )
        connection.commit()

    store.save_scan(
        ScanRecord(
            scan_id="SCAN-1",
            symbol="BTCUSDT",
            period="4h",
            status="DONE",
            trigger_source="MANUAL",
            metrics_only=False,
            fixture_path="",
            started_at=NOW_TEXT,
            finished_at=NOW_TEXT,
            window_start=NOW_TEXT,
            window_end=NOW_TEXT,
            trade_count=0,
            buy_volume="0",
            sell_volume="0",
            delta="0",
            cvd="0",
            data_quality="COMPLETE",
            manifest_path="",
            parquet_rows=0,
            created_at=NOW_TEXT,
            report_id="RPT-1",
        )
    )
    metrics = {
        "cvd": {"summary": {"cvd_change": "12.5"}, "points": [{"delta": "3"}]},
        "volume_profile": {"poc": "70000", "vah": "71000", "val": "69000"},
        "open_interest": {"delta_over_window": "1.2", "points": [{}]},
        "funding": {"current_rate": "0.0001", "history": [{}]},
        "data_quality": "PARTIAL",
    }
    store.save_report(
        ReportRecord(
            report_id="RPT-1",
            scan_id="SCAN-1",
            symbol="BTCUSDT",
            timeframe="4h",
            generated_at=NOW_TEXT,
            metrics_manifest_json="[]",
            llm_decision_id="DEC-1",
            narrative_ref="inline:DEC-1",
            data_quality="PARTIAL",
            snapshot_json=json.dumps(
                {"data_quality": "PARTIAL", "quality_reasons": ["liquidation coverage"]}
            ),
            metrics_json=json.dumps(metrics),
            llm_json=json.dumps(
                {
                    "configured": True,
                    "validation_status": "VALID",
                    "narrative": "Range high retest with positive delta.",
                    "opportunity_ids": ["OPP-OPEN"],
                }
            ),
        )
    )
    store.upsert_symbols(
        (_contract("BTCUSDT"), _contract("ETHUSDT"), _contract("XAUUSDT", tradifi=True)),
        observed_at=NOW_TEXT,
    )
    for symbol in ("BTCUSDT", "XAUUSDT"):
        store.set_starred(symbol, True, starred_at=NOW_TEXT)


def _rendered_value(text: str, key: str) -> str:
    prefix = f"{key}: "
    line = next(line for line in text.splitlines() if line.startswith(prefix))
    return line.removeprefix(prefix).removesuffix(" USDT")


def test_positions_render_parity_with_the_api(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _seed(settings)
    container = build_container(settings, clock=FixedClock(NOW))
    text = telegram_queries.render_positions_query(
        store=container.store,
        clock=container.clock,
        settings=container.settings,
        reference_id="TG-00000001",
    )
    app = create_app(settings, clock=FixedClock(NOW))
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/positions?state=OPEN",
            headers={"Authorization": f"Bearer {app.state.api_token}"},
        )
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]

    assert len(items) == 1
    item = items[0]
    assert _rendered_value(text, "size") == _num(item["size"])
    assert _rendered_value(text, "entry") == _num(item["avg_entry_price"])
    assert _rendered_value(text, "mark") == _num(item["resolved_mark_price"])
    assert _rendered_value(text, "stop") == _num(item["stop_price"])
    assert _rendered_value(text, "upnl") == "10"


def test_opportunities_render_parity_with_the_api(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _seed(settings)
    container = build_container(settings, clock=FixedClock(NOW))
    text = telegram_queries.render_opportunities_query(
        store=container.store,
        clock=container.clock,
        reference_id="TG-00000002",
    )
    app = create_app(settings, clock=FixedClock(NOW))
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/opportunities",
            headers={"Authorization": f"Bearer {app.state.api_token}"},
        )
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]

    for item in items:
        assert f"opportunity: {item['opportunity_id']}" in text
        assert f"state: {item['state']}" in text
    assert text.index("OPP-TRIG") < text.index("OPP-OPEN")


def test_report_render_parity_with_the_api(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _seed(settings)
    container = build_container(settings, clock=FixedClock(NOW))
    text = telegram_queries.render_report_query(
        store=container.store, symbol="BTCUSDT", reference_id="TG-00000003"
    )
    app = create_app(settings, clock=FixedClock(NOW))
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/reports/RPT-1",
            headers={"Authorization": f"Bearer {app.state.api_token}"},
        )
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    assert f"report_id: {data['report_id']}" in text
    assert f"symbol: {data['symbol']}" in text
    assert f"data_quality: {data['data_quality']}" in text
    assert "quality_reasons: liquidation coverage" in text
    metrics = data["metrics"]
    assert f"cvd_change: {metrics['cvd']['summary']['cvd_change']}" in text
    assert f"poc: {metrics['volume_profile']['poc']}" in text
    assert f"open_interest_delta: {metrics['open_interest']['delta_over_window']}" in text
    assert f"funding_rate: {metrics['funding']['current_rate']}" in text
    assert data["llm"]["narrative"] in text


def test_budget_render_parity_with_the_api(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _seed(settings)
    container = build_container(settings, clock=FixedClock(NOW))
    text = telegram_queries.render_budget_query(
        store=container.store,
        clock=container.clock,
        settings=container.settings,
        reference_id="TG-00000004",
    )
    app = create_app(settings, clock=FixedClock(NOW))
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/budget",
            headers={"Authorization": f"Bearer {app.state.api_token}"},
        )
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    thresholds = data["thresholds"]
    assert _rendered_value(text, "daily_usd") == _num(thresholds["daily_usd"])
    assert _rendered_value(text, "weekly_usd") == _num(thresholds["weekly_usd"])
    assert _rendered_value(text, "monthly_usd") == _num(thresholds["monthly_usd"])
    for period in ("day", "week", "month"):
        assert Decimal(_rendered_value(text, "spend_usd")) == Decimal(
            data["periods"][period]["spend_usd"]
        )


def test_watchlist_render_parity_with_the_api(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _seed(settings)
    container = build_container(settings, clock=FixedClock(NOW))
    text = telegram_queries.render_watchlist_query(
        store=container.store,
        clock=container.clock,
        reference_id="TG-00000005",
    )
    app = create_app(settings, clock=FixedClock(NOW))
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/watchlist",
            headers={"Authorization": f"Bearer {app.state.api_token}"},
        )
    assert response.status_code == 200, response.text
    rows = response.json()["data"]["rows"]
    starred = {row["symbol"] for row in rows if row["starred"]}
    rendered: set[str] = set()
    for line in text.splitlines():
        if line.startswith("symbols: "):
            rendered.update(line.removeprefix("symbols: ").split(", "))

    assert rendered == starred
