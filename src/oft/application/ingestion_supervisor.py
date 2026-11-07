"""Live ingestion supervisor: bounded buffering, dedupe, reconnect, checkpoints.

The supervisor is deliberately synchronous and dependency-injected so the
recorded fixtures replay deterministically with no sockets. It treats every
stream message as hostile: parsing/validation failures are counted, duplicates
and out-of-order ids are quarantined, and backpressure either spills to a
bounded WAL or pauses reads -- it never drops silently.
"""

from __future__ import annotations

import json
import os
import random
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

import structlog

from oft.adapters.binance_ws import MessageTooLargeError
from oft.adapters.parquet_store import LiveParquetBuffer
from oft.application.freshness import (
    KLINE_1M_STREAM,
    MARK_STREAM,
    FreshnessGuard,
    FreshnessStamp,
)
from oft.config import Settings
from oft.domain.data_quality import DataGap, GapSeverity
from oft.domain.events import (
    AggTrade,
    EventValidationError,
    ForceOrderEvent,
    KlineEvent,
    MarkPriceEvent,
    NormalizedEvent,
    event_from_dict,
    event_stream_name,
    event_to_dict,
    normalize_event,
    parse_stream_message,
    strip_control_characters,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.market_data import AggTradeRecoveryPort, MarketDataStreamPort
from oft.ports.store import StorePort

logger = structlog.get_logger(__name__)

DEFAULT_QUEUE_SIZE = 1_000
DEFAULT_MAX_WAL_BYTES = 8 * 1024 * 1024
DEFAULT_QUARANTINE_BYTES = 4 * 1024 * 1024
DEFAULT_CHECKPOINT_STALE_SECONDS = 15 * 60
DEFAULT_BACKOFF_BASE = 0.5
DEFAULT_BACKOFF_MAX = 30.0
DEFAULT_MAX_RECONNECT_ATTEMPTS = 3
RUN_DIRECTORY = "run"
CHECKPOINT_FILENAME = "ingest_checkpoints.json"
WAL_FILENAME = "ingest.wal"
QUARANTINE_FILENAME = "quarantine.jsonl"
WS_STATE_FILENAME = "ws_state.json"


class StreamDisconnected(Exception):
    """The injected stream ended or the socket closed; trigger reconnect."""


class WalFullError(Exception):
    """The bounded WAL cannot accept another event; the reader must pause."""

    code = "DATA_UNAVAILABLE"


class BackpressureError(Exception):
    """Draining did not restore capacity; stopping is safer than dropping."""

    code = "DATA_UNAVAILABLE"


class ConnectionState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    SUBSCRIBED = "SUBSCRIBED"
    RECONNECTING = "RECONNECTING"
    REPLAY = "REPLAY"
    IDLE = "IDLE"
    FAILED = "FAILED"
    CLOSED = "CLOSED"


class DedupVerdict(StrEnum):
    NEW = "NEW"
    DUPLICATE = "DUPLICATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"


class EnqueueOutcome(StrEnum):
    QUEUED = "QUEUED"
    SPILLED = "SPILLED"


@dataclass(frozen=True)
class IngestStats:
    """Exported counters; every rejected/spilled event is accounted for."""

    received: int
    accepted: int
    duplicates: int
    out_of_order: int
    malformed: int
    oversized: int
    ignored: int
    queued: int
    spilled: int
    blocked: int
    dropped: int
    persisted_rows: int
    flushes: int
    reconnects: int
    recoveries: int
    recovery_trades: int
    gaps_recorded: int
    wal_bytes: int
    clock_skew: int = 0


@dataclass(frozen=True)
class CheckpointEntry:
    stream: str
    last_event_time: datetime | None
    last_event_id: int | None
    updated_at: datetime


@dataclass(frozen=True)
class CheckpointDiagnostic:
    present: bool
    path: str
    age_seconds: float | None
    stale: bool
    streams: Mapping[str, CheckpointEntry]
    updated_at: datetime | None


def _run_dir(settings: Settings) -> Path:
    return settings.data_dir / RUN_DIRECTORY


class CheckpointStore:
    """Atomic per-stream checkpoint JSON (last event time/id)."""

    def __init__(self, path: Path, clock: ClockPort) -> None:
        self._path = path
        self._clock = clock

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, CheckpointEntry]:
        if not self._path.is_file():
            return {}
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("ingest_checkpoint_unreadable", path=str(self._path))
            return {}
        if not isinstance(document, dict) or not isinstance(document.get("streams"), dict):
            return {}
        entries: dict[str, CheckpointEntry] = {}
        for stream, raw in document["streams"].items():
            if not isinstance(raw, dict):
                continue
            try:
                event_time = (
                    datetime.fromisoformat(str(raw["last_event_time"]).replace("Z", "+00:00"))
                    if raw.get("last_event_time")
                    else None
                )
                updated_at = datetime.fromisoformat(
                    str(raw.get("updated_at", "1970-01-01T00:00:00+00:00")).replace("Z", "+00:00")
                )
                raw_id = raw.get("last_event_id")
                last_event_id = int(raw_id) if raw_id is not None else None
            except (KeyError, ValueError, TypeError):
                continue
            if updated_at.tzinfo is None or (event_time is not None and event_time.tzinfo is None):
                continue
            entries[stream] = CheckpointEntry(
                stream=stream,
                last_event_time=event_time,
                last_event_id=last_event_id,
                updated_at=updated_at,
            )
        return entries

    def save(self, entries: Mapping[str, CheckpointEntry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "version": 1,
            "saved_at": utc_timestamp(self._clock.now()),
            "streams": {
                stream: {
                    "last_event_time": (
                        utc_timestamp(entry.last_event_time)
                        if entry.last_event_time is not None
                        else None
                    ),
                    "last_event_id": entry.last_event_id,
                    "updated_at": utc_timestamp(entry.updated_at),
                }
                for stream, entry in entries.items()
            },
        }
        payload = json.dumps(document, sort_keys=True)
        temp_path = self._path.with_name(f".{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
            _fsync_directory(self._path.parent)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def update(
        self,
        stream: str,
        *,
        event_time: datetime | None,
        event_id: int | None,
    ) -> CheckpointEntry:
        entries = self.load()
        entry = CheckpointEntry(
            stream=stream,
            last_event_time=event_time,
            last_event_id=event_id,
            updated_at=self._clock.now(),
        )
        entries[stream] = entry
        self.save(entries)
        return entry

    def age_seconds(self, now: datetime | None = None) -> float | None:
        entries = self.load()
        if not entries:
            return None
        newest = max(entry.updated_at for entry in entries.values())
        return ((now or self._clock.now()) - newest).total_seconds()


def checkpoint_diagnostic(
    settings: Settings,
    clock: ClockPort,
    *,
    max_age_seconds: float = DEFAULT_CHECKPOINT_STALE_SECONDS,
) -> CheckpointDiagnostic:
    """Read-only checkpoint health used by ``oft doctor``."""
    store = CheckpointStore(_run_dir(settings) / CHECKPOINT_FILENAME, clock)
    entries = store.load()
    if not entries:
        return CheckpointDiagnostic(
            present=False,
            path=str(store.path),
            age_seconds=None,
            stale=False,
            streams={},
            updated_at=None,
        )
    newest = max(entry.updated_at for entry in entries.values())
    age = (clock.now() - newest).total_seconds()
    return CheckpointDiagnostic(
        present=True,
        path=str(store.path),
        age_seconds=age,
        stale=age > max_age_seconds,
        streams=entries,
        updated_at=newest,
    )


class QuarantineWriter:
    """Append dropped duplicates/out-of-order events to a bounded file."""

    def __init__(self, path: Path, *, max_bytes: int = DEFAULT_QUARANTINE_BYTES) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._counters: dict[str, int] = {
            "duplicates": 0,
            "out_of_order": 0,
            "rejected": 0,
            "bytes": 0,
        }

    @property
    def counters(self) -> dict[str, int]:
        return dict(self._counters)

    @property
    def path(self) -> Path:
        return self._path

    def write(self, event: NormalizedEvent, *, stream: str, reason: str) -> None:
        line = json.dumps(
            {"stream": stream, "reason": reason, "event": event_to_dict(event)},
            ensure_ascii=False,
            sort_keys=True,
        )
        size = len(line.encode("utf-8")) + 1
        if self._counters["bytes"] + size > self._max_bytes:
            self._counters["rejected"] += 1
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        self._counters["bytes"] += size
        if reason == "DUPLICATE":
            self._counters["duplicates"] += 1
        elif reason == "OUT_OF_ORDER":
            self._counters["out_of_order"] += 1


class DedupeGuard:
    """Per-stream duplicate/out-of-order detection with bounded memory."""

    def __init__(self, *, max_recent_ids: int = 10_000) -> None:
        self._max_recent_ids = max_recent_ids
        self._last: dict[str, tuple[int | None, datetime | None]] = {}
        self._recent_ids: dict[str, deque[int]] = {}
        self._recent_sets: dict[str, set[int]] = {}

    def seed(
        self,
        stream: str,
        *,
        event_time: datetime | None,
        event_id: int | None,
    ) -> None:
        self._last[stream] = (event_id, event_time)
        if event_id is not None:
            self._remember(stream, event_id)

    def _remember(self, stream: str, event_id: int) -> None:
        recent = self._recent_ids.setdefault(stream, deque(maxlen=self._max_recent_ids))
        known = self._recent_sets.setdefault(stream, set())
        if recent.maxlen is not None and len(recent) == recent.maxlen and recent:
            evicted = recent[0]
            known.discard(evicted)
        recent.append(event_id)
        known.add(event_id)

    def classify(self, event: NormalizedEvent, stream: str) -> DedupVerdict:
        last_id, last_time = self._last.get(stream, (None, None))
        if isinstance(event, AggTrade):
            event_id = event.agg_trade_id
            known = self._recent_sets.get(stream, set())
            if event_id == last_id or event_id in known:
                return DedupVerdict.DUPLICATE
            if last_id is not None and event_id < last_id:
                return DedupVerdict.OUT_OF_ORDER
            self._last[stream] = (event_id, event.transact_time)
            self._remember(stream, event_id)
            return DedupVerdict.NEW

        event_time = self._event_time(event)
        if last_time is not None and event_time is not None:
            if event_time == last_time:
                return DedupVerdict.DUPLICATE
            if event_time < last_time:
                return DedupVerdict.OUT_OF_ORDER
        self._last[stream] = (last_id, event_time)
        return DedupVerdict.NEW

    def _event_time(self, event: NormalizedEvent) -> datetime | None:
        if isinstance(event, (KlineEvent, MarkPriceEvent)):
            return event.event_time
        if isinstance(event, ForceOrderEvent):
            return event.trade_time
        return None

    def last_event_id(self, stream: str) -> int | None:
        return self._last.get(stream, (None, None))[0]

    def last_event_time(self, stream: str) -> datetime | None:
        return self._last.get(stream, (None, None))[1]


class BoundedEventQueue:
    """Bounded in-memory queue; overflow spills to an fsynced WAL file."""

    def __init__(
        self,
        *,
        maxsize: int,
        wal_path: Path,
        max_wal_bytes: int = DEFAULT_MAX_WAL_BYTES,
        clock: ClockPort,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        if max_wal_bytes <= 0:
            raise ValueError("max_wal_bytes must be positive")
        self._maxsize = maxsize
        self._wal_path = wal_path
        self._max_wal_bytes = max_wal_bytes
        self._clock = clock
        self._items: deque[NormalizedEvent] = deque()
        self._wal_bytes = 0
        self._counters = {
            "queued": 0,
            "spilled": 0,
            "blocked": 0,
            "dropped": 0,
            "replayed": 0,
        }
        wal_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def stats(self) -> dict[str, int]:
        return {
            **self._counters,
            "wal_bytes": self._wal_bytes,
            "depth": len(self._items),
        }

    def __len__(self) -> int:
        return len(self._items)

    @property
    def has_capacity(self) -> bool:
        return len(self._items) < self._maxsize

    def put(self, event: NormalizedEvent) -> EnqueueOutcome:
        if self.has_capacity:
            self._items.append(event)
            self._counters["queued"] += 1
            return EnqueueOutcome.QUEUED
        line = json.dumps(event_to_dict(event), ensure_ascii=False, sort_keys=True) + "\n"
        size = len(line.encode("utf-8"))
        if self._wal_bytes + size > self._max_wal_bytes:
            self._counters["blocked"] += 1
            raise WalFullError("bounded WAL is full; the reader must pause")
        with self._wal_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self._wal_bytes += size
        self._counters["spilled"] += 1
        return EnqueueOutcome.SPILLED

    def get_nowait(self) -> NormalizedEvent | None:
        return self._items.popleft() if self._items else None

    def replay_wal(self) -> tuple[NormalizedEvent, ...]:
        if not self._wal_path.is_file():
            return ()
        events: list[NormalizedEvent] = []
        for line in self._wal_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                document = json.loads(line)
                events.append(event_from_dict(document))
                self._counters["replayed"] += 1
            except (json.JSONDecodeError, RecursionError, EventValidationError):
                # A torn WAL line is skipped; counters still show the spill.
                continue
        return tuple(events)

    def clear_wal(self) -> None:
        if self._wal_path.is_file():
            self._wal_path.write_text("", encoding="utf-8")
        self._wal_bytes = 0


class WsRuntimeState:
    """Read model of the last written WS state file."""

    def __init__(
        self,
        *,
        connection_state: str,
        streams: Mapping[str, Mapping[str, object]],
        freshness: Mapping[str, Mapping[str, object]],
        updated_at: str,
        stale: bool,
        stats: Mapping[str, int] | None = None,
    ) -> None:
        self.connection_state = connection_state
        self.streams = {key: dict(value) for key, value in streams.items()}
        self.freshness = {key: dict(value) for key, value in freshness.items()}
        self.updated_at = updated_at
        self.stale = stale
        self.stats = dict(stats or {})

    def to_dict(self) -> dict[str, object]:
        return {
            "connection_state": self.connection_state,
            "streams": self.streams,
            "freshness": self.freshness,
            "updated_at": self.updated_at,
            "stale": self.stale,
            "stats": self.stats,
        }


class WsStateStore:
    """Atomic writer for the daemon-independent ``oft status`` WS surface."""

    def __init__(self, path: Path, clock: ClockPort) -> None:
        self._path = path
        self._clock = clock

    @property
    def path(self) -> Path:
        return self._path

    def write(
        self,
        connection_state: str,
        streams: Mapping[str, Mapping[str, object]],
        freshness: Mapping[str, Mapping[str, object]],
        stats: Mapping[str, int] | None = None,
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "version": 1,
            "connection_state": connection_state,
            "streams": {key: dict(value) for key, value in streams.items()},
            "freshness": {key: dict(value) for key, value in freshness.items()},
            "stats": dict(stats or {}),
            "updated_at": utc_timestamp(self._clock.now()),
            "stale": any(bool(value.get("stale")) for value in freshness.values()),
        }
        temp_path = self._path.with_name(f".{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
            with temp_path.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
            _fsync_directory(self._path.parent)
        finally:
            if temp_path.exists():
                temp_path.unlink()


def read_ws_state(settings: Settings) -> WsRuntimeState | None:
    path = _run_dir(settings) / WS_STATE_FILENAME
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    streams = document.get("streams")
    freshness = document.get("freshness")
    raw_stats = document.get("stats")
    stats = (
        {str(key): int(value) for key, value in raw_stats.items() if isinstance(value, int)}
        if isinstance(raw_stats, dict)
        else {}
    )
    return WsRuntimeState(
        connection_state=str(document.get("connection_state", "UNKNOWN")),
        streams=streams if isinstance(streams, dict) else {},
        freshness=freshness if isinstance(freshness, dict) else {},
        updated_at=str(document.get("updated_at", "")),
        stale=bool(document.get("stale", False)),
        stats=stats,
    )


def reconnect_delay(
    attempt: int,
    *,
    base: float = DEFAULT_BACKOFF_BASE,
    max_delay: float = DEFAULT_BACKOFF_MAX,
    jitter: Callable[[], float] = random.random,
) -> float:
    """Exponential backoff with additive jitter, capped at ``max_delay + base``."""
    exponent = max(0, attempt)
    return float(min(max_delay, base * (2**exponent)) + jitter() * base)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _event_time(event: NormalizedEvent) -> datetime | None:
    if isinstance(event, AggTrade):
        return event.transact_time
    if isinstance(event, (KlineEvent, MarkPriceEvent)):
        return event.event_time
    return event.trade_time


def _event_id(event: NormalizedEvent) -> int | None:
    return event.agg_trade_id if isinstance(event, AggTrade) else None


def _safe_reason(exc: BaseException) -> str:
    """Upstream text is untrusted: strip control/invisible characters for logs."""
    return strip_control_characters(str(exc))[:200]


class IngestionSupervisor:
    """Owns one WS session: connect, buffer, persist, checkpoint, recover."""

    def __init__(
        self,
        *,
        settings: Settings,
        source: Iterable[str],
        store: StorePort,
        parquet: LiveParquetBuffer,
        clock: ClockPort,
        client: MarketDataStreamPort | None = None,
        recovery: AggTradeRecoveryPort | None = None,
        symbols: Sequence[str] = ("BTCUSDT",),
        streams: Sequence[str] | None = None,
        queue: BoundedEventQueue | None = None,
        checkpoint: CheckpointStore | None = None,
        dedupe: DedupeGuard | None = None,
        quarantine: QuarantineWriter | None = None,
        state_store: WsStateStore | None = None,
        freshness: FreshnessGuard | None = None,
        max_message_bytes: int = 64 * 1024,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        jitter: Callable[[], float] = random.random,
        sleep: Callable[[float], None] = time.sleep,
        max_reconnect_attempts: int = DEFAULT_MAX_RECONNECT_ATTEMPTS,
        symbol_provider: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        run_dir = _run_dir(settings)
        self._settings = settings
        self._source = source
        self._store = store
        self._parquet = parquet
        self._clock = clock
        self._client = client
        self._recovery = recovery
        self._symbols = tuple(symbols)
        self._explicit_streams = streams is not None
        self._streams = tuple(streams) if streams is not None else _default_streams(symbols)
        self._symbol_provider = symbol_provider
        self._queue = (
            queue
            if queue is not None
            else BoundedEventQueue(
                maxsize=DEFAULT_QUEUE_SIZE,
                wal_path=run_dir / WAL_FILENAME,
                clock=clock,
            )
        )
        self._checkpoint = (
            checkpoint
            if checkpoint is not None
            else CheckpointStore(run_dir / CHECKPOINT_FILENAME, clock)
        )
        self._dedupe = dedupe if dedupe is not None else DedupeGuard()
        self._quarantine = (
            quarantine
            if quarantine is not None
            else QuarantineWriter(run_dir / QUARANTINE_FILENAME)
        )
        self._state_store = (
            state_store
            if state_store is not None
            else WsStateStore(run_dir / WS_STATE_FILENAME, clock)
        )
        self._freshness = freshness if freshness is not None else FreshnessGuard(clock=clock)
        self._max_message_bytes = max_message_bytes
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._jitter = jitter
        self._sleep = sleep
        self._max_reconnect_attempts = max_reconnect_attempts
        self.state = ConnectionState.REPLAY if client is None else ConnectionState.DISCONNECTED
        self._checkpoint_entries: dict[str, CheckpointEntry] = {}
        self._state_streams: dict[str, dict[str, object]] = {}
        self._persisted_rows = 0
        self._counters: dict[str, int] = {
            "received": 0,
            "accepted": 0,
            "duplicates": 0,
            "out_of_order": 0,
            "malformed": 0,
            "oversized": 0,
            "ignored": 0,
            "reconnects": 0,
            "recoveries": 0,
            "recovery_trades": 0,
            "gaps_recorded": 0,
            "clock_skew": 0,
        }

    @property
    def streams(self) -> tuple[str, ...]:
        return self._streams

    @property
    def freshness(self) -> FreshnessGuard:
        return self._freshness

    @property
    def checkpoint(self) -> CheckpointStore:
        return self._checkpoint

    def run(self, *, max_events: int | None = None) -> IngestStats:
        """Replay/consume the source, then flush and persist a checkpoint."""
        self._checkpoint_entries = self._checkpoint.load()
        for stream, entry in self._checkpoint_entries.items():
            self._dedupe.seed(
                stream, event_time=entry.last_event_time, event_id=entry.last_event_id
            )
        disconnected = False
        if self._client is not None:
            self._connect_and_subscribe()
        self._write_state()
        try:
            for raw in self._source:
                self._counters["received"] += 1
                try:
                    self._handle_raw(raw)
                except Exception as exc:  # noqa: BLE001 - data must not kill the run
                    logger.warning("ws_event_internal_error", reason=_safe_reason(exc))
                    self._reject("INTERNAL_ERROR")
                if max_events is not None and self._counters["received"] >= max_events:
                    break
        except StreamDisconnected as exc:
            logger.warning("ws_stream_disconnected", reason=_safe_reason(exc))
            disconnected = True
        if disconnected:
            # Durable flush first; only then persist the checkpoint.
            self._drain_all()
            self._handle_disconnect()
        self._drain_all()
        self._save_checkpoint()
        self._write_state()
        return self.stats

    def run_forever(self, *, stop: Callable[[], bool] | None = None) -> IngestStats:
        """Drive a real socket until ``stop`` returns True, reconnecting in place."""
        if self._client is None:
            raise ValueError("run_forever requires a stream client")
        self._checkpoint_entries = self._checkpoint.load()
        for stream, entry in self._checkpoint_entries.items():
            self._dedupe.seed(
                stream, event_time=entry.last_event_time, event_id=entry.last_event_id
            )
        self._write_state()
        stopper = stop or (lambda: False)
        connected = False
        attempt = 0
        try:
            while not stopper():
                if not connected:
                    try:
                        if not self._connect_and_subscribe():
                            # Idle until stars change (or the daemon restarts).
                            self._sleep(max(0.05, self._backoff_base))
                            continue
                        connected = True
                        # 48h REST gap recovery on startup and every reconnect.
                        self._recover()
                        self._write_state()
                    except Exception as exc:
                        logger.warning(
                            "ws_startup_connect_failed", attempt=attempt, reason=_safe_reason(exc)
                        )
                        self.state = ConnectionState.RECONNECTING
                        self._write_state()
                        self._sleep(
                            reconnect_delay(
                                attempt,
                                base=self._backoff_base,
                                max_delay=self._backoff_max,
                                jitter=self._jitter,
                            )
                        )
                        attempt += 1
                        continue
                try:
                    stream, payload = self._client.receive_event()
                except MessageTooLargeError as exc:
                    # A too-large frame is a data problem: count it, then drop
                    # the connection and reconnect instead of hanging.
                    self._reject("OVERSIZED")
                    logger.warning("ws_message_oversized", reason=_safe_reason(exc))
                    connected = False
                    continue
                except EventValidationError as exc:
                    # Malformed/oversized payloads are data problems, not a
                    # dead socket: count them and keep the connection.
                    self._reject(exc.reason)
                    continue
                except Exception as exc:
                    logger.warning("ws_stream_disconnected", reason=_safe_reason(exc))
                    connected = False
                    self._counters["reconnects"] += 1
                    # Flush + fsync parts before the checkpoint advances.
                    self._drain_all()
                    self.state = ConnectionState.RECONNECTING
                    self._write_state()
                    self._sleep(
                        reconnect_delay(
                            attempt,
                            base=self._backoff_base,
                            max_delay=self._backoff_max,
                            jitter=self._jitter,
                        )
                    )
                    attempt += 1
                    continue
                attempt = 0  # a real event proves the connection is healthy
                raw = json.dumps({"stream": stream, "data": payload}, ensure_ascii=False)
                self._counters["received"] += 1
                try:
                    self._handle_raw(raw)
                except Exception as exc:  # noqa: BLE001 - data must not kill the thread
                    logger.warning("ws_event_internal_error", reason=_safe_reason(exc))
                    self._reject("INTERNAL_ERROR")
        finally:
            self._drain_all()
            self._save_checkpoint()
            self._write_state()
        return self.stats

    @property
    def stats(self) -> IngestStats:
        queue_stats = self._queue.stats
        buffer_stats = self._parquet.stats
        return IngestStats(
            received=self._counters["received"],
            accepted=self._counters["accepted"],
            duplicates=self._counters["duplicates"],
            out_of_order=self._counters["out_of_order"],
            malformed=self._counters["malformed"],
            oversized=self._counters["oversized"],
            ignored=self._counters["ignored"],
            queued=queue_stats["queued"],
            spilled=queue_stats["spilled"],
            blocked=queue_stats["blocked"] + self._counters.get("blocked", 0),
            dropped=queue_stats["dropped"],
            persisted_rows=self._persisted_rows,
            flushes=buffer_stats["flushes"],
            reconnects=self._counters["reconnects"],
            recoveries=self._counters["recoveries"],
            recovery_trades=self._counters["recovery_trades"],
            gaps_recorded=self._counters["gaps_recorded"],
            wal_bytes=queue_stats["wal_bytes"],
            clock_skew=self._counters["clock_skew"],
        )

    def evaluate_freshness(self, now: datetime | None = None) -> tuple[FreshnessStamp, ...]:
        stamps = self._freshness.evaluate(now)
        self._write_state(now)
        return stamps

    def _handle_raw(self, raw: str) -> None:
        try:
            stream, payload = parse_stream_message(raw, max_bytes=self._max_message_bytes)
        except EventValidationError as exc:
            self._reject(exc.reason)
            return
        try:
            event = normalize_event(payload)
        except EventValidationError as exc:
            self._reject(exc.reason)
            return
        if not stream:
            stream = event_stream_name(event)
        if self._streams and stream not in self._streams:
            self._counters["ignored"] += 1
            return
        self._accept(stream, event)

    def _reject(self, reason: str) -> None:
        if reason == "OVERSIZED":
            self._counters["oversized"] += 1
        else:
            self._counters["malformed"] += 1
        logger.warning("ws_event_rejected", reason=reason)

    def _accept(self, stream: str, event: NormalizedEvent) -> None:
        event_time = _event_time(event)
        if self._client is not None and event_time is not None:
            now = self._clock.now()
            if event_time > now + timedelta(minutes=5) or event_time < now - timedelta(hours=48):
                # Never let a skewed timestamp poison dedupe/recovery state.
                self._counters["clock_skew"] += 1
                self._quarantine.write(event, stream=stream, reason="CLOCK_SKEW")
                logger.warning("ws_event_clock_skew", stream=stream, reason="CLOCK_SKEW")
                return
        verdict = self._dedupe.classify(event, stream)
        if verdict is DedupVerdict.DUPLICATE:
            self._counters["duplicates"] += 1
            self._quarantine.write(event, stream=stream, reason="DUPLICATE")
            return
        if verdict is DedupVerdict.OUT_OF_ORDER:
            self._counters["out_of_order"] += 1
            self._quarantine.write(event, stream=stream, reason="OUT_OF_ORDER")
            return
        self._counters["accepted"] += 1
        self._update_checkpoint(stream, event)
        self._record_freshness(event)
        self._enqueue(event)

    def _record_freshness(self, event: NormalizedEvent) -> None:
        if isinstance(event, KlineEvent):
            self._freshness.record(KLINE_1M_STREAM, event.event_time, closes_at=event.close_time)
        elif isinstance(event, MarkPriceEvent):
            self._freshness.record(MARK_STREAM, event.event_time)

    def _update_checkpoint(self, stream: str, event: NormalizedEvent) -> None:
        event_time = _event_time(event)
        event_id = _event_id(event)
        entry = CheckpointEntry(
            stream=stream,
            last_event_time=event_time,
            last_event_id=event_id,
            updated_at=self._clock.now(),
        )
        self._checkpoint_entries[stream] = entry
        self._state_streams[stream] = {
            "symbol": event.symbol,
            "last_event_time": utc_timestamp(event_time) if event_time is not None else None,
            "last_event_id": event_id,
        }

    def _enqueue(self, event: NormalizedEvent) -> None:
        try:
            self._queue.put(event)
        except WalFullError:
            self._counters["blocked"] += 1
            self._relieve()
            try:
                self._queue.put(event)
            except WalFullError as exc:
                raise BackpressureError(
                    "queue and WAL are both full; stopping instead of dropping"
                ) from exc
        if self._queue.stats["spilled"] > 0 or self._is_flush_due():
            self._relieve()

    def _is_flush_due(self) -> bool:
        depth = self._queue.stats["depth"]
        return (
            self._parquet.is_due()
            or self._parquet.pending_rows + depth >= self._parquet.max_rows
            or self._parquet.should_flush()
        )

    def _relieve(self) -> None:
        while True:
            event = self._queue.get_nowait()
            if event is None:
                break
            self._parquet.add(event)
        for event in self._queue.replay_wal():
            self._parquet.add(event)
        self._queue.clear_wal()
        entries = self._parquet.maybe_flush(self._clock.now())
        self._persisted_rows += sum(entry.rows for entry in entries)
        if entries:
            # Fsync the parts before advancing the durable checkpoint.
            self._save_checkpoint()
            self._write_state()

    def _drain_all(self) -> None:
        self._relieve()
        entries = self._parquet.flush()
        self._persisted_rows += sum(entry.rows for entry in entries)
        if entries:
            self._save_checkpoint()
        self._write_state()

    def _save_checkpoint(self) -> None:
        try:
            self._checkpoint.save(self._checkpoint_entries)
        except OSError as exc:  # a checkpoint failure must not stop ingestion
            logger.warning("ingest_checkpoint_write_failed", reason=_safe_reason(exc))

    def _connect_and_subscribe(self) -> bool:
        """Connect; return False and enter IDLE when the active set is empty."""
        assert self._client is not None
        if self._symbol_provider is not None:
            # Star changes take effect on the next connect/reconnect.
            refreshed = tuple(self._symbol_provider())
            if refreshed != self._symbols:
                self._symbols = refreshed
                if not self._explicit_streams:
                    self._streams = _default_streams(refreshed)
        if not self._symbols:
            # Nothing starred and no explicit override: stay idle, no socket.
            self.state = ConnectionState.IDLE
            self._streams = ()
            self._state_streams = {}
            self._write_state()
            return False
        self.state = ConnectionState.CONNECTING
        self._client.connect(self._streams)
        self._client.subscribe(self._streams)
        self.state = ConnectionState.SUBSCRIBED
        return True

    def _handle_disconnect(self) -> None:
        self._counters["reconnects"] += 1
        self._checkpoint.save(self._checkpoint_entries)
        self.state = ConnectionState.RECONNECTING
        self._write_state()
        for attempt in range(self._max_reconnect_attempts):
            delay = reconnect_delay(
                attempt, base=self._backoff_base, max_delay=self._backoff_max, jitter=self._jitter
            )
            self._sleep(delay)
            try:
                self._connect_and_subscribe()
                self._recover()
                self._write_state()
                return
            except Exception as exc:
                logger.warning("ws_reconnect_failed", attempt=attempt, reason=_safe_reason(exc))
        self.state = ConnectionState.FAILED
        self._write_state()

    def _recover(self) -> None:
        if self._recovery is None:
            return
        now = self._clock.now()
        for symbol in self._symbols:
            stream = f"{symbol.lower()}@aggTrade"
            entry = self._checkpoint_entries.get(stream)
            last_event_time = entry.last_event_time if entry is not None else None
            try:
                result = self._recovery.recover_agg_trades(
                    symbol, last_event_time=last_event_time, now=now
                )
            except Exception as exc:
                logger.warning("ws_recovery_failed", symbol=symbol, reason=_safe_reason(exc))
                self._record_gap(
                    symbol,
                    start=last_event_time or now,
                    end=now,
                    resolution="RECOVERY_ERROR",
                    detected_at=now,
                )
                continue
            self._counters["recoveries"] += 1
            self._counters["recovery_trades"] += len(result.trades)
            for trade in result.trades:
                self._accept(stream, trade)
            if not result.complete:
                self._record_gap(
                    symbol,
                    start=result.start,
                    end=result.end,
                    resolution=result.reason or "INCOMPLETE",
                    detected_at=now,
                )

    def _record_gap(
        self,
        symbol: str,
        *,
        start: datetime,
        end: datetime,
        resolution: str,
        detected_at: datetime,
    ) -> None:
        detected = utc_timestamp(detected_at)
        gap = DataGap(
            gap_id=f"GAP-{symbol}-aggTrades-LIVE-{detected}",
            symbol=symbol,
            data_type="aggTrades",
            start=utc_timestamp(start),
            end=utc_timestamp(end),
            reason="LIVE_RECOVERY_INCOMPLETE",
            severity=GapSeverity.WARNING,
            detected_at=detected,
            source="LIVE_WS",
            resolution=resolution,
        )
        self._store.upsert_data_gap(gap)
        self._counters["gaps_recorded"] += 1

    def _write_state(self, now: datetime | None = None) -> None:
        try:
            # evaluate() emits stale/fresh transitions exactly once; snapshot()
            # alone would make those alerts unreachable in the live loop.
            self._freshness.evaluate(now)
            self._state_store.write(
                self.state.value,
                self._state_streams,
                self._freshness.snapshot(now),
                {**self.stats.__dict__},
            )
        except OSError as exc:  # status must never take the ingester down
            logger.warning("ws_state_write_failed", reason=_safe_reason(exc))


def _default_streams(symbols: Sequence[str]) -> tuple[str, ...]:
    suffixes = ("aggTrade", "kline_1m", "markPrice@1s", "forceOrder")
    return tuple(f"{symbol.lower()}@{suffix}" for symbol in symbols for suffix in suffixes)


__all__ = [
    "BackpressureError",
    "BoundedEventQueue",
    "CHECKPOINT_FILENAME",
    "CheckpointDiagnostic",
    "CheckpointEntry",
    "CheckpointStore",
    "ConnectionState",
    "DedupeGuard",
    "DedupVerdict",
    "EnqueueOutcome",
    "IngestStats",
    "IngestionSupervisor",
    "QuarantineWriter",
    "StreamDisconnected",
    "WalFullError",
    "WsRuntimeState",
    "WsStateStore",
    "checkpoint_diagnostic",
    "read_ws_state",
    "reconnect_delay",
]
