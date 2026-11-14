"""Deterministic journal read models: eligibility, statistics, comparison, and CSV.

This module is pure: standard library only, no I/O, clock, or ``oft.*`` import.
Every money value is parsed and aggregated as an exact ``Decimal``; the service
layer supplies the persisted trade-log payloads, and this module owns the
formulas. The definitions are data (:data:`DEFINITIONS`) so the CLI, API, and
export all state the same basis.

Formulas (code-owned):

- ``net = gross - fees + funding``; statistics consume the persisted
  ``net_pnl_usd`` and never recompute it from other fields.
- win rate = eligible closed trades with ``net > 0`` / eligible closed trades;
  a tie (``net == 0``) is not a win.
- expectancy = mean ``net_pnl_usd`` over eligible closed trades.
- average R = mean ``r_multiple`` over eligible trades with a non-null R;
  null-R trades are excluded from the mean and counted separately.

Eligibility: the row is a persisted trade log, its ``data_quality.status`` is
``COMPLETE``, it carries no ``PARTIAL``/``DATA_PARTIAL`` outcome tag, its
``closed_at`` is parseable (and inside the selected period when one is
supplied), and its ``net_pnl_usd`` is parseable, finite, and plausible. Open
positions without a trade log are excluded and counted. When no trade is
eligible the metrics are ``UNDEFINED`` and every metric is ``null`` -- never a
fabricated zero.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal

EIGHT_DP = Decimal("0.00000001")
ZERO = Decimal("0")

# Keep a corrupt-but-finite persisted value from overflowing ``quantize``.
MAX_ABS_MONEY = Decimal("1000000000000000")

# A period longer than this is rejected before ``timedelta``/date arithmetic can
# overflow. 365_000 days (~1000 years) is far beyond any plausible journal read
# and still fits a 16-character label.
MAX_PERIOD_DAYS = 365_000

# A month is a fixed 30 days by definition; calendar months are never used.
_DAYS_PER_UNIT = {"d": 1, "w": 7, "m": 30}
_PERIOD_PATTERN = re.compile(r"^([0-9]+)([dwm])$")

_PARTIAL_TAGS = frozenset({"PARTIAL", "DATA_PARTIAL"})

DEFINITIONS: dict[str, str] = {
    "net": "net = gross PnL - fees + funding; the persisted net_pnl_usd is consumed as-is",
    "win_rate": (
        "win rate = eligible closed trades with net > 0 / eligible closed trades; "
        "ties (net == 0) are not wins"
    ),
    "expectancy": "expectancy = mean net_pnl_usd over eligible closed trades",
    "avg_r": (
        "avg R = mean r_multiple over eligible trades with a non-null R; "
        "null-R trades are excluded from the mean and counted separately"
    ),
    "eligibility": (
        "eligible = persisted trade log with data_quality.status COMPLETE, no "
        "PARTIAL/DATA_PARTIAL outcome tag, parseable closed_at inside the selected "
        "period, and a parseable finite net_pnl_usd; open positions without a trade "
        "log are excluded and counted"
    ),
    "period": (
        "period Nd = N days, Nw = N*7 days, Nm = N*30 days; the window is "
        "[now - duration, now] on closed_at in UTC"
    ),
}

CSV_COLUMNS: tuple[str, ...] = (
    "trade_id",
    "position_id",
    "environment",
    "symbol",
    "side",
    "opened_at",
    "closed_at",
    "entry_avg_price",
    "exit_avg_price",
    "size",
    "gross_pnl_usd",
    "fees_usd",
    "funding_usd",
    "net_pnl_usd",
    "r_multiple",
    "mae_usd",
    "mfe_usd",
    "return_pct",
    "outcome_tags",
    "data_quality_status",
)


class JournalError(Exception):
    """A typed journal/read-model failure; ``code`` maps to the API catalog."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Period:
    """A parsed period: the original label and its duration in days."""

    label: str
    days: int


