"""Deterministic risk gate: stop required, stricter-of caps, named binding cap."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from oft.domain.risk import (
    DEFAULT_CAPS,
    BindingCap,
    RiskRequest,
    RiskVerdict,
    Verdict,
    evaluate_risk,
)

NOW = "2026-11-06T12:00:00.000Z"
FRESH = "2026-11-06T11:59:30.000Z"
STALE = "2026-11-06T11:58:00.000Z"


def _request(**overrides: object) -> RiskRequest:
    payload: dict[str, object] = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry_price": Decimal("70000.00"),
        "stop_price": Decimal("69000.00"),
        "quantity": Decimal("0.10"),
        "risk_pct": 0.8,
        "leverage": 2.0,
        "notional_usd": Decimal("7000.00"),
    }
    payload.update(overrides)
    return RiskRequest(**payload)  # type: ignore[arg-type]


def _evaluate(**overrides: object):
    kwargs: dict[str, object] = {
        "request": _request(),
        "caps": DEFAULT_CAPS,
        "equity": Decimal("10000.00"),
        "open_positions": 0,
        "daily_loss_usd": Decimal("0"),
        "mark_price": Decimal("70000.00"),
        "mark_as_of": FRESH,
        "now": NOW,
        "tick_size": Decimal("0.10"),
        "step_size": Decimal("0.001"),
        "min_notional": Decimal("5.00"),
    }
    kwargs.update(overrides)
    return evaluate_risk(**kwargs)  # type: ignore[arg-type]


def test_stop_is_required() -> None:
    verdict = _evaluate(request=_request(stop_price=None))

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "MISSING_STOP"
    assert verdict.allowed_quantity is None


def test_long_stop_must_be_below_entry() -> None:
    verdict = _evaluate(request=_request(stop_price=Decimal("71000.00")))

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "MISSING_STOP"


def test_short_stop_must_be_above_entry() -> None:
    good = _evaluate(
        request=_request(side="SHORT", stop_price=Decimal("71000.00"), quantity=Decimal("0.10"))
    )
    bad = _evaluate(
        request=_request(side="SHORT", stop_price=Decimal("69000.00"), quantity=Decimal("0.10"))
    )

    assert good.verdict in {"ALLOW", "REDUCE"}
    assert bad.verdict == "BLOCK"
    assert bad.binding_cap == "MISSING_STOP"


def test_stale_mark_blocks() -> None:
    verdict = _evaluate(mark_as_of=STALE)

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "STALE_MARK"
    assert "mark" in " ".join(verdict.stale_inputs).lower() or verdict.binding_cap == "STALE_MARK"


def test_max_positions_blocks() -> None:
    verdict = _evaluate(open_positions=DEFAULT_CAPS.max_concurrent_positions)

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "MAX_POSITIONS"


def test_daily_loss_blocks() -> None:
    verdict = _evaluate(daily_loss_usd=DEFAULT_CAPS.daily_loss_limit_usd)

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "DAILY_LOSS"


def test_per_trade_cap_reduces_size() -> None:
    # 0.8% of 10,000 = 80 USD risk; distance 4,000 => 0.02 max size.
    verdict = _evaluate(request=_request(quantity=Decimal("0.50"), stop_price=Decimal("66000.00")))

    assert verdict.verdict == "REDUCE"
    assert verdict.binding_cap == "PER_TRADE_RISK"
    assert verdict.allowed_quantity == Decimal("0.020")


def test_notional_cap_reduces_size() -> None:
    # 20% of 10,000 = 2,000 USD / 70,000 = 0.028571... => 0.028 at step 0.001.
    verdict = _evaluate(
        request=_request(quantity=Decimal("0.50"), stop_price=Decimal("69999.00")),
        caps=DEFAULT_CAPS,
    )

    assert verdict.verdict == "REDUCE"
    assert verdict.binding_cap == "MAX_NOTIONAL"
    assert verdict.allowed_quantity == Decimal("0.028")


def test_effective_size_takes_the_stricter_side() -> None:
    # Per-trade allows 0.08, notional allows 0.028: the smaller must win.
    verdict = _evaluate(request=_request(quantity=Decimal("0.50"), stop_price=Decimal("69999.00")))

    assert verdict.allowed_notional_usd <= DEFAULT_CAPS.max_notional_usd
    assert verdict.allowed_quantity is not None
    assert verdict.allowed_quantity * Decimal("70000.00") <= DEFAULT_CAPS.max_notional_usd


def test_leverage_is_clamped_to_the_cap() -> None:
    verdict = _evaluate(
        request=_request(leverage=10.0, quantity=Decimal("0.01"), stop_price=Decimal("69999.90"))
    )

    assert verdict.allowed_leverage == DEFAULT_CAPS.max_leverage
    assert verdict.binding_cap in {"MAX_LEVERAGE", "NONE"}


def test_min_notional_blocks_a_tiny_order() -> None:
    verdict = _evaluate(
        request=_request(quantity=Decimal("0.00001"), stop_price=Decimal("69999.99"))
    )

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "MIN_NOTIONAL"


def test_step_size_rounds_down_to_a_tradable_quantity() -> None:
    # 80 / 3,700 = 0.0216216... => 0.021 at step 0.001.
    verdict = _evaluate(
        request=_request(quantity=Decimal("0.0809"), stop_price=Decimal("66300.00"))
    )

    assert verdict.allowed_quantity == Decimal("0.021")


def test_tick_size_rounding_is_recorded() -> None:
    verdict = _evaluate(
        request=_request(entry_price=Decimal("69999.95"), stop_price=Decimal("69000.00"))
    )

    assert any("tick" in reason.lower() for reason in verdict.reasons)


def test_allow_when_requested_size_fits_all_caps() -> None:
    verdict = _evaluate(
        request=_request(quantity=Decimal("0.01"), stop_price=Decimal("69000.00"), leverage=2.0)
    )

    assert verdict.verdict == "ALLOW"
    assert verdict.binding_cap == "NONE"
    assert verdict.allowed_quantity == Decimal("0.010")


def test_quantity_derived_from_risk_percent_when_absent() -> None:
    verdict = _evaluate(
        request=_request(quantity=None, notional_usd=None, risk_pct=0.8),
    )

    assert verdict.verdict in {"ALLOW", "REDUCE"}
    assert verdict.allowed_quantity == Decimal("0.028")


def test_zero_quantity_request_is_blocked() -> None:
    verdict = _evaluate(request=_request(quantity=Decimal("0"), notional_usd=None))

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "MIN_NOTIONAL"


def test_verdict_carries_ttl_and_inputs_hash() -> None:
    first = _evaluate()
    second = _evaluate()

    assert first.expires_at == "2026-11-06T12:00:30.000Z"
    assert first.inputs_hash == second.inputs_hash
    assert first.computed_at == NOW


def test_custom_caps_are_applied() -> None:
    from dataclasses import replace

    tightened = replace(DEFAULT_CAPS, per_trade_risk_pct=0.1)
    verdict = _evaluate(request=_request(quantity=Decimal("0.50")), caps=tightened)

    assert verdict.verdict == "REDUCE"
    assert verdict.binding_cap == "PER_TRADE_RISK"


def test_zero_equity_is_blocked() -> None:
    verdict = _evaluate(equity=Decimal("0"))

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "MIN_NOTIONAL"


def test_unknown_side_is_rejected() -> None:
    with pytest.raises(ValueError):
        _request(side="SIDEWAYS")


@pytest.mark.parametrize(
    "overrides",
    [
        {"entry_price": Decimal("0")},
        {"quantity": Decimal("-0.1")},
        {"leverage": 0.0},
    ],
)
def test_invalid_request_fields_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _request(**overrides)


def test_zero_mark_price_blocks() -> None:
    verdict = _evaluate(mark_price=Decimal("0"))

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "STALE_MARK"


def test_zero_step_and_tick_sizes_are_no_ops() -> None:
    verdict = _evaluate(
        request=_request(quantity=Decimal("0.01")),
        step_size=Decimal("0"),
        tick_size=Decimal("0"),
    )

    assert verdict.verdict == "ALLOW"


def test_unaligned_stop_is_recorded_as_rounded() -> None:
    verdict = _evaluate(request=_request(stop_price=Decimal("68500.05"), quantity=Decimal("0.01")))

    assert any("stop price" in reason for reason in verdict.reasons)
    assert verdict.stop_price == Decimal("68500.00")


def test_missing_daily_loss_input_is_recorded_as_unevaluated() -> None:
    # Slice 11 has no realized-PnL source: a missing daily-loss input must be
    # recorded rather than silently treated as zero.
    verdict = _evaluate(daily_loss_usd=None)

    assert verdict.verdict in {"ALLOW", "REDUCE"}
    assert "daily_loss_usd" in verdict.stale_inputs


def test_provided_daily_loss_still_blocks() -> None:
    verdict = _evaluate(daily_loss_usd=DEFAULT_CAPS.daily_loss_limit_usd)

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == BindingCap.DAILY_LOSS
    assert "daily_loss_usd" not in verdict.stale_inputs


def test_block_verdict_may_not_carry_an_allowed_quantity() -> None:
    with pytest.raises(ValueError):
        RiskVerdict(
            verdict=Verdict.BLOCK,
            requested_quantity=None,
            allowed_quantity=Decimal("0.01"),
            allowed_notional_usd=None,
            allowed_leverage=1.0,
            risk_amount_usd=Decimal("0"),
            binding_cap=BindingCap.MISSING_STOP,
            reasons=(),
            computed_at=NOW,
            expires_at=NOW,
            inputs_hash="h",
            stale_inputs=(),
            entry_price=Decimal("70000.00"),
            stop_price=None,
        )


@pytest.mark.parametrize("verdict", [Verdict.ALLOW, Verdict.REDUCE])
def test_sized_verdict_requires_a_positive_allowed_quantity(verdict: Verdict) -> None:
    for quantity in (None, Decimal("0"), Decimal("-0.01")):
        with pytest.raises(ValueError):
            RiskVerdict(
                verdict=verdict,
                requested_quantity=None,
                allowed_quantity=quantity,
                allowed_notional_usd=None,
                allowed_leverage=1.0,
                risk_amount_usd=Decimal("0"),
                binding_cap=BindingCap.NONE,
                reasons=(),
                computed_at=NOW,
                expires_at=NOW,
                inputs_hash="h",
                stale_inputs=(),
                entry_price=Decimal("70000.00"),
                stop_price=Decimal("69000.00"),
            )


def test_verdict_enums_match_the_common_contract() -> None:
    schema_path = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "common.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    risk_verdict = schema["$defs"]["riskVerdict"]["properties"]
    verdict_contract = schema["$defs"]["riskVerdictStatus"]["enum"]
    binding_contract = schema["$defs"]["bindingCap"]["enum"]

    assert risk_verdict["verdict"]["$ref"] == "#/$defs/riskVerdictStatus"
    assert risk_verdict["binding_cap"]["$ref"] == "#/$defs/bindingCap"
    assert {item.value for item in Verdict} == set(verdict_contract)
    assert {item.value for item in BindingCap} == set(binding_contract)
