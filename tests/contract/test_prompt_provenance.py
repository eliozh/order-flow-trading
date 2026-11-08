"""Decision provenance: prompt version/hash on decisions, missing versions fail closed."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import _helpers
from _registry import validate

from oft.adapters.sqlite_store import database_path
from oft.application.analysis_service import AnalysisService
from oft.application.prompt_service import PromptService, secret_values_from_settings
from oft.domain.prompts import PromptRole
from oft.ports.llm import CostEstimate, LLMRequest, LLMResult, ProviderCapabilities

NOW = datetime(2026, 10, 8, tzinfo=UTC)


class _CapturingProvider:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []
        self._name = "opportunity_valid.json"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider="stub", strict_schema=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        raw = (_helpers.LLM_FIXTURES / self._name).read_text(encoding="utf-8")
        return LLMResult(provider="stub", model="fixture", raw_text=raw, request_id="r1")

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate:
        return CostEstimate(amount_usd="0", price_version="stub/v1", unpriced=True)


def test_every_decision_records_the_prompt_version_and_hash(tmp_path: Path) -> None:
    loop = _helpers.build_loop(tmp_path)
    container = loop["container"]

    decisions = container.store.list_decisions()
    assert {decision.role for decision in decisions} == {"ANALYSIS", "ENTRY_RECHECK"}
    for decision in decisions:
        payload = json.loads(decision.payload_json)
        validate("llm-decision.schema.json", payload)
        provenance = payload["provenance"]
        assert provenance["prompt_id"] in {"analysis", "entry_recheck"}
        assert provenance["prompt_version"] >= 1
        assert len(provenance["prompt_hash"]) == 64
        assert provenance["knowledge_artifact_hash"] is None

    calls = container.store.list_llm_calls()
    assert calls
    for call in calls:
        assert call.system_prompt_id.endswith(":system")
        assert call.system_prompt_version >= 1
        assert len(call.system_prompt_hash) == 64


def test_changing_the_body_changes_the_recorded_prompt_hash(tmp_path: Path) -> None:
    loop = _helpers.build_loop(tmp_path)
    container = loop["container"]
    service = PromptService(store=container.store, clock=container.clock)
    values = {
        "symbol": "BTCUSDT",
        "timeframe": "4h",
        "as_of": "2026-10-08T00:00:00.000Z",
        "metrics": "{}",
        "snapshot": "{}",
        "knowledge": "",
        "schema": "{}",
    }
    before = service.render_active(PromptRole.ANALYSIS, values).prompt_hash

    created = service.edit(
        "analysis",
        "Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n"
        "<market_data>{{metrics}}</market_data>\n<knowledge>{{knowledge}}</knowledge>\n"
        "CHANGED\n<schema>{{schema}}</schema>\n",
    )
    service.activate("analysis", created.version)
    after = service.render_active(PromptRole.ANALYSIS, values).prompt_hash

    assert before != after


def test_a_historical_decision_with_a_missing_version_is_missing_not_substituted(
    tmp_path: Path,
) -> None:
    loop = _helpers.build_loop(tmp_path)
    container = loop["container"]
    decision = json.loads(container.store.list_decisions(role="ANALYSIS")[0].payload_json)
    provenance = decision["provenance"]
    prompt_id = provenance["prompt_id"]
    version = provenance["prompt_version"]

    connection = sqlite3.connect(database_path(container.settings))
    try:
        connection.execute(
            "DELETE FROM prompt_versions WHERE prompt_id = ? AND version = ?",
            (prompt_id, version),
        )
        connection.commit()
    finally:
        connection.close()

    service = PromptService(store=container.store, clock=container.clock)
    resolution = service.resolve_decision(decision)

    assert resolution.status == "missing"
    assert resolution.exact_replay is False
    assert resolution.code == "PROMPT_VERSION_MISSING"
    assert resolution.prompt_hash is None


def test_rendered_prompts_and_hash_inputs_never_carry_a_secret(tmp_path: Path) -> None:
    api_secret = "api-token-super-secret-value"
    provider_secret = "sk-openai-super-secret-value"
    settings = _helpers.settings(tmp_path).model_copy(
        update={"api_token": api_secret, "openai_api_key": provider_secret}
    )
    from oft.adapters.sqlite_store import SqliteStore

    store = SqliteStore(settings)
    store.initialize()
    provider = _CapturingProvider()
    service = AnalysisService(
        store=store,
        clock=_helpers.FixedClock(NOW),
        provider=provider,
        settings=settings,
    )

    service.analyze(
        symbol="BTCUSDT",
        timeframe="4h",
        metrics={
            "metric_version": "orderflow_metrics.v1",
            "data_quality": "COMPLETE",
            "leaked_api": api_secret,
            "leaked_provider": provider_secret,
        },
        snapshot={"snapshot_id": "SNAP-1", "as_of": "2026-10-08T00:00:00.000Z"},
        scan_id="SCAN-1",
        report_id="RPT-1",
    )

    request = provider.requests[0]
    assert api_secret not in request.user_prompt
    assert provider_secret not in request.user_prompt
    assert api_secret not in request.system_prompt
    assert provider_secret not in request.system_prompt
    # The hash input is the redacted prompt; the digest must not be a hash of
    # text that still contains the secret.
    expected = hashlib.sha256(
        f"{request.system_prompt}\n{request.user_prompt}\n{request.schema_json}\n".encode()
    ).hexdigest()
    assert request.input_hash == expected
    assert secret_values_from_settings(settings)
