"""Hostile-input and durability fixes for live ingestion."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oft.adapters.binance_ws import MessageTooLargeError
from oft.adapters.parquet_store import LiveParquetBuffer
from oft.adapters.sqlite_store import SqliteStore
from oft.application.ingestion_supervisor import (
    CheckpointStore,
    ConnectionState,
    IngestionSupervisor,
)
from oft.config import Settings

NOW = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW


class FakeRecovery:
    def __init__(self) -> None:
        self.calls = 0

    def recover_agg_trades(self, symbol, *, last_event_time, now, stop=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        return _RecoveryResult(trades=(), complete=True, start=now, end=now, reason=None)


@dataclass
class _RecoveryResult:
    trades: tuple
    complete: bool
    start: datetime
    end: datetime
    reason: str | None


class FlakyClient:
    def __init__(self, failures: int, events: list[tuple[str, dict]]) -> None:
        self.failures = failures
        self.events = events
        self.connects = 0

    def connect(self, streams) -> None:  # type: ignore[no-untyped-def]
        self.connects += 1
        if self.connects <= self.failures:
            raise OSError("upstream down")

    def subscribe(self, streams) -> None:  # type: ignore[no-untyped-def]
        return None

    def receive_event(self) -> tuple[str, dict]:
        if self.events:
            return self.events.pop(0)
        raise OSError("closed")


def _supervisor(settings: Settings, clocks, source, *, client=None, recovery=None):
    store = SqliteStore(settings)
    buffer = LiveParquetBuffer(settings=settings, store=store, clock=clocks)
    return IngestionSupervisor(
        settings=settings,
        source=source,
        store=store,
        parquet=buffer,
        clock=clocks,
        client=client,
        recovery=recovery,
        sleep=lambda _seconds: None,
        max_message_bytes=64 * 1024,
    )


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    return settings


def test_deeply_nested_json_is_counted_and_does_not_raise(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    nested = '{"a":' * 10_000 + "1" + "}" * 10_000
    raw = '{"stream":"btcusdt@aggTrade","data":' + nested + "}"

    stats = _supervisor(settings, Clock(), [raw]).run()

    assert stats.malformed == 1
    assert stats.accepted == 0


def test_startup_connect_failure_retries_then_recovers(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    recovery = FakeRecovery()
    client = FlakyClient(
        failures=2,
        events=[("btcusdt@aggTrade", {"e": "aggTrade", "s": "BTCUSDT"})],
    )
    supervisor = _supervisor(settings, Clock(), [], client=client, recovery=recovery)

    def counted_stop() -> bool:
        # The stop predicate is now also consulted by ``_recover`` before its
        # per-symbol work, so let the recovery complete before stopping.
        return client.connects >= 3 and recovery.calls >= 1

    result = supervisor.run_forever(stop=counted_stop)

    assert client.connects >= 3
    assert recovery.calls >= 1
    assert result.recoveries >= 1


def test_recover_forwards_the_stop_predicate(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seen: list[Any] = []

    class Recovery:
        def recover_agg_trades(self, symbol, *, last_event_time, now, stop=None):  # type: ignore[no-untyped-def]
            seen.append(stop)
            return _RecoveryResult(trades=(), complete=True, start=now, end=now, reason=None)

    supervisor = _supervisor(settings, Clock(), [], recovery=Recovery())

    def predicate() -> bool:
        return False

    supervisor._recover(stop=predicate)

    assert seen == [predicate]


def test_lost_checkpoint_does_not_duplicate_klines(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fixture = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "ws" / "kline.jsonl"
    lines = fixture.read_text(encoding="utf-8").splitlines()
    first = _supervisor(settings, Clock(), lines)
    first.run()
    checkpoint = first.checkpoint.path
    checkpoint.unlink()

    second = _supervisor(settings, Clock(), lines)
    second.run()

    root = settings.data_dir / "market" / "um" / "klines_1m" / "symbol=BTCUSDT"
    parts = list(root.glob("date=*/*.parquet"))
    seen: set[object] = set()
    for path in parts:
        seen.update(_parquet_values(path, "open_time"))
    # Only closed bars are durable; the lost checkpoint must not duplicate them.
    assert len(seen) == 1
    before = len(parts)
    third = _supervisor(settings, Clock(), lines)
    third.run()
    assert len(list(root.glob("date=*/*.parquet"))) == before


def _parquet_values(path: Path, column: str) -> set[object]:
    import pyarrow.parquet as pq

    return set(pq.read_table(path, columns=[column]).column(column).to_pylist())


def _open_time(line: str) -> object:
    import pyarrow.parquet as pq  # noqa: F401

    payload = json.loads(line)
    data = payload.get("data", payload)
    return data["k"]["t"]


def test_oversized_live_message_is_counted_not_disconnected_silently(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    class OversizedThenClose:
        def __init__(self) -> None:
            self.calls = 0

        def connect(self, streams) -> None:  # type: ignore[no-untyped-def]
            return None

        def subscribe(self, streams) -> None:  # type: ignore[no-untyped-def]
            return None

        def receive_event(self):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                raise MessageTooLargeError("too large")
            raise OSError("closed")

    client = OversizedThenClose()
    supervisor = _supervisor(settings, Clock(), [], client=client)

    def stop() -> bool:
        return client.calls >= 2

    stats = supervisor.run_forever(stop=stop)

    assert stats.oversized == 1


def test_corrupt_checkpoint_is_ignored_without_raising(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = CheckpointStore(settings.data_dir / "run" / "ingest_checkpoints.json", Clock())
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps(
            {
                "streams": {
                    "s": {
                        "last_event_id": "abc",
                        "updated_at": "2026-10-01T00:00:00+00:00",
                    },
                    "t": {
                        "last_event_id": {},
                        "updated_at": "2026-10-01T00:00:00+00:00",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    assert store.load() == {}
    assert store.age_seconds() is None


def test_closed_kline_replaces_an_open_update(tmp_path: Path) -> None:
    from oft.adapters.parquet_store import LiveParquetBuffer
    from oft.domain.events import KlineEvent

    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    clock = Clock()
    buffer = LiveParquetBuffer(settings=settings, store=store, clock=clock)

    def kline(close: str, *, is_closed: bool) -> KlineEvent:
        return KlineEvent(
            symbol="BTCUSDT",
            event_time=NOW,
            open_time=NOW,
            close_time=NOW + timedelta(minutes=1),
            interval="1m",
            open=_dec("100"),
            high=_dec(close),
            low=_dec("100"),
            close=_dec(close),
            volume=_dec("1"),
            quote_volume=_dec("100"),
            trade_count=1,
            taker_buy_volume=_dec("1"),
            taker_buy_quote_volume=_dec("100"),
            is_closed=is_closed,
        )

    buffer.add(kline("150", is_closed=False))
    buffer.flush()
    buffer.add(kline("200", is_closed=True))
    buffer.flush()

    root = settings.data_dir / "market" / "um" / "klines_1m" / "symbol=BTCUSDT"
    import pyarrow.parquet as pq

    rows = []
    for path in root.glob("date=*/*.parquet"):
        rows.extend(pq.read_table(path).to_pylist())
    final = [row for row in rows if row["is_closed"]]
    assert len(rows) == 1  # only the closed bar is durable
    assert final and final[0]["close"] == "200"
    assert final[0]["is_closed"] is True
    assert len({row["open_time"] for row in rows}) == 1


def _dec(value: str):  # type: ignore[no-untyped-def]
    from decimal import Decimal

    return Decimal(value)


def test_kline_dedupe_memory_is_bounded(tmp_path: Path) -> None:
    from oft.adapters.parquet_store import LiveParquetBuffer
    from oft.domain.events import KlineEvent

    settings = _settings(tmp_path)
    buffer = LiveParquetBuffer(
        settings=settings,
        store=SqliteStore(settings),
        clock=Clock(),
        dedupe_max_keys=10,
    )
    for index in range(100):
        buffer.add(
            KlineEvent(
                symbol="BTCUSDT",
                event_time=NOW + timedelta(minutes=index),
                open_time=NOW + timedelta(minutes=index),
                close_time=NOW + timedelta(minutes=index + 1),
                interval="1m",
                open=_dec("100"),
                high=_dec("101"),
                low=_dec("100"),
                close=_dec("101"),
                volume=_dec("1"),
                quote_volume=_dec("100"),
                trade_count=1,
                taker_buy_volume=_dec("1"),
                taker_buy_quote_volume=_dec("100"),
                is_closed=True,
            )
        )
        if index % 10 == 0:
            buffer.flush()

    assert buffer.dedupe_keys <= 10


def test_disconnect_flushes_before_checkpointing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    order: list[str] = []

    class OneDisconnect:
        def connect(self, streams) -> None:  # type: ignore[no-untyped-def]
            return None

        def subscribe(self, streams) -> None:  # type: ignore[no-untyped-def]
            return None

        def receive_event(self):  # type: ignore[no-untyped-def]
            raise OSError("socket closed")

    supervisor = _supervisor(settings, Clock(), [], client=OneDisconnect())
    supervisor._drain_all = lambda: order.append("drain")  # type: ignore[method-assign]
    supervisor._save_checkpoint = lambda: order.append("save")  # type: ignore[method-assign]
    disconnects = {"count": 0}

    original_receive = OneDisconnect.receive_event

    def counted_receive(self):  # type: ignore[no-untyped-def]
        disconnects["count"] += 1
        return original_receive(self)

    OneDisconnect.receive_event = counted_receive  # type: ignore[method-assign]

    def stop() -> bool:
        return disconnects["count"] >= 1

    supervisor.run_forever(stop=stop)

    assert order.index("drain") < order.index("save")


def test_active_symbols_default_to_stars_and_explicit_overrides() -> None:
    from oft.cli.main import _active_symbols

    assert _active_symbols(None, ("BTCUSDT", "XAUUSDT")) == ("BTCUSDT", "XAUUSDT")
    assert _active_symbols(None, ()) == ()
    assert _active_symbols("ETHUSDT", ("BTCUSDT",)) == ("ETHUSDT",)


def test_run_forever_flushes_buffered_rows_to_parquet_on_stop(tmp_path: Path) -> None:
    """F4: the supervisor's finally drains and checkpoints before returning."""
    import pyarrow.dataset as dataset

    from oft.adapters.parquet_store import LiveParquetBuffer
    from oft.application.ingestion_supervisor import IngestionSupervisor

    settings = _settings(tmp_path)

    class EventClock:
        def now(self) -> datetime:
            return datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

    clock = EventClock()
    fixture = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "ws" / "aggtrade.jsonl"
    payload = json.loads(fixture.read_text(encoding="utf-8").splitlines()[0])["data"]

    class OneEventThenStop:
        def __init__(self) -> None:
            self.delivered = False

        def connect(self, streams) -> None:  # type: ignore[no-untyped-def]
            return None

        def subscribe(self, streams) -> None:  # type: ignore[no-untyped-def]
            return None

        def receive_event(self):  # type: ignore[no-untyped-def]
            if not self.delivered:
                self.delivered = True
                return "btcusdt@aggTrade", payload
            raise OSError("closed")

    client = OneEventThenStop()
    store = SqliteStore(settings)
    supervisor = IngestionSupervisor(
        settings=settings,
        source=(),
        store=store,
        parquet=LiveParquetBuffer(settings=settings, store=store, clock=clock),
        clock=clock,
        client=client,
        sleep=lambda _seconds: None,
    )

    stats = supervisor.run_forever(stop=lambda: client.delivered)

    assert stats.accepted == 1
    root = settings.data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT"
    parts = [str(path) for path in root.glob("date=*/*.parquet")]
    assert parts, "the buffered row must be flushed on shutdown"
    table = dataset.dataset(parts, format="parquet").to_table(columns=["agg_trade_id"])
    assert table.column("agg_trade_id").to_pylist() == [100]


