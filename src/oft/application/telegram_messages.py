"""Pure ``/status`` and ``/help`` renderers for Telegram replies.

The reply is ``key: value`` text with one event line first, ISO-8601 UTC
timestamps, and no emoji. The numbers are the same ``StatusReport`` the API and
``oft status`` render, so a Telegram reply cannot drift from the CLI.
"""

from __future__ import annotations

from oft.application.status import StatusReport
from oft.domain.telegram import HELP_COMMAND, STATUS_COMMAND, truncate_outbound


def render_help() -> str:
    """The fixed help text; only the two supported commands are listed."""
    return truncate_outbound(
        "\n".join(
            [
                "HELP",
                f"{STATUS_COMMAND} - show the current system status",
                f"{HELP_COMMAND} - list the available commands",
            ]
        )
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
    return truncate_outbound("\n".join(lines))


def render_test_message(generated_at: str) -> str:
    """One deterministic connectivity test message (no secret, no data)."""
    return truncate_outbound(
        "\n".join(
            [
                "TELEGRAM TEST",
                f"generated_at: {generated_at}",
                "read-only channel: no order or approval command exists",
            ]
        )
    )


__all__ = ["render_help", "render_status", "render_test_message"]
