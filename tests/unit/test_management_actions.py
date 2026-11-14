"""The one order-intent representation and its management classification."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from oft.application.execution_gateway import classify_intent
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import (
    _MANAGEMENT_HISTORY_MAX_BYTES,
    _MANAGEMENT_HISTORY_MAX_ENTRIES,
    _MANAGEMENT_HISTORY_TEXT_LIMIT,
    ManagementService,
    _bounded_management_history,
)
from oft.application.paper_broker import OrderIntent
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, PositionSide


def _intent(**overrides: object) -> OrderIntent:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "side": OrderSide.SELL,
        "order_type": OrderType.MARKET,
        "quantity": Decimal("0.04"),
        "price": None,
        "stop_price": Decimal("69000"),
        "take_profits": (),
        "leverage": 2.0,
        "decision_id": "MGD-1",
        "risk_verdict_id": "RISK-1",
        "approval_id": "APR-1",
        "idempotency_key": "mgmt-1",
        "position_side": PositionSide.LONG,
        "risk_amount_usd": Decimal("0"),
        "risk_pct": 0.0,
        "position_id": "POS-1",
    }
    values.update(overrides)
    return OrderIntent(**values)  # type: ignore[arg-type]


def test_partial_and_full_close_are_reduce_only_from_the_one_representation() -> None:
    for purpose in ("PARTIAL_CLOSE", "partial_close", "FULL_CLOSE"):
        intent = _intent(purpose=purpose)
        assert intent.purpose == purpose.upper()
        assert intent.reduce_only is True
        assert classify_intent(intent).risk_reducing is True


def test_an_add_is_not_reduce_only_and_is_risk_increasing() -> None:
    intent = _intent(purpose="ADD", side=OrderSide.BUY)

    assert intent.purpose == "ADD"
    assert intent.reduce_only is False
    assert classify_intent(intent).risk_reducing is False


def test_an_exit_stays_reduce_only() -> None:
    intent = _intent(purpose="EXIT")

    assert intent.reduce_only is True
    assert classify_intent(intent).risk_reducing is True


def test_an_entry_is_never_treated_as_a_reduce_only_management_order() -> None:
    intent = _intent(purpose="ENTRY", reduce_only=True)

    assert intent.purpose == "ENTRY"
    assert intent.reduce_only is False
    assert classify_intent(intent).risk_reducing is False


def test_the_intent_hash_binds_the_normalized_purpose() -> None:
    partial = _intent(purpose="partial_close")
    full = _intent(purpose="FULL_CLOSE")

    assert partial.hash() != full.hash()
    assert partial.hash() == _intent(purpose="PARTIAL_CLOSE").hash()


def test_the_close_reason_is_a_journal_fact_and_not_part_of_the_intent_hash() -> None:
    stopped = _intent(purpose="FULL_CLOSE", close_reason="STOP_TOUCH")
    manual = _intent(purpose="FULL_CLOSE", close_reason="MANUAL_CLOSE")

    assert stopped.close_reason == "STOP_TOUCH"
    assert manual.close_reason == "MANUAL_CLOSE"
    assert stopped.hash() == manual.hash()


def test_an_unknown_purpose_is_risk_increasing() -> None:
    intent = _intent(purpose="REBALANCE")

    assert intent.reduce_only is False
    assert classify_intent(intent).risk_reducing is False


def test_classification_is_idempotent_for_the_same_intent() -> None:
    intent = _intent(purpose="PARTIAL_CLOSE")
    assert classify_intent(intent) == classify_intent(intent)


@pytest.mark.parametrize("purpose", ("PARTIAL_CLOSE", "FULL_CLOSE"))
def test_a_reduce_only_purpose_derives_the_flag_even_when_the_caller_says_no(
    purpose: str,
) -> None:
    intent = _intent(purpose=purpose, reduce_only=False)
    assert intent.reduce_only is True


class _DecisionStore:
    """The one store method ``_save_decision`` needs for this unit test."""

    def __init__(self) -> None:
        self.records = []

    def save_decision(self, record):  # type: ignore[no-untyped-def]
        self.records.append(record)


def _service(tmp_path) -> tuple[ManagementService, _DecisionStore]:  # type: ignore[no-untyped-def]
    settings = Settings(env="test", data_dir=tmp_path)
    clock = FixedClock(datetime(2026, 10, 8, tzinfo=UTC))
    store = _DecisionStore()
    service = ManagementService(
        store=store,  # type: ignore[arg-type]
        clock=clock,
        provider=None,
        settings=settings,
        kill_switch=KillSwitch(settings=settings, store=store, clock=clock),  # type: ignore[arg-type]
    )
    return service, store


def test_management_decision_persistence_rejects_non_json_numbers(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """M5: a NaN envelope is never persisted as the non-JSON token ``NaN``."""
    service, store = _service(tmp_path)

    service._save_decision("MGD-OK", "VALID", "hash", {"value": 1.0}, "2026-10-08T00:00:00.000Z")
    assert len(store.records) == 1
    assert "NaN" not in store.records[0].payload_json

    with pytest.raises(ValueError):
        service._save_decision(
            "MGD-NAN",
            "VALID",
            "hash",
            {"value": float("nan")},
            "2026-10-08T00:00:00.000Z",
        )
    assert len(store.records) == 1


def test_management_history_is_bounded_for_the_prompt() -> None:
    """M6: many/oversized history entries are capped by count, text, and bytes."""
    entries = [
        {
            "decision_id": f"MGD-{index}",
            "action": "HOLD",
            "at": "2026-10-08T00:00:00.000Z",
            "executed_order_id": None,
            "note": f"{index}:" + "x" * 10_000,
            "rationale": "y" * 10_000,
        }
        for index in range(100)
    ]

    bounded = _bounded_management_history(entries)

    assert 0 < len(bounded) <= _MANAGEMENT_HISTORY_MAX_ENTRIES
    assert len(json.dumps(bounded, sort_keys=True).encode("utf-8")) <= (
        _MANAGEMENT_HISTORY_MAX_BYTES
    )
    assert all(len(entry["note"]) <= _MANAGEMENT_HISTORY_TEXT_LIMIT for entry in bounded)
    assert all(len(entry["rationale"]) <= _MANAGEMENT_HISTORY_TEXT_LIMIT for entry in bounded)
    assert bounded[-1]["decision_id"] == "MGD-99"
    assert bounded[-1]["note"].startswith("99:")


def test_management_history_ignores_malformed_entries() -> None:
    assert _bounded_management_history(None) == []
    assert _bounded_management_history({"note": "x"}) == []
    assert _bounded_management_history([1, "x", {"decision_id": "MGD-1"}]) == [
        {"decision_id": "MGD-1"}
    ]


def _result(**overrides: object):  # type: ignore[no-untyped-def]
    from oft.application.management_service import ManagementResult

    values: dict[str, object] = {
        "position_id": "POS-1",
        "decision_id": "MGD-1",
        "action": "HOLD",
        "status": "NO_ACTION",
        "executed": False,
        "reason_code": "HOLD",
    }
    values.update(overrides)
    return ManagementResult(**values)  # type: ignore[arg-type]


def test_management_result_rejects_contradictory_states() -> None:
    """M10: EXECUTED requires order+fill; non-executed forbids them."""
    import pytest

    with pytest.raises(ValueError):
        _result(status="EXECUTED", executed=False)
    with pytest.raises(ValueError):
        _result(status="EXECUTED", executed=True)
    with pytest.raises(ValueError):
        _result(status="BLOCKED", executed=True, order={"order_id": "ORD-1"}, fill={})
    with pytest.raises(ValueError):
        _result(action="SCALE_IN")
    with pytest.raises(ValueError):
        _result(status="UNKNOWN")
    executed = _result(
        action="PARTIAL_CLOSE",
        status="EXECUTED",
        executed=True,
        reason_code="PARTIAL_CLOSE",
        order={"order_id": "ORD-1"},
        fill={"fill_id": "FIL-1"},
    )
    assert executed.to_contract()["action"] == "PARTIAL_CLOSE"
    assert executed.to_contract()["status"] == "EXECUTED"


def test_management_result_binds_the_trade_log_and_reason_code() -> None:
    """MEDIUM 3: trade_log and reason_code are part of the result invariant."""
    with pytest.raises(ValueError):
        # An EXECUTED FULL_CLOSE without its journal evidence is the H2 shape.
        _result(
            action="FULL_CLOSE",
            status="EXECUTED",
            executed=True,
            reason_code="FULL_CLOSE",
            order={"order_id": "ORD-1"},
            fill={"fill_id": "FIL-1"},
        )
    with pytest.raises(ValueError):
        # A non-executed result never carries a trade log.
        _result(
            status="NO_ACTION",
            executed=False,
            reason_code="HOLD",
            trade_log={"trade_id": "TRD-1"},
        )
    with pytest.raises(ValueError):
        _result(reason_code="")
    with pytest.raises(ValueError):
        _result(reason_code="   ")
    executed = _result(
        action="FULL_CLOSE",
        status="EXECUTED",
        executed=True,
        reason_code="FULL_CLOSE",
        order={"order_id": "ORD-1"},
        fill={"fill_id": "FIL-1"},
        trade_log={"trade_id": "TRD-1"},
    )
    assert executed.to_contract()["trade_log"] == {"trade_id": "TRD-1"}


def test_a_management_update_record_requires_its_audits() -> None:
    """M13: an action can never be constructed without its audit trail."""
    import inspect

    from oft.ports.store import ManagementUpdateRecord

    parameters = inspect.signature(ManagementUpdateRecord).parameters
    assert parameters["audits"].default is inspect.Parameter.empty


def test_management_quality_never_reports_complete_without_risk_evidence() -> None:
    """M4: an executed action with no persisted verdict is PARTIAL, never COMPLETE."""
    from oft.api.routes.positions import _management_quality

    executed_without_risk = _result(
        action="PARTIAL_CLOSE",
        status="EXECUTED",
        executed=True,
        reason_code="PARTIAL_CLOSE",
        order={"order_id": "ORD-1"},
        fill={"fill_id": "FIL-1"},
        risk=None,
    )
    assert _management_quality(executed_without_risk) == "PARTIAL"
    assert _management_quality(_result(status="NO_ACTION", executed=False)) == "COMPLETE"
    assert (
        _management_quality(_result(status="BLOCKED", executed=False, risk={"verdict": "BLOCK"}))
        == "PARTIAL"
    )
