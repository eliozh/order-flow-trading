"""The simulated paper account: a 10,000 USDT seed plus read models.

The seed is inserted exactly once and is never overwritten by a live balance.
Equity is only ever ``cash + marked_value``, where a LONG open position
contributes ``+size × recorded mark`` and a SHORT contributes ``−size ×
recorded mark`` (the liability to buy it back). When any open position has no
recorded mark the account reports ``equity = cash`` and names ``mark`` in
``stale_inputs``; it never fabricates a PnL number. All money values cross the
boundary as exact decimal strings.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

from oft.config import Settings
from oft.domain.pnl import plain_money_str
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import PaperAccountRecord, StorePort

_LOGGER = logging.getLogger(__name__)
_EIGHT_DP = Decimal("0.00000001")
DEFAULT_ACCOUNT_ID = "ACC-PAPER"


@dataclass(frozen=True)
class EquityView:
    """The marked position value and any inputs that were unavailable.

    ``marked_value`` is signed so that ``equity = cash + marked_value``: a LONG
    position contributes ``+size × mark`` and an open SHORT its negative
    liability ``−size × mark``.
    """

    marked_value: Decimal
    stale_inputs: tuple[str, ...]

    @property
    def available(self) -> bool:
        return not self.stale_inputs


def compute_equity(
    cash: Decimal, positions: Sequence[Mapping[str, Any]]
) -> tuple[Decimal, EquityView]:
    """Return ``(equity, view)``; never treats a missing mark as zero value.

    A LONG position contributes ``+size × mark``; a SHORT contributes
    ``−size × mark``, the liability to buy it back, so ``equity = cash +
    marked_value`` holds for both sides. A corrupt position payload (unknown
    side, non-numeric or non-finite size/mark) is treated exactly like a
    missing mark: the account reports stale inputs and equity is cash, never a
    fabricated or half-computed total (DB-5/P10).
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
            # A payload predating the side field is long-only (slice pre-23).
            raw_side = position.get("side", "LONG")
            if str(raw_side) not in {"LONG", "SHORT"}:
                raise ValueError("position side must be LONG or SHORT")
            direction = Decimal("-1") if str(raw_side) == "SHORT" else Decimal("1")
            marked += direction * size * mark
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


def _money_term(payload: Mapping[str, Any], key: str) -> Decimal:
    """Parse one required, finite decimal money term from a trade-log payload."""
    if key not in payload:
        raise KeyError(f"trade-log payload is missing {key}")
    value = Decimal(str(payload[key]))
    if not value.is_finite():
        raise ValueError(f"trade-log {key} must be finite")
    return value


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

    def closed_totals(self) -> tuple[Decimal, Decimal, int]:
        """``(realized_pnl, total_fees, closed_trades)`` from the trade log.

        Cash already reflects every realized close; these totals are the
        explicit read model so the journal and CLI can show them without
        re-deriving them from fills.

        Corruption policy: a trade log whose payload is not valid JSON, is
        valid JSON that is not an object (``[]``, ``null``, ``"x"``), omits
        either required money term (``net_pnl_usd``/``fees_usd``), or does not
        carry finite decimal money terms is skipped deterministically, named in
        a warning, and left out of the totals. The read model keeps serving
        instead of raising on one bad row, and the skip is never silent.
        """
        realized = Decimal("0")
        fees = Decimal("0")
        count = 0
        # ``limit=None`` reads every closed trade; a page size would silently
        # under-report the lifetime totals.
        for record in self._store.list_trade_logs(limit=None):
            try:
                payload = json.loads(record.payload_json)
                if not isinstance(payload, Mapping):
                    raise ValueError("trade-log payload must be a JSON object")
                # Parse both terms before accumulating so a row missing one is
                # skipped whole, never counted as a partial zero-or-value.
                net_term = _money_term(payload, "net_pnl_usd")
                fee_term = _money_term(payload, "fees_usd")
                realized += net_term
                fees += fee_term
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, ArithmeticError):
                _LOGGER.warning(
                    "skipping corrupt trade log %s; totals exclude this row",
                    record.trade_id,
                )
                continue
            count += 1
        return (
            realized.quantize(_EIGHT_DP, rounding=ROUND_DOWN),
            fees.quantize(_EIGHT_DP, rounding=ROUND_DOWN),
            count,
        )

    def summary(self) -> dict[str, Any]:
        account = self.ensure()
        positions = self.positions()
        cash = Decimal(account.balance)
        equity, view = compute_equity(cash, positions)
        realized, fees, closed_trades = self.closed_totals()
        return {
            "account_id": account.account_id,
            "environment": account.environment,
            "currency": account.currency,
            "initial_balance": account.initial_balance,
            "cash_balance": plain_money_str(cash),
            # ``balance`` is kept for the pre-slice-18 API/CLI read shape.
            "balance": plain_money_str(cash),
            "positions": list(positions),
            "open_positions": len(positions),
            "unrealized_at_last_mark": plain_money_str(view.marked_value),
            "equity": plain_money_str(equity),
            # Closed-trade totals. Realized PnL is already inside cash; these
            # keys expose it explicitly and never double-count it into equity.
            "realized_pnl_usd": plain_money_str(realized),
            "fees_usd": plain_money_str(fees),
            "closed_trades": closed_trades,
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
