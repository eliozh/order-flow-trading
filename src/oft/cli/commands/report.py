"""``oft report list|show|export``: CALC metrics first, then the explicit LLM state."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from oft.api.envelope import new_request_id
from oft.application.report_export import EXPORT_FORMATS
from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings

report_app = typer.Typer(
    help="Report commands: list and show frozen CALC reports, export them as md/json.",
    no_args_is_help=True,
    add_completion=False,
)


def export_exit_code(code: str) -> int:
    """Export-only exit mapping: an API validation failure is a usage error (2)."""
    return 2 if code == "VALIDATION_FAILED" else exit_code_for(code)


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _signed(value: str) -> str:
    try:
        number = Decimal(value)
    except (TypeError, ValueError, InvalidOperation):
        return str(value)
    if number > 0:
        return f"▲ +{value}"
    if number < 0:
        return f"▼ {value}"
    return str(value)


def _section_lines(metrics: dict[str, Any]) -> list[str]:
    cvd = metrics.get("cvd") or {}
    points = cvd.get("points") or []
    summary = cvd.get("summary") or {}
    profile = metrics.get("volume_profile") or {}
    oi = metrics.get("open_interest") or {}
    funding = metrics.get("funding") or {}
    liquidations = metrics.get("liquidations") or {}
    footprint = metrics.get("footprint") or {}
    gaps = metrics.get("gaps") or []

    latest_delta = points[-1]["delta"] if points else "0"
    fp_bars = footprint.get("bars") or []
    fp_cells = sum(len(bar.get("cells") or []) for bar in fp_bars)
    liq_count = liquidations.get("count", 0)
    coverage = liquidations.get("coverage", "NONE")
    oi_measured = bool(oi.get("points"))
    funding_measured = bool(funding.get("history"))
    oi_value = str(oi.get("delta_over_window", "0")) if oi_measured else "not measured"
    funding_value = str(funding.get("current_rate", "0")) if funding_measured else "not measured"
    oi_note = f" · {oi.get('sampling')}" if oi_measured else ""
    funding_note = f" · next {funding.get('next_funding_time')}" if funding_measured else ""

    return [
        f"  CVD (window)      {_signed(str(summary.get('cvd_change', '0')))}"
        f"    delta {_signed(str(summary.get('delta', '0')))}"
        f" · buy {summary.get('buy_volume', '-')} / sell {summary.get('sell_volume', '-')}",
        f"  Delta (latest)    {_signed(str(latest_delta))}",
        f"  POC               {profile.get('poc', '-')}",
        f"  VAH / VAL         {profile.get('vah', '-')} / {profile.get('val', '-')}",
        f"  Volume Profile    {len(profile.get('bins') or [])} bins"
        f" · {profile.get('bin_size', '-')} per bin",
        f"  Footprint         {len(fp_bars)} bars · {fp_cells} cells",
        f"  OI Δ              {oi_value}{oi_note}",
        f"  Funding           {funding_value}{funding_note}",
        f"  Liquidations      {liq_count} events · {coverage}",
        f"  Gaps              {len(gaps)}",
    ]


def render_report_table(data: dict[str, Any]) -> str:
    """Render CALC metrics before the explicit LLM-not-configured notice."""
    snapshot = data.get("snapshot") or {}
    metrics = data.get("metrics") or {}
    window = snapshot.get("window") or metrics.get("window") or {}
    lines = [
        f"REPORT {data.get('report_id', '-')} · {data.get('symbol', '-')}"
        f" · {data.get('timeframe', '-')}",
        f"window {window.get('start', '-')} → {window.get('end', '-')}",
        f"generated {data.get('generated_at', '-')} · scan {data.get('scan_id', '-')}"
        f" · quality {data.get('data_quality', '-')}",
        "",
        "CALC",
        *_section_lines(metrics),
        "",
    ]
    llm = data.get("llm") or {}
    if llm.get("configured"):
        status = str(llm.get("validation_status") or "")
        if status in {"VALID", "REPAIRED"}:
            narrative = llm.get("narrative") or llm.get("rationale") or "(no narrative)"
            lines.append(f"LLM › {narrative}")
            lines.append(
                f"LLM decision {llm.get('decision_type', '-')}"
                f" · confidence {llm.get('confidence', '-')}"
                f" · provider {llm.get('provider', '-')}/{llm.get('model', '-')}"
            )
            opportunities = llm.get("opportunity_ids") or []
            lines.append(
                "LLM opportunities " + (", ".join(str(item) for item in opportunities) or "none")
            )
        else:
            reason = llm.get("reason") or "the model output was rejected"
            lines.append(f"LLM rejected — {reason}")
            lines.append("No narrative, opportunity, or order was created from this output.")
    else:
        reason = llm.get("reason") or (
            "LLM not configured; no narrative or decision was generated."
        )
        notice = (
            reason
            if reason.lower().startswith("llm not configured")
            else f"LLM not configured — {reason}"
        )
        lines.append(notice)
        lines.append("No LLM call, order, or credential was required or recorded.")
    return "\n".join(lines)


@report_app.command("list")
def report_list(
    scan: Annotated[str | None, typer.Option("--scan", help="Filter to one scan ID.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Rows to show.")] = 20,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the API envelope as JSON."),
    ] = False,
) -> None:
    """List CALC-only reports."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).list_reports(limit=limit, scan_id=scan)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc

    data = envelope.get("data")
    if not isinstance(data, dict):
        emit_error("INTERNAL_ERROR", "API returned no reports.", json_output=json_output)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    items = data.get("items") or []
    if not items:
        typer.echo("No reports yet. Run: oft scan run --metrics-only --symbol BTCUSDT")
        return
    typer.echo(f"{'REPORT':<36}{'SYMBOL':<12}{'PERIOD':<8}QUALITY")
    for item in items:
        typer.echo(
            f"{str(item.get('report_id', '-')):<36}"
            f"{str(item.get('symbol', '-')):<12}"
            f"{str(item.get('timeframe', '-')):<8}"
            f"{item.get('data_quality', '-')}"
        )


