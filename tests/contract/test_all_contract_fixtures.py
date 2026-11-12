"""Every committed fixture validates against its contract; the catalog stays in sync.

The repository ships two kinds of fixtures: JSON documents that map to a
contract subsection (order-flow metric goldens) and untrusted wire samples that
must normalize through the domain models (WebSocket frames, liquidations,
exchangeInfo). This module fails when a fixture is added without a schema
mapping, so coverage cannot silently shrink.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import _helpers
import pytest
from _registry import PAPER_LOOP_CONTRACTS, validate
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from oft.adapters.archive import parse_checksum_text, verify_checksum
from oft.application.symbol_registry import parse_exchange_info
from oft.domain.events import (
    EventValidationError,
    event_from_dict,
    event_to_dict,
    normalize_event,
    parse_stream_message,
)
from oft.domain.metrics.liquidations import load_liquidation_events
from oft.domain.resampling import Bar

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = ROOT / "tests" / "fixtures"

SECTION_BY_PREFIX = {
    "cvd_": "cvd",
    "footprint_": "footprint",
    "funding_": "funding",
    "liquidations_": "liquidations",
    "open_interest_": "open_interest",
    "volume_profile_": "volume_profile",
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _orderflow_validator() -> Draft202012Validator:
    common = _load(CONTRACTS / "common.schema.json")
    metrics = _load(CONTRACTS / "orderflow-metrics.schema.json")
    resource = Resource.from_contents(common, default_specification=DRAFT202012)
    registry = Registry().with_resource(common["$id"], resource)
    return Draft202012Validator(metrics, registry=registry)


def _placeholder_document() -> dict[str, Any]:
    """A schema-valid document whose sections can be replaced by a golden."""
    stamp = "2025-11-05T00:00:00.000Z"
    return {
        "schema_version": "1.0.0",
        "symbol": "BTCUSDT",
        "period": "4h",
        "window": {"start": stamp, "end": "2025-11-05T04:00:00.000Z"},
        "computed_at": stamp,
        "metric_version": "orderflow_metrics.v1.0000000000000000",
        "input_manifest_hash": "0" * 64,
        "data_quality": "COMPLETE",
        "quality_reasons": [],
        "cvd": {
            "unit": "BASE",
            "points": [],
            "summary": {
                "delta": "0",
                "cvd_change": "0",
                "buy_volume": "0",
                "sell_volume": "0",
            },
            "divergence_flags": [],
        },
        "footprint": {
            "price_bucket": "0.10",
            "imbalance_threshold": 3,
            "bars": [],
            "top_imbalances": [],
        },
        "volume_profile": {
            "window": {"start": stamp, "end": "2025-11-05T04:00:00.000Z"},
            "bin_size": "0.10",
            "value_area_pct": 70,
            "poc": "0",
            "vah": "0",
            "val": "0",
            "bins": [],
            "notes": [],
        },
        "open_interest": {
            "unit": "BASE",
            "sampling": "REGULAR",
            "points": [],
            "delta_over_window": "0",
            "last": {"ts": stamp, "open_interest": "0", "open_interest_value": "0"},
        },
        "funding": {
            "current_rate": "0",
            "next_funding_time": stamp,
            "interval_hours": 8,
            "mark_price": "0",
            "index_price": "0",
            "history": [],
        },
        "liquidations": {
            "coverage": "NONE",
            "coverage_start": None,
            "coverage_end": None,
            "total_long_notional": "0",
            "total_short_notional": "0",
            "count": 0,
            "buckets": [],
            "limitations": [],
        },
        "sources": [],
        "gaps": [],
    }


def _validate_document(document: dict[str, Any]) -> None:
    errors = sorted(
        _orderflow_validator().iter_errors(document), key=lambda error: list(error.path)
    )
    assert not errors, "\n".join(error.message for error in errors)


def _json_fixture_paths() -> list[Path]:
    return sorted(FIXTURES.rglob("*.json"))


def test_every_contract_schema_is_valid_draft_2020_12() -> None:
    schemas = sorted(CONTRACTS.glob("*.schema.json"))
    assert schemas
    for path in schemas:
        Draft202012Validator.check_schema(_load(path))


def test_error_catalog_validates_and_matches_the_envelope_enum() -> None:
    catalog = _load(CONTRACTS / "error-catalog.json")
    catalog_schema = _load(CONTRACTS / "error-catalog.schema.json")
    envelope = _load(CONTRACTS / "api-envelope.schema.json")

    Draft202012Validator(catalog_schema).validate(catalog)
    assert {entry["code"] for entry in catalog["codes"]} == set(
        envelope["$defs"]["errorCode"]["enum"]
    )
    assert set(catalog["categories"]) == set(envelope["$defs"]["errorCategory"]["enum"])


@pytest.mark.parametrize(
    "path",
    [path for path in _json_fixture_paths() if path.name.startswith(tuple(SECTION_BY_PREFIX))],
    ids=lambda path: path.name,
)
def test_golden_metric_sections_validate_against_the_orderflow_schema(path: Path) -> None:
    prefix = next(prefix for prefix in SECTION_BY_PREFIX if path.name.startswith(prefix))
    section = SECTION_BY_PREFIX[prefix]
    document = _placeholder_document()
    document[section] = _load(path)[section]

    _validate_document(document)


@pytest.mark.parametrize(
    "path",
    [path for path in _json_fixture_paths() if path.name.startswith("bars_")],
    ids=lambda path: path.name,
)
def test_golden_bar_fixtures_validate_through_the_domain_bar_model(path: Path) -> None:
    payload = _load(path)
    assert payload["data_quality"] in {
        "COMPLETE",
        "PARTIAL",
        "STALE",
        "MISSING",
        "ESTIMATED",
    }
    assert isinstance(payload["expected_bar_count"], int)
    assert isinstance(payload["missing_bar_count"], int)
    assert len(payload["bars"]) + payload["missing_bar_count"] == payload["expected_bar_count"]

    for raw in payload["bars"]:
        Bar(
            symbol="BTCUSDT",
            period=path.stem.split("_")[-1],
            bar_start=datetime.fromisoformat(raw["bar_start"].replace("Z", "+00:00")),
            bar_end=datetime.fromisoformat(raw["bar_end"].replace("Z", "+00:00")),
            open=Decimal(raw["open"]),
            high=Decimal(raw["high"]),
            low=Decimal(raw["low"]),
            close=Decimal(raw["close"]),
            volume=Decimal(raw["volume"]),
            quote_volume=Decimal(raw["quote_volume"]),
            trade_count=raw["trade_count"],
            taker_buy_volume=Decimal(raw["taker_buy_volume"]),
        )


def test_exchange_info_fixture_normalizes_through_the_symbol_registry() -> None:
    parsed = parse_exchange_info(
        _load(FIXTURES / "binance" / "exchange_info_sample.json"),
        now=datetime(2026, 11, 6, tzinfo=UTC),
    )
    symbols = {contract.symbol for contract in parsed.contracts}

    assert {"BTCUSDT", "XAUUSDT"} <= symbols
    for contract in parsed.contracts:
        assert contract.tick_size > 0
        assert contract.status == "TRADING"


@pytest.mark.parametrize(
    "path",
    sorted((FIXTURES / "ws").glob("*.jsonl")),
    ids=lambda path: path.name,
)
def test_ws_fixtures_normalize_and_round_trip(path: Path) -> None:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines
    accepted = 0
    rejected = 0
    for line in lines:
        try:
            stream, payload = parse_stream_message(line)
            assert isinstance(stream, str)
            event = normalize_event(payload)
        except EventValidationError:
            # Hostile/negative fixtures must fail closed with a typed reason,
            # never crash or normalize into a stored event.
            rejected += 1
            continue
        restored = event_from_dict(event_to_dict(event))
        assert restored == event
        accepted += 1

    assert accepted > 0, f"{path.name} contains no valid frame"
    assert accepted + rejected == len(lines)


def test_liquidation_fixture_normalizes_through_the_domain_model() -> None:
    events = load_liquidation_events(FIXTURES / "market" / "liquidations_btcusdt.jsonl")

    assert len(events) == 4
    assert {event.side for event in events} == {"BUY", "SELL"}
    assert all(event.symbol == "BTCUSDT" for event in events)


def test_market_fixture_checksums_verify() -> None:
    verified = 0
    for line in (FIXTURES / "market" / "CHECKSUMS").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, filename = parse_checksum_text(line)
        path = FIXTURES / "market" / filename
        assert verify_checksum(path, digest) == digest
        verified += 1

    assert verified > 0


def test_archive_fixture_checksums_verify_except_the_deliberate_corrupt_sample() -> None:
    verified = 0
    for archive in sorted((FIXTURES / "archive").glob("*.zip")):
        checksum_path = archive.with_name(archive.name + ".CHECKSUM")
        if not checksum_path.is_file():
            # Hostile fixtures (zip-slip, oversize) intentionally have no checksum.
            continue
        digest, filename = parse_checksum_text(checksum_path.read_text(encoding="utf-8"))
        assert filename == archive.name
        assert verify_checksum(archive, digest) == digest
        verified += 1

    assert verified >= 7


def test_every_json_fixture_has_a_known_schema_mapping() -> None:
    unmapped: list[str] = []
    for path in _json_fixture_paths():
        name = path.name
        if path.parent.name == "binance":
            continue  # external exchangeInfo shape, validated above
        if path.parent.name == "llm":
            continue  # provider fixtures validated by tests/contract/test_llm_decision_contract.py
        if path.parent.parent.name == "providers" and path.parent.parent.parent.name == "llm":
            continue  # recorded provider bodies under tests/fixtures/llm/providers/<provider>
        if path.parent.name == "knowledge":
            continue  # private knowledge artifacts are not API contract fixtures
        if name.startswith(tuple(SECTION_BY_PREFIX)) or name.startswith("bars_"):
            continue
        unmapped.append(str(path.relative_to(ROOT)))

    assert not unmapped, "add a schema mapping for new JSON fixtures:\n" + "\n".join(unmapped)


def test_the_paper_loop_persists_only_registered_contract_families(tmp_path: Path) -> None:
    """Every paper-loop record family validates, including the budget ledger."""
    loop = _helpers.build_loop(tmp_path)

    assert loop["budget_records"], "the loop must persist at least one budget record"
    for record in loop["budget_records"]:
        validate(PAPER_LOOP_CONTRACTS["budget"], record)

    validate(PAPER_LOOP_CONTRACTS["trigger"], loop["trigger"])
    assert loop["decision"] is not None
    validate(PAPER_LOOP_CONTRACTS["decision"], loop["decision"])

    result = loop["result"]
    assert result.order is not None
    assert result.fill is not None
    assert result.position is not None
    validate(PAPER_LOOP_CONTRACTS["order"], result.order)
    validate(PAPER_LOOP_CONTRACTS["fill"], result.fill)
    validate(PAPER_LOOP_CONTRACTS["position"], result.position)
