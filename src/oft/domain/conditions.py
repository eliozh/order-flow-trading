"""The closed deterministic condition registry (ADR-0006, ADR-0011).

The LLM may select one of the 19 contract ``conditionType`` values but cannot
invent a new one. This module validates required parameters, assigns the
required-data set, and evaluates a condition against recorded exact-decimal
inputs. It is pure: standard library plus :mod:`decimal`, no adapters.

Missing data is never zero. A condition whose required input is absent is
``evaluable=False`` and ``met=False``; it can never be treated as satisfied.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

SESSION_STATES: frozenset[str] = frozenset(
    {"PRE_MARKET", "REGULAR", "AFTER_HOURS", "CLOSED", "NO_TRADING", "UNKNOWN"}
)
COMPARATORS: frozenset[str] = frozenset({">", ">=", "<", "<="})
_DRAFT_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "price",
        "price_low",
        "price_high",
        "threshold",
        "unit",
        "comparator",
        "lookback_bars",
        "session_states",
        "note",
    }
)
_DESCRIPTIVE_KEYS: frozenset[str] = frozenset({"unit", "note"})
_DECIMAL_PATTERN = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")


class ConditionType(StrEnum):
    PRICE_CROSS_ABOVE = "PRICE_CROSS_ABOVE"
    PRICE_CROSS_BELOW = "PRICE_CROSS_BELOW"
    PRICE_INSIDE_ZONE = "PRICE_INSIDE_ZONE"
    PRICE_TOUCH_ZONE = "PRICE_TOUCH_ZONE"
    PRICE_OUTSIDE_ZONE = "PRICE_OUTSIDE_ZONE"
    DELTA_ABOVE = "DELTA_ABOVE"
    DELTA_BELOW = "DELTA_BELOW"
    CVD_ABOVE = "CVD_ABOVE"
    CVD_BELOW = "CVD_BELOW"
    CVD_DIVERGENCE = "CVD_DIVERGENCE"
    OI_DELTA_ABOVE = "OI_DELTA_ABOVE"
    OI_DELTA_BELOW = "OI_DELTA_BELOW"
    FUNDING_ABOVE = "FUNDING_ABOVE"
    FUNDING_BELOW = "FUNDING_BELOW"
    LIQUIDATION_NOTIONAL_ABOVE = "LIQUIDATION_NOTIONAL_ABOVE"
    VOLUME_ABOVE = "VOLUME_ABOVE"
    FOOTPRINT_IMBALANCE_ABOVE = "FOOTPRINT_IMBALANCE_ABOVE"
    SESSION_STATE = "SESSION_STATE"
    TIME_WINDOW = "TIME_WINDOW"


class ConditionParamsError(ValueError):
    """A draft or persisted condition does not satisfy its type's schema."""

    code = "VALIDATION_FAILED"

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.field = field


@dataclass(frozen=True)
class EvaluationContext:
    """Recorded inputs for a one-off display evaluation.

    ``None`` means the input is absent, never zero. Every decimal is exact.
    """

    as_of: str | None = None
    last_price: Decimal | None = None
    previous_price: Decimal | None = None
    tick_size: Decimal | None = None
    delta: Decimal | None = None
    cvd: Decimal | None = None
    cvd_series: tuple[Decimal, ...] | None = None
    price_series: tuple[Decimal, ...] | None = None
    oi_delta_pct: Decimal | None = None
    funding_rate: Decimal | None = None
    liquidation_notional: Decimal | None = None
    volume: Decimal | None = None
    footprint_imbalance: Decimal | None = None
    session_state: str | None = None
    # Per-bar series for the lookback-aware metrics. Each series is aligned to
    # ``bar_timestamps``; a hole makes the whole series absent, never zero.
    bar_timestamps: tuple[str, ...] | None = None
    delta_series: tuple[Decimal, ...] | None = None
    volume_series: tuple[Decimal, ...] | None = None
    oi_series: tuple[Decimal, ...] | None = None


@dataclass(frozen=True)
class ConditionEvaluation:
    condition_id: str
    type: str
    met: bool
    observed_value: str | None
    threshold: str | None
    evaluable: bool
    unavailable_reason: str | None
    data_quality: str
    note: str
    evaluated_at: str | None

    def to_contract(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "type": self.type,
            "met": self.met,
            "observed_value": self.observed_value,
            "threshold": self.threshold,
            "evaluable": self.evaluable,
            "unavailable_reason": self.unavailable_reason,
            "data_quality": self.data_quality,
            "note": self.note,
            "evaluated_at": self.evaluated_at,
        }


@dataclass(frozen=True)
class ValidatedCondition:
    """The canonical, persistable shape of a validated draft."""

    type: ConditionType
    params: Mapping[str, Any]
    required_data: tuple[str, ...]


@dataclass(frozen=True)
class MappedCondition:
    """An invalidation draft mapped onto a real registry type."""

    type: ConditionType
    params: Mapping[str, Any]
    window_start: str
    window_end: str
    required_data: tuple[str, ...]
    evaluable: bool
    unavailable_reason: str | None


@dataclass(frozen=True)
class _Outcome:
    met: bool
    observed_value: str | None
    threshold: str | None
    note: str
    evaluable: bool = True
    unavailable_reason: str | None = None
    data_quality: str = "COMPLETE"


