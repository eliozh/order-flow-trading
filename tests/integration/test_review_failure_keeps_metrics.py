"""Integration: a refused/invalid review never links text or changes the trade."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_review_flow import FIXTURES, _closed_trade, _review_service

from oft.adapters.llm_stub import LLMStub
from oft.application.review_service import ReviewRejected, ReviewService
from oft.ports.llm import (
    CostEstimate,
    LLMRequest,
    LLMResult,
    LLMRole,
    LLMUsage,
    ProviderCapabilities,
)


class _FlaggedProvider:
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


def _payload(container, trade_id: str) -> dict:  # type: ignore[no-untyped-def]
    record = container.store.get_trade_log(trade_id)
    assert record is not None
    return json.loads(record.payload_json)


class _FailOnceProvider:
    """Refuses the first attempt, then delegates to the valid review fixture."""

    provider = "stub"

    def __init__(self) -> None:
        self._inner = LLMStub(
            fixtures_dir=FIXTURES, overrides={LLMRole.REVIEW: "review_valid.json"}
        )
        self.calls = 0

    def capabilities(self) -> ProviderCapabilities:
        return self._inner.capabilities()

    def complete(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        if self.calls == 1:
            return LLMResult(
                provider="stub",
                model="fixture",
                raw_text="",
                usage=LLMUsage(),
                request_id=request.request_id,
                refusal=True,
            )
        return self._inner.complete(request)

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate:
        return self._inner.estimate_cost(request, model)


@pytest.mark.parametrize(
    ("flags", "code"),
    (
        ({"refusal": True}, "LLM_REFUSED"),
        ({"truncated": True}, "LLM_OUTPUT_TRUNCATED"),
    ),
)
def test_a_refusal_or_truncation_creates_no_review_text(
    tmp_path: Path, flags: dict[str, bool], code: str
) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    before = _payload(container, trade_id)
    metrics = ("net_pnl_usd", "gross_pnl_usd", "fees_usd", "funding_usd", "r_multiple")

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container, _FlaggedProvider(**flags)).review(
            trade_id, idempotency_key=f"reject-{code}"
        )

    assert excinfo.value.code == code
    assert excinfo.value.message
    assert excinfo.value.retryable is False
    after = _payload(container, trade_id)
    assert after["review_decision_id"] is None
    assert {field: after[field] for field in metrics} == {field: before[field] for field in metrics}
    decisions = container.store.list_decisions(role="REVIEW")
    assert len(decisions) == 1
    assert decisions[0].validation_status == "REJECTED"
    envelope = json.loads(decisions[0].payload_json)
    assert envelope["rationale"] == ""
    assert envelope["analysis"] is None
    assert envelope["position_management"] is None
    read = _review_service(container).read(trade_id)
    assert read.status == "MISSING"


def test_a_schema_invalid_review_is_repaired_once_then_rejected(
    tmp_path: Path,
) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    before = _payload(container, trade_id)
    provider = LLMStub(fixtures_dir=FIXTURES, overrides={LLMRole.REVIEW: "invalid_schema.json"})

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container, provider).review(trade_id, idempotency_key="invalid")

    assert excinfo.value.code == "LLM_SCHEMA_INVALID"
    assert excinfo.value.field is not None
    calls = container.store.list_llm_calls(role="REVIEW")
    assert len(calls) == 2
    assert {call.attempt for call in calls} == {1, 2}
    after = _payload(container, trade_id)
    assert after["review_decision_id"] is None
    assert after["net_pnl_usd"] == before["net_pnl_usd"]
    assert container.store.list_decisions(role="REVIEW")[-1].validation_status == "REJECTED"


def test_a_failed_refresh_keeps_the_previous_link_and_metrics(tmp_path: Path) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    first = _review_service(container).review(trade_id, idempotency_key="ok-1")
    before = _payload(container, trade_id)

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container, _FlaggedProvider(refusal=True)).review(
            trade_id, refresh=True, idempotency_key="refresh-fail"
        )

    assert excinfo.value.code == "LLM_REFUSED"
    after = _payload(container, trade_id)
    assert after["review_decision_id"] == first.decision_id
    assert after["net_pnl_usd"] == before["net_pnl_usd"]
    assert container.store.get_decision(first.decision_id) is not None
    assert container.store.get_decision(first.decision_id).validation_status == "VALID"  # type: ignore[union-attr]
    read = _review_service(container).read(trade_id)
    assert read.status == "FOUND"
    assert read.decision_id == first.decision_id


def test_missing_provider_is_a_typed_catalog_error(tmp_path: Path) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    service = ReviewService(
        store=container.store,
        clock=container.clock,
        settings=container.settings,
        provider=None,
        runtime=container.llm_runtime,
    )

    with pytest.raises(ReviewRejected) as excinfo:
        service.review(trade["trade_id"], idempotency_key="no-provider")

    assert excinfo.value.code == "LLM_PROVIDER_NOT_CONFIGURED"
    assert container.store.list_decisions(role="REVIEW") == ()
    assert _payload(container, trade["trade_id"])["review_decision_id"] is None


def test_a_missing_trade_is_not_found_and_creates_nothing(tmp_path: Path) -> None:
    _settings, container, _position, _trade = _closed_trade(tmp_path)

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container).review("TRD-MISSING", idempotency_key="missing")

    assert excinfo.value.code == "NOT_FOUND"
    assert container.store.list_decisions(role="REVIEW") == ()


def test_a_plain_retry_after_a_rejection_re_attempts_and_links(tmp_path: Path) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    provider = _FailOnceProvider()
    service = _review_service(container, provider)

    with pytest.raises(ReviewRejected) as excinfo:
        service.review(trade_id, idempotency_key="review:fixed")
    assert excinfo.value.code == "LLM_REFUSED"
    assert "Retry the review" in excinfo.value.message
    rejected = container.store.list_decisions(role="REVIEW")
    assert len(rejected) == 1
    assert rejected[0].validation_status == "REJECTED"
    # The rejection is auditable under an attempt-specific id, not the
    # deterministic key that a later plain retry looks up.
    assert rejected[0].decision_id.endswith("-R1")
    assert _payload(container, trade_id)["review_decision_id"] is None

    result = service.review(trade_id, idempotency_key="review:fixed")

    assert provider.calls == 2
    assert result.validation_status == "VALID"
    assert result.idempotent_replay is False
    assert not result.decision_id.endswith("-R1")
    assert len(container.store.list_decisions(role="REVIEW")) == 2
    assert _payload(container, trade_id)["review_decision_id"] == result.decision_id
    read = _review_service(container).read(trade_id)
    assert read.status == "FOUND"
    assert read.decision_id == result.decision_id
