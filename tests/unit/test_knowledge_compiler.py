"""Compiling a synthetic skill corpus into a reviewed playbook artifact."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oft.domain.knowledge import (
    COMPILER_VERSION,
    DEFAULT_TAXONOMY,
    LICENSE_NOTE,
    PLAYBOOK_TOKEN_BUDGET,
    SECTION_ID_PATTERN,
    TOKEN_COUNT_METHOD,
    KnowledgeError,
    KnowledgeManifest,
    KnowledgeModeUnavailable,
    KnowledgeSection,
    absent_state,
    artifact_hash,
    available_state,
    build_rules_document,
    compile_artifact,
    count_tokens,
    fit_sections_to_budget,
    load_sections,
    parse_markdown,
    render_playbook,
    sections_for_topics,
    select_topics_for_context,
    summarize_text,
    topic_definitions,
    validate_manifest,
    verify_artifact,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge"
GENERATED_AT = "2026-10-08T00:00:00.000Z"


def _topic_config() -> dict[str, object]:
    return json.loads((FIXTURES / "topics.json").read_text(encoding="utf-8"))


def _rules_config() -> dict[str, object]:
    return json.loads((FIXTURES / "rules.json").read_text(encoding="utf-8"))


def _compile() -> object:
    config = _topic_config()
    taxonomy = topic_definitions(config)
    calc_context = config.get("calc_context")
    return compile_artifact(
        [
            (
                "synthetic_playbook",
                (FIXTURES / "synthetic_playbook.md").read_text(encoding="utf-8"),
            )
        ],
        generated_at=GENERATED_AT,
        taxonomy=taxonomy,
        rules_config=_rules_config(),
        calc_context=calc_context if isinstance(calc_context, dict) else None,
    )


def test_compile_produces_stable_section_ids_and_a_complete_manifest() -> None:
    artifact = _compile()

    assert artifact.manifest.compiler_version == COMPILER_VERSION
    assert artifact.manifest.created_at == GENERATED_AT
    assert artifact.manifest.license_note == LICENSE_NOTE
    assert artifact.manifest.reviewed_by is None
    assert artifact.manifest.review_status == "DRAFT"
    assert artifact.manifest.token_count_method == TOKEN_COUNT_METHOD
    # Documented chars/4 approximation, recorded in the manifest.
    assert artifact.manifest.token_count == count_tokens(artifact.playbook)
    assert artifact.manifest.token_count <= PLAYBOOK_TOKEN_BUDGET
    assert artifact.manifest.playbook_token_budget == PLAYBOOK_TOKEN_BUDGET
    assert artifact.manifest.source_skills[0]["slug"] == "synthetic_playbook"
    assert len(artifact.manifest.source_skills[0]["content_hash"]) == 64

    section_ids = [section.section_id for section in artifact.sections]
    assert section_ids
    assert all(SECTION_ID_PATTERN.fullmatch(section_id) for section_id in section_ids)
    assert "K-market_structure-1" in section_ids
    assert "K-delta-1" in section_ids
    assert "## K-market_structure-1" in artifact.playbook


def test_compiling_the_same_source_twice_is_deterministic() -> None:
    first = _compile()
    second = _compile()

    assert first.artifact_id == second.artifact_id
    assert first.playbook == second.playbook
    assert first.topics == second.topics
    assert first.rules == second.rules
    assert artifact_hash(first.playbook, first.topics, first.rules) == first.artifact_id


def test_out_of_taxonomy_sections_are_dropped_and_recorded() -> None:
    artifact = _compile()

    dropped_titles = {item["title"] for item in artifact.manifest.dropped}
    assert "Cooking recipes" in dropped_titles
    cooking = next(item for item in artifact.manifest.dropped if item["title"] == "Cooking recipes")
    assert cooking["reason"] == "out_of_taxonomy"
    assert cooking["tokens"] > 0


def test_rules_resolve_to_compiled_section_ids() -> None:
    artifact = _compile()

    analysis = artifact.rules["roles"]["ANALYSIS"]
    assert analysis["rules"]
    assert analysis["section_ids"]
    assert set(analysis["section_ids"]).issubset(
        {section.section_id for section in artifact.sections}
    )


def test_topics_include_topic_sections_and_deterministic_calc_mapping() -> None:
    artifact = _compile()

    topics = artifact.topics["topics"]
    assert topics["market_structure"]["section_ids"] == ["K-market_structure-1"]
    assert artifact.topics["token_count_method"] == TOKEN_COUNT_METHOD
    assert artifact.topics["calc_context"]["regime"]["TRENDING_UP"] == [
        "market_structure",
        "delta",
    ]


def test_manifest_round_trips_and_validates() -> None:
    artifact = _compile()

    payload = json.loads(json.dumps(artifact.manifest.to_dict()))
    manifest = validate_manifest(payload)

    assert manifest.artifact_id == artifact.artifact_id
    assert manifest.reviewed_by is None
    assert manifest.review_status == "DRAFT"
    assert len(manifest.artifact_id) == 64


def test_manifest_validation_rejects_missing_and_invalid_fields() -> None:
    artifact = _compile()
    payload = artifact.manifest.to_dict()

    with pytest.raises(KnowledgeError):
        validate_manifest({"artifact_id": payload["artifact_id"]})

    missing_hash = dict(payload)
    missing_hash.pop("artifact_id")
    with pytest.raises(KnowledgeError):
        validate_manifest(missing_hash)

    bad_hash = dict(payload, artifact_id="not-a-hash")
    with pytest.raises(KnowledgeError):
        validate_manifest(bad_hash)

    bad_license = dict(payload, license_note="distribute freely")
    with pytest.raises(KnowledgeError):
        validate_manifest(bad_license)

    bad_method = dict(payload, token_count_method="magic")
    with pytest.raises(KnowledgeError):
        validate_manifest(bad_method)

    too_big = dict(payload, token_count=PLAYBOOK_TOKEN_BUDGET + 1)
    with pytest.raises(KnowledgeError):
        validate_manifest(too_big)


def test_loading_sections_back_from_the_playbook_preserves_ids_and_bodies() -> None:
    artifact = _compile()

    sections = load_sections(artifact.playbook)

    assert [section.section_id for section in sections] == [
        section.section_id for section in artifact.sections
    ]
    assert sections[0].body == artifact.sections[0].body
    assert load_sections("# nothing here\n") == ()


def test_empty_or_irrelevant_source_is_rejected_instead_of_fabricated() -> None:
    with pytest.raises(KnowledgeError):
        compile_artifact([], generated_at=GENERATED_AT)
    with pytest.raises(KnowledgeError):
        compile_artifact(
            [("junk", "## Cooking recipes\n\nOnly soup.\n")],
            generated_at=GENERATED_AT,
        )


def test_parse_markdown_normalizes_line_endings_and_whitespace() -> None:
    sections = parse_markdown(
        "# Title\r\n\r\n## Market structure   \r\n\r\nBody   line.  \r\n\r\n",
        source="doc",
    )

    assert len(sections) == 1
    assert sections[0].title == "Market structure"
    assert sections[0].body == "Body   line."


def test_summarize_text_keeps_the_prefix_and_marks_the_cut() -> None:
    text = "First sentence. Second sentence. Third sentence."

    summary = summarize_text(text, 4)

    assert len(summary) < len(text)
    assert summary.startswith("First")
    assert "[summarized]" in summary


def test_state_is_explicit_about_a_missing_artifact() -> None:
    artifact = _compile()

    available = available_state(artifact.manifest, active=True)
    assert available.status == "AVAILABLE"
    assert available.artifact_id == artifact.artifact_id
    assert available.exact_replay is True
    assert available.private is True

    inactive = available_state(artifact.manifest, active=False)
    assert inactive.active is False

    absent = absent_state()
    assert absent.status == "ABSENT"
    assert absent.artifact_id is None
    assert absent.exact_replay is False
    assert absent.reason == "KNOWLEDGE_ABSENT"


def test_manifest_from_dict_delegates_to_validation() -> None:
    artifact = _compile()

    manifest = KnowledgeManifest.from_dict(artifact.manifest.to_dict())

    assert manifest == artifact.manifest


def test_parse_markdown_skips_a_document_title_and_empty_sections() -> None:
    assert parse_markdown("# Only a title\n\nbody\n") == ()
    sections = parse_markdown("## Empty\n\n## Filled\n\nbody\n")
    assert [section.title for section in sections] == ["Filled"]


def test_topic_definitions_fall_back_and_skip_invalid_entries() -> None:
    assert topic_definitions(None) == DEFAULT_TAXONOMY
    assert topic_definitions({"topics": "not-a-list"}) == DEFAULT_TAXONOMY
    definitions = topic_definitions(
        {
            "topics": [
                1,
                {"name": "BAD NAME", "title": "x"},
                {"name": "blank", "title": "   "},
                {"name": "valid", "title": "Valid", "keywords": "not-a-list"},
            ]
        }
    )
    assert [definition.name for definition in definitions] == ["valid"]
    assert definitions[0].keywords == ("valid",)


def test_a_section_can_be_classified_from_its_body_when_the_title_is_neutral() -> None:
    artifact = compile_artifact(
        [("neutral", "## Neutral heading\n\nThis text mentions delta explicitly.\n")],
        generated_at=GENERATED_AT,
        taxonomy=topic_definitions(_topic_config()),
    )

    assert artifact.sections[0].topic == "delta"


def test_render_playbook_with_no_sections_is_a_header_only() -> None:
    assert render_playbook(()) == "# Order-flow playbook\n\n"


def test_fit_sections_keeps_short_sections_while_summarizing_long_ones() -> None:
    long_body = " ".join(["delta absorption evidence"] * 100)
    sections = (
        KnowledgeSection("K-a-1", "a", "Short", "tiny body", "s"),
        KnowledgeSection("K-b-1", "b", "Long", long_body, "s"),
    )

    result = fit_sections_to_budget(sections, budget=80)

    assert result.summarized is True
    assert result.token_count <= 80
    assert result.sections[0].body == "tiny body"
    assert result.sections[1].body != long_body


def test_unsafe_calc_context_values_are_nulled_not_leaked() -> None:
    artifact = compile_artifact(
        [("doc", "## Delta context\n\nBody.\n")],
        generated_at=GENERATED_AT,
        taxonomy=topic_definitions(_topic_config()),
        calc_context={"ok": "value", "bad": 5},
    )

    assert artifact.topics["calc_context"]["ok"] == "value"
    assert artifact.topics["calc_context"]["bad"] is None


def test_build_rules_document_skips_unknown_topics_and_malformed_roles() -> None:
    topics = {
        "topics": {
            "a": {"section_ids": ["K-a-1"]},
            "b": {"section_ids": "not-a-list"},
        }
    }
    document = build_rules_document(
        {
            "roles": {
                "ANALYSIS": {"topics": ["a", "b", "missing"], "rules": ["rule", 1]},
                "BROKEN": "not-a-mapping",
                "NO_RULES": {"topics": [], "rules": "not-a-list"},
            }
        },
        topics,
    )

    assert document["roles"]["ANALYSIS"] == {
        "section_ids": ["K-a-1"],
        "rules": ["rule"],
    }
    assert document["roles"]["NO_RULES"] == {"section_ids": [], "rules": []}
    assert "BROKEN" not in document["roles"]
    assert build_rules_document(None, topics) == {"version": 1, "roles": {}}


def test_compile_rejects_a_non_positive_budget() -> None:
    with pytest.raises(KnowledgeError):
        compile_artifact(
            [("doc", "## Delta context\n\nBody.\n")],
            generated_at=GENERATED_AT,
            budget=0,
        )


def test_verify_artifact_rejects_a_tampered_playbook() -> None:
    artifact = _compile()

    verify_artifact(artifact.manifest, artifact.playbook, artifact.topics, artifact.rules)
    with pytest.raises(KnowledgeError) as error:
        verify_artifact(
            artifact.manifest, artifact.playbook + "tampered", artifact.topics, artifact.rules
        )
    assert error.value.code == "KNOWLEDGE_ARTIFACT_MISMATCH"


def test_select_topics_is_deterministic_bounded_and_rejects_bad_input() -> None:
    topics_document = {
        "topics": {
            name: {"section_ids": [f"K-{name}-1"]} for name in ("a", "b", "c", "d", "e", "f")
        },
        "calc_context": {
            "f": {"UP": ["a", "missing", "a", "b", "c", "d", "e", "f"]},
            "g": "not-a-mapping",
            "h": {"UP": "not-a-list"},
        },
    }

    selected = select_topics_for_context(
        topics_document, {"f": "UP", "g": "X", "h": "UP"}, maximum=5
    )
    assert selected == ("a", "b", "c", "d", "e")

    with pytest.raises(KnowledgeModeUnavailable):
        select_topics_for_context({}, {})
    with pytest.raises(KnowledgeModeUnavailable):
        select_topics_for_context(topics_document, {"f": "DOWN"})


def test_sections_for_topics_handles_missing_and_malformed_entries() -> None:
    assert sections_for_topics({"topics": "x"}, ["a"]) == ()
    assert sections_for_topics({"topics": {"a": {"section_ids": "x"}}}, ["a"]) == ()
    assert sections_for_topics(
        {"topics": {"a": {"section_ids": ["K-a-1", "K-a-1"]}}}, ["a", "missing"]
    ) == ("K-a-1",)


def test_manifest_validation_rejects_each_invalid_field() -> None:
    payload = _compile().manifest.to_dict()
    invalid_payloads = [
        [1],
        dict(payload, artifact_id=123),
        dict(payload, compiler_version=""),
        dict(payload, created_at=""),
        dict(payload, reviewed_by=5),
        dict(payload, token_count="x"),
        dict(payload, playbook_token_budget=0),
        dict(payload, source_skills=[]),
        dict(payload, source_skills=["x"]),
        dict(payload, source_skills=[{"slug": 1, "content_hash": "x"}]),
        dict(payload, review_status="UNKNOWN"),
        dict(payload, reviewed_at=5),
        dict(payload, summarized="yes"),
        dict(payload, dropped="x"),
        dict(payload, dropped=[1]),
        dict(payload, sections=[1]),
        dict(payload, topics=[1]),
    ]

    for invalid in invalid_payloads:
        with pytest.raises(KnowledgeError):
            validate_manifest(invalid)


def test_the_fixture_manifest_is_a_valid_private_manifest() -> None:
    payload = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))

    manifest = validate_manifest(payload)

    assert manifest.license_note == LICENSE_NOTE
    assert "private" in manifest.license_note.lower()
    assert len(manifest.artifact_id) == 64


def test_build_rules_document_without_a_topic_index_keeps_rules_only() -> None:
    document = build_rules_document(
        {"roles": {"ANALYSIS": {"topics": ["delta"], "rules": ["keep me"]}}}, {}
    )

    assert document["roles"]["ANALYSIS"] == {"section_ids": [], "rules": ["keep me"]}
