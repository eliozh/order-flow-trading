"""Deterministic, redacted Markdown/JSON rendering for report export (slice 42).

The export is a *view* of ``report_payload``; it never re-reads or re-computes a
report, so frozen content, provenance, and data-quality stamps cannot drift.
Every value is allowlisted and escaped: the Markdown never renders a raw tag,
and the redactor drops secret/prompt keys before either format is built.

This module is pure and imports only the standard library. It has no CLI or API
coupling: the renderer is deterministic for identical input, and both surfaces
reuse it so frozen content and stamps cannot drift. (The JSON surface wraps the
payload in the shared envelope, which carries a fresh request id per response.)
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

#: Export formats the route and CLI accept.
EXPORT_FORMATS: tuple[str, ...] = ("md", "json")

#: Exact-key denylist (case-insensitive). It is intentionally *exact*: keys such
#: as ``prompt_id``, ``prompt_version``, ``llm_tokens``, and ``total_tokens``
#: are provenance metadata and must survive.
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

#: The metric families the CALC table renders, in table order. Nothing outside
#: this allowlist (or the fixed metadata fields below) reaches the Markdown.
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

    Underscores are escaped only at a word boundary: CommonMark never turns an
    intraword ``_`` into emphasis, so identifiers such as ``OPPORTUNITY_PROPOSAL``
    and ``open_interest`` stay readable while ``_emphasis_`` stays inert.
    """
    if value is None:
        return ""
    text = str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
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
            if str(key).lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def redact_report_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a fresh, recursively redacted copy (drop ``SENSITIVE_KEYS``)."""
    redacted = _redact_value(payload)
    return redacted if isinstance(redacted, dict) else {}


def _has_items(mapping: Mapping[str, Any], key: str) -> bool:
    return bool(_sequence(mapping.get(key)))


def provenance_counts(payload: Mapping[str, Any]) -> dict[str, int]:
    """Count rendered CALC families and whether a valid LLM band is present."""
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
    if (isinstance(count, int) and count > 0) or coverage in {"SINCE_DEPLOYMENT", "PARTIAL"}:
        calc += 1
    if _has_items(metrics, "gaps"):
        calc += 1
    if snapshot.get("data_quality"):
        calc += 1

    llm_blocks = (
        1
        if bool(llm.get("configured"))
        and llm.get("validation_status")
        in {
            "VALID",
            "REPAIRED",
        }
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


def _llm_lines(llm: Mapping[str, Any]) -> list[str]:
    configured = bool(llm.get("configured"))
    status = llm.get("validation_status")
    if configured and status in {"VALID", "REPAIRED"}:
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
        if reason.lower().startswith("llm not configured")
        else f"LLM not configured — {escape_markdown_text(reason)}"
    )
    return [
        notice,
        "No LLM call, order, or credential was required or recorded.",
    ]


def render_report_markdown(payload: Mapping[str, Any]) -> str:
    """Render the allowlisted Markdown export; exactly one trailing newline."""
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
    """Serialize the shared envelope deterministically (sorted, 2-space, UTF-8)."""
    return json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


__all__ = [
    "EXPORT_FORMATS",
    "SENSITIVE_KEYS",
    "escape_markdown_text",
    "provenance_counts",
    "redact_report_payload",
    "render_report_json",
    "render_report_markdown",
]
