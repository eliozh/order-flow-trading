"""Fixture-backed market-data adapter for the walking skeleton."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path

from oft.config import DEFAULT_FIXTURES_DIR
from oft.domain.events import AggTrade
from oft.domain.symbols import SymbolUniverse

_REQUIRED_COLUMNS = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
)


class FixtureDataError(Exception):
    """The fixture is missing, malformed, or unusable."""

    code = "DATA_UNAVAILABLE"


class FixturePathForbidden(FixtureDataError):
    """The fixture path resolves outside the configured fixtures directory."""

    code = "VALIDATION_FAILED"


def _parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"true", "1"}:
        return True
    if value in {"false", "0"}:
        return False
    raise FixtureDataError(f"invalid is_buyer_maker value {raw!r}; expected true or false")


def _parse_ms(raw: str) -> datetime:
    milliseconds = int(raw)
    return datetime.fromtimestamp(milliseconds // 1000, tz=UTC) + timedelta(
        milliseconds=milliseconds % 1000
    )


class FixtureMarketData:
    """Read committed fixtures: the symbol universe and aggTrade samples.

    This stands in for the live exchange adapter until later slices add
    network access.
    """

    def __init__(
        self,
        fixture_name: str = "symbol-universe.json",
        fixtures_dir: Path | str | None = None,
    ) -> None:
        self._fixture_name = fixture_name
        self._fixtures_dir = Path(fixtures_dir) if fixtures_dir is not None else None

    @property
    def fixtures_dir(self) -> Path:
        return (self._fixtures_dir or DEFAULT_FIXTURES_DIR).expanduser().resolve()

    def _resolve_fixture(self, fixture_path: Path | str) -> Path:
        candidate = Path(fixture_path).expanduser().resolve()
        if not candidate.is_relative_to(self.fixtures_dir):
            raise FixturePathForbidden(
                f"fixture path is outside the configured fixtures directory: {fixture_path}"
            )
        return candidate

    def load_universe(self) -> SymbolUniverse:
        raw = files("oft.fixtures").joinpath(self._fixture_name).read_text(encoding="utf-8")
        return SymbolUniverse.model_validate_json(raw)

    def load_agg_trades(self, fixture_path: Path | str, symbol: str) -> tuple[AggTrade, ...]:
        """Parse a Binance-shaped aggTrades CSV into normalized, typed rows."""
        path = self._resolve_fixture(fixture_path)
        if not path.is_file():
            raise FixtureDataError(f"fixture not found: {path}")

        trades: list[AggTrade] = []
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                fieldnames = tuple(reader.fieldnames or ())
                missing = [name for name in _REQUIRED_COLUMNS if name not in fieldnames]
                if missing:
                    raise FixtureDataError(
                        f"fixture {path} is missing columns: {', '.join(missing)}"
                    )
                for line_number, row in enumerate(reader, start=2):
                    try:
                        trades.append(
                            AggTrade(
                                symbol=symbol,
                                agg_trade_id=int(row["agg_trade_id"]),
                                price=Decimal(row["price"]),
                                quantity=Decimal(row["quantity"]),
                                first_trade_id=int(row["first_trade_id"]),
                                last_trade_id=int(row["last_trade_id"]),
                                transact_time=_parse_ms(row["transact_time"]),
                                is_buyer_maker=_parse_bool(row["is_buyer_maker"]),
                            )
                        )
                    except (KeyError, ValueError, InvalidOperation) as exc:
                        raise FixtureDataError(
                            f"invalid aggTrade row at line {line_number}: {exc}"
                        ) from exc
        except OSError as exc:
            raise FixtureDataError(f"cannot read fixture {path}: {exc}") from exc

        if not trades:
            raise FixtureDataError(f"fixture {path} contains no aggTrades")
        return tuple(trades)
