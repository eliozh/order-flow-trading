"""``oft system info|logs``: offline diagnostics and structured log access.

``info`` works with the daemon down: it reports the version, environment,
paths, or an honest cause when a value is unavailable. ``logs`` reads the same
local structured log as ``oft daemon logs`` and redacts credentials, cookies,
tokens, and untrusted prompt bodies before anything reaches a terminal.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from oft import __version__
from oft.api.envelope import new_request_id, success_envelope
from oft.application.jobs import latest_jobs_payload
from oft.cli.commands.daemon import (
    log_path,
    pid_matches,
    pid_meta_path,
    pid_path,
    read_pid,
    schedule_state,
    settings_or_exit,
)
from oft.cli.commands.doctor import emit_error
from oft.config import SECRET_FIELDS, ConfigError, Settings
from oft.domain.time import utc_timestamp

system_app = typer.Typer(
    help="System diagnostics: version/paths/daemon state and structured logs.",
    no_args_is_help=True,
    add_completion=False,
)

REDACTED = "[REDACTED]"
PROMPT_REDACTED = "[REDACTED UNTRUSTED PROMPT]"

# Hints are matched against a key normalized to lowercase alphanumerics, so
# ``api_key``, ``apikey``, ``X-Api-Key``, and ``x-api-key`` all match ``apikey``.
_SENSITIVE_KEY_HINTS = (
    "token",
    "secret",
    "password",
    "apikey",
    "authorization",
    "cookie",
    "setcookie",
    "session",
    "signature",
)
# Metadata keys that legitimately contain a sensitive-looking hint are kept.
_PROMPT_METADATA_KEYS = frozenset({"promptid", "promptversion"})
# Untrusted LLM prompt/response bodies are matched after normalization to
# lowercase alphanumerics, so ``systemPrompt``, ``system_prompt``, and
# ``SYSTEM-PROMPT`` all match ``systemprompt``.
_PROMPT_BODY_KEYS = frozenset(
    {
        "prompt",
        "systemprompt",
        "userprompt",
        "messages",
        "promptbody",
        "rawprompt",
        "prompttext",
        "promptcontent",
        "requestbody",
        "responsebody",
        "response",
        "completion",
        "completiontext",
        "output",
        "rawresponse",
        "rawtext",
        "llmrequest",
        "llmresponse",
        "input",
        "body",
        "content",
        # Residual body aliases that do not share a ``prompt*``/``completion*``
        # or ``reasoning*`` prefix.
        "reasoning",
        "reasoningcontent",
        "thinking",
        "answer",
        # Response/completion/reasoning/thinking/answer + text/content forms
        # that do not share a ``prompt*``/``completion*``/``reasoning*`` prefix.
        "responsetext",
        "responsecontent",
        "rawcompletion",
        "thinkingtext",
        "thinkingcontent",
        "answertext",
        "answercontent",
        "contenttext",
    }
)
# Untrusted bodies also arrive under arbitrary ``prompt*``/``completion*``/
# ``reasoning*`` keys (``prompt_text``, ``completionContent``, ``reasoningBody``,
# ...). Metadata keys above are checked first, so ``prompt_id`` /
# ``prompt_version`` survive.
_PROMPT_BODY_PREFIXES = ("prompt", "completion", "reasoning")

_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
# A header value is everything to end-of-line, so multi-pair cookies do not
# leak their later pairs (``Cookie: a=1; b=SECRET``).
_COOKIE_PATTERN = re.compile(r"(?i)\b((?:set-)?cookie)\s*:\s*[^\r\n]+")
# An un-coloned assignment (``cookie=a=1; b=SECRET``) runs to end-of-line too.
_COOKIE_ASSIGNMENT_PATTERN = re.compile(r"(?i)\bcookie\s*=\s*[^\r\n]+")
# Any authorization scheme (Basic, Digest, custom), not just Bearer.
_AUTHORIZATION_PATTERN = re.compile(r"(?i)\bauthorization\s*:\s*[^\r\n]+")
# Match a prefixed key too (``my_api_key=...``), not just a bare ``api_key``.
# Quoted (and backticked) values may contain spaces and are replaced whole,
# matching ``_SECRET_ASSIGNMENT_PATTERN`` below.
_API_KEY_PATTERN = re.compile(
    r"(?i)\b[\w-]*api[_-]?key\s*[=:]\s*(\"[^\"]*\"|'[^']*'|`[^`]*`|[^\s,;]+)"
)
# The left guard is ``(?<![A-Za-z0-9])`` rather than ``\b`` so a key embedded
# in a longer identifier (``db_password=...``, ``app_signature=...``) still
# matches: ``_`` is a word character, so ``\b`` would not fire there. Quoted
# (and backticked) values may contain spaces and are replaced whole.
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(password|passwd|signature|access_token|client_secret|secret|token)"
    r"\s*[=:]\s*(\"[^\"]*\"|'[^']*'|`[^`]*`|[^\s,;]+)"
)
_TELEGRAM_TOKEN_PATTERN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")
# Joining/variation marks are category ``Mn`` but render as nothing. Ordinary
# combining marks are preserved so non-Latin text is not mangled.
_INVISIBLE_MARK_RANGES = (
    (0x034F, 0x034F),  # COMBINING GRAPHEME JOINER
    (0x180B, 0x180D),  # MONGOLIAN FREE VARIATION SELECTORS
    (0xFE00, 0xFE0F),  # VARIATION SELECTORS
    (0xE0100, 0xE01EF),  # VARIATION SELECTORS SUPPLEMENT
)


def _is_invisible_mark(ch: str) -> bool:
    code = ord(ch)
    return any(start <= code <= end for start, end in _INVISIBLE_MARK_RANGES)


def _strip_invisible(value: str) -> str:
    """Drop code points a terminal does not render.

    ``Cc``/``Cf`` controls (except tab and newline; ``\\r`` is stripped) and the
    explicit invisible marks must be removed BEFORE the redaction passes:
    otherwise a keyword split by one (``pass\\u00adword``) evades every pattern
    and the later strip re-joins it into a visible secret.
    """
    kept: list[str] = []
    for ch in value:
        if ch in "\t\n":
            kept.append(ch)
            continue
        category = unicodedata.category(ch)
        if category in ("Cc", "Cf"):
            continue
        if category == "Mn" and _is_invisible_mark(ch):
            continue
        kept.append(ch)
    return "".join(kept)


_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_TOKEN_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # ghp_ (personal), gho_ (OAuth), ghu_ (user), ghs_ (server), ghr_ (refresh).
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
)

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}
_RELATIVE_SINCE = re.compile(r"^(\d+)([smhd])$")
_RELATIVE_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_FOLLOW_POLL_SECONDS = 0.2


# ---------------------------------------------------------------------------
# Clock / validation seams
# ---------------------------------------------------------------------------


def _now(settings: Settings) -> datetime:
    """The clock used to resolve a relative ``--since`` window (test seam).

    ``settings`` is kept for the existing monkeypatch seam, but a relative
    window must not build the whole container (and fail on unrelated config).
    """
    from oft.clock import SystemClock

    return SystemClock().now()


def _sleep(seconds: float) -> None:
    """The follow poll delay (test seam)."""
    time.sleep(seconds)


def _usage_error(message: str, *, json_output: bool) -> NoReturn:
    emit_error("VALIDATION_FAILED", message, json_output=json_output)
    raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _configured_secret_values(settings: Settings) -> tuple[str, ...]:
    """The loaded ``SecretStr`` values, for exact-value free-text redaction."""
    values: list[str] = []
    for field in SECRET_FIELDS:
        secret = getattr(settings, field, None)
        if secret is None:
            continue
        value = secret.get_secret_value()
        if value:
            values.append(value)
    return tuple(values)


def redact_record(value: object, *, secret_values: Sequence[str] = ()) -> object:
    """Return a recursively redacted copy of a JSON-like log record."""
    if isinstance(value, dict):
        redacted: dict[object, object] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            normalized = re.sub(r"[^a-z0-9]", "", lowered)
            safe_key = _redact_string(key_text, secret_values=secret_values)
            if normalized in _PROMPT_METADATA_KEYS:
                redacted[safe_key] = redact_record(item, secret_values=secret_values)
            elif normalized in _PROMPT_BODY_KEYS or normalized.startswith(_PROMPT_BODY_PREFIXES):
                redacted[safe_key] = PROMPT_REDACTED
            elif any(hint in normalized for hint in _SENSITIVE_KEY_HINTS):
                redacted[safe_key] = (
                    REDACTED
                    if item not in (None, "")
                    else redact_record(item, secret_values=secret_values)
                )
            else:
                redacted[safe_key] = redact_record(item, secret_values=secret_values)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_record(item, secret_values=secret_values) for item in value]
    if isinstance(value, str):
        return _redact_string(value, secret_values=secret_values)
    return value


def _redact_string(value: str, *, secret_values: Sequence[str] = ()) -> str:
    # Strip invisible controls BEFORE redaction. Redacting first would miss a
    # keyword split by a control (``pass\x01word``) or a configured value with
    # one inserted; the final strip would then re-join the secret into view.
    value = _strip_invisible(value)
    redacted = _PRIVATE_KEY_PATTERN.sub(REDACTED, value)
    redacted = _AUTHORIZATION_PATTERN.sub(f"Authorization: {REDACTED}", redacted)
    redacted = _BEARER_PATTERN.sub(f"Bearer {REDACTED}", redacted)
    redacted = _COOKIE_PATTERN.sub(lambda match: f"{match.group(1)}: {REDACTED}", redacted)
    redacted = _COOKIE_ASSIGNMENT_PATTERN.sub(f"cookie={REDACTED}", redacted)
    redacted = _API_KEY_PATTERN.sub(f"api_key={REDACTED}", redacted)
    redacted = _SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}={REDACTED}", redacted
    )
    redacted = _TELEGRAM_TOKEN_PATTERN.sub(REDACTED, redacted)
    for pattern in _TOKEN_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    # Configured exact values plus the shared sk/xox/bearer/api_key/secret/token
    # assignment rules, so a key echoed in free text never reaches output.
    from oft.domain.prompts import redact_secrets

    redacted = redact_secrets(redacted, secret_values=secret_values)
    return _strip_invisible(redacted)


# ---------------------------------------------------------------------------
# system info
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _read_started_at(settings: Settings) -> datetime | None:
    try:
        meta = json.loads(pid_meta_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # A JSON array/scalar is a corrupt meta file: ``.get`` would raise.
    if not isinstance(meta, dict):
        return None
    raw = meta.get("started_at")
    return _parse_iso(raw) if isinstance(raw, str) else None


def _daemon_payload(settings: Settings, container: Any) -> dict[str, Any]:
    pid = read_pid(settings)
    running = pid is not None and pid_matches(settings, pid)
    uptime: int | None = None
    if running:
        started_at = _read_started_at(settings)
        if started_at is not None:
            uptime = max(0, int((container.clock.now() - started_at).total_seconds()))
    return {
        "state": "RUNNING" if running else "OFFLINE",
        "pid": pid if running else None,
        "uptime_seconds": uptime,
    }


def _config_source_payload() -> dict[str, Any]:
    path = Path(".env")
    present = path.is_file()
    return {
        "env_prefix": "OFT_",
        "env_file": str(path.resolve()) if present else None,
        "env_file_present": present,
    }


def _info_payload(settings: Settings, container: Any, *, json_output: bool) -> dict[str, Any]:
    now = container.clock.now()
    state = schedule_state(settings, container=container, json_output=json_output)
    schedule = state.next_run_payload(now)
    return {
        "version": __version__,
        "environment": settings.env.value,
        "paths": {
            "data_dir": str(settings.data_dir),
            "log_file": str(log_path(settings)),
            "pid_file": str(pid_path(settings)),
        },
        "config_source": _config_source_payload(),
        "daemon": _daemon_payload(settings, container),
        "scheduler": {
            "period": schedule["period"],
            "pending_period": schedule["pending_period"],
            "next_run_at": schedule["next_run_at"],
            "timezone": schedule["timezone"],
        },
        "jobs": redact_record(
            latest_jobs_payload(container.store),
            secret_values=_configured_secret_values(settings),
        ),
        "generated_at": utc_timestamp(now),
    }


def _render_info(payload: dict[str, Any]) -> str:
    source = payload["config_source"]
    if source["env_file_present"]:
        config_source = f"OFT_* environment plus .env at {source['env_file']}"
    else:
        config_source = "OFT_* environment only (.env not found in the working directory)"
    daemon = payload["daemon"]
    daemon_line = f"RUNNING (pid {daemon['pid']})" if daemon["state"] == "RUNNING" else "OFFLINE"
    if daemon["uptime_seconds"] is None:
        if daemon["state"] == "RUNNING":
            uptime = "not available (pid file has no recorded start time)"
        else:
            uptime = "not available (daemon is offline)"
    else:
        uptime = f"{daemon['uptime_seconds']}s"
    schedule = payload["scheduler"]
    pending = schedule["pending_period"] or "none"
    jobs = " · ".join(_job_human(name, summary) for name, summary in payload["jobs"].items())
    return "\n".join(
        [
            f"{'Version':<16} {payload['version']}",
            f"{'Environment':<16} {payload['environment']}",
            f"{'Data dir':<16} {payload['paths']['data_dir']}",
            f"{'Log file':<16} {payload['paths']['log_file']}",
            f"{'PID file':<16} {payload['paths']['pid_file']}",
            f"{'Config source':<16} {config_source}",
            f"{'Daemon':<16} {daemon_line}",
            f"{'Uptime':<16} {uptime}",
            f"{'Scheduler':<16} period {schedule['period']} · next run "
            f"{schedule['next_run_at']} {schedule['timezone']}",
            f"{'Pending period':<16} {pending}",
            f"{'Jobs':<16} {jobs}",
            f"{'Generated at':<16} {payload['generated_at']}",
        ]
    )


def _job_human(name: str, summary: dict[str, Any] | None) -> str:
    if summary is None:
        return f"{name}: never run"
    finished = summary.get("finished_at") or "unknown"
    return f"{name}: {summary.get('status')} · {finished}"


@system_app.command("info")
def system_info(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the system state as JSON.")
    ] = False,
) -> None:
    """Show version, environment, paths, daemon state, scheduler, and jobs."""
    settings = settings_or_exit(json_output)
    from oft.bootstrap import build_container

    try:
        container = build_container(settings)
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc
    payload = _info_payload(settings, container, json_output=json_output)
    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    payload,
                    request_id=new_request_id(),
                    generated_at=payload["generated_at"],
                    provenance="CALC",
                    data_quality="COMPLETE",
                ),
                indent=2,
            )
        )
        return
    typer.echo(_render_info(payload))


# ---------------------------------------------------------------------------
# system logs
# ---------------------------------------------------------------------------


def _parse_level(value: str) -> int:
    normalized = value.strip().lower()
    if normalized not in _LEVELS:
        raise ValueError(
            f"--level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL; got {value!r}"
        )
    return _LEVELS[normalized]


def _parse_since(value: str, settings: Settings) -> datetime:
    text = value.strip()
    match = _RELATIVE_SINCE.match(text.lower())
    if match is not None:
        seconds = int(match.group(1)) * _RELATIVE_SECONDS[match.group(2)]
        return _now(settings) - timedelta(seconds=seconds)
    parsed = _parse_iso(text)
    if parsed is None:
        raise ValueError(
            "--since must be ISO-8601 (with Z or an offset) or a relative "
            f"window such as 30s/10m/2h/1d; got {value!r}"
        )
    return parsed


def _record_timestamp(record: Any) -> datetime | None:
    if not isinstance(record, dict):
        return None
    raw = record.get("timestamp") or record.get("ts")
    return _parse_iso(raw) if isinstance(raw, str) else None


def _record_level(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    raw = record.get("level")
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower()
    return normalized if normalized in _LEVELS else None


@dataclass(frozen=True)
class _Selection:
    """Selected tail plus the totals needed to report honestly."""

    records: tuple[object, ...]
    matched: int
    dropped: int


def _select_records(
    raw_lines: Iterable[str],
    *,
    since: datetime | None,
    min_level: int | None,
    secret_values: Sequence[str] = (),
    max_records: int | None = None,
) -> _Selection:
    """Parse, filter, and redact raw log lines from a streaming iterable.

    With no filters active a non-JSON line is retained and redacted as text.
    Under a filter it cannot be evaluated, so it is dropped and counted. A
    structured record without a timestamp is dropped only when ``--since`` is
    set; a record without a usable level is dropped only when ``--level`` is set.
    ``matched`` counts every line that passes the filter; only the most recent
    ``max_records`` (when bounded) are retained.
    """
    filters_active = since is not None or min_level is not None
    selected: deque[object] = deque(maxlen=max_records)
    matched = 0
    dropped = 0
    for raw in raw_lines:
        # Strip only the line terminator: leading/interior whitespace is part of
        # the plain line, but a retained ``\n`` would add blank lines and embed
        # a newline in the JSON string.
        text = raw.rstrip("\r\n")
        if not text.strip():
            continue
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        structured = isinstance(parsed, (dict, list))
        record: object = parsed if structured else text
        if not filters_active:
            selected.append(redact_record(record, secret_values=secret_values))
            matched += 1
            continue
        if not structured:
            dropped += 1
            continue
        if since is not None:
            stamp = _record_timestamp(record)
            if stamp is None:
                dropped += 1
                continue
            if stamp < since:
                continue
        if min_level is not None:
            level = _record_level(record)
            if level is None:
                dropped += 1
                continue
            if _LEVELS[level] < min_level:
                continue
        selected.append(redact_record(record, secret_values=secret_values))
        matched += 1
    return _Selection(records=tuple(selected), matched=matched, dropped=dropped)


def _warn_dropped(dropped: int) -> None:
    if dropped:
        typer.echo(
            f"warning: dropped {dropped} log line(s) that could not be parsed or "
            "lacked a timestamp/level for the active filter",
            err=True,
        )


def _emit_record(record: object, *, json_lines: bool) -> None:
    if json_lines or isinstance(record, (dict, list)):
        typer.echo(json.dumps(record, sort_keys=True))
    else:
        typer.echo(str(record))


def _empty_cause(path: Path, *, filters_active: bool) -> str:
    if filters_active:
        return f"no log lines matched --since/--level at {path}"
    return f"no log lines at {path}"


def _follow_log(
    path: Path,
    *,
    lines: int,
    since: datetime | None,
    min_level: int | None,
    secret_values: Sequence[str],
    json_output: bool,
) -> None:
    inode: int | None = None
    offset = 0
    try:
        inode = path.stat().st_ino
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            selection = _select_records(
                handle,
                since=since,
                min_level=min_level,
                secret_values=secret_values,
                max_records=lines,
            )
        offset = path.stat().st_size
    except FileNotFoundError:
        inode = None
        offset = 0
        if not json_output:
            typer.echo(f"no system log at {path}; waiting for it to appear", err=True)
    except OSError as exc:
        # A directory, permission denial, etc. is terminal: never spin forever.
        emit_error(
            "DATA_UNAVAILABLE",
            f"could not read system log at {path}: {type(exc).__name__}",
            json_output=json_output,
        )
        raise typer.Exit(code=1) from exc
    else:
        _warn_dropped(selection.dropped)
        for record in selection.records:
            _emit_record(record, json_lines=json_output)
    try:
        while True:
            _sleep(_FOLLOW_POLL_SECONDS)
            try:
                stat = path.stat()
            except FileNotFoundError:
                offset = 0
                inode = None
                continue
            except OSError as exc:
                # Surface a persistent stat error once instead of hanging.
                emit_error(
                    "DATA_UNAVAILABLE",
                    f"could not stat system log at {path}: {type(exc).__name__}",
                    json_output=json_output,
                )
                raise typer.Exit(code=1) from exc
            # A truncated or rotated file restarts from the beginning.
            if inode is not None and stat.st_ino != inode:
                offset = 0
            inode = stat.st_ino
            if stat.st_size < offset:
                offset = 0
            if stat.st_size == offset:
                continue
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
            except FileNotFoundError:
                offset = 0
                inode = None
                continue
            except OSError as exc:
                emit_error(
                    "DATA_UNAVAILABLE",
                    f"could not read system log at {path}: {type(exc).__name__}",
                    json_output=json_output,
                )
                raise typer.Exit(code=1) from exc
            if chunk.endswith(b"\n"):
                consumed = chunk
            else:
                last_newline = chunk.rfind(b"\n")
                if last_newline == -1:
                    # No complete line yet; wait for the writer to finish it.
                    continue
                consumed = chunk[: last_newline + 1]
            offset += len(consumed)
            selection = _select_records(
                consumed.decode("utf-8", errors="replace").splitlines(),
                since=since,
                min_level=min_level,
                secret_values=secret_values,
            )
            _warn_dropped(selection.dropped)
            for record in selection.records:
                _emit_record(record, json_lines=json_output)
    except KeyboardInterrupt:
        return


@system_app.command("logs")
def system_logs(
    lines: Annotated[int, typer.Option("--lines", "-n", min=1, help="Tail line count.")] = 100,
    since: Annotated[
        str | None,
        typer.Option("--since", help="ISO-8601 instant or relative window (10m, 2h, 1d)."),
    ] = None,
    level: Annotated[
        str | None,
        typer.Option("--level", help="Minimum severity: DEBUG/INFO/WARNING/ERROR/CRITICAL."),
    ] = None,
    follow: Annotated[
        bool,
        typer.Option(
            "--follow",
            help="Stream appended lines until SIGINT (with --json, one JSON object per line).",
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the selection as JSON.")
    ] = False,
) -> None:
    """Print or follow the local structured daemon log, redacted."""
    settings = settings_or_exit(json_output)
    since_at: datetime | None = None
    min_level: int | None = None
    try:
        if since is not None:
            since_at = _parse_since(since, settings)
        if level is not None:
            min_level = _parse_level(level)
    except ValueError as exc:
        _usage_error(str(exc), json_output=json_output)

    path = log_path(settings)
    secret_values = _configured_secret_values(settings)
    if follow:
        _follow_log(
            path,
            lines=lines,
            since=since_at,
            min_level=min_level,
            secret_values=secret_values,
            json_output=json_output,
        )
        return

    filters_active = since_at is not None or min_level is not None
    generated_at = utc_timestamp(_now(settings))
    try:
        # Stream the file: never read an unbounded log into memory.
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            selection = _select_records(
                handle,
                since=since_at,
                min_level=min_level,
                secret_values=secret_values,
                max_records=lines,
            )
    except FileNotFoundError:
        if json_output:
            typer.echo(
                json.dumps(
                    success_envelope(
                        {
                            "path": str(path),
                            "lines": [],
                            "matched": 0,
                            "returned": 0,
                            "dropped": 0,
                        },
                        request_id=new_request_id(),
                        generated_at=generated_at,
                        provenance="CALC",
                        data_quality="COMPLETE",
                    ),
                    indent=2,
                )
            )
        else:
            typer.echo(f"no system log at {path}")
        return
    except OSError as exc:
        emit_error(
            "DATA_UNAVAILABLE",
            f"could not read system log at {path}: {type(exc).__name__}",
            json_output=json_output,
        )
        raise typer.Exit(code=1) from exc

    _warn_dropped(selection.dropped)
    if not selection.records:
        cause = _empty_cause(path, filters_active=filters_active)
        if json_output:
            typer.echo(
                json.dumps(
                    success_envelope(
                        {
                            "path": str(path),
                            "lines": [],
                            "matched": selection.matched,
                            "returned": 0,
                            "dropped": selection.dropped,
                            "cause": cause,
                        },
                        request_id=new_request_id(),
                        generated_at=generated_at,
                        provenance="CALC",
                        data_quality="PARTIAL" if selection.dropped else "COMPLETE",
                    ),
                    indent=2,
                )
            )
        else:
            typer.echo(cause)
        return
    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    {
                        "path": str(path),
                        "lines": list(selection.records),
                        "matched": selection.matched,
                        "returned": len(selection.records),
                        "dropped": selection.dropped,
                    },
                    request_id=new_request_id(),
                    generated_at=generated_at,
                    provenance="CALC",
                    data_quality="PARTIAL" if selection.dropped else "COMPLETE",
                ),
                indent=2,
            )
        )
        return
    for record in selection.records:
        _emit_record(record, json_lines=False)


__all__ = [
    "PROMPT_REDACTED",
    "REDACTED",
    "redact_record",
    "system_app",
    "system_info",
    "system_logs",
]
