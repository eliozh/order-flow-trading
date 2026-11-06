"""Contract tests: orderflow metrics schema, golden files, exact JSON decimals."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from oft.adapters.archive import FixtureArchiveSource
from oft.adapters.parquet_store import ParquetStore
from oft.adapters.sqlite_store import SqliteStore
from oft.application.backfill_service import BackfillService
from oft.application.metric_service import MetricDocument, MetricRequest, MetricService
from oft.clock import FixedClock
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"
GOLDEN = ROOT / "tests" / "fixtures" / "golden"
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z$"
)


def _load(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    common = _load("common.schema.json")
    metrics = _load("orderflow-metrics.schema.json")
    resource = Resource.from_contents(common, default_specification=DRAFT202012)
    registry = Registry().with_resource(common["$id"], resource)
    return Draft202012Validator(metrics, registry=registry)


@pytest.fixture(scope="module")
def document(tmp_path_factory: pytest.TempPathFactory) -> MetricDocument:
    tmp_path = tmp_path_factory.mktemp("metrics-contract")
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    parquet = ParquetStore(settings)
    parquet.ensure_layout()
    BackfillService(
        settings=settings,
        archive=FixtureArchiveSource(ARCHIVE),
        parquet=parquet,
        store=store,
        clock=FixedClock(NOW),
    ).run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    service = MetricService(
        settings=settings,
        store=store,
        parquet=parquet,
        clock=FixedClock(NOW),
    )
    return service.compute(
        MetricRequest(
            symbol="BTCUSDT",
            period="4h",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            tick_size=Decimal("0.10"),
        )
    )


def _validate(validator: Draft202012Validator, payload: dict[str, Any]) -> None:
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)


def test_output_validates_against_the_orderflow_metrics_schema(
    document: MetricDocument, validator: Draft202012Validator
) -> None:
    payload = document.to_dict()

    _validate(validator, payload)
    assert payload["schema_version"] == "1.0.0"
    assert payload["symbol"] == "BTCUSDT"
    assert payload["period"] == "4h"
    assert payload["metric_version"] == document.metric_version
    assert payload["input_manifest_hash"] == document.input_manifest_hash
    assert payload["computed_at"] == "2026-11-06T12:00:00.000Z"
    assert payload["data_quality"] == "COMPLETE"
    assert payload["quality_reasons"] == []


def test_every_decimal_field_is_a_json_string(document: MetricDocument) -> None:
    payload = document.to_dict()

    for point in payload["cvd"]["points"]:
        for key in ("buy_volume", "sell_volume", "delta", "cvd"):
            assert isinstance(point[key], str), key
        assert point["taker_buy_ratio"] is None or not isinstance(point["taker_buy_ratio"], str)
    for key, value in payload["cvd"]["summary"].items():
        assert isinstance(value, str), key
    profile = payload["volume_profile"]
    for key in ("bin_size", "poc", "vah", "val"):
        assert isinstance(profile[key], str), key
    for bin_ in profile["bins"]:
        for key in ("price_low", "price_high", "volume", "buy_volume", "sell_volume"):
            assert isinstance(bin_[key], str), key
    assert not isinstance(profile["value_area_pct"], str)


def test_all_timestamps_are_utc_with_z(document: MetricDocument) -> None:
    payload = document.to_dict()

    stamps = [
        payload["window"]["start"],
        payload["window"]["end"],
        payload["computed_at"],
        payload["volume_profile"]["window"]["start"],
        payload["volume_profile"]["window"]["end"],
    ]
    for point in payload["cvd"]["points"]:
        stamps.extend((point["bar_start"], point["bar_end"]))
    for value in stamps:
        assert TIMESTAMP_PATTERN.fullmatch(value), value


def test_cvd_section_matches_the_golden_file(document: MetricDocument) -> None:
    golden = json.loads((GOLDEN / "cvd_btcusdt_4h.json").read_text(encoding="utf-8"))
    payload = document.to_dict()

    assert payload["metric_version"] == golden["metric_version"], (
        "golden metric version drift: bump metric_version and regenerate the golden"
    )
    assert payload["input_manifest_hash"] == golden["input_manifest_hash"]
    assert payload["cvd"] == golden["cvd"]


def test_volume_profile_matches_the_golden_file(document: MetricDocument) -> None:
    golden = json.loads((GOLDEN / "volume_profile_btcusdt.json").read_text(encoding="utf-8"))
    payload = document.to_dict()

    assert payload["metric_version"] == golden["metric_version"], (
        "golden metric version drift: bump metric_version and regenerate the golden"
    )
    assert payload["volume_profile"] == golden["volume_profile"]


def test_golden_version_is_derived_from_algorithm_and_parameters(document: MetricDocument) -> None:
    for name in ("cvd_btcusdt_4h.json", "volume_profile_btcusdt.json"):
        golden = json.loads((GOLDEN / name).read_text(encoding="utf-8"))
        assert golden["metric_version"] == document.metric_version, name
