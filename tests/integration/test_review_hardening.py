"""Integration: fail-closed, typed, and replay-safe review hardening (M2-M5/M11-M15)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_review_flow import (
    _METRIC_FIELDS,
    FIXTURES,
    NOW,
    _audit_payloads,
    _closed_trade,
    _review_service,
    _trade_payload,
)

import oft.application.review_service as review_module
from oft.adapters.llm_stub import LLMStub
from oft.api.app import create_app
from oft.application.review_models import VALID_REVIEW_STATUSES
from oft.application.review_service import ReviewRejected, ReviewService, _review_decision_id
from oft.clock import FixedClock
from oft.ports.llm import (
    CostEstimate,
    LLMRateLimited,
    LLMRequest,
    LLMResult,
    LLMRole,
    LLMTimeoutError,
    LLMUsage,
    ProviderCapabilities,
)
from oft.ports.store import DecisionRecord


def _execute(settings: Any, statement: str, params: tuple[Any, ...] = ()) -> None:
    connection = sqlite3.connect(settings.data_dir / "oft.db")
    try:
        connection.execute(statement, params)
        connection.commit()
    finally:
        connection.close()


def _save_linked(
    container: Any,
    trade_id: str,
    decision_id: str,
    payload: Any,
    *,
    validation_status: str = "VALID",
    role: str = "REVIEW",
    decision_type: str = "REVIEW",
) -> None:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    container.store.save_decision(
        DecisionRecord(
            decision_id=decision_id,
            role=role,
            decision_type=decision_type,
            validation_status=validation_status,
            raw_output_hash="hash",
            persisted_at="2026-10-08T00:00:00.000Z",
            payload_json=raw,
        )
    )
    assert container.store.link_trade_log_review(trade_id, decision_id=decision_id)


def _valid_fields(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": "REVIEW",
        "decision_type": "REVIEW",
        "validation_status": "VALID",
        "rationale": "the trade followed the plan",
        "caveats": ["advisory only"],
        "confidence": 0.5,
        "provenance": {},
    }
    payload.update(overrides)
    return payload


def _save_unlinked(
    container: Any,
    decision_id: str,
    payload: Any,
    *,
    validation_status: str = "VALID",
) -> None:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    container.store.save_decision(
        DecisionRecord(
            decision_id=decision_id,
            role="REVIEW",
            decision_type="REVIEW",
            validation_status=validation_status,
            raw_output_hash="hash",
            persisted_at="2026-10-08T00:00:00.000Z",
            payload_json=raw,
        )
    )


def _accepted_envelope(
    trade: dict[str, Any],
    *,
    decision_id: str,
    symbol: str | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "decision_id": decision_id,
        "decision_type": "REVIEW",
        "role": "REVIEW",
        "subject": {
            "symbol": symbol or trade["symbol"],
            "timeframe": "4h",
            "as_of": "2026-10-08T00:00:00.000Z",
            "position_id": trade["position_id"],
            "environment": environment or trade["environment"],
        },
        "provenance": {},
        "confidence": 0.5,
        "rationale": "a seeded accepted review",
        "caveats": [],
        "analysis": None,
        "opportunity": None,
        "entry_recheck": None,
        "position_management": None,
        "validation_status": "VALID",
        "validation_errors": [],
        "rejected_reason": None,
        "raw_output_hash": "hash",
        "persisted_at": "2026-10-08T00:00:00.000Z",
    }


class _RawTextProvider:
    """Returns one fixed provider body for both the initial and repair attempt."""

    provider = "stub"

    def __init__(self, raw_text: str) -> None:
        self._raw_text = raw_text
        self.calls = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider="stub", strict_schema=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        return LLMResult(
            provider="stub",
            model="fixture",
            raw_text=self._raw_text,
            usage=LLMUsage(),
            request_id=request.request_id,
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate:
        return CostEstimate(amount_usd="0", price_version="stub/v1", unpriced=True)


# -- M2: a tampered trigger_id is ignored, never a SQL bind error --------------


def test_a_non_string_trigger_id_is_ignored_and_the_review_still_runs(tmp_path: Path) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    payload = _trade_payload(container, trade_id)
    payload["signal"]["trigger_id"] = {"hostile": "value"}
    _execute(
        settings,
        "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
        (json.dumps(payload), trade_id),
    )

    result = _review_service(container).review(trade_id, idempotency_key="trigger-dict")

    assert result.status == "FOUND"
    assert result.validation_status == "VALID"
    envelope = json.loads(container.store.get_decision(result.decision_id).payload_json)  # type: ignore[union-attr]
    assert envelope["subject"]["trigger_id"] is None
    assert envelope["subject"]["timeframe"] == "4h"


# -- M3: authoritative envelope status and a guarded validation_errors shape --


def test_a_rejected_replay_with_a_dict_validation_errors_is_a_typed_error(
    tmp_path: Path,
) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    decision_id = _review_decision_id(trade_id, "rejected-replay")
    _save_linked(
        container,
        trade_id,
        decision_id,
        _valid_fields(
            validation_status="REJECTED",
            rationale="",
            caveats=[],
            confidence=0.0,
            validation_errors={"code": "SCHEMA_INVALID"},
            rejected_reason="the review output was rejected",
        ),
        validation_status="REJECTED",
    )

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container).review(trade_id, idempotency_key="rejected-replay")

    assert excinfo.value.code == "LLM_SCHEMA_INVALID"
    assert excinfo.value.message == "the review output was rejected"
    assert excinfo.value.retryable is False


def test_the_envelope_status_is_authoritative_over_the_stored_column(tmp_path: Path) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    decision_id = _review_decision_id(trade_id, "authoritative")
    # The column says REJECTED, but the persisted envelope (the authority) says
    # VALID with complete fields, so the replay is a FOUND review.
    _save_linked(
        container,
        trade_id,
        decision_id,
        _valid_fields(validation_status="VALID"),
        validation_status="REJECTED",
    )

    result = _review_service(container).review(trade_id, idempotency_key="authoritative")

    assert result.status == "FOUND"
    assert result.validation_status == "VALID"
    assert result.rationale == "the trade followed the plan"


@pytest.mark.parametrize("stored_status", (None, "SOMETHING_ELSE"))
def test_an_unrecognized_envelope_status_is_partial_not_a_500(
    tmp_path: Path, stored_status: str | None
) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    _save_linked(
        container,
        trade_id,
        "RVW-ODD-STATUS",
        _valid_fields(validation_status=stored_status),
        validation_status="VALID",
    )

    replay = _review_service(container).review(trade_id, idempotency_key="odd-status")
    assert replay.status == "PARTIAL"
    assert replay.idempotent_replay is True
    assert replay.reason

    app = create_app(settings, clock=FixedClock(NOW))
    headers = {"Authorization": f"Bearer {app.state.api_token}"}
    with TestClient(app) as client:
        response = client.get(f"/api/v1/journal/{trade_id}/review", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "PARTIAL"
    assert response.json()["meta"]["data_quality"] == "PARTIAL"


# -- M4: a missing/unreadable position or fill fails closed --------------------


def _assert_no_decision_and_unchanged(
    container: Any, trade_id: str, metrics_before: dict[str, Any]
) -> None:
    assert container.store.list_decisions(role="REVIEW") == ()
    link = container.store.get_trade_log(trade_id)
    assert link is not None and link.review_decision_id is None
    after = _trade_payload(container, trade_id)
    assert {field: after[field] for field in _METRIC_FIELDS} == metrics_before


def test_a_missing_position_row_is_data_unavailable(tmp_path: Path) -> None:
    settings, container, position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    metrics_before = {field: _trade_payload(container, trade_id)[field] for field in _METRIC_FIELDS}
    _execute(settings, "DELETE FROM positions WHERE position_id = ?", (position["position_id"],))

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container).review(trade_id, idempotency_key="missing-position")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert position["position_id"] in excinfo.value.message
    _assert_no_decision_and_unchanged(container, trade_id, metrics_before)


def test_an_unreadable_position_payload_is_data_unavailable(tmp_path: Path) -> None:
    settings, container, position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    metrics_before = {field: _trade_payload(container, trade_id)[field] for field in _METRIC_FIELDS}
    _execute(
        settings,
        "UPDATE positions SET payload_json = 'not-json' WHERE position_id = ?",
        (position["position_id"],),
    )

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container).review(trade_id, idempotency_key="garbled-position")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    _assert_no_decision_and_unchanged(container, trade_id, metrics_before)


def test_a_missing_fill_for_the_trade_order_is_data_unavailable(tmp_path: Path) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    metrics_before = {field: _trade_payload(container, trade_id)[field] for field in _METRIC_FIELDS}
    record = container.store.get_trade_log(trade_id)
    assert record is not None and record.order_id
    _execute(settings, "DELETE FROM fills WHERE order_id = ?", (record.order_id,))

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container).review(trade_id, idempotency_key="missing-fill")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert record.order_id in excinfo.value.message
    _assert_no_decision_and_unchanged(container, trade_id, metrics_before)


# -- M5: a failed audit is repaired on replay --------------------------------


def test_a_lost_generated_audit_is_repaired_on_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    service = _review_service(container)
    real = review_module.record_audit
    calls = {"count": 0}

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("audit store unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(review_module, "record_audit", fail_once)
    with pytest.raises(RuntimeError):
        service.review(trade_id, idempotency_key="audit-repair")

    decisions = container.store.list_decisions(role="REVIEW")
    assert len(decisions) == 1
    decision_id = decisions[0].decision_id
    linked = container.store.get_trade_log(trade_id)
    assert linked is not None and linked.review_decision_id == decision_id
    assert _audit_payloads(container, "REVIEW_GENERATED") == []

    monkeypatch.setattr(review_module, "record_audit", real)
    replay = service.review(trade_id, idempotency_key="audit-repair")

    assert replay.idempotent_replay is True
    audits = _audit_payloads(container, "REVIEW_GENERATED")
    assert [item["decision_id"] for item in audits] == [decision_id]

    # A second replay after the repair must not append another event, and the
    # repaired event carries the stored classification and provider.
    second = service.review(trade_id, idempotency_key="audit-repair")
    assert second.idempotent_replay is True
    audits = _audit_payloads(container, "REVIEW_GENERATED")
    assert [item["decision_id"] for item in audits] == [decision_id]
    assert audits[0]["validation_status"] == "VALID"
    assert audits[0]["provider"] == "stub"


# -- NEW-2: an unreadable existing audit fails closed, never duplicates ------


def test_an_unreadable_review_audit_fails_closed_without_duplicating(tmp_path: Path) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    first = _review_service(container).review(trade_id, idempotency_key="audit-unreadable-1")
    assert first.status == "FOUND"
    assert len(_audit_payloads(container, "REVIEW_GENERATED")) == 1

    # Simulate a damaged row: the append-only trigger must be dropped to tamper
    # with the stored payload, then restored to keep the table append-only.
    _execute(settings, "DROP TRIGGER IF EXISTS audit_events_no_update")
    _execute(
        settings,
        "UPDATE audit_events SET payload_json = 'not-json' WHERE event_type = 'REVIEW_GENERATED'",
    )
    _execute(
        settings,
        "CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
    )

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container).review(trade_id, idempotency_key="audit-unreadable-2")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert "unreadable event" in excinfo.value.message
    connection = sqlite3.connect(settings.data_dir / "oft.db")
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = 'REVIEW_GENERATED'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1


# -- M11: every named read state is a PARTIAL, never a 500 -------------------


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    (
        ("missing", "is missing"),
        ("unreadable", "is unreadable"),
        ("not_review", "is not a REVIEW"),
        ("rejected", "was rejected; no advisory text"),
    ),
)
def test_every_linked_read_state_is_partial(
    tmp_path: Path, case: str, expected_reason: str
) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    if case == "missing":
        decision_id = "RVW-DOES-NOT-EXIST"
        # A dangling link is only reachable through corruption; write it with
        # foreign-key enforcement off, as a damaged database would present it.
        payload = _trade_payload(container, trade_id)
        payload["review_decision_id"] = decision_id
        _execute(
            settings,
            "UPDATE trade_logs SET payload_json = ?, review_decision_id = ? WHERE trade_id = ?",
            (json.dumps(payload), decision_id, trade_id),
        )
    elif case == "unreadable":
        decision_id = "RVW-UNREADABLE"
        _save_linked(container, trade_id, decision_id, "not-json")
    elif case == "not_review":
        decision_id = "RVW-WRONG-ROLE"
        _save_linked(
            container,
            trade_id,
            decision_id,
            _valid_fields(role="ANALYSIS", decision_type="MARKET_ANALYSIS"),
            role="ANALYSIS",
            decision_type="MARKET_ANALYSIS",
        )
    else:
        decision_id = "RVW-REJECTED-STATE"
        _save_linked(
            container,
            trade_id,
            decision_id,
            _valid_fields(
                validation_status="REJECTED",
                rationale="",
                caveats=[],
                confidence=0.0,
            ),
            validation_status="REJECTED",
        )

    read = _review_service(container).read(trade_id)
    assert read.status == "PARTIAL"
    assert read.decision_id == decision_id
    assert read.reason is not None and expected_reason in read.reason

    app = create_app(settings, clock=FixedClock(NOW))
    headers = {"Authorization": f"Bearer {app.state.api_token}"}
    with TestClient(app) as client:
        response = client.get(f"/api/v1/journal/{trade_id}/review", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "PARTIAL"
    assert response.json()["meta"]["data_quality"] == "PARTIAL"


# -- M8: the read path applies the write-path content invariants --------------


@pytest.mark.parametrize(
    "overrides",
    (
        {"rationale": ""},
        {"rationale": "   "},
        {"confidence": 2.0},
        {"confidence": -0.1},
        {"confidence": float("nan")},
        {"confidence": True},
    ),
)
def test_an_invalid_stored_review_is_partial_not_a_500(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    decision_id = "RVW-INVALID-FIELDS"
    _save_linked(container, trade_id, decision_id, _valid_fields(**overrides))

    read = _review_service(container).read(trade_id)
    assert read.status == "PARTIAL"
    assert read.reason == "the linked review fields are malformed"

    replay = _review_service(container).review(trade_id, idempotency_key="invalid-fields")
    assert replay.status == "PARTIAL"
    assert replay.reason == "the stored review fields are malformed"

    app = create_app(settings, clock=FixedClock(NOW))
    headers = {"Authorization": f"Bearer {app.state.api_token}"}
    with TestClient(app) as client:
        response = client.get(f"/api/v1/journal/{trade_id}/review", headers=headers)
        posted = client.post(
            f"/api/v1/journal/{trade_id}/review",
            headers={**headers, "Idempotency-Key": "invalid-fields-post"},
        )
    assert response.status_code == 200, response.text
    assert posted.status_code == 200, posted.text
    assert response.json()["meta"]["data_quality"] == "PARTIAL"
    assert posted.json()["meta"]["data_quality"] == "PARTIAL"


# -- M12: corrupt trade payloads are named, never a 500 ----------------------


@pytest.mark.parametrize("raw", ("not-json", "[1,2,3]"))
def test_an_unreadable_trade_payload_is_named(tmp_path: Path, raw: str) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    _execute(
        settings,
        "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
        (raw, trade_id),
    )

    read = _review_service(container).read(trade_id)
    assert read.status == "PARTIAL"
    assert read.reason == "the trade log payload is unreadable"

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container).review(trade_id, idempotency_key="corrupt-trade")
    assert excinfo.value.code == "DATA_UNAVAILABLE"

    app = create_app(settings, clock=FixedClock(NOW))
    headers = {"Authorization": f"Bearer {app.state.api_token}"}
    with TestClient(app) as client:
        response = client.get(f"/api/v1/journal/{trade_id}/review", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "PARTIAL"


# -- M14: fail-closed timeframe and environment ------------------------------


def test_a_trade_without_a_source_timeframe_is_data_unavailable(tmp_path: Path) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    metrics_before = {field: _trade_payload(container, trade_id)[field] for field in _METRIC_FIELDS}
    payload = _trade_payload(container, trade_id)
    payload["signal"] = {"scan_id": None, "report_id": None, "trigger_id": None}
    _execute(
        settings,
        "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
        (json.dumps(payload), trade_id),
    )

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container).review(trade_id, idempotency_key="no-timeframe")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    _assert_no_decision_and_unchanged(container, trade_id, metrics_before)


def test_a_disallowed_source_timeframe_is_data_unavailable(tmp_path: Path) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    metrics_before = {field: _trade_payload(container, trade_id)[field] for field in _METRIC_FIELDS}
    connection = sqlite3.connect(settings.data_dir / "oft.db")
    try:
        row = connection.execute("SELECT report_id FROM reports LIMIT 1").fetchone()
    finally:
        connection.close()
    assert row is not None
    report_id = str(row[0])
    payload = _trade_payload(container, trade_id)
    payload["signal"] = {"scan_id": None, "report_id": report_id, "trigger_id": None}
    _execute(
        settings,
        "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
        (json.dumps(payload), trade_id),
    )
    _execute(settings, "UPDATE reports SET timeframe = '2m' WHERE report_id = ?", (report_id,))

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container).review(trade_id, idempotency_key="invalid-timeframe")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    _assert_no_decision_and_unchanged(container, trade_id, metrics_before)


def test_a_non_contract_environment_is_data_unavailable(tmp_path: Path) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    metrics_before = {field: _trade_payload(container, trade_id)[field] for field in _METRIC_FIELDS}
    _execute(
        settings,
        "UPDATE trade_logs SET environment = 'TEST' WHERE trade_id = ?",
        (trade_id,),
    )

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container).review(trade_id, idempotency_key="bad-environment")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert "TEST" in excinfo.value.message
    _assert_no_decision_and_unchanged(container, trade_id, metrics_before)


# -- M15: provider errors and link failures are typed ------------------------


class _RaisingProvider:
    provider = "stub"

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider="stub", strict_schema=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        raise self._error

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate:
        return CostEstimate(amount_usd="0", price_version="stub/v1", unpriced=True)


@pytest.mark.parametrize(
    ("error", "code"),
    (
        (LLMRateLimited("rate limited"), "LLM_RATE_LIMITED"),
        (LLMTimeoutError("timed out"), "LLM_TIMEOUT"),
    ),
)
def test_a_provider_error_is_mapped_to_its_typed_code(
    tmp_path: Path, error: Exception, code: str
) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    metrics_before = {field: _trade_payload(container, trade_id)[field] for field in _METRIC_FIELDS}

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container, _RaisingProvider(error)).review(
            trade_id, idempotency_key=f"provider-{code}"
        )

    assert excinfo.value.code == code
    _assert_no_decision_and_unchanged(container, trade_id, metrics_before)


def _assert_decision_persisted_but_unlinked(
    container: Any, trade_id: str, metrics_before: dict[str, Any]
) -> None:
    decisions = container.store.list_decisions(role="REVIEW")
    assert len(decisions) == 1
    assert decisions[0].validation_status == "VALID"
    link = container.store.get_trade_log(trade_id)
    assert link is not None and link.review_decision_id is None
    after = _trade_payload(container, trade_id)
    assert {field: after[field] for field in _METRIC_FIELDS} == metrics_before


def test_a_false_link_write_is_data_unavailable_and_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    metrics_before = {field: _trade_payload(container, trade_id)[field] for field in _METRIC_FIELDS}
    monkeypatch.setattr(container.store, "link_trade_log_review", lambda *args, **kwargs: False)

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container).review(trade_id, idempotency_key="link-false")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    _assert_decision_persisted_but_unlinked(container, trade_id, metrics_before)


def test_a_raising_link_write_is_data_unavailable_and_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    metrics_before = {field: _trade_payload(container, trade_id)[field] for field in _METRIC_FIELDS}

    def raise_value_error(*args: Any, **kwargs: Any) -> bool:
        raise ValueError(f"trade log {trade_id} payload is unreadable")

    monkeypatch.setattr(container.store, "link_trade_log_review", raise_value_error)

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container).review(trade_id, idempotency_key="link-raise")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert "payload is unreadable" in excinfo.value.message
    _assert_decision_persisted_but_unlinked(container, trade_id, metrics_before)


# -- M10: the review prompt never leaks a configured secret ------------------


def test_a_seeded_secret_is_redacted_from_the_prompt_and_never_persisted(
    tmp_path: Path,
) -> None:
    settings, container, position, trade = _closed_trade(tmp_path)
    sentinel = "REVIEW-OPAQUE-SENTINEL-9f3a2c"
    scoped = settings.model_copy(update={"api_token": sentinel})
    from test_review_flow import _CountingStub

    counting = _CountingStub(
        LLMStub(fixtures_dir=FIXTURES, overrides={LLMRole.REVIEW: "review_valid.json"})
    )
    service = ReviewService(
        store=container.store,
        clock=container.clock,
        settings=scoped,
        provider=counting,
        runtime=container.llm_runtime,
    )
    connection = sqlite3.connect(settings.data_dir / "oft.db")
    try:
        row = connection.execute(
            "SELECT payload_json FROM positions WHERE position_id = ?",
            (position["position_id"],),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["management_history"] = [
            *(payload.get("management_history") or []),
            {"action": "HOLD", "at": "2026-10-07T00:00:00.000Z", "note": sentinel},
        ]
        connection.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ?",
            (json.dumps(payload), position["position_id"]),
        )
        connection.commit()
    finally:
        connection.close()

    result = service.review(trade["trade_id"], idempotency_key="seeded-secret")

    prompt = counting.requests[0].user_prompt
    assert sentinel not in prompt
    assert "[REDACTED]" in prompt
    decision = container.store.get_decision(result.decision_id)
    assert decision is not None
    assert sentinel not in decision.payload_json
    assert sentinel not in json.dumps(result.to_contract(), sort_keys=True)


# -- N3: the read/replay path applies the write path's text safety -----------


@pytest.mark.parametrize(
    ("overrides", "decision_id"),
    (
        ({"rationale": "terminal \x1b[31m red"}, "RVW-CONTROL-CHAR"),
        ({"provenance": {"note": "lone surrogate \ud800"}}, "RVW-SURROGATE"),
    ),
)
def test_an_unsafe_stored_review_is_partial_for_get_and_post(
    tmp_path: Path, overrides: dict[str, Any], decision_id: str
) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    _save_linked(container, trade_id, decision_id, _valid_fields(**overrides))

    read = _review_service(container).read(trade_id)
    assert read.status == "PARTIAL"
    assert read.reason == "the linked review fields are malformed"

    replay = _review_service(container).review(trade_id, idempotency_key=f"replay-{decision_id}")
    assert replay.status == "PARTIAL"
    assert replay.reason == "the stored review fields are malformed"

    app = create_app(settings, clock=FixedClock(NOW))
    headers = {"Authorization": f"Bearer {app.state.api_token}"}
    with TestClient(app) as client:
        response = client.get(f"/api/v1/journal/{trade_id}/review", headers=headers)
        posted = client.post(
            f"/api/v1/journal/{trade_id}/review",
            headers={**headers, "Idempotency-Key": f"post-{decision_id}"},
        )
    assert response.status_code == 200, response.text
    assert posted.status_code == 200, posted.text
    assert response.json()["data"]["status"] == "PARTIAL"
    assert posted.json()["data"]["status"] == "PARTIAL"
    assert response.json()["meta"]["data_quality"] == "PARTIAL"
    assert posted.json()["meta"]["data_quality"] == "PARTIAL"


# -- NEW-1: a huge-int stored confidence is PARTIAL, never a 500 --------------


def test_a_huge_integer_confidence_is_partial_for_get_and_post(tmp_path: Path) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    _save_linked(container, trade_id, "RVW-HUGE-CONFIDENCE", _valid_fields(confidence=10**400))

    read = _review_service(container).read(trade_id)
    assert read.status == "PARTIAL"
    assert read.reason == "the linked review fields are malformed"

    replay = _review_service(container).review(trade_id, idempotency_key="replay-huge")
    assert replay.status == "PARTIAL"

    app = create_app(settings, clock=FixedClock(NOW))
    headers = {"Authorization": f"Bearer {app.state.api_token}"}
    with TestClient(app) as client:
        response = client.get(f"/api/v1/journal/{trade_id}/review", headers=headers)
        posted = client.post(
            f"/api/v1/journal/{trade_id}/review",
            headers={**headers, "Idempotency-Key": "post-huge"},
        )
    assert response.status_code == 200, response.text
    assert posted.status_code == 200, posted.text
    assert response.json()["data"]["status"] == "PARTIAL"
    assert posted.json()["data"]["status"] == "PARTIAL"
    assert response.json()["meta"]["data_quality"] == "PARTIAL"
    assert posted.json()["meta"]["data_quality"] == "PARTIAL"


# -- NEW-3: a fresh key recovers an orphaned decision without another call ----


def test_a_fresh_key_relinks_an_orphaned_decision_without_a_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    from test_review_flow import _CountingStub

    provider = _CountingStub(
        LLMStub(fixtures_dir=FIXTURES, overrides={LLMRole.REVIEW: "review_valid.json"})
    )
    service = _review_service(container, provider)
    real_link = container.store.link_trade_log_review
    state = {"calls": 0}

    def fail_once(*args: Any, **kwargs: Any) -> bool:
        state["calls"] += 1
        if state["calls"] == 1:
            raise ValueError("the trade log link is temporarily unavailable")
        return real_link(*args, **kwargs)

    monkeypatch.setattr(container.store, "link_trade_log_review", fail_once)
    with pytest.raises(ReviewRejected) as excinfo:
        service.review(trade_id, idempotency_key="orphan-key-a")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert provider.calls == 1
    decisions = container.store.list_decisions(role="REVIEW")
    assert len(decisions) == 1
    orphan_id = decisions[0].decision_id
    orphaned_link = container.store.get_trade_log(trade_id)
    assert orphaned_link is not None and orphaned_link.review_decision_id is None
    assert _audit_payloads(container, "REVIEW_GENERATED") == []

    # Restore the real link and retry with a DIFFERENT key: the orphaned
    # accepted decision is recovered, so no second provider call is paid for.
    monkeypatch.setattr(container.store, "link_trade_log_review", real_link)
    result = service.review(trade_id, idempotency_key="orphan-key-b")

    assert result.status == "FOUND"
    assert result.decision_id == orphan_id
    assert result.idempotent_replay is True
    assert provider.calls == 1
    assert len(container.store.list_decisions(role="REVIEW")) == 1
    linked = container.store.get_trade_log(trade_id)
    assert linked is not None and linked.review_decision_id == orphan_id
    audits = _audit_payloads(container, "REVIEW_GENERATED")
    assert [item["decision_id"] for item in audits] == [orphan_id]


# -- N4: non-UTF-8 provider text is rejected before persistence/linking -------


def _surrogate_review_body(field: str, value: Any) -> str:
    document = json.loads((FIXTURES / "review_valid.json").read_text(encoding="utf-8"))
    document[field] = value
    # ``ensure_ascii`` renders the lone surrogate as the JSON escape a provider
    # can legitimately return.
    return json.dumps(document)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("rationale", "lone surrogate \ud800"),
        ("caveats", ["lone surrogate \ud800"]),
    ),
)
def test_a_non_utf8_review_is_rejected_before_link_and_never_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    provider = _RawTextProvider(_surrogate_review_body(field, value))
    monkeypatch.setattr("oft.bootstrap.build_llm_adapter", lambda *_: provider)
    app = create_app(settings, clock=FixedClock(NOW))
    headers = {"Authorization": f"Bearer {app.state.api_token}"}

    with TestClient(app) as client:
        posted = client.post(
            f"/api/v1/journal/{trade_id}/review",
            headers={**headers, "Idempotency-Key": "non-utf8"},
        )

    assert posted.status_code == 422, posted.text
    assert posted.json()["error"]["code"] == "LLM_SCHEMA_INVALID"
    # The provider text never reaches the response body (which would 500).
    assert "ud800" not in posted.text

    decisions = container.store.list_decisions(role="REVIEW")
    # No accepted decision was persisted and nothing was linked: only the
    # code-owned REJECTED audit attempt exists.
    assert all(decision.validation_status not in VALID_REVIEW_STATUSES for decision in decisions)
    assert all("\ud800" not in decision.payload_json for decision in decisions)
    linked = container.store.get_trade_log(trade_id)
    assert linked is not None and linked.review_decision_id is None


# -- N5: a stored non-UTF-8 link id/metadata is neutralized, never a 500 ------


def test_a_malformed_linked_review_id_is_partial_on_read_and_get(tmp_path: Path) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    payload = _trade_payload(container, trade_id)
    payload["review_decision_id"] = "RVW-\ud800"
    _execute(
        settings,
        "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
        (json.dumps(payload), trade_id),
    )

    read = _review_service(container).read(trade_id)
    assert read.status == "PARTIAL"
    assert read.decision_id is None
    assert read.reason == "the linked review id is malformed"

    app = create_app(settings, clock=FixedClock(NOW))
    headers = {"Authorization": f"Bearer {app.state.api_token}"}
    with TestClient(app) as client:
        response = client.get(f"/api/v1/journal/{trade_id}/review", headers=headers)
        posted = client.post(
            f"/api/v1/journal/{trade_id}/review",
            headers={**headers, "Idempotency-Key": "malformed-link"},
        )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "PARTIAL"
    assert body["decision_id"] is None
    assert body["reason"] == "the linked review id is malformed"
    assert response.json()["meta"]["data_quality"] == "PARTIAL"
    # An unreadable link id falls through to a fresh review instead of 500ing.
    assert posted.status_code == 200, posted.text
    generated = posted.json()["data"]
    assert generated["status"] == "FOUND"
    assert generated["decision_id"] != "RVW-\ud800"
    linked = container.store.get_trade_log(trade_id)
    assert linked is not None and linked.review_decision_id == generated["decision_id"]


@pytest.mark.parametrize(
    ("unsafe", "expected_code", "expected_message"),
    (
        (
            {"rejected_reason": "bad \ud800"},
            "LLM_SCHEMA_INVALID",
            "the review decision was rejected",
        ),
        (
            {"rejected_reason": "terminal \x1b[31m red"},
            "LLM_SCHEMA_INVALID",
            "the review decision was rejected",
        ),
        (
            {"validation_errors": [{"field": "bad \ud800", "code": "LLM_REFUSED", "message": "x"}]},
            "LLM_REFUSED",
            "the review output was rejected",
        ),
        (
            {"validation_errors": [{"field": "bad \x1b", "code": "LLM_REFUSED", "message": "x"}]},
            "LLM_REFUSED",
            "the review output was rejected",
        ),
    ),
)
def test_a_rejected_replay_with_unsafe_reason_or_field_uses_the_safe_fallback(
    tmp_path: Path, unsafe: dict[str, Any], expected_code: str, expected_message: str
) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    decision_id = _review_decision_id(trade_id, "rejected-unsafe")
    envelope = _valid_fields(
        validation_status="REJECTED",
        rationale="",
        caveats=[],
        confidence=0.0,
        rejected_reason="the review output was rejected",
        validation_errors=[],
    )
    envelope.update(unsafe)
    _save_linked(container, trade_id, decision_id, envelope, validation_status="REJECTED")

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container).review(trade_id, idempotency_key="rejected-unsafe")

    assert excinfo.value.code == expected_code
    assert excinfo.value.message == expected_message
    assert excinfo.value.field is None
    assert excinfo.value.retryable is False

    app = create_app(settings, clock=FixedClock(NOW))
    headers = {"Authorization": f"Bearer {app.state.api_token}"}
    with TestClient(app) as client:
        posted = client.post(
            f"/api/v1/journal/{trade_id}/review",
            headers={**headers, "Idempotency-Key": "rejected-unsafe-api"},
        )

    assert posted.status_code == 422, posted.text
    error = posted.json()["error"]
    assert error["code"] == expected_code
    assert error["message"] == expected_message
    assert error.get("details") in (None, [])


@pytest.mark.parametrize("stored_status", ("VALID\x1b[31m", "VALID\ud800"))
def test_an_unsafe_stored_validation_status_is_partial_for_get_and_post(
    tmp_path: Path, stored_status: str
) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    _save_linked(
        container,
        trade_id,
        "RVW-UNSAFE-STATUS",
        _valid_fields(validation_status=stored_status),
    )

    read = _review_service(container).read(trade_id)
    assert read.status == "PARTIAL"
    assert read.validation_status is None
    assert read.reason == "the linked review has an unrecognized validation status"

    replay = _review_service(container).review(trade_id, idempotency_key="unsafe-status")
    assert replay.status == "PARTIAL"
    assert replay.reason == "the stored review has an unrecognized validation status"

    app = create_app(settings, clock=FixedClock(NOW))
    headers = {"Authorization": f"Bearer {app.state.api_token}"}
    with TestClient(app) as client:
        response = client.get(f"/api/v1/journal/{trade_id}/review", headers=headers)
        posted = client.post(
            f"/api/v1/journal/{trade_id}/review",
            headers={**headers, "Idempotency-Key": "unsafe-status-post"},
        )

    assert response.status_code == 200, response.text
    assert posted.status_code == 200, posted.text
    assert response.json()["data"]["status"] == "PARTIAL"
    assert posted.json()["data"]["status"] == "PARTIAL"
    assert response.json()["meta"]["data_quality"] == "PARTIAL"
    assert posted.json()["meta"]["data_quality"] == "PARTIAL"


@pytest.mark.parametrize("key", ("trigger_id", "scan_id"))
def test_a_non_utf8_source_id_is_ignored_and_the_review_still_runs(
    tmp_path: Path, key: str
) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    tampered = "id-\ud800"
    payload = _trade_payload(container, trade_id)
    payload["signal"][key] = tampered
    _execute(
        settings,
        "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
        (json.dumps(payload), trade_id),
    )

    result = _review_service(container).review(trade_id, idempotency_key=f"unsafe-{key}")

    assert result.status == "FOUND"
    envelope = json.loads(container.store.get_decision(result.decision_id).payload_json)  # type: ignore[union-attr]
    assert envelope["subject"][key] != tampered
    assert envelope["subject"]["timeframe"] == "4h"


# -- N6: a parseable but non-object audit body fails closed -------------------


def test_a_non_object_review_audit_fails_closed_without_duplicating(tmp_path: Path) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    first = _review_service(container).review(trade_id, idempotency_key="audit-object-1")
    assert first.status == "FOUND"
    assert len(_audit_payloads(container, "REVIEW_GENERATED")) == 1

    _execute(settings, "DROP TRIGGER IF EXISTS audit_events_no_update")
    _execute(
        settings,
        "UPDATE audit_events SET payload_json = '[]' WHERE event_type = 'REVIEW_GENERATED'",
    )
    _execute(
        settings,
        "CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
    )

    with pytest.raises(ReviewRejected) as excinfo:
        _review_service(container).review(trade_id, idempotency_key="audit-object-2")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    connection = sqlite3.connect(settings.data_dir / "oft.db")
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = 'REVIEW_GENERATED'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1


# -- N7: the orphan search never adopts a non-matching or malformed decision --


def test_the_orphan_search_does_not_adopt_a_non_matching_or_malformed_decision(
    tmp_path: Path,
) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    from test_review_flow import _CountingStub

    provider = _CountingStub(
        LLMStub(fixtures_dir=FIXTURES, overrides={LLMRole.REVIEW: "review_valid.json"})
    )
    # (a) same position but a different symbol: not this trade's review.
    wrong_symbol = _accepted_envelope(trade, decision_id="RVW-ORPHAN-SYMBOL", symbol="ETHUSDT")
    _save_unlinked(container, "RVW-ORPHAN-SYMBOL", wrong_symbol)
    # (b) an unreadable envelope that cannot be attributed to any trade.
    _save_unlinked(container, "RVW-ORPHAN-UNREADABLE", "not-json")

    result = _review_service(container, provider).review(
        trade_id, idempotency_key="orphan-negative"
    )

    assert provider.calls == 1
    assert result.decision_id not in {"RVW-ORPHAN-SYMBOL", "RVW-ORPHAN-UNREADABLE"}
    linked = container.store.get_trade_log(trade_id)
    assert linked is not None and linked.review_decision_id == result.decision_id
    audits = _audit_payloads(container, "REVIEW_GENERATED")
    assert [item["decision_id"] for item in audits] == [result.decision_id]
    # The seeded decisions stay unlinked: the link names the fresh decision.
    assert linked.review_decision_id != "RVW-ORPHAN-SYMBOL"
    assert linked.review_decision_id != "RVW-ORPHAN-UNREADABLE"
