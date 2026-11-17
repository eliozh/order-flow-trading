"""Integration: read-only Telegram queries answer from the shared read models."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from fakes.telegram import MemorySecretBackend, make_update
from oft.adapters.fixture_market_data import FixtureMarketData
from oft.adapters.sqlite_store import SqliteStore
from oft.adapters.telegram_fake import TelegramFake
from oft.application import telegram_messages, telegram_queries
from oft.application import telegram_service as service_module
from oft.application.paper_account import PaperAccountService
from oft.application.secrets_service import SecretsService
from oft.application.settings_service import SettingsService
from oft.application.telegram_dispatch import OutboxDispatcher
from oft.application.telegram_messages import (
    OPPORTUNITIES_EMPTY_TEXT,
    POSITIONS_EMPTY_TEXT,
    REPORT_EMPTY_TEXT,
    WATCHLIST_EMPTY_TEXT,
)
from oft.application.telegram_poller import TelegramPoller
from oft.application.telegram_service import TelegramService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.symbols import SymbolContract
from oft.domain.telegram import COMMANDS, TelegramUpdate
from oft.ports.store import ReportRecord, ScanRecord

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
NOW_TEXT = "2026-11-17T09:30:00.000Z"
OLDER_TEXT = "2026-11-17T08:30:00.000Z"
STALE_TEXT = "2026-11-17T09:20:00.000Z"
SENTINEL_TOKEN = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _service(
    tmp_path: Path, *, clock: FixedClock | None = None
) -> tuple[TelegramService, SqliteStore, Settings, TelegramFake]:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    active_clock = clock or FixedClock(NOW)
    secrets = SecretsService(
        store=store,
        settings=settings,
        clock=active_clock,
        backend=MemorySecretBackend(),
    )
    settings_service = SettingsService(store=store, clock=active_clock, settings=settings)
    secrets.set_secret("telegram", "bot_token", SENTINEL_TOKEN, actor="test")
    settings_service.update_telegram_allowlist([1001], [2002], actor="test")
    port = TelegramFake()
    service = TelegramService(
        settings=settings,
        store=store,
        clock=active_clock,
        market_data=FixtureMarketData(fixtures_dir=settings.fixtures_dir),
        secrets=secrets,
        settings_service=settings_service,
        port=port,
    )
    return service, store, settings, port


def _ask(
    service: TelegramService,
    text: str,
    *,
    update_id: int = 1,
    chat_id: int = 1001,
    user_id: int = 2002,
) -> str | None:
    update = TelegramUpdate(
        update_id=update_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        message_id=update_id + 100,
    )
    record = service.handle_update(update)
    if record is None:
        return None
    return json.loads(record.payload_json)["text"]


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


def _position_payload(
    *,
    position_id: str = "POS-1",
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    size: str = "0.01",
    entry: str = "70000",
    mark: str | None = "71000",
    updated_at: str = NOW_TEXT,
    stop: str | None = "69000",
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "position_id": position_id,
        "account_id": "ACC-PAPER",
        "environment": "PAPER",
        "symbol": symbol,
        "side": side,
        "state": "OPEN",
        "size": size,
        "entry_price": entry,
        "avg_entry_price": entry,
        "mark_price": mark,
        "notional_usd": str(Decimal(entry) * Decimal(size)),
        "unrealized_pnl_usd": None,
        "unrealized_pnl_pct": None,
        "stop_price": stop,
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
        "updated_at": updated_at,
        "closed_at": None,
        "exit_price": None,
        "source_trigger_id": None,
        "source_decision_id": None,
        "management_history": [],
        "data_quality": {"status": "COMPLETE", "reasons": [], "gaps": []},
        "version": 1,
    }


def _seed_position(settings: Settings, store: SqliteStore, payload: dict[str, Any]) -> None:
    PaperAccountService(settings=settings, store=store, clock=FixedClock(NOW)).ensure()
    with sqlite3.connect(settings.data_dir / "oft.db") as connection:
        connection.execute(
            "INSERT INTO positions "
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


def _opportunity_contract(
    opportunity_id: str,
    *,
    symbol: str = "BTCUSDT",
    state: str = "OPEN",
    created_at: str = NOW_TEXT,
    side: str = "LONG",
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "opportunity_id": opportunity_id,
        "symbol": symbol,
        "side": side,
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


def _seed_opportunity(
    settings: Settings, opportunity_id: str, *, state: str, created_at: str
) -> None:
    contract = _opportunity_contract(opportunity_id, state=state, created_at=created_at)
    with sqlite3.connect(settings.data_dir / "oft.db") as connection:
        connection.execute(
            "INSERT INTO opportunities "
            "(opportunity_id, symbol, state, created_at, updated_at, payload_json, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
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


def _scan_record(scan_id: str = "SCAN-1") -> ScanRecord:
    return ScanRecord(
        scan_id=scan_id,
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
        report_id=None,
    )


def _seed_report(
    store: SqliteStore,
    *,
    report_id: str = "RPT-1",
    symbol: str = "BTCUSDT",
    generated_at: str = NOW_TEXT,
    data_quality: str = "COMPLETE",
    snapshot_json: str | None = None,
    narrative: str = "Range high retest with positive delta.",
    open_interest_measured: bool = True,
    funding_measured: bool = True,
    liquidations: dict[str, Any] | None = None,
    metrics_override: dict[str, Any] | None = None,
) -> None:
    if store.get_scan("SCAN-1") is None:
        store.save_scan(_scan_record())
    open_interest = (
        {"delta_over_window": "1.2", "points": [{}], "data_quality": "COMPLETE"}
        if open_interest_measured
        else {
            "unit": "BASE",
            "sampling": "REGULAR",
            "points": [],
            "delta_over_window": "0",
            "last": {
                "ts": NOW_TEXT,
                "open_interest": "0",
                "open_interest_value": "0",
            },
        }
    )
    funding = (
        {"current_rate": "0.0001", "history": [{}], "data_quality": "COMPLETE"}
        if funding_measured
        else {
            "current_rate": "0",
            "next_funding_time": NOW_TEXT,
            "interval_hours": 8,
            "mark_price": "0",
            "index_price": "0",
            "history": [],
        }
    )
    metrics: dict[str, Any] = {
        "cvd": {
            "summary": {"cvd_change": "12.5"},
            "points": [{"delta": "3"}],
            "data_quality": "COMPLETE",
        },
        "volume_profile": {
            "poc": "70000",
            "vah": "71000",
            "val": "69000",
            "bins": [{}],
            "bin_size": "100",
            "data_quality": "COMPLETE",
        },
        "open_interest": open_interest,
        "funding": funding,
        "data_quality": data_quality,
    }
    if liquidations is not None:
        metrics["liquidations"] = liquidations
    if metrics_override is not None:
        metrics = metrics_override
    snapshot = {
        "data_quality": data_quality,
        "quality_reasons": ["liquidation coverage since deployment"],
        "orderflow": {},
    }
    llm = {
        "configured": True,
        "validation_status": "VALID",
        "narrative": narrative,
        "provider": "stub",
        "model": "fixture",
        "prompt_id": "analysis",
        "prompt_version": 1,
        "opportunity_ids": ["OPP-OPEN"],
    }
    store.save_report(
        ReportRecord(
            report_id=report_id,
            scan_id="SCAN-1",
            symbol=symbol,
            timeframe="4h",
            generated_at=generated_at,
            metrics_manifest_json="[]",
            llm_decision_id="DEC-1",
            narrative_ref="inline:DEC-1",
            data_quality=data_quality,
            snapshot_json=(snapshot_json if snapshot_json is not None else json.dumps(snapshot)),
            metrics_json=json.dumps(metrics),
            llm_json=json.dumps(llm),
        )
    )


def _table_counts(settings: Settings) -> dict[str, int]:
    tables = (
        "audit_events",
        "orders",
        "positions",
        "opportunities",
        "trigger_events",
        "budget_ledger",
    )
    with sqlite3.connect(settings.data_dir / "oft.db") as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


# Literal queries (no f-string SQL) for a content snapshot of every table a
# write-like token must leave byte-for-byte unchanged.
_TABLE_CONTENT_QUERIES = {
    "audit_events": "SELECT * FROM audit_events",
    "orders": "SELECT * FROM orders",
    "positions": "SELECT * FROM positions",
    "opportunities": "SELECT * FROM opportunities",
    "trigger_events": "SELECT * FROM trigger_events",
    "budget_ledger": "SELECT * FROM budget_ledger",
}


def _table_contents(settings: Settings) -> dict[str, list[tuple[Any, ...]]]:
    with sqlite3.connect(settings.data_dir / "oft.db") as connection:
        contents: dict[str, list[tuple[Any, ...]]] = {}
        for table, query in _TABLE_CONTENT_QUERIES.items():
            rows = [tuple(row) for row in connection.execute(query)]
            # Rowid order is not part of the contract; compare content sets.
            contents[table] = sorted(rows, key=repr)
        return contents


class _ReadSpyStore:
    """Forward every store call but record reads of the query read models."""

    _READ_METHODS = frozenset(
        {
            "list_positions",
            "count_positions",
            "list_opportunities",
            "count_opportunities",
            "list_reports",
            "list_reports_for_symbol_page",
            "list_reports_for_symbol",
            "count_reports",
            "count_reports_for_symbol",
            "get_budget_settings",
            "list_budget_records_for_period",
            "list_symbols",
            "list_starred",
        }
    )

    def __init__(self, inner: SqliteStore) -> None:
        self._inner = inner
        self.read_calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name in self._READ_METHODS:
            self.read_calls.append(name)
        return getattr(self._inner, name)


def test_positions_empty_uses_the_exact_copy(tmp_path: Path) -> None:
    service, _store, _settings, _port = _service(tmp_path)
    assert _ask(service, "/positions") == POSITIONS_EMPTY_TEXT


def test_positions_render_the_open_position_values(tmp_path: Path) -> None:
    service, store, settings, _port = _service(tmp_path)
    _seed_position(settings, store, _position_payload())

    text = _ask(service, "/positions")

    assert text is not None
    assert "size: 0.01" in text
    assert "entry: 70000 USDT" in text
    assert "mark: 71000 USDT" in text
    assert "mark_state: FRESH" in text
    assert "upnl: 10 USDT" in text
    assert "stop: 69000 USDT" in text


def test_positions_label_a_stale_mark_and_never_show_it_fresh(tmp_path: Path) -> None:
    service, store, settings, _port = _service(tmp_path)
    _seed_position(settings, store, _position_payload(updated_at=STALE_TEXT))

    text = _ask(service, "/positions")

    assert text is not None
    assert "mark_state: STALE" in text
    assert "mark: 71000 USDT · STALE" in text
    assert "FRESH" not in text


def test_positions_show_an_explicit_na_for_a_missing_mark(tmp_path: Path) -> None:
    service, store, settings, _port = _service(tmp_path)
    _seed_position(settings, store, _position_payload(mark=None))

    text = _ask(service, "/positions")

    assert text is not None
    assert "mark: N/A" in text
    assert "mark_state: MISSING" in text
    assert "upnl: N/A" in text


def test_opportunities_list_open_and_triggered_without_any_transition(
    tmp_path: Path,
) -> None:
    service, store, settings, _port = _service(tmp_path)
    _seed_opportunity(settings, "OPP-OPEN", state="OPEN", created_at=OLDER_TEXT)
    _seed_opportunity(settings, "OPP-TRIG", state="TRIGGERED", created_at=NOW_TEXT)
    before = _table_counts(settings)
    payloads_before = {
        record.opportunity_id: record.payload_json for record in store.list_opportunities()
    }

    text = _ask(service, "/opportunities")

    assert text is not None
    assert "opportunity: OPP-OPEN" in text
    assert "state: OPEN" in text
    assert "opportunity: OPP-TRIG" in text
    assert "state: TRIGGERED" in text
    assert text.index("OPP-TRIG") < text.index("OPP-OPEN")
    # No ledger transition, no audit write, no state-history append.
    assert _table_counts(settings) == before
    payloads_after = {
        record.opportunity_id: record.payload_json for record in store.list_opportunities()
    }
    assert payloads_after == payloads_before


def test_opportunities_empty_uses_the_exact_copy(tmp_path: Path) -> None:
    service, _store, _settings, _port = _service(tmp_path)
    assert _ask(service, "/opportunities") == OPPORTUNITIES_EMPTY_TEXT


def test_report_resolves_the_latest_stored_report(tmp_path: Path) -> None:
    service, store, _settings, _port = _service(tmp_path)
    _seed_report(store, report_id="RPT-OLD", generated_at=OLDER_TEXT)
    _seed_report(
        store,
        report_id="RPT-NEW",
        generated_at=NOW_TEXT,
        data_quality="PARTIAL",
        liquidations={
            "coverage": "SINCE_DEPLOYMENT",
            "count": 3,
            "limitations": [
                "No historical USD-M liquidation backfill exists; coverage starts at deployment."
            ],
        },
    )

    text = _ask(service, "/report btcusdt")

    assert text is not None
    assert "report_id: RPT-NEW" in text
    assert "symbol: BTCUSDT" in text
    assert f"generated_at: {NOW_TEXT}" in text
    assert "data_quality: PARTIAL" in text
    assert "quality_reasons: liquidation coverage since deployment" in text
    assert "liquidation_coverage: SINCE_DEPLOYMENT" in text
    assert "liquidation_note: No historical USD-M liquidation backfill exists" in text
    assert "cvd_change: 12.5" in text
    assert "poc: 70000" in text
    assert "narrative: Range high retest with positive delta." in text
    assert "RPT-OLD" not in text


def test_report_unmeasured_open_interest_and_funding_render_na_not_zero(
    tmp_path: Path,
) -> None:
    service, store, _settings, _port = _service(tmp_path)
    _seed_report(
        store,
        data_quality="PARTIAL",
        open_interest_measured=False,
        funding_measured=False,
    )

    text = _ask(service, "/report BTCUSDT")

    assert text is not None
    assert "open_interest_delta: N/A — open interest not measured" in text
    assert "funding_rate: N/A — funding not measured" in text
    assert "open_interest_delta: 0" not in text
    assert "funding_rate: 0" not in text


def test_report_with_no_symbol_resolves_when_only_one_symbol_has_reports(
    tmp_path: Path,
) -> None:
    service, store, _settings, _port = _service(tmp_path)
    _seed_report(store)

    text = _ask(service, "/report")

    assert text is not None
    assert text.splitlines()[0] == "REPORT"
    assert "report_id: RPT-1" in text


def test_report_with_no_symbol_is_empty_when_several_symbols_have_reports(
    tmp_path: Path,
) -> None:
    service, store, _settings, _port = _service(tmp_path)
    _seed_report(store, report_id="RPT-BTC", symbol="BTCUSDT")
    _seed_report(store, report_id="RPT-ETH", symbol="ETHUSDT")

    assert _ask(service, "/report") == REPORT_EMPTY_TEXT


def test_report_for_an_unknown_symbol_uses_the_exact_empty_copy(tmp_path: Path) -> None:
    service, store, _settings, _port = _service(tmp_path)
    _seed_report(store)

    assert _ask(service, "/report DOGEUSDT") == REPORT_EMPTY_TEXT


def test_report_with_an_invalid_argument_returns_an_error_and_reference(
    tmp_path: Path,
) -> None:
    service, _store, _settings, _port = _service(tmp_path)

    text = _ask(service, "/report <script>BTC</script>")

    assert text is not None
    assert text.splitlines()[0] == "查询失败：参数无效。请重试或查看桌面端。"
    assert text.splitlines()[1].startswith("reference_id: TG-")
    assert "<script>" not in text
    assert "Traceback" not in text


def test_a_corrupt_report_returns_an_error_copy_not_a_traceback(tmp_path: Path) -> None:
    service, store, _settings, _port = _service(tmp_path)
    _seed_report(store, snapshot_json="{not-json")

    text = _ask(service, "/report BTCUSDT")

    assert text is not None
    assert text.splitlines()[0] == "查询失败：数据不可用。请重试或查看桌面端。"
    assert text.splitlines()[1].startswith("reference_id: TG-")
    assert "Traceback" not in text


def test_budget_shows_day_week_month_spend_and_thresholds(tmp_path: Path) -> None:
    service, _store, _settings, _port = _service(tmp_path)

    text = _ask(service, "/budget")

    assert text is not None
    assert "usage_quality: COMPLETE" in text
    assert "daily_usd: 10 USDT" in text
    assert "weekly_usd: 50 USDT" in text
    assert "monthly_usd: 150 USDT" in text
    for period in ("DAY", "WEEK", "MONTH"):
        assert period in text
    assert "spend_usd: 0 USDT" in text
    assert "unpriced_count: 0" in text


def test_degraded_budget_shows_usage_unavailable_and_keeps_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, settings, _port = _service(tmp_path)

    def _unavailable(period: str, key: str) -> tuple[Any, ...]:
        raise sqlite3.OperationalError("ledger is locked")

    monkeypatch.setattr(store, "list_budget_records_for_period", _unavailable)
    before = _table_counts(settings)
    settings_before = store.get_budget_settings()

    text = _ask(service, "/budget")

    assert text is not None
    assert "usage_quality: USAGE_UNAVAILABLE" in text
    assert "daily_usd: 10 USDT" in text
    assert "spend_usd: N/A" in text
    assert _table_counts(settings) == before
    assert store.get_budget_settings() == settings_before


def test_budget_never_records_a_call_or_changes_thresholds(tmp_path: Path) -> None:
    service, store, settings, _port = _service(tmp_path)
    before = _table_counts(settings)
    thresholds_before = store.get_budget_settings()

    _ask(service, "/budget")

    assert _table_counts(settings) == before
    assert store.get_budget_settings() == thresholds_before
    assert store.list_budget_records() == ()


def test_watchlist_groups_starred_symbols(tmp_path: Path) -> None:
    service, store, _settings, _port = _service(tmp_path)
    store.upsert_symbols(
        (
            _contract("BTCUSDT"),
            _contract("ETHUSDT"),
            _contract("XAUUSDT", tradifi=True),
            _contract("SOLUSDT"),
        ),
        observed_at=NOW_TEXT,
    )
    for symbol in ("BTCUSDT", "ETHUSDT", "XAUUSDT"):
        store.set_starred(symbol, True, starred_at=NOW_TEXT)

    text = _ask(service, "/watchlist")

    assert text is not None
    assert "scope: starred" in text
    assert "symbols: BTCUSDT, ETHUSDT" in text
    assert "symbols: XAUUSDT" in text
    assert "SOLUSDT" not in text


def test_watchlist_empty_uses_the_exact_copy(tmp_path: Path) -> None:
    service, _store, _settings, _port = _service(tmp_path)
    assert _ask(service, "/watchlist") == WATCHLIST_EMPTY_TEXT


@pytest.mark.parametrize(
    "token",
    ["/approve", "/accept", "/reject", "/cancel", "/buy", "/sell", "/close"],
)
def test_write_like_tokens_only_enqueue_the_fixed_refusal(tmp_path: Path, token: str) -> None:
    service, _store, settings, _port = _service(tmp_path)
    before = _table_contents(settings)
    outbox_before = len(service.receipts())

    text = _ask(service, token)

    assert text == "MVP 只读，请在桌面端或 CLI 操作"
    # Row contents (not just counts) are unchanged; only the outbox row is new.
    assert _table_contents(settings) == before
    assert len(service.receipts()) == outbox_before + 1


@pytest.mark.parametrize("command", COMMANDS)
def test_an_unauthorized_query_performs_no_read_model_work(tmp_path: Path, command: str) -> None:
    settings = _settings(tmp_path)
    inner = SqliteStore(settings)
    inner.initialize()
    clock = FixedClock(NOW)
    secrets = SecretsService(
        store=inner,
        settings=settings,
        clock=clock,
        backend=MemorySecretBackend(),
    )
    settings_service = SettingsService(store=inner, clock=clock, settings=settings)
    secrets.set_secret("telegram", "bot_token", SENTINEL_TOKEN, actor="test")
    settings_service.update_telegram_allowlist([1001], [2002], actor="test")
    spy = _ReadSpyStore(inner)
    service = TelegramService(
        settings=settings,
        store=spy,  # type: ignore[arg-type]
        clock=clock,
        market_data=FixtureMarketData(fixtures_dir=settings.fixtures_dir),
        secrets=secrets,
        settings_service=settings_service,
        port=TelegramFake(),
    )

    text = _ask(service, command, chat_id=9999)

    assert text == "未授权"
    assert spy.read_calls == []
    for marker in ("POS-", "count:", "symbols:", "version:", "TG-", "report_id:"):
        assert marker not in text


def test_an_authorized_query_does_read_the_model(tmp_path: Path) -> None:
    service, store, _settings, _port = _service(tmp_path)
    spy = _ReadSpyStore(store)
    service._store = spy  # noqa: SLF001 - spy the query read path

    assert _ask(service, "/positions") == POSITIONS_EMPTY_TEXT
    assert "list_positions" in spy.read_calls


class _LongRender:
    """A deterministic over-cap renderer used to prove the one-message budget."""

    @staticmethod
    def render(**_kwargs: Any) -> str:
        return "HEAD\n" + "x" * 6000


def test_an_overlong_render_is_truncated_to_one_budgeted_outbox_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, settings, port = _service(tmp_path)
    monkeypatch.setattr(telegram_queries, "render_positions_query", _LongRender.render)
    before = _table_counts(settings)

    text = _ask(service, "/positions")

    assert text is not None
    assert len(text) <= 4096
    assert "…" in text
    assert "reference_id: TG-" in text
    outbox = store.list_telegram_receipts()
    assert len(outbox) == 1
    assert port.send_calls == []
    assert _table_counts(settings) == before


def test_an_unexpected_renderer_defect_yields_the_typed_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _store, _settings, _port = _service(tmp_path)

    def _boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("renderer defect must not reach chat")

    monkeypatch.setattr(telegram_messages, "render_positions", _boom)

    text = _ask(service, "/positions")

    assert text is not None
    assert text.splitlines()[0] == "查询失败：数据不可用。请重试或查看桌面端。"
    assert text.splitlines()[1].startswith("reference_id: TG-")
    assert "renderer defect" not in text
    assert "Traceback" not in text


def test_a_report_with_corrupt_metric_shapes_renders_na_not_a_traceback(
    tmp_path: Path,
) -> None:
    service, store, _settings, _port = _service(tmp_path)
    corrupt_metrics = {
        "cvd": {"summary": {"cvd_change": "not-a-number"}, "points": []},
        "volume_profile": {"poc": None, "vah": {"nested": True}, "val": []},
        "open_interest": {"delta_over_window": "1e100000000", "points": [{}]},
        "funding": {"current_rate": None, "history": [{}]},
    }
    _seed_report(store, metrics_override=corrupt_metrics)

    text = _ask(service, "/report BTCUSDT")

    assert text is not None
    assert text.splitlines()[0] == "REPORT"
    assert "cvd_change: N/A" in text
    assert "poc: N/A" in text
    assert "open_interest_delta: N/A" in text
    assert "funding_rate: N/A" in text
    assert "Traceback" not in text


def test_a_budget_render_defect_returns_the_typed_copy_and_advances_the_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, _settings, port = _service(tmp_path)

    def _boom(_snapshot: Any) -> str:
        raise RuntimeError("budget render defect")

    monkeypatch.setattr(telegram_messages, "render_budget", _boom)
    port.script_updates([make_update(1, chat_id=1001, user_id=2002, text="/budget")])
    poller = TelegramPoller(
        service=service,
        store=store,
        clock=FixedClock(NOW),
        port_resolver=lambda: port,
        long_poll_timeout=0,
        poll_interval_seconds=0.01,
    )

    assert poller.poll_once() == 1

    receipt = store.list_telegram_receipts()[0]
    text = json.loads(receipt.payload_json)["text"]
    assert text.splitlines()[0] == "查询失败：数据不可用。请重试或查看桌面端。"
    assert "budget render defect" not in text
    # The poison update was acknowledged, so the next poll does not replay it.
    assert store.get_system_state("telegram.poll_offset") == "2"
    assert poller.poll_once() == 0


@pytest.mark.parametrize(
    ("command", "renderer"), [("/status", "render_status"), ("/help", "render_help")]
)
def test_an_over_cap_status_or_help_render_gets_marker_reference_and_one_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, renderer: str
) -> None:
    service, store, _settings, _port = _service(tmp_path)
    over_cap = "HEAD\n" + "x" * 6000
    if renderer == "render_status":
        monkeypatch.setattr(service_module, "render_status", lambda report: over_cap)
    else:
        monkeypatch.setattr(service_module, "render_help", lambda: over_cap)

    text = _ask(service, command)

    assert text is not None
    assert len(text) <= 4096
    assert "…" in text
    assert "桌面端" in text
    assert "reference_id: TG-" in text
    assert len(store.list_telegram_receipts()) == 1


def test_poller_service_dispatcher_full_loop_sends_one_receipt_with_message_id(
    tmp_path: Path,
) -> None:
    service, store, _settings, port = _service(tmp_path)
    port.script_updates([make_update(1, chat_id=1001, user_id=2002, text="/positions")])
    poller = TelegramPoller(
        service=service,
        store=store,
        clock=FixedClock(NOW),
        port_resolver=lambda: port,
        long_poll_timeout=0,
        poll_interval_seconds=0.01,
    )
    dispatcher = OutboxDispatcher(
        store=store,
        clock=FixedClock(NOW),
        port_resolver=lambda: port,
    )

    assert poller.poll_once() == 1
    assert dispatcher.dispatch_once() == 1

    receipt = store.get_telegram_outbox("update:1:reply")
    assert receipt is not None
    assert receipt.state == "sent"
    assert receipt.message_id is not None
    assert receipt.sent_at == NOW_TEXT
    assert port.sent[0]["message_id"] == receipt.message_id
    assert port.sent[0]["text"] == POSITIONS_EMPTY_TEXT
    # One row, one send; a second drain sends nothing.
    assert len(port.sent) == 1
    assert dispatcher.dispatch_once() == 0
