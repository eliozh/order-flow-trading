"""Compiled knowledge artifact model (ADR-0009).

This module is pure: it parses synthetic/book-to-skill markdown, classifies
sections into the order-flow taxonomy, assigns stable ``K-<topic>-<n>`` IDs,
enforces the playbook token budget, builds ``topics.json``/``rules.json``, and
validates the private manifest. Filesystem I/O lives in
``oft.application.knowledge_service``; prompt injection lives in
``oft.application.prompt_builder``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

COMPILER_VERSION = "knowledge-compiler.v1"
TOKEN_COUNT_METHOD = "chars_div_4"
CHARS_PER_TOKEN = 4
PLAYBOOK_TOKEN_BUDGET = 10_000
INJECTION_TOKEN_BUDGET = 10_000
RULES_TOKEN_BUDGET = 2_000
GLOSSARY_TOKEN_BUDGET = 1_500
LICENSE_NOTE = "private; do not distribute"
PRIVATE_WARNING = (
    "The knowledge artifact is private: it may contain derived copyrighted "
    "material. Do not export, share, or commit it."
)

KNOWLEDGE_ABSENT = "KNOWLEDGE_ABSENT"
KNOWLEDGE_MANIFEST_INVALID = "KNOWLEDGE_MANIFEST_INVALID"
KNOWLEDGE_ARTIFACT_MISMATCH = "KNOWLEDGE_ARTIFACT_MISMATCH"
KNOWLEDGE_SOURCE_EMPTY = "KNOWLEDGE_SOURCE_EMPTY"
KNOWLEDGE_NO_SECTIONS = "KNOWLEDGE_NO_SECTIONS"
KNOWLEDGE_NOT_REVIEWED = "KNOWLEDGE_NOT_REVIEWED"
KNOWLEDGE_NOT_ACTIVE = "KNOWLEDGE_NOT_ACTIVE"
KNOWLEDGE_MODE_UNAVAILABLE = "KNOWLEDGE_MODE_UNAVAILABLE"
KNOWLEDGE_BUDGET_EXCEEDED = "KNOWLEDGE_BUDGET_EXCEEDED"

REVIEW_DRAFT = "DRAFT"
REVIEW_APPROVED = "APPROVED"

MIN_SECTION_TOKENS = 8
_ARTIFACT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TOPIC_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
SECTION_ID_PATTERN = re.compile(r"^K-[a-z][a-z0-9_]*-\d+$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_SECTION_HEADING = re.compile(r"^##\s+(K-[a-z][a-z0-9_]*-\d+)\s+—\s+(.*\S)\s*$")

PLAYBOOK_HEADER = "# Order-flow playbook\n\n"


@dataclass(frozen=True)
class TopicDefinition:
    """One order-flow topic and the keywords that classify a section into it."""

    name: str
    title: str
    keywords: tuple[str, ...]


DEFAULT_TAXONOMY: tuple[TopicDefinition, ...] = (
    TopicDefinition(
        "market_structure",
        "Market structure and auction theory",
        ("market structure", "auction", "acceptance", "rejection"),
    ),
    TopicDefinition(
        "absorption", "Absorption and exhaustion", ("absorption", "exhaustion", "trapped")
    ),
    TopicDefinition("cvd", "Cumulative volume delta", ("cvd", "cumulative volume delta")),
    TopicDefinition("delta", "Delta interpretation", ("delta",)),
    TopicDefinition(
        "footprint", "Footprint reading", ("footprint", "imbalance", "stacked imbalance")
    ),
    TopicDefinition(
        "volume_profile", "Volume profile", ("volume profile", "poc", "point of control")
    ),
    TopicDefinition("open_interest", "Open interest", ("open interest",)),
    TopicDefinition("funding", "Funding context", ("funding",)),
    TopicDefinition(
        "liquidations", "Liquidations and stop runs", ("liquidation", "stop run", "stop-run")
    ),
    TopicDefinition("entry", "Entry and invalidation", ("entry", "invalidation")),
    TopicDefinition("management", "Trade management", ("trade management", "management")),
    TopicDefinition("risk", "Risk", ("risk", "position size")),
    TopicDefinition(
        "psychology", "Psychology and discipline", ("psychology", "discipline", "revenge")
    ),
    TopicDefinition(
        "anti_patterns",
        "Anti-patterns and review checklists",
        ("anti-pattern", "anti pattern", "checklist", "review"),
    ),
)


class KnowledgeError(Exception):
    """A knowledge pipeline failure carrying a stable internal code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class KnowledgeModeUnavailable(KnowledgeError):
    """Mode B was requested without a usable deterministic topic mapping."""

    def __init__(self, message: str) -> None:
        super().__init__(KNOWLEDGE_MODE_UNAVAILABLE, message)


