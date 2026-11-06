"""Compute versioned CVD and volume-profile documents from backfill Parquet.

The service is the boundary between the pure metric core and persistence. It
reads only published agg_trades parts, injects time through ``ClockPort``, and
renders exact-decimal contract JSON. No wall clock and no binary float are used
for metric values.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol, cast

import structlog

from oft.adapters.parquet_store import recover_orphan_metric_parts
from oft.config import Settings
from oft.domain.data_quality import DataGap, DataQuality
from oft.domain.events import AggTrade
from oft.domain.metrics.cvd import assert_kline_cross_check, compute_cvd
from oft.domain.metrics.footprint import (
    DEFAULT_IMBALANCE_THRESHOLD,
    DEFAULT_STACKED_MIN_LEVELS,
    DEFAULT_TOP_IMBALANCES,
    FootprintResult,
    compute_footprint,
)
from oft.domain.metrics.funding import FundingResult
from oft.domain.metrics.liquidations import (
    LIMITATIONS,
    LiquidationEvent,
    LiquidationResult,
    liquidation_input_hash,
)
from oft.domain.metrics.open_interest import OpenInterestResult
from oft.domain.metrics.types import (
    CvdResult,
    MetricQuality,
    MetricRow,
    MetricWindow,
    Unit,
    contract_json,
    decimal_str,
)
from oft.domain.metrics.version import MetricVersion, orderflow_metric_version
from oft.domain.metrics.volume_profile import (
    DEFAULT_VALUE_AREA_PCT,
    VolumeProfile,
    compute_volume_profile,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import ManifestEntry, StorePort, StoreUnavailableError

SCHEMA_VERSION = "1.0.0"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,24}$")


class MetricsWriterPort(Protocol):
    """Publishes immutable metrics, footprint, and liquidation Parquet parts."""

    def write_metrics_part(
        self,
        rows: tuple[MetricRow, ...] | list[MetricRow],
        *,
        symbol: str,
        period: str,
        window_start: datetime,
        window_end: datetime,
        metric_version: str,
        input_manifest_hash: str,
        computed_at: str,
        data_quality: str,
        quality_reasons: tuple[str, ...],
    ) -> ManifestEntry: ...

    def write_footprint_part(
        self,
        footprint: FootprintResult,
        *,
        symbol: str,
        period: str,
        window_start: datetime,
        window_end: datetime,
        metric_version: str,
        input_manifest_hash: str,
        computed_at: str,
        data_quality: str,
        quality_reasons: tuple[str, ...],
    ) -> ManifestEntry: ...

    def write_liquidation_part(
        self,
        events: Sequence[LiquidationEvent],
        *,
        symbol: str,
        window_start: datetime,
        window_end: datetime,
        metric_version: str,
        input_manifest_hash: str,
        computed_at: str,
        data_quality: str,
        quality_reasons: tuple[str, ...],
    ) -> ManifestEntry: ...


@dataclass(frozen=True)
class MetricInputs:
    """Measured inputs that are not derivable from the aggTrade row set.

    ``None`` means the section was not measured for this document; the service
    then emits its deterministic placeholder and a PARTIAL/MISSING reason.
    """

    open_interest: OpenInterestResult | None = None
    funding: FundingResult | None = None
    liquidations: LiquidationResult | None = None


_QUALITY_RANK: dict[DataQuality, int] = {
    DataQuality.COMPLETE: 0,
    DataQuality.PARTIAL: 1,
    DataQuality.ESTIMATED: 2,
    DataQuality.STALE: 3,
    DataQuality.MISSING: 4,
}


def _merge_quality(*qualities: MetricQuality) -> MetricQuality:
    """Merge section quality; the most severe stamp wins, reasons are deduped."""
    present = tuple(quality for quality in qualities if quality is not None)
    if not present:
        return MetricQuality.complete()
    status = max(
        (quality.data_quality for quality in present),
        key=lambda value: _QUALITY_RANK[value],
    )
    reasons: list[str] = []
    for quality in present:
        reasons.extend(quality.quality_reasons)
    return MetricQuality(data_quality=status, quality_reasons=tuple(dict.fromkeys(reasons)))


@dataclass(frozen=True)
class KlineBar:
    """One independently sourced OHLCV bar used only for cross-checking."""

    bar_start: datetime
    volume: Decimal
    taker_buy_volume: Decimal


def _parse_kline_time(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("kline timestamps must be timezone-aware")
    return value.astimezone(UTC)


def load_klines(settings: Settings, request: MetricRequest) -> tuple[KlineBar, ...]:
    """Read the independent kline fixture for this symbol/period.

    A missing fixture is not an error here; callers record it as a PARTIAL
    quality reason. Malformed content raises, because silently ignoring a
    corrupt cross-check input would re-open the tautology this guards against.
    """
    path = settings.klines_fixture
    if not path.is_file():
        return ()
    rows: list[KlineBar] = []
    required = {"symbol", "period", "bar_start", "volume", "taker_buy_volume"}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fieldnames = set(reader.fieldnames or ())
            missing = required - fieldnames
            if missing:
                raise ValueError(f"kline fixture is missing columns: {', '.join(sorted(missing))}")
            for row in reader:
                if row.get("symbol") != request.symbol:
                    continue
                if row.get("period") != request.period:
                    continue
                rows.append(
                    KlineBar(
                        bar_start=_parse_kline_time(str(row["bar_start"])),
                        volume=Decimal(str(row["volume"])),
                        taker_buy_volume=Decimal(str(row["taker_buy_volume"])),
                    )
                )
    except (OSError, UnicodeDecodeError, csv.Error, KeyError, ValueError) as exc:
        raise ValueError(f"invalid kline fixture {path.name}: {exc}") from exc
    return tuple(rows)


def _kline_cross_check_reasons(
    klines: Sequence[KlineBar], cvd: CvdResult, period: str
) -> tuple[str, ...]:
    if not cvd.points:
        return ()
    if not klines:
        # No kline rows for this period: do not cross-check a differently
        # keyed fixture (that caused false 1h/8h/1d failures).
        return (f"missing kline cross-check input for period {period}",)
    by_start = {bar.bar_start: bar for bar in klines}
    reasons: list[str] = []
    for point in cvd.points:
        kline = by_start.get(point.bar_start)
        if kline is None:
            reasons.append(f"missing kline bar {utc_timestamp(point.bar_start)}")
            continue
        # Raises ValueError when the independent kline disagrees.
        assert_kline_cross_check(
            point,
            volume=kline.volume,
            taker_buy_volume=kline.taker_buy_volume,
        )
    return tuple(reasons)


@dataclass(frozen=True)
class MetricRequest:
    """Inputs that fully determine one metric document."""

    symbol: str
    period: str
    window_start: datetime
    window_end: datetime
    tick_size: Decimal
    bin_size_multiple: int = 1
    value_area_pct: Decimal = DEFAULT_VALUE_AREA_PCT
    unit: Unit = "BASE"

    def __post_init__(self) -> None:
        if _SYMBOL_PATTERN.fullmatch(self.symbol) is None:
            raise ValueError("symbol must match ^[A-Z0-9]{2,24}$")
        if self.window_start.tzinfo is None or self.window_end.tzinfo is None:
            raise ValueError("window bounds must be timezone-aware")
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        if not isinstance(self.tick_size, Decimal):
            raise TypeError("tick_size must be a Decimal")
        if not isinstance(self.value_area_pct, Decimal):
            raise TypeError("value_area_pct must be a Decimal")
        if isinstance(self.bin_size_multiple, bool) or not isinstance(self.bin_size_multiple, int):
            raise TypeError("bin_size_multiple must be an int")


@dataclass(frozen=True)
class MetricDocument:
    """A rendered metric document plus the domain results and provenance."""

    request: MetricRequest
    metric_version: str
    input_manifest_hash: str
    computed_at: str
    quality: MetricQuality
    cvd: CvdResult
    footprint: FootprintResult
    volume_profile: VolumeProfile
    open_interest: OpenInterestResult | None
    funding: FundingResult | None
    liquidations: LiquidationResult | None
    rows: tuple[MetricRow, ...]
    json_text: str

    def to_dict(self) -> dict[str, object]:
        """Parse the rendered JSON for schema validation and golden comparison."""
        return cast("dict[str, object]", json.loads(self.json_text))


@dataclass(frozen=True)
class PersistedMetric:
    """A document and the immutable parts/manifest rows that store it."""

    document: MetricDocument
    entry: ManifestEntry
    footprint_entry: ManifestEntry | None = None


def _epoch_millis(value: datetime) -> int:
    """Convert to integer epoch milliseconds without going through float."""
    delta = value.astimezone(UTC) - _EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def input_manifest_hash(
    trades: Sequence[AggTrade],
    request: MetricRequest,
    inputs: MetricInputs | None = None,
) -> str:
    """Hash the canonical row set that produced the metric document.

    The two-argument form is byte-identical to the slice-04 hash so existing
    goldens and persisted documents keep their provenance. Measured v2 inputs
    extend the hash deterministically when they are supplied.
    """
    digest = hashlib.sha256()
    digest.update(
        (
            f"{request.symbol}|{request.period}|"
            f"{utc_timestamp(request.window_start)}|{utc_timestamp(request.window_end)}\n"
        ).encode()
    )
    ordered = sorted(trades, key=lambda trade: (trade.transact_time, trade.agg_trade_id))
    for trade in ordered:
        digest.update(
            (
                f"{trade.agg_trade_id}|{decimal_str(trade.price)}|{decimal_str(trade.quantity)}|"
                f"{_epoch_millis(trade.transact_time)}|{int(trade.is_buyer_maker)}\n"
            ).encode()
        )
    if inputs is not None and any(
        section is not None
        for section in (inputs.open_interest, inputs.funding, inputs.liquidations)
    ):
        digest.update(b"\0inputs\0")
        digest.update(
            contract_json(
                {
                    "open_interest": (
                        inputs.open_interest.to_contract()
                        if inputs.open_interest is not None and inputs.open_interest.points
                        else None
                    ),
                    "funding": (
                        inputs.funding.to_contract()
                        if inputs.funding is not None and inputs.funding.current_rate is not None
                        else None
                    ),
                    "liquidations": (
                        inputs.liquidations.to_contract()
                        if inputs.liquidations is not None
                        else None
                    ),
                }
            ).encode()
        )
    return digest.hexdigest()


def _expected_days(request: MetricRequest) -> set[date]:
    first = request.window_start.astimezone(UTC).date()
    last = (request.window_end - timedelta(microseconds=1)).astimezone(UTC).date()
    return {first + timedelta(days=offset) for offset in range((last - first).days + 1)}


def _agg_trades_root(settings: Settings, symbol: str) -> Path:
    return settings.data_dir / "market" / "um" / "agg_trades" / f"symbol={symbol}"


def _present_days(
    settings: Settings, request: MetricRequest, store: StorePort | None = None
) -> set[date]:
    root = _agg_trades_root(settings, request.symbol)
    allowed: set[str] | None = None
    if store is not None:
        allowed = {entry.path for entry in store.list_manifest() if entry.data_type == "agg_trades"}
    present: set[date] = set()
    for day in _expected_days(request):
        directory = root / f"date={day.isoformat()}"
        if not directory.is_dir():
            continue
        parts = tuple(directory.glob("*.parquet"))
        if allowed is not None:
            parts = tuple(path for path in parts if str(path) in allowed)
        if parts:
            present.add(day)
    return present


def load_agg_trades(
    settings: Settings, request: MetricRequest, store: StorePort | None = None
) -> tuple[AggTrade, ...]:
    """Read the published agg_trades parts intersecting the request window.

    When a store is available only manifest-registered parts are used, so a
    crash-orphaned part can never inject duplicate ids into the metric core.
    """
    root = _agg_trades_root(settings, request.symbol)
    allowed: set[str] | None = None
    if store is not None:
        allowed = {entry.path for entry in store.list_manifest() if entry.data_type == "agg_trades"}
    parts: list[str] = []
    for day in sorted(_expected_days(request)):
        directory = root / f"date={day.isoformat()}"
        if directory.is_dir():
            parts.extend(
                str(path)
                for path in sorted(directory.glob("*.parquet"))
                if allowed is None or str(path) in allowed
            )
    if not parts:
        return ()
    import pyarrow as pa
    import pyarrow.dataset as dataset

    try:
        table = dataset.dataset(parts, format="parquet").to_table(
            columns=[
                "agg_trade_id",
                "price",
                "quantity",
                "first_trade_id",
                "last_trade_id",
                "transact_time",
                "is_buyer_maker",
                "symbol",
            ]
        )
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise StoreUnavailableError(
            f"cannot read agg_trades parts for {request.symbol}: {exc}"
        ) from exc

    trades: list[AggTrade] = []
    for row in table.to_pylist():
        transact_time = row["transact_time"]
        if transact_time.tzinfo is None:
            transact_time = transact_time.replace(tzinfo=UTC)
        if not (request.window_start <= transact_time < request.window_end):
            continue
        trades.append(
            AggTrade(
                symbol=row["symbol"],
                agg_trade_id=row["agg_trade_id"],
                price=Decimal(str(row["price"])),
                quantity=Decimal(str(row["quantity"])),
                first_trade_id=row["first_trade_id"],
                last_trade_id=row["last_trade_id"],
                transact_time=transact_time,
                is_buyer_maker=row["is_buyer_maker"],
            )
        )
    trades.sort(key=lambda trade: (trade.transact_time, trade.agg_trade_id))
    return tuple(trades)


def _quality(
    settings: Settings,
    request: MetricRequest,
    trades: Sequence[AggTrade],
    store: StorePort | None = None,
) -> MetricQuality:
    if not trades:
        return MetricQuality.missing("no aggTrade rows in the requested window")
    missing = sorted(_expected_days(request) - _present_days(settings, request, store))
    if missing:
        return MetricQuality.partial(
            tuple(f"missing agg_trades part {day.isoformat()}" for day in missing)
        )
    return MetricQuality.complete()


def _metric_rows(cvd: CvdResult, profile: VolumeProfile) -> tuple[MetricRow, ...]:
    rows: list[MetricRow] = []
    for point in cvd.points:
        rows.append(
            MetricRow(
                record_type="CVD_BAR",
                bar_start=point.bar_start,
                bar_end=point.bar_end,
                buy_volume=point.buy_volume,
                sell_volume=point.sell_volume,
                delta=point.delta,
                cvd=point.cvd,
                taker_buy_ratio=point.taker_buy_ratio,
            )
        )
    for bin_ in profile.bins:
        rows.append(
            MetricRow(
                record_type="VOLUME_PROFILE_BIN",
                price_low=bin_.price_low,
                price_high=bin_.price_high,
                volume=bin_.volume,
                buy_volume=bin_.buy_volume,
                sell_volume=bin_.sell_volume,
                is_poc=bin_.is_poc,
                in_value_area=bin_.in_value_area,
            )
        )
    return tuple(rows)


def _cvd_payload(cvd: CvdResult) -> dict[str, object]:
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


def _volume_profile_payload(profile: VolumeProfile) -> dict[str, object]:
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


def _source_ref(
    settings: Settings, request: MetricRequest, version: str, input_hash: str, computed_at: str
) -> dict[str, object]:
    return {
        "kind": "ARCHIVE",
        "ref": str(_agg_trades_root(settings, request.symbol)),
        "as_of": computed_at,
        "version": version,
        "hash": input_hash,
    }


def _gap_payload(gaps: Sequence[DataGap]) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for gap in gaps:
        payload.append(
            {
                "gap_id": gap.gap_id,
                "data_type": "AGG_TRADES",
                "start": gap.start,
                "end": gap.end,
                "reason": gap.reason,
                "severity": gap.severity.value,
                "resolved": gap.resolved_at is not None,
            }
        )
    return payload


def _document_payload(
    *,
    settings: Settings,
    request: MetricRequest,
    version: MetricVersion,
    input_hash: str,
    computed_at: str,
    quality: MetricQuality,
    cvd: CvdResult,
    footprint: FootprintResult,
    profile: VolumeProfile,
    inputs: MetricInputs,
    gaps: Sequence[DataGap],
) -> dict[str, object]:
    window = MetricWindow(request.window_start, request.window_end)
    open_interest = (
        inputs.open_interest.to_contract()
        if inputs.open_interest is not None and inputs.open_interest.points
        else {
            "unit": "BASE",
            "sampling": "REGULAR",
            "points": [],
            "delta_over_window": "0",
            "last": {
                "ts": utc_timestamp(window.end),
                "open_interest": "0",
                "open_interest_value": "0",
            },
        }
    )
    funding = (
        inputs.funding.to_contract()
        if inputs.funding is not None and inputs.funding.current_rate is not None
        else {
            "current_rate": "0",
            "next_funding_time": utc_timestamp(window.end),
            "interval_hours": 8,
            "mark_price": "0",
            "index_price": "0",
            "history": [],
        }
    )
    liquidations = (
        inputs.liquidations.to_contract()
        if inputs.liquidations is not None
        else {
            "coverage": "NONE",
            "coverage_start": None,
            "coverage_end": None,
            "total_long_notional": "0",
            "total_short_notional": "0",
            "count": 0,
            "buckets": [],
            "limitations": list(LIMITATIONS),
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": request.symbol,
        "period": request.period,
        "window": {
            "start": utc_timestamp(window.start),
            "end": utc_timestamp(window.end),
        },
        "computed_at": computed_at,
        "metric_version": version.identifier,
        "input_manifest_hash": input_hash,
        "data_quality": quality.data_quality.value,
        "quality_reasons": list(quality.quality_reasons),
        "cvd": _cvd_payload(cvd),
        "footprint": footprint.to_contract(),
        "volume_profile": _volume_profile_payload(profile),
        "open_interest": open_interest,
        "funding": funding,
        "liquidations": liquidations,
        "sources": [_source_ref(settings, request, version.identifier, input_hash, computed_at)],
        "gaps": _gap_payload(gaps),
    }


class MetricService:
    """Compute and persist deterministic metric documents from Parquet inputs."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: StorePort,
        parquet: MetricsWriterPort,
        clock: ClockPort,
    ) -> None:
        self._settings = settings
        self._store = store
        self._parquet = parquet
        self._clock = clock
        self._reconciled = False

    def _version(self, request: MetricRequest) -> MetricVersion:
        return orderflow_metric_version(
            period=request.period,
            unit=request.unit,
            tick_size=request.tick_size,
            value_area_pct=request.value_area_pct,
            bin_size_multiple=request.bin_size_multiple,
            footprint_imbalance_threshold=DEFAULT_IMBALANCE_THRESHOLD,
            footprint_stacked_min_levels=DEFAULT_STACKED_MIN_LEVELS,
            footprint_top_imbalances=DEFAULT_TOP_IMBALANCES,
        )

    def compute(
        self,
        request: MetricRequest,
        *,
        inputs: MetricInputs | None = None,
    ) -> MetricDocument:
        MetricWindow(request.window_start, request.window_end)
        if request.unit != "BASE":
            raise ValueError("metric engine v1 supports BASE-unit CVD only")
        version = self._version(request)
        resolved = inputs if inputs is not None else MetricInputs()
        trades = load_agg_trades(self._settings, request, self._store)
        cvd = compute_cvd(
            trades,
            symbol=request.symbol,
            period=request.period,
            window_start=request.window_start,
            window_end=request.window_end,
            unit=request.unit,
        )
        footprint = compute_footprint(
            trades,
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
            trades,
            symbol=request.symbol,
            window_start=request.window_start,
            window_end=request.window_end,
            tick_size=request.tick_size,
            bin_size_multiple=request.bin_size_multiple,
            value_area_pct=request.value_area_pct,
        )
        input_hash = input_manifest_hash(trades, request, inputs)
        base_quality = _quality(self._settings, request, trades, self._store)
        klines = load_klines(self._settings, request)
        cross_reasons = _kline_cross_check_reasons(klines, cvd, request.period)
        if base_quality.data_quality != DataQuality.MISSING:
            base_quality = MetricQuality(
                data_quality=base_quality.data_quality,
                quality_reasons=(*base_quality.quality_reasons, *cross_reasons),
            )
        contributions: list[MetricQuality] = [base_quality]
        if resolved.open_interest is not None:
            contributions.append(resolved.open_interest.quality)
        elif base_quality.data_quality != DataQuality.MISSING:
            # Not measured for this document; never present the placeholder as
            # a measured zero.
            contributions.append(
                MetricQuality.partial(("open_interest not measured in this slice",))
            )
        if resolved.funding is not None:
            contributions.append(resolved.funding.quality)
        elif base_quality.data_quality != DataQuality.MISSING:
            contributions.append(MetricQuality.partial(("funding not measured in this slice",)))
        quality = _merge_quality(*contributions)
        computed_at = utc_timestamp(self._clock.now())
        start_day = request.window_start.astimezone(UTC).date().isoformat()
        last_day = (
            (request.window_end - timedelta(microseconds=1)).astimezone(UTC).date().isoformat()
        )
        gaps = self._store.list_data_gaps(
            symbol=request.symbol,
            data_type="aggTrades",
            start=start_day,
            end=last_day,
        )
        payload = _document_payload(
            settings=self._settings,
            request=request,
            version=version,
            input_hash=input_hash,
            computed_at=computed_at,
            quality=quality,
            cvd=cvd,
            footprint=footprint,
            profile=profile,
            inputs=resolved,
            gaps=gaps,
        )
        return MetricDocument(
            request=request,
            metric_version=version.identifier,
            input_manifest_hash=input_hash,
            computed_at=computed_at,
            quality=quality,
            cvd=cvd,
            footprint=footprint,
            volume_profile=profile,
            open_interest=resolved.open_interest,
            funding=resolved.funding,
            liquidations=resolved.liquidations,
            rows=_metric_rows(cvd, profile),
            json_text=contract_json(payload),
        )

    def _reconcile_metric_parts(self) -> None:
        """Adopt/repair metrics parts orphaned by a crash before manifest insert.

        Lazy: runs once per service instance (startup/first persist).
        """
        if self._reconciled:
            return
        root = self._settings.data_dir / "metrics" / "orderflow"
        if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
            # A transiently unavailable metrics mount must not erase manifest rows.
            self._reconciled = True
            return
        known: set[str] = set()
        for entry in self._store.list_manifest():
            if entry.data_type != "metrics":
                continue
            path = Path(entry.path)
            if path.is_file():
                known.add(entry.path)
            elif path.parent.is_dir():
                # The date directory is readable and the part is definitively
                # absent; this is a real missing-file row.
                self._store.delete_manifest_entry(entry.path)
            else:
                # Parent directory missing/unreadable: treat as drift, keep the
                # manifest row rather than deleting it during a transient fault.
                structlog.get_logger(__name__).warning("metrics_manifest_drift", path=entry.path)
        for entry in recover_orphan_metric_parts(
            self._settings, known, now=utc_timestamp(self._clock.now())
        ):
            self._store.upsert_manifest_entry(entry)
        self._reconciled = True

    def _cleanup_unreferenced(self, entries: Sequence[ManifestEntry]) -> None:
        for entry in entries:
            # Only delete when the manifest explicitly says this path is
            # unreferenced. An unreadable DB leaves the part for adoption.
            try:
                referenced = self._store.get_manifest_entry(entry.path) is not None
            except StoreUnavailableError:
                referenced = True
            if not referenced:
                Path(entry.path).unlink(missing_ok=True)

    def compute_and_persist(
        self,
        request: MetricRequest,
        *,
        inputs: MetricInputs | None = None,
    ) -> PersistedMetric:
        document = self.compute(request, inputs=inputs)
        self._reconcile_metric_parts()
        entries: list[ManifestEntry] = []
        try:
            metrics_entry = self._parquet.write_metrics_part(
                document.rows,
                symbol=request.symbol,
                period=request.period,
                window_start=request.window_start,
                window_end=request.window_end,
                metric_version=document.metric_version,
                input_manifest_hash=document.input_manifest_hash,
                computed_at=document.computed_at,
                data_quality=document.quality.data_quality.value,
                quality_reasons=document.quality.quality_reasons,
            )
            entries.append(metrics_entry)
            footprint_entry = self._parquet.write_footprint_part(
                document.footprint,
                symbol=request.symbol,
                period=request.period,
                window_start=request.window_start,
                window_end=request.window_end,
                metric_version=document.metric_version,
                input_manifest_hash=document.input_manifest_hash,
                computed_at=document.computed_at,
                data_quality=document.quality.data_quality.value,
                quality_reasons=document.quality.quality_reasons,
            )
            entries.append(footprint_entry)
            self._store.upsert_manifest_entries(tuple(entries))
        except Exception:
            self._cleanup_unreferenced(entries)
            raise
        return PersistedMetric(
            document=document,
            entry=metrics_entry,
            footprint_entry=footprint_entry,
        )

    def persist_liquidation_events(
        self,
        events: Sequence[LiquidationEvent],
        *,
        symbol: str,
        window_start: datetime,
        window_end: datetime,
        metric_version: str,
        data_quality: str = "COMPLETE",
        quality_reasons: tuple[str, ...] = (),
    ) -> ManifestEntry:
        """Publish a content-addressed liquidation event part and manifest row."""
        if window_start.tzinfo is None or window_end.tzinfo is None:
            raise ValueError("window bounds must be timezone-aware")
        if window_end <= window_start:
            raise ValueError("window_end must be after window_start")
        for event in events:
            if event.symbol != symbol:
                raise ValueError(f"liquidation symbol {event.symbol!r} does not match {symbol!r}")
            if not (window_start <= event.received_at < window_end):
                raise ValueError("liquidation event is outside the requested window")
        computed_at = utc_timestamp(self._clock.now())
        entry = self._parquet.write_liquidation_part(
            events,
            symbol=symbol,
            window_start=window_start,
            window_end=window_end,
            metric_version=metric_version,
            input_manifest_hash=liquidation_input_hash(events, symbol),
            computed_at=computed_at,
            data_quality=data_quality,
            quality_reasons=quality_reasons,
        )
        try:
            self._store.upsert_manifest_entry(entry)
        except Exception:
            self._cleanup_unreferenced((entry,))
            raise
        return entry