@report_app.command("show")
def report_show(
    report_id: str,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the API envelope as JSON."),
    ] = False,
) -> None:
    """Show one CALC report; CALC metrics always precede the LLM notice."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).get_report(report_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc

    data = envelope.get("data")
    if not isinstance(data, dict):
        emit_error("NOT_FOUND", f"Report {report_id} does not exist.", json_output=json_output)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
    else:
        typer.echo(render_report_table(data))


def _refuse(code: str, message: str, exit_code: int) -> NoReturn:
    """Emit a typed error on stderr and exit without touching the filesystem."""
    emit_error(code, message, json_output=False)
    raise typer.Exit(code=exit_code)


def _output_path_problem(path: Path) -> str | None:
    """Reject traversal and directory-shaped output paths before any write."""
    if not path.parts:
        return "the path must name a new file"
    for part in path.parts:
        if part == "..":
            return "path traversal ('..') is not allowed"
    if path.name in {"", ".", ".."}:
        return "the path must name a new file, not a directory"
    return None


def _write_export(path: Path, body: bytes, headers: dict[str, str]) -> None:
    """Create ``path`` exclusively, never overwriting or leaving a partial file."""
    try:
        with path.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        _refuse(
            "VALIDATION_FAILED",
            f"VALIDATION_FAILED: refusing to overwrite existing path {path}; "
            f"choose a new path. Reference: {new_request_id()}.",
            2,
        )
    except OSError:
        # The ``with`` block closed the handle; remove anything it created.
        with suppress(OSError):
            path.unlink()
        _refuse(
            "STORAGE_UNAVAILABLE",
            f"STORAGE_UNAVAILABLE: could not write export to {path}. "
            f"Reference: {new_request_id()}.",
            1,
        )

    parts = [f"Wrote {path}", f"{len(body)} bytes"]
    calc_blocks = headers.get("x-oft-calc-blocks")
    llm_blocks = headers.get("x-oft-llm-blocks")
    if calc_blocks is not None and llm_blocks is not None:
        parts.append(f"CALC blocks: {calc_blocks}")
        parts.append(f"LLM blocks: {llm_blocks}")
    typer.echo(" · ".join(parts))


@report_app.command("export")
def report_export(
    report_id: str,
    format: Annotated[
        str,
        typer.Option("--format", help="Export format: md or json."),
    ] = "md",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write to this new file instead of stdout."),
    ] = None,
    stdout: Annotated[
        bool,
        typer.Option("--stdout", help="Write only the export body to stdout."),
    ] = False,
) -> None:
    """Export one frozen report as redacted Markdown or JSON."""
    resolved_format = format.strip().lower()
    if resolved_format not in EXPORT_FORMATS:
        _refuse(
            "VALIDATION_FAILED",
            f"VALIDATION_FAILED: format must be one of md, json; got {format!r}.",
            2,
        )
    if output is not None and stdout:
        _refuse(
            "VALIDATION_FAILED",
            "VALIDATION_FAILED: --output and --stdout are mutually exclusive; choose one.",
            2,
        )

    settings = _settings_or_exit(False)
    try:
        body, headers = ApiClient.from_settings(settings).export_report(
            report_id, format=resolved_format
        )
    except ApiClientError as exc:
        emit_error(exc.code, f"{exc.code}: {exc.message}", json_output=False)
        raise typer.Exit(code=export_exit_code(exc.code)) from exc

    if output is not None:
        problem = _output_path_problem(output)
        if problem is not None:
            _refuse(
                "VALIDATION_FAILED",
                f"VALIDATION_FAILED: refusing to write export to {output}: {problem}. "
                f"Reference: {new_request_id()}.",
                2,
            )
        if output.is_symlink() or output.exists():
            _refuse(
                "VALIDATION_FAILED",
                f"VALIDATION_FAILED: refusing to overwrite existing path {output}; "
                f"choose a new path. Reference: {new_request_id()}.",
                2,
            )
        _write_export(output, body, headers)
        return

    # Default and explicit ``--stdout``: only the export touches stdout.
    typer.echo(body.decode("utf-8"), nl=False)
