"""Recorded WS replay through the supervisor into immutable Parquet parts."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.dataset as dataset
import pytest
from typer.testing import CliRunner

from oft.adapters.parquet_store import FlushPolicy, LiveParquetBuffer
from oft.adapters.sqlite_store import SqliteStore
from oft.application.ingestion_supervisor import (
    CheckpointStore,
    IngestionSupervisor,
    read_ws_state,
)
from oft.cli.main import app
from oft.config import Settings
from oft.domain.events import AggTrade

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ws"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant

    def advance(self, seconds: float) -> None:
        self.instant += timedelta(seconds=seconds)


def _fixture_lines() -> list[str]:
    lines: list[str] = []
    for name in ("aggtrade", "kline", "markprice", "forceorder"):
        lines.extend((FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines())
    return lines


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    return settings


def _supervisor(
    settings: Settings,
    clock: MutableClock,
    source: list[str],
    *,
    checkpoint: CheckpointStore | None = None,
    max_message_bytes: int = 512,
) -> IngestionSupervisor:
    store = SqliteStore(settings)
    buffer = LiveParquetBuffer(
        settings=settings,
        store=store,
        clock=clock,
        policy=FlushPolicy(max_rows=50_000, max_interval_seconds=30),
    )
    return IngestionSupervisor(
        settings=settings,
        source=source,
        store=store,
        parquet=buffer,
        clock=clock,
        checkpoint=checkpoint,
        max_message_bytes=max_message_bytes,
        sleep=lambda _seconds: None,
    )


def _agg_trade_ids(settings: Settings) -> list[int]:
    root = settings.data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT"
    parts = [str(path) for path in root.glob("date=*/*.parquet")]
    if not parts:
        return []
    table = dataset.dataset(parts, format="parquet").to_table(columns=["agg_trade_id"])
    return table.column("agg_trade_id").to_pylist()


def _rows(settings: Settings, data_type: str) -> int:
    root = settings.data_dir / "market" / "um" / data_type / "symbol=BTCUSDT"
    parts = [str(path) for path in root.glob("date=*/*.parquet")]
    if not parts:
        return 0
    return dataset.dataset(parts, format="parquet").to_table().num_rows


def test_recorded_session_produces_expected_unique_parquet_rows(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock(NOW)
    supervisor = _supervisor(settings, clock, _fixture_lines())

    stats = supervisor.run()

    assert stats.accepted == 14
    assert stats.persisted_rows == 12
    assert stats.duplicates == 2
    assert stats.out_of_order == 1
    assert stats.malformed == 7
    assert stats.oversized == 1
    assert sorted(_agg_trade_ids(settings)) == [100, 101, 103, 105]
    assert len(_agg_trade_ids(settings)) == len(set(_agg_trade_ids(settings)))
    assert _rows(settings, "klines_1m") == 1  # only closed bars are durable
    assert _rows(settings, "mark_price_klines") == 3
    manifest = SqliteStore(settings).list_manifest(symbol="BTCUSDT")
    assert {entry.data_type for entry in manifest} >= {
        "agg_trades",
        "klines_1m",
        "mark_price_klines",
        "liquidations",
    }


def test_malformed_and_oversized_events_are_counted_not_fatal(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock(NOW)
    supervisor = _supervisor(settings, clock, _fixture_lines())

    stats = supervisor.run()

    assert stats.malformed == 7
    assert stats.oversized == 1
    assert stats.dropped == 0
    assert SqliteStore(settings).integrity_check() is True


def test_restart_from_checkpoint_does_not_lose_or_duplicate_events(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock(NOW)
    checkpoint_path = settings.data_dir / "run" / "checkpoints.json"
    first = _supervisor(
        settings,
        clock,
        _fixture_lines(),
        checkpoint=CheckpointStore(checkpoint_path, clock),
    )
    first.run()

    duplicate_last = (
        '{"e":"aggTrade","E":1793966400600,"s":"BTCUSDT","a":105,"p":"35005.0",'
        '"q":"0.6","f":1007,"l":1007,"T":1793966400600,"m":false}'
    )
    new_event = (
        '{"e":"aggTrade","E":1793966400700,"s":"BTCUSDT","a":106,"p":"35006.0",'
        '"q":"0.7","f":1008,"l":1008,"T":1793966400700,"m":false}'
    )
    second = _supervisor(
        settings,
        clock,
        [duplicate_last, new_event],
        checkpoint=CheckpointStore(checkpoint_path, clock),
    )
    stats = second.run()

    assert stats.duplicates == 1
    assert stats.persisted_rows == 1
    ids = _agg_trade_ids(settings)
    assert sorted(ids) == [100, 101, 103, 105, 106]
    assert len(ids) == len(set(ids))


def test_ws_state_and_freshness_are_exported(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock(NOW)
    supervisor = _supervisor(settings, clock, _fixture_lines())
    supervisor.run()

    state = read_ws_state(settings)
    assert state is not None
    assert state.streams["btcusdt@aggTrade"]["last_event_id"] == 105
    assert state.streams["btcusdt@markPrice@1s"]["last_event_time"] == ("2026-11-06T12:00:03.000Z")

    clock.instant = NOW + timedelta(seconds=121)
    stamps = supervisor.evaluate_freshness()
    by_stream = {stamp.stream: stamp for stamp in stamps}
    assert by_stream["mark"].stale is True
    assert by_stream["kline_1m"].stale is False
    assert read_ws_state(settings).stale is True


def test_oft_daemon_start_and_status_show_ws_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    import uvicorn

    data_dir = tmp_path / "data"
    orphan = (
        data_dir
        / "market"
        / "um"
        / "agg_trades"
        / "symbol=BTCUSDT"
        / "date=2026-11-06"
        / "part-orphan.parquet"
    )
    orphan.parent.mkdir(parents=True)
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(pa.table({"agg_trade_id": pa.array([999], pa.int64())}), orphan)

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    runner = CliRunner()

    started = runner.invoke(
        app,
        ["daemon", "start", "--replay-file", str(FIXTURES / "aggtrade.jsonl")],
    )
    status = runner.invoke(app, ["status", "--json"])

    assert started.exit_code == 0, started.output
    assert calls
    assert not orphan.exists()
    assert list((data_dir / "quarantine").rglob("*.parquet"))
    payload = json.loads(status.stdout)
    market_data = payload["data"]["market_data"]
    assert market_data["connection_state"] == "REPLAY"
    streams = {item["stream"]: item for item in market_data["streams"]}
    assert streams["btcusdt@aggTrade"]["last_event_id"] == 105


@pytest.mark.live_smoke
@pytest.mark.skipif(
    os.environ.get("OFT_LIVE_SMOKE") != "1",
    reason="opt-in live smoke; set OFT_LIVE_SMOKE=1",
)
def test_btcusdt_live_smoke() -> None:
    from oft.adapters.binance_ws import BinanceWsClient
    from oft.domain.events import normalize_event

    client = BinanceWsClient(receive_timeout=15.0)
    client.connect(["btcusdt@aggTrade"])
    try:
        for _ in range(50):
            _, payload = client.receive_event(timeout=15.0)
            event = normalize_event(payload)
            if isinstance(event, AggTrade):
                assert event.symbol == "BTCUSDT"
                return
        raise AssertionError("no aggTrade received from the live BTCUSDT stream")
    finally:
        client.close()
