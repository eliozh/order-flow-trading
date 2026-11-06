"""Immutable metric versions derived from the algorithm and its parameters.

A version identifier is a pure function of the algorithm name, algorithm
version, and every parameter that can change an output. Changing any of them
creates a new identifier and therefore a new output path; existing versions
are never rewritten.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from oft.domain.metrics.delta import parse_period
from oft.domain.metrics.types import Unit, decimal_str

_ALGORITHM_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
RATIO_PRECISION = 8
RATIO_ROUNDING = "ROUND_HALF_EVEN"
POC_TIEBREAK = "LOWEST_PRICE"
POC_PRICE_RULE = "BIN_LOW"
VALUE_AREA_RULE = "MIN_CONTIGUOUS_BINS"
_ALGORITHM_SOURCE_FILES = ("cvd.py", "volume_profile.py", "types.py", "delta.py")


def _algorithm_source_digest() -> str:
    """Hash the algorithm implementation so code edits change the version."""
    digest = hashlib.sha256()
    here = Path(__file__).resolve().parent
    for name in _ALGORITHM_SOURCE_FILES:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((here / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


ALGORITHM_SOURCE_DIGEST = _algorithm_source_digest()


def _canonical_value(value: object) -> str:
    if isinstance(value, Decimal):
        return decimal_str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    raise TypeError(f"unsupported metric-version parameter: {type(value).__name__}")


@dataclass(frozen=True)
class MetricVersion:
    """A canonical algorithm/parameter identity."""

    algorithm: str
    algorithm_version: int
    parameters: tuple[tuple[str, str], ...]
    digest: str

    @property
    def identifier(self) -> str:
        """Filesystem-safe version string, e.g. ``cvd.v1.ab12cd34ef56``."""
        return f"{self.algorithm}.v{self.algorithm_version}.{self.digest[:16]}"


def build_metric_version(
    *,
    algorithm: str,
    algorithm_version: int,
    parameters: Mapping[str, object],
) -> MetricVersion:
    """Build a deterministic version for an algorithm and parameter set."""
    if _ALGORITHM_PATTERN.fullmatch(algorithm) is None:
        raise ValueError("algorithm must match ^[a-z][a-z0-9_]*$")
    if algorithm_version < 1:
        raise ValueError("algorithm_version must be >= 1")
    canonical = tuple(
        sorted((str(key), _canonical_value(value)) for key, value in parameters.items())
    )
    payload = json.dumps(
        {
            "algorithm": algorithm,
            "algorithm_version": algorithm_version,
            "parameters": dict(canonical),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return MetricVersion(
        algorithm=algorithm,
        algorithm_version=algorithm_version,
        parameters=canonical,
        digest=digest,
    )


def orderflow_metric_version(
    *,
    period: str,
    unit: Unit,
    tick_size: Decimal,
    value_area_pct: Decimal,
    bin_size_multiple: int,
    cvd_algorithm_version: int = 1,
    volume_profile_algorithm_version: int = 1,
) -> MetricVersion:
    """Version the combined CVD + volume-profile document."""
    parse_period(period)
    if unit not in ("BASE", "QUOTE"):
        raise ValueError("unit must be BASE or QUOTE")
    if not tick_size.is_finite() or tick_size <= 0:
        raise ValueError("tick_size must be a positive finite decimal")
    if not (Decimal("0") < value_area_pct <= Decimal("100")):
        raise ValueError("value_area_pct must be in (0, 100]")
    if bin_size_multiple < 1:
        raise ValueError("bin_size_multiple must be >= 1")
    parameters: dict[str, object] = {
        "algorithm_source_sha256": ALGORITHM_SOURCE_DIGEST,
        "cvd_algorithm": "cvd",
        "cvd_algorithm_version": cvd_algorithm_version,
        "cvd_bar_aggregation": "PERIOD",
        "cvd_ratio_precision": RATIO_PRECISION,
        "cvd_ratio_rounding": RATIO_ROUNDING,
        "cvd_taker_mapping": "IS_BUYER_MAKER_FALSE_IS_BUY",
        "period": period,
        "unit": unit,
        "volume_profile_algorithm": "volume_profile",
        "volume_profile_algorithm_version": volume_profile_algorithm_version,
        "volume_profile_bin_size_multiple": bin_size_multiple,
        "volume_profile_bin_size_rule": "TICK_SIZE_MULTIPLE",
        "volume_profile_tick_size": tick_size,
        "volume_profile_poc_price_rule": POC_PRICE_RULE,
        "volume_profile_poc_rule": "MAX_VOLUME",
        "volume_profile_poc_tiebreak": POC_TIEBREAK,
        "volume_profile_value_area_pct": value_area_pct,
        "volume_profile_value_area_rule": VALUE_AREA_RULE,
    }
    return build_metric_version(
        algorithm="orderflow_metrics",
        algorithm_version=1,
        parameters=parameters,
    )
