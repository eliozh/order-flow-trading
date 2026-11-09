"""Versioned price table and exact Decimal cost math (ADR-0005 D4)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from oft.domain.pricing import (
    EMPTY_PRICE_VERSION,
    ModelPrice,
    PriceTable,
    PriceTableError,
    TokenUsage,
    load_price_table,
)

_ONE = Decimal("1.00000000")


def _model(
    *,
    provider: str = "openai",
    model: str = "gpt-fixture",
    input_rate: str = "2.50",
    output_rate: str = "10.00",
    cache_read: str = "1.25",
    cache_write: str = "3.00",
) -> ModelPrice:
    return ModelPrice(
        provider=provider,
        model=model,
        input_per_mtok=Decimal(input_rate),
        output_per_mtok=Decimal(output_rate),
        cache_read_per_mtok=Decimal(cache_read),
        cache_write_per_mtok=Decimal(cache_write),
        source_url="https://example.invalid/pricing",
        as_of="2026-11-09",
    )


def test_empty_table_is_honest_unpriced_and_never_invents_prices() -> None:
    table = PriceTable(version=EMPTY_PRICE_VERSION, models={})

    priced = table.price_call("openai", "any-model", TokenUsage(input_tokens=1000))

    assert priced.unpriced is True
    assert priced.cost_usd == "0"
    assert priced.price_version == EMPTY_PRICE_VERSION
    assert priced.input_per_mtok == "0"
    assert priced.output_per_mtok == "0"
    assert priced.cache_read_per_mtok == "0"
    assert priced.cache_write_per_mtok == "0"


def test_unknown_model_in_a_populated_table_is_unpriced() -> None:
    table = PriceTable(version="2026-11-09", models={("openai", "known"): _model(model="known")})

    priced = table.price_call("openai", "unknown", TokenUsage(output_tokens=10))

    assert priced.unpriced is True
    assert priced.cost_usd == "0"
    assert priced.price_version == "2026-11-09"


def test_cost_is_the_sum_of_all_four_token_classes_at_eight_decimals() -> None:
    table = PriceTable(version="2026-11-09", models={("openai", "gpt-fixture"): _model()})

    priced = table.price_call(
        "openai",
        "gpt-fixture",
        TokenUsage(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
            reasoning_tokens=999,
        ),
    )

    # 2.50 + 10.00 + 1.25 + 3.00; reasoning tokens are already billed in output.
    assert priced.unpriced is False
    assert priced.cost_usd == "16.75000000"
    assert priced.input_per_mtok == "2.50"
    assert priced.output_per_mtok == "10.00"
    assert priced.cache_read_per_mtok == "1.25"
    assert priced.cache_write_per_mtok == "3.00"


def test_fractional_cost_rounds_half_up_to_eight_places() -> None:
    table = PriceTable(
        version="v",
        models={("openai", "m"): _model(model="m", input_rate="0.00000003")},
    )

    priced = table.price_call("openai", "m", TokenUsage(input_tokens=1))

    # 0.00000000003 -> 0.00000000 at 8 dp; half-up is applied to quantum 1e-8.
    assert priced.cost_usd == "0.00000000"

    table_two = PriceTable(
        version="v",
        models={("openai", "m"): _model(model="m", input_rate="0.5")},
    )
    # 1 token * 0.5 / 1e6 = 0.0000005 -> rounds half-up to 0.00000050.
    assert table_two.price_call("openai", "m", TokenUsage(input_tokens=1)).cost_usd == (
        "0.00000050"
    )


def test_models_for_returns_the_provider_catalog_sorted_by_model() -> None:
    table = PriceTable(
        version="v",
        models={
            ("openai", "b"): _model(model="b"),
            ("openai", "a"): _model(model="a"),
            ("anthropic", "c"): _model(provider="anthropic", model="c"),
        },
    )

    assert [entry.model for entry in table.models_for("openai")] == ["a", "b"]
    assert [entry.model for entry in table.models_for("anthropic")] == ["c"]
    assert table.models_for("deepseek") == ()


def test_load_from_a_file_parses_every_field() -> None:
    path = Path("/tmp/prices-fixture.json")

    document = {
        "version": "2026-11-09",
        "models": [
            {
                "provider": "deepseek",
                "model": "deepseek-fixture",
                "input_per_mtok": "0.27",
                "output_per_mtok": "1.10",
                "cache_read_per_mtok": "0.07",
                "cache_write_per_mtok": "0",
                "source_url": "https://example.invalid",
                "as_of": "2026-11-09",
            }
        ],
    }
    path.write_text(json.dumps(document), encoding="utf-8")

    table = load_price_table(path)

    assert table.version == "2026-11-09"
    price = table.price_for("deepseek", "deepseek-fixture")
    assert price is not None
    assert price.input_per_mtok == Decimal("0.27")
    assert price.source_url == "https://example.invalid"
    assert price.as_of == "2026-11-09"


def test_missing_path_is_the_empty_table() -> None:
    table = load_price_table(None)

    assert table.version == EMPTY_PRICE_VERSION
    assert table.models == {}


def test_unreadable_or_invalid_json_is_config_invalid(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"
    with pytest.raises(PriceTableError) as caught:
        load_price_table(missing)
    assert caught.value.code == "CONFIG_INVALID"

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(PriceTableError):
        load_price_table(broken)


def test_a_non_utf8_price_table_is_config_invalid(tmp_path: Path) -> None:
    """A non-UTF-8 operator file degrades accounting instead of escaping (F5 residual)."""
    path = tmp_path / "prices.json"
    path.write_bytes(b'{"version": "v", "models": []}\n\xff\xfe')

    with pytest.raises(PriceTableError) as caught:
        load_price_table(path)

    assert caught.value.code == "CONFIG_INVALID"
    assert "not valid UTF-8" in caught.value.message


def test_duplicate_provider_model_is_config_invalid(tmp_path: Path) -> None:
    path = tmp_path / "prices.json"
    entry = {
        "provider": "openai",
        "model": "m",
        "input_per_mtok": "1",
        "output_per_mtok": "1",
        "cache_read_per_mtok": "1",
        "cache_write_per_mtok": "1",
        "source_url": "",
        "as_of": "2026-11-09",
    }
    path.write_text(json.dumps({"version": "v", "models": [entry, entry]}), encoding="utf-8")

    with pytest.raises(PriceTableError) as caught:
        load_price_table(path)
    assert caught.value.code == "CONFIG_INVALID"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_per_mtok", "not-a-number"),
        ("output_per_mtok", None),
        ("cache_read_per_mtok", ""),
        ("cache_write_per_mtok", []),
    ],
)
def test_non_decimal_price_is_config_invalid(tmp_path: Path, field: str, value: object) -> None:
    entry: dict[str, object] = {
        "provider": "openai",
        "model": "m",
        "input_per_mtok": "1",
        "output_per_mtok": "1",
        "cache_read_per_mtok": "1",
        "cache_write_per_mtok": "1",
        "source_url": "",
        "as_of": "2026-11-09",
    }
    entry[field] = value
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"version": "v", "models": [entry]}), encoding="utf-8")

    with pytest.raises(PriceTableError):
        load_price_table(path)


@pytest.mark.parametrize(
    "document",
    [
        {"models": []},
        {"version": "", "models": []},
        {"version": "v", "models": [{"provider": "openai"}]},
        [],
    ],
)
def test_malformed_table_document_is_config_invalid(tmp_path: Path, document: object) -> None:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PriceTableError):
        load_price_table(path)


def test_example_pricing_file_documents_a_placeholder_shape() -> None:
    root = Path(__file__).resolve().parents[2]
    document = json.loads((root / "config" / "pricing.example.json").read_text(encoding="utf-8"))

    assert document["version"]
    assert document["models"]
    assert {entry["provider"] for entry in document["models"]} == {
        "openai",
        "anthropic",
        "deepseek",
    }


def test_non_finite_prices_are_config_invalid(tmp_path: Path) -> None:
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {
                "version": "v",
                "models": [
                    {
                        "provider": "openai",
                        "model": "m",
                        "input_per_mtok": "NaN",
                        "output_per_mtok": "1",
                        "cache_read_per_mtok": "1",
                        "cache_write_per_mtok": "1",
                        "source_url": "",
                        "as_of": "2026-11-09",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PriceTableError):
        load_price_table(path)


@pytest.mark.parametrize(
    "models",
    [
        ["not-an-object"],
        [{}],
        [{"provider": "", "model": "m"}],
        [{"provider": "openai"}],
        [{"provider": "openai", "model": ""}],
        [
            {
                "provider": "openai",
                "model": "m",
                "input_per_mtok": "1",
                "output_per_mtok": "1",
                "cache_read_per_mtok": "1",
                "cache_write_per_mtok": "1",
                "source_url": 123,
                "as_of": "2026-11-09",
            }
        ],
        {"not": "a list"},
    ],
)
def test_malformed_model_entries_are_config_invalid(tmp_path: Path, models: object) -> None:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"version": "v", "models": models}), encoding="utf-8")

    with pytest.raises(PriceTableError):
        load_price_table(path)


def test_price_metadata_may_be_omitted() -> None:
    entry = {
        "provider": "openai",
        "model": "m",
        "input_per_mtok": "1",
        "output_per_mtok": "1",
        "cache_read_per_mtok": "1",
        "cache_write_per_mtok": "1",
    }
    table = load_price_table(None)
    parsed = table.price_for("openai", "m")
    assert parsed is None
    from oft.domain.pricing import _parse_document

    result = _parse_document({"version": "v", "models": [entry]})
    assert result.price_for("openai", "m").source_url == ""  # type: ignore[union-attr]


def test_negative_token_prices_are_config_invalid(tmp_path: Path) -> None:
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {
                "version": "v",
                "models": [
                    {
                        "provider": "openai",
                        "model": "m",
                        "input_per_mtok": "-1",
                        "output_per_mtok": "1",
                        "cache_read_per_mtok": "1",
                        "cache_write_per_mtok": "1",
                        "source_url": "",
                        "as_of": "2026-11-09",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PriceTableError):
        load_price_table(path)


def test_a_price_key_must_match_its_row_identity() -> None:
    mismatched = {
        ("openai", "m"): ModelPrice(
            provider="anthropic",
            model="m",
            input_per_mtok=Decimal("1"),
            output_per_mtok=Decimal("1"),
            cache_read_per_mtok=Decimal("1"),
            cache_write_per_mtok=Decimal("1"),
            source_url="",
            as_of="2026-11-09",
        )
    }

    # The invariant lives on construction, so direct construction is rejected
    # too (not only the private file-loader path).
    with pytest.raises(PriceTableError):
        PriceTable(version="v", models=mismatched)
