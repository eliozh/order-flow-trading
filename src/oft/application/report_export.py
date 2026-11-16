"""Deterministic Markdown/JSON rendering for report export (slice 42).

The export is a *view* of ``report_payload``; it never re-reads or re-computes a
report, so frozen content, provenance, and data-quality stamps cannot drift.
Every value is allowlisted and escaped: the Markdown never renders a raw tag.

The renderer does **not** redact. Redaction is a caller precondition: the route
applies :func:`redact_report_payload` before calling either renderer, and
:func:`render_report_json` is public and serializes exactly what it is given.
A caller that renders an unredacted payload is responsible for that choice.

This module is pure and imports only the standard library. It has no CLI or API
coupling: the renderer is deterministic for identical input, and both surfaces
reuse it so frozen content and stamps cannot drift. (The JSON surface wraps the
payload in the shared envelope, which carries a fresh request id per response.)
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

#: Export formats the route and CLI accept.
EXPORT_FORMATS: tuple[str, ...] = ("md", "json")

#: Exact-key denylist (case-insensitive). It is intentionally *exact*: keys such
#: as ``prompt_id``, ``prompt_version``, ``llm_tokens``, ``total_tokens``,
#: ``cost``, and ``usage`` are provenance metadata and must survive.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "api_secret",
        "secret",
        "secret_key",
        "password",
        "authorization",
        "bearer",
        "token",
        "access_token",
        "refresh_token",
        "prompt",
        "system_prompt",
        "user_prompt",
        "prompt_text",
        "prompt_body",
        "raw_prompt",
        "request_body",
        "response_body",
        "raw_response",
        "messages",
        "headers",
        "cookies",
        "credentials",
    }
)

#: Additional spellings of the same secrets, matched by exact *normalized*
#: equality (lowercase, ``_`` and ``-`` removed) rather than by substring. The
#: normalized denylist above already covers keys such as ``API-KEY``; these
#: named variants cover the provider conventions that appear in stored JSON.
_SENSITIVE_KEY_VARIANTS: frozenset[str] = frozenset(
    {
        "apikey",
        "openaiapikey",
        "xapikey",
        "clientsecret",
        "secretvalue",
    }
)


def _normalize_key(key: str) -> str:
    return key.lower().replace("_", "").replace("-", "")


_SENSITIVE_KEY_NORMALIZED: frozenset[str] = (
    frozenset(_normalize_key(key) for key in SENSITIVE_KEYS) | _SENSITIVE_KEY_VARIANTS
)


def _is_sensitive_key(key: str) -> bool:
    # Normalization subsumes the exact denylist: every denylist key's
    # normalized form is already in the normalized set.
    return _normalize_key(key) in _SENSITIVE_KEY_NORMALIZED


#: Metric-family order used by the data-quality stamp scan. The CALC table
#: reads families explicitly; this tuple only drives per-family stamp rows.
_METRIC_FAMILIES: tuple[str, ...] = (
    "cvd",
    "volume_profile",
    "footprint",
    "open_interest",
    "funding",
    "liquidations",
    "gaps",
)

#: HTML entities first, then the Markdown characters that could open emphasis,
#: a link, a heading, a table cell, or a fence. ``.``/``-``/``+``/``(``/``)``
#: are deliberately left alone: every dynamic value is inline (never at line
#: start) and ``[``/``]`` are escaped, so they cannot change the structure.
_ESCAPES = str.maketrans(
    {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "\\": "\\\\",
        "`": "\\`",
        "*": "\\*",
        "[": "\\[",
        "]": "\\]",
        "{": "\\{",
        "}": "\\}",
        "|": "\\|",
        "#": "\\#",
        "~": "\\~",
    }
)

_NO_STAMP = "—"

#: C0/C1 controls that can move a cursor or start a terminal escape. Tab, CR,
#: and LF are excluded: CR/LF are normalized to spaces and a tab is inert inline.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

#: Absolute POSIX paths and Windows drive paths in an error reason. Only
#: ``llm.reason`` is passed through these; other payload strings are untouched.
_ABSOLUTE_PATHS = (
    re.compile(r"(?<![\w:/])/(?:[^\s/]+/)*[^\s/]+"),
    re.compile(r"\b[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]+"),
)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text(value: Any, default: str = "N/A") -> str:
    if value is None or value == "":
        return default
    return str(value)


def escape_markdown_text(value: object) -> str:
    """Escape one dynamic value for inline Markdown (``''`` for ``None``).

    CR/LF become spaces, dangerous C0/C1 control characters are stripped (a tab
    is kept because it is inert inline), and the remaining Markdown/HTML
    metacharacters are escaped. Underscores are escaped only at a word
    boundary: CommonMark never turns an intraword ``_`` into emphasis, so
    identifiers such as ``OPPORTUNITY_PROPOSAL`` and ``open_interest`` stay
    readable while ``_emphasis_`` stays inert.
    """
    if value is None:
        return ""
    text = str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    text = _CONTROL_CHARACTERS.sub("", text)
    escaped: list[str] = []
    for index, character in enumerate(text):
        if character == "_":
            before = text[index - 1] if index > 0 else ""
            after = text[index + 1] if index + 1 < len(text) else ""
            escaped.append("_" if before.isalnum() and after.isalnum() else "\\_")
            continue
        escaped.append(character.translate(_ESCAPES))
    return "".join(escaped)


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _redact_value(item)
            for key, item in value.items()
            if not _is_sensitive_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def _basename_absolute_paths(text: str) -> str:
    """Replace absolute POSIX/Windows paths with their basename only."""
    for pattern in _ABSOLUTE_PATHS:
        text = pattern.sub(lambda match: match.group(0).replace("\\", "/").rsplit("/", 1)[-1], text)
    return text


def redact_report_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a fresh, recursively redacted copy.

    Exact ``SENSITIVE_KEYS`` and their named normalized variants (``apiKey``,
    ``openai_api_key``, ``x-api-key``, ``client_secret``, ``secret_value``) are
    dropped. Daemon-absolute paths inside ``llm.reason`` are reduced to their
    basename; no other payload string is rewritten. This is the export path's
    redaction step: both formats render the returned copy.
    """
    redacted = _redact_value(payload)
    if not isinstance(redacted, dict):
        return {}
    llm = redacted.get("llm")
    if isinstance(llm, dict) and isinstance(llm.get("reason"), str):
        llm["reason"] = _basename_absolute_paths(llm["reason"])
    return redacted