@dataclass(frozen=True)
class PeriodWindow:
    """A resolved UTC window over ``closed_at``."""

    label: str
    start: str
    end: str
    basis: str = "closed_at"

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise JournalError("VALIDATION_FAILED", "the period label must be a non-empty string")
        start = _parse_timestamp(self.start)
        end = _parse_timestamp(self.end)
        if start is None or end is None:
            raise JournalError(
                "VALIDATION_FAILED", "the period window bounds must be parseable timestamps"
            )
        if start > end:
            raise JournalError(
                "VALIDATION_FAILED", "the period window start must not be after its end"
            )
        if self.basis != "closed_at":
            raise JournalError("VALIDATION_FAILED", f"unsupported period basis {self.basis!r}")

    def to_contract(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "start": self.start,
            "end": self.end,
            "basis": self.basis,
        }


@dataclass(frozen=True)
class JournalAvgR:
    """Average R with its sample and excluded-null counts."""

    value: float | None
    samples: int
    excluded_null_r: int

    def __post_init__(self) -> None:
        if self.samples < 0 or self.excluded_null_r < 0:
            raise ValueError("average-R counts must be non-negative")
        if (self.value is not None) != (self.samples > 0):
            raise ValueError("average-R value must be present iff samples > 0")

    def to_contract(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "samples": self.samples,
            "excluded_null_r": self.excluded_null_r,
        }


@dataclass(frozen=True)
class JournalTotals:
    """The rendered money sums; ``None`` means at least one term was unreadable."""

    gross_pnl_usd: str | None
    fees_usd: str | None
    funding_usd: str | None
    net_pnl_usd: str | None

    def to_contract(self) -> dict[str, Any]:
        return {
            "gross_pnl_usd": self.gross_pnl_usd,
            "fees_usd": self.fees_usd,
            "funding_usd": self.funding_usd,
            "net_pnl_usd": self.net_pnl_usd,
        }


@dataclass(frozen=True)
class JournalStats:
    """A discriminated, invariant-checked statistics result.

    ``DEFINED`` requires a positive sample, buckets that sum to it, non-null
    win-rate/expectancy, and an average-R split that accounts for every sample.
    ``UNDEFINED`` requires every metric and total to be empty -- never a
    fabricated zero.
    """

    status: Literal["DEFINED", "UNDEFINED"]
    reason: str | None
    period: PeriodWindow | None
    sample_size: int
    denominator: int
    exclusions: dict[str, int]
    definitions: dict[str, str]
    wins: int
    losses: int
    ties: int
    win_rate: float | None
    expectancy_usd: str | None
    avg_r: JournalAvgR
    totals: JournalTotals
    data_quality: Literal["COMPLETE", "PARTIAL", "MISSING"]

    def __post_init__(self) -> None:
        if self.status not in {"DEFINED", "UNDEFINED"}:
            raise ValueError(f"invalid statistics status {self.status!r}")
        if self.data_quality not in {"COMPLETE", "PARTIAL", "MISSING"}:
            raise ValueError(f"invalid data quality {self.data_quality!r}")
        if self.status == "DEFINED":
            if self.sample_size <= 0:
                raise ValueError("DEFINED statistics require a positive sample size")
            if self.wins + self.losses + self.ties != self.sample_size:
                raise ValueError("wins + losses + ties must equal sample_size")
            if self.win_rate is None or self.expectancy_usd is None:
                raise ValueError("DEFINED statistics require a win rate and expectancy")
            if self.avg_r.samples + self.avg_r.excluded_null_r != self.sample_size:
                raise ValueError("average-R samples must account for every eligible trade")
            return
        if self.sample_size != 0 or self.wins or self.losses or self.ties:
            raise ValueError("UNDEFINED statistics require a zero sample and empty buckets")
        if self.win_rate is not None or self.expectancy_usd is not None:
            raise ValueError("UNDEFINED statistics must not fabricate a metric")
        if (
            self.avg_r.value is not None
            or self.avg_r.samples != 0
            or self.avg_r.excluded_null_r != 0
        ):
            raise ValueError("UNDEFINED statistics require an empty average-R result")
        totals = self.totals
        if any(
            value is not None
            for value in (
                totals.gross_pnl_usd,
                totals.fees_usd,
                totals.funding_usd,
                totals.net_pnl_usd,
            )
        ):
            raise ValueError("UNDEFINED statistics require empty totals")

    def to_contract(self) -> dict[str, Any]:
        """The stable JSON shape shared by the API, the CLI, and the export."""
        return {
            "period": None if self.period is None else self.period.to_contract(),
            "sample_size": self.sample_size,
            "denominator": self.denominator,
            "exclusions": dict(self.exclusions),
            "definitions": dict(self.definitions),
            "status": self.status,
            "reason": self.reason,
            "wins": self.wins,
            "losses": self.losses,
            "ties": self.ties,
            "win_rate": self.win_rate,
            "expectancy_usd": self.expectancy_usd,
            "avg_r": self.avg_r.to_contract(),
            "totals": self.totals.to_contract(),
            "data_quality": self.data_quality,
        }


