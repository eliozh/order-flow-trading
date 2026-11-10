"""Deterministic risk gate: stop required, stricter-of caps, named binding cap."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from oft.domain.risk import (
    DEFAULT_CAPS,
    BindingCap,
    RiskCaps,
    RiskRequest,
    RiskVerdict,
    Verdict,
    caps_loosened,
    evaluate_risk,
    validate_caps,
    verdict_inputs_hash,
)

NOW = "2026-11-06T12:00:00.000Z"
FRESH = "2026-11-06T11:59:30.000Z"
STALE = "2026-11-06T11:58:00.000Z"
EQUITY = Decimal("10000.00")


def _equity_limit(pct: float, equity: Decimal = EQUITY) -> Decimal:
    return equity * Decimal(str(pct)) / Decimal("100")


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
    assert "mark" in " ".join(verdict.stale_inputs).lower()


def test_max_positions_blocks() -> None:
    verdict = _evaluate(open_positions=DEFAULT_CAPS.max_concurrent_positions)

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "MAX_POSITIONS"


def test_daily_loss_blocks() -> None:
    # 3% of 10,000 equity = 300 USD; the limit is derived from equity.
    verdict = _evaluate(daily_loss_usd=_equity_limit(DEFAULT_CAPS.daily_loss_pct))

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
    max_notional = _equity_limit(DEFAULT_CAPS.max_notional_pct)

    assert verdict.allowed_notional_usd <= max_notional
    assert verdict.allowed_quantity is not None
    assert verdict.allowed_quantity * Decimal("70000.00") <= max_notional


def test_notional_cap_is_derived_from_equity() -> None:
    tighter = _evaluate(equity=Decimal("5000.00"))

    assert tighter.allowed_notional_usd is not None
    assert tighter.allowed_notional_usd <= _equity_limit(
        DEFAULT_CAPS.max_notional_pct, Decimal("5000")
    )


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


# -- the LLM proposal is a distinct strictest-of term (F3) -----------------
#
# Entry mark 70,000, stop 67,000 => a 3,000 distance. The per-trade cap is
# 0.8% of 10,000 = 80 USD => 0.026666... quantity. The notional cap allows
# 2,000/70,000 = 0.028571..., and leverage allows 50,000/70,000 = 0.714, so the
# proposal-vs-per-trade comparison is the only one that can bind here.


def test_proposal_below_the_per_trade_cap_binds_the_proposal() -> None:
    # 0.4% of 10,000 = 40 USD risk / 3,000 = 0.0133; the 0.8% cap is looser.
    verdict = _evaluate(
        request=_request(
            quantity=None, notional_usd=None, risk_pct=0.4, stop_price=Decimal("67000.00")
        )
    )

    assert verdict.verdict == "ALLOW"
    assert verdict.binding_cap == "NONE"
    assert verdict.allowed_quantity == Decimal("0.013")
    assert verdict.requested_quantity.quantize(Decimal("0.000001")) == Decimal("0.013333")


def test_proposal_above_the_per_trade_cap_reduces_to_the_cap() -> None:
    # 2.0% of 10,000 = 200 USD risk / 3,000 = 0.0666; the 0.8% cap must win.
    verdict = _evaluate(
        request=_request(
            quantity=None, notional_usd=None, risk_pct=2.0, stop_price=Decimal("67000.00")
        )
    )

    assert verdict.verdict == "REDUCE"
    assert verdict.binding_cap == "PER_TRADE_RISK"
    assert verdict.allowed_quantity == Decimal("0.026")
    assert verdict.requested_quantity.quantize(Decimal("0.000001")) == Decimal("0.066667")


def test_proposal_equal_to_the_per_trade_cap_is_not_reduced() -> None:
    # 0.8% proposal == 0.8% cap: no cap reduces the requested size.
    verdict = _evaluate(
        request=_request(
            quantity=None, notional_usd=None, risk_pct=0.8, stop_price=Decimal("67000.00")
        )
    )

    assert verdict.verdict == "ALLOW"
    assert verdict.binding_cap == "NONE"
    assert verdict.allowed_quantity == Decimal("0.026")
    assert verdict.requested_quantity.quantize(Decimal("0.000001")) == Decimal("0.026667")


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
    # The daily-loss limit is equity-derived: with zero equity the limit is 0,
    # so the measured zero loss already meets it (percentage-cap behavior).
    verdict = _evaluate(equity=Decimal("0"))

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "DAILY_LOSS"

    # Without a daily-loss measurement it still fails closed at MIN_NOTIONAL.
    unmeasured = _evaluate(equity=Decimal("0"), daily_loss_usd=None)

    assert unmeasured.verdict == "BLOCK"
    assert unmeasured.binding_cap == "MIN_NOTIONAL"


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
    verdict = _evaluate(daily_loss_usd=_equity_limit(DEFAULT_CAPS.daily_loss_pct))

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


# -- percentage caps, property-style boundaries ---------------------------


def test_default_caps_are_percentage_based() -> None:
    assert DEFAULT_CAPS.per_trade_risk_pct == 0.8
    assert DEFAULT_CAPS.max_concurrent_positions == 3
    assert DEFAULT_CAPS.daily_loss_pct == 3.0
    assert DEFAULT_CAPS.max_leverage == 5.0
    assert DEFAULT_CAPS.max_notional_pct == 20.0
    assert DEFAULT_CAPS.max_slippage_bps == 25.0
    assert DEFAULT_CAPS.max_spread_bps == 15.0
    assert DEFAULT_CAPS.stale_mark_seconds == 60
    assert not hasattr(DEFAULT_CAPS, "max_notional_usd")
    assert not hasattr(DEFAULT_CAPS, "daily_loss_limit_usd")


@pytest.mark.parametrize("pct", [0.1, 0.4, 0.8, 1.0, 2.5])
def test_per_trade_pct_scales_with_equity(pct: float) -> None:
    for equity in (Decimal("1000.00"), EQUITY, Decimal("25000.00")):
        caps = RiskCaps(per_trade_risk_pct=pct)
        # A wide stop keeps the per-trade cap stricter than notional/leverage.
        verdict = _evaluate(
            request=_request(quantity=Decimal("50"), stop_price=Decimal("61000.00")),
            caps=caps,
            equity=equity,
            step_size=Decimal("0.000001"),
            min_notional=Decimal("0.01"),
        )
        expected = (equity * Decimal(str(pct)) / Decimal("100")) / Decimal("9000")
        expected = expected.quantize(Decimal("0.000001"), rounding="ROUND_DOWN")
        assert verdict.binding_cap == "PER_TRADE_RISK"
        assert verdict.allowed_quantity == expected


@pytest.mark.parametrize("pct", [1.0, 20.0, 50.0, 100.0])
def test_notional_pct_boundary(pct: float) -> None:
    caps = RiskCaps(max_notional_pct=pct)
    verdict = _evaluate(
        request=_request(quantity=Decimal("10"), stop_price=Decimal("69999.00")),
        caps=caps,
    )
    limit = _equity_limit(pct)
    assert verdict.allowed_notional_usd is not None
    assert verdict.allowed_notional_usd <= limit
    # The effective quantity is the stricter of the notional and per-trade caps.
    assert verdict.allowed_quantity is not None
    assert verdict.allowed_quantity * Decimal("70000.00") <= limit


def test_the_notional_cap_equality_is_the_boundary() -> None:
    from dataclasses import replace

    # 7% of 10,000 = 700 USD / 70,000 = exactly 0.01 at a 0.001 step.
    caps = replace(DEFAULT_CAPS, max_notional_pct=7.0)
    verdict = _evaluate(
        request=_request(quantity=Decimal("10"), stop_price=Decimal("69999.00")),
        caps=caps,
    )

    assert verdict.binding_cap == "MAX_NOTIONAL"
    assert verdict.allowed_quantity == Decimal("0.010")
    assert verdict.allowed_notional_usd == _equity_limit(7.0)


def test_degraded_equity_inputs_are_named_stale() -> None:
    verdict = _evaluate(equity_stale_inputs=("mark", "equity"))

    assert "mark" in verdict.stale_inputs
    assert "equity" in verdict.stale_inputs
    # The degraded-equity names are merged with the other measured-input names.
    assert "spread_bps" in verdict.stale_inputs


@pytest.mark.parametrize("positions", [0, 1, 2])
def test_below_max_positions_is_not_blocked(positions: int) -> None:
    verdict = _evaluate(open_positions=positions)

    assert verdict.verdict in {"ALLOW", "REDUCE"}
    assert verdict.binding_cap != "MAX_POSITIONS"


def test_at_max_positions_is_blocked() -> None:
    verdict = _evaluate(open_positions=DEFAULT_CAPS.max_concurrent_positions)

    assert verdict.binding_cap == "MAX_POSITIONS"


@pytest.mark.parametrize("loss_pct", [0.0, 1.0, 2.999])
def test_under_daily_loss_limit_is_allowed(loss_pct: float) -> None:
    verdict = _evaluate(daily_loss_usd=_equity_limit(loss_pct))

    assert verdict.verdict in {"ALLOW", "REDUCE"}
    assert verdict.binding_cap != "DAILY_LOSS"


def test_at_daily_loss_limit_blocks() -> None:
    verdict = _evaluate(daily_loss_usd=_equity_limit(DEFAULT_CAPS.daily_loss_pct))

    assert verdict.binding_cap == "DAILY_LOSS"


@pytest.mark.parametrize("leverage", [1.0, 2.0, DEFAULT_CAPS.max_leverage])
def test_leverage_at_or_under_the_cap_is_kept(leverage: float) -> None:
    verdict = _evaluate(request=_request(leverage=leverage, quantity=Decimal("0.01")))

    assert verdict.allowed_leverage == leverage


@pytest.mark.parametrize("leverage", [DEFAULT_CAPS.max_leverage + 0.01, 10.0, 125.0])
def test_leverage_above_the_cap_is_clamped(leverage: float) -> None:
    verdict = _evaluate(request=_request(leverage=leverage, quantity=Decimal("0.01")))

    assert verdict.allowed_leverage == DEFAULT_CAPS.max_leverage


# -- spread, slippage, and stale inputs -----------------------------------


def test_missing_spread_is_stale_not_zero() -> None:
    verdict = _evaluate(spread_bps=None)

    assert verdict.verdict in {"ALLOW", "REDUCE"}
    assert "spread_bps" in verdict.stale_inputs


def test_missing_expected_slippage_is_stale_not_zero() -> None:
    verdict = _evaluate(expected_slippage_bps=None)

    assert verdict.verdict in {"ALLOW", "REDUCE"}
    assert "expected_slippage_bps" in verdict.stale_inputs


def test_zero_spread_and_slippage_are_measured_inputs() -> None:
    verdict = _evaluate(spread_bps=0.0, expected_slippage_bps=0.0)

    assert "spread_bps" not in verdict.stale_inputs
    assert "expected_slippage_bps" not in verdict.stale_inputs
    assert verdict.verdict in {"ALLOW", "REDUCE"}


@pytest.mark.parametrize("spread", [1.0, 7.5, 15.0])
def test_spread_at_or_under_cap_is_allowed(spread: float) -> None:
    verdict = _evaluate(spread_bps=spread)

    assert verdict.verdict in {"ALLOW", "REDUCE"}
    assert verdict.binding_cap != "SPREAD"


def test_spread_over_cap_blocks() -> None:
    verdict = _evaluate(spread_bps=DEFAULT_CAPS.max_spread_bps + 0.01)

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "SPREAD"
    assert verdict.allowed_quantity is None


@pytest.mark.parametrize("slippage", [0.0, 2.0, 25.0])
def test_slippage_at_or_under_cap_is_allowed(slippage: float) -> None:
    verdict = _evaluate(expected_slippage_bps=slippage)

    assert verdict.verdict in {"ALLOW", "REDUCE"}
    assert verdict.binding_cap != "SLIPPAGE"


def test_slippage_over_cap_blocks() -> None:
    verdict = _evaluate(expected_slippage_bps=DEFAULT_CAPS.max_slippage_bps + 0.01)

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "SLIPPAGE"


def test_daily_loss_missing_defaults_to_stale() -> None:
    verdict = _evaluate(daily_loss_usd=None)

    assert {"daily_loss_usd", "spread_bps", "expected_slippage_bps"} <= set(verdict.stale_inputs)


def test_all_money_values_are_exact_decimals() -> None:
    verdict = _evaluate(
        request=_request(quantity=Decimal("0.50"), stop_price=Decimal("69999.00")),
        spread_bps=1.0,
        expected_slippage_bps=1.0,
        daily_loss_usd=Decimal("0"),
    )

    assert isinstance(verdict.allowed_quantity, Decimal)
    assert isinstance(verdict.allowed_notional_usd, Decimal)
    assert isinstance(verdict.risk_amount_usd, Decimal)


# -- caps validation and serialization ------------------------------------


def test_valid_default_caps_pass_validation() -> None:
    assert validate_caps(DEFAULT_CAPS) == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"per_trade_risk_pct": 0.0},
        {"per_trade_risk_pct": 100.01},
        {"max_concurrent_positions": 0},
        {"daily_loss_pct": 0.0},
        {"daily_loss_pct": 101.0},
        {"max_leverage": 0.0},
        {"max_leverage": 126.0},
        {"max_notional_pct": 0.0},
        {"max_notional_pct": 100.01},
        {"max_slippage_bps": 0.0},
        {"max_spread_bps": 0.0},
        {"stale_mark_seconds": 0},
    ],
)
def test_invalid_caps_fail_validation(overrides: dict[str, object]) -> None:
    from dataclasses import replace

    assert validate_caps(replace(DEFAULT_CAPS, **overrides))


def test_caps_round_trip_through_the_state_document() -> None:
    caps = RiskCaps(per_trade_risk_pct=0.5, max_concurrent_positions=2)

    restored = RiskCaps.from_state(caps.to_state())

    assert restored == caps


def test_caps_from_state_json_round_trips_a_full_document() -> None:
    caps = RiskCaps(per_trade_risk_pct=0.5, max_concurrent_positions=2, max_spread_bps=10.0)

    restored = RiskCaps.from_state_json(json.dumps(caps.to_state()))

    assert restored == caps


def test_caps_from_state_rejects_a_non_object() -> None:
    with pytest.raises(ValueError):
        RiskCaps.from_state_json("[]")


def test_caps_from_state_rejects_corrupt_json() -> None:
    with pytest.raises(ValueError):
        RiskCaps.from_state_json("{not-json")


def test_caps_from_state_rejects_invalid_types() -> None:
    with pytest.raises(ValueError):
        RiskCaps.from_state({"per_trade_risk_pct": "not-a-number"})


def test_caps_from_state_rejects_unknown_keys() -> None:
    # A legacy absolute-dollar document (or any other drift) must fail closed
    # instead of being silently ignored.
    with pytest.raises(ValueError) as excinfo:
        RiskCaps.from_state({"max_notional_usd": "2000", "daily_loss_limit_usd": "300"})

    assert "max_notional_usd" in str(excinfo.value)


def test_caps_from_state_json_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        RiskCaps.from_state_json('{"per_trade_risk_pct": 0.8, "max_notional_usd": "2000"}')


def test_caps_from_state_json_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        RiskCaps.from_state_json('{"per_trade_risk_pct": 0}')


def test_merge_caps_applies_every_persisted_field() -> None:
    from dataclasses import replace

    from oft.domain.risk import merge_caps

    override = replace(DEFAULT_CAPS, per_trade_risk_pct=0.4, stale_mark_seconds=30)

    merged = merge_caps(DEFAULT_CAPS, override)

    assert merged == override


def test_negative_spread_blocks() -> None:
    verdict = _evaluate(spread_bps=-1.0)

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "SPREAD"


def test_negative_expected_slippage_blocks() -> None:
    verdict = _evaluate(expected_slippage_bps=-1.0)

    assert verdict.verdict == "BLOCK"
    assert verdict.binding_cap == "SLIPPAGE"


def test_caps_loosened_names_every_risky_direction() -> None:
    from dataclasses import replace

    looser = replace(
        DEFAULT_CAPS,
        per_trade_risk_pct=1.0,
        max_concurrent_positions=5,
        daily_loss_pct=5.0,
        max_leverage=10.0,
        max_notional_pct=30.0,
        max_slippage_bps=50.0,
        max_spread_bps=30.0,
        stale_mark_seconds=120,
    )

    assert set(caps_loosened(DEFAULT_CAPS, looser)) == {
        "per_trade_risk_pct",
        "max_concurrent_positions",
        "daily_loss_pct",
        "max_leverage",
        "max_notional_pct",
        "max_slippage_bps",
        "max_spread_bps",
        "stale_mark_seconds",
    }


def test_caps_tightened_is_not_loosening() -> None:
    from dataclasses import replace

    tighter = replace(
        DEFAULT_CAPS,
        per_trade_risk_pct=0.4,
        max_concurrent_positions=2,
        daily_loss_pct=1.0,
        max_leverage=2.0,
        max_notional_pct=10.0,
        max_slippage_bps=10.0,
        max_spread_bps=5.0,
        stale_mark_seconds=30,
    )

    assert caps_loosened(DEFAULT_CAPS, tighter) == ()


# -- `oft risk show|set|validate` (D4) ------------------------------------


def _risk_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *args: str):
    from typer.testing import CliRunner

    from oft.cli.main import app as cli_app

    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    return CliRunner().invoke(cli_app, ["risk", *args])


def _risk_store(tmp_path: Path):
    from oft.adapters.sqlite_store import SqliteStore
    from oft.config import Settings

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    return settings, store


def test_risk_show_reports_config_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _risk_cli(monkeypatch, tmp_path, "show", "--json")

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["source"] == "config"
    assert data["caps"]["per_trade_risk_pct"] == 0.8
    assert data["caps"]["max_notional_pct"] == 20.0
    assert data["defaults"]["stale_mark_seconds"] == 60


def test_risk_show_human_output_lists_the_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _risk_cli(monkeypatch, tmp_path, "show")

    assert result.exit_code == 0, result.output
    assert "risk caps · source config" in result.output
    assert "per_trade_risk_pct" in result.output
    assert "defaults:" in result.output


def test_risk_validate_human_output_reports_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _risk_cli(monkeypatch, tmp_path, "validate")

    assert result.exit_code == 0, result.output
    assert "PASS · risk caps valid · source config" in result.output


def test_risk_set_without_any_cap_is_a_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _risk_cli(monkeypatch, tmp_path, "set")

    assert result.exit_code != 0
    assert "no risk cap was provided to set" in result.output
    _, store = _risk_store(tmp_path)
    assert store.get_system_state("risk_caps") is None


def test_risk_set_tightening_persists_and_audits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _risk_cli(monkeypatch, tmp_path, "set", "--per-trade-pct", "0.4", "--json")

    assert result.exit_code == 0, result.output
    _, store = _risk_store(tmp_path)
    assert json.loads(store.get_system_state("risk_caps") or "{}")["per_trade_risk_pct"] == 0.4
    events = store.list_audit_events(limit=50)
    assert any(event.event_type == "RISK_CAPS_UPDATED" for event in events)


def test_risk_set_loosening_requires_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = _risk_cli(monkeypatch, tmp_path, "set", "--per-trade-pct", "1.0", "--json")
    assert refused.exit_code != 0
    _, store = _risk_store(tmp_path)
    assert store.get_system_state("risk_caps") is None

    accepted = _risk_cli(
        monkeypatch,
        tmp_path,
        "set",
        "--per-trade-pct",
        "1.0",
        "--confirm",
        "--reason",
        "operator review",
        "--json",
    )
    assert accepted.exit_code == 0, accepted.output
    payload = json.loads(store.get_system_state("risk_caps") or "{}")
    assert payload["per_trade_risk_pct"] == 1.0
    audit = store.list_audit_events(limit=10)[0]
    assert audit.event_type == "RISK_CAPS_UPDATED"
    assert "operator review" in audit.payload_json


def test_risk_set_rejects_invalid_ranges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _risk_cli(monkeypatch, tmp_path, "set", "--per-trade-pct", "0", "--json")

    assert result.exit_code != 0
    _, store = _risk_store(tmp_path)
    assert store.get_system_state("risk_caps") is None


def test_risk_set_shows_persisted_source_after_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _risk_cli(monkeypatch, tmp_path, "set", "--max-leverage", "2", "--json")

    shown = _risk_cli(monkeypatch, tmp_path, "show", "--json")

    data = json.loads(shown.stdout)["data"]
    assert data["source"] == "persisted"
    assert data["caps"]["max_leverage"] == 2.0


def test_risk_validate_reports_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _risk_cli(monkeypatch, tmp_path, "validate", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["status"] == "PASS"


def test_settings_precedence_persisted_over_env(tmp_path: Path) -> None:
    from dataclasses import replace

    from oft.adapters.sqlite_store import SqliteStore
    from oft.application.entry_service import effective_caps
    from oft.config import Settings

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data", risk_per_trade_pct=0.5)
    store = SqliteStore(settings)
    store.initialize()

    configured, source = effective_caps(settings, store)
    assert configured.per_trade_risk_pct == 0.5
    assert source == "config"

    caps = replace(DEFAULT_CAPS, per_trade_risk_pct=0.4)
    from oft.ports.store import AuditEventDraft

    store.save_risk_caps(
        value=json.dumps(caps.to_state(), sort_keys=True),
        updated_at="2026-11-06T12:00:00.000Z",
        audit=AuditEventDraft(
            event_type="RISK_CAPS_UPDATED",
            actor={"kind": "USER", "id": "test"},
            subject={"kind": "SYSTEM", "id": "risk_caps"},
            payload={"after": caps.to_state()},
            created_at="2026-11-06T12:00:00.000Z",
        ),
    )

    persisted, source = effective_caps(settings, store)
    assert persisted.per_trade_risk_pct == 0.4
    assert source == "persisted"


def test_an_invalid_risk_env_value_is_config_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oft.config import ConfigError, load_settings

    monkeypatch.setenv("OFT_RISK_PER_TRADE_PCT", "0")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    assert excinfo.value.code == "CONFIG_INVALID"


# -- non-finite caps fail closed everywhere (S1/Python-1) ------------------


@pytest.mark.parametrize(
    "field",
    [
        "per_trade_risk_pct",
        "daily_loss_pct",
        "max_notional_pct",
        "max_leverage",
        "max_slippage_bps",
        "max_spread_bps",
    ],
)
def test_nan_and_infinity_caps_fail_validation(field: str) -> None:
    from dataclasses import replace

    for value in (float("nan"), float("inf"), float("-inf")):
        assert validate_caps(replace(DEFAULT_CAPS, **{field: value}))


def test_boolean_caps_fail_validation() -> None:
    from dataclasses import replace

    assert validate_caps(replace(DEFAULT_CAPS, per_trade_risk_pct=True))
    assert validate_caps(replace(DEFAULT_CAPS, max_concurrent_positions=True))
    assert validate_caps(replace(DEFAULT_CAPS, stale_mark_seconds=True))


def test_out_of_range_bps_caps_fail_validation() -> None:
    from dataclasses import replace

    assert validate_caps(replace(DEFAULT_CAPS, max_slippage_bps=10000.01))
    assert validate_caps(replace(DEFAULT_CAPS, max_spread_bps=10000.01))


@pytest.mark.parametrize(
    "payload",
    [
        {"per_trade_risk_pct": float("nan")},
        {"per_trade_risk_pct": float("inf")},
        {"max_spread_bps": float("inf")},
        {"max_slippage_bps": float("nan")},
        {"per_trade_risk_pct": True},
        {"max_concurrent_positions": True},
        {"max_concurrent_positions": 1.5},
        {"stale_mark_seconds": float("inf")},
        {"max_leverage": "1e400"},
    ],
)
def test_caps_from_state_rejects_non_finite_or_boolean(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RiskCaps.from_state(payload)
    with pytest.raises(ValueError):
        RiskCaps.from_state_json(json.dumps(payload))


def test_a_non_finite_risk_env_value_is_config_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oft.config import ConfigError, load_settings

    monkeypatch.setenv("OFT_RISK_MAX_SPREAD_BPS", "inf")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    assert excinfo.value.code == "CONFIG_INVALID"


def test_an_out_of_range_bps_env_value_is_config_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oft.config import ConfigError, load_settings

    monkeypatch.setenv("OFT_RISK_MAX_SLIPPAGE_BPS", "10001")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    assert excinfo.value.code == "CONFIG_INVALID"


def test_risk_set_rejects_nan_without_persisting_or_auditing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _risk_cli(monkeypatch, tmp_path, "set", "--per-trade-pct", "nan", "--json")

    assert result.exit_code != 0
    _, store = _risk_store(tmp_path)
    assert store.get_system_state("risk_caps") is None
    assert not any(
        event.event_type == "RISK_CAPS_UPDATED" for event in store.list_audit_events(limit=20)
    )


def test_risk_validate_never_reports_pass_for_a_persisted_nan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = _risk_store(tmp_path)
    store.set_system_state(
        "risk_caps",
        '{"per_trade_risk_pct": NaN}',
        updated_at="2026-11-06T12:00:00.000Z",
    )

    result = _risk_cli(monkeypatch, tmp_path, "validate", "--json")

    assert result.exit_code != 0
    assert "PASS" not in result.stdout


# -- persisted caps merge over the config base (Python-2/R3) --------------


def test_a_partial_persisted_row_keeps_the_env_tightened_caps(tmp_path: Path) -> None:
    from oft.adapters.sqlite_store import SqliteStore
    from oft.application.entry_service import effective_caps
    from oft.config import Settings

    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        risk_max_leverage=2.0,
    )
    store = SqliteStore(settings)
    store.initialize()
    store.set_system_state(
        "risk_caps",
        json.dumps({"per_trade_risk_pct": 0.5}),
        updated_at="2026-11-06T12:00:00.000Z",
    )

    caps, source = effective_caps(settings, store)

    assert source == "persisted"
    assert caps.per_trade_risk_pct == 0.5
    # The env-tightened leverage must not be reset to the 5x dataclass default.
    assert caps.max_leverage == 2.0
    assert caps.max_spread_bps == DEFAULT_CAPS.max_spread_bps


# -- the verdict input digest binds the measured inputs (Python-5) ---------


def test_verdict_inputs_hash_covers_spread_slippage_and_daily_loss() -> None:
    base = verdict_inputs_hash(_request(), equity=EQUITY, open_positions=0)

    assert verdict_inputs_hash(_request(), equity=EQUITY, open_positions=0, spread_bps=1.0) != base
    assert (
        verdict_inputs_hash(_request(), equity=EQUITY, open_positions=0, expected_slippage_bps=1.0)
        != base
    )
    assert (
        verdict_inputs_hash(
            _request(), equity=EQUITY, open_positions=0, daily_loss_usd=Decimal("1")
        )
        != base
    )
    # An unmeasured input is distinct from a measured zero.
    assert verdict_inputs_hash(
        _request(), equity=EQUITY, open_positions=0, spread_bps=None
    ) != verdict_inputs_hash(_request(), equity=EQUITY, open_positions=0, spread_bps=0.0)


# -- stale-mark boundary (T3) ---------------------------------------------


@pytest.mark.parametrize(
    ("mark_as_of", "blocked"),
    [
        ("2026-11-06T11:59:01.000Z", False),  # 59 s
        ("2026-11-06T11:59:00.000Z", False),  # 60 s: the edge is not stale
        ("2026-11-06T11:58:59.000Z", True),  # 61 s
    ],
)
def test_stale_mark_boundary_is_strict(mark_as_of: str, blocked: bool) -> None:
    verdict = _evaluate(mark_as_of=mark_as_of)

    assert (verdict.binding_cap == "STALE_MARK") is blocked


def test_a_custom_stale_mark_budget_changes_the_outcome() -> None:
    from dataclasses import replace

    verdict = _evaluate(
        caps=replace(DEFAULT_CAPS, stale_mark_seconds=30),
        mark_as_of="2026-11-06T11:59:29.000Z",
    )

    assert verdict.binding_cap == "STALE_MARK"


# -- RISK_CAPS_UPDATED before/after/loosened (T4) --------------------------


def test_risk_set_audits_before_after_and_the_loosened_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tightened = _risk_cli(monkeypatch, tmp_path, "set", "--per-trade-pct", "0.4", "--json")
    assert tightened.exit_code == 0, tightened.output
    _, store = _risk_store(tmp_path)
    first = json.loads(store.list_audit_events(limit=1)[0].payload_json)["payload"]
    assert first["before"]["per_trade_risk_pct"] == 0.8
    assert first["after"]["per_trade_risk_pct"] == 0.4
    assert first["loosened"] == []

    loosened = _risk_cli(
        monkeypatch,
        tmp_path,
        "set",
        "--per-trade-pct",
        "1.0",
        "--confirm",
        "--reason",
        "operator review",
        "--json",
    )
    assert loosened.exit_code == 0, loosened.output
    second = json.loads(store.list_audit_events(limit=1)[0].payload_json)["payload"]
    assert second["before"]["per_trade_risk_pct"] == 0.4
    assert second["after"]["per_trade_risk_pct"] == 1.0
    assert second["loosened"] == ["per_trade_risk_pct"]
    assert second["reason"] == "operator review"
