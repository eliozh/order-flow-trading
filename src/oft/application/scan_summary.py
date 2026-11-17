"""Pure scan-completion derivations shared by jobs, routes, and notifications.

The scheduled runner, the manual scan route, the API payloads, and the Telegram
summary must all agree on a scan's status, failure stage, and cost totals. Those
rules live here once, as pure functions over persisted rows, so a new error code
or unpriced rule cannot silently diverge between surfaces.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, DecimalException

import structlog

from oft.domain.telegram import MAX_DECIMAL_ADJUSTED_EXPONENT
from oft.ports.store import ScanSymbolRecord

LOGGER = structlog.get_logger(__name__)

_FAILED_STAGE_BY_CODE = {
    "DATA_UNAVAILABLE": "DATA",
    "METRICS_INCOMPLETE": "METRICS",
}
# Terminal row stages and persisted run statuses both map onto the three
# notification severities. A terminal ``FAILED`` row or run dominates by rank.
_STAGE_SEVERITY = {
    "DONE": "COMPLETE",
    "DEGRADED": "PARTIAL",
    "PARTIAL": "PARTIAL",
    "FAILED": "FAILED",
}
_RUN_SEVERITY = {"DONE": "COMPLETE", "PARTIAL": "PARTIAL", "FAILED": "FAILED"}
_SEVERITY_RANK = {"COMPLETE": 0, "PARTIAL": 1, "FAILED": 2}


def stage_for_error_code(code: object) -> str:
    """The stage a failure code belongs to, mirroring the scan routes/jobs."""
    if isinstance(code, str):
        return _FAILED_STAGE_BY_CODE.get(code) or "REPORT"
    return "REPORT"


def failure_stage(exc: BaseException, code: str) -> str:
    """The stage that failed, from the scan service or the error code."""
    stage = getattr(exc, "failed_stage", None)
    if stage:
        return str(stage)
    return stage_for_error_code(code)


def scan_cost_totals(rows: Sequence[ScanSymbolRecord]) -> tuple[Decimal, bool]:
    """Aggregate child costs; an unparseable amount is surfaced as unpriced.

    A failure row carries no LLM usage, so its default ``cost_unpriced`` must
    not mark the whole batch unpriced. Only configured-LLM rows participate in
    the unpriced flag.

    An unparseable amount is skipped and marked unpriced (the finite subtotal is
    still reported). A non-finite or hostile-exponent amount (``1e100000000``)
    would overflow the sum or expand to an unbounded fixed-point string, so the
    total reads ``NaN`` and every formatter renders a bounded ``N/A`` instead.
    """
    cost = Decimal("0")
    unpriced = False
    unsafe = False
    for row in rows:
        try:
            amount = Decimal(row.cost_usd)
        except (DecimalException, TypeError, ValueError):
            unpriced = True
            LOGGER.warning(
                "scan_cost_unparseable",
                symbol=row.symbol,
                cost_usd=row.cost_usd,
            )
            continue
        if not amount.is_finite() or (
            amount != 0 and abs(amount.adjusted()) > MAX_DECIMAL_ADJUSTED_EXPONENT
        ):
            unpriced = True
            unsafe = True
            LOGGER.warning(
                "scan_cost_unparseable",
                symbol=row.symbol,
                cost_usd=row.cost_usd,
            )
            continue
        cost += amount
        if row.llm_status is not None:
            unpriced = unpriced or bool(row.cost_unpriced)
    if unsafe:
        return Decimal("NaN"), unpriced
    return cost, unpriced


def format_cost_total(cost: Decimal) -> str:
    """Render a cost total with the hostile-exponent guard.

    ``1e100000000`` would materialize a ~100 MB fixed-point string, so a
    non-finite or unbounded-exponent total is rendered as ``N/A`` up front.
    Every surface (scan API and Telegram push) uses this one formatter, so the
    ``NaN`` sentinel from :func:`scan_cost_totals` can never leak as text.
    """
    if not cost.is_finite():
        return "N/A"
    if cost == 0:
        return "0"
    if abs(cost.adjusted()) > MAX_DECIMAL_ADJUSTED_EXPONENT:
        return "N/A"
    return format(cost, "f")


def derive_scan_status(rows: Sequence[ScanSymbolRecord], run_status: object) -> str | None:
    """Merge the run status with terminal row stages: FAILED > PARTIAL > COMPLETE.

    Terminal rows are aggregated first (all ``DONE`` -> COMPLETE, all ``FAILED``
    -> FAILED, anything mixed/degraded -> PARTIAL), then merged with the run's
    own status by severity. A terminal ``FAILED`` run therefore dominates, so a
    reclaimed-lock failure cannot be masked by a previous owner's all-``DONE``
    rows, while a batch with one good and one failed symbol stays PARTIAL. When
    neither source maps, there is nothing to report yet (``None``).
    """
    severities: list[str] = []
    row_severities = [_STAGE_SEVERITY[row.stage] for row in rows if row.stage in _STAGE_SEVERITY]
    if row_severities:
        if all(severity == "COMPLETE" for severity in row_severities):
            severities.append("COMPLETE")
        elif all(severity == "FAILED" for severity in row_severities):
            severities.append("FAILED")
        else:
            severities.append("PARTIAL")
    if isinstance(run_status, str):
        run_severity = _RUN_SEVERITY.get(run_status)
        if run_severity is not None:
            severities.append(run_severity)
    if not severities:
        return None
    return max(severities, key=_SEVERITY_RANK.__getitem__)


__all__ = [
    "derive_scan_status",
    "failure_stage",
    "format_cost_total",
    "scan_cost_totals",
    "stage_for_error_code",
]