def _has_items(mapping: Mapping[str, Any], key: str) -> bool:
    return bool(_sequence(mapping.get(key)))


def provenance_counts(payload: Mapping[str, Any]) -> dict[str, int]:
    """Count measured CALC families plus the snapshot stamp and the LLM band.

    A CALC block is credited when a family carries measured data; the snapshot
    stamp itself counts as one block. Untrusted JSON values are type-checked
    before membership so a list/dict value cannot raise ``TypeError``.
    """
    metrics = _mapping(payload.get("metrics"))
    snapshot = _mapping(payload.get("snapshot"))
    llm = _mapping(payload.get("llm"))

    calc = 0
    if _has_items(_mapping(metrics.get("cvd")), "points"):
        calc += 1
    if _has_items(_mapping(metrics.get("volume_profile")), "bins"):
        calc += 1
    if _has_items(_mapping(metrics.get("footprint")), "bars"):
        calc += 1
    if _has_items(_mapping(metrics.get("open_interest")), "points"):
        calc += 1
    if _has_items(_mapping(metrics.get("funding")), "history"):
        calc += 1
    liquidations = _mapping(metrics.get("liquidations"))
    count = liquidations.get("count")
    coverage = liquidations.get("coverage")
    if (isinstance(count, int) and count > 0) or (
        isinstance(coverage, str) and coverage in ("SINCE_DEPLOYMENT", "PARTIAL")
    ):
        calc += 1
    if _has_items(metrics, "gaps"):
        calc += 1
    if snapshot.get("data_quality"):
        calc += 1

    status = llm.get("validation_status")
    llm_blocks = (
        1
        if bool(llm.get("configured"))
        and isinstance(status, str)
        and status in ("VALID", "REPAIRED")
        else 0
    )
    return {"calc": calc, "llm": llm_blocks}


def _row(label: str, value: object, quality: object | None = None) -> str:
    quality_text = _NO_STAMP if quality is None or quality == "" else escape_markdown_text(quality)
    return f"| {escape_markdown_text(label)} | {escape_markdown_text(value)} | {quality_text} |"


