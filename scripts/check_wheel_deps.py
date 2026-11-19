#!/usr/bin/env python3
"""Fail when the wheel under test does not declare a runtime import.

The parse is lexical (AST) and deliberately conservative: every top-level
third-party module imported anywhere under ``src/oft`` must map to a normalized
distribution name present in the wheel's ``Requires-Dist`` metadata. Imports
guarded by ``if TYPE_CHECKING:`` are typing-only and are skipped.

Use ``--wheel`` to check a built wheel. Without it the script falls back to
``[project].dependencies`` in ``pyproject.toml``, which is the same source
hatchling turns into ``Requires-Dist``.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
import zipfile
from collections import defaultdict
from email.parser import Parser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = REPO_ROOT / "src" / "oft"
DEFAULT_PYPROJECT = REPO_ROOT / "pyproject.toml"

# Module name -> distribution name where the two differ. Underscore/hyphen
# variants (for example ``pydantic_settings`` -> ``pydantic-settings``) are
# handled by normalization and do not need an entry.
MODULE_DISTRIBUTIONS: dict[str, str] = {
    "apscheduler": "apscheduler",
    "cryptography": "cryptography",
    "duckdb": "duckdb",
    "fastapi": "fastapi",
    "httpx": "httpx",
    "jsonschema": "jsonschema",
    "keyring": "keyring",
    "pyarrow": "pyarrow",
    "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings",
    "starlette": "starlette",
    "structlog": "structlog",
    "typer": "typer",
    "uvicorn": "uvicorn",
    "websockets": "websockets",
}

_REQUIREMENT_NAME = re.compile(r"[<>=!~;\[\s(]")


def normalize_distribution(name: str) -> str:
    """Normalize a distribution name the way packaging metadata does."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def distribution_for(module: str) -> str:
    """Map an import name to the distribution name that must be declared."""
    return normalize_distribution(MODULE_DISTRIBUTIONS.get(module, module))


class _ImportCollector(ast.NodeVisitor):
    """Collect top-level modules imported outside ``if TYPE_CHECKING`` blocks."""

    def __init__(self) -> None:
        self.imports: dict[str, set[str]] = defaultdict(set)
        self._typing_only = 0
        self._path = ""

    def collect(self, path: Path) -> dict[str, set[str]]:
        self._path = str(path)
        self.visit(ast.parse(path.read_text(encoding="utf-8"), filename=self._path))
        return self.imports

    def _record(self, module: str) -> None:
        if not self._typing_only:
            self.imports[module.split(".")[0]].add(self._path)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            self._record(node.module)

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking(node.test):
            self._typing_only += 1
            for child in node.body:
                self.visit(child)
            self._typing_only -= 1
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def third_party_imports(src: Path) -> dict[str, list[str]]:
    """Return ``{module: [files]}`` for non-stdlib, non-``oft`` imports."""
    collected: dict[str, set[str]] = defaultdict(set)
    for path in sorted(src.rglob("*.py")):
        for module, files in _ImportCollector().collect(path).items():
            collected[module].update(files)
    stdlib = sys.stdlib_module_names
    return {
        module: sorted(files)
        for module, files in collected.items()
        if module not in stdlib and module != "oft"
    }


def parse_requires_dist(requirements: list[str]) -> set[str]:
    """Extract normalized base distribution names from ``Requires-Dist`` values."""
    names: set[str] = set()
    for requirement in requirements:
        requirement = requirement.strip()
        if not requirement or "extra ==" in requirement:
            continue
        name = _REQUIREMENT_NAME.split(requirement, maxsplit=1)[0]
        if name:
            names.add(normalize_distribution(name))
    return names


def wheel_requirements(wheel: Path) -> set[str]:
    """Read base ``Requires-Dist`` entries from a built wheel's METADATA."""
    with zipfile.ZipFile(wheel) as archive:
        metadata_path = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_path).decode("utf-8")
    message = Parser().parsestr(metadata)
    return parse_requires_dist(message.get_all("Requires-Dist", []))


def pyproject_requirements(pyproject: Path) -> set[str]:
    """Read ``[project].dependencies`` from a pyproject file."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return parse_requires_dist(list(data["project"]["dependencies"]))


def missing_dependencies(
    imported: dict[str, list[str]], declared: set[str]
) -> dict[str, list[str]]:
    """Return imports whose distribution is absent from ``declared``."""
    return {
        module: files
        for module, files in imported.items()
        if distribution_for(module) not in declared
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that runtime imports are declared by the wheel."
    )
    parser.add_argument(
        "--wheel",
        type=Path,
        help="built wheel to inspect; defaults to pyproject [project].dependencies",
    )
    parser.add_argument(
        "--src",
        type=Path,
        default=DEFAULT_SRC,
        help=f"source root to scan (default: {DEFAULT_SRC})",
    )
    args = parser.parse_args(argv)

    imported = third_party_imports(args.src)
    if args.wheel is not None:
        declared = wheel_requirements(args.wheel)
        source = f"wheel {args.wheel.name}"
    else:
        declared = pyproject_requirements(DEFAULT_PYPROJECT)
        source = "pyproject.toml"

    missing = missing_dependencies(imported, declared)
    if missing:
        print(f"check-wheel-deps: {source} does not declare:", file=sys.stderr)
        for module in sorted(missing):
            print(
                f"  {module} (distribution {distribution_for(module)}) "
                f"imported by {', '.join(missing[module])}",
                file=sys.stderr,
            )
        return 1

    print(f"check-wheel-deps: all {len(imported)} runtime imports declared in {source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
