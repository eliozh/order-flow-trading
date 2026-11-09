"""Versioned LLM price table and exact Decimal cost math (ADR-0005 D4).

No prices are bundled and no default model is shipped. Without an operator
supplied ``OFT_LLM_PRICE_TABLE`` the table is empty (``version="none/v1"``) and
every call is honestly ``unpriced`` with a zero cost (contract-valid), never a
silently invented price.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

EMPTY_PRICE_VERSION = "none/v1"
_MICRO = Decimal("1000000")
_QUANTUM = Decimal("0.00000001")
_ZERO = "0"


class PriceTableError(Exception):
    """An operator price table is missing, unreadable, or inconsistent."""

    code = "CONFIG_INVALID"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ModelPrice:
    """One ``(provider, model)`` price row, in USD per million tokens."""

    provider: str
    model: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal
    cache_write_per_mtok: Decimal
    source_url: str
    as_of: str


@dataclass(frozen=True)
class TokenUsage:
    """Provider-reported token counts (reasoning is already inside output)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class PricedCall:
    """The priced view of one call, rendered with contract-safe strings."""

    unpriced: bool
    cost_usd: str
    price_version: str
    input_per_mtok: str
    output_per_mtok: str
    cache_read_per_mtok: str
    cache_write_per_mtok: str


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True)
class PriceTable:
    """A versioned price table keyed by ``(provider, model)``.

    ``models`` is a caller-owned mutable mapping; do not mutate it after
    construction or the key/row identity invariant no longer holds.
    """

    version: str
    models: Mapping[tuple[str, str], ModelPrice]

    def __post_init__(self) -> None:
        """A mismatched key/row identity is unrepresentable (M21).

        Enforcing the invariant on construction means every ``PriceTable`` — not
        only one built by the file loader — is internally consistent.
        """
        for key, price in self.models.items():
            if key != (price.provider, price.model):
                raise PriceTableError(
                    f"price key {key!r} does not match its row {price.provider}/{price.model}"
                )

    def price_for(self, provider: str, model: str) -> ModelPrice | None:
        return self.models.get((provider, model))

    def models_for(self, provider: str) -> tuple[ModelPrice, ...]:
        rows = [entry for entry in self.models.values() if entry.provider == provider]
        return tuple(sorted(rows, key=lambda entry: entry.model))

    def price_call(self, provider: str, model: str, usage: TokenUsage) -> PricedCall:
        price = self.price_for(provider, model)
        if price is None:
            return PricedCall(
                unpriced=True,
                cost_usd=_ZERO,
                price_version=self.version,
                input_per_mtok=_ZERO,
                output_per_mtok=_ZERO,
                cache_read_per_mtok=_ZERO,
                cache_write_per_mtok=_ZERO,
            )
        cost = (
            Decimal(usage.input_tokens) / _MICRO * price.input_per_mtok
            + Decimal(usage.output_tokens) / _MICRO * price.output_per_mtok
            + Decimal(usage.cache_read_tokens) / _MICRO * price.cache_read_per_mtok
            + Decimal(usage.cache_write_tokens) / _MICRO * price.cache_write_per_mtok
        ).quantize(_QUANTUM, rounding=ROUND_HALF_UP)
        return PricedCall(
            unpriced=False,
            cost_usd=_decimal_text(cost),
            price_version=self.version,
            input_per_mtok=_decimal_text(price.input_per_mtok),
            output_per_mtok=_decimal_text(price.output_per_mtok),
            cache_read_per_mtok=_decimal_text(price.cache_read_per_mtok),
            cache_write_per_mtok=_decimal_text(price.cache_write_per_mtok),
        )


def _require_mapping(document: object) -> dict[str, object]:
    if not isinstance(document, dict):
        raise PriceTableError("the price table must be a JSON object")
    return document


def _require_version(document: dict[str, object]) -> str:
    version = document.get("version")
    if not isinstance(version, str) or not version.strip():
        raise PriceTableError("the price table requires a non-empty version")
    return version


def _require_decimal(entry: dict[str, object], field: str, provider: str, model: str) -> Decimal:
    raw = entry.get(field)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PriceTableError(f"price {field} for {provider}/{model} is not a decimal") from exc
    if not value.is_finite():
        raise PriceTableError(f"price {field} for {provider}/{model} is not finite")
    if value < 0:
        raise PriceTableError(f"price {field} for {provider}/{model} must not be negative")
    return value


def _parse_model(entry: object) -> ModelPrice:
    if not isinstance(entry, dict):
        raise PriceTableError("every price model entry must be a JSON object")
    provider = entry.get("provider")
    model = entry.get("model")
    if not isinstance(provider, str) or not provider.strip():
        raise PriceTableError("every price model entry requires a provider")
    if not isinstance(model, str) or not model.strip():
        raise PriceTableError("every price model entry requires a model")
    source_url = entry.get("source_url", "")
    as_of = entry.get("as_of", "")
    if not isinstance(source_url, str) or not isinstance(as_of, str):
        raise PriceTableError(f"price metadata for {provider}/{model} must be strings")
    return ModelPrice(
        provider=provider,
        model=model,
        input_per_mtok=_require_decimal(entry, "input_per_mtok", provider, model),
        output_per_mtok=_require_decimal(entry, "output_per_mtok", provider, model),
        cache_read_per_mtok=_require_decimal(entry, "cache_read_per_mtok", provider, model),
        cache_write_per_mtok=_require_decimal(entry, "cache_write_per_mtok", provider, model),
        source_url=source_url,
        as_of=as_of,
    )


def _parse_document(document: object) -> PriceTable:
    table = _require_mapping(document)
    version = _require_version(table)
    raw_models = table.get("models")
    if not isinstance(raw_models, list):
        raise PriceTableError("the price table requires a models list")
    models: dict[tuple[str, str], ModelPrice] = {}
    for entry in raw_models:
        price = _parse_model(entry)
        key = (price.provider, price.model)
        if key in models:
            raise PriceTableError(f"duplicate price for {price.provider}/{price.model}")
        models[key] = price
    return PriceTable(version=version, models=models)


def load_price_table(path: Path | None) -> PriceTable:
    """Load an operator price table; ``None`` yields the honest empty table."""
    if path is None:
        return PriceTable(version=EMPTY_PRICE_VERSION, models={})
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        # A non-UTF-8 operator file is a corrupt table, not an uncaught decode
        # error: callers degrade accounting instead of failing the scan (F5).
        raise PriceTableError(f"the price table {path} is not valid UTF-8") from exc
    except OSError as exc:
        raise PriceTableError(f"cannot read the price table {path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PriceTableError(f"the price table {path} is not valid JSON") from exc
    return _parse_document(document)


__all__ = [
    "EMPTY_PRICE_VERSION",
    "ModelPrice",
    "PriceTable",
    "PriceTableError",
    "PricedCall",
    "TokenUsage",
    "load_price_table",
]
