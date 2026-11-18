"""Shared setup for the fake-transport Telegram channel journey.

Nothing here starts a process or opens a socket: ``create_app`` builds the same
composition root the daemon uses, and ``OFT_ENV=test`` selects
:class:`~oft.adapters.telegram_fake.TelegramFake`. The helpers seed the SQLite
read models from the committed fixtures so every command renders real data.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from oft.adapters.sqlite_store import SqliteStore
from oft.application.paper_account import PaperAccountService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.symbols import SymbolContract
from oft.ports.store import ReportRecord, ScanRecord

ROOT = Path(__file__).resolve().parents[2]
TELEGRAM_FIXTURES = ROOT / "tests" / "fixtures" / "telegram"
NOW = "2026-11-17T09:30:00.000Z"
OLDER = "2026-11-17T08:30:00.000Z"
SENTINEL_TOKEN = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"


def load_updates() -> dict[str, Any]:
    return json.loads((TELEGRAM_FIXTURES / "updates.json").read_text(encoding="utf-8"))


def load_read_model(name: str) -> dict[str, Any]:
    path = TELEGRAM_FIXTURES / "read_models" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class Channel:
    """One in-process daemon and its real Telegram service."""

    settings: Settings
    app: Any
    client: TestClient
    auth: dict[str, str]

    @property
    def container(self) -> Any:
        return self.app.state.container

    @property
    def store(self) -> SqliteStore:
        return self.container.store

    @property
    def port(self) -> Any:
        return self.container.telegram_port

    def configure(
        self,
        *,
        chat_ids: list[int] | None = None,
        user_ids: list[int] | None = None,
        token: str = SENTINEL_TOKEN,
    ) -> None:
        """Store the token and both allowlists through the real API routes."""
        token_response = self.client.put(
            "/api/v1/settings/telegram/token",
            json={"bot_token": token},
            headers={**self.auth, "Idempotency-Key": f"channel-token-{uuid.uuid4().hex}"},
        )
        assert token_response.status_code == 200, token_response.text
        allowlist_response = self.client.put(
            "/api/v1/settings/telegram/allowlist",
            json={
                "chat_ids": [1001] if chat_ids is None else chat_ids,
                "allowed_user_ids": [2002] if user_ids is None else user_ids,
            },
            headers={**self.auth, "Idempotency-Key": f"channel-allowlist-{uuid.uuid4().hex}"},
        )
        assert allowlist_response.status_code == 200, allowlist_response.text

    def close(self) -> None:
        self.client.close()


def build_channel(settings: Settings, clock: Any) -> Channel:
    from oft.api.app import create_app

    app = create_app(settings, clock=clock)
    client = TestClient(app)
    return Channel(
        settings=settings,
        app=app,
        client=client,
        auth={"Authorization": f"Bearer {app.state.api_token}"},
    )


def seed_read_models(channel: Channel) -> None:
    """Seed one position, opportunity, report, and watchlist pair."""
    settings = channel.settings
    store = channel.store
    _seed_scan(store)
    _seed_position(settings, store)
    _seed_opportunity(settings)
    _seed_report(store)
    _seed_watchlist(store)


def _seed_scan(store: SqliteStore) -> None:
    store.save_scan(
        ScanRecord(
            scan_id="SCAN-TG-1",
            symbol="BTCUSDT",
            period="4h",
            status="DONE",
            trigger_source="MANUAL",
            metrics_only=False,
            fixture_path="",
            started_at=NOW,
            finished_at=NOW,
            window_start=NOW,
            window_end=NOW,
            trade_count=0,
            buy_volume="0",
            sell_volume="0",
            delta="0",
            cvd="0",
            data_quality="COMPLETE",
            manifest_path="",
            parquet_rows=0,
            created_at=NOW,
            report_id="RPT-TG-1",
        )
    )


def _seed_position(settings: Settings, store: SqliteStore) -> None:
    payload = load_read_model("position")
    PaperAccountService(settings=settings, store=store, clock=FixedClock(_now())).ensure()
    with sqlite3.connect(settings.data_dir / "oft.db") as connection:
        connection.execute(
            "INSERT OR REPLACE INTO positions "
            "(position_id, account_id, environment, symbol, state, opened_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                payload["position_id"],
                payload["account_id"],
                payload["environment"],
                payload["symbol"],
                payload["state"],
                payload["opened_at"],
                json.dumps(payload, sort_keys=True),
            ),
        )
        connection.commit()


def _seed_opportunity(settings: Settings) -> None:
    payload = load_read_model("opportunity")
    with sqlite3.connect(settings.data_dir / "oft.db") as connection:
        connection.execute(
            "INSERT OR REPLACE INTO opportunities "
            "(opportunity_id, symbol, state, created_at, updated_at, payload_json, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                payload["opportunity_id"],
                payload["symbol"],
                payload["state"],
                payload["created_at"],
                payload["updated_at"],
                json.dumps(payload, sort_keys=True),
                payload["expires_at"],
            ),
        )
        connection.commit()


def _seed_report(store: SqliteStore) -> None:
    payload = load_read_model("report")
    store.save_report(
        ReportRecord(
            report_id=payload["report_id"],
            scan_id=payload["scan_id"],
            symbol=payload["symbol"],
            timeframe=payload["timeframe"],
            generated_at=payload["generated_at"],
            metrics_manifest_json="[]",
            llm_decision_id="DEC-TG-1",
            narrative_ref=payload["narrative_ref"],
            data_quality=payload["data_quality"],
            snapshot_json=json.dumps(payload["snapshot"]),
            metrics_json=json.dumps(payload["metrics"]),
            llm_json=json.dumps(payload["llm"]),
        )
    )


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


def _seed_watchlist(store: SqliteStore) -> None:
    store.upsert_symbols(
        (_contract("BTCUSDT"), _contract("XAUUSDT", tradifi=True)),
        observed_at=NOW,
    )
    for symbol in ("BTCUSDT", "XAUUSDT"):
        store.set_starred(symbol, True, starred_at=NOW)


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime(2026, 11, 17, 9, 30, tzinfo=UTC)


__all__ = [
    "Channel",
    "NOW",
    "OLDER",
    "SENTINEL_TOKEN",
    "TELEGRAM_FIXTURES",
    "build_channel",
    "load_read_model",
    "load_updates",
    "seed_read_models",
]