@dataclass(frozen=True)
class CsvExport:
    """A rendered CSV document plus its row and neutralized-cell counts."""

    text: str
    rows: int
    neutralized_cells: int


# -- parsing ---------------------------------------------------------------


def parse_period(label: str) -> Period:
    """Parse ``Nd``/``Nw``/``Nm``; an unknown unit or a zero count is invalid."""
    if not isinstance(label, str):
        raise JournalError("VALIDATION_FAILED", "period must be a string")
    normalized = label.strip().lower()
    match = _PERIOD_PATTERN.match(normalized)
    if match is None:
        raise JournalError(
            "VALIDATION_FAILED",
            f"invalid period {label!r}: use Nd (days), Nw (weeks), or Nm (months)",
        )
    count = int(match.group(1))
    unit = match.group(2)
    if count <= 0:
        raise JournalError("VALIDATION_FAILED", f"invalid period {label!r}: the count must be > 0")
    days = count * _DAYS_PER_UNIT[unit]
    if days > MAX_PERIOD_DAYS:
        raise JournalError(
            "VALIDATION_FAILED",
            f"invalid period {label!r}: the duration exceeds {MAX_PERIOD_DAYS} days",
        )
    return Period(label=normalized, days=days)


def resolve_period(label: str | None, now: datetime) -> PeriodWindow | None:
    """Resolve the UTC window ``[now - duration, now]``; ``None`` means all trades."""
    if label is None:
        return None
    period = parse_period(label)
    end = _as_utc(now)
    try:
        start = end - timedelta(days=period.days)
    except OverflowError as exc:
        raise JournalError(
            "VALIDATION_FAILED",
            f"invalid period {period.label!r}: the window does not fit the calendar",
        ) from exc
    return PeriodWindow(label=period.label, start=_iso(start), end=_iso(end))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _as_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    return _as_utc(parsed)


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _money(value: object) -> Decimal | None:
    parsed = _decimal(value)
    if parsed is None or abs(parsed) > MAX_ABS_MONEY:
        return None
    return parsed


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def _quantize(value: Decimal) -> Decimal | None:
    if not value.is_finite() or abs(value) > MAX_ABS_MONEY:
        return None
    return value.quantize(EIGHT_DP, rounding=ROUND_DOWN)


def _money_text(value: Decimal) -> str | None:
    """An exact money string, or ``None`` when the value cannot be quantized."""
    quantized = _quantize(value)
    if quantized is None:
        return None
    if quantized == 0:
        return "0"
    return format(quantized, "f")


def decimal_text(value: object) -> str | None:
    """Render an exact decimal string, or ``None`` when the value is unusable."""
    parsed = _decimal(value)
    return None if parsed is None else format(parsed, "f")


# -- statistics ------------------------------------------------------------


def _quality_status(payload: Mapping[str, Any]) -> str | None:
    quality = payload.get("data_quality")
    if not isinstance(quality, Mapping):
        return None
    status = quality.get("status")
    return status if isinstance(status, str) and status else None


def _has_partial_tag(payload: Mapping[str, Any]) -> bool:
    tags = payload.get("outcome_tags")
    if not isinstance(tags, list):
        return False
    return any(str(tag) in _PARTIAL_TAGS for tag in tags)


