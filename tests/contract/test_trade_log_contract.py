"""Contract: a closed trade satisfies ``trade-log.schema.json`` exactly."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import _helpers
from _registry import validate

from oft.application.journal_service import JournalService
from oft.application.position_service import PositionService
from oft.domain.oms import Fill, OrderSide, Position, PositionSide
from oft.domain.positions import (
    TradeLogContext,
    close_full,
    trade_id_for,
    trade_log_from_close,
)
from position_support import set_position_mark


def _close(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    loop = _helpers.build_loop(tmp_path)
    container = loop["container"]
    position = loop["result"].position
    assert position is not None
    set_position_mark(
        container,
        position["position_id"],
        position["stop_price"],
        position["updated_at"],
    )
    service = PositionService(
        store=container.store, clock=container.clock, settings=container.settings
    )
    result = service.monitor(position["position_id"])
    assert result.trade_log is not None
    return position, result.trade_log


def test_trade_log_validates_the_contract(tmp_path: Path) -> None:
    _position, trade = _close(tmp_path)
    validate("trade-log.schema.json", trade)


def test_trade_log_formula_holds_to_eight_decimal_places(tmp_path: Path) -> None:
    _position, trade = _close(tmp_path)

    gross = Decimal(trade["gross_pnl_usd"])
    fees = Decimal(trade["fees_usd"])
    funding = Decimal(trade["funding_usd"])
    net = Decimal(trade["net_pnl_usd"])
    assert net == (gross - fees + funding).quantize(Decimal("0.00000001"))
    # No recorded funding settlement falls inside this short holding window, so
    # funding is a measured zero, never an unmeasured placeholder.
    assert trade["funding_usd"] == "0"
    assert trade["data_quality"]["status"] == "PARTIAL"
    assert any("MAE/MFE" in reason for reason in trade["data_quality"]["reasons"])


def test_trade_log_names_every_unmeasured_field(tmp_path: Path) -> None:
    _position, trade = _close(tmp_path)

    reasons = trade["data_quality"]["reasons"]
    assert any("MAE" in reason for reason in reasons)
    assert any("MFE" in reason for reason in reasons)
    # The frozen initial risk is recorded, so R is measured rather than null.
    assert isinstance(trade["r_multiple"], float)
    assert trade["mae_usd"] == "0.00000000"
    assert trade["mfe_usd"] == "0.00000000"
    signal = trade["signal"]
    assert signal["trigger_id"] is not None
    assert signal["opportunity_id"] is not None
    assert signal["decision_id"] is not None
    assert "LOSS" in trade["outcome_tags"]
    assert "STOPPED" in trade["outcome_tags"]
    assert "DATA_PARTIAL" in trade["outcome_tags"]
    assert trade["review_decision_id"] is None
    assert trade["decision_quality"]["matched_rules"] is None
    assert trade["decision_quality"]["confidence"] is None
    assert trade["provenance"]["llm_provider"] == "stub"
    assert trade["provenance"]["prompt_version"] >= 1
    assert trade["provenance"]["calc_metric_version"] != ""


def test_closed_position_validates_the_position_contract(tmp_path: Path) -> None:
    position, _trade = _close(tmp_path)
    loop_payload = position
    assert loop_payload["state"] == "OPEN"  # the pre-close payload from the entry

    # Read the persisted row back and validate the closed contract document.
    database = Path(tmp_path) / "data" / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM positions WHERE position_id = ?",
            (position["position_id"],),
        ).fetchone()
    closed = json.loads(row[0])
    validate("position.schema.json", closed)
    assert closed["state"] == "CLOSED"
    assert closed["closed_at"] is not None
    assert closed["exit_price"] is not None
    assert closed["realized_pnl_usd"] is not None
    assert closed["unrealized_pnl_usd"] is None


def test_zero_fee_zero_net_close_serializes_money_as_plain_decimals() -> None:
    """An exact zero must be "0", never the exponent form that fails `money`."""
    position = Position(
        position_id="POS-ZERO",
        account_id="ACC-PAPER",
        environment="PAPER",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        state="CLOSED",
        size=Decimal("0.01"),
        entry_price=Decimal("70000"),
        avg_entry_price=Decimal("70000"),
        mark_price=Decimal("70000"),
        notional_usd=Decimal("700.00"),
        stop_price=Decimal("69000"),
        target_prices=(),
        leverage=2.0,
        margin_usd=Decimal("350.00"),
        risk_amount_usd=Decimal("10.00"),
        risk_pct=0.8,
        fees_usd=Decimal("0").quantize(Decimal("0.00000001")),
        opened_at="2026-11-14T11:00:00.000Z",
        updated_at="2026-11-14T12:00:00.000Z",
        source_trigger_id="TRG-TEST",
        closed_at="2026-11-14T12:00:00.000Z",
        exit_price=Decimal("70000"),
        realized_pnl_usd=Decimal("0").quantize(Decimal("0.00000001")),
    )

    payload = position.to_contract()

    validate("position.schema.json", payload)
    assert payload["fees_usd"] == "0"
    assert payload["funding_usd"] == "0"
    assert payload["realized_pnl_usd"] == "0"
    assert "E" not in payload["fees_usd"]
    assert "E" not in payload["funding_usd"]
    assert "E" not in payload["realized_pnl_usd"]


def test_a_funded_open_position_validates_the_position_contract() -> None:
    position = Position(
        position_id="POS-FUNDED",
        account_id="ACC-PAPER",
        environment="PAPER",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        state="OPEN",
        size=Decimal("0.005"),
        entry_price=Decimal("70000"),
        avg_entry_price=Decimal("70000"),
        mark_price=Decimal("70500"),
        notional_usd=Decimal("350"),
        stop_price=Decimal("69000"),
        target_prices=(),
        leverage=2.0,
        margin_usd=Decimal("175"),
        risk_amount_usd=Decimal("10"),
        risk_pct=0.8,
        fees_usd=Decimal("0.35"),
        funding_usd=Decimal("-0.07"),
        opened_at="2026-11-14T11:00:00.000Z",
        updated_at="2026-11-14T12:00:00.000Z",
        source_trigger_id="TRG-TEST",
        management_history=(
            {
                "decision_id": "MGD-PARTIAL",
                "action": "PARTIAL_CLOSE",
                "at": "2026-11-14T12:00:00.000Z",
                "executed_order_id": "ORD-PARTIAL",
                "note": "partial close",
            },
        ),
        version=2,
    )

    payload = position.to_contract()

    validate("position.schema.json", payload)
    assert payload["funding_usd"] == "-0.07"
    assert all(entry["action"] != "FUNDING" for entry in payload["management_history"])


def test_an_enriched_trade_log_validates_with_bar_backed_excursions() -> None:
    position = Position(
        position_id="POS-ENRICHED",
        account_id="ACC-PAPER",
        environment="PAPER",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        state="OPEN",
        size=Decimal("0.01"),
        entry_price=Decimal("70000"),
        avg_entry_price=Decimal("70000"),
        mark_price=Decimal("70000"),
        notional_usd=Decimal("700"),
        stop_price=Decimal("69000"),
        target_prices=(Decimal("72000"),),
        leverage=2.0,
        margin_usd=Decimal("350"),
        risk_amount_usd=Decimal("10"),
        risk_pct=0.8,
        fees_usd=Decimal("0.35"),
        funding_usd=Decimal("-0.07"),
        opened_at="2026-11-14T11:00:00.000Z",
        updated_at="2026-11-14T12:00:00.000Z",
        source_trigger_id="TRG-TEST",
    )
    fill = Fill(
        fill_id="FIL-EXIT",
        order_id="ORD-EXIT",
        environment="PAPER",
        account_id="ACC-PAPER",
        symbol="BTCUSDT",
        side=OrderSide.SELL,
        price=Decimal("68980"),
        quantity=Decimal("0.01"),
        quote_quantity=Decimal("689.80"),
        fee=Decimal("0.345"),
        fee_asset="USDT",
        maker=False,
        filled_at="2026-11-14T12:00:00.000Z",
        received_at="2026-11-14T12:00:00.000Z",
    )
    close = close_full(
        position,
        fill,
        at="2026-11-14T12:00:00.000Z",
        decision_id="MGD-ENRICHED",
        accumulated_gross=Decimal("0"),
    )
    context = TradeLogContext(
        signal={
            "scan_id": "SCAN-1",
            "report_id": "RPT-1",
            "opportunity_id": "OPP-1",
            "trigger_id": "TRG-TEST",
            "decision_id": "DEC-ENTRY",
        },
        provenance={
            "calc_metric_version": "ofi.v1.deadbeef",
            "prompt_version": 2,
            "llm_provider": "stub",
            "llm_model": "fixture",
        },
        decision_quality={"matched_rules": None, "confidence": None, "notes": "not reviewed"},
        mae_usd=Decimal("-12.50"),
        mfe_usd=Decimal("4.25"),
        risk_amount_usd=Decimal("10"),
        entry_notional_usd=Decimal("700"),
        data_quality_status="COMPLETE",
        data_quality_reasons=(),
        close_reason="STOP_TOUCH",
        stopped=True,
        traded_quantity=Decimal("0.01"),
    )

    trade = trade_log_from_close(
        close,
        trade_id=trade_id_for("POS-ENRICHED"),
        context=context,
    )

    validate("trade-log.schema.json", trade)
    assert trade["r_multiple"] == -1.0965
    assert trade["mae_usd"] == "-12.50000000"
    assert trade["mfe_usd"] == "4.25000000"
    assert trade["outcome_tags"] == ["LOSS", "STOPPED"]
    assert trade["signal"]["trigger_id"] == "TRG-TEST"
    assert trade["provenance"]["calc_metric_version"] == "ofi.v1.deadbeef"
    assert trade["data_quality"]["status"] == "COMPLETE"


def test_trade_log_provenance_matches_the_recorded_decision_and_report(
    tmp_path: Path,
) -> None:
    loop = _helpers.build_loop(tmp_path)
    container = loop["container"]
    position = loop["result"].position
    assert position is not None
    set_position_mark(
        container,
        position["position_id"],
        position["stop_price"],
        position["updated_at"],
    )
    service = PositionService(
        store=container.store, clock=container.clock, settings=container.settings
    )
    result = service.monitor(position["position_id"])
    assert result.trade_log is not None
    trade = result.trade_log

    provenance = loop["decision"]["provenance"]
    assert trade["provenance"]["llm_model"] == "fixture"
    assert trade["provenance"]["llm_model"] == provenance["model"]
    assert trade["provenance"]["llm_provider"] == provenance["provider"]
    assert trade["provenance"]["prompt_version"] == provenance["prompt_version"]
    report = container.store.get_report(str(loop["opportunity"]["source"]["report_id"]))
    assert report is not None
    recorded_metric_version = next(
        entry["metric_version"]
        for entry in json.loads(report.metrics_manifest_json)
        if isinstance(entry, dict) and entry.get("metric_version")
    )
    assert trade["provenance"]["calc_metric_version"] == recorded_metric_version


def _closed_with_container(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    loop = _helpers.build_loop(tmp_path)
    container = loop["container"]
    position = loop["result"].position
    assert position is not None
    set_position_mark(
        container,
        position["position_id"],
        position["stop_price"],
        position["updated_at"],
    )
    monitored = PositionService(
        store=container.store, clock=container.clock, settings=container.settings
    ).monitor(position["position_id"])
    assert monitored.trade_log is not None
    return container, monitored.trade_log


def test_journal_stats_marks_an_all_partial_sample_undefined(tmp_path: Path) -> None:
    container, _trade = _closed_with_container(tmp_path)
    service = JournalService(container.store, container.clock, container.settings)

    stats = service.stats()

    # The protective-exit journal has no recorded-bar excursion, so it is
    # PARTIAL and excluded: the metrics must be undefined, never zero.
    assert stats["status"] == "UNDEFINED"
    assert stats["win_rate"] is None
    assert stats["expectancy_usd"] is None
    assert stats["totals"]["net_pnl_usd"] is None
    assert stats["data_quality"] == "MISSING"


def test_journal_json_export_contains_only_valid_trade_log_documents(
    tmp_path: Path,
) -> None:
    container, trade = _closed_with_container(tmp_path)
    service = JournalService(container.store, container.clock, container.settings)

    result = service.export("json")

    document = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert document["schema_version"] == "1.0.0"
    assert [item["trade_id"] for item in document["trades"]] == [trade["trade_id"]]
    for item in document["trades"]:
        validate("trade-log.schema.json", item)
    assert document["stats"]["definitions"]
    assert "system_prompt" not in json.dumps(document).lower()


def test_journal_csv_export_uses_the_read_model_header(tmp_path: Path) -> None:
    container, _trade = _closed_with_container(tmp_path)
    service = JournalService(container.store, container.clock, container.settings)

    result = service.export("csv")

    text = Path(result["path"]).read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("trade_id,position_id,environment,symbol")
    assert result["rows"] == 1
    assert "system_prompt" not in text
    assert "raw_output" not in text


def test_journal_compare_names_each_unavailable_side(tmp_path: Path) -> None:
    container, trade = _closed_with_container(tmp_path)
    service = JournalService(container.store, container.clock, container.settings)

    comparison = service.compare(trade["trade_id"])

    assert comparison["zone"]["status"] in {"INSIDE", "ABOVE_ZONE", "BELOW_ZONE"}
    assert comparison["slippage"]["status"] == "OK"
    assert comparison["decision"]["status"] == "OK"
    # No recorded bar covers the fixed close instant, so first touch is N/A.
    assert comparison["first_touch"]["status"] == "N/A"
    assert comparison["first_touch"]["kind"] is None
    assert comparison["first_touch"]["reasons"]
    assert comparison["data_quality"] == "PARTIAL"
