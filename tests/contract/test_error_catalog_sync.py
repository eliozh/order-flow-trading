"""The API envelope error enum and the error catalog cannot drift."""

from __future__ import annotations

import json
import re
from pathlib import Path

from oft.api.errors import ERROR_CATALOG

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"
SOURCE = ROOT / "src" / "oft"

# Codes that a route forwards to ``ApiError`` (directly or through a typed
# application exception whose ``code`` becomes the envelope code).
_API_CODE_CALL = re.compile(
    r"(?:ApiError|error_response|EntryRejected|TriggerError|GatewayError)"
    r'\(\s*"([A-Z][A-Z0-9_]+)"'
)


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


def test_security_and_scan_codes_are_covered_in_both_directions() -> None:
    """Emitted codes exist in the catalog, and the catalog matches their spec."""
    catalog = {entry["code"]: entry for entry in _load("error-catalog.json")["codes"]}
    envelope = set(_load("api-envelope.schema.json")["$defs"]["errorCode"]["enum"])

    for code in ("HOST_FORBIDDEN", "SCAN_ALREADY_RUNNING"):
        # implemented -> catalog -> envelope schema
        assert code in ERROR_CATALOG
        assert code in catalog
        assert code in envelope
        # catalog -> implemented spec (same values in both directions)
        spec = ERROR_CATALOG[code]
        entry = catalog[code]
        assert (spec.http_status, spec.category, spec.retryable) == (
            entry["httpStatus"],
            entry["category"],
            entry["retryable"],
        )
    assert set(ERROR_CATALOG) <= set(catalog)


def test_every_code_forwarded_to_the_api_is_implemented() -> None:
    """No route or service may emit a code that ``ERROR_CATALOG`` cannot render.

    The catalog contract covers the whole surface (including codes that are not
    implemented yet). This guard closes the other direction: a code emitted by
    the application must have an ``ErrorSpec`` so it cannot become a ``KeyError``
    -> 500 at the envelope boundary.
    """
    emitted: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        emitted.update(_API_CODE_CALL.findall(path.read_text(encoding="utf-8")))

    assert emitted
    assert emitted <= set(ERROR_CATALOG), sorted(emitted - set(ERROR_CATALOG))


def test_conflict_is_defined_as_a_409() -> None:
    spec = ERROR_CATALOG["CONFLICT"]

    assert spec.http_status == 409
    assert spec.category == "CONFLICT"
