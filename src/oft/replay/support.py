"""Shared safety and fixture-pinning helpers for deterministic replays.

The paper-loop and position-management scenarios run the same domain code in
isolated replay stores. They must agree on which ambient settings are pinned
and on the path-safety rules, so those helpers live here once instead of being
imported from one scenario by the other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from oft.replay.harness import ReplayError

# Every setting that can change a fill, fee, account balance, or risk verdict is
# pinned here. ``Settings`` is a ``BaseSettings`` model, so an ambient
# ``OFT_PAPER_*``/``OFT_RISK_*`` value would otherwise leak into a replay on a
# different machine and surface only as a generic non-determinism error.
PINNED_OUTCOME_SETTINGS: dict[str, Any] = {
    "paper_initial_balance": "10000.00",
    "paper_slippage_bps": "2",
    "paper_taker_fee_bps": "5",
    "risk_per_trade_pct": 0.8,
    "risk_max_concurrent_positions": 3,
    "risk_daily_loss_pct": 3.0,
    "risk_max_leverage": 5.0,
    "risk_max_notional_pct": 20.0,
    "risk_max_slippage_bps": 25.0,
    "risk_max_spread_bps": 15.0,
    "risk_stale_mark_seconds": 60,
}


def pinned_fixture_paths() -> dict[str, Path]:
    """The committed fixtures that seed sizing/risk and data-quality outcomes.

    The exchange-info fixture seeds the symbol registry (tick/step/min-notional
    drive entry sizing and the risk gate); the klines fixture changes the
    cross-check ``quality_reasons`` and therefore ``data_quality``. Pinning them
    explicitly keeps an ambient ``OFT_*_FIXTURE`` value from changing a replay.
    """
    from oft.config import (
        DEFAULT_EXCHANGE_INFO_FIXTURE,
        DEFAULT_FIXTURES_DIR,
        DEFAULT_KLINES_FIXTURE,
    )

    return {
        "fixtures_dir": DEFAULT_FIXTURES_DIR,
        "klines_fixture": DEFAULT_KLINES_FIXTURE,
        "exchange_info_fixture": DEFAULT_EXCHANGE_INFO_FIXTURE,
    }


def reject_symlink_components(path: Path, *, stop: Path) -> None:
    """Refuse a symlinked path component between ``path`` and ``stop`` (lstat)."""
    current = path
    while True:
        if current.is_symlink():
            raise ReplayError(f"replay path component is a symlink: {current}")
        if current == stop or current.parent == current:
            return
        current = current.parent


__all__ = [
    "PINNED_OUTCOME_SETTINGS",
    "pinned_fixture_paths",
    "reject_symlink_components",
]
