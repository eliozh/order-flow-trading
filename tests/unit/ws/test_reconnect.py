"""Reconnect, recovery, checkpoint, and stale-checkpoint diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from oft.adapters.fixture_market_data import FixtureMarketData
from oft.adapters.parquet_store import FlushPolicy, LiveParquetBuffer
from oft.adapters.sqlite_store import SqliteStore
from oft.application.health import DaemonProbe, build_doctor
from oft.application.ingestion_supervisor import (
    CHECKPOINT_FILENAME,
    CheckpointStore,
    ConnectionState,
    IngestionSupervisor,
    StreamDisconnected,
    checkpoint_diagnostic,
    reconnect_delay,
)
from oft.config import Settings
from oft.domain.events import AggTrade
from oft.ports.market_data import AggTradeRecovery

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ws"
STREAM = "btcusdt@aggTrade"


class MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant

    def advance(self, seconds: float) -> None:
        self.instant += timedelta(seconds=seconds)


def _trade(agg_trade_id: int, *, event_time: datetime = NOW) -> AggTrade:
    return AggTrade(
        symbol="BTCUSDT",
        agg_trade_id=agg_trade_id,
        price=Decimal("35000.00"),
        quantity=Decimal("0.100"),
        first_trade_id=agg_trade_id,
        last_trade_id=agg_trade_id,
        transact_time=event_time,
        is_buyer_maker=False,
    )


def _raw(agg_trade_id: int) -> str:
    return (
        f'{{"e":"aggTrade","E":1793966400000,"s":"BTCUSDT","a":{agg_trade_id},'
        f'"p":"35000.0","q":"0.1","f":{agg_trade_id},"l":{agg_trade_id},'
        f'"T":1793966400000,"m":false}}'
    )


class FakeClient:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.subscribe_calls = 0
        self.subscribed: tuple[str, ...] = ()
        self.closed = False
        self.counters: dict[str, int] = {}
        self.alerts: tuple[object, ...] = ()

    def connect(self, streams: tuple[str, ...]) -> None:
        self.connect_calls += 1

    def subscribe(self, streams: tuple[str, ...], *, request_id: int = 1) -> object:
        self.subscribe_calls += 1
        self.subscribed = streams
        return object()

    def receive_event(self, timeout: float | None = None) -> tuple[str, dict[str, object]]:
        raise StreamDisconnected("simulated socket close")

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeRecovery:
    result: AggTradeRecovery
    calls: list[tuple[str, datetime | None, datetime]] = field(default_factory=list)

    def recover_agg_trades(
        self,
        symbol: str,
        *,
        last_event_time: datetime | None,
        now: datetime,
        stop: object = None,
    ) -> AggTradeRecovery:
        self.calls.append((symbol, last_event_time, now))
        return self.result


class DisconnectingSource:
    def __init__(self, messages: list[str]) -> None:
        self._messages = messages

    def __iter__(self):
        yield from self._messages
        raise StreamDisconnected("simulated socket close")


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    return settings


def _supervisor(
    settings: Settings,
    clock: MutableClock,
    *,
    source,
    client: FakeClient | None = None,
    recovery: FakeRecovery | None = None,
    checkpoint: CheckpointStore | None = None,
    store: SqliteStore | None = None,
) -> IngestionSupervisor:
    store = store or SqliteStore(settings)
    buffer = LiveParquetBuffer(
        settings=settings,
        store=store,
        clock=clock,
        policy=FlushPolicy(max_rows=100, max_interval_seconds=30),
    )
    return IngestionSupervisor(
        settings=settings,
        source=source,
        store=store,
        parquet=buffer,
        clock=clock,
        client=client,
        recovery=recovery,
        checkpoint=checkpoint,
        sleep=lambda _seconds: None,
    )


def test_reconnect_delay_is_exponential_with_jitter_and_capped() -> None:
    delays = [reconnect_delay(attempt, jitter=lambda: 1.0) for attempt in range(8)]

    assert delays[0] == 1.0
    assert delays[1] == 1.5
    assert delays[2] == 2.5
    assert delays == sorted(delays)
    assert delays[-1] <= 30.5


def test_disconnect_reconnects_resubscribes_and_recovers(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock(NOW)
    client = FakeClient()
    recovery = FakeRecovery(
        AggTradeRecovery(
            trades=(_trade(11),),
            requests=1,
            weight_used=20,
            complete=True,
            reason=None,
            start=NOW - timedelta(hours=1),
            end=NOW,
        )
    )
    supervisor = _supervisor(
        settings,
        clock,
        source=DisconnectingSource([_raw(10)]),
        client=client,
        recovery=recovery,
    )

    stats = supervisor.run()

    assert client.connect_calls == 2
    assert client.subscribe_calls == 2
    assert STREAM in client.subscribed
    assert len(client.subscribed) == 4
    assert stats.reconnects == 1
    assert stats.recoveries == 1
    assert stats.recovery_trades == 1
    assert stats.persisted_rows == 2
    assert supervisor.state is ConnectionState.SUBSCRIBED


def test_incomplete_recovery_records_a_data_gap(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock(NOW)
    recovery = FakeRecovery(
        AggTradeRecovery(
            trades=(),
            requests=2,
            weight_used=40,
            complete=False,
            reason="RECOVERY_WINDOW_EXCEEDED_48H",
            start=NOW - timedelta(hours=49),
            end=NOW,
        )
    )
    supervisor = _supervisor(
        settings,
        clock,
        source=DisconnectingSource([]),
        client=FakeClient(),
        recovery=recovery,
    )

    stats = supervisor.run()

    gaps = SqliteStore(settings).list_data_gaps(symbol="BTCUSDT")
    assert stats.gaps_recorded == 1
    assert len(gaps) == 1
    assert gaps[0].reason == "LIVE_RECOVERY_INCOMPLETE"
    assert gaps[0].source == "LIVE_WS"
    assert "RECOVERY_WINDOW_EXCEEDED_48H" in (gaps[0].resolution or "")


def test_restart_from_checkpoint_dedupes_the_last_event_and_continues(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock(NOW)
    checkpoint = CheckpointStore(settings.data_dir / "run" / "checkpoints.json", clock)
    first = _supervisor(
        settings,
        clock,
        source=[_raw(105)],
        checkpoint=checkpoint,
    )
    first.run()

    second = _supervisor(
        settings,
        clock,
        source=[_raw(105), _raw(106)],
        checkpoint=CheckpointStore(settings.data_dir / "run" / "checkpoints.json", clock),
    )
    stats = second.run()

    assert stats.duplicates == 1
    assert stats.persisted_rows == 1
    loaded = checkpoint.load()
    assert loaded[STREAM].last_event_id == 106
    import pyarrow.dataset as dataset

    root = settings.data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT"
    table = dataset.dataset([str(path) for path in root.glob("date=*/*.parquet")]).to_table()
    assert sorted(table.column("agg_trade_id").to_pylist()) == [105, 106]


def test_stale_checkpoint_is_visible_in_doctor(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock(NOW)
    checkpoint = CheckpointStore(settings.data_dir / "run" / CHECKPOINT_FILENAME, clock)
    checkpoint.update(STREAM, event_time=NOW, event_id=9)

    clock.advance(3600)
    diagnostic = checkpoint_diagnostic(settings, clock, max_age_seconds=300)
    report = build_doctor(
        settings,
        SqliteStore(settings),
        FixtureMarketData(),
        clock,
        daemon=DaemonProbe(state="UP", detail="test"),
    )

    assert diagnostic.present is True
    assert diagnostic.stale is True
    checks = {check.name: check for check in report.checks}
    assert checks["ingest.checkpoint"].status == "degraded"
    assert "stale" in checks["ingest.checkpoint"].detail.lower()
