"""``oft report list|show|export``: CALC metrics first, then the explicit LLM state."""

from __future__ import annotations

import errno
import json
import os
import re
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from oft.api.envelope import new_request_id
from oft.application.report_export import EXPORT_FORMATS, is_configuration_reason
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


#: Errnos from filesystems that cannot create a hard link. In that case the
#: publisher falls back to an exclusive ``os.open(..., O_CREAT | O_EXCL, 0o600)``
#: create of the final path.
_LINK_UNSUPPORTED_ERRNOS = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV})

#: C0/C1 controls never belong in a one-line diagnostic; cap the cause too.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CAUSE_LIMIT = 200


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
    """Render CALC metrics before the explicit LLM state/notice."""
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
        reason = str(
            llm.get("reason") or "LLM not configured; no narrative or decision was generated."
        )
        notice = reason if is_configuration_reason(reason) else f"LLM skipped — {reason}"
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


def _bounded_cause(exc: OSError) -> str:
    """A short single-line cause with the errno name and control bytes stripped."""
    label = type(exc).__name__
    if exc.errno is not None:
        code = errno.errorcode.get(exc.errno)
        if code:
            label = f"{label} ({code})"
    detail = _CONTROL_CHARACTERS.sub("", exc.strerror or str(exc))
    return f"{label}: {detail}".strip()[:_CAUSE_LIMIT]


def _refuse_storage(path: Path, exc: OSError) -> NoReturn:
    """Refuse a failed write with a bounded cause and a fresh reference."""
    _refuse(
        "STORAGE_UNAVAILABLE",
        f"STORAGE_UNAVAILABLE: could not write export to {path}: {_bounded_cause(exc)}. "
        f"Reference: {new_request_id()}.",
        1,
    )


def _remove_temp(temp_path: Path) -> None:
    """Remove a staged temporary file; name it on stderr if that fails."""
    try:
        temp_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        typer.echo(
            f"Warning: could not remove the temporary export file {temp_path} "
            f"({_bounded_cause(exc)}); remove it manually.",
            err=True,
        )


def _restrict_mode(file_descriptor: int) -> None:
    """Force the export to mode 0600 (owner read/write) where POSIX modes exist.

    ``mkstemp`` requests 0600 and the fallback requests 0600 at create time, but
    the operator's umask can reduce either. ``fchmod`` makes both publish paths
    exactly 0600. Platforms without ``fchmod`` keep their native permission
    model.
    """
    if hasattr(os, "fchmod"):
        os.fchmod(file_descriptor, 0o600)


def _publish_exclusive(path: Path, body: bytes) -> None:
    """Fallback publish without hard links: exclusive create, then fsync.

    The file is created at mode 0600 by the creating syscall (``O_EXCL``), so a
    local reader cannot observe the body through a permissive umask before the
    mode is normalized. The exclusive create happens before the cleanup guard,
    so a pre-existing destination still raises ``FileExistsError`` and is never
    deleted. A write or fsync failure removes the partial file this call
    created; if that removal fails, the leftover final path is named on stderr.
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        handle = os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        raise
    try:
        with handle:
            _restrict_mode(handle.fileno())
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as cleanup_exc:
            typer.echo(
                f"Warning: could not remove the incomplete export file {path} "
                f"({_bounded_cause(cleanup_exc)}); remove it manually.",
                err=True,
            )
        raise


def _fsync_parent(parent: Path) -> None:
    """Make the new directory entry durable (POSIX)."""
    descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_export(path: Path, body: bytes, headers: dict[str, str]) -> None:
    """Publish ``path`` exclusively; never overwrite, and clean up on handled failures.

    The body goes to a same-directory temporary file with an exclusive create,
    is flushed and fsynced, then published with ``os.link``. The link fails with
    ``FileExistsError`` when the destination exists, so an existing file is
    never replaced and the hard-link publish is atomic (no reader sees a partial
    final file). After publishing, the temporary name is removed and the parent
    directory is fsynced. Filesystems without hard links fall back to an
    exclusive ``os.open(..., O_CREAT | O_EXCL, 0o600)`` create of the final path
    (still no overwrite), and the
    file and parent are fsynced there too; a handled failure removes that
    partial file, but the fallback is not crash-atomic (a SIGKILL/power loss
    between create and cleanup can leave a partial final file). The published
    file is mode 0600 on both paths.

    A refusal or failure removes the temporary file; if that cleanup fails, the
    leftover temporary path is named explicitly on stderr. Failures report a
    bounded, control-stripped cause: ``FileExistsError`` is
    ``VALIDATION_FAILED`` (exit 2) with the original file untouched, and any
    other ``OSError`` is ``STORAGE_UNAVAILABLE`` (exit 1). A parent-directory
    fsync failure after a successful publish is reported as a warning and the
    complete export is kept.
    """
    parent = path.parent
    try:
        file_descriptor, temp_name = tempfile.mkstemp(
            dir=parent, prefix=f".{path.name}.", suffix=".tmp"
        )
    except OSError as exc:
        _refuse_storage(path, exc)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            _restrict_mode(handle.fileno())
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno in _LINK_UNSUPPORTED_ERRNOS:
                _publish_exclusive(path, body)
            else:
                raise
    except FileExistsError:
        _refuse(
            "VALIDATION_FAILED",
            f"VALIDATION_FAILED: refusing to overwrite existing path {path}; "
            f"choose a new path. Reference: {new_request_id()}.",
            2,
        )
    except OSError as exc:
        _refuse_storage(path, exc)
    finally:
        _remove_temp(temp_path)

    try:
        _fsync_parent(parent)
    except OSError as exc:
        typer.echo(
            f"Warning: export {path} was written, but its parent directory could not be "
            f"fsynced ({_bounded_cause(exc)}).",
            err=True,
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
    export_format: Annotated[
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
    resolved_format = export_format.strip().lower()
    if resolved_format not in EXPORT_FORMATS:
        _refuse(
            "VALIDATION_FAILED",
            f"VALIDATION_FAILED: format must be one of md, json; got {export_format!r}.",
            2,
        )
    if output is not None and stdout:
        _refuse(
            "VALIDATION_FAILED",
            "VALIDATION_FAILED: --output and --stdout are mutually exclusive; choose one.",
            2,
        )
    # Validate a local output path before any daemon call or settings load, so a
    # usage error is not masked by a transport error. ``_write_export`` keeps
    # the exclusive publish as the TOCTOU authority.
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

    settings = _settings_or_exit(False)
    try:
        body, headers = ApiClient.from_settings(settings).export_report(
            report_id, export_format=resolved_format
        )
    except ApiClientError as exc:
        message = f"{exc.code}: {exc.message}"
        if exc.reference_id is not None:
            message += f" Reference: {exc.reference_id}."
        emit_error(exc.code, message, json_output=False)
        raise typer.Exit(code=export_exit_code(exc.code)) from exc

    if output is not None:
        _write_export(output, body, headers)
        return

    # Default and explicit ``--stdout``: only the export touches stdout.
    typer.echo(body.decode("utf-8"), nl=False)