class ConditionLike(Protocol):
    @property
    def condition_id(self) -> str: ...

    @property
    def type(self) -> str: ...

    @property
    def params(self) -> Mapping[str, Any]: ...

    @property
    def window_start(self) -> str: ...

    @property
    def window_end(self) -> str: ...

    @property
    def evaluable(self) -> bool: ...

    @property
    def unavailable_reason(self) -> str | None: ...


Evaluator = Callable[[Mapping[str, Any], str, str, EvaluationContext], _Outcome]


@dataclass(frozen=True)
class ConditionSpec:
    type: ConditionType
    required_params: tuple[str, ...]
    optional_params: tuple[str, ...]
    required_data: frozenset[str]
    evaluator: Evaluator = field(repr=False)


# -- exact decimal helpers --------------------------------------------------


def _required_decimal(value: object, field_name: str) -> str:
    if value is None:
        raise ConditionParamsError(f"{field_name} is required", field=field_name)
    if not isinstance(value, str) or not _DECIMAL_PATTERN.fullmatch(value):
        raise ConditionParamsError(
            f"{field_name} must be an exact decimal string", field=field_name
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:  # pragma: no cover - the pattern excludes this
        raise ConditionParamsError(
            f"{field_name} must be an exact decimal string", field=field_name
        ) from exc
    if not parsed.is_finite():  # pragma: no cover - the pattern excludes NaN/Infinity
        raise ConditionParamsError(f"{field_name} must be a finite decimal", field=field_name)
    return format(parsed, "f")


def _positive_decimal(value: object, field_name: str) -> str:
    canonical = _required_decimal(value, field_name)
    if Decimal(canonical) <= 0:
        raise ConditionParamsError(f"{field_name} must be positive", field=field_name)
    return canonical


def _unit_interval_decimal(value: object, field_name: str) -> str:
    canonical = _required_decimal(value, field_name)
    parsed = Decimal(canonical)
    if parsed < 0 or parsed > 1:
        raise ConditionParamsError(f"{field_name} must be within [0, 1]", field=field_name)
    return canonical


def _required_int(value: object, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConditionParamsError(f"{field_name} must be an integer", field=field_name)
    if value < minimum:
        raise ConditionParamsError(f"{field_name} must be at least {minimum}", field=field_name)
    return value


def _session_state_list(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConditionParamsError(
            "session_states must be a non-empty list", field="session_states"
        )
    states: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in SESSION_STATES:
            raise ConditionParamsError(
                f"unsupported session state {item!r}", field="session_states"
            )
        states.append(item)
    return states


def _comparator(params: Mapping[str, Any], default: str) -> str:
    value = params.get("comparator")
    return default if value is None else str(value)


def _format(value: Decimal) -> str:
    return format(value, "f")


def _quantize(value: Decimal, tick_size: Decimal) -> Decimal:
    if not tick_size.is_finite() or tick_size <= 0:
        raise ValueError("tick_size must be a positive finite decimal")
    return value.quantize(tick_size, rounding=ROUND_HALF_UP)


_COMPARATOR_FUNCS: Mapping[str, Callable[[Decimal, Decimal], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


def _compare(value: Decimal, threshold: Decimal, comparator: str) -> bool:
    return _COMPARATOR_FUNCS[comparator](value, threshold)


# -- evaluators -------------------------------------------------------------


def _eval_price_cross_above(
    params: Mapping[str, Any], _start: str, _end: str, context: EvaluationContext
) -> _Outcome:
    return _price_cross(params, context, above=True)


def _eval_price_cross_below(
    params: Mapping[str, Any], _start: str, _end: str, context: EvaluationContext
) -> _Outcome:
    return _price_cross(params, context, above=False)


def _price_cross(params: Mapping[str, Any], context: EvaluationContext, *, above: bool) -> _Outcome:
    if context.previous_price is None:
        return _Outcome(
            met=False,
            observed_value=None,
            threshold=str(params.get("price")),
            note="previous price is unavailable",
            evaluable=False,
            unavailable_reason="previous price is unavailable",
            data_quality="PARTIAL",
        )
    last_price = context.last_price
    tick_size = context.tick_size
    if last_price is None or tick_size is None:
        return _Outcome(
            met=False,
            observed_value=None,
            threshold=str(params.get("price")),
            note="price data is unavailable",
            evaluable=False,
            unavailable_reason="price data is unavailable",
            data_quality="MISSING",
        )
    threshold = _quantize(Decimal(_positive_decimal(params.get("price"), "price")), tick_size)
    previous = _quantize(context.previous_price, tick_size)
    last = _quantize(last_price, tick_size)
    met = previous < threshold <= last if above else previous > threshold >= last
    return _Outcome(
        met=met,
        observed_value=_format(last),
        threshold=_format(threshold),
        note=(
            "price crossed above the level"
            if above and met
            else "price crossed below the level"
            if met
            else "price did not cross the level"
        ),
    )


def _eval_inside_zone(
    params: Mapping[str, Any], _start: str, _end: str, context: EvaluationContext
) -> _Outcome:
    return _zone(params, context, mode="inside")


def _eval_touch_zone(
    params: Mapping[str, Any], _start: str, _end: str, context: EvaluationContext
) -> _Outcome:
    return _zone(params, context, mode="inside")


def _eval_outside_zone(
    params: Mapping[str, Any], _start: str, _end: str, context: EvaluationContext
) -> _Outcome:
    return _zone(params, context, mode="outside")


def _zone(params: Mapping[str, Any], context: EvaluationContext, *, mode: str) -> _Outcome:
    last_price = context.last_price
    tick_size = context.tick_size
    if last_price is None or tick_size is None:
        return _Outcome(
            met=False,
            observed_value=None,
            threshold=None,
            note="price data is unavailable",
            evaluable=False,
            unavailable_reason="price data is unavailable",
            data_quality="MISSING",
        )
    low = _quantize(
        Decimal(_positive_decimal(params.get("price_low"), "price_low")),
        tick_size,
    )
    high = _quantize(
        Decimal(_positive_decimal(params.get("price_high"), "price_high")),
        tick_size,
    )
    observed = _quantize(last_price, tick_size)
    met = low <= observed <= high if mode == "inside" else observed < low or observed > high
    return _Outcome(
        met=met,
        observed_value=_format(observed),
        threshold=f"{_format(low)}-{_format(high)}",
        note=(
            "price is inside the zone"
            if mode == "inside" and met
            else "price is outside the zone"
            if mode == "outside" and met
            else "price is not in the required zone"
        ),
    )


def _threshold_evaluator(field_name: str, default_comparator: str) -> Evaluator:
    def evaluate(
        params: Mapping[str, Any], _start: str, _end: str, context: EvaluationContext
    ) -> _Outcome:
        value = getattr(context, field_name)
        threshold = Decimal(_required_decimal(params.get("threshold"), "threshold"))
        if value is None:
            return _Outcome(
                met=False,
                observed_value=None,
                threshold=_format(threshold),
                note=f"{field_name} is unavailable",
                evaluable=False,
                unavailable_reason=f"{field_name} is unavailable",
                data_quality="PARTIAL",
            )
        comparator = _comparator(params, default_comparator)
        return _Outcome(
            met=_compare(value, threshold, comparator),
            observed_value=_format(value),
            threshold=_format(threshold),
            note=f"{field_name} {comparator} {_format(threshold)}",
        )

    return evaluate


def _windowed_value(
    context: EvaluationContext,
    start: str,
    end: str,
    lookback: int,
    series: tuple[Decimal, ...],
    reduction: str,
    label: str,
) -> tuple[Decimal | None, str | None]:
    """Reduce the last ``lookback`` bars inside the condition window.

    Bars after ``as_of`` or outside ``[start, end)`` are never used. A short or
    unaligned series is reported as absent rather than zero-filled.
    """
    timestamps = context.bar_timestamps
    if any(point is None for point in series):
        return None, f"{label} has a gap in the per-bar series"
    if timestamps is None or len(timestamps) != len(series):
        return None, f"{label} bar timestamps are unavailable"
    if context.as_of is None:
        return None, "as_of is unavailable"
    try:
        window_start = _parse(start)
        window_end = _parse(end)
        as_of = _parse(context.as_of)
        selected = [
            series[index]
            for index, raw in enumerate(timestamps)
            if window_start <= _parse(raw) < window_end and _parse(raw) <= as_of
        ]
    except (TypeError, ValueError):
        return None, f"{label} bar timestamps are malformed"
    if len(selected) < lookback:
        return None, f"lookback_bars {lookback} exceeds the available bars"
    window = selected[-lookback:]
    if reduction == "sum":
        return sum(window, Decimal(0)), None
    if reduction == "last":
        return window[-1], None
    first, last = window[0], window[-1]
    if first == 0:
        return None, f"{label} is unavailable"
    return (last - first) / first * Decimal(100), None


def _lookback_evaluator(
    *,
    label: str,
    series: Callable[[EvaluationContext], tuple[Decimal, ...] | None],
    reduction: str,
    default_comparator: str,
) -> Evaluator:
    """Compare a metric honouring the condition's ``lookback_bars``.

    Only the per-bar series is trusted. The recorded scalar aggregate is a
    whole-window number and cannot stand in for a missing series: doing so
    would silently ignore the requested lookback (and hide recorded gaps).
    """

    def evaluate(
        params: Mapping[str, Any], start: str, end: str, context: EvaluationContext
    ) -> _Outcome:
        lookback = _required_int(params.get("lookback_bars"), "lookback_bars", 1)
        threshold = Decimal(_required_decimal(params.get("threshold"), "threshold"))
        recorded = series(context)
        if recorded is None:
            missing_reason = f"{label} per-bar series is unavailable"
            return _Outcome(
                met=False,
                observed_value=None,
                threshold=_format(threshold),
                note=missing_reason,
                evaluable=False,
                unavailable_reason=missing_reason,
                data_quality="PARTIAL",
            )
        value, reason = _windowed_value(context, start, end, lookback, recorded, reduction, label)
        if reason is not None:
            return _Outcome(
                met=False,
                observed_value=None,
                threshold=_format(threshold),
                note=reason,
                evaluable=False,
                unavailable_reason=reason,
                data_quality="PARTIAL",
            )
        if value is None:
            return _Outcome(
                met=False,
                observed_value=None,
                threshold=_format(threshold),
                note=f"{label} is unavailable",
                evaluable=False,
                unavailable_reason=f"{label} is unavailable",
                data_quality="PARTIAL",
            )
        comparator = _comparator(params, default_comparator)
        return _Outcome(
            met=_compare(value, threshold, comparator),
            observed_value=_format(value),
            threshold=_format(threshold),
            note=f"{label} over {lookback} bar(s) {comparator} {_format(threshold)}",
        )

    return evaluate


def _eval_cvd_divergence(
    params: Mapping[str, Any], _start: str, _end: str, context: EvaluationContext
) -> _Outcome:
    lookback = _required_int(params.get("lookback_bars"), "lookback_bars", 3)
    if context.price_series is None or context.cvd_series is None:
        return _Outcome(
            met=False,
            observed_value=None,
            threshold=None,
            note="price and CVD series are required",
            evaluable=False,
            unavailable_reason="price_series and cvd_series are unavailable",
            data_quality="PARTIAL",
        )
    if len(context.price_series) != len(context.cvd_series):
        return _Outcome(
            met=False,
            observed_value=None,
            threshold=None,
            note="price and CVD series must be aligned",
            evaluable=False,
            unavailable_reason="price_series and cvd_series have different lengths",
            data_quality="PARTIAL",
        )
    if len(context.price_series) < lookback:
        return _Outcome(
            met=False,
            observed_value=None,
            threshold=None,
            note="not enough bars for the requested lookback",
            evaluable=False,
            unavailable_reason=f"lookback_bars {lookback} exceeds the available series",
            data_quality="PARTIAL",
        )
    price_window = context.price_series[-lookback:]
    cvd_window = context.cvd_series[-lookback:]
    price_change = price_window[-1] - price_window[0]
    cvd_change = cvd_window[-1] - cvd_window[0]
    price_direction = _direction(price_change)
    cvd_direction = _direction(cvd_change)
    met = price_direction * cvd_direction == -1
    return _Outcome(
        met=met,
        observed_value=None,
        threshold=None,
        note=(f"price {_direction_word(price_direction)}, CVD {_direction_word(cvd_direction)}"),
    )


def _direction(value: Decimal) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _direction_word(direction: int) -> str:
    if direction > 0:
        return "up"
    if direction < 0:
        return "down"
    return "flat"


def _eval_session_state(
    params: Mapping[str, Any], _start: str, _end: str, context: EvaluationContext
) -> _Outcome:
    if context.session_state is None:
        return _Outcome(
            met=False,
            observed_value=None,
            threshold=None,
            note="session state is not measured",
            evaluable=False,
            unavailable_reason="session state is not measured",
            data_quality="MISSING",
        )
    states = tuple(_session_state_list(params.get("session_states")))
    met = context.session_state in states
    return _Outcome(
        met=met,
        observed_value=context.session_state,
        threshold=",".join(states),
        note=(
            "trading session matches"
            if met
            else "trading session does not match the required states"
        ),
    )


def _eval_time_window(
    params: Mapping[str, Any], start: str, end: str, context: EvaluationContext
) -> _Outcome:
    if not context.as_of:
        return _Outcome(
            met=False,
            observed_value=None,
            threshold=f"{start}/{end}",
            note="as_of is unavailable",
            evaluable=False,
            unavailable_reason="as_of is unavailable",
            data_quality="MISSING",
        )
    try:
        window_start = _parse(start)
        window_end = _parse(end)
        as_of = _parse(context.as_of)
    except (TypeError, ValueError):
        return _Outcome(
            met=False,
            observed_value=context.as_of,
            threshold=f"{start}/{end}",
            note="the time window is malformed",
            evaluable=False,
            unavailable_reason="the time window is malformed",
            data_quality="PARTIAL",
        )
    # TIME_EXPIRED is an invalidation mapped onto TIME_WINDOW; it is met only
    # once the validity window has ended, not while the idea is still valid.
    original_type = params.get("original_type")
    if original_type == "TIME_EXPIRED":
        met = as_of >= window_end
        note = "validity window has ended" if met else "validity window has not ended"
    else:
        met = window_start <= as_of < window_end
        note = "as_of is inside the UTC window" if met else "as_of is outside the UTC window"
    return _Outcome(
        met=met,
        observed_value=context.as_of,
        threshold=f"{start}/{end}",
        note=note,
    )


def normalize_timestamp(value: str) -> str:
    """Normalise an ISO timestamp to a UTC ``...Z`` string.

    Offset-less values are UTC (semantic validation allows them). Persisting
    one textual form keeps TEXT range comparisons correct.
    """
    parsed = _parse(value).astimezone(UTC)
    if parsed.microsecond % 1000 == 0:
        return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: str) -> datetime:
    """Parse an ISO timestamp, treating an offset-less value as UTC.

    Semantic validation accepts timestamps without an offset; normalising them
    here keeps naive/aware comparisons from raising ``TypeError``. Malformed
    values still raise ``ValueError`` and are reported as unevaluable.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


# -- registry ---------------------------------------------------------------

_PRICE_DATA = frozenset({"MARK_PRICE", "CONTRACT_SPECS"})
_METRICS_DATA = frozenset({"METRICS_5M"})


def _spec(
    condition_type: ConditionType,
    *,
    required_params: tuple[str, ...],
    required_data: frozenset[str],
    evaluator: Evaluator,
    optional_params: tuple[str, ...] = (),
) -> ConditionSpec:
    return ConditionSpec(
        type=condition_type,
        required_params=required_params,
        optional_params=optional_params,
        required_data=required_data,
        evaluator=evaluator,
    )


CONDITION_SPECS: Mapping[ConditionType, ConditionSpec] = {
    ConditionType.PRICE_CROSS_ABOVE: _spec(
        ConditionType.PRICE_CROSS_ABOVE,
        required_params=("price",),
        required_data=_PRICE_DATA,
        evaluator=_eval_price_cross_above,
    ),
    ConditionType.PRICE_CROSS_BELOW: _spec(
        ConditionType.PRICE_CROSS_BELOW,
        required_params=("price",),
        required_data=_PRICE_DATA,
        evaluator=_eval_price_cross_below,
    ),
    ConditionType.PRICE_INSIDE_ZONE: _spec(
        ConditionType.PRICE_INSIDE_ZONE,
        required_params=("price_low", "price_high"),
        required_data=_PRICE_DATA,
        evaluator=_eval_inside_zone,
    ),
    ConditionType.PRICE_TOUCH_ZONE: _spec(
        ConditionType.PRICE_TOUCH_ZONE,
        required_params=("price_low", "price_high"),
        required_data=_PRICE_DATA,
        evaluator=_eval_touch_zone,
    ),
    ConditionType.PRICE_OUTSIDE_ZONE: _spec(
        ConditionType.PRICE_OUTSIDE_ZONE,
        required_params=("price_low", "price_high"),
        required_data=_PRICE_DATA,
        evaluator=_eval_outside_zone,
    ),
    ConditionType.DELTA_ABOVE: _spec(
        ConditionType.DELTA_ABOVE,
        required_params=("threshold", "lookback_bars"),
        required_data=_METRICS_DATA,
        evaluator=_lookback_evaluator(
            label="delta",
            series=lambda context: context.delta_series,
            reduction="sum",
            default_comparator=">",
        ),
    ),
    ConditionType.DELTA_BELOW: _spec(
        ConditionType.DELTA_BELOW,
        required_params=("threshold", "lookback_bars"),
        required_data=_METRICS_DATA,
        evaluator=_lookback_evaluator(
            label="delta",
            series=lambda context: context.delta_series,
            reduction="sum",
            default_comparator="<",
        ),
    ),
    ConditionType.CVD_ABOVE: _spec(
        ConditionType.CVD_ABOVE,
        required_params=("threshold", "lookback_bars"),
        required_data=_METRICS_DATA,
        evaluator=_lookback_evaluator(
            label="CVD",
            series=lambda context: context.cvd_series,
            reduction="last",
            default_comparator=">",
        ),
    ),
    ConditionType.CVD_BELOW: _spec(
        ConditionType.CVD_BELOW,
        required_params=("threshold", "lookback_bars"),
        required_data=_METRICS_DATA,
        evaluator=_lookback_evaluator(
            label="CVD",
            series=lambda context: context.cvd_series,
            reduction="last",
            default_comparator="<",
        ),
    ),
    ConditionType.CVD_DIVERGENCE: _spec(
        ConditionType.CVD_DIVERGENCE,
        required_params=("lookback_bars",),
        required_data=_METRICS_DATA,
        evaluator=_eval_cvd_divergence,
    ),
    ConditionType.OI_DELTA_ABOVE: _spec(
        ConditionType.OI_DELTA_ABOVE,
        required_params=("threshold", "lookback_bars"),
        required_data=frozenset({"OPEN_INTEREST"}),
        evaluator=_lookback_evaluator(
            label="open interest",
            series=lambda context: context.oi_series,
            reduction="pct_change",
            default_comparator=">",
        ),
    ),
    ConditionType.OI_DELTA_BELOW: _spec(
        ConditionType.OI_DELTA_BELOW,
        required_params=("threshold", "lookback_bars"),
        required_data=frozenset({"OPEN_INTEREST"}),
        evaluator=_lookback_evaluator(
            label="open interest",
            series=lambda context: context.oi_series,
            reduction="pct_change",
            default_comparator="<",
        ),
    ),
    ConditionType.FUNDING_ABOVE: _spec(
        ConditionType.FUNDING_ABOVE,
        required_params=("threshold",),
        required_data=frozenset({"FUNDING"}),
        evaluator=_threshold_evaluator("funding_rate", ">"),
    ),
    ConditionType.FUNDING_BELOW: _spec(
        ConditionType.FUNDING_BELOW,
        required_params=("threshold",),
        required_data=frozenset({"FUNDING"}),
        evaluator=_threshold_evaluator("funding_rate", "<"),
    ),
    ConditionType.LIQUIDATION_NOTIONAL_ABOVE: _spec(
        ConditionType.LIQUIDATION_NOTIONAL_ABOVE,
        required_params=("threshold",),
        required_data=frozenset({"LIQUIDATIONS"}),
        evaluator=_threshold_evaluator("liquidation_notional", ">"),
    ),
    ConditionType.VOLUME_ABOVE: _spec(
        ConditionType.VOLUME_ABOVE,
        required_params=("threshold", "lookback_bars"),
        required_data=_METRICS_DATA,
        evaluator=_lookback_evaluator(
            label="volume",
            series=lambda context: context.volume_series,
            reduction="sum",
            default_comparator=">",
        ),
    ),
    ConditionType.FOOTPRINT_IMBALANCE_ABOVE: _spec(
        ConditionType.FOOTPRINT_IMBALANCE_ABOVE,
        required_params=("threshold",),
        required_data=frozenset({"FOOTPRINT"}),
        evaluator=_threshold_evaluator("footprint_imbalance", ">"),
    ),
    ConditionType.SESSION_STATE: _spec(
        ConditionType.SESSION_STATE,
        required_params=("session_states",),
        required_data=frozenset({"SESSION"}),
        evaluator=_eval_session_state,
    ),
    ConditionType.TIME_WINDOW: _spec(
        ConditionType.TIME_WINDOW,
        required_params=(),
        required_data=frozenset(),
        evaluator=_eval_time_window,
    ),
}


def condition_spec(condition_type: str) -> ConditionSpec:
    try:
        resolved = ConditionType(condition_type)
    except ValueError as exc:
        raise ConditionParamsError(
            f"unknown condition type {condition_type!r}", field="type"
        ) from exc
    return CONDITION_SPECS[resolved]


def contract_required_data(condition_type: ConditionType) -> tuple[str, ...]:
    """The ``required_data`` array persisted in the contract envelope.

    The contract requires at least one item; ``TIME_WINDOW`` needs no external
    data for evaluation, so it records the price data it is displayed against
    only to satisfy ``minItems: 1``. The registry's empty required-data set
    remains authoritative: ``creation_evaluable`` and ``evaluate_condition``
    read ``CONDITION_SPECS``, so a ``TIME_WINDOW`` is always evaluable.
    """
    required = CONDITION_SPECS[condition_type].required_data
    if required:
        return tuple(sorted(required))
    return ("MARK_PRICE",)


# -- validation -------------------------------------------------------------

_COMPARATOR_ALLOWED: Mapping[ConditionType, frozenset[str]] = {
    ConditionType.PRICE_CROSS_ABOVE: frozenset({">", ">="}),
    ConditionType.PRICE_CROSS_BELOW: frozenset({"<", "<="}),
    ConditionType.DELTA_ABOVE: frozenset({">", ">="}),
    ConditionType.DELTA_BELOW: frozenset({"<", "<="}),
    ConditionType.CVD_ABOVE: frozenset({">", ">="}),
    ConditionType.CVD_BELOW: frozenset({"<", "<="}),
    ConditionType.OI_DELTA_ABOVE: frozenset({">", ">="}),
    ConditionType.OI_DELTA_BELOW: frozenset({"<", "<="}),
    ConditionType.FUNDING_ABOVE: frozenset({">", ">="}),
    ConditionType.FUNDING_BELOW: frozenset({"<", "<="}),
    ConditionType.LIQUIDATION_NOTIONAL_ABOVE: frozenset({">", ">="}),
    ConditionType.VOLUME_ABOVE: frozenset({">", ">="}),
    ConditionType.FOOTPRINT_IMBALANCE_ABOVE: frozenset({">", ">="}),
}

_ZONE_TYPES = frozenset(
    {
        ConditionType.PRICE_INSIDE_ZONE,
        ConditionType.PRICE_TOUCH_ZONE,
        ConditionType.PRICE_OUTSIDE_ZONE,
    }
)
_PRICE_CROSS_TYPES = frozenset({ConditionType.PRICE_CROSS_ABOVE, ConditionType.PRICE_CROSS_BELOW})


def _validate_comparator(spec: ConditionSpec, params: Mapping[str, Any]) -> str | None:
    value = params.get("comparator")
    if value is None:
        return None
    if not isinstance(value, str) or value not in COMPARATORS:
        raise ConditionParamsError(
            f"comparator must be one of {sorted(COMPARATORS)}", field="comparator"
        )
    allowed = _COMPARATOR_ALLOWED.get(spec.type)
    if allowed is None or value not in allowed:
        raise ConditionParamsError(
            f"comparator {value!r} is inconsistent with {spec.type.value}",
            field="comparator",
        )
    return value


def _reject_inapplicable_params(spec: ConditionSpec, params: Mapping[str, Any]) -> None:
    used = set(spec.required_params) | _DESCRIPTIVE_KEYS | {"comparator"}
    for key in _DRAFT_PARAM_KEYS - used:
        value = params.get(key)
        if value is None:
            continue
        if key == "session_states" and value == []:
            continue
        raise ConditionParamsError(f"{key} does not apply to {spec.type.value}", field=key)


def _canonical_params(spec: ConditionSpec, params: Mapping[str, Any]) -> dict[str, Any]:
    condition_type = spec.type
    canonical: dict[str, Any] = {}
    if condition_type in _PRICE_CROSS_TYPES:
        canonical["price"] = _positive_decimal(params.get("price"), "price")
    elif condition_type in _ZONE_TYPES:
        low = _positive_decimal(params.get("price_low"), "price_low")
        high = _positive_decimal(params.get("price_high"), "price_high")
        if Decimal(low) >= Decimal(high):
            raise ConditionParamsError(
                "price_low must be strictly below price_high", field="price_low"
            )
        canonical["price_low"] = low
        canonical["price_high"] = high
    elif condition_type in {
        ConditionType.DELTA_ABOVE,
        ConditionType.DELTA_BELOW,
        ConditionType.CVD_ABOVE,
        ConditionType.CVD_BELOW,
    }:
        canonical["threshold"] = _required_decimal(params.get("threshold"), "threshold")
        canonical["lookback_bars"] = _required_int(params.get("lookback_bars"), "lookback_bars", 1)
    elif condition_type is ConditionType.CVD_DIVERGENCE:
        canonical["lookback_bars"] = _required_int(params.get("lookback_bars"), "lookback_bars", 3)
    elif condition_type in {
        ConditionType.OI_DELTA_ABOVE,
        ConditionType.OI_DELTA_BELOW,
    }:
        canonical["threshold"] = _required_decimal(params.get("threshold"), "threshold")
        canonical["lookback_bars"] = _required_int(params.get("lookback_bars"), "lookback_bars", 1)
    elif condition_type in {
        ConditionType.FUNDING_ABOVE,
        ConditionType.FUNDING_BELOW,
    }:
        canonical["threshold"] = _required_decimal(params.get("threshold"), "threshold")
    elif condition_type in {
        ConditionType.LIQUIDATION_NOTIONAL_ABOVE,
        ConditionType.VOLUME_ABOVE,
    }:
        canonical["threshold"] = _positive_decimal(params.get("threshold"), "threshold")
        if condition_type is ConditionType.VOLUME_ABOVE:
            canonical["lookback_bars"] = _required_int(
                params.get("lookback_bars"), "lookback_bars", 1
            )
    elif condition_type is ConditionType.FOOTPRINT_IMBALANCE_ABOVE:
        canonical["threshold"] = _unit_interval_decimal(params.get("threshold"), "threshold")
    elif condition_type is ConditionType.SESSION_STATE:
        canonical["session_states"] = _session_state_list(params.get("session_states"))
    comparator = _validate_comparator(spec, params)
    if comparator is not None:
        canonical["comparator"] = comparator
    return canonical


def validate_draft(condition_type: str, params: Mapping[str, Any]) -> ValidatedCondition:
    """Validate one draft against the closed registry and canonicalise it."""
    spec = condition_spec(condition_type)
    for key in params:
        if key not in _DRAFT_PARAM_KEYS:
            raise ConditionParamsError(f"unknown parameter {key!r}", field=key)
    _reject_inapplicable_params(spec, params)
    canonical = _canonical_params(spec, params)
    return ValidatedCondition(
        type=spec.type,
        params=canonical,
        required_data=contract_required_data(spec.type),
    )


def creation_evaluable(condition_type: ConditionType, available: Collection[str]) -> bool:
    """True only when every registry-required data type is available."""
    return CONDITION_SPECS[condition_type].required_data <= frozenset(available)


_INVALIDATION_TYPES = frozenset(
    {
        "PRICE_CROSS_ABOVE",
        "PRICE_CROSS_BELOW",
        "PRICE_OUTSIDE_ZONE",
        "DELTA_ABOVE",
        "DELTA_BELOW",
        "OI_DELTA_ABOVE",
        "OI_DELTA_BELOW",
        "FUNDING_ABOVE",
        "FUNDING_BELOW",
        "TIME_EXPIRED",
        "MANUAL",
    }
)


def map_invalidation_draft(
    draft_type: str,
    *,
    price: str | None,
    threshold: str | None,
    window_end: str | None,
    created_at: str,
    expires_at: str,
    zone_low: str,
    zone_high: str,
    available_data: Collection[str] | None,
) -> MappedCondition:
    """Map a closed invalidation draft onto a real registry condition."""
    if draft_type not in _INVALIDATION_TYPES:
        raise ConditionParamsError(f"unsupported invalidation type {draft_type!r}", field="type")
    end = window_end or expires_at

    def mapped_evaluable(condition_type: ConditionType) -> bool:
        if available_data is None:
            return True
        return creation_evaluable(condition_type, available_data)

    if draft_type == "MANUAL":
        return MappedCondition(
            type=ConditionType.TIME_WINDOW,
            params={"original_type": "MANUAL"},
            window_start=normalize_timestamp(created_at),
            window_end=normalize_timestamp(end),
            required_data=contract_required_data(ConditionType.TIME_WINDOW),
            evaluable=False,
            unavailable_reason="manual invalidation is code-only",
        )
    if draft_type == "TIME_EXPIRED":
        return MappedCondition(
            type=ConditionType.TIME_WINDOW,
            params={"original_type": "TIME_EXPIRED"},
            window_start=normalize_timestamp(created_at),
            window_end=normalize_timestamp(end),
            required_data=contract_required_data(ConditionType.TIME_WINDOW),
            evaluable=mapped_evaluable(ConditionType.TIME_WINDOW),
            unavailable_reason=None,
        )
    if draft_type == "PRICE_OUTSIDE_ZONE":
        raw: dict[str, Any] = {"price_low": zone_low, "price_high": zone_high}
    elif draft_type in {"PRICE_CROSS_ABOVE", "PRICE_CROSS_BELOW"}:
        raw = {"price": price}
    elif draft_type in {
        "DELTA_ABOVE",
        "DELTA_BELOW",
        "OI_DELTA_ABOVE",
        "OI_DELTA_BELOW",
    }:
        # The trigger registry requires a lookback; an invalidation draft has
        # no bar count, so the smallest legal window is used.
        raw = {"threshold": threshold, "lookback_bars": 1}
    else:
        raw = {"threshold": threshold}
    validated = validate_draft(draft_type, raw)
    return MappedCondition(
        type=validated.type,
        params=validated.params,
        window_start=normalize_timestamp(created_at),
        window_end=normalize_timestamp(end),
        required_data=validated.required_data,
        evaluable=mapped_evaluable(validated.type),
        unavailable_reason=None,
    )


# -- evaluation -------------------------------------------------------------


def available_data(context: EvaluationContext) -> frozenset[str]:
    """The data types actually present in a context (absence is never zero)."""
    present: set[str] = set()
    if context.last_price is not None:
        present.add("MARK_PRICE")
    if context.tick_size is not None:
        present.add("CONTRACT_SPECS")
    if any(
        value is not None
        for value in (
            context.delta,
            context.cvd,
            context.cvd_series,
            context.price_series,
            context.volume,
            context.delta_series,
            context.volume_series,
        )
    ):
        present.add("METRICS_5M")
    if context.oi_delta_pct is not None or context.oi_series is not None:
        present.add("OPEN_INTEREST")
    if context.funding_rate is not None:
        present.add("FUNDING")
    if context.liquidation_notional is not None:
        present.add("LIQUIDATIONS")
    if context.footprint_imbalance is not None:
        present.add("FOOTPRINT")
    if context.session_state is not None:
        present.add("SESSION")
    return frozenset(present)


def evaluate_condition(condition: ConditionLike, context: EvaluationContext) -> ConditionEvaluation:
    """Evaluate one condition exactly; side-effect free by construction.

    Evaluation is total: a malformed persisted condition (unknown comparator,
    out-of-range decimal, bad timestamp) yields an unevaluable outcome instead
    of raising out of the read path.
    """
    evaluated_at = context.as_of or None
    if evaluated_at is None:
        return _unavailable(condition, None, "as_of is unavailable", "MISSING")
    try:
        spec = condition_spec(condition.type)
    except ConditionParamsError:
        return _unavailable(condition, evaluated_at, "unknown condition type", "MISSING")
    if not condition.evaluable:
        return _unavailable(
            condition,
            evaluated_at,
            condition.unavailable_reason or "condition is not evaluable",
            "MISSING",
        )
    present = available_data(context)
    missing = spec.required_data - present
    if missing:
        quality = "MISSING" if not (spec.required_data & present) else "PARTIAL"
        return _unavailable(
            condition,
            evaluated_at,
            "missing required data: " + ", ".join(sorted(missing)),
            quality,
        )
    try:
        outcome = spec.evaluator(
            condition.params,
            condition.window_start,
            condition.window_end,
            context,
        )
    except ConditionParamsError as exc:
        return _unavailable(
            condition,
            evaluated_at,
            f"invalid condition parameters: {exc.message}",
            "PARTIAL",
        )
    except (
        InvalidOperation,
        KeyError,
        ValueError,
        ArithmeticError,
        TypeError,
        AssertionError,
    ) as exc:
        return _unavailable(
            condition,
            evaluated_at,
            f"condition evaluation failed ({type(exc).__name__})",
            "PARTIAL",
        )
    return ConditionEvaluation(
        condition_id=condition.condition_id,
        type=condition.type,
        met=outcome.met,
        observed_value=outcome.observed_value,
        threshold=outcome.threshold,
        evaluable=outcome.evaluable,
        unavailable_reason=outcome.unavailable_reason,
        data_quality=outcome.data_quality,
        note=outcome.note,
        evaluated_at=evaluated_at,
    )


def _unavailable(
    condition: ConditionLike, evaluated_at: str | None, reason: str, quality: str
) -> ConditionEvaluation:
    return ConditionEvaluation(
        condition_id=condition.condition_id,
        type=condition.type,
        met=False,
        observed_value=None,
        threshold=None,
        evaluable=False,
        unavailable_reason=reason,
        data_quality=quality,
        note=reason,
        evaluated_at=evaluated_at,
    )


def aggregate_data_quality(qualities: Iterable[str]) -> str:
    values = set(qualities)
    if not values:
        return "MISSING"
    if values == {"COMPLETE"}:
        return "COMPLETE"
    if values == {"MISSING"}:
        return "MISSING"
    return "PARTIAL"


__all__ = [
    "COMPARATORS",
    "CONDITION_SPECS",
    "ConditionEvaluation",
    "ConditionLike",
    "ConditionParamsError",
    "ConditionSpec",
    "ConditionType",
    "EvaluationContext",
    "MappedCondition",
    "SESSION_STATES",
    "ValidatedCondition",
    "aggregate_data_quality",
    "available_data",
    "condition_spec",
    "contract_required_data",
    "creation_evaluable",
    "evaluate_condition",
    "map_invalidation_draft",
    "normalize_timestamp",
    "validate_draft",
]