def _calc_rows(metrics: Mapping[str, Any]) -> list[str]:
    cvd = _mapping(metrics.get("cvd"))
    summary = _mapping(cvd.get("summary"))
    points = _sequence(cvd.get("points"))
    latest_delta = _mapping(points[-1]).get("delta") if points else None

    profile = _mapping(metrics.get("volume_profile"))
    footprint = _mapping(metrics.get("footprint"))
    oi = _mapping(metrics.get("open_interest"))
    funding = _mapping(metrics.get("funding"))
    liquidations = _mapping(metrics.get("liquidations"))
    gaps = _sequence(metrics.get("gaps"))

    fp_bars = _sequence(footprint.get("bars"))
    fp_cells = sum(len(_sequence(bar.get("cells"))) for bar in fp_bars if isinstance(bar, dict))

    oi_measured = _has_items(oi, "points")
    funding_measured = _has_items(funding, "history")
    oi_value = (
        _text(oi.get("delta_over_window")) if oi_measured else "N/A — open interest not measured"
    )
    funding_value = (
        _text(funding.get("current_rate")) if funding_measured else "N/A — funding not measured"
    )

    # The persisted contract stamps ``data_quality`` once for the metrics
    # document; a per-family stamp is only honored defensively if one appears.
    family_default = metrics.get("data_quality")

    def _family_quality(family: Mapping[str, Any]) -> object | None:
        stamp = family.get("data_quality")
        return stamp if stamp is not None else family_default

    return [
        _row("CVD (window)", _text(summary.get("cvd_change")), _family_quality(cvd)),
        _row("Delta (latest)", _text(latest_delta), _family_quality(cvd)),
        _row("POC", _text(profile.get("poc")), _family_quality(profile)),
        _row(
            "VAH / VAL",
            f"{_text(profile.get('vah'))} / {_text(profile.get('val'))}",
            _family_quality(profile),
        ),
        _row(
            "Volume Profile",
            f"{len(_sequence(profile.get('bins')))} bins · "
            f"{_text(profile.get('bin_size'))} per bin",
            _family_quality(profile),
        ),
        _row(
            "Footprint",
            f"{len(fp_bars)} bars · {fp_cells} cells",
            _family_quality(footprint),
        ),
        _row("Open interest Δ", oi_value, _family_quality(oi)),
        _row("Funding", funding_value, _family_quality(funding)),
        _row(
            "Liquidations",
            f"{_text(liquidations.get('count'), '0')} events · "
            f"{_text(liquidations.get('coverage'), 'NONE')}",
            _family_quality(liquidations),
        ),
        _row("Gaps", len(gaps), family_default),
    ]


def _quality_stamp_rows(metrics: Mapping[str, Any], snapshot: Mapping[str, Any]) -> list[str]:
    rows: list[tuple[str, str]] = []
    # A per-family stamp is not part of the metrics contract, but honor one if
    # a future document carries it.
    for name in _METRIC_FAMILIES:
        family = _mapping(metrics.get(name))
        stamp = family.get("data_quality")
        if stamp is not None:
            rows.append((name, str(stamp)))
    metrics_quality = metrics.get("data_quality")
    if metrics_quality is not None:
        rows.append(("metrics", str(metrics_quality)))
    snapshot_quality = snapshot.get("data_quality")
    if snapshot_quality is not None:
        rows.append(("snapshot", str(snapshot_quality)))
    reasons = _sequence(snapshot.get("quality_reasons")) or _sequence(
        metrics.get("quality_reasons")
    )
    rows.append(("quality_reasons", "; ".join(str(reason) for reason in reasons) or "none"))
    return [
        f"| {escape_markdown_text(name)} | {escape_markdown_text(stamp)} |"
        for name, stamp in sorted(rows)
    ]


def is_configuration_reason(reason: str) -> bool:
    """True when a ``configured=false`` reason is a configuration skip.

    ``LLM_DISABLED_REASON`` starts "LLM not configured" and
    ``LLM_PROVIDER_NOT_CONFIGURED_REASON`` starts
    ``LLM_PROVIDER_NOT_CONFIGURED``; every other skip (for example
    ``LIVE_DATA_STALE``) is not a configuration failure.
    """
    lowered = reason.lower()
    return lowered.startswith("llm not configured") or lowered.startswith(
        "llm_provider_not_configured"
    )


