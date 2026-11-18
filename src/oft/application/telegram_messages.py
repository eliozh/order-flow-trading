"""Pure Telegram renderers: deterministic, plain text, and unit-bearing.

Every reply is ``key: value`` text with no ASCII tables, no box drawing, and no
emoji. The first line states the event; timestamps are ISO-8601 UTC and every
number carries a unit. Values come from the same read models the Web and CLI
render, so a Telegram reply cannot drift from them. No renderer performs I/O or
calls a model; callers pass already-read payloads.

Documented maintainability exception: this module is over the 800-line soft
ceiling because every alert renderer (triggered, invalidated, entry re-check,
coalesced bursts, position management, risk, and degradation) shares the
sanitizer, decimal/unit formatting, and outbound truncation primitives defined
here. Extracting them would either import those private helpers across modules
or duplicate the sanitization boundary security depends on, so the renderers
stay next to the primitives they must use. Revisit if the alert surface grows
further: extract a ``telegram_alert_messages.py`` that imports the shared
primitives without copying them.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from oft.domain.symbols import SymbolContract
from oft.domain.telegram import (
    BUDGET_COMMAND,
    HELP_COMMAND,
    MAX_DECIMAL_ADJUSTED_EXPONENT,
    MAX_OPPORTUNITY_ALERT_ITEMS,
    MAX_SCAN_REPORT_IDS,
    OPPORTUNITIES_COMMAND,
    POSITIONS_COMMAND,
    REPORT_COMMAND,
    STATUS_COMMAND,
    TRUNCATION_MARKER,
    WATCHLIST_COMMAND,
    truncate_outbound,
)
from oft.domain.time import utc_timestamp

POSITIONS_EMPTY_TEXT = "没有未平仓持仓。"
OPPORTUNITIES_EMPTY_TEXT = "当前没有开放或触发的机会。"
REPORT_EMPTY_TEXT = "还没有报告，先运行一次扫描。"
REPORT_AMBIGUOUS_TEXT = "存在多个品种的报告，请使用 /report SYMBOL。"
REPORT_SYMBOL_EMPTY_TEXT = "该品种没有报告。"
WATCHLIST_EMPTY_TEXT = "还没有星标品种。"
QUERY_ERROR_INVALID_ARGUMENT = "参数无效"
QUERY_ERROR_UNAVAILABLE = "数据不可用"
READ_ONLY_NOTICE = "MVP 只读：不支持任何写入或审批操作。"
# The documented F10e parity line: threshold crossings alert, calls continue.
BUDGET_CONTINUES_NOTICE = "达到 80% 与 100% 阈值时经 Telegram 告警；不会停止调用。"
# The one instruction an entry re-check alert may carry: the human acts on the
# desktop or CLI. The push itself never renders an approval shortcut.
ENTRY_RECHECK_INSTRUCTION = "请在桌面端或 CLI 处理。"
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


class _StatusLastScanLike(Protocol):
    """The last-scan fields :func:`render_status` reads."""

    @property
    def scan_id(self) -> str: ...
    @property
    def symbol(self) -> str: ...
    @property
    def status(self) -> str: ...
    @property
    def delta(self) -> str: ...
    @property
    def cvd(self) -> str: ...
    @property
    def finished_at(self) -> str: ...


class _StatusScheduleLike(Protocol):
    """The schedule fields :func:`render_status` reads."""

    @property
    def period(self) -> str: ...
    @property
    def next_run_at(self) -> str: ...


class _StatusUniverseLike(Protocol):
    """The universe summary fields :func:`render_status` reads."""

    @property
    def count(self) -> int: ...
    @property
    def tradifi_count(self) -> int: ...


class _StatusDaemonLike(Protocol):
    """The daemon probe field :func:`render_status` reads."""

    @property
    def state(self) -> str: ...


class StatusReportLike(Protocol):
    """The status report surface :func:`render_status` dereferences.

    Typed structurally here instead of importing ``oft.application.status``:
    that module reaches the ingestion supervisor and an adapter, and the
    import-linter "Execution path never imports adapters" contract must hold.
    A local protocol keeps attribute-level mypy checking without the edge.
    """

    @property
    def env(self) -> str: ...
    @property
    def version(self) -> str: ...
    @property
    def host(self) -> str: ...
    @property
    def port(self) -> int: ...
    @property
    def generated_at(self) -> str: ...
    @property
    def daemon(self) -> _StatusDaemonLike: ...
    @property
    def universe(self) -> _StatusUniverseLike: ...
    @property
    def schedule(self) -> _StatusScheduleLike | None: ...
    @property
    def last_scan(self) -> _StatusLastScanLike | None: ...


def render_status(report: StatusReportLike) -> str:
    """Render the shared status report as deterministic ``key: value`` lines.

    The report is typed by the local :class:`StatusReportLike` protocol so this
    pure renderer does not import the status service: that edge would pull the
    ingestion supervisor and an adapter into the execution path's import graph.
    """
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
    if mark is not None and _plain_decimal(mark) is None:
        # A corrupt/absurd stored decimal cannot be rendered, so it is a
        # missing observation rather than a FRESH mark with ``N/A``.
        mark = None
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


def render_scan_summary(payload: Mapping[str, Any]) -> str:
    """Render one scan-completion push: deterministic, plain text, bounded.

    The first line stamps COMPLETE/PARTIAL/FAILED. Every dynamic value is
    sanitized to a single line, the report-id list is bounded, and a failure is
    described by its stage and stable reason code only -- never by an exception
    message, traceback, narrative, or provider body.
    """
    status = _scan_status(payload.get("status"))
    report_ids = _scan_report_ids(payload.get("report_ids"))
    lines = [
        f"SCAN {status}",
        f"status: {status}",
        f"scan_id: {_text(payload.get('scan_id'))}",
        f"trigger: {_text(payload.get('trigger_source'))}",
        f"period: {_text(payload.get('period'))}",
        f"symbols: {_count_text(payload.get('symbols_completed'))}/"
        f"{_count_text(payload.get('symbols_total'))}",
        f"degraded: {_count_text(payload.get('symbols_degraded'))}",
        f"failed: {_count_text(payload.get('symbols_failed'))}",
        f"new_opportunities: {_count_text(payload.get('new_opportunities'))}",
        f"cost: {_scan_cost(payload.get('cost_usd'), payload.get('cost_unpriced'))}",
        f"reports: {_count_text(payload.get('reports_total'))}",
        f"generated_at: {_timestamp(payload.get('generated_at'))}",
        f"report_ids: {', '.join(report_ids) if report_ids else 'none'}",
    ]
    if status == "FAILED":
        lines.extend(
            [
                f"stage: {_text(payload.get('failed_stage'))}",
                f"reason_code: {_text(payload.get('reason_code'))}",
            ]
        )
    return truncate_outbound("\n".join(lines))


def _scan_status(value: object) -> str:
    status = value.upper() if isinstance(value, str) else ""
    return status if status in ("COMPLETE", "PARTIAL", "FAILED") else "FAILED"


def _scan_report_ids(value: object) -> tuple[str, ...]:
    report_ids: list[str] = []
    for item in _sequence(value):
        cleaned = _single_line(item, limit=128)
        if cleaned:
            report_ids.append(cleaned)
        if len(report_ids) >= MAX_SCAN_REPORT_IDS:
            break
    return tuple(report_ids)


def _scan_cost(value: object, unpriced: object) -> str:
    cleaned = _single_line(value, limit=64) if value not in (None, "") else "N/A"
    suffix = " (unpriced)" if unpriced is True else ""
    return f"{cleaned} USDT{suffix}"


def render_trigger_alert(payload: Mapping[str, Any]) -> str:
    """Render one TRIGGERED opportunity push: plain text, bounded, no narrative.

    The payload is already an allowlist built by the event builder; here every
    dynamic value is still sanitized to a single line so stored/hostile text
    cannot inject a structured field. Signal-bearing numbers carry a unit.
    """
    lines = [
        f"机会触发 · {_symbol_text(payload.get('symbol'))} · "
        f"{_text(payload.get('side'))} · 置信度 {_decimal_text(payload.get('confidence'))}",
        f"opportunity: {_text(payload.get('opportunity_id'))}",
        "state: TRIGGERED",
        f"zone: {_decimal_text(payload.get('zone_low'))} - "
        f"{_decimal_text(payload.get('zone_high'))} USDT",
    ]
    for raw in _sequence(payload.get("trigger_levels")):
        level = _mapping(raw)
        unit = _condition_unit(level.get("type"))
        lines.append(
            f"trigger: {_text(level.get('condition_id'))} {_text(level.get('type'))} "
            f"observed {_level_text(level.get('observed_value'), unit)} "
            f"threshold {_level_text(level.get('threshold'), unit)}"
        )
    for raw in _sequence(payload.get("invalidation_levels")):
        level = _mapping(raw)
        unit = _condition_unit(level.get("type"))
        lines.append(
            f"invalidation: {_text(level.get('condition_id'))} "
            f"{_text(level.get('type'))} {_level_text(level.get('level'), unit)}"
        )
    lines.extend(
        [
            f"trigger_id: {_text(payload.get('trigger_id'))}",
            f"triggered_at: {_timestamp(payload.get('triggered_at'))}",
            f"state_version: {_count_text(payload.get('state_version'))}",
            OPPORTUNITIES_COMMAND,
        ]
    )
    return truncate_outbound("\n".join(lines))


def render_invalidation_alert(payload: Mapping[str, Any]) -> str:
    """Render one INVALIDATED opportunity push with its stable reason code.

    The free-text note is deliberately absent; a reader gets the machine reason
    code and the ledger pointer, never operator prose or a path.
    """
    lines = [
        f"机会失效 · {_symbol_text(payload.get('symbol'))} · {_text(payload.get('side'))}",
        f"reason_code: {_text(payload.get('reason_code'))}",
        "state: INVALIDATED",
        f"opportunity: {_text(payload.get('opportunity_id'))}",
        f"invalidated_at: {_timestamp(payload.get('invalidated_at'))}",
        f"state_version: {_count_text(payload.get('state_version'))}",
        OPPORTUNITIES_COMMAND,
    ]
    return truncate_outbound("\n".join(lines))


def render_entry_recheck_alert(payload: Mapping[str, Any]) -> str:
    """Render one entry re-check awaiting a human, read-only by construction.

    Only the decision identity, confidence, and the desktop/CLI instruction are
    rendered. No rationale, provider text, approval URL, or command token can
    appear: none is in the payload, and the instruction is this module's
    constant — a payload-supplied ``instruction`` is deliberately ignored so a
    future caller cannot turn the alert into an approval surface.
    """
    lines = [
        f"入场复核 · {_symbol_text(payload.get('symbol'))} · "
        f"{_text(payload.get('side'))} · 置信度 {_decimal_text(payload.get('confidence'))}",
        f"decision: {_text(payload.get('decision'))}",
        f"decision_id: {_text(payload.get('decision_id'))}",
        f"trigger_id: {_text(payload.get('trigger_id'))}",
        f"opportunity: {_text(payload.get('opportunity_id'))}",
        ENTRY_RECHECK_INSTRUCTION,
        OPPORTUNITIES_COMMAND,
    ]
    return truncate_outbound("\n".join(lines))


def render_opportunity_burst(payload: Mapping[str, Any]) -> str:
    """Render a coalesced multi-trigger push with a bounded item list.

    At most ``MAX_OPPORTUNITY_ALERT_ITEMS`` items are shown; the rest become an
    explicit count plus the ``/opportunities`` pointer so the reader can see the
    true scale without an unbounded message.
    """
    count = payload.get("count")
    total = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
    items = _sequence(payload.get("items"))[:MAX_OPPORTUNITY_ALERT_ITEMS]
    lines = [f"机会触发 {_count_text(count)} 个"]
    for raw in items:
        item = _mapping(raw)
        lines.append(
            f"- {_symbol_text(item.get('symbol'))} {_text(item.get('side'))} "
            f"confidence {_decimal_text(item.get('confidence'))} "
            f"opportunity_id {_text(item.get('opportunity_id'))}"
        )
    remaining = payload.get("remaining")
    if isinstance(remaining, bool) or not isinstance(remaining, int):
        remaining = max(0, total - len(items))
    if remaining > 0:
        lines.append(f"另有 {remaining} 个")
    lines.append("请使用 /opportunities 查看")
    return truncate_outbound("\n".join(lines))


def render_position_management_alert(payload: Mapping[str, Any]) -> str:
    """Render one executed/blocked management outcome, read-only by construction.

    The payload is already an allowlist; every dynamic value is sanitized to a
    single line. The message names the final size and price, the status, and the
    stable reason code. It deliberately carries no approval URL, command token,
    callback data, or inline keyboard: none is in the payload, and the closing
    notice is this module's constant.
    """
    lines = [
        f"持仓管理 · {_symbol_text(payload.get('symbol'))} · {_text(payload.get('action'))}",
        f"position: {_text(payload.get('position_id'))}",
        f"action: {_text(payload.get('action'))}",
        f"status: {_text(payload.get('status'))}",
        f"size: {_decimal_text(payload.get('size'))}",
        f"price: {_usd_text(_decimal_or_none(payload.get('price')))}",
        f"reason_code: {_text(payload.get('reason_code'))}",
        f"decision_id: {_text(payload.get('decision_id'))}",
        f"occurred_at: {_timestamp(payload.get('occurred_at'))}",
        READ_ONLY_NOTICE,
    ]
    return truncate_outbound("\n".join(lines))


def render_budget_alert(payload: Mapping[str, Any]) -> str:
    """Render one budget-threshold push, read-only and bounded.

    The payload is the allowlisted audit-event subset. Every dynamic value is
    sanitized to a single line; spend and threshold carry USDT, and an
    unreadable unpriced count is ``N/A`` rather than a fabricated zero. The
    closing notice is this module's constant so no caller can add an action.
    """
    period = _text(payload.get("period"))
    state = _text(payload.get("state"))
    lines = [
        f"预算告警 · {period.upper()} · {state}",
        f"period: {period}",
        f"period_key: {_text(payload.get('period_key'))}",
        f"spend_usd: {_usd_text(_decimal_or_none(payload.get('spend_usd')))}",
        f"threshold_usd: {_usd_text(_decimal_or_none(payload.get('threshold_usd')))}",
        f"percentage: {_pct_text(payload.get('percentage'))}",
        f"state: {state}",
        f"unpriced_count: {_count_text(payload.get('unpriced_count'))}",
        f"occurred_at: {_timestamp(payload.get('occurred_at'))}",
        BUDGET_CONTINUES_NOTICE,
    ]
    return truncate_outbound("\n".join(lines))


def render_risk_alert(payload: Mapping[str, Any]) -> str:
    """Render one daily-loss/leverage/notional risk alert, bounded and read-only."""
    kind = _text(payload.get("kind"))
    state = _text(payload.get("state"))
    unit = _text(payload.get("unit"), default="") if payload.get("unit") else ""
    lines = [
        f"风险告警 · {kind} · {state}",
        f"kind: {kind}",
        f"state: {state}",
        f"subject: {_text(payload.get('subject'))}",
        f"binding_cap: {_text(payload.get('binding_cap'))}",
        f"observed: {_unit_value(payload.get('observed'), unit)}",
        f"threshold: {_unit_value(payload.get('threshold'), unit)}",
        f"period: {_text(payload.get('period'))}",
        f"reason_code: {_text(payload.get('reason_code'))}",
        f"occurred_at: {_timestamp(payload.get('occurred_at'))}",
        READ_ONLY_NOTICE,
    ]
    return truncate_outbound("\n".join(lines))


_DEGRADATION_LABELS = {"DEGRADED": "系统降级", "RECOVERED": "系统恢复"}


def render_degradation_alert(payload: Mapping[str, Any]) -> str:
    """Render one DEGRADED/RECOVERED system alert (market data, WS, provider)."""
    state = payload.get("state")
    label = _DEGRADATION_LABELS.get(state, "系统状态") if isinstance(state, str) else "系统状态"
    lines = [
        f"{label} · {_text(payload.get('subject'))}",
        f"state: {_text(state)}",
        f"reason_code: {_text(payload.get('reason_code'))}",
        f"occurred_at: {_timestamp(payload.get('occurred_at'))}",
    ]
    return truncate_outbound("\n".join(lines))


def _unit_value(value: object, unit: str) -> str:
    """One observed/threshold value with its unit; missing values stay ``N/A``."""
    if value is None or value == "":
        return "N/A"
    rendered = _decimal_text(value, default="")
    if not rendered:
        return _text(value)
    return f"{rendered} {unit}".strip()


def _condition_unit(value: object) -> str:
    """The display unit for one condition type (empty when it has none).

    Only price and liquidation notional are quoted in USDT. An ``OI_DELTA_*``
    condition is evaluated as a percent change (``(last-first)/first*100``).
    ``DELTA_*``, ``CVD_*``, and ``VOLUME_*`` are base-asset quantities from the
    resampled bars (not quote volume), so they carry no fabricated unit; funding
    and footprint imbalance are ratios. Everything else is categorical.
    """
    condition_type = value.upper() if isinstance(value, str) else ""
    if condition_type.startswith(("PRICE_", "LIQUIDATION_")):
        return "USDT"
    if condition_type.startswith("OI_DELTA_"):
        return "%"
    if condition_type.startswith(("FUNDING_", "FOOTPRINT_")):
        return "ratio"
    return ""


def _level_text(value: object, unit: str) -> str:
    if not unit:
        return _text(value)
    rendered = _decimal_text(value)
    if rendered != "N/A":
        return f"{rendered} {unit}"
    # A zone threshold is recorded as a range (for example
    # ``69900.00-70100.00``): keep the recorded plain text rather than a bare
    # ``N/A`` that would hide the level the reader is deciding on.
    return _text(value)


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
    if abs(value.adjusted()) > MAX_DECIMAL_ADJUSTED_EXPONENT:
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
    "BUDGET_CONTINUES_NOTICE",
    "ENTRY_RECHECK_INSTRUCTION",
    "NARRATIVE_EXCERPT_CHARS",
    "OPPORTUNITIES_EMPTY_TEXT",
    "POSITIONS_EMPTY_TEXT",
    "QUERY_ERROR_INVALID_ARGUMENT",
    "QUERY_ERROR_UNAVAILABLE",
    "READ_ONLY_NOTICE",
    "REPORT_AMBIGUOUS_TEXT",
    "REPORT_EMPTY_TEXT",
    "REPORT_SYMBOL_EMPTY_TEXT",
    "STATUS_HELP_FIELDS",
    "WATCHLIST_EMPTY_TEXT",
    "render_budget",
    "render_budget_alert",
    "render_degradation_alert",
    "render_entry_recheck_alert",
    "render_help",
    "render_invalidation_alert",
    "render_opportunities",
    "render_opportunity_burst",
    "render_position_management_alert",
    "render_positions",
    "render_query_error",
    "render_report",
    "render_risk_alert",
    "render_scan_summary",
    "render_status",
    "render_test_message",
    "render_trigger_alert",
    "render_watchlist",
]
