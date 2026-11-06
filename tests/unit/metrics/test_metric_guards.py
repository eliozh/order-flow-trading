"""AST guard: metric code must not read the wall clock or build floats."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULES = (
    "src/oft/domain/metrics/cvd.py",
    "src/oft/domain/metrics/volume_profile.py",
    "src/oft/domain/metrics/funding.py",
    "src/oft/domain/metrics/open_interest.py",
    "src/oft/domain/metrics/liquidations.py",
    "src/oft/domain/metrics/footprint.py",
    "src/oft/application/metric_service.py",
    "src/oft/application/oi_funding_service.py",
)


@pytest.mark.parametrize("relative", MODULES)
def test_metric_modules_have_no_wall_clock_or_float_construction(relative: str) -> None:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)
    forbidden = {
        ("datetime", "now"),
        ("datetime", "utcnow"),
        ("date", "today"),
        ("time", "time"),
        ("time", "monotonic"),
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                assert (func.value.id, func.attr) not in forbidden, (
                    f"{relative}:{node.lineno} uses {func.value.id}.{func.attr}()"
                )
            if isinstance(func, ast.Name) and func.id == "Decimal" and node.args:
                argument = node.args[0]
                assert not (
                    isinstance(argument, ast.Call)
                    and isinstance(argument.func, ast.Name)
                    and argument.func.id == "float"
                ), f"{relative}:{node.lineno} constructs Decimal from float"
