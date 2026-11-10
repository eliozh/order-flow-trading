"""Recorded report snapshot -> exact :class:`EvaluationContext`.

This is the single mapping shared by the opportunity display path and the
trigger evaluation path (slice 17). It never coerces missing data to zero: an
absent or malformed input stays ``None``, and a series with a hole is absent
rather than silently truncated.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from oft.domain.conditions import (
    SESSION_STATES,
    EvaluationContext,
    available_data,
)

# UNKNOWN is a contract enum value, not a measured regime: a recorder that
# cannot tell the session must leave SESSION absent rather than evaluable.
_MEASURED_SESSION_STATES = SESSION_STATES - {"UNKNOWN"}


def optional_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if not isinstance(value, str):
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def bar_series(bars: object, key: str) -> tuple[Decimal, ...] | None:
    if not isinstance(bars, list):
        return None
    values: list[Decimal] = []
    for bar in bars:
        if not isinstance(bar, Mapping):
            return None
        value = optional_decimal(bar.get(key))
        if value is None:
            return None
        values.append(value)
    return tuple(values)


def bar_timestamps(bars: object) -> tuple[str, ...] | None:
    if not isinstance(bars, list) or not bars:
        return None
    values: list[str] = []
    for bar in bars:
        if not isinstance(bar, Mapping):
            return None
        value = bar.get("bar_start")
        if not isinstance(value, str) or not value:
            return None
        values.append(value)
    return tuple(values)


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO timestamp; offset-less values are treated as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def footprint_imbalance(footprint: Mapping[str, Any]) -> Decimal | None:
    bars = footprint.get("bars")
    if not isinstance(bars, list) or not bars:
        return None
    latest = bars[-1]
    cells = mapping(latest).get("cells")
    if not isinstance(cells, list):
        return None
    strongest: Decimal | None = None
    for cell in cells:
        cell_map = mapping(cell)
        bid = optional_decimal(cell_map.get("bid_volume"))
        ask = optional_decimal(cell_map.get("ask_volume"))
        if bid is None or ask is None:
            continue
        total = bid + ask
        if total <= 0:
            continue
        normalized = abs(bid - ask) / total
        if strongest is None or normalized > strongest:
            strongest = normalized
    return strongest


def open_interest_delta_pct(section: Mapping[str, Any]) -> Decimal | None:
    points = section.get("points")
    if not isinstance(points, list) or len(points) < 2:
        return None
    first = optional_decimal(mapping(points[0]).get("open_interest"))
    last = optional_decimal(mapping(points[-1]).get("open_interest"))
    if first is None or last is None or first == 0:
        return None
    return (last - first) / first * Decimal(100)


def liquidation_notional(
    section: Mapping[str, Any], *, window_start: str | None = None
) -> Decimal | None:
    coverage = section.get("coverage")
    if coverage in (None, "NONE"):
        return None
    long_notional = optional_decimal(section.get("total_long_notional"))
    short_notional = optional_decimal(section.get("total_short_notional"))
    if long_notional is None or short_notional is None:
        return None
    if coverage == "SINCE_DEPLOYMENT":
        # Since-deployment coverage only measures the window it actually
        # covers. A recorder that started after the window (or has no end
        # bound yet) cannot report a real zero.
        recorded_start = parse_timestamp(section.get("coverage_start"))
        recorded_end = parse_timestamp(section.get("coverage_end"))
        if recorded_start is None or recorded_end is None:
            return None
        window = parse_timestamp(window_start)
        if window is not None and recorded_start > window:
            return None
    return long_notional + short_notional


def session_state(snapshot: Mapping[str, Any]) -> str | None:
    session = mapping(snapshot.get("session"))
    if not session:
        return None
    if bool(session.get("is_no_trading")):
        return "NO_TRADING"
    raw = session.get("underlying_session")
    note = session.get("note")
    if isinstance(note, str) and "not measured" in note.lower():
        # The placeholder written by the metrics-only snapshot builder is an
        # unmeasured field, never a recorded trading regime.
        return None
    # Only a known measured regime counts. Every other label (UNKNOWN,
    # "unknown", empty, garbage) is absent: coercing it to the measured value
    # "UNKNOWN" would let unmeasured data satisfy a SESSION_STATE condition.
    if not isinstance(raw, str) or raw not in _MEASURED_SESSION_STATES:
        return None
    return raw


def evaluation_context_from_snapshot(
    snapshot: Mapping[str, Any], *, as_of: str
) -> EvaluationContext:
    """Build the exact-decimal display context from a recorded report snapshot."""
    contract = mapping(snapshot.get("contract_specs"))
    tick_size = optional_decimal(contract.get("tick_size"))
    if tick_size is not None and tick_size <= 0:
        tick_size = None
    bars = snapshot.get("bars")
    # An empty or malformed series is absent, never an empty tuple: ``volume``
    # must stay None (missing) instead of collapsing to zero.
    closes = bar_series(bars, "close") or None
    cvds = bar_series(bars, "cvd") or None
    volumes = bar_series(bars, "volume") or None
    deltas = bar_series(bars, "delta") or None
    ois = bar_series(bars, "open_interest") or None
    timestamps = bar_timestamps(bars)
    last_price = optional_decimal(snapshot.get("last_price"))
    if last_price is None:
        last_price = optional_decimal(snapshot.get("mark_price"))
    if last_price is None and closes:
        last_price = closes[-1]
    previous_price = closes[-2] if closes is not None and len(closes) >= 2 else None
    orderflow = mapping(snapshot.get("orderflow"))
    cvd_section = mapping(orderflow.get("cvd"))
    summary = mapping(cvd_section.get("summary"))
    delta = optional_decimal(summary.get("delta"))
    points = cvd_section.get("points")
    if cvds is None and isinstance(points, list):
        # A hole anywhere in the series makes it unavailable; never compress.
        collected: list[Decimal] = []
        complete = True
        for point in points:
            value = optional_decimal(mapping(point).get("cvd"))
            if value is None:
                complete = False
                break
            collected.append(value)
        cvds = tuple(collected) if complete and collected else None
    cvd = cvds[-1] if cvds else None
    volume = sum(volumes, Decimal(0)) if volumes is not None else None
    funding_section = mapping(orderflow.get("funding"))
    history = funding_section.get("history")
    funding_rate = (
        optional_decimal(funding_section.get("current_rate"))
        if isinstance(history, list) and history
        else None
    )
    window = mapping(snapshot.get("window"))
    window_start = window.get("start")
    return EvaluationContext(
        as_of=as_of,
        last_price=last_price,
        previous_price=previous_price,
        tick_size=tick_size,
        delta=delta,
        cvd=cvd,
        cvd_series=cvds,
        price_series=closes,
        oi_delta_pct=open_interest_delta_pct(mapping(orderflow.get("open_interest"))),
        funding_rate=funding_rate,
        liquidation_notional=liquidation_notional(
            mapping(orderflow.get("liquidations")),
            window_start=window_start if isinstance(window_start, str) else None,
        ),
        volume=volume,
        footprint_imbalance=footprint_imbalance(mapping(orderflow.get("footprint"))),
        session_state=session_state(snapshot),
        bar_timestamps=timestamps,
        delta_series=deltas,
        volume_series=volumes,
        oi_series=ois,
    )


def available_data_from_snapshot(snapshot: Mapping[str, Any]) -> frozenset[str]:
    context = evaluation_context_from_snapshot(snapshot, as_of="1970-01-01T00:00:00.000Z")
    return available_data(context)


__all__ = [
    "available_data_from_snapshot",
    "evaluation_context_from_snapshot",
    "optional_decimal",
    "parse_timestamp",
]
