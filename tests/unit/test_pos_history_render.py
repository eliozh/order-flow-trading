"""The ``oft pos history`` text projection explains funding accrual status."""

from __future__ import annotations

from oft.cli.commands.pos import _render_history


def test_render_history_shows_accrued_and_rejected_funding() -> None:
    data = {
        "position_id": "POS-1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "state": "OPEN",
        "size": "1",
        "avg_entry_price": "70000",
        "pnl": {
            "basis": "ACCUMULATED_GROSS",
            "gross_pnl_usd": "10",
            "fees_usd": "1",
            "funding_usd": "0",
            "net_pnl_usd": None,
            "r_multiple": None,
            "return_pct": None,
            "mae_usd": None,
            "mfe_usd": None,
            "outcome_tags": [],
        },
        "funding": [
            {
                "settlement_time": "t1",
                "rate": "0.0001",
                "mark_price": "70000",
                "amount_usd": "-0.07",
                "accrued": True,
                "reason": None,
            },
            {
                "settlement_time": "t2",
                "rate": "0.0001",
                "mark_price": None,
                "amount_usd": None,
                "accrued": False,
                "reason": "missing a recorded rate or mark; not accrued",
            },
        ],
        "funding_reasons": [
            "funding settlement t2 is missing a recorded rate or mark; not accrued"
        ],
        "events": [],
    }

    rendered = _render_history(data)

    assert "pnl basis ACCUMULATED_GROSS" in rendered
    assert "t1" in rendered and "accrued" in rendered
    assert "not accrued: missing a recorded rate or mark" in rendered
    assert "FUNDING QUALITY" in rendered
