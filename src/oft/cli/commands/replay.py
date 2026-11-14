"""``oft replay``: deterministic paper-loop replay over committed fixtures.

The command never opens the live daemon store. It builds an isolated database
under ``$OFT_DATA/replay/paper-loop/<run-id>/`` and runs the same domain code as
live against recorded LLM outputs. A live environment or a live order path is
refused with a non-zero exit; a missing recording is a clear error unless
``--live-llm`` is explicitly passed.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ERROR_CATALOG
from oft.application.entry_service import EntryRejected
from oft.cli.commands.doctor import emit_error
from oft.config import ConfigError, load_settings
from oft.domain.time import utc_timestamp
from oft.replay.harness import (
    ReplayError,
    ReplayLivePathError,
    ReplayNonDeterministicError,
)
from oft.replay.llm_source import LLMRecordingError, LLMRecordingInvalid
from oft.replay.paper_loop import (
    DEFAULT_REPLAY_NOW,
    PaperLoopConfig,
    PaperLoopReplay,
    PaperLoopResult,
    ReplayRecordingError,
    validate_run_id,
)
from oft.replay.position_management import (
    PositionManagementConfig,
    PositionManagementReplay,
    PositionManagementResult,
)

replay_app = typer.Typer(
    help="Deterministic paper-loop replay (offline; replay namespace only).",
    no_args_is_help=True,
)

_EXIT_FAILED = 2
SCENARIOS = ("entry", "position-management")

# Recording failures have no dedicated catalog code yet. Map them to the
# closest existing code so JSON consumers never see a misleading 500
# ``INTERNAL_ERROR``; every code is asserted against the catalog before emit.
_RECORDING_ERROR_CODES: dict[str, str] = {
    "LLM_RECORDING_MISSING": "DATA_UNAVAILABLE",
    "LLM_RECORDING_INVALID": "VALIDATION_FAILED",
}


def _catalog_code(code: str) -> str:
    """Map the two recording codes; refuse to relabel anything else.

    Only the recording failures have an explicit mapping. A non-recording code
    must already be a real ``ERROR_CATALOG`` entry; silently rewriting an
    unknown code to ``VALIDATION_FAILED`` would hide a programming error (and
    make the membership assert in ``_fail`` meaningless for that path).
    """
    resolved = _RECORDING_ERROR_CODES.get(code, code)
    if resolved not in ERROR_CATALOG:
        raise ReplayError(
            f"replay error code {code!r} has no ERROR_CATALOG mapping; "
            "add one instead of relabeling it"
        )
    return resolved


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"expected YYYY-MM-DD: {value}") from exc


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter(f"expected ISO-8601 timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise typer.BadParameter("--now must be timezone-aware (for example 2026-11-06T12:00:00Z)")
    return parsed.astimezone(UTC)


def _fail(message: str, *, json_output: bool, code: str = "VALIDATION_FAILED") -> NoReturn:
    assert code in ERROR_CATALOG, f"unknown error code for replay: {code}"
    if json_output:
        emit_error(code, message, json_output=True)
    else:
        typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=_EXIT_FAILED)


def _result_payload(result: PaperLoopResult | PositionManagementResult) -> dict[str, Any]:
    return asdict(result)


def _render(result: PaperLoopResult) -> str:
    return "\n".join(
        (
            f"replay {result.run_id} · deterministic={str(result.deterministic).lower()} "
            f"· data_quality={result.data_quality}",
            f"decision  {result.decision_id} ({result.decision_validation})",
            f"trigger   {result.trigger_id}",
            f"order     {result.order.order_id} ({result.order.status})",
            f"fill      {result.fill.fill_id} @ {result.fill.price} fee {result.fill.fee}",
            f"position  {result.position.position_id} size {result.position.size} "
            f"notional {result.position.notional_usd}",
            f"summary   {result.summary_path}",
        )
    )


def _render_position_management(result: PositionManagementResult) -> str:
    return "\n".join(
        (
            f"replay {result.run_id} · scenario={result.scenario} "
            f"· deterministic={str(result.deterministic).lower()}",
            f"position  {result.position_id}",
            f"trade     {result.trade_id} · data_quality={result.data_quality}",
            f"journal   stats={result.statistics_status}",
            f"review    {result.review_decision_id}",
            f"summary   {result.summary_path}",
        )
    )


@replay_app.command("run")
def replay_run(
    archive_fixture_dir: Annotated[
        Path | None,
        typer.Option(
            "--archive-fixture-dir",
            help="Committed archive fixtures used for the deterministic backfill.",
        ),
    ] = None,
    recordings_dir: Annotated[
        Path | None,
        typer.Option(
            "--recordings-dir",
            help="Directory of recorded LLM responses keyed by (role, input_hash).",
        ),
    ] = None,
    live_llm: Annotated[
        bool,
        typer.Option(
            "--live-llm",
            help="Explicitly call the configured provider and record its redacted output.",
        ),
    ] = False,
    capture_recordings: Annotated[
        bool,
        typer.Option(
            "--capture",
            help="Record outputs from the offline fixture provider (no network).",
        ),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider", help="Provider name to capture from (default: OFT_LLM_PROVIDER)."
        ),
    ] = None,
    symbol: Annotated[str, typer.Option("--symbol")] = "BTCUSDT",
    period: Annotated[str, typer.Option("--period")] = "4h",
    scenario: Annotated[
        str,
        typer.Option(
            "--scenario",
            help="Replay scenario: entry (default) or position-management (journal+review).",
        ),
    ] = "entry",
    start: Annotated[
        str, typer.Option("--start", help="Backfill start date (YYYY-MM-DD).")
    ] = "2026-10-01",
    end: Annotated[
        str, typer.Option("--end", help="Backfill end date (YYYY-MM-DD).")
    ] = "2026-10-07",
    environment: Annotated[
        str,
        typer.Option(
            "--environment",
            help="Replay environment; only paper/test are accepted (anything else exits non-zero).",
        ),
    ] = "paper",
    now: Annotated[
        str,
        typer.Option("--now", help="Fixed clock instant (ISO-8601 UTC)."),
    ] = "2026-11-06T12:00:00Z",
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Output run id (defaults per scenario).")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit the replay as JSON.")] = False,
) -> None:
    """Replay the entry loop, or the management/journal/review scenario, twice."""
    resolved_now = DEFAULT_REPLAY_NOW
    try:
        settings = load_settings()
        resolved_now = _parse_datetime(now)
        resolved_scenario = scenario.strip().lower()
        if resolved_scenario not in SCENARIOS:
            _fail(
                f"unknown replay scenario {scenario!r}; expected one of {', '.join(SCENARIOS)}",
                json_output=json_output,
            )
        resolved_run_id = run_id or (
            "paper-loop" if resolved_scenario == "entry" else "position-management"
        )
        validate_run_id(resolved_run_id)
        resolved_archive = archive_fixture_dir or settings.archive_fixture_dir
        if resolved_archive is None:
            _fail(
                "no archive fixture directory configured; pass --archive-fixture-dir",
                json_output=json_output,
            )
        if resolved_scenario == "entry":
            config = PaperLoopConfig(
                symbol=symbol,
                archive_fixture_dir=Path(resolved_archive),
                backfill_start=_parse_date(start),
                backfill_end=_parse_date(end),
                period=period,
                recordings_dir=recordings_dir,
                live_llm=live_llm,
                capture_recordings=capture_recordings,
                provider=provider,
                environment=environment,
                now=resolved_now,
                run_id=resolved_run_id,
            )
            result: PaperLoopResult | PositionManagementResult = PaperLoopReplay(
                data_dir=settings.data_dir, config=config
            ).run()
        else:
            management_config = PositionManagementConfig(
                symbol=symbol,
                archive_fixture_dir=Path(resolved_archive),
                backfill_start=_parse_date(start),
                backfill_end=_parse_date(end),
                period=period,
                recordings_dir=recordings_dir,
                live_llm=live_llm,
                capture_recordings=capture_recordings,
                provider=provider,
                environment=environment,
                now=resolved_now,
                run_id=resolved_run_id,
            )
            result = PositionManagementReplay(
                data_dir=settings.data_dir, config=management_config
            ).run()
    except ReplayLivePathError as exc:
        _fail(str(exc), json_output=json_output, code="LIVE_DISABLED")
    except ReplayRecordingError as exc:
        _fail(str(exc), json_output=json_output, code=_catalog_code(exc.code))
    except LLMRecordingError as exc:
        _fail(exc.message, json_output=json_output, code=_catalog_code(exc.code))
    except LLMRecordingInvalid as exc:
        _fail(exc.message, json_output=json_output, code=_catalog_code(exc.code))
    except EntryRejected as exc:
        _fail(exc.message, json_output=json_output, code=_catalog_code(exc.code))
    except ReplayNonDeterministicError as exc:
        _fail(str(exc), json_output=json_output, code="VALIDATION_FAILED")
    except ReplayError as exc:
        _fail(str(exc), json_output=json_output)
    except ConfigError as exc:
        _fail(exc.message, json_output=json_output, code="CONFIG_INVALID")
    except ValueError as exc:
        _fail(str(exc), json_output=json_output)

    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    _result_payload(result),
                    request_id=new_request_id(),
                    generated_at=utc_timestamp(resolved_now),
                    provenance="CALC",
                    data_quality=result.data_quality,
                ),
                indent=2,
            )
        )
    elif isinstance(result, PositionManagementResult):
        typer.echo(_render_position_management(result))
    else:
        typer.echo(_render(result))
