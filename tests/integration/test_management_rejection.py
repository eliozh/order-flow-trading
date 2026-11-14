"""Integration: refused, truncated, and invalid management output creates no order."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_management_flow import FIXTURES, _setup

from oft.adapters.llm_stub import LLMStub
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import ManagementRejected, ManagementService
from oft.ports.llm import (
    CostEstimate,
    LLMRequest,
    LLMResult,
    LLMRole,
    LLMUsage,
    ProviderCapabilities,
)


class _FlaggedProvider:
    """A minimal provider that returns one explicit refusal/truncation."""

    provider = "stub"

    def __init__(self, *, refusal: bool = False, truncated: bool = False) -> None:
        self._refusal = refusal
        self._truncated = truncated

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider="stub", strict_schema=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        return LLMResult(
            provider="stub",
            model="fixture",
            raw_text="",
            usage=LLMUsage(),
            request_id=request.request_id,
            refusal=self._refusal,
            truncated=self._truncated,
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate:
        return CostEstimate(amount_usd="0", price_version="stub/v1", unpriced=True)


def _service(setup: dict[str, Any], provider: Any) -> ManagementService:
    return ManagementService(
        store=setup["container"].store,
        clock=setup["container"].clock,
        provider=provider,
        settings=setup["settings"],
        kill_switch=KillSwitch(
            settings=setup["settings"],
            store=setup["container"].store,
            clock=setup["container"].clock,
        ),
        runtime=setup["container"].llm_runtime,
    )


def _assert_no_order(container: Any) -> None:
    assert container.store.count_orders() == 1  # the entry order only
    assert container.store.list_trade_logs(limit=None) == ()


@pytest.mark.parametrize(
    ("flags", "code"),
    (
        ({"refusal": True}, "LLM_REFUSED"),
        ({"truncated": True}, "LLM_OUTPUT_TRUNCATED"),
    ),
)
def test_a_refusal_or_truncation_creates_no_order(
    tmp_path: Path, flags: dict[str, bool], code: str
) -> None:
    setup = _setup(tmp_path)
    service = _service(setup, _FlaggedProvider(**flags))

    with pytest.raises(ManagementRejected) as excinfo:
        service.manage(setup["position"]["position_id"], idempotency_key=f"reject-{code}")

    assert excinfo.value.code == code
    _assert_no_order(setup["container"])
    decisions = setup["container"].store.list_decisions(role="MANAGEMENT")
    assert decisions and decisions[-1].validation_status == "REJECTED"


def test_a_schema_invalid_output_is_repaired_once_then_rejected(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    provider = LLMStub(
        fixtures_dir=FIXTURES,
        overrides={LLMRole.MANAGEMENT: "invalid_schema.json"},
    )
    service = _service(setup, provider)

    with pytest.raises(ManagementRejected) as excinfo:
        service.manage(setup["position"]["position_id"], idempotency_key="repair-once")

    assert excinfo.value.code == "LLM_SCHEMA_INVALID"
    assert excinfo.value.field is not None
    calls = setup["container"].store.list_llm_calls(role="MANAGEMENT")
    assert len(calls) == 2
    assert {call.attempt for call in calls} == {1, 2}
    _assert_no_order(setup["container"])


def test_a_management_decision_rejection_records_the_failing_field(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    provider = LLMStub(
        fixtures_dir=FIXTURES,
        overrides={LLMRole.MANAGEMENT: "management_invalid_stop.json"},
    )
    service = _service(setup, provider)

    with pytest.raises(ManagementRejected) as excinfo:
        service.manage(setup["position"]["position_id"], idempotency_key="semantic-reject")

    assert excinfo.value.code == "LLM_SCHEMA_INVALID"
    assert excinfo.value.field == "position_management.new_stop_price"
    _assert_no_order(setup["container"])
    decision = setup["container"].store.list_decisions(role="MANAGEMENT")[-1]
    envelope = json.loads(decision.payload_json)
    assert envelope["validation_status"] == "REJECTED"
    assert envelope["validation_errors"]
    assert envelope["position_management"]["action"] == "PARTIAL_CLOSE"
