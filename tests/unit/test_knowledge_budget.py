"""Token accounting and budget enforcement for compiled knowledge."""

from __future__ import annotations

import pytest

from oft.domain.knowledge import (
    PLAYBOOK_TOKEN_BUDGET,
    compile_artifact,
    count_tokens,
    summarize_text,
    topic_definitions,
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


def test_summarize_text_does_not_increase_the_text() -> None:
    text = "Alpha sentence. Beta sentence. Gamma sentence. Delta sentence."

    summary = summarize_text(text, 3)

    assert len(summary) <= len(text)
    assert summary.endswith("[summarized]")


def test_summarize_text_returns_short_text_unchanged() -> None:
    assert summarize_text("short", 100) == "short"


def test_empty_corpus_is_rejected() -> None:
    from oft.domain.knowledge import KnowledgeError

    with pytest.raises(KnowledgeError):
        compile_artifact([("empty", "")], generated_at=GENERATED_AT)
