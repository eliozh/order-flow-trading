"""Deterministic replay harness.

The harness runs the *same* pure metric functions as live, but it never opens
the daemon store, never reads Parquet, and never calls the wall clock. It reads
one event source, advances a :class:`~oft.replay.clock.ReplayClock` in event
time, and writes one immutable JSON document under
``$OFT_DATA/replay/<symbol>/<period>/<metric_version>/<input_manifest_hash>.json``.

Invariants (ADR-0011, slice 10):

* same events + same metric version -> identical metric hash;
* a different metric version -> a new path; existing files are never edited;
* re-running the same inputs is idempotent;
* a computation that is not deterministic fails loudly;
* nothing outside the replay namespace is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from oft.application.metric_service import (
    SCHEMA_VERSION,
    MetricRequest,
    input_manifest_hash,
)
from oft.domain.events import AggTrade
from oft.domain.metrics.cvd import compute_cvd
from oft.domain.metrics.footprint import (
    DEFAULT_IMBALANCE_THRESHOLD,
    DEFAULT_STACKED_MIN_LEVELS,
    DEFAULT_TOP_IMBALANCES,
    compute_footprint,
)
from oft.domain.metrics.liquidations import LIMITATIONS
from oft.domain.metrics.types import Unit, contract_json, decimal_str
from oft.domain.metrics.version import MetricVersion, orderflow_metric_version
from oft.domain.metrics.volume_profile import (
    DEFAULT_VALUE_AREA_PCT,
    compute_volume_profile,
)
from oft.domain.time import utc_timestamp
from oft.replay.clock import ReplayClock
from oft.replay.event_source import ReplayEventSource, ReplayWindow

REPLAY_NAMESPACE = "replay"


class ReplayError(ValueError):
    """The replay configuration or event source is unusable."""


class ReplayNonDeterministicError(ReplayError):
    """Two identical computations disagreed; replay must fail loudly."""


@dataclass(frozen=True)
class ReplayConfig:
    """Inputs that fully determine one replay document."""

    symbol: str
    period: str
    window_start: datetime
    window_end: datetime
    tick_size: Decimal
    unit: Unit = "BASE"
    bin_size_multiple: int = 1
    value_area_pct: Decimal = DEFAULT_VALUE_AREA_PCT
    cvd_algorithm_version: int = 1
    volume_profile_algorithm_version: int = 1
    footprint_algorithm_version: int = 1
    open_interest_algorithm_version: int = 1
    funding_algorithm_version: int = 1
    liquidation_algorithm_version: int = 1

    def __post_init__(self) -> None:
        ReplayWindow(symbol=self.symbol, start=self.window_start, end=self.window_end)
        if not isinstance(self.tick_size, Decimal) or not self.tick_size.is_finite():
            raise ReplayError("tick_size must be a finite Decimal")
        if self.tick_size <= 0:
            raise ReplayError("tick_size must be positive")


@dataclass(frozen=True)
class ReplayResult:
    """The identity and location of one deterministic replay document."""

    metric_version: str
    input_manifest_hash: str
    metric_hash: str
    computed_at: str
    event_count: int
    data_quality: str
    output_path: str
    idempotent: bool = False


@dataclass(frozen=True)
class _Computation:
    metric_version: str
    input_manifest_hash: str
    metric_hash: str
    json_text: str
    payload: dict[str, Any]
    computed_at: str
    event_count: int


def _cvd_payload(cvd: Any) -> dict[str, Any]:
    return {
        "unit": cvd.unit,
        "points": [
            {
                "bar_start": utc_timestamp(point.bar_start),
                "bar_end": utc_timestamp(point.bar_end),
                "buy_volume": decimal_str(point.buy_volume),
                "sell_volume": decimal_str(point.sell_volume),
                "delta": decimal_str(point.delta),
                "cvd": decimal_str(point.cvd),
                "taker_buy_ratio": point.taker_buy_ratio,
            }
            for point in cvd.points
        ],
        "summary": {
            "delta": decimal_str(cvd.summary.delta),
            "cvd_change": decimal_str(cvd.summary.cvd_change),
            "buy_volume": decimal_str(cvd.summary.buy_volume),
            "sell_volume": decimal_str(cvd.summary.sell_volume),
        },
        "divergence_flags": [
            {
                "bar_start": utc_timestamp(flag.bar_start),
                "kind": flag.kind,
                "note": flag.note,
            }
            for flag in cvd.divergence_flags
        ],
    }


def _volume_profile_payload(profile: Any) -> dict[str, Any]:
    return {
        "window": {
            "start": utc_timestamp(profile.window.start),
            "end": utc_timestamp(profile.window.end),
        },
        "bin_size": decimal_str(profile.bin_size),
        "value_area_pct": profile.value_area_pct,
        "poc": decimal_str(profile.poc),
        "vah": decimal_str(profile.vah),
        "val": decimal_str(profile.val),
        "bins": [
            {
                "price_low": decimal_str(bin_.price_low),
                "price_high": decimal_str(bin_.price_high),
                "volume": decimal_str(bin_.volume),
                "buy_volume": decimal_str(bin_.buy_volume),
                "sell_volume": decimal_str(bin_.sell_volume),
                "is_poc": bin_.is_poc,
                "in_value_area": bin_.in_value_area,
            }
            for bin_ in profile.bins
        ],
        "notes": list(profile.notes),
    }


def _empty_open_interest(window_end: datetime) -> dict[str, Any]:
    return {
        "unit": "BASE",
        "sampling": "REGULAR",
        "points": [],
        "delta_over_window": "0",
        "last": {
            "ts": utc_timestamp(window_end),
            "open_interest": "0",
            "open_interest_value": "0",
        },
    }


def _empty_funding(window_end: datetime) -> dict[str, Any]:
    return {
        "current_rate": "0",
        "next_funding_time": utc_timestamp(window_end),
        "interval_hours": 8,
        "mark_price": "0",
        "index_price": "0",
        "history": [],
    }


def _empty_liquidations() -> dict[str, Any]:
    return {
        "coverage": "NONE",
        "coverage_start": None,
        "coverage_end": None,
        "total_long_notional": "0",
        "total_short_notional": "0",
        "count": 0,
        "buckets": [],
        "limitations": list(LIMITATIONS),
    }


class ReplayHarness:
    """Compute and publish one deterministic replay document."""

    def __init__(
        self,
        *,
        data_dir: Path,
        source: ReplayEventSource,
        config: ReplayConfig,
    ) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve()
        self._source = source
        self._config = config
        self._replay_root = self._data_dir / REPLAY_NAMESPACE

    @property
    def replay_root(self) -> Path:
        return self._replay_root

    def _request(self) -> MetricRequest:
        return MetricRequest(
            symbol=self._config.symbol,
            period=self._config.period,
            window_start=self._config.window_start,
            window_end=self._config.window_end,
            tick_size=self._config.tick_size,
            bin_size_multiple=self._config.bin_size_multiple,
            value_area_pct=self._config.value_area_pct,
            unit=self._config.unit,
        )

    def _version(self) -> MetricVersion:
        return orderflow_metric_version(
            period=self._config.period,
            unit=self._config.unit,
            tick_size=self._config.tick_size,
            value_area_pct=self._config.value_area_pct,
            bin_size_multiple=self._config.bin_size_multiple,
            cvd_algorithm_version=self._config.cvd_algorithm_version,
            volume_profile_algorithm_version=self._config.volume_profile_algorithm_version,
            footprint_algorithm_version=self._config.footprint_algorithm_version,
            open_interest_algorithm_version=self._config.open_interest_algorithm_version,
            funding_algorithm_version=self._config.funding_algorithm_version,
            liquidation_algorithm_version=self._config.liquidation_algorithm_version,
            footprint_imbalance_threshold=DEFAULT_IMBALANCE_THRESHOLD,
            footprint_stacked_min_levels=DEFAULT_STACKED_MIN_LEVELS,
            footprint_top_imbalances=DEFAULT_TOP_IMBALANCES,
        )

    def _payload(
        self,
        request: MetricRequest,
        version: MetricVersion,
        input_hash: str,
        computed_at: str,
        events: tuple[AggTrade, ...],
    ) -> dict[str, Any]:
        cvd = compute_cvd(
            events,
            symbol=request.symbol,
            period=request.period,
            window_start=request.window_start,
            window_end=request.window_end,
            unit=request.unit,
        )
        footprint = compute_footprint(
            events,
            symbol=request.symbol,
            period=request.period,
            window_start=request.window_start,
            window_end=request.window_end,
            tick_size=request.tick_size,
            bucket_multiple=request.bin_size_multiple,
            imbalance_threshold=DEFAULT_IMBALANCE_THRESHOLD,
            stacked_min_levels=DEFAULT_STACKED_MIN_LEVELS,
            top_imbalances=DEFAULT_TOP_IMBALANCES,
        )
        profile = compute_volume_profile(
            events,
            symbol=request.symbol,
            window_start=request.window_start,
            window_end=request.window_end,
            tick_size=request.tick_size,
            bin_size_multiple=request.bin_size_multiple,
            value_area_pct=request.value_area_pct,
        )
        if events:
            data_quality = "PARTIAL"
            quality_reasons = [
                "open_interest not measured in this slice",
                "funding not measured in this slice",
            ]
        else:
            data_quality = "MISSING"
            quality_reasons = ["no aggTrade rows in the replay window"]
        return {
            "schema_version": SCHEMA_VERSION,
            "symbol": request.symbol,
            "period": request.period,
            "window": {
                "start": utc_timestamp(request.window_start),
                "end": utc_timestamp(request.window_end),
            },
            "computed_at": computed_at,
            "metric_version": version.identifier,
            "input_manifest_hash": input_hash,
            "data_quality": data_quality,
            "quality_reasons": quality_reasons,
            "cvd": _cvd_payload(cvd),
            "footprint": footprint.to_contract(),
            "volume_profile": _volume_profile_payload(profile),
            "open_interest": _empty_open_interest(request.window_end),
            "funding": _empty_funding(request.window_end),
            "liquidations": _empty_liquidations(),
            "sources": [
                {
                    "kind": "REPLAY",
                    "ref": (
                        f"{request.symbol}/{request.period}/{utc_timestamp(request.window_start)}"
                    ),
                    "as_of": computed_at,
                    "version": version.identifier,
                    "hash": input_hash,
                }
            ],
            "gaps": [],
        }

    def _compute(
        self,
        events: tuple[AggTrade, ...],
        request: MetricRequest,
        version: MetricVersion,
        input_hash: str,
        computed_at: str,
    ) -> _Computation:
        payload = self._payload(request, version, input_hash, computed_at, events)
        json_text = contract_json(payload)
        metric_hash = hashlib.sha256(json_text.encode("utf-8")).hexdigest()
        return _Computation(
            metric_version=version.identifier,
            input_manifest_hash=input_hash,
            metric_hash=metric_hash,
            json_text=json_text,
            payload=payload,
            computed_at=computed_at,
            event_count=len(events),
        )

    def _output_path(self, computation: _Computation) -> Path:
        path = (
            self._replay_root
            / self._config.symbol
            / self._config.period
            / computation.metric_version
            / f"{computation.input_manifest_hash}.json"
        )
        resolved = path.resolve()
        if not resolved.is_relative_to(self._replay_root):
            raise ReplayError("replay output escaped the replay namespace")
        return resolved

    def _record(self, computation: _Computation) -> str:
        return contract_json(
            {
                "metric_hash": computation.metric_hash,
                "metric_version": computation.metric_version,
                "input_manifest_hash": computation.input_manifest_hash,
                "computed_at": computation.computed_at,
                "event_count": computation.event_count,
                "data_quality": computation.payload["data_quality"],
                "document": computation.payload,
            }
        )

    def run(self) -> ReplayResult:
        """Replay the configured window, write once, and return its identity."""
        if (
            self._source.window.symbol != self._config.symbol
            or self._source.window.start != self._config.window_start
            or self._source.window.end != self._config.window_end
        ):
            raise ReplayError("event source window does not match the replay config")

        events = tuple(self._source.iter_events())
        clock = ReplayClock(self._config.window_start)
        for event in events:
            clock.advance_to(event.transact_time)
        computed_at = utc_timestamp(clock.now())

        request = self._request()
        version = self._version()
        input_hash = input_manifest_hash(events, request)
        first = self._compute(events, request, version, input_hash, computed_at)
        second = self._compute(events, request, version, input_hash, computed_at)
        if first.metric_hash != second.metric_hash:
            raise ReplayNonDeterministicError(
                "replay computation is non-deterministic: "
                f"{first.metric_hash} != {second.metric_hash}"
            )

        output_path = self._output_path(first)
        if output_path.exists():
            try:
                existing = json.loads(output_path.read_text(encoding="utf-8"), parse_float=Decimal)
            except (OSError, json.JSONDecodeError) as exc:
                raise ReplayNonDeterministicError(
                    f"existing replay document is unreadable: {output_path.name}"
                ) from exc
            if not isinstance(existing, dict):
                raise ReplayNonDeterministicError("existing replay document is not a JSON object")
            existing_hash = existing.get("metric_hash")
            existing_document = existing.get("document")
            if not isinstance(existing_document, dict):
                raise ReplayNonDeterministicError("existing replay document has no document body")
            recomputed_hash = hashlib.sha256(
                contract_json(existing_document).encode("utf-8")
            ).hexdigest()
            if existing_hash != recomputed_hash or existing_hash != first.metric_hash:
                raise ReplayNonDeterministicError(
                    "existing replay document is tampered or has a different metric hash; "
                    "refusing to edit it in place"
                )
            return ReplayResult(
                metric_version=first.metric_version,
                input_manifest_hash=first.input_manifest_hash,
                metric_hash=first.metric_hash,
                computed_at=first.computed_at,
                event_count=first.event_count,
                data_quality=first.payload["data_quality"],
                output_path=str(output_path),
                idempotent=True,
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(self._record(first))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, output_path)
            directory_fd = os.open(output_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
        return ReplayResult(
            metric_version=first.metric_version,
            input_manifest_hash=first.input_manifest_hash,
            metric_hash=first.metric_hash,
            computed_at=first.computed_at,
            event_count=first.event_count,
            data_quality=first.payload["data_quality"],
            output_path=str(output_path),
        )