@dataclass(frozen=True)
class RawSection:
    title: str
    body: str
    source: str
    order: int


@dataclass(frozen=True)
class KnowledgeSection:
    section_id: str
    topic: str
    title: str
    body: str
    source: str


@dataclass(frozen=True)
class DroppedContent:
    title: str
    reason: str
    tokens: int
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "reason": self.reason,
            "tokens": self.tokens,
            "source": self.source,
        }


@dataclass(frozen=True)
class BudgetResult:
    sections: tuple[KnowledgeSection, ...]
    dropped: tuple[DroppedContent, ...]
    summarized: bool
    token_count: int


@dataclass(frozen=True)
class KnowledgeManifest:
    artifact_id: str
    compiler_version: str
    source_skills: tuple[dict[str, str], ...]
    created_at: str
    license_note: str
    reviewed_by: str | None
    token_count_method: str
    token_count: int
    playbook_token_budget: int
    review_status: str = REVIEW_DRAFT
    reviewed_at: str | None = None
    summarized: bool = False
    dropped: tuple[dict[str, Any], ...] = ()
    sections: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "compiler_version": self.compiler_version,
            "source_skills": [dict(item) for item in self.source_skills],
            "created_at": self.created_at,
            "license_note": self.license_note,
            "reviewed_by": self.reviewed_by,
            "review_status": self.review_status,
            "reviewed_at": self.reviewed_at,
            "token_count_method": self.token_count_method,
            "token_count": self.token_count,
            "playbook_token_budget": self.playbook_token_budget,
            "summarized": self.summarized,
            "dropped": [dict(item) for item in self.dropped],
            "sections": list(self.sections),
            "topics": list(self.topics),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> KnowledgeManifest:
        return validate_manifest(data)


@dataclass(frozen=True)
class CompiledArtifact:
    playbook: str
    sections: tuple[KnowledgeSection, ...]
    topics: dict[str, Any]
    rules: dict[str, Any]
    manifest: KnowledgeManifest

    @property
    def artifact_id(self) -> str:
        return self.manifest.artifact_id


@dataclass(frozen=True)
class KnowledgeState:
    status: str
    reason: str | None
    artifact_id: str | None
    exact_replay: bool
    active: bool
    reviewed_by: str | None = None
    review_status: str | None = None
    token_count: int | None = None
    license_note: str | None = None
    private: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "artifact_id": self.artifact_id,
            "exact_replay": self.exact_replay,
            "active": self.active,
            "reviewed_by": self.reviewed_by,
            "review_status": self.review_status,
            "token_count": self.token_count,
            "license_note": self.license_note,
            "private": self.private,
        }


def count_tokens(text: str) -> int:
    """Documented token approximation: ``ceil(len(text) / 4)`` characters."""
    if not text:
        return 0
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def summarize_text(text: str, max_tokens: int) -> str:
    """Deterministically keep the leading prose within ``max_tokens``.

    This is an extractive summary (first complete sentences/lines), never a
    concatenation of later material without a budget.
    """
    normalized = _normalize_body(text)
    if count_tokens(normalized) <= max_tokens:
        return normalized
    max_chars = max(CHARS_PER_TOKEN, max_tokens * CHARS_PER_TOKEN)
    window = normalized[:max_chars]
    cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
    kept = window if cut <= 0 else window[: cut + 1]
    kept = kept.rstrip()
    if not kept:
        kept = window.rstrip()
    return f"{kept}\n[summarized]"


