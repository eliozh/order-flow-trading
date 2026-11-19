"""Packaging gate: the built wheel must declare every runtime import.

These tests pin the regression from slice 59: `jsonschema` and `starlette`
were imported at runtime but only reachable through the dev group / a
transitive dependency, so a clean wheel install failed with
``ModuleNotFoundError``.
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_wheel_deps.py"


def _load_checker():
    """Load scripts/check_wheel_deps.py without relying on sys.path."""
    spec = importlib.util.spec_from_file_location("check_wheel_deps", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_wheel_deps"] = module
    spec.loader.exec_module(module)
    return module


def _write_fake_wheel(path: Path, requires: list[str]) -> None:
    metadata = "Metadata-Version: 2.1\nName: oft\nVersion: 0.1.0\n"
    for requirement in requires:
        metadata += f"Requires-Dist: {requirement}\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("oft-0.1.0.dist-info/METADATA", metadata)


def test_every_runtime_import_is_declared_in_pyproject() -> None:
    checker = _load_checker()

    imported = checker.third_party_imports(REPO_ROOT / "src" / "oft")
    declared = checker.pyproject_requirements(REPO_ROOT / "pyproject.toml")
    missing = checker.missing_dependencies(imported, declared)

    assert not missing, f"runtime imports not declared in [project].dependencies: {sorted(missing)}"


def test_jsonschema_and_starlette_are_runtime_dependencies() -> None:
    checker = _load_checker()

    declared = checker.pyproject_requirements(REPO_ROOT / "pyproject.toml")

    assert "jsonschema" in declared
    assert "starlette" in declared


def test_reports_import_missing_from_wheel_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = _load_checker()
    # Every real import except jsonschema, mimicking the shipped regression.
    imported = checker.third_party_imports(REPO_ROOT / "src" / "oft")
    requires = sorted(
        {checker.distribution_for(module) for module in imported if module != "jsonschema"}
    )
    wheel = tmp_path / "oft-0.1.0-py3-none-any.whl"
    _write_fake_wheel(wheel, requires)

    exit_code = checker.main(["--wheel", str(wheel)])

    assert exit_code == 1
    assert "jsonschema" in capsys.readouterr().err


def test_passes_when_wheel_metadata_declares_all_imports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = _load_checker()
    imported = checker.third_party_imports(REPO_ROOT / "src" / "oft")
    requires = sorted({checker.distribution_for(module) for module in imported})
    wheel = tmp_path / "oft-0.1.0-py3-none-any.whl"
    _write_fake_wheel(wheel, requires)

    exit_code = checker.main(["--wheel", str(wheel)])

    assert exit_code == 0
    assert "jsonschema" not in capsys.readouterr().err


def test_optional_extra_requirements_are_not_counted_as_base() -> None:
    checker = _load_checker()

    names = checker.parse_requires_dist(
        [
            "jsonschema>=4.23.0",
            "uvicorn[standard]>=0.32.1",
            'mypy>=1.13.0; extra == "dev"',
        ]
    )

    assert "jsonschema" in names
    assert "uvicorn" in names
    assert "mypy" not in names
