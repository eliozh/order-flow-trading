"""Stable human-readable CLI rendering."""

from __future__ import annotations

from oft.application.status import StatusReport


def render_status_table(report: StatusReport) -> str:
    """Render a deterministic, copy-friendly status table."""
    rows = (
        ("Environment", report.env),
        ("Status", report.status),
        ("Version", report.version),
        ("API bind", f"{report.host}:{report.port}"),
        ("Data dir", report.data_dir),
        ("Log level", report.log_level),
        ("Symbols", str(report.universe.count)),
        ("TradFi symbols", str(report.universe.tradifi_count)),
        ("Universe source", report.universe.source),
        ("Generated at", report.generated_at),
    )
    width = max(len(label) for label, _ in rows)
    lines = [f"{label:<{width}}  {value}" for label, value in rows]
    lines.append("")
    lines.append(f"{'SYMBOL':<14}{'CONTRACT':<22}SOURCE")
    for symbol in report.universe.symbols:
        lines.append(f"{symbol.symbol:<14}{symbol.contract_type:<22}{report.universe.source}")
    return "\n".join(lines)
