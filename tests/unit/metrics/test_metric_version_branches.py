"""Branch coverage for pure metric-version canonicalization and validation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oft.domain.metrics.version import (
    _canonical_value,
    build_metric_version,
    orderflow_metric_version,
)


def test_canonical_value_handles_bool_none_and_rejects_unknown_types() -> None:
    assert _canonical_value(True) == "true"
    assert _canonical_value(False) == "false"
    assert _canonical_value(None) == "null"
    with pytest.raises(TypeError, match="unsupported"):
        _canonical_value(["not", "allowed"])  # type: ignore[arg-type]


def test_build_metric_version_rejects_bad_algorithm_inputs() -> None:
    with pytest.raises(ValueError, match="algorithm"):
        build_metric_version(algorithm="Bad", algorithm_version=1, parameters={})
    with pytest.raises(ValueError, match="algorithm_version"):
        build_metric_version(algorithm="good", algorithm_version=0, parameters={})


def test_orderflow_metric_version_rejects_each_invalid_parameter() -> None:
    base = {
        "period": "4h",
        "unit": "BASE",
        "tick_size": Decimal("0.1"),
        "value_area_pct": Decimal("70"),
        "bin_size_multiple": 1,
    }
    for overrides in (
        {"unit": "ETH"},
        {"tick_size": Decimal("0")},
        {"value_area_pct": Decimal("0")},
        {"bin_size_multiple": 0},
        {"footprint_imbalance_threshold": Decimal("1")},
        {"footprint_stacked_min_levels": 1},
        {"footprint_top_imbalances": -1},
        {"liquidation_bucket_seconds": 0},
    ):
        with pytest.raises(ValueError):
            orderflow_metric_version(**{**base, **overrides})  # type: ignore[arg-type]
