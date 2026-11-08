"""Required-variable validation and secret redaction at the prompt boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.prompt_service import PromptService, secret_values_from_settings
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.prompts import (
    DEFAULT_SYSTEM_BODIES,
    DEFAULT_USER_BODIES,
    PromptRole,
    PromptValidationError,
    prompt_id_for_role,
    provenance_status,
    redact_secrets,
    render,
    rendered_prompt_hash,
    system_prompt_id_for_role,
    validate_body,
)

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)

MISSING_CASES = (
    (PromptRole.ANALYSIS, "symbol"),
    (PromptRole.ANALYSIS, "timeframe"),
    (PromptRole.ANALYSIS, "metrics"),
    (PromptRole.ANALYSIS, "schema"),
    (PromptRole.ANALYSIS, "knowledge"),
    (PromptRole.ENTRY_RECHECK, "trigger"),
    (PromptRole.MANAGEMENT, "position"),
    (PromptRole.REVIEW, "position"),
)


@pytest.mark.parametrize(("role", "variable"), MISSING_CASES)
def test_missing_required_variable_is_named(role: PromptRole, variable: str) -> None:
    body = DEFAULT_USER_BODIES[role].replace("{{" + variable + "}}", "")

    with pytest.raises(PromptValidationError) as excinfo:
        validate_body(body, role=role)

    assert excinfo.value.code == "PROMPT_VALIDATION_FAILED"
    assert excinfo.value.variable == variable
    assert variable in excinfo.value.message


def test_every_default_body_is_valid() -> None:
    for role in PromptRole:
        validate_body(DEFAULT_USER_BODIES[role], role=role)


def test_knowledge_placeholder_must_be_inside_knowledge_delimiters() -> None:
    undelimited = (
        DEFAULT_USER_BODIES[PromptRole.ANALYSIS]
        .replace("<knowledge>", "")
        .replace("</knowledge>", "")
    )

    with pytest.raises(PromptValidationError) as excinfo:
        validate_body(undelimited, role=PromptRole.ANALYSIS)

    assert excinfo.value.variable == "knowledge"
    assert "knowledge" in excinfo.value.message


def test_unknown_variable_is_rejected_and_named() -> None:
    with pytest.raises(PromptValidationError) as excinfo:
        validate_body("{{openai_api_key}} {{symbol}}", role=PromptRole.ANALYSIS)

    assert excinfo.value.variable == "openai_api_key"


def test_malformed_placeholder_is_rejected() -> None:
    with pytest.raises(PromptValidationError) as excinfo:
        validate_body("{{symbol} and {{timeframe}}", role=PromptRole.ANALYSIS)

    assert excinfo.value.code == "PROMPT_VALIDATION_FAILED"
    assert excinfo.value.variable is None


def test_empty_body_is_rejected() -> None:
    with pytest.raises(PromptValidationError):
        validate_body("   \n", role=PromptRole.ANALYSIS)


def test_service_edit_rejects_a_missing_variable_without_saving(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    service = PromptService(store=store, clock=FixedClock(NOW))
    bad = DEFAULT_USER_BODIES[PromptRole.ANALYSIS].replace("{{metrics}}", "")

    with pytest.raises(PromptValidationError) as excinfo:
        service.edit("analysis", bad)

    assert excinfo.value.variable == "metrics"
    assert len(service.history("analysis")) == 1


def test_a_template_cannot_interpolate_a_provider_key_or_api_token() -> None:
    for variable in ("openai_api_key", "api_token"):
        with pytest.raises(PromptValidationError) as excinfo:
            validate_body("{{" + variable + "}}", role=PromptRole.ANALYSIS)
        assert excinfo.value.variable == variable


def test_rendered_prompt_redacts_secret_like_strings() -> None:
    rendered = render("key={{key}}", {"key": "sk-live-abcdefghijklmnop"})

    assert "sk-live-abcdefghijklmnop" not in rendered
    assert "[REDACTED]" in rendered


def test_rendered_prompt_redacts_exact_configured_secret_values() -> None:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=Path("/tmp/oft-test"),
        api_token="api-token-super-secret-value",
        openai_api_key="sk-openai-super-secret-value",
    )
    secrets = secret_values_from_settings(settings)

    rendered = render(
        "api={{api}} key={{key}}",
        {
            "api": "api-token-super-secret-value",
            "key": "sk-openai-super-secret-value",
        },
        secret_values=secrets,
    )

    assert "api-token-super-secret-value" not in rendered
    assert "sk-openai-super-secret-value" not in rendered
    assert rendered.count("[REDACTED]") == 2


def test_render_active_redacts_configured_secrets_even_without_caller_values(
    tmp_path: Path,
) -> None:
    api_secret = "api-token-super-secret-value"
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data", api_token=api_secret)
    store = SqliteStore(settings)
    store.initialize()
    service = PromptService(
        store=store,
        clock=FixedClock(NOW),
        secret_values=secret_values_from_settings(settings),
    )

    rendered = service.render_active(
        PromptRole.ANALYSIS,
        {
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "as_of": "2026-11-08T12:00:00.000Z",
            "metrics": f'{{"leaked_api": "{api_secret}"}}',
            "snapshot": "{}",
            "knowledge": "",
            "schema": "{}",
        },
    )

    # The service's own secrets are applied even when the caller passes none.
    assert api_secret not in rendered.user
    assert api_secret not in rendered.system
    assert "[REDACTED]" in rendered.user
    # The hash describes the redacted rendered prompt.
    assert rendered.prompt_hash == rendered_prompt_hash(
        rendered.prompt_id, rendered.version, rendered.system, rendered.user
    )


def test_render_requires_a_value_for_every_placeholder() -> None:
    with pytest.raises(PromptValidationError) as excinfo:
        render("Symbol: {{symbol}}", {})

    assert excinfo.value.variable == "symbol"


def test_domain_helpers_and_system_prompt_validation() -> None:
    assert prompt_id_for_role(PromptRole.ANALYSIS) == "analysis"
    assert system_prompt_id_for_role(PromptRole.ANALYSIS) == "analysis:system"
    assert (
        validate_body(
            DEFAULT_SYSTEM_BODIES[PromptRole.ANALYSIS],
            role=PromptRole.ANALYSIS,
            system=True,
        )
        == ()
    )
    with pytest.raises(PromptValidationError) as excinfo:
        validate_body("{{symbol}}", role=PromptRole.ANALYSIS, system=True)
    assert excinfo.value.variable == "symbol"
    assert provenance_status("analysis", 1, [1, 2]) == "available"
    assert provenance_status("analysis", 3, [1, 2]) == "missing"
    # An empty configured value is ignored; an exact match is redacted.
    assert redact_secrets("sk-test12345678", secret_values=("", "abc", "sk-test12345678")) == (
        "[REDACTED]"
    )