def _llm_lines(llm: Mapping[str, Any]) -> list[str]:
    configured = bool(llm.get("configured"))
    status = llm.get("validation_status")
    if configured and isinstance(status, str) and status in ("VALID", "REPAIRED"):
        narrative = llm.get("narrative") or llm.get("rationale") or "(no narrative)"
        return [f"LLM › {escape_markdown_text(narrative)}"]
    if configured:
        reason = _text(llm.get("reason"), "the model output was rejected")
        return [
            f"LLM rejected — {escape_markdown_text(reason)}",
            "No narrative, opportunity, or order was created from this output.",
        ]
    reason = _text(llm.get("reason"), "LLM not configured; no narrative or decision was generated.")
    notice = (
        escape_markdown_text(reason)
        if is_configuration_reason(reason)
        else f"LLM skipped — {escape_markdown_text(reason)}"
    )
    return [
        notice,
        "No LLM call, order, or credential was required or recorded.",
    ]


def render_report_markdown(payload: Mapping[str, Any]) -> str:
    """Render the allowlisted Markdown export; exactly one trailing newline.

    This renderer does not redact: pass a payload already processed by
    :func:`redact_report_payload` (as the route does) when it may contain
    secrets or raw prompts. Values are still HTML/Markdown-escaped here.
    """
    report_id = payload.get("report_id")
    symbol = payload.get("symbol")
    timeframe = payload.get("timeframe")
    metrics = _mapping(payload.get("metrics"))
    snapshot = _mapping(payload.get("snapshot"))
    llm = _mapping(payload.get("llm"))
    window = _mapping(snapshot.get("window")) or _mapping(metrics.get("window"))
    counts = provenance_counts(payload)

    prompt = "N/A"
    if llm.get("prompt_id") is not None and llm.get("prompt_version") is not None:
        prompt = f"{llm.get('prompt_id')} v{llm.get('prompt_version')}"
    provider = "N/A/N/A"
    if llm.get("provider") is not None or llm.get("model") is not None:
        provider = f"{_text(llm.get('provider'))}/{_text(llm.get('model'))}"
    cost_basis = "unpriced" if payload.get("cost_unpriced", True) else "priced"
    opportunities = _sequence(llm.get("opportunity_ids"))
    provenance = "CALC (metrics)"
    if counts["llm"]:
        provenance += " · LLM (narrative)"

    lines: list[str] = [
        f"# REPORT {escape_markdown_text(report_id)} · {escape_markdown_text(symbol)}"
        f" · {escape_markdown_text(timeframe)}",
        "",
        f"Window: {escape_markdown_text(window.get('start'))} → "
        f"{escape_markdown_text(window.get('end'))}",
        f"Generated: {escape_markdown_text(payload.get('generated_at'))}",
        f"Scan: {escape_markdown_text(payload.get('scan_id'))}",
        f"Data quality: {escape_markdown_text(payload.get('data_quality'))}",
        f"Provenance: {escape_markdown_text(provenance)}",
        f"Prompt: {escape_markdown_text(prompt)}",
        f"Decision: {escape_markdown_text(_text(llm.get('decision_type')))}",
        f"Confidence: {escape_markdown_text(_text(llm.get('confidence')))}",
        f"Provider: {escape_markdown_text(provider)}",
        f"Cost: {escape_markdown_text(_text(payload.get('cost_usd'), '0'))} USD ({cost_basis})"
        f" · tokens {escape_markdown_text(payload.get('llm_tokens', 0))}",
        "Opportunities: "
        + (", ".join(escape_markdown_text(item) for item in opportunities) or "none"),
        "",
        "## CALC metrics",
        "",
        "| Metric | Value | Quality |",
        "| --- | --- | --- |",
        *_calc_rows(metrics),
        "",
        "### Data quality stamps",
        "",
        "| Part | Data quality |",
        "| --- | --- |",
        *_quality_stamp_rows(metrics, snapshot),
        "",
        "## LLM band",
        "",
        *_llm_lines(llm),
        "",
        "## Provenance",
        "",
        f"CALC blocks: {counts['calc']} · LLM blocks: {counts['llm']}",
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def render_report_json(envelope: Mapping[str, Any]) -> str:
    """Serialize the shared envelope deterministically (sorted, 2-space, UTF-8).

    The renderer does not redact: it serializes exactly the mapping it is given,
    so the caller applies :func:`redact_report_payload` first when needed.
    """
    return json.dumps(dict(envelope), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


__all__ = [
    "EXPORT_FORMATS",
    "SENSITIVE_KEYS",
    "escape_markdown_text",
    "is_configuration_reason",
    "provenance_counts",
    "redact_report_payload",
    "render_report_json",
    "render_report_markdown",
]
