"""Pure renderers and command grammar for the read-only Telegram queries."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from oft.application import telegram_messages as messages
from oft.domain.symbols import SymbolContract
from oft.domain.telegram import (
    BUDGET_COMMAND,
    COMMANDS,
    HELP_COMMAND,
    MAX_OUTBOUND_CHARS,
    OPPORTUNITIES_COMMAND,
    POSITIONS_COMMAND,
    REPORT_COMMAND,
    STATUS_COMMAND,
    WATCHLIST_COMMAND,
    WRITE_LIKE_COMMANDS,
    WRITE_REFUSAL_TEXT,
    budget_outbound,
    normalize_symbol_argument,
    parse_command,
    parse_write_like_command,
    query_reference_id,
)

NOW_TEXT = "2026-11-17T09:30:00.000Z"


def _contract(symbol: str, *, tradifi: bool = False) -> SymbolContract:
    return SymbolContract(
        symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        contract_type="TRADIFI_PERPETUAL" if tradifi else "PERPETUAL",
        underlying_type=None,
        status="TRADING",
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
        price_precision=2,
        quantity_precision=3,
        onboard_date=1_600_000_000_000,
        tradifi=tradifi,
    )


def _position(
    *,
    position_id: str = "POS-1",
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    size: str = "0.01",
    entry: str = "70000",
    mark: str | None = "71000",
    mark_as_of: str | None = NOW_TEXT,
    stop: str | None = "69000",
) -> dict[str, Any]:
    return {
        "position_id": position_id,
        "symbol": symbol,
        "side": side,
        "state": "OPEN",
        "size": size,
        "entry_price": entry,
        "avg_entry_price": entry,
        "mark_price": mark,
        "resolved_mark_price": mark,
        "resolved_mark_as_of": mark_as_of,
        "resolved_mark_source": "POSITION" if mark is not None else None,
        "stop_price": stop,
        "updated_at": mark_as_of or NOW_TEXT,
    }


def _report_payload(
    *,
    data_quality: str = "PARTIAL",
    narrative: str = "Range high retest with positive delta.",
    validation_status: str = "VALID",
) -> dict[str, Any]:
    return {
        "report_id": "RPT-1",
        "symbol": "BTCUSDT",
        "timeframe": "4h",
        "generated_at": NOW_TEXT,
        "data_quality": data_quality,
        "narrative_ref": "inline:DEC-1",
        "metrics": {
            "cvd": {
                "summary": {"cvd_change": "12.5"},
                "points": [{"delta": "3"}],
            },
            "volume_profile": {"poc": "70000", "vah": "71000", "val": "69000"},
            "open_interest": {"delta_over_window": "1.2", "points": [{}]},
            "funding": {"current_rate": "0.0001", "history": [{}]},
        },
        "llm": {
            "configured": True,
            "validation_status": validation_status,
            "narrative": narrative,
            "reason": None,
            # These hostile keys must never be selected by the renderer.
            "prompt": "SYSTEM PROMPT SECRET",
            "api_key": "sk-live-secret",
        },
    }


def _unmeasured_report_payload() -> dict[str, Any]:
    """The production fallback shape: a zero with no measured samples.

    ``metric_service._document_payload`` persists ``delta_over_window: "0"``
    with empty ``points`` and ``current_rate: "0"`` with empty ``history``
    when a family was not measured.
    """
    payload = _report_payload()
    payload["metrics"]["open_interest"] = {
        "unit": "BASE",
        "sampling": "REGULAR",
        "points": [],
        "delta_over_window": "0",
        "last": {"ts": NOW_TEXT, "open_interest": "0", "open_interest_value": "0"},
    }
    payload["metrics"]["funding"] = {
        "current_rate": "0",
        "next_funding_time": NOW_TEXT,
        "interval_hours": 8,
        "mark_price": "0",
        "index_price": "0",
        "history": [],
    }
    return payload


def _budget_snapshot(*, usage_quality: str = "COMPLETE") -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "thresholds": {
            "daily_usd": "10",
            "weekly_usd": "50",
            "monthly_usd": "150",
            "warn_pct": 80.0,
        },
        "usage_quality": usage_quality,
        "budget_state": "OK" if usage_quality == "COMPLETE" else None,
        "periods": None,
    }
    if usage_quality == "COMPLETE":
        snapshot["periods"] = {
            name: {
                "period_key": f"2026-11-{17 if name == 'day' else 1}",
                "spend_usd": "1.5",
                "call_count": 2,
                "unpriced_count": 1,
                "budget_state": "OK",
            }
            for name in ("day", "week", "month")
        }
    return snapshot


class TestCommandGrammar:
    def test_the_closed_table_has_the_seven_command_surface(self) -> None:
        assert COMMANDS == (
            STATUS_COMMAND,
            POSITIONS_COMMAND,
            OPPORTUNITIES_COMMAND,
            REPORT_COMMAND,
            BUDGET_COMMAND,
            WATCHLIST_COMMAND,
            HELP_COMMAND,
        )

    @pytest.mark.parametrize(
        "text",
        [
            "/positions",
            "/opportunities",
            "/report",
            "/report BTCUSDT",
            "/budget",
            "/watchlist",
        ],
    )
    def test_query_commands_parse(self, text: str) -> None:
        assert parse_command(text) is not None

    def test_report_carries_the_raw_bounded_argument(self) -> None:
        parsed = parse_command("/report btcusdt")
        assert parsed is not None
        assert parsed.argument == "btcusdt"
        assert normalize_symbol_argument(parsed.argument) == "BTCUSDT"

    def test_prose_and_inexact_commands_never_parse(self) -> None:
        assert parse_command("please run /positions") is None
        assert parse_command("/Positions") is None
        assert parse_command("/reportx") is None


class TestSymbolArgument:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("btcusdt", "BTCUSDT"),
            ("  btcusdt  ", "BTCUSDT"),
            ("Btc​Usdt", "BTCUSDT"),
            ("‮ethusdt‬", "ETHUSDT"),
        ],
    )
    def test_symbols_are_sanitized_uppercased_and_bounded(self, raw: str, expected: str) -> None:
        assert normalize_symbol_argument(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "BTC USDT",
            "BTC-USDT",
            "<script>BTC</script>",
            "A",
            "A" * 25,
            "BTC$USDT",
        ],
    )
    def test_invalid_symbols_are_refused(self, raw: str) -> None:
        assert normalize_symbol_argument(raw) is None

    def test_more_than_one_token_is_invalid(self) -> None:
        assert normalize_symbol_argument("BTCUSDT ETHUSDT") is None


class TestWriteLikeCommands:
    def test_all_seven_write_like_tokens_are_refused_with_one_text(self) -> None:
        assert WRITE_LIKE_COMMANDS == (
            "/approve",
            "/accept",
            "/reject",
            "/cancel",
            "/buy",
            "/sell",
            "/close",
        )
        assert WRITE_REFUSAL_TEXT == "MVP 只读，请在桌面端或 CLI 操作"
        for token in WRITE_LIKE_COMMANDS:
            assert parse_write_like_command(token) == token

    def test_prose_embedded_or_inexact_write_like_tokens_are_not_recognized(self) -> None:
        assert parse_write_like_command("please /approve") is None
        assert parse_write_like_command("/approval") is None
        assert parse_write_like_command("/Approve") is None


class TestHelp:
    def test_help_lists_every_command_and_the_read_only_notice(self) -> None:
        text = messages.render_help()

        for command in COMMANDS:
            assert command in text
        assert "MVP 只读" in text
        assert "/approve" not in text


class TestPositionRender:
    def test_empty_positions_use_the_exact_copy(self) -> None:
        assert messages.POSITIONS_EMPTY_TEXT == "没有未平仓持仓。"
        assert messages.render_positions((), now=NOW_TEXT, stale_seconds=60) == (
            messages.POSITIONS_EMPTY_TEXT
        )

    def test_a_position_renders_size_entry_mark_upnl_and_stop(self) -> None:
        text = messages.render_positions((_position(),), now=NOW_TEXT, stale_seconds=60)
        lines = text.splitlines()

        assert lines[0] == "POSITIONS"
        assert "count: 1" in lines
        assert "symbol: BTCUSDT" in lines
        assert "side: LONG" in lines
        assert "size: 0.01" in lines
        assert "entry: 70000 USDT" in lines
        assert "mark: 71000 USDT" in lines
        assert "mark_state: FRESH" in lines
        assert "upnl: 10 USDT" in lines
        assert "stop: 69000 USDT" in lines

    def test_a_stale_mark_is_labelled_inline_and_never_fresh(self) -> None:
        stale_as_of = "2026-11-17T09:20:00.000Z"
        text = messages.render_positions(
            (_position(mark_as_of=stale_as_of),),
            now=NOW_TEXT,
            stale_seconds=60,
        )

        assert "mark_state: STALE" in text
        assert "mark: 71000 USDT · STALE" in text
        assert "FRESH" not in text

    def test_a_missing_mark_is_an_explicit_na_with_a_reason(self) -> None:
        text = messages.render_positions(
            (_position(mark=None, mark_as_of=None),), now=NOW_TEXT, stale_seconds=60
        )

        assert "mark: N/A" in text
        assert "mark_state: MISSING" in text
        assert "mark_reason: 无记录的标记价格" in text
        assert "upnl: N/A" in text
        assert "FRESH" not in text

    def test_a_short_position_upnl_uses_the_short_sign(self) -> None:
        text = messages.render_positions(
            (_position(side="SHORT", entry="71000", mark="70000"),),
            now=NOW_TEXT,
            stale_seconds=60,
        )
        assert "upnl: 10 USDT" in text

    def test_unparseable_mark_timestamp_is_stale_not_fresh(self) -> None:
        text = messages.render_positions(
            (_position(mark_as_of="not-a-timestamp"),), now=NOW_TEXT, stale_seconds=60
        )
        assert "mark_state: STALE" in text

    def test_an_absurd_stored_mark_is_missing_not_fresh(self) -> None:
        text = messages.render_positions(
            (_position(mark="1e100000000"),), now=NOW_TEXT, stale_seconds=60
        )

        assert "mark: N/A" in text
        assert "mark_state: MISSING" in text
        assert "mark_reason: 无记录的标记价格" in text
        assert "upnl: N/A" in text
        assert "FRESH" not in text
        assert "100000000" not in text


class TestOpportunityRender:
    def _row(self, opportunity_id: str, state: str, created_at: str) -> dict[str, Any]:
        return {
            "opportunity_id": opportunity_id,
            "symbol": "BTCUSDT",
            "side": "LONG",
            "state": state,
            "confidence": 0.72,
            "zone": {"low": "69000", "high": "70000"},
            "created_at": created_at,
        }

    def test_empty_opportunities_use_the_exact_copy(self) -> None:
        assert messages.OPPORTUNITIES_EMPTY_TEXT == "当前没有开放或触发的机会。"
        assert messages.render_opportunities((), now=NOW_TEXT) == (
            messages.OPPORTUNITIES_EMPTY_TEXT
        )

    def test_ids_states_and_ordering_are_deterministic(self) -> None:
        rows = (
            self._row("OPP-2", "TRIGGERED", "2026-11-17T09:00:00.000Z"),
            self._row("OPP-1", "OPEN", "2026-11-17T08:00:00.000Z"),
        )
        text = messages.render_opportunities(rows, now=NOW_TEXT)
        lines = text.splitlines()

        assert lines[0] == "OPPORTUNITIES"
        assert "count: 2" in lines
        assert "opportunity: OPP-2" in lines
        assert "state: TRIGGERED" in lines
        assert "opportunity: OPP-1" in lines
        assert "state: OPEN" in lines
        assert text.index("OPP-2") < text.index("OPP-1")


class TestReportRender:
    def test_empty_report_copy_is_exact(self) -> None:
        assert messages.REPORT_EMPTY_TEXT == "还没有报告，先运行一次扫描。"

    def test_report_preserves_quality_and_whitelists_metrics_and_narrative(self) -> None:
        text = messages.render_report(_report_payload())
        lines = text.splitlines()

        assert lines[0] == "REPORT"
        assert "report_id: RPT-1" in lines
        assert "symbol: BTCUSDT" in lines
        assert "generated_at: 2026-11-17T09:30:00.000Z" in lines
        assert "data_quality: PARTIAL" in lines
        assert "cvd_change: 12.5" in lines
        assert "delta: 3" in lines
        assert "poc: 70000" in lines
        assert "vah: 71000" in lines
        assert "val: 69000" in lines
        assert "open_interest_delta: 1.2" in lines
        assert "funding_rate: 0.0001" in lines
        assert "narrative_ref: inline:DEC-1" in lines
        assert "narrative: Range high retest with positive delta." in lines
        # Only whitelisted fields escape: no raw prompt, provider key, or HTML.
        assert "SYSTEM PROMPT SECRET" not in text
        assert "sk-live-secret" not in text
        assert "<script>" not in text

    def test_a_rejected_report_never_renders_a_narrative(self) -> None:
        payload = _report_payload(validation_status="REJECTED")
        text = messages.render_report(payload)

        assert "llm: REJECTED" in text
        assert "Range high retest" not in text
        assert "narrative: N/A" in text

    def test_unmeasured_open_interest_and_funding_render_na_not_a_zero(self) -> None:
        text = messages.render_report(_unmeasured_report_payload())
        lines = text.splitlines()

        assert "open_interest_delta: N/A — open interest not measured" in lines
        assert "funding_rate: N/A — funding not measured" in lines
        assert "open_interest_delta: 0" not in lines
        assert "funding_rate: 0" not in lines

    def test_stored_quality_reasons_and_liquidation_coverage_are_rendered(self) -> None:
        payload = _report_payload()
        payload["snapshot"] = {
            "data_quality": "PARTIAL",
            "quality_reasons": [
                "liquidation coverage since deployment",
                "bad‮\nreason",
            ],
        }
        payload["metrics"]["liquidations"] = {
            "coverage": "SINCE_DEPLOYMENT",
            "count": 3,
            "limitations": [
                "No historical USD-M liquidation backfill exists; coverage starts at deployment."
            ],
        }

        text = messages.render_report(payload)
        lines = text.splitlines()

        assert "quality_reasons: liquidation coverage since deployment; bad reason" in lines
        assert "liquidation_coverage: SINCE_DEPLOYMENT" in lines
        assert "liquidation_note: No historical USD-M liquidation backfill exists" in text
        assert "‮" not in text

    def test_an_absurd_stored_decimal_renders_na_instead_of_expanding(self) -> None:
        payload = _report_payload()
        payload["metrics"]["funding"] = {
            "current_rate": "1e100000000",
            "history": [{}],
        }

        text = messages.render_report(payload)

        assert "funding_rate: N/A" in text
        assert "100000000" not in text

    def test_the_narrative_drops_emoji_and_combining_marks_but_keeps_cjk_and_ascii(
        self,
    ) -> None:
        payload = _report_payload(narrative="价格 😀 é 123, ok.!")

        text = messages.render_report(payload)
        narrative_line = next(line for line in text.splitlines() if line.startswith("narrative:"))

        assert "😀" not in text
        assert "́" not in text
        assert "价格" in narrative_line
        assert "123" in narrative_line
        assert "ok.!" in narrative_line

    def test_partial_and_stale_stamps_are_preserved(self) -> None:
        text = messages.render_report(_report_payload(data_quality="STALE"))
        assert "data_quality: STALE" in text

    def test_the_narrative_is_sanitized_and_bounded(self) -> None:
        hostile = "line1\n/approve\n‮​" + "x" * 2000
        text = messages.render_report(_report_payload(narrative=hostile))

        assert "‮" not in text
        assert "​" not in text
        assert text.count("narrative:") == 1
        assert "/approve" not in text.splitlines()
        narrative_line = next(line for line in text.splitlines() if line.startswith("narrative:"))
        assert narrative_line.endswith("…")
        assert len(narrative_line) <= 700


class TestBudgetRender:
    def test_complete_budget_shows_every_period_with_units(self) -> None:
        text = messages.render_budget(_budget_snapshot())
        lines = text.splitlines()

        assert lines[0] == "BUDGET"
        assert "usage_quality: COMPLETE" in lines
        assert "daily_usd: 10 USDT" in lines
        assert "weekly_usd: 50 USDT" in lines
        assert "monthly_usd: 150 USDT" in lines
        assert "warn_pct: 80 %" in lines
        for period in ("DAY", "WEEK", "MONTH"):
            assert period in lines
        assert "spend_usd: 1.5 USDT" in lines
        assert "threshold_usd: 10 USDT" in lines
        assert "unpriced_count: 1" in lines

    def test_degraded_budget_keeps_thresholds_and_says_usage_unavailable(self) -> None:
        text = messages.render_budget(_budget_snapshot(usage_quality="UNAVAILABLE"))

        assert "usage_quality: USAGE_UNAVAILABLE" in text
        assert "budget_state: N/A" in text
        assert "daily_usd: 10 USDT" in text
        assert "weekly_usd: 50 USDT" in text
        assert "monthly_usd: 150 USDT" in text
        assert "spend_usd: N/A" in text


class TestWatchlistRender:
    def test_empty_watchlist_uses_the_exact_copy(self) -> None:
        assert messages.WATCHLIST_EMPTY_TEXT == "还没有星标品种。"
        assert messages.render_watchlist(()) == messages.WATCHLIST_EMPTY_TEXT

    def test_starred_symbols_are_grouped_and_sorted(self) -> None:
        contracts = (
            _contract("XAUUSDT", tradifi=True),
            _contract("ETHUSDT"),
            _contract("BTCUSDT"),
        )
        text = messages.render_watchlist(contracts)
        lines = text.splitlines()

        assert lines[0] == "WATCHLIST"
        assert "scope: starred" in lines
        assert "CRYPTO" in lines
        assert "TRADFI" in lines
        assert "symbols: BTCUSDT, ETHUSDT" in lines
        assert "symbols: XAUUSDT" in lines
        assert text.index("CRYPTO") < text.index("TRADFI")

    def test_hostile_stored_symbols_cannot_alter_the_line_structure(self) -> None:
        contracts = (
            _contract("BTC\nUSDT‮"),
            _contract("ETH\x00USDT"),
        )

        text = messages.render_watchlist(contracts)
        lines = text.splitlines()

        assert "\n" not in lines[-1]
        assert "‮" not in text
        assert "\x00" not in text
        assert "symbols: BTC USDT, ETH USDT" in lines
        # One header plus CRYPTO/TRADFI blocks; no injected extra lines.
        assert len(lines) == 10


class TestQueryError:
    def test_the_error_copy_and_reference_id_are_exact(self) -> None:
        reference_id = query_reference_id(7, "/report")
        text = messages.render_query_error("参数无效", reference_id)

        assert text.splitlines()[0] == "查询失败：参数无效。请重试或查看桌面端。"
        assert text.splitlines()[1] == f"reference_id: {reference_id}"
        assert reference_id.startswith("TG-")
        assert len(reference_id) == 11

    def test_reference_ids_are_deterministic_and_not_sequential(self) -> None:
        first = query_reference_id(7, "/positions")
        second = query_reference_id(8, "/positions")
        assert first == query_reference_id(7, "/positions")
        assert first != second

    def test_error_reason_control_characters_are_stripped(self) -> None:
        text = messages.render_query_error("bad\u0000\nreason", "TG-00000000")
        assert "bad reason" in text
        assert "\u0000" not in text


class TestLengthBudget:
    def test_short_text_is_unchanged(self) -> None:
        assert budget_outbound("short", "TG-00000000") == "short"

    def test_an_overlong_render_is_truncated_once_with_marker_and_reference(self) -> None:
        text = budget_outbound("x" * (MAX_OUTBOUND_CHARS + 500), "TG-ABCDEF12")

        assert len(text) <= MAX_OUTBOUND_CHARS
        assert "…" in text
        assert "reference_id: TG-ABCDEF12" in text
        assert "桌面端" in text

    def test_truncation_keeps_the_head_of_the_render(self) -> None:
        text = budget_outbound("HEAD" + "x" * MAX_OUTBOUND_CHARS, "TG-ABCDEF12")
        assert text.startswith("HEAD")
