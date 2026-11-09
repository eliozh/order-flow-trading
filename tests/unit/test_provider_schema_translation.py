"""Provider-specific canonical schema translation and error redaction (D3)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from oft.adapters.llm_common import LLMSchemaUnsupported, redact, translate_schema
from oft.application.llm_validation import canonical_schema

PROVIDERS = ("openai", "anthropic", "deepseek")
_CONSTRAINT_KEYWORDS = {
    "pattern",
    "format",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "uniqueItems",
    "default",
}


def _property_names(node: Any, names: set[str]) -> None:
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for value in node.values():
            _property_names(value, names)
    elif isinstance(node, list):
        for item in node:
            _property_names(item, names)


def _additional_property_values(node: Any, found: list[Any]) -> None:
    if isinstance(node, dict):
        if "additionalProperties" in node:
            found.append(node["additionalProperties"])
        for value in node.values():
            _additional_property_values(value, found)
    elif isinstance(node, list):
        for item in node:
            _additional_property_values(item, found)


def _keywords(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        found.update(node)
        for value in node.values():
            _keywords(value, found)
    elif isinstance(node, list):
        for item in node:
            _keywords(item, found)


def test_translation_never_adds_or_removes_a_field() -> None:
    canonical = canonical_schema()
    canonical_names: set[str] = set()
    _property_names(canonical, canonical_names)

    for provider in PROVIDERS:
        translated = translate_schema(canonical, provider)
        names: set[str] = set()
        _property_names(translated, names)
        assert names == canonical_names, provider


def test_translation_never_relaxes_additional_properties() -> None:
    canonical = canonical_schema()
    canonical_values: list[Any] = []
    _additional_property_values(canonical, canonical_values)
    assert canonical_values and set(canonical_values) == {False}

    for provider in PROVIDERS:
        translated = translate_schema(canonical, provider)
        values: list[Any] = []
        _additional_property_values(translated, values)
        assert set(values) == {False}, provider


def test_translation_never_mutates_the_cached_canonical_schema() -> None:
    before = copy.deepcopy(canonical_schema())

    for provider in PROVIDERS:
        translate_schema(canonical_schema(), provider)

    assert canonical_schema() == before


def test_openai_strips_only_unsupported_constraint_keywords() -> None:
    translated = translate_schema(canonical_schema(), "openai")

    keywords: set[str] = set()
    _keywords(translated, keywords)
    assert not (_CONSTRAINT_KEYWORDS & keywords)
    assert {"type", "properties", "required", "additionalProperties", "items", "enum"} <= (keywords)
    assert "$defs" in keywords and "$ref" in keywords
    assert "anyOf" in keywords


def test_anthropic_keeps_enums_and_keeps_refs_inside_document_limits() -> None:
    translated = translate_schema(canonical_schema(), "anthropic")

    keywords: set[str] = set()
    _keywords(translated, keywords)
    assert not (_CONSTRAINT_KEYWORDS & keywords)
    assert "enum" in keywords
    assert "$defs" in keywords and "$ref" in keywords


def test_deepseek_inlines_refs_and_drops_defs() -> None:
    translated = translate_schema(canonical_schema(), "deepseek")

    keywords: set[str] = set()
    _keywords(translated, keywords)
    assert "$defs" not in keywords
    assert "$ref" not in keywords
    assert not (_CONSTRAINT_KEYWORDS & keywords)
    assert {"type", "properties", "required", "additionalProperties", "enum"} <= keywords


def test_deepseek_folds_scalar_nullable_any_of_into_a_type_array() -> None:
    translated = translate_schema(canonical_schema(), "deepseek")
    trigger = translated["properties"]["opportunity"]["anyOf"][0]
    params = trigger["properties"]["trigger_conditions"]["items"]["properties"]["params"]
    price = params["properties"]["price"]

    assert price["type"] == ["string", "null"]
    assert "anyOf" not in price


def test_an_unknown_provider_is_rejected() -> None:
    canonical = canonical_schema()

    with pytest.raises(ValueError):
        translate_schema(canonical, "other")


def test_required_lists_are_preserved() -> None:
    canonical = canonical_schema()
    translated = translate_schema(canonical, "deepseek")

    assert translated["required"] == canonical["required"]
    subject = translated["properties"]["subject"]
    assert subject["required"] == ["symbol", "timeframe", "as_of"]


def test_redact_replaces_configured_secrets_and_key_shapes() -> None:
    sentinel = "sk-SENTINEL-DO-NOT-LEAK"
    text = (
        f"provider said {sentinel} and Authorization: Bearer abc.def-123 and an sk-live-other-token"
    )

    cleaned = redact(text, [sentinel])

    assert sentinel not in cleaned
    assert "abc.def-123" not in cleaned
    assert "sk-live-other-token" not in cleaned
    assert cleaned.count("[REDACTED]") >= 3


def test_redact_ignores_empty_secrets_and_leaves_ordinary_text() -> None:
    assert redact("ordinary text", ["", "   "]) == "ordinary text"


@pytest.mark.parametrize(
    "schema",
    [
        {"anyOf": [{"type": "string"}, {"type": "null"}, {"type": "integer"}]},
        {"anyOf": [{"type": "string"}, {"type": "null", "description": "d"}]},
        {"type": "object", "properties": {"p": {"type": "string"}}, "required": ["p"]},
        {"properties": "not-a-dict"},
        {"items": [{"type": "string"}]},
        {"anyOf": ["string", {"type": "null"}]},
        {"type": "object", "unknown_keyword": 1},
    ],
)
def test_deepseek_translation_handles_awkward_shapes(schema: dict[str, Any]) -> None:
    translated = translate_schema(schema, "deepseek")

    assert isinstance(translated, dict)
    # Field names survive even when a pointer cannot be resolved.
    for key in ("properties", "required"):
        if key in schema and isinstance(schema[key], dict):
            assert translated.get(key) is not None


@pytest.mark.parametrize(
    "schema",
    [
        {"anyOf": [{"$ref": "#/other/x"}, {"type": "null"}]},
        {"anyOf": [{"$ref": "#/$defs/missing"}, {"type": "null"}]},
        {"type": "object", "properties": {"p": {"$ref": "#/$defs/missing"}}},
    ],
)
def test_deepseek_translation_rejects_an_unresolvable_ref(schema: dict[str, Any]) -> None:
    # Silently dropping the ref weakens strictness; fail instead (M23).
    with pytest.raises(LLMSchemaUnsupported):
        translate_schema(schema, "deepseek")


def test_unknown_keywords_are_dropped_for_openai_and_anthropic() -> None:
    schema = {"type": "object", "unknown_keyword": 1, "x-extra": {"nested": True}}

    for provider in ("openai", "anthropic"):
        assert translate_schema(schema, provider) == {"type": "object"}
