"""The API envelope error enum and the error catalog cannot drift."""

from __future__ import annotations

import json
from pathlib import Path

from oft.api.errors import ERROR_CATALOG

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"


def _load(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def test_error_code_enum_equals_catalog_codes() -> None:
    envelope = _load("api-envelope.schema.json")
    catalog = _load("error-catalog.json")

    enum_codes = set(envelope["$defs"]["errorCode"]["enum"])
    catalog_codes = {entry["code"] for entry in catalog["codes"]}

    assert enum_codes == catalog_codes


def test_error_category_enum_equals_catalog_categories() -> None:
    envelope = _load("api-envelope.schema.json")
    catalog = _load("error-catalog.json")

    enum_categories = set(envelope["$defs"]["errorCategory"]["enum"])
    catalog_categories = set(catalog["categories"])

    assert enum_categories == catalog_categories


def test_catalog_has_no_duplicate_codes() -> None:
    catalog = _load("error-catalog.json")
    codes = [entry["code"] for entry in catalog["codes"]]

    assert len(codes) == len(set(codes))


def test_implemented_error_specs_match_the_catalog() -> None:
    catalog = {entry["code"]: entry for entry in _load("error-catalog.json")["codes"]}

    for code, spec in ERROR_CATALOG.items():
        entry = catalog[code]
        assert spec.http_status == entry["httpStatus"], code
        assert spec.category == entry["category"], code
        assert spec.retryable == entry["retryable"], code