def _normalize_body(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]

    def _trim(lines_in: list[str]) -> list[str]:
        start = 0
        end = len(lines_in)
        while start < end and not lines_in[start].strip():
            start += 1
        while end > start and not lines_in[end - 1].strip():
            end -= 1
        return lines_in[start:end]

    return "\n".join(_trim(lines))


def _normalize_heading(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_markdown(markdown: str, *, source: str = "inline") -> tuple[RawSection, ...]:
    """Split a skill document into level-2+ sections with normalized bodies."""
    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    sections: list[RawSection] = []
    title: str | None = None
    body: list[str] = []

    def _flush() -> None:
        if title is None:
            return
        text = _normalize_body("\n".join(body))
        if text:
            sections.append(RawSection(title=title, body=text, source=source, order=len(sections)))

    for raw_line in normalized.split("\n"):
        line = raw_line.rstrip()
        match = _HEADING.match(line)
        if match is not None:
            _flush()
            title = None
            body = []
            if len(match.group(1)) >= 2:
                title = _normalize_heading(match.group(2))
            continue
        if title is not None:
            body.append(line)
    _flush()
    return tuple(sections)


def topic_definitions(config: Mapping[str, Any] | None = None) -> tuple[TopicDefinition, ...]:
    """Read a taxonomy from ``topics.json``; fall back to the built-in list."""
    if config is None:
        return DEFAULT_TAXONOMY
    raw_topics = config.get("topics")
    if not isinstance(raw_topics, list):
        return DEFAULT_TAXONOMY
    definitions: list[TopicDefinition] = []
    for item in raw_topics:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        title = item.get("title")
        keywords = item.get("keywords")
        if not isinstance(name, str) or _TOPIC_PATTERN.fullmatch(name) is None:
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        keyword_values = (
            tuple(str(keyword) for keyword in keywords if isinstance(keyword, str))
            if isinstance(keywords, list)
            else ()
        )
        definitions.append(
            TopicDefinition(
                name=name,
                title=title.strip(),
                keywords=keyword_values or (name.replace("_", " "),),
            )
        )
    return tuple(definitions) or DEFAULT_TAXONOMY


def _classify(title: str, body: str, taxonomy: Sequence[TopicDefinition]) -> str | None:
    title_lower = title.lower()
    for definition in taxonomy:
        if any(keyword.lower() in title_lower for keyword in definition.keywords):
            return definition.name
    body_lower = body.lower()
    for definition in taxonomy:
        if any(keyword.lower() in body_lower for keyword in definition.keywords):
            return definition.name
    return None


def render_section(section: KnowledgeSection) -> str:
    return f"## {section.section_id} — {section.title}\n\n{section.body.strip()}\n"


def render_playbook(sections: Sequence[KnowledgeSection]) -> str:
    if not sections:
        return PLAYBOOK_HEADER
    return PLAYBOOK_HEADER + "\n" + "\n".join(render_section(section) for section in sections)


def _total_tokens(sections: Sequence[KnowledgeSection]) -> int:
    return count_tokens(render_playbook(sections))


def _classify_sections(
    raw_sections: Sequence[RawSection], taxonomy: Sequence[TopicDefinition]
) -> tuple[tuple[KnowledgeSection, ...], tuple[DroppedContent, ...]]:
    counters: dict[str, int] = {}
    selected: list[KnowledgeSection] = []
    dropped: list[DroppedContent] = []
    for raw in raw_sections:
        topic = _classify(raw.title, raw.body, taxonomy)
        if topic is None:
            dropped.append(
                DroppedContent(
                    title=raw.title,
                    reason="out_of_taxonomy",
                    tokens=count_tokens(raw.body),
                    source=raw.source,
                )
            )
            continue
        counters[topic] = counters.get(topic, 0) + 1
        selected.append(
            KnowledgeSection(
                section_id=f"K-{topic}-{counters[topic]}",
                topic=topic,
                title=raw.title,
                body=raw.body,
                source=raw.source,
            )
        )
    return tuple(selected), tuple(dropped)


def _dedupe_dropped(items: Sequence[DroppedContent]) -> tuple[DroppedContent, ...]:
    best: dict[tuple[str, str, str], DroppedContent] = {}
    for item in items:
        key = (item.source, item.title, item.reason)
        current = best.get(key)
        if current is None or item.tokens > current.tokens:
            best[key] = item
    return tuple(best.values())


def fit_sections_to_budget(
    sections: Sequence[KnowledgeSection], *, budget: int = PLAYBOOK_TOKEN_BUDGET
) -> BudgetResult:
    """Select/summarize sections until the rendered playbook fits the budget."""
    header_tokens = count_tokens(PLAYBOOK_HEADER)
    body_budget = max(1, budget - header_tokens)
    if _total_tokens(sections) <= budget:
        return BudgetResult(tuple(sections), (), False, _total_tokens(sections))

    dropped: list[DroppedContent] = []
    current = list(sections)
    per_section = max(MIN_SECTION_TOKENS, body_budget // max(1, len(current)))
    for _ in range(16):
        summarized_sections: list[KnowledgeSection] = []
        for section in current:
            rendered_tokens = count_tokens(render_section(section))
            if rendered_tokens <= per_section:
                summarized_sections.append(section)
                continue
            heading_tokens = count_tokens(f"## {section.section_id} — {section.title}\n\n")
            allowed = max(1, per_section - heading_tokens)
            summary = summarize_text(section.body, allowed)
            if summary.strip() == section.body.strip():
                summarized_sections.append(section)
                continue
            lost = max(0, count_tokens(section.body) - count_tokens(summary))
            summarized_sections.append(replace(section, body=summary))
            if lost:
                dropped.append(
                    DroppedContent(
                        title=section.title,
                        reason="summarized",
                        tokens=lost,
                        source=section.source,
                    )
                )
        current = summarized_sections
        if _total_tokens(current) <= budget:
            return BudgetResult(
                tuple(current), _dedupe_dropped(dropped), True, _total_tokens(current)
            )
        per_section = max(MIN_SECTION_TOKENS, per_section * 3 // 4)

    while len(current) > 1 and _total_tokens(current) > budget:
        removed = current.pop()
        dropped.append(
            DroppedContent(
                title=removed.title,
                reason="budget",
                tokens=count_tokens(render_section(removed)),
                source=removed.source,
            )
        )

    if current and _total_tokens(current) > budget:
        section = current[0]
        summary = summarize_text(
            section.body,
            max(1, body_budget - count_tokens(f"## {section.section_id} — {section.title}\n\n")),
        )
        if summary.strip() != section.body.strip():
            lost = max(0, count_tokens(section.body) - count_tokens(summary))
            current[0] = replace(section, body=summary)
            dropped.append(
                DroppedContent(
                    title=section.title,
                    reason="summarized",
                    tokens=lost,
                    source=section.source,
                )
            )

    return BudgetResult(tuple(current), _dedupe_dropped(dropped), True, _total_tokens(current))


def _clean_mapping(value: object) -> Any:
    """Keep only JSON-safe mapping/list/string leaves from a config value."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return {str(key): _clean_mapping(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_mapping(item) for item in value]
    return None


def build_topics_document(
    taxonomy: Sequence[TopicDefinition],
    sections: Sequence[KnowledgeSection],
    calc_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    by_topic: dict[str, list[str]] = {}
    for section in sections:
        by_topic.setdefault(section.topic, []).append(section.section_id)
    topics: dict[str, Any] = {}
    for definition in taxonomy:
        section_ids = by_topic.get(definition.name)
        if section_ids is None:
            continue
        topics[definition.name] = {
            "title": definition.title,
            "keywords": list(definition.keywords),
            "section_ids": section_ids,
        }
    document: dict[str, Any] = {
        "version": 1,
        "token_count_method": TOKEN_COUNT_METHOD,
        "topics": topics,
    }
    cleaned = _clean_mapping(calc_context) if calc_context is not None else None
    if isinstance(cleaned, dict) and cleaned:
        document["calc_context"] = cleaned
    return document


def build_rules_document(
    rules_config: Mapping[str, Any] | None,
    topics_document: Mapping[str, Any],
) -> dict[str, Any]:
    roles: dict[str, Any] = {}
    raw_roles = rules_config.get("roles") if isinstance(rules_config, Mapping) else None
    if isinstance(raw_roles, Mapping):
        topics_section = topics_document.get("topics")
        for role in sorted(str(key) for key in raw_roles):
            entry = raw_roles.get(role)
            if not isinstance(entry, Mapping):
                continue
            raw_topics = entry.get("topics")
            topic_names = (
                [str(name) for name in raw_topics if isinstance(name, str)]
                if isinstance(raw_topics, list)
                else []
            )
            section_ids: list[str] = []
            if isinstance(topics_section, Mapping):
                for name in topic_names:
                    topic_entry = topics_section.get(name)
                    if not isinstance(topic_entry, Mapping):
                        continue
                    ids = topic_entry.get("section_ids")
                    if isinstance(ids, list):
                        section_ids.extend(str(item) for item in ids if isinstance(item, str))
            raw_rules = entry.get("rules")
            rules = (
                [str(rule) for rule in raw_rules if isinstance(rule, str)]
                if isinstance(raw_rules, list)
                else []
            )
            roles[role] = {
                "section_ids": list(dict.fromkeys(section_ids)),
                "rules": rules,
            }
    return {"version": 1, "roles": roles}


def artifact_hash(playbook: str, topics: Mapping[str, Any], rules: Mapping[str, Any]) -> str:
    """Deterministic content hash over the compiled outputs (not timestamps)."""
    payload = {"playbook": playbook, "topics": topics, "rules": rules}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compile_artifact(
    source_documents: Sequence[tuple[str, str]],
    *,
    generated_at: str,
    taxonomy: Sequence[TopicDefinition] | None = None,
    rules_config: Mapping[str, Any] | None = None,
    calc_context: Mapping[str, Any] | None = None,
    budget: int = PLAYBOOK_TOKEN_BUDGET,
    reviewed_by: str | None = None,
    review_status: str = REVIEW_DRAFT,
) -> CompiledArtifact:
    """Compile skill documents into a playbook + topic index + rules + manifest."""
    if budget < 1:
        raise KnowledgeError(KNOWLEDGE_SOURCE_EMPTY, "the playbook budget must be positive")
    resolved_taxonomy = tuple(taxonomy) if taxonomy else DEFAULT_TAXONOMY
    sources: list[dict[str, str]] = []
    raw_sections: list[RawSection] = []
    for slug, markdown in source_documents:
        if not markdown.strip():
            continue
        sources.append(
            {
                "slug": slug,
                "content_hash": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            }
        )
        raw_sections.extend(parse_markdown(markdown, source=slug))
    if not raw_sections:
        raise KnowledgeError(
            KNOWLEDGE_SOURCE_EMPTY,
            "no section headings were found in the knowledge source",
        )

    selected, dropped = _classify_sections(raw_sections, resolved_taxonomy)
    if not selected:
        raise KnowledgeError(
            KNOWLEDGE_NO_SECTIONS,
            "no source section matched the order-flow taxonomy",
        )
    budgeted = fit_sections_to_budget(selected, budget=budget)
    playbook = render_playbook(budgeted.sections)
    topics_document = build_topics_document(resolved_taxonomy, budgeted.sections, calc_context)
    rules_document = build_rules_document(rules_config, topics_document)
    digest = artifact_hash(playbook, topics_document, rules_document)
    dropped_items = _dedupe_dropped((*dropped, *budgeted.dropped))
    manifest = KnowledgeManifest(
        artifact_id=digest,
        compiler_version=COMPILER_VERSION,
        source_skills=tuple(sources),
        created_at=generated_at,
        license_note=LICENSE_NOTE,
        reviewed_by=reviewed_by,
        token_count_method=TOKEN_COUNT_METHOD,
        token_count=budgeted.token_count,
        playbook_token_budget=budget,
        review_status=review_status,
        summarized=budgeted.summarized,
        dropped=tuple(item.to_dict() for item in dropped_items),
        sections=tuple(section.section_id for section in budgeted.sections),
        topics=tuple(topics_document["topics"].keys()),
    )
    return CompiledArtifact(
        playbook=playbook,
        sections=budgeted.sections,
        topics=topics_document,
        rules=rules_document,
        manifest=manifest,
    )


def load_sections(playbook: str, *, source: str = "playbook.md") -> tuple[KnowledgeSection, ...]:
    """Parse a compiled playbook back into sections (used on artifact load)."""
    sections: list[KnowledgeSection] = []
    current_id: str | None = None
    current_title = ""
    body: list[str] = []

    def _flush() -> None:
        if current_id is None:
            return
        text = _normalize_body("\n".join(body))
        topic = current_id.split("-")[1]
        sections.append(
            KnowledgeSection(
                section_id=current_id,
                topic=topic,
                title=current_title,
                body=text,
                source=source,
            )
        )

    for raw_line in playbook.replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        match = _SECTION_HEADING.match(line)
        if match is not None:
            _flush()
            current_id = match.group(1)
            current_title = _normalize_heading(match.group(2))
            body = []
            continue
        if current_id is not None:
            body.append(line)
    _flush()
    return tuple(sections)


_MANIFEST_REQUIRED = (
    "artifact_id",
    "compiler_version",
    "source_skills",
    "created_at",
    "license_note",
    "reviewed_by",
    "token_count_method",
    "token_count",
    "playbook_token_budget",
)


def validate_manifest(data: Mapping[str, Any]) -> KnowledgeManifest:
    """Validate the private manifest; a missing/invalid hash is never guessed."""
    if not isinstance(data, Mapping):
        raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "manifest must be an object")
    missing = [field for field in _MANIFEST_REQUIRED if field not in data]
    if missing:
        raise KnowledgeError(
            KNOWLEDGE_MANIFEST_INVALID,
            f"manifest is missing required field(s): {', '.join(missing)}",
        )
    artifact_id = data.get("artifact_id")
    if not isinstance(artifact_id, str) or _ARTIFACT_HASH_PATTERN.fullmatch(artifact_id) is None:
        raise KnowledgeError(
            KNOWLEDGE_MANIFEST_INVALID, "manifest artifact_id must be a sha256 hex digest"
        )
    compiler_version = data.get("compiler_version")
    if not isinstance(compiler_version, str) or not compiler_version.strip():
        raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "manifest compiler_version is required")
    created_at = data.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "manifest created_at is required")
    license_note = data.get("license_note")
    if not isinstance(license_note, str) or "private" not in license_note.lower():
        raise KnowledgeError(
            KNOWLEDGE_MANIFEST_INVALID,
            "manifest license_note must state that the artifact is private",
        )
    reviewed_by = data.get("reviewed_by")
    if reviewed_by is not None and not isinstance(reviewed_by, str):
        raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "reviewed_by must be a string or null")
    method = data.get("token_count_method")
    if method != TOKEN_COUNT_METHOD:
        raise KnowledgeError(
            KNOWLEDGE_MANIFEST_INVALID,
            f"unsupported token_count_method {method!r}; expected {TOKEN_COUNT_METHOD}",
        )
    token_count = data.get("token_count")
    if not isinstance(token_count, int) or token_count < 0:
        raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "token_count must be a non-negative int")
    budget = data.get("playbook_token_budget")
    if not isinstance(budget, int) or budget < 1:
        raise KnowledgeError(
            KNOWLEDGE_MANIFEST_INVALID, "playbook_token_budget must be a positive int"
        )
    if token_count > budget:
        raise KnowledgeError(
            KNOWLEDGE_MANIFEST_INVALID,
            f"token_count {token_count} exceeds the playbook budget {budget}",
        )
    source_skills = data.get("source_skills")
    if not isinstance(source_skills, list) or not source_skills:
        raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "source_skills must be a non-empty list")
    parsed_sources: list[dict[str, str]] = []
    for item in source_skills:
        if not isinstance(item, Mapping):
            raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "each source_skill must be an object")
        slug = item.get("slug")
        content_hash = item.get("content_hash")
        if not isinstance(slug, str) or not isinstance(content_hash, str):
            raise KnowledgeError(
                KNOWLEDGE_MANIFEST_INVALID,
                "each source_skill needs slug and content_hash strings",
            )
        parsed_sources.append({"slug": slug, "content_hash": content_hash})
    review_status = data.get("review_status", REVIEW_DRAFT)
    if review_status not in {REVIEW_DRAFT, REVIEW_APPROVED}:
        raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "review_status must be DRAFT or APPROVED")
    reviewed_at = data.get("reviewed_at")
    if reviewed_at is not None and not isinstance(reviewed_at, str):
        raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "reviewed_at must be a string or null")
    summarized = data.get("summarized", False)
    if not isinstance(summarized, bool):
        raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "summarized must be a bool")
    dropped_raw = data.get("dropped", [])
    if not isinstance(dropped_raw, list):
        raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "dropped must be a list")
    dropped: list[dict[str, Any]] = []
    for item in dropped_raw:
        if not isinstance(item, Mapping):
            raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "each dropped item must be an object")
        dropped.append(dict(item))
    sections_raw = data.get("sections", [])
    if not isinstance(sections_raw, list) or any(
        not isinstance(item, str) for item in sections_raw
    ):
        raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "sections must be a list of strings")
    topics_raw = data.get("topics", [])
    if not isinstance(topics_raw, list) or any(not isinstance(item, str) for item in topics_raw):
        raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "topics must be a list of strings")
    return KnowledgeManifest(
        artifact_id=artifact_id,
        compiler_version=compiler_version,
        source_skills=tuple(parsed_sources),
        created_at=created_at,
        license_note=license_note,
        reviewed_by=reviewed_by,
        token_count_method=str(method),
        token_count=token_count,
        playbook_token_budget=budget,
        review_status=str(review_status),
        reviewed_at=reviewed_at,
        summarized=summarized,
        dropped=tuple(dropped),
        sections=tuple(sections_raw),
        topics=tuple(topics_raw),
    )


def verify_artifact(
    manifest: KnowledgeManifest,
    playbook: str,
    topics: Mapping[str, Any],
    rules: Mapping[str, Any],
) -> None:
    """Fail loudly when on-disk content no longer matches the manifest hash."""
    actual = artifact_hash(playbook, topics, rules)
    if actual != manifest.artifact_id:
        raise KnowledgeError(
            KNOWLEDGE_ARTIFACT_MISMATCH,
            "the artifact content does not match the manifest artifact_id",
        )


def select_topics_for_context(
    topics_document: Mapping[str, Any],
    calc_context: Mapping[str, Any],
    *,
    maximum: int = 5,
) -> tuple[str, ...]:
    """Mode B: deterministic CALC-context -> topic mapping from topics.json."""
    mapping = topics_document.get("calc_context")
    known_topics = topics_document.get("topics")
    if not isinstance(mapping, Mapping) or not mapping:
        raise KnowledgeModeUnavailable("Mode B requires a calc_context mapping in the topic index")
    selected: list[str] = []
    for field in sorted(str(key) for key in mapping):
        values = mapping.get(field)
        if not isinstance(values, Mapping):
            continue
        wanted = calc_context.get(field)
        if not isinstance(wanted, str):
            continue
        names = values.get(wanted)
        if not isinstance(names, list):
            continue
        for name in names:
            if (
                isinstance(name, str)
                and isinstance(known_topics, Mapping)
                and name in known_topics
                and name not in selected
            ):
                selected.append(name)
            if len(selected) >= maximum:
                return tuple(selected)
    if not selected:
        raise KnowledgeModeUnavailable("Mode B found no topics for the supplied CALC context")
    return tuple(selected)


def sections_for_topics(
    topics_document: Mapping[str, Any], topic_names: Sequence[str]
) -> tuple[str, ...]:
    topics = topics_document.get("topics")
    if not isinstance(topics, Mapping):
        return ()
    section_ids: list[str] = []
    for name in topic_names:
        entry = topics.get(name)
        if not isinstance(entry, Mapping):
            continue
        ids = entry.get("section_ids")
        if not isinstance(ids, list):
            continue
        for item in ids:
            if isinstance(item, str) and item not in section_ids:
                section_ids.append(item)
    return tuple(section_ids)


def absent_state(reason: str = KNOWLEDGE_ABSENT) -> KnowledgeState:
    return KnowledgeState(
        status="ABSENT",
        reason=reason,
        artifact_id=None,
        exact_replay=False,
        active=False,
    )


def available_state(manifest: KnowledgeManifest, *, active: bool) -> KnowledgeState:
    return KnowledgeState(
        status="AVAILABLE",
        reason=None,
        artifact_id=manifest.artifact_id,
        exact_replay=True,
        active=active,
        reviewed_by=manifest.reviewed_by,
        review_status=manifest.review_status,
        token_count=manifest.token_count,
        license_note=manifest.license_note,
        private=True,
    )


__all__ = [
    "CHARS_PER_TOKEN",
    "COMPILER_VERSION",
    "DEFAULT_TAXONOMY",
    "GLOSSARY_TOKEN_BUDGET",
    "INJECTION_TOKEN_BUDGET",
    "KNOWLEDGE_ABSENT",
    "KNOWLEDGE_ARTIFACT_MISMATCH",
    "KNOWLEDGE_BUDGET_EXCEEDED",
    "KNOWLEDGE_MANIFEST_INVALID",
    "KNOWLEDGE_MODE_UNAVAILABLE",
    "KNOWLEDGE_NOT_ACTIVE",
    "KNOWLEDGE_NOT_REVIEWED",
    "KNOWLEDGE_NO_SECTIONS",
    "KNOWLEDGE_SOURCE_EMPTY",
    "LICENSE_NOTE",
    "PLAYBOOK_TOKEN_BUDGET",
    "PRIVATE_WARNING",
    "REVIEW_APPROVED",
    "REVIEW_DRAFT",
    "RULES_TOKEN_BUDGET",
    "SECTION_ID_PATTERN",
    "TOKEN_COUNT_METHOD",
    "BudgetResult",
    "CompiledArtifact",
    "DroppedContent",
    "KnowledgeError",
    "KnowledgeManifest",
    "KnowledgeModeUnavailable",
    "KnowledgeSection",
    "KnowledgeState",
    "RawSection",
    "TopicDefinition",
    "absent_state",
    "artifact_hash",
    "available_state",
    "build_rules_document",
    "build_topics_document",
    "compile_artifact",
    "count_tokens",
    "fit_sections_to_budget",
    "load_sections",
    "parse_markdown",
    "render_playbook",
    "render_section",
    "sections_for_topics",
    "select_topics_for_context",
    "summarize_text",
    "topic_definitions",
    "validate_manifest",
    "verify_artifact",
]
