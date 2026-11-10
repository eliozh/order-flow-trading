"""The simulated paper account: a 10,000 USDT seed plus read models (D3).

Equity is only ever ``cash + Σ(size × recorded mark)``. When any open position
has no recorded mark the account reports ``equity = cash`` and names ``mark`` in
``stale_inputs``; it never fabricates a PnL number. All money values cross the
boundary as exact decimal strings.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import PaperAccountRecord, StorePort

_EIGHT_DP = Decimal("0.00000001")
DEFAULT_ACCOUNT_ID = "ACC-PAPER"


@dataclass(frozen=True)
class EquityView:
    """The marked position value and any inputs that were unavailable."""

    marked_value: Decimal
    stale_inputs: tuple[str, ...]

    @property
    def available(self) -> bool:
        return not self.stale_inputs


def compute_equity(
    cash: Decimal, positions: Sequence[Mapping[str, Any]]
) -> tuple[Decimal, EquityView]:
    """Return ``(equity, view)``; never treats a missing mark as zero value.

    A corrupt position payload (non-numeric or non-finite size/mark) is treated
    exactly like a missing mark: the account reports stale inputs and equity is
    cash, never a fabricated or half-computed total (DB-5/P10).
    """
    marked = Decimal("0")
    stale: list[str] = []
    for position in positions:
        raw_mark = position.get("mark_price")
        raw_size = position.get("size")
        if raw_mark is None or raw_size is None:
            if "mark" not in stale:
                stale.append("mark")
            continue
        try:
            size = Decimal(str(raw_size))
            mark = Decimal(str(raw_mark))
            if not size.is_finite() or not mark.is_finite():
                raise ValueError("position size/mark must be finite")
            marked += size * mark
        except (ArithmeticError, TypeError, ValueError):
            if "mark" not in stale:
                stale.append("mark")
            continue
    if stale:
        return cash, EquityView(marked_value=Decimal("0"), stale_inputs=tuple(stale))
    if not positions:
        # No marks to add: equity is exactly the seeded/debited cash string.
        return cash, EquityView(marked_value=Decimal("0"), stale_inputs=())
    marked = marked.quantize(_EIGHT_DP, rounding=ROUND_DOWN)
    equity = (cash + marked).quantize(_EIGHT_DP, rounding=ROUND_DOWN)
    return equity, EquityView(marked_value=marked, stale_inputs=())


class PaperAccountService:
    def __init__(self, *, settings: Settings, store: StorePort, clock: ClockPort) -> None:
        self._settings = settings
        self._store = store
        self._clock = clock

    def ensure(self) -> PaperAccountRecord:
        """Seed the 10k account exactly once (idempotent)."""
        existing = self._store.get_paper_account(DEFAULT_ACCOUNT_ID)
        if existing is not None:
            return existing
        initial = str(self._settings.paper_initial_balance)
        record = PaperAccountRecord(
            account_id=DEFAULT_ACCOUNT_ID,
            environment="PAPER",
            currency="USDT",
            initial_balance=initial,
            balance=initial,
            updated_at=utc_timestamp(self._clock.now()),
        )
        return self._store.seed_paper_account(record)

    def account(self) -> PaperAccountRecord:
        return self.ensure()

    def positions(self) -> tuple[dict[str, Any], ...]:
        # No LIMIT: equity must sum every open position, not the first 100.
        return tuple(
            json.loads(record.payload_json)
            for record in self._store.list_positions(state="OPEN", limit=None)
        )

    def summary(self) -> dict[str, Any]:
        account = self.ensure()
        positions = self.positions()
        cash = Decimal(account.balance)
        equity, view = compute_equity(cash, positions)
        return {
            "account_id": account.account_id,
            "environment": account.environment,
            "currency": account.currency,
            "initial_balance": account.initial_balance,
            "cash_balance": str(cash),
            # ``balance`` is kept for the pre-slice-18 API/CLI read shape.
            "balance": str(cash),
            "positions": list(positions),
            "open_positions": len(positions),
            "unrealized_at_last_mark": str(view.marked_value),
            "equity": str(equity),
            "stale_inputs": list(view.stale_inputs),
            "updated_at": account.updated_at,
        }

    def orders(self, *, limit: int = 100, offset: int = 0) -> tuple[dict[str, Any], ...]:
        return tuple(
            json.loads(record.payload_json)
            for record in self._store.list_orders(limit=limit, offset=offset)
        )

    def fills(self, *, limit: int = 100, offset: int = 0) -> tuple[dict[str, Any], ...]:
        return tuple(
            json.loads(record.payload_json)
            for record in self._store.list_fills(limit=limit, offset=offset)
        )


__all__ = [
    "DEFAULT_ACCOUNT_ID",
    "EquityView",
    "PaperAccountService",
    "compute_equity",
]
