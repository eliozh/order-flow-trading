"""Pure Telegram renderers: deterministic, plain text, and unit-bearing.

Every reply is ``key: value`` text with no ASCII tables, no box drawing, and no
emoji. The first line states the event; timestamps are ISO-8601 UTC and every
number carries a unit. Values come from the same read models the Web and CLI
render, so a Telegram reply cannot drift from them. No renderer performs I/O or
calls a model; callers pass already-read payloads.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from oft.application.status import StatusReport
from oft.domain.symbols import SymbolContract
from oft.domain.telegram import (
    BUDGET_COMMAND,
    HELP_COMMAND,
    OPPORTUNITIES_COMMAND,
    POSITIONS_COMMAND,
    REPORT_COMMAND,
    STATUS_COMMAND,
    TRUNCATION_MARKER,
    WATCHLIST_COMMAND,
)
from oft.domain.time import utc_timestamp

POSITIONS_EMPTY_TEXT = "没有未平仓持仓。"
OPPORTUNITIES_EMPTY_TEXT = "当前没有开放或触发的机会。"
REPORT_EMPTY_TEXT = "还没有报告，先运行一次扫描。"
WATCHLIST_EMPTY_TEXT = "还没有星标品种。"
QUERY_ERROR_INVALID_ARGUMENT = "参数无效"
QUERY_ERROR_UNAVAILABLE = "数据不可用"
READ_ONLY_NOTICE = "MVP 只读：不支持任何写入或审批操作。"
NARRATIVE_EXCERPT_CHARS = 600
NO_MARK_REASON = "无记录的标记价格"
UNMEASURED_PNL_REASON = "标记或入场价格不可用"

_MARK_FRESH = "FRESH"
_MARK_STALE = "STALE"
_MARK_MISSING = "MISSING"
_PERIOD_LABELS = {"day": "DAY", "week": "WEEK", "month": "MONTH"}
_THRESHOLD_KEYS = {"day": "daily_usd", "week": "weekly_usd", "month": "monthly_usd"}
# Controls/format characters are replaced by a space; emoji (``So``) and
# combining marks (``Mn``/``Mc``) are dropped per design A10 ("No emoji",
# avoid combining diacritics). CJK, Latin, digits, and basic punctuation pass.
_PLAIN_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "So", "Mn", "Mc"})
# A corrupt stored decimal with an absurd exponent must render ``N/A`` instead
# of expanding to an unbounded fixed-point string (``1e100000000``).
_MAX_DECIMAL_ADJUSTED_EXPONENT = 30
# The ``/status`` concepts advertised by ``/help``. Every key here is a field
# ``render_status`` can actually produce, so the help copy cannot drift from
# the reply. The value is the plain Chinese label the help line shows.
STATUS_HELP_FIELDS: tuple[tuple[str, str], ...] = (
    ("env", "环境"),
    ("daemon", "守护进程"),
    ("symbols", "品种"),
    ("next_run", "下一次扫描"),
    ("last_scan", "最近扫描"),
)


def render_help() -> str:
    """The fixed help text listing the closed read-only command table.

    Every ``/status`` concept named here is a field ``render_status`` renders;
    the command list is bounded by construction, and the service owns the one
    outbound cap (``budget_outbound``) so a long render still gains the ``…``
    marker and reference id.
    """
    status_fields = "、".join(label for _key, label in STATUS_HELP_FIELDS)
    return "\n".join(
        [
            "HELP",
            f"{STATUS_COMMAND} - {status_fields}",
            f"{POSITIONS_COMMAND} - 未平仓持仓",
            f"{OPPORTUNITIES_COMMAND} - 开放与已触发的机会",
            f"{REPORT_COMMAND} [symbol] - 最新报告摘要",
            f"{BUDGET_COMMAND} - 日/周/月支出与阈值",
            f"{WATCHLIST_COMMAND} - 星标品种分组",
            f"{HELP_COMMAND} - 命令列表",
            READ_ONLY_NOTICE,
        ]
    )


def render_status(report: StatusReport) -> str:
    """Render the shared status report as deterministic ``key: value`` lines."""
    lines = [
        "STATUS",
        f"env: {report.env}",
        f"version: {report.version}",
        f"host: {report.host}",
        f"port: {report.port}",
        f"daemon: {report.daemon.state}",
        f"symbols: {report.universe.count}",
        f"tradifi_symbols: {report.universe.tradifi_count}",
        f"generated_at: {report.generated_at}",
    ]
    if report.schedule is not None:
        lines.extend(
            [
                f"period: {report.schedule.period}",
                f"next_run: {report.schedule.next_run_at}",
            ]
        )
    if report.last_scan is not None:
        scan = report.last_scan
        lines.extend(
            [
                f"last_scan: {scan.scan_id}",
                f"last_scan_symbol: {scan.symbol}",
                f"last_scan_status: {scan.status}",
                f"last_scan_delta: {scan.delta}",
                f"last_scan_cvd: {scan.cvd}",
                f"last_scan_finished_at: {scan.finished_at}",
            ]
        )
    return "\n".join(lines)


def render_test_message(generated_at: str) -> str:
    """One deterministic connectivity test message (no secret, no data)."""
    return "\n".join(
        [
            "TELEGRAM TEST",
            f"generated_at: {generated_at}",
            "read-only channel: no order or approval command exists",
        ]
    )


def render_positions(
    payloads: Sequence[Mapping[str, Any]],
    *,
    now: str,
    stale_seconds: int,
) -> str:
    """Render OPEN positions with the server-resolved mark and freshness."""
    if not payloads:
        return POSITIONS_EMPTY_TEXT
    now_dt = _parse_timestamp(now)
    lines = ["POSITIONS", f"count: {len(payloads)}", f"generated_at: {_timestamp(now)}"]
    for payload in payloads:
        lines.extend(_position_block(payload, now_dt=now_dt, stale_seconds=stale_seconds))
    return "\n".join(lines)


def _position_block(
    payload: Mapping[str, Any],
    *,
    now_dt: datetime | None,
    stale_seconds: int,
) -> list[str]:
    mark = _decimal_or_none(_resolved_mark(payload))
    as_of = _resolved_mark_as_of(payload)
    state = _mark_state(mark, as_of, now_dt=now_dt, stale_seconds=stale_seconds)
    entry = _decimal_or_none(payload.get("avg_entry_price") or payload.get("entry_price"))
    size = _decimal_or_none(payload.get("size"))
    side = str(payload.get("side") or "").upper()
    stop = _decimal_or_none(payload.get("stop_price"))

    block = [
        "",
        f"position: {_text(payload.get('position_id'))}",
        f"symbol: {_text(payload.get('symbol'))}",
        f"side: {_text(payload.get('side'))}",
        f"size: {_decimal_text(payload.get('size'))}",
        f"entry: {_usd_text(entry)}",
    ]
    if mark is None:
        block.extend(["mark: N/A", f"mark_as_of: {_timestamp(as_of)}", f"mark_state: {state}"])
        block.append(f"mark_reason: {NO_MARK_REASON}")
    elif state == _MARK_STALE:
        block.extend(
            [
                f"mark: {_decimal_text(mark)} USDT · STALE",
                f"mark_as_of: {_timestamp(as_of)}",
                f"mark_state: {state}",
            ]
        )
    else:
        block.extend(
            [
                f"mark: {_decimal_text(mark)} USDT",
                f"mark_as_of: {_timestamp(as_of)}",
                f"mark_state: {state}",
            ]
        )
    upnl = _unrealized_pnl(mark=mark, entry=entry, size=size, side=side)
    if upnl is None:
        block.extend(["upnl: N/A", f"upnl_reason: {UNMEASURED_PNL_REASON}"])
    else:
        block.append(f"upnl: {_decimal_text(upnl)} USDT")
    block.append(f"stop: {_usd_text(stop)}")
    return block


def render_opportunities(rows: Sequence[Mapping[str, Any]], *, now: str) -> str:
    """Render OPEN and TRIGGERED opportunities with their ids and states."""
    if not rows:
        return OPPORTUNITIES_EMPTY_TEXT
    lines = ["OPPORTUNITIES", f"count: {len(rows)}", f"generated_at: {_timestamp(now)}"]
    for row in rows:
        zone = _mapping(row.get("zone"))
        lines.extend(
            [
                "",
                f"opportunity: {_text(row.get('opportunity_id'))}",
                f"symbol: {_text(row.get('symbol'))}",
                f"side: {_text(row.get('side'))}",
                f"state: {_text(row.get('state'))}",
                f"confidence: {_decimal_text(row.get('confidence'))}",
                f"zone: {_decimal_text(zone.get('low'))} - {_decimal_text(zone.get('high'))} USDT",
                f"created_at: {_timestamp(row.get('created_at'))}",
            ]
        )
    return "\n".join(lines)


def render_report(payload: Mapping[str, Any]) -> str:
    """Render one stored report from whitelisted CALC and LLM fields.

    The narrative is untrusted plain text: it is sanitized and bounded and is
    never accompanied by raw prompts, provider keys, or HTML. ``data_quality``
    and the stored ``snapshot.quality_reasons`` are copied so PARTIAL/STALE is
    never presented as COMPLETE. A family without measured samples renders
    ``N/A`` with a named reason rather than the document's fallback zero, and
    liquidation coverage/limitations are named when the snapshot carries them.
    """
    metrics = _mapping(payload.get("metrics"))
    snapshot = _mapping(payload.get("snapshot"))
    cvd = _mapping(metrics.get("cvd"))
    summary = _mapping(cvd.get("summary"))
    points = _sequence(cvd.get("points"))
    latest_delta = _mapping(points[-1]).get("delta") if points else None
    profile = _mapping(metrics.get("volume_profile"))
    open_interest = _mapping(metrics.get("open_interest"))
    funding = _mapping(metrics.get("funding"))
    liquidations = _mapping(metrics.get("liquidations")) or _mapping(
        _mapping(snapshot.get("orderflow")).get("liquidations")
    )
    llm = _mapping(payload.get("llm"))

    # Mirror the canonical export's measuredness rule: a family is only
    # rendered when it carries samples. The production writer persists a
    # fallback zero with an empty ``points``/``history`` when a family was not
    # measured; that zero must never be presented as a measurement.
    oi_measured = _has_items(open_interest, "points")
    funding_measured = _has_items(funding, "history")
    oi_value = (
        _decimal_text(open_interest.get("delta_over_window"))
        if oi_measured
        else "N/A — open interest not measured"
    )
    funding_value = (
        _decimal_text(funding.get("current_rate"))
        if funding_measured
        else "N/A — funding not measured"
    )

    lines = [
        "REPORT",
        f"report_id: {_text(payload.get('report_id'))}",
        f"symbol: {_text(payload.get('symbol'))}",
        f"timeframe: {_text(payload.get('timeframe'))}",
        f"generated_at: {_timestamp(payload.get('generated_at'))}",
        f"data_quality: {_text(payload.get('data_quality'))}",
    ]
    reasons = _sequence(snapshot.get("quality_reasons")) or _sequence(
        metrics.get("quality_reasons")
    )
    reason_text = _reason_text(reasons)
    if reason_text:
        lines.append(f"quality_reasons: {reason_text}")
    lines.extend(
        [
            "",
            "CALC",
            f"cvd_change: {_decimal_text(summary.get('cvd_change'))}",
            f"delta: {_decimal_text(latest_delta)}",
            f"poc: {_decimal_text(profile.get('poc'))}",
            f"vah: {_decimal_text(profile.get('vah'))}",
            f"val: {_decimal_text(profile.get('val'))}",
            f"open_interest_delta: {oi_value}",
            f"funding_rate: {funding_value}",
        ]
    )
    coverage = liquidations.get("coverage")
    if isinstance(coverage, str) and coverage.strip():
        lines.append(f"liquidation_coverage: {_single_line(coverage, limit=32)}")
    limitations = _sequence(liquidations.get("limitations"))
    limitation_text = _reason_text(limitations, limit=300)
    if limitation_text:
        lines.append(f"liquidation_note: {limitation_text}")
    lines.extend(
        [
            "",
            "LLM",
            f"llm: {_llm_state(llm)}",
        ]
    )
    narrative_ref = payload.get("narrative_ref")
    if isinstance(narrative_ref, str) and narrative_ref:
        lines.append(f"narrative_ref: {_single_line(narrative_ref, limit=128)}")
    narrative = _llm_narrative(llm)
    lines.append(f"narrative: {narrative}")
    return "\n".join(lines)


def _llm_state(llm: Mapping[str, Any]) -> str:
    if not bool(llm.get("configured")):
        return "SKIPPED"
    status = llm.get("validation_status")
    if status in ("VALID", "REPAIRED"):
        return str(status)
    return "REJECTED"


def _llm_narrative(llm: Mapping[str, Any]) -> str:
    if _llm_state(llm) not in ("VALID", "REPAIRED"):
        return "N/A"
    narrative = llm.get("narrative") or llm.get("rationale")
    if not isinstance(narrative, str) or not narrative.strip():
        return "N/A"
    excerpt = _single_line(narrative, limit=NARRATIVE_EXCERPT_CHARS)
    if len(excerpt) >= NARRATIVE_EXCERPT_CHARS:
        excerpt = excerpt[: NARRATIVE_EXCERPT_CHARS - 1].rstrip() + TRUNCATION_MARKER
    return excerpt


def render_budget(snapshot: Mapping[str, Any]) -> str:
    """Render thresholds plus the day/week/month spend and states."""
    quality = "COMPLETE" if snapshot.get("usage_quality") == "COMPLETE" else "USAGE_UNAVAILABLE"
    thresholds = _mapping(snapshot.get("thresholds"))
    lines = [
        "BUDGET",
        f"usage_quality: {quality}",
        f"budget_state: {_text(snapshot.get('budget_state'))}",
        "",
        "THRESHOLDS",
        f"daily_usd: {_usd_text(_decimal_or_none(thresholds.get('daily_usd')))}",
        f"weekly_usd: {_usd_text(_decimal_or_none(thresholds.get('weekly_usd')))}",
        f"monthly_usd: {_usd_text(_decimal_or_none(thresholds.get('monthly_usd')))}",
        f"warn_pct: {_pct_text(thresholds.get('warn_pct'))}",
    ]
    periods = snapshot.get("periods")
    if not isinstance(periods, Mapping):
        lines.extend(["", "USAGE", "spend_usd: N/A", "state: USAGE_UNAVAILABLE"])
        return "\n".join(lines)
    for name, label in _PERIOD_LABELS.items():
        period = _mapping(periods.get(name))
        lines.extend(
            [
                "",
                label,
                f"spend_usd: {_usd_text(_decimal_or_none(period.get('spend_usd')))}",
                "threshold_usd: "
                f"{_usd_text(_decimal_or_none(thresholds.get(_THRESHOLD_KEYS[name])))}",
                f"state: {_text(period.get('budget_state'))}",
                f"call_count: {_count_text(period.get('call_count'))}",
                f"unpriced_count: {_count_text(period.get('unpriced_count'))}",
            ]
        )
    return "\n".join(lines)


def render_watchlist(contracts: Sequence[SymbolContract]) -> str:
    """Render the starred symbols grouped by CRYPTO / TRADFI, sorted by symbol.

    The concrete ``SymbolContract`` type (not ``Any`` + ``getattr``) lets mypy
    catch a field rename, and every stored symbol passes the shared plain-text
    sanitizer before joining, so a tampered registry row cannot inject a newline,
    control character, or bidi/emoji run into the reply's line structure.
    """
    if not contracts:
        return WATCHLIST_EMPTY_TEXT
    lines = ["WATCHLIST", "scope: starred", f"count: {len(contracts)}"]
    for label, tradifi in (("CRYPTO", False), ("TRADFI", True)):
        symbols = sorted(
            _symbol_text(contract.symbol)
            for contract in contracts
            if bool(contract.tradifi) is tradifi
        )
        lines.extend(["", label, f"count: {len(symbols)}"])
        if symbols:
            lines.append("symbols: " + ", ".join(symbols))
    return "\n".join(lines)


def render_query_error(reason: object, reference_id: str) -> str:
    """The honest query-failure reply; never a traceback or raw input."""
    safe_reason = _single_line(reason, limit=64) or QUERY_ERROR_UNAVAILABLE
    return "\n".join(
        [
            f"查询失败：{safe_reason}。请重试或查看桌面端。",
            f"reference_id: {_single_line(reference_id, limit=32)}",
        ]
    )


# -- shared formatting ---------------------------------------------------


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, str) else ()


def _has_items(mapping: Mapping[str, Any], key: str) -> bool:
    """Whether a metric family carries at least one measured sample.

    The canonical export uses the same rule (``report_export._has_items``).
    """
    return bool(_sequence(mapping.get(key)))


def _reason_text(values: Sequence[Any], *, limit: int = 200) -> str:
    """Join stored reasons as one sanitized plain-text line."""
    cleaned = (_single_line(value, limit=limit) for value in values)
    return "; ".join(reason for reason in cleaned if reason)


def _symbol_text(value: object, *, limit: int = 24) -> str:
    return _single_line(value, limit=limit)


def _text(value: object, default: str = "N/A") -> str:
    if value is None or value == "":
        return default
    return _single_line(value, limit=128)


def _text_or_na(value: object) -> str:
    return _text(value)


def _count_text(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return "N/A"
    return str(value)


def _single_line(value: object, *, limit: int) -> str:
    """Strip controls/zero-width/bidi, collapse whitespace, and bound length.

    A stripped control/format character becomes a space so adjacent words stay
    separate (``bad\\x00reason`` renders ``bad reason``); the result is one line
    of plain text.
    """
    if not isinstance(value, str):
        value = str(value)
    cleaned = "".join(
        " " if unicodedata.category(character) in _PLAIN_CONTROL_CATEGORIES else character
        for character in unicodedata.normalize("NFC", value)
    )
    collapsed = " ".join(cleaned.split())
    return collapsed[:limit]


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return parsed if parsed.is_finite() else None


def _decimal_text(value: object, default: str = "N/A") -> str:
    parsed = _decimal_or_none(value)
    if parsed is None:
        return default
    rendered = _plain_decimal(parsed)
    return default if rendered is None else rendered


def _plain_decimal(value: Decimal) -> str | None:
    if value == 0:
        return "0"
    if abs(value.adjusted()) > _MAX_DECIMAL_ADJUSTED_EXPONENT:
        # A corrupt/hostile exponent (for example ``1e100000000``) would expand
        # to an unbounded string; render ``N/A`` instead of burning CPU/memory.
        return None
    return format(value.normalize(), "f")


def _usd_text(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    rendered = _plain_decimal(value)
    return "N/A" if rendered is None else f"{rendered} USDT"


def _pct_text(value: object) -> str:
    parsed = _decimal_or_none(value)
    if parsed is None:
        return "N/A"
    rendered = _plain_decimal(parsed)
    return "N/A" if rendered is None else f"{rendered} %"


def _timestamp(value: object) -> str:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return "N/A"
    return utc_timestamp(parsed)


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _resolved_mark(payload: Mapping[str, Any]) -> object:
    resolved = payload.get("resolved_mark_price")
    return resolved if resolved not in (None, "") else payload.get("mark_price")


def _resolved_mark_as_of(payload: Mapping[str, Any]) -> object:
    resolved = payload.get("resolved_mark_as_of")
    return resolved if resolved not in (None, "") else payload.get("updated_at")


def _mark_state(
    mark: Decimal | None,
    as_of: object,
    *,
    now_dt: datetime | None,
    stale_seconds: int,
) -> str:
    if mark is None:
        return _MARK_MISSING
    observed = _parse_timestamp(as_of)
    if observed is None or now_dt is None:
        return _MARK_STALE
    age_seconds = (now_dt - observed).total_seconds()
    return _MARK_STALE if age_seconds > stale_seconds else _MARK_FRESH


def _unrealized_pnl(
    *, mark: Decimal | None, entry: Decimal | None, size: Decimal | None, side: str
) -> Decimal | None:
    if mark is None or entry is None or size is None or side not in ("LONG", "SHORT"):
        return None
    sign = Decimal("1") if side == "LONG" else Decimal("-1")
    try:
        return (mark - entry) * size * sign
    except ArithmeticError:  # pragma: no cover - finite Decimals cannot overflow here
        return None


__all__ = [
    "NARRATIVE_EXCERPT_CHARS",
    "OPPORTUNITIES_EMPTY_TEXT",
    "POSITIONS_EMPTY_TEXT",
    "QUERY_ERROR_INVALID_ARGUMENT",
    "QUERY_ERROR_UNAVAILABLE",
    "READ_ONLY_NOTICE",
    "REPORT_EMPTY_TEXT",
    "STATUS_HELP_FIELDS",
    "WATCHLIST_EMPTY_TEXT",
    "render_budget",
    "render_help",
    "render_opportunities",
    "render_positions",
    "render_query_error",
    "render_report",
    "render_status",
    "render_test_message",
    "render_watchlist",
]