def compute_stats(
    trades: Sequence[Mapping[str, Any] | None],
    *,
    open_positions: int = 0,
    window: PeriodWindow | None = None,
) -> JournalStats:
    """Compute the documented statistics from persisted closed-trade payloads."""
    exclusions = {
        "open_positions": max(0, int(open_positions)),
        "partial_trades": 0,
        "unreadable_trades": 0,
        "outside_period": 0,
    }
    bounds: tuple[datetime, datetime] | None = None
    if window is not None:
        start_dt = _parse_timestamp(window.start)
        end_dt = _parse_timestamp(window.end)
        if start_dt is None or end_dt is None:
            # A window whose bounds do not parse must never silently classify
            # every row as outside the period.
            raise JournalError(
                "VALIDATION_FAILED", "the period window bounds must be parseable timestamps"
            )
        bounds = (start_dt, end_dt)
    eligible: list[tuple[Mapping[str, Any], Decimal]] = []
    for payload in trades:
        if not isinstance(payload, Mapping):
            exclusions["unreadable_trades"] += 1
            continue
        closed_at = _parse_timestamp(payload.get("closed_at"))
        net = _money(payload.get("net_pnl_usd"))
        quality = _quality_status(payload)
        if closed_at is None or net is None or quality is None:
            exclusions["unreadable_trades"] += 1
            continue
        if quality != "COMPLETE" or _has_partial_tag(payload):
            exclusions["partial_trades"] += 1
            continue
        if bounds is not None and not (bounds[0] <= closed_at <= bounds[1]):
            exclusions["outside_period"] += 1
            continue
        eligible.append((payload, net))

    sample_size = len(eligible)
    if sample_size == 0:
        return JournalStats(
            status="UNDEFINED",
            reason=_undefined_reason(exclusions, total_rows=len(trades)),
            period=window,
            sample_size=0,
            denominator=0,
            exclusions=exclusions,
            definitions=dict(DEFINITIONS),
            wins=0,
            losses=0,
            ties=0,
            win_rate=None,
            expectancy_usd=None,
            avg_r=JournalAvgR(value=None, samples=0, excluded_null_r=0),
            totals=JournalTotals(
                gross_pnl_usd=None,
                fees_usd=None,
                funding_usd=None,
                net_pnl_usd=None,
            ),
            data_quality="MISSING",
        )

    wins = losses = ties = 0
    net_total = ZERO
    r_values: list[float] = []
    null_r = 0
    degraded = exclusions["unreadable_trades"] > 0
    gross_total = ZERO
    fees_total = ZERO
    funding_total = ZERO
    gross_complete = fees_complete = funding_complete = True
    for payload, net in eligible:
        if net > 0:
            wins += 1
        elif net < 0:
            losses += 1
        else:
            ties += 1
        net_total += net
        r_value = _number(payload.get("r_multiple"))
        if r_value is None:
            null_r += 1
        else:
            r_values.append(r_value)
        gross_total, gross_complete = _accumulate(
            gross_total, gross_complete, payload.get("gross_pnl_usd")
        )
        fees_total, fees_complete = _accumulate(fees_total, fees_complete, payload.get("fees_usd"))
        funding_total, funding_complete = _accumulate(
            funding_total, funding_complete, payload.get("funding_usd")
        )
    if not (gross_complete and fees_complete and funding_complete):
        degraded = True
    # The mean of bounded per-trade nets is itself bounded, so this quantize
    # cannot fail; a sum above the plausible range still degrades the totals.
    expectancy = (net_total / Decimal(sample_size)).quantize(EIGHT_DP, rounding=ROUND_DOWN)
    expectancy_text = _money_text(expectancy)
    net_total_text = _money_text(net_total)
    gross_text = _money_text(gross_total) if gross_complete else None
    fees_text = _money_text(fees_total) if fees_complete else None
    funding_text = _money_text(funding_total) if funding_complete else None
    # Quality is symmetric: a rendered component that cannot be quantized is a
    # named degradation, not a silently COMPLETE total.
    if net_total_text is None:
        degraded = True
    if gross_complete and gross_text is None:
        degraded = True
    if fees_complete and fees_text is None:
        degraded = True
    if funding_complete and funding_text is None:
        degraded = True
    avg_r_value: float | None = None
    if r_values:
        avg_r_value = sum(r_values) / len(r_values)
    return JournalStats(
        status="DEFINED",
        reason=None,
        period=window,
        sample_size=sample_size,
        denominator=sample_size,
        exclusions=exclusions,
        definitions=dict(DEFINITIONS),
        wins=wins,
        losses=losses,
        ties=ties,
        win_rate=wins / sample_size,
        expectancy_usd=expectancy_text,
        avg_r=JournalAvgR(
            value=avg_r_value,
            samples=len(r_values),
            excluded_null_r=null_r,
        ),
        totals=JournalTotals(
            gross_pnl_usd=gross_text,
            fees_usd=fees_text,
            funding_usd=funding_text,
            net_pnl_usd=net_total_text,
        ),
        data_quality="PARTIAL" if degraded else "COMPLETE",
    )


