"""Missing/coerced provider usage is recorded honestly (M10)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from oft.adapters.llm_common import int_field, usage_reported
from oft.adapters.llm_openai import OpenAIAdapter
from oft.adapters.sqlite_store import SqliteStore
from oft.application.budget_service import BudgetService
from oft.config import Settings
from oft.domain.pricing import ModelPrice, PriceTable
from oft.ports.llm import LLMResult, LLMUsage
from oft.ports.store import LLMCallRecord

NOW = "2026-11-09T10:00:00.000Z"


def _adapter(body: dict[str, Any]) -> OpenAIAdapter:
    return OpenAIAdapter(
        api_key="sk-test",
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body)),
    )


def _request() -> Any:
    from oft.application.llm_validation import schema_text
    from oft.ports.llm import LLMRequest, LLMRole

    return LLMRequest(
        role=LLMRole.ANALYSIS,
        model="gpt-fixture",
        system_prompt="system",
        user_prompt="user",
        schema_json=schema_text(),
        input_hash="ih",
        prompt_id="analysis",
        prompt_version=1,
        prompt_hash="ph",
    )


def _body(*, usage: object = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "resp",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "{}"}],
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def test_a_response_without_usage_is_marked_unreported() -> None:
    result = _adapter(_body()).complete(_request())

    assert result.usage_reported is False


def test_integral_float_usage_is_accepted_and_reported() -> None:
    result = _adapter(_body(usage={"input_tokens": 1000.0, "output_tokens": 5})).complete(
        _request()
    )

    assert result.usage_reported is True
    assert result.usage.input_tokens == 1000
    assert result.usage.output_tokens == 5


def test_int_field_accepts_integral_floats_only() -> None:
    assert int_field({"v": 10}, "v") == 10
    assert int_field({"v": 10.0}, "v") == 10
    assert int_field({"v": 10.5}, "v") == 0
    assert int_field({"v": True}, "v") == 0
    assert int_field(None, "v") == 0
    assert usage_reported({"input_tokens": 1.0}, ("input_tokens",)) is True
    assert usage_reported({"input_tokens": 1.5}, ("input_tokens",)) is False
    assert usage_reported(None, ("input_tokens",)) is False


def test_empty_partial_and_negative_usage_objects_are_not_reported() -> None:
    """Every expected field must be present, integral, and non-negative (F6)."""
    names = ("input_tokens", "output_tokens")
    assert usage_reported({}, names) is False
    assert usage_reported({"input_tokens": 1000}, names) is False
    assert usage_reported({"input_tokens": 1000, "output_tokens": -1}, names) is False
    assert usage_reported({"input_tokens": 1000, "output_tokens": 5}, names) is True


def test_int_field_clamps_huge_counts_to_avoid_decimal_overflow() -> None:
    assert int_field({"v": 10**100}, "v") == 2**31 - 1
    assert int_field({"v": -5}, "v") == 0


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"input_tokens": 5},
    ],
)
def test_an_incomplete_usage_object_marks_the_call_unreported(usage: dict[str, Any]) -> None:
    result = _adapter(_body(usage=usage)).complete(_request())

    assert result.usage_reported is False


def test_missing_usage_forces_an_unpriced_row_and_an_audit_event(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    table = PriceTable(
        version="v",
        models={
            ("openai", "gpt-fixture"): ModelPrice(
                provider="openai",
                model="gpt-fixture",
                input_per_mtok=Decimal("2.50"),
                output_per_mtok=Decimal("10.00"),
                cache_read_per_mtok=Decimal("1.00"),
                cache_write_per_mtok=Decimal("1.00"),
                source_url="",
                as_of="2026-11-09",
            )
        },
    )
    service = BudgetService(store=store, price_table=table)
    result = LLMResult(
        provider="openai",
        model="gpt-fixture",
        raw_text="{}",
        usage=LLMUsage(input_tokens=1_000_000, output_tokens=0),
        usage_reported=False,
    )
    store.save_llm_call(
        LLMCallRecord(
            call_id="CALL-NO-USAGE",
            role="ANALYSIS",
            provider="openai",
            model="gpt-fixture",
            prompt_id="analysis",
            prompt_version=1,
            prompt_hash="ph",
            input_hash="ih",
            attempt=1,
            request_id="req",
            status="OK",
            raw_output_hash="rh",
            usage_json=json.dumps(result.usage.to_contract()),
            latency_ms=1,
            created_at=NOW,
        )
    )

    entry = service.record_call(
        call_id="CALL-NO-USAGE",
        decision_id=None,
        role="ANALYSIS",
        result=result,
        occurred_at=NOW,
    )

    assert entry.payload["unpriced"] is True
    assert entry.payload["cost_usd"] == "0"
    events = [
        event for event in store.list_audit_events() if event.event_type == "LLM_USAGE_MISSING"
    ]
    assert len(events) == 1