def test_live_ingestion_handle_stop_closes_the_stream_and_joins() -> None:
    import threading

    from oft.cli.main import LiveIngestionHandle

    stop_event = threading.Event()
    closed: list[bool] = []

    class _Stream:
        def close(self) -> None:
            closed.append(True)

    thread = threading.Thread(target=lambda: stop_event.wait(timeout=5), daemon=True)
    thread.start()
    handle = LiveIngestionHandle(
        stop_event=stop_event,
        thread=thread,
        supervisor=object(),
        clients=(_Stream(),),
    )

    assert handle.stop(timeout=1) is True
    assert closed == [True]
    assert thread.is_alive() is False


def test_run_forever_backoff_is_interruptible_by_stop(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    stop_event = threading.Event()
    sleeps: list[float] = []

    class Client:
        def connect(self, streams) -> None:  # type: ignore[no-untyped-def]
            return None

        def subscribe(self, streams) -> None:  # type: ignore[no-untyped-def]
            return None

        def receive_event(self):  # type: ignore[no-untyped-def]
            raise OSError("closed")

    def sleeper(delay: float) -> bool:
        sleeps.append(delay)
        stop_event.set()
        return True

    supervisor = IngestionSupervisor(
        settings=settings,
        source=(),
        store=SqliteStore(settings),
        parquet=LiveParquetBuffer(settings=settings, store=SqliteStore(settings), clock=Clock()),
        clock=Clock(),
        client=Client(),
        symbols=("BTCUSDT",),
        sleep=sleeper,
        backoff_base=0.01,
    )

    stats = supervisor.run_forever(stop=stop_event.is_set)

    # The sleeper was reached and returned immediately because it set the event.
    assert sleeps
    assert stop_event.is_set()
    assert stats.reconnects == 1


def test_connect_and_subscribe_honours_a_requested_stop(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.stopped = False

        def connect(self, streams) -> None:  # type: ignore[no-untyped-def]
            self.calls.append("connect")
            self.stopped = True

        def subscribe(self, streams) -> None:  # type: ignore[no-untyped-def]
            self.calls.append("subscribe")

        def receive_event(self):  # type: ignore[no-untyped-def]
            raise OSError("closed")

    client = Client()
    supervisor = IngestionSupervisor(
        settings=settings,
        source=(),
        store=SqliteStore(settings),
        parquet=LiveParquetBuffer(settings=settings, store=SqliteStore(settings), clock=Clock()),
        clock=Clock(),
        client=client,
        symbols=("BTCUSDT",),
        sleep=lambda _seconds: None,
    )

    # Already stopped: neither handshake call is made.
    assert supervisor._connect_and_subscribe(stop=lambda: True) is False
    assert client.calls == []

    # Stop requested during the connect handshake: subscribe is skipped.
    assert supervisor._connect_and_subscribe(stop=lambda: client.stopped) is False
    assert client.calls == ["connect"]
    # A clean shutdown must not persist CONNECTING after the connect handshake;
    # the supervisor reports DISCONNECTED instead.
    assert supervisor.state is ConnectionState.DISCONNECTED


def test_run_live_ingestion_wires_a_stop_aware_sleeper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oft.cli.main as cli_main

    settings = Settings(env="paper", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    container = SimpleNamespace(
        settings=settings,
        store=store,
        clock=Clock(),
        symbol_registry=SimpleNamespace(list_starred=lambda: [SimpleNamespace(symbol="BTCUSDT")]),
        notifications=None,
    )
    captured: dict[str, Any] = {}

    class FakeSupervisor:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def run_forever(self, *, stop: Any = None) -> None:
            captured["stop"] = stop

    class FakeClient:
        def close(self) -> None:
            return None

    monkeypatch.setattr("oft.application.ingestion_supervisor.IngestionSupervisor", FakeSupervisor)
    monkeypatch.setattr(
        cli_main, "_build_limited_clients", lambda _container: (FakeClient(), FakeClient())
    )

    handle = cli_main._run_live_ingestion(settings, None, "BTCUSDT", container=container)

    assert handle is not None
    assert captured["sleep"] is not time.sleep
    assert captured["sleep"] == handle.stop_event.wait
    assert handle.stop() is True
    assert captured["stop"]() is True


def test_daemon_starts_idle_without_starred_symbols(tmp_path: Path) -> None:
    from oft.application.ingestion_supervisor import read_ws_state
    from oft.cli.main import _run_live_ingestion

    settings = _settings(tmp_path)
    _run_live_ingestion(settings, None, None)
    state = read_ws_state(settings)
    assert state is not None and state.connection_state == "IDLE"

    container = SqliteStore(settings)
    container.set_starred("BTCUSDT", True, starred_at="2026-11-07T00:00:00.000Z")
    _run_live_ingestion(settings, None, None)
    state = read_ws_state(settings)
    assert state is not None and state.connection_state == "DISCONNECTED"


def test_reconnect_recomputes_starred_symbols(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    provider_symbols = [("BTCUSDT",)]
    streams_seen: list[tuple[str, ...]] = []

    class Client:
        def connect(self, streams) -> None:  # type: ignore[no-untyped-def]
            streams_seen.append(tuple(streams))

        def subscribe(self, streams) -> None:  # type: ignore[no-untyped-def]
            return None

        def receive_event(self):  # type: ignore[no-untyped-def]
            if len(streams_seen) == 1:
                raise OSError("drop")
            raise OSError("closed")

    def provider() -> tuple[str, ...]:
        return provider_symbols[0]

    supervisor = IngestionSupervisor(
        settings=settings,
        source=(),
        store=SqliteStore(settings),
        parquet=LiveParquetBuffer(settings=settings, store=SqliteStore(settings), clock=Clock()),
        clock=Clock(),
        client=Client(),
        symbols=("BTCUSDT",),
        symbol_provider=provider,
        sleep=lambda _seconds: None,
    )

    def stop() -> bool:
        if len(streams_seen) == 1:
            provider_symbols[0] = ("ETHUSDT",)
        return len(streams_seen) >= 2

    supervisor.run_forever(stop=stop)

    assert any("btcusdt@aggTrade" in stream for stream in streams_seen[0])
    assert any("ethusdt@aggTrade" in stream for stream in streams_seen[1])


def test_reconnect_goes_idle_when_all_symbols_are_unstarred_then_recovers(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    provider_symbols: list[tuple[str, ...]] = [("BTCUSDT",)]
    streams_seen: list[tuple[str, ...]] = []

    class Client:
        def connect(self, streams) -> None:  # type: ignore[no-untyped-def]
            streams_seen.append(tuple(streams))
            if len(streams_seen) == 1:
                provider_symbols[0] = ()  # unstar everything before reconnect

        def subscribe(self, streams) -> None:  # type: ignore[no-untyped-def]
            return None

        def receive_event(self):  # type: ignore[no-untyped-def]
            raise OSError("drop")

    supervisor = IngestionSupervisor(
        settings=settings,
        source=(),
        store=SqliteStore(settings),
        parquet=LiveParquetBuffer(settings=settings, store=SqliteStore(settings), clock=Clock()),
        clock=Clock(),
        client=Client(),
        symbols=("BTCUSDT",),
        symbol_provider=lambda: provider_symbols[0],
        sleep=lambda _seconds: None,
        backoff_base=0.05,
    )

    def stop() -> bool:
        if supervisor.state.value == "IDLE":
            provider_symbols[0] = ("ETHUSDT",)  # star again during the idle poll
        return len(streams_seen) >= 2

    supervisor.run_forever(stop=stop)

    assert streams_seen[0] and any("btcusdt@aggTrade" in item for item in streams_seen[0])
    assert len(streams_seen) == 2  # no connect attempt was made while idle
    assert any("ethusdt@aggTrade" in item for item in streams_seen[1])


def test_reconnect_delay_is_clamped_for_huge_attempts() -> None:
    from oft.application.ingestion_supervisor import reconnect_delay

    assert reconnect_delay(100_000, jitter=lambda: 0.0) == 30.0


def test_live_agg_dedupe_is_incremental_not_a_full_rescan(tmp_path: Path, monkeypatch) -> None:
    from decimal import Decimal

    from oft.adapters.parquet_store import ParquetStore
    from oft.domain.events import AggTrade

    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    buffer = LiveParquetBuffer(settings=settings, store=store, clock=Clock())

    def no_rescan(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("write_agg_trades_batch rescanned history")

    monkeypatch.setattr(ParquetStore, "_existing_agg_trade_ids", no_rescan)

    def trade(trade_id: int) -> AggTrade:
        return AggTrade(
            symbol="BTCUSDT",
            agg_trade_id=trade_id,
            price=Decimal("100"),
            quantity=Decimal("1"),
            first_trade_id=trade_id,
            last_trade_id=trade_id,
            transact_time=NOW,
            is_buyer_maker=False,
        )

    buffer.add(trade(1))
    buffer.flush()
    buffer.add(trade(1))
    buffer.add(trade(2))
    buffer.flush()

    root = settings.data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT"
    import pyarrow.parquet as pq

    rows = []
    for path in root.glob("date=*/*.parquet"):
        rows.extend(pq.read_table(path, columns=["agg_trade_id"]).to_pylist())
    assert sorted(row["agg_trade_id"] for row in rows) == [1, 2]


def _agg_trade(symbol: str, trade_id: int):
    from decimal import Decimal

    from oft.domain.events import AggTrade

    return AggTrade(
        symbol=symbol,
        agg_trade_id=trade_id,
        price=Decimal("100"),
        quantity=Decimal("1"),
        first_trade_id=trade_id,
        last_trade_id=trade_id,
        transact_time=NOW,
        is_buyer_maker=False,
    )


def test_same_id_on_different_symbols_is_not_deduped(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    buffer = LiveParquetBuffer(settings=settings, store=SqliteStore(settings), clock=Clock())
    buffer.add(_agg_trade("BTCUSDT", 7))
    buffer.add(_agg_trade("ETHUSDT", 7))
    buffer.flush()

    import pyarrow.parquet as pq

    rows = []
    for symbol in ("BTCUSDT", "ETHUSDT"):
        root = settings.data_dir / "market" / "um" / "agg_trades" / f"symbol={symbol}"
        for path in root.glob("date=*/*.parquet"):
            rows.extend(pq.read_table(path, columns=["symbol", "agg_trade_id"]).to_pylist())
    assert sorted((row["symbol"], row["agg_trade_id"]) for row in rows) == [
        ("BTCUSDT", 7),
        ("ETHUSDT", 7),
    ]


def test_transient_flush_failure_retries_without_duplicates(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    buffer = LiveParquetBuffer(settings=settings, store=SqliteStore(settings), clock=Clock())
    original = buffer._writer.write_agg_trades_batch
    calls = {"count": 0}

    def flaky(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] == 1:
            original(*args, **kwargs)  # publish, then simulate a crash
            raise OSError("disk I/O error")
        return original(*args, **kwargs)

    monkeypatch.setattr(buffer._writer, "write_agg_trades_batch", flaky)
    buffer.add(_agg_trade("BTCUSDT", 1))

    with pytest.raises(OSError):
        buffer.flush()
    buffer.flush()

    import pyarrow.parquet as pq

    root = settings.data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT"
    rows = []
    for path in root.glob("date=*/*.parquet"):
        rows.extend(pq.read_table(path, columns=["agg_trade_id"]).to_pylist())
    assert [row["agg_trade_id"] for row in rows] == [1]


def test_manifest_failure_keeps_events_retryable_and_checkpoint_behind(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    clock = Clock()
    store = SqliteStore(settings)
    buffer = LiveParquetBuffer(settings=settings, store=store, clock=clock)
    fixture = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "ws" / "aggtrade.jsonl"
    lines = fixture.read_text(encoding="utf-8").splitlines()[:2]
    supervisor = IngestionSupervisor(
        settings=settings,
        source=lines,
        store=store,
        parquet=buffer,
        clock=clock,
        sleep=lambda _seconds: None,
    )
    original = store.upsert_manifest_entries
    calls = {"count": 0}

    def flaky(entries):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("sqlite busy")
        return original(entries)

    monkeypatch.setattr(store, "upsert_manifest_entries", flaky)

    with pytest.raises(OSError):
        supervisor.run()
    assert supervisor.state.value == "FAILED"
    assert supervisor.checkpoint.load() == {}  # checkpoint did not advance

    monkeypatch.setattr(store, "upsert_manifest_entries", original)
    supervisor._drain_all()

    import pyarrow.parquet as pq

    root = settings.data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT"
    ids: list[int] = []
    for path in root.glob("date=*/*.parquet"):
        ids.extend(pq.read_table(path, columns=["agg_trade_id"]).column(0).to_pylist())
    assert len(ids) == len(set(ids))  # no duplicate rows
    assert all(row.path for row in store.list_manifest() if row.data_type == "agg_trades")
    assert supervisor.checkpoint.load()  # advanced only after a durable commit


@pytest.mark.parametrize(
    ("fixture_name", "data_type"),
    [
        ("aggtrade.jsonl", "agg_trades"),
        ("kline.jsonl", "klines_1m"),
        ("markprice.jsonl", "mark_price_klines"),
    ],
)
def test_locked_manifest_is_fatal_but_retryable(
    tmp_path: Path, monkeypatch, fixture_name: str, data_type: str
) -> None:
    import sqlite3

    from oft.ports.store import StoreUnavailableError

    settings = _settings(tmp_path)
    clock = Clock()
    store = SqliteStore(settings)
    buffer = LiveParquetBuffer(settings=settings, store=store, clock=clock)
    fixture = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "ws" / fixture_name
    lines = fixture.read_text(encoding="utf-8").splitlines()
    supervisor = IngestionSupervisor(
        settings=settings,
        source=lines,
        store=store,
        parquet=buffer,
        clock=clock,
        sleep=lambda _seconds: None,
    )
    original_connect = store._connect
    state = {"fail": True}

    def flaky_connect():  # type: ignore[no-untyped-def]
        if state["fail"]:
            state["fail"] = False
            raise sqlite3.OperationalError("database is locked")
        return original_connect()

    monkeypatch.setattr(store, "_connect", flaky_connect)

    with pytest.raises(StoreUnavailableError):
        supervisor.run()
    assert supervisor.state.value == "FAILED"
    assert supervisor.checkpoint.load() == {}

    supervisor._drain_all()
    manifest = [row for row in store.list_manifest() if row.data_type == data_type]
    assert manifest
    before = list(store.list_manifest())
    supervisor._drain_all()
    assert store.list_manifest() == tuple(before)