def _accumulate(total: Decimal, complete: bool, value: object) -> tuple[Decimal, bool]:
    parsed = _money(value)
    if parsed is None:
        return total, False
    return total + parsed, complete


def _undefined_reason(exclusions: Mapping[str, int], *, total_rows: int) -> str:
    if total_rows == 0 and exclusions["open_positions"] == 0:
        return "metrics undefined: no closed trades have been recorded"
    excluded = (
        f"{exclusions['partial_trades']} partial, "
        f"{exclusions['unreadable_trades']} unreadable, "
        f"{exclusions['outside_period']} outside the period"
    )
    return (
        "metrics undefined: no eligible closed trades "
        f"(excluded: {excluded}; {exclusions['open_positions']} open position(s))"
    )


# -- formula-safe CSV ------------------------------------------------------


_FORMULA_PREFIXES = ("=", "+", "-", "@")


def neutralize_csv_cell(value: object) -> str:
    """Prefix a spreadsheet formula start with ``'`` so it is rendered as text.

    A cell is dangerous when it starts with a formula character or a control
    character (TAB/CR/LF), and also when its first non-whitespace character is a
    formula character: some spreadsheets strip the leading whitespace and then
    evaluate the cell.
    """
    if value is None:
        return ""
    text = str(value)
    if text.startswith((*_FORMULA_PREFIXES, "\t", "\r", "\n")):
        return f"'{text}"
    if text.lstrip(" \t\r\n").startswith(_FORMULA_PREFIXES):
        return f"'{text}"
    return text


def _csv_raw(payload: Mapping[str, Any]) -> tuple[str, ...]:
    quality = payload.get("data_quality")
    status = ""
    if isinstance(quality, Mapping):
        raw_status = quality.get("status")
        status = raw_status if isinstance(raw_status, str) else ""
    tags = payload.get("outcome_tags")
    rendered_tags = "|".join(str(tag) for tag in tags) if isinstance(tags, list) else ""
    values = (
        payload.get("trade_id"),
        payload.get("position_id"),
        payload.get("environment"),
        payload.get("symbol"),
        payload.get("side"),
        payload.get("opened_at"),
        payload.get("closed_at"),
        payload.get("entry_avg_price"),
        payload.get("exit_avg_price"),
        payload.get("size"),
        payload.get("gross_pnl_usd"),
        payload.get("fees_usd"),
        payload.get("funding_usd"),
        payload.get("net_pnl_usd"),
        payload.get("r_multiple"),
        payload.get("mae_usd"),
        payload.get("mfe_usd"),
        payload.get("return_pct"),
        rendered_tags,
        status,
    )
    return tuple("" if value is None else str(value) for value in values)


