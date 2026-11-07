"""Stable human-readable CLI rendering."""

from __future__ import annotations

from oft.application.health import DoctorReport
from oft.application.status import StatusReport
from oft.ports.store import ScanRecord


def render_status_table(report: StatusReport) -> str:
    """Render a deterministic, copy-friendly status table."""
    rows: list[tuple[str, str]] = [
        ("Environment", report.env),
        ("Status", report.status),
        ("Version", report.version),
        ("API bind", f"{report.host}:{report.port}"),
        ("Data dir", report.data_dir),
        ("Log level", report.log_level),
        ("Daemon", f"{report.daemon.state} · {report.daemon.detail}"),
        ("Symbols", str(report.universe.count)),
        ("TradFi symbols", str(report.universe.tradifi_count)),
        ("Universe source", report.universe.source),
        ("Generated at", report.generated_at),
    ]
    if report.market_data is not None:
        rows.extend(
            [
                ("WS state", report.market_data.connection_state),
                ("WS stale", "yes" if report.market_data.stale else "no"),
            ]
        )
    width = max(len(label) for label, _ in rows)
    lines = [f"{label:<{width}}  {value}" for label, value in rows]
    if report.last_scan is not None:
        scan = report.last_scan
        lines.append("")
        lines.append(f"Last scan    {scan.scan_id} · {scan.symbol} · {scan.period}")
        lines.append(f"             status {scan.status} · delta {scan.delta} · cvd {scan.cvd}")
    if report.market_data is not None:
        lines.append("")
        for stream in report.market_data.streams:
            lines.append(
                f"WS {stream.stream:<32} {stream.last_event_time or 'never'}"
                f"{' STALE' if stream.stale else ''}"
            )
    lines.append("")
    lines.append(f"{'SYMBOL':<14}{'CONTRACT':<22}SOURCE")
    for symbol in report.universe.symbols:
        lines.append(f"{symbol.symbol:<14}{symbol.contract_type:<22}{report.universe.source}")
    return "\n".join(lines)


def render_doctor_table(report: DoctorReport) -> str:
    """Render named doctor checks; overall status is the last line."""
    lines = [f"oft doctor · {report.version} · {report.checked_at}", ""]
    width = max(len(check.name) for check in report.checks)
    for check in report.checks:
        lines.append(f"{check.name:<{width}}  {check.status.upper():<8}  {check.detail}")
    lines.append("")
    lines.append(f"STATUS  {report.status}")
    if report.latest_scan is not None:
        lines.append(
            f"Last scan  {report.latest_scan['scan_id']} · delta {report.latest_scan['delta']}"
        )
    return "\n".join(lines)


def render_scan_table(record: ScanRecord) -> str:
    """Render one scan summary."""
    rows = (
        ("Scan ID", record.scan_id),
        ("Symbol", record.symbol),
        ("Period", record.period),
        ("Status", record.status),
        ("Trigger", record.trigger_source),
        ("Window", f"{record.window_start} → {record.window_end}"),
        ("Trades", str(record.trade_count)),
        ("Buy volume", record.buy_volume),
        ("Sell volume", record.sell_volume),
        ("Delta", record.delta),
        ("CVD", record.cvd),
        ("Quality", record.data_quality),
        ("Parquet part", record.manifest_path),
    )
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"{label:<{width}}  {value}" for label, value in rows)
