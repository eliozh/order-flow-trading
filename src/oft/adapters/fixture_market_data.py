"""Fixture-backed market-data adapter for the walking skeleton."""

from __future__ import annotations

from importlib.resources import files

from oft.domain.symbols import SymbolUniverse


class FixtureMarketData:
    """Read the committed symbol universe fixture.

    This stands in for the live exchange adapter until the symbol-universe
    slice adds network access.
    """

    def __init__(self, fixture_name: str = "symbol-universe.json") -> None:
        self._fixture_name = fixture_name

    def load_universe(self) -> SymbolUniverse:
        raw = files("oft.fixtures").joinpath(self._fixture_name).read_text(encoding="utf-8")
        return SymbolUniverse.model_validate_json(raw)
