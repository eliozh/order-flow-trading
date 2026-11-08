"""Token accounting and budget enforcement for compiled knowledge."""

from __future__ import annotations

import pytest

from oft.domain.knowledge import (
    PLAYBOOK_TOKEN_BUDGET,
    KnowledgeSection,
    compile_artifact,
    count_tokens,
    fit_sections_to_budget,
    summarize_text,
    topic_definitions,
    validate_manifest,
)

GENERATED_AT = "2026-10-08T00:00:00.000Z"
TAXONOMY = (
    ("market_structure", "Market structure", ("market structure",)),
    ("delta", "Delta", ("delta",)),
)


def _corpus() -> list[tuple[str, str]]:
    long_body = " ".join(["delta absorption structure evidence"] * 120)
    return [
        (
            "skill",
            (f"## Market structure context\n\n{long_body}\n\n## Delta context\n\n{long_body}\n"),
        )
    ]


def test_token_count_uses_the_documented_chars_div_4_method() -> None:
    assert count_tokens("") == 0
    assert count_tokens("abcd") == 1
    assert count_tokens("abcde") == 2
    # The method is an approximation: four characters per token.
    assert count_tokens("a" * 40) == 10


def test_a_corpus_within_budget_is_compiled_without_dropping() -> None:
    artifact = compile_artifact(
        _corpus(),
        generated_at=GENERATED_AT,
        taxonomy=topic_definitions(
            {
                "topics": [
                    {"name": name, "title": title, "keywords": list(keywords)}
                    for name, title, keywords in TAXONOMY
                ]
            }
        ),
        budget=10_000,
    )

    assert not artifact.manifest.dropped
    assert artifact.manifest.summarized is False
    assert artifact.manifest.token_count <= 10_000


def test_an_over_budget_corpus_is_selected_or_summarized_never_concatenated() -> None:
    budget = 120
    artifact = compile_artifact(
        _corpus(),
        generated_at=GENERATED_AT,
        taxonomy=topic_definitions(
            {
                "topics": [
                    {"name": name, "title": title, "keywords": list(keywords)}
                    for name, title, keywords in TAXONOMY
                ]
            }
        ),
        budget=budget,
    )

    assert artifact.manifest.token_count <= budget
    assert artifact.manifest.token_count == count_tokens(artifact.playbook)
    assert artifact.manifest.summarized is True
    assert artifact.manifest.dropped
    reasons = {item["reason"] for item in artifact.manifest.dropped}
    assert reasons & {"summarized", "budget"}
    # A dropped or summarized section is visible in the manifest, not hidden.
    assert all(item["tokens"] > 0 for item in artifact.manifest.dropped)


def test_a_budget_that_cannot_fit_every_section_drops_the_lowest_priority() -> None:
    budget = 30
    artifact = compile_artifact(
        _corpus(),
        generated_at=GENERATED_AT,
        taxonomy=topic_definitions(
            {
                "topics": [
                    {"name": name, "title": title, "keywords": list(keywords)}
                    for name, title, keywords in TAXONOMY
                ]
            }
        ),
        budget=budget,
    )

    assert artifact.manifest.token_count <= budget
    assert len(artifact.sections) >= 1
    assert any(item["reason"] == "budget" for item in artifact.manifest.dropped)


def test_default_budget_matches_the_adr_limit() -> None:
    assert PLAYBOOK_TOKEN_BUDGET == 10_000


def test_summarize_text_keeps_the_prefix_and_marks_the_cut() -> None:
    text = "First sentence. Second sentence. Third sentence."

    summary = summarize_text(text, 10)

    assert len(summary) < len(text)
    assert summary.startswith("First")
    assert "[summarized]" in summary


def test_summarize_text_reserves_space_for_the_marker() -> None:
    text = "Alpha sentence. Beta sentence. Gamma sentence. Delta sentence."

    for limit in (5, 6, 8, 12):
        summary = summarize_text(text, limit)
        assert count_tokens(summary) <= limit


def test_summarize_text_returns_the_original_when_no_shorter_summary_fits() -> None:
    text = "Alpha sentence. Beta sentence."

    # The marker alone costs four tokens, so a three-token budget cannot
    # produce a shorter marked summary and the original is returned instead.
    assert summarize_text(text, 3) == text


def test_summarize_text_returns_short_text_unchanged() -> None:
    assert summarize_text("short", 100) == "short"


def test_empty_corpus_is_rejected() -> None:
    from oft.domain.knowledge import KnowledgeError

    with pytest.raises(KnowledgeError):
        compile_artifact([("empty", "")], generated_at=GENERATED_AT)


def test_a_single_section_that_cannot_fit_is_dropped_never_oversized() -> None:
    # After the summarize fallback the section (18 tokens) still cannot fit a
    # 10-token playbook; it must be dropped rather than emitted oversized.
    sections = (KnowledgeSection("K-delta-1", "delta", "Delta context", "delta " * 400, "s"),)

    result = fit_sections_to_budget(sections, budget=10)

    assert result.token_count <= 10
    assert result.sections == ()
    assert any(item.reason == "budget" for item in result.dropped)


def test_a_tiny_budget_still_produces_a_valid_reloadable_manifest() -> None:
    artifact = compile_artifact(
        [("doc", "## Delta context\n\n" + "delta " * 400 + "\n")],
        generated_at=GENERATED_AT,
        taxonomy=topic_definitions(
            {
                "topics": [
                    {"name": name, "title": title, "keywords": list(keywords)}
                    for name, title, keywords in TAXONOMY
                ]
            }
        ),
        budget=10,
    )

    assert artifact.manifest.token_count <= 10
    assert artifact.manifest.token_count == count_tokens(artifact.playbook)
    # The manifest validates on reload: token_count never exceeds the budget.
    reloaded = validate_manifest(artifact.manifest.to_dict())
    assert reloaded.token_count == artifact.manifest.token_count


def test_a_budget_smaller_than_the_playbook_header_is_rejected() -> None:
    from oft.domain.knowledge import KnowledgeError

    with pytest.raises(KnowledgeError) as error:
        compile_artifact(
            [("doc", "## Delta context\n\nBody.\n")],
            generated_at=GENERATED_AT,
            taxonomy=topic_definitions(
                {
                    "topics": [
                        {"name": name, "title": title, "keywords": list(keywords)}
                        for name, title, keywords in TAXONOMY
                    ]
                }
            ),
            budget=1,
        )

    assert error.value.code == "KNOWLEDGE_BUDGET_EXCEEDED"