def csv_row(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """One neutralized CSV row in :data:`CSV_COLUMNS` order."""
    return tuple(neutralize_csv_cell(value) for value in _csv_raw(payload))


def csv_document(trades: Sequence[Mapping[str, Any]]) -> CsvExport:
    """Render the trade-log read model as a neutralized CSV document."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    neutralized_cells = 0
    for payload in trades:
        raw = _csv_raw(payload)
        cells = tuple(neutralize_csv_cell(value) for value in raw)
        neutralized_cells += sum(
            1 for before, after in zip(raw, cells, strict=True) if before != after
        )
        writer.writerow(cells)
    return CsvExport(text=buffer.getvalue(), rows=len(trades), neutralized_cells=neutralized_cells)


# -- signal vs outcome -----------------------------------------------------


def zone_vs_fill(entry_price: object, zone_low: object, zone_high: object) -> dict[str, Any]:
    """Compare the actual entry with the proposed opportunity zone."""
    entry = _money(entry_price)
    low = _money(zone_low)
    high = _money(zone_high)
    reasons: list[str] = []
    if entry is None or entry <= 0:
        return {
            "status": "N/A",
            "entry": decimal_text(entry_price),
            "zone_low": decimal_text(zone_low),
            "zone_high": decimal_text(zone_high),
            "inside": None,
            "distance_bps": None,
            "reasons": ["the trade has no parseable entry average price"],
        }
    if low is None or high is None or low <= 0 or high <= 0 or low > high:
        return {
            "status": "N/A",
            "entry": decimal_text(entry),
            "zone_low": decimal_text(zone_low),
            "zone_high": decimal_text(zone_high),
            "inside": None,
            "distance_bps": None,
            "reasons": ["the source opportunity zone is missing or invalid"],
        }
    if low <= entry <= high:
        status = "INSIDE"
        distance: float | None = None
        inside = True
    elif entry < low:
        status = "BELOW_ZONE"
        distance = float((low - entry) / low * Decimal("10000"))
        inside = False
    else:
        status = "ABOVE_ZONE"
        distance = float((entry - high) / high * Decimal("10000"))
        inside = False
    return {
        "status": status,
        "entry": decimal_text(entry),
        "zone_low": decimal_text(low),
        "zone_high": decimal_text(high),
        "inside": inside,
        "distance_bps": distance,
        "reasons": reasons,
    }


def trigger_to_fill_bps(side: str, entry_price: object, reference_price: object) -> float | None:
    """Adverse trigger->fill slippage in bps (positive = adverse)."""
    entry = _money(entry_price)
    reference = _money(reference_price)
    if entry is None or reference is None or entry <= 0 or reference <= 0:
        return None
    if side == "LONG":
        raw = (entry - reference) / reference * Decimal("10000")
    elif side == "SHORT":
        raw = (reference - entry) / reference * Decimal("10000")
    else:
        return None
    return float(raw)


def decision_vs_outcome(decision: object, side: str, net_pnl: object) -> dict[str, Any]:
    """Compare the persisted entry decision with the trade direction and result."""
    if not isinstance(decision, str) or decision not in {"ENTER_LONG", "ENTER_SHORT", "NO_ENTRY"}:
        return {
            "status": "N/A",
            "decision": None,
            "direction_agrees": None,
            "matched": None,
            "net_pnl_usd": _money_text(net) if (net := _money(net_pnl)) is not None else None,
            "reasons": ["no persisted entry decision is available"],
        }
    expected_side = {"ENTER_LONG": "LONG", "ENTER_SHORT": "SHORT"}.get(decision)
    direction_agrees = expected_side is not None and expected_side == side
    net = _money(net_pnl)
    if net is None:
        return {
            "status": "PARTIAL",
            "decision": decision,
            "direction_agrees": direction_agrees,
            "matched": None,
            "net_pnl_usd": None,
            "reasons": ["the trade net PnL is unreadable; the outcome match is not measured"],
        }
    return {
        "status": "OK",
        "decision": decision,
        "direction_agrees": direction_agrees,
        "matched": direction_agrees and net > 0,
        "net_pnl_usd": _money_text(net),
        "reasons": [],
    }


__all__ = [
    "CSV_COLUMNS",
    "DEFINITIONS",
    "MAX_PERIOD_DAYS",
    "CsvExport",
    "JournalAvgR",
    "JournalError",
    "JournalStats",
    "JournalTotals",
    "Period",
    "PeriodWindow",
    "compute_stats",
    "csv_document",
    "csv_row",
    "decimal_text",
    "decision_vs_outcome",
    "neutralize_csv_cell",
    "parse_period",
    "resolve_period",
    "trigger_to_fill_bps",
    "zone_vs_fill",
]
