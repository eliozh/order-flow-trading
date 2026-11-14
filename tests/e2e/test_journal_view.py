"""E2E: the journal read models behind the /journal Web surfaces.

The daemon runs in-process over a temporary data dir against the committed
archive fixtures and the deterministic ``stub`` LLM. A closed trade is seeded
through the real scan -> trigger -> recheck -> accept -> (touch stop) -> monitor
path. Statistics never fabricate zeroes for an empty sample; an open position is
counted and excluded; the comparison names N/A / PARTIAL / AMBIGUOUS_BAR; and
the server-generated export neutralizes CSV formulas before the file exists.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from journal_support import (
    NOW,
    ROOT,
    WEB_SRC,
    _closed_trade,
    _harness,
    _open_position,
    _validate_envelope,
)

from oft.config import Settings

TRADE_SUMMARY_FIELDS = {
    "trade_id",
    "position_id",
    "symbol",
    "side",
    "opened_at",
    "closed_at",
    "gross_pnl_usd",
    "fees_usd",
    "funding_usd",
    "net_pnl_usd",
    "return_pct",
    "r_multiple",
    "mae_usd",
    "mfe_usd",
    "outcome_tags",
    "data_quality",
}
STATS_FIELDS = {
    "status",
    "reason",
    "period",
    "sample_size",
    "denominator",
    "exclusions",
    "definitions",
    "wins",
    "losses",
    "ties",
    "win_rate",
    "expectancy_usd",
    "avg_r",
    "totals",
    "data_quality",
}
EXCLUSION_FIELDS = {
    "open_positions",
    "partial_trades",
    "unreadable_trades",
    "outside_period",
}


class _TickingClock:
    """A monotonic test clock so two exports never share a filename."""

    def __init__(self, start: datetime) -> None:
        self._start = start
        self._count = 0

    def now(self) -> datetime:
        self._count += 1
        return self._start + timedelta(seconds=self._count)


def _mutate_trade(
    settings: Settings, trade_id: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM trade_logs WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        mutate(payload)
        connection.execute(
            "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
            (json.dumps(payload, sort_keys=True), trade_id),
        )
        connection.commit()


def _rewrite_trade_columns(
    settings: Settings,
    trade_id: str,
    *,
    symbol: str | None = None,
    closed_at: str | None = None,
) -> None:
    """Rewrite payload *and* the indexed columns the store filters on."""
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM trade_logs WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        assignments: list[str] = []
        params: list[Any] = []
        if symbol is not None:
            payload["symbol"] = symbol
            assignments.append("symbol = ?")
            params.append(symbol)
        if closed_at is not None:
            payload["closed_at"] = closed_at
            assignments.append("closed_at = ?")
            params.append(closed_at)
        assignments.append("payload_json = ?")
        params.append(json.dumps(payload, sort_keys=True))
        params.append(trade_id)
        connection.execute(
            f"UPDATE trade_logs SET {', '.join(assignments)} WHERE trade_id = ?", params
        )
        connection.commit()


def _mutate_position(
    settings: Settings, position_id: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM positions WHERE position_id = ?", (position_id,)
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        mutate(payload)
        connection.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ?",
            (json.dumps(payload, sort_keys=True), position_id),
        )
        connection.commit()


def _mark_complete(payload: dict[str, Any]) -> None:
    payload["data_quality"] = {
        **payload.get("data_quality", {}),
        "status": "COMPLETE",
        "reasons": [],
    }
    payload["outcome_tags"] = [
        tag for tag in payload.get("outcome_tags", []) if "PARTIAL" not in str(tag).upper()
    ]


def test_empty_stats_are_undefined_not_zeroed(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        stats_response = harness.client.get("/api/v1/journal/stats", headers=harness.auth)
        list_response = harness.client.get("/api/v1/journal", headers=harness.auth)
    finally:
        harness.client.close()

    assert stats_response.status_code == 200, stats_response.text
    stats_envelope = stats_response.json()
    _validate_envelope(stats_envelope)
    stats = stats_envelope["data"]
    assert set(stats) >= STATS_FIELDS
    assert stats["status"] == "UNDEFINED"
    assert stats["sample_size"] == 0
    assert stats["win_rate"] is None
    assert stats["expectancy_usd"] is None
    assert stats["avg_r"] == {"value": None, "samples": 0, "excluded_null_r": 0}
    assert all(value is None for value in stats["totals"].values())
    assert stats["definitions"]
    assert set(stats["exclusions"]) == EXCLUSION_FIELDS
    assert set(stats["exclusions"].values()) == {0}
    assert stats_envelope["meta"]["data_quality"] == "MISSING"
    # The schema itself rejects a fabricated zero; this is the honest shape.
    assert stats["wins"] == stats["losses"] == stats["ties"] == 0

    assert list_response.status_code == 200, list_response.text
    list_envelope = list_response.json()
    _validate_envelope(list_envelope)
    assert list_envelope["data"] == {"items": [], "total": 0}
    assert list_envelope["meta"]["data_quality"] == "MISSING"


def test_closed_trade_contract_and_defined_stats(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        position, trade = _closed_trade(harness)
        _mutate_trade(harness.settings, trade["trade_id"], _mark_complete)
        trade_id = trade["trade_id"]
        listed = harness.client.get("/api/v1/journal", headers=harness.auth)
        detail = harness.client.get(f"/api/v1/journal/{trade_id}", headers=harness.auth)
        stats = harness.client.get(
            "/api/v1/journal/stats", params={"period": "8w"}, headers=harness.auth
        )
        compare = harness.client.get(f"/api/v1/journal/compare/{trade_id}", headers=harness.auth)
        review = harness.client.get(f"/api/v1/journal/{trade_id}/review", headers=harness.auth)
    finally:
        harness.client.close()

    assert listed.status_code == 200, listed.text
    list_envelope = listed.json()
    _validate_envelope(list_envelope)
    list_data = list_envelope["data"]
    assert list_data["total"] == 1
    item = list_data["items"][0]
    assert set(item) >= TRADE_SUMMARY_FIELDS
    assert item["trade_id"] == trade_id
    assert item["position_id"] == position["position_id"]
    assert item["symbol"] == "BTCUSDT"
    assert item["side"] == "LONG"
    assert item["data_quality"]["status"] == "COMPLETE"

    assert detail.status_code == 200, detail.text
    detail_envelope = detail.json()
    _validate_envelope(detail_envelope)
    payload = detail_envelope["data"]
    assert payload["trade_id"] == trade_id
    assert set(payload["signal"]) >= {
        "scan_id",
        "report_id",
        "opportunity_id",
        "trigger_id",
        "decision_id",
    }
    assert payload["signal"]["trigger_id"] is not None
    assert payload["signal"]["opportunity_id"] is not None
    assert payload["entry_avg_price"]
    assert payload["exit_avg_price"]
    assert payload["review_decision_id"] is None

    assert stats.status_code == 200, stats.text
    stats_envelope = stats.json()
    _validate_envelope(stats_envelope)
    stats_data = stats_envelope["data"]
    assert stats_data["status"] == "DEFINED"
    assert stats_data["sample_size"] == 1
    assert stats_data["wins"] + stats_data["losses"] + stats_data["ties"] == 1
    assert stats_data["win_rate"] is not None
    assert stats_data["expectancy_usd"] is not None
    assert stats_data["avg_r"]["samples"] + stats_data["avg_r"]["excluded_null_r"] == 1
    assert stats_data["definitions"]
    assert stats_data["period"]["label"] == "8w"
    assert set(stats_data["exclusions"]) == EXCLUSION_FIELDS
    assert stats_data["exclusions"]["partial_trades"] == 0

    assert compare.status_code == 200, compare.text
    compare_envelope = compare.json()
    _validate_envelope(compare_envelope)
    comparison = compare_envelope["data"]
    assert comparison["trade_id"] == trade_id
    assert set(comparison) >= {
        "zone",
        "slippage",
        "first_touch",
        "decision",
        "reasons",
        "data_quality",
    }
    # The closed position has no recorded bars in its holding window, so the
    # first-touch side is a named N/A rather than an invented event.
    assert comparison["first_touch"]["status"] == "N/A"
    assert comparison["first_touch"]["reasons"]
    assert comparison["data_quality"] in {"COMPLETE", "PARTIAL"}

    assert review.status_code == 200, review.text
    review_envelope = review.json()
    _validate_envelope(review_envelope)
    assert review_envelope["data"]["status"] == "MISSING"
    assert review_envelope["data"]["advisory"] is True
    assert review_envelope["data"]["decision_id"] is None


def test_open_position_is_counted_and_excluded_from_stats(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        _position, trade = _closed_trade(harness)
        open_position = _open_position(harness, period="1h", key="journal-open")
        listed = harness.client.get("/api/v1/journal", headers=harness.auth)
        stats = harness.client.get(
            "/api/v1/journal/stats", params={"period": "8w"}, headers=harness.auth
        )
        positions = harness.client.get(
            "/api/v1/positions", params={"state": "OPEN"}, headers=harness.auth
        )
    finally:
        harness.client.close()

    assert listed.status_code == 200, listed.text
    list_data = listed.json()["data"]
    # The list endpoint returns closed trade logs only.
    assert list_data["total"] == 1
    assert [row["trade_id"] for row in list_data["items"]] == [trade["trade_id"]]

    assert positions.status_code == 200, positions.text
    assert positions.json()["data"]["total"] == 1
    assert positions.json()["data"]["items"][0]["position_id"] == open_position["position_id"]

    assert stats.status_code == 200, stats.text
    stats_data = stats.json()["data"]
    assert stats_data["exclusions"]["open_positions"] == 1
    # The seeded trade is PARTIAL (no recorded bars for MAE/MFE), so neither it
    # nor the open position is in the sample. Nothing is imputed.
    assert stats_data["exclusions"]["partial_trades"] == 1
    assert stats_data["sample_size"] == 0
    assert stats_data["status"] == "UNDEFINED"


def test_stats_and_list_apply_period_and_symbol_scope(tmp_path: Path) -> None:
    """The period window and symbol scope change the sample honestly.

    ``/journal`` filters the already-fetched table by side/outcome in the UI
    (asserted by the Web source test below); the server-side scope is period +
    symbol. An out-of-window trade is counted as excluded, never silently
    dropped, and a symbol filter scopes both the list and the stats sample.
    """
    harness = _harness(tmp_path)
    try:
        _position, trade = _closed_trade(harness)
        trade_id = trade["trade_id"]
        _mutate_trade(harness.settings, trade_id, _mark_complete)
        _rewrite_trade_columns(harness.settings, trade_id, closed_at="2020-01-01T00:00:00.000Z")
        out_of_window = harness.client.get(
            "/api/v1/journal/stats", params={"period": "8w"}, headers=harness.auth
        )
        all_time = harness.client.get("/api/v1/journal/stats", headers=harness.auth)

        _rewrite_trade_columns(harness.settings, trade_id, symbol="ETHUSDT")
        eth_list = harness.client.get(
            "/api/v1/journal", params={"symbol": "ETHUSDT"}, headers=harness.auth
        )
        btc_list = harness.client.get(
            "/api/v1/journal", params={"symbol": "BTCUSDT"}, headers=harness.auth
        )
        eth_stats = harness.client.get(
            "/api/v1/journal/stats", params={"symbol": "ethusdt"}, headers=harness.auth
        )
        btc_stats = harness.client.get(
            "/api/v1/journal/stats", params={"symbol": "BTCUSDT"}, headers=harness.auth
        )
    finally:
        harness.client.close()

    assert out_of_window.status_code == 200, out_of_window.text
    window_envelope = out_of_window.json()
    _validate_envelope(window_envelope)
    windowed = window_envelope["data"]
    assert windowed["status"] == "UNDEFINED"
    assert windowed["sample_size"] == 0
    assert windowed["exclusions"]["outside_period"] == 1
    assert "outside the period" in (windowed["reason"] or "")
    assert windowed["period"]["label"] == "8w"

    assert all_time.status_code == 200, all_time.text
    all_envelope = all_time.json()
    _validate_envelope(all_envelope)
    all_data = all_envelope["data"]
    assert all_data["status"] == "DEFINED"
    assert all_data["sample_size"] == 1
    assert all_data["exclusions"]["outside_period"] == 0

    assert eth_list.status_code == 200, eth_list.text
    eth_envelope = eth_list.json()
    _validate_envelope(eth_envelope)
    assert eth_envelope["data"]["total"] == 1
    assert [row["symbol"] for row in eth_envelope["data"]["items"]] == ["ETHUSDT"]

    assert btc_list.status_code == 200, btc_list.text
    btc_envelope = btc_list.json()
    _validate_envelope(btc_envelope)
    assert btc_envelope["data"] == {"items": [], "total": 0}
    assert btc_envelope["meta"]["data_quality"] == "MISSING"

    assert eth_stats.status_code == 200, eth_stats.text
    eth_stats_envelope = eth_stats.json()
    _validate_envelope(eth_stats_envelope)
    assert eth_stats_envelope["data"]["status"] == "DEFINED"
    assert eth_stats_envelope["data"]["sample_size"] == 1

    assert btc_stats.status_code == 200, btc_stats.text
    btc_stats_envelope = btc_stats.json()
    _validate_envelope(btc_stats_envelope)
    assert btc_stats_envelope["data"]["status"] == "UNDEFINED"
    assert btc_stats_envelope["data"]["sample_size"] == 0


def test_compare_missing_signal_is_named_na_without_blanking_metrics(tmp_path: Path) -> None:
    """A missing signal source degrades the comparison only, never the page."""
    harness = _harness(tmp_path)
    try:
        _position, trade = _closed_trade(harness)
        trade_id = trade["trade_id"]
        _mutate_trade(harness.settings, trade_id, _mark_complete)

        def _unavailable_signal(payload: dict[str, Any]) -> None:
            payload["signal"] = {
                "scan_id": None,
                "report_id": None,
                "opportunity_id": "OPP-MISSING",
                "trigger_id": "TRG-MISSING",
                "decision_id": None,
            }

        _mutate_trade(harness.settings, trade_id, _unavailable_signal)
        detail = harness.client.get(f"/api/v1/journal/{trade_id}", headers=harness.auth)
        compare = harness.client.get(f"/api/v1/journal/compare/{trade_id}", headers=harness.auth)
        stats = harness.client.get("/api/v1/journal/stats", headers=harness.auth)
        review = harness.client.get(f"/api/v1/journal/{trade_id}/review", headers=harness.auth)
    finally:
        harness.client.close()

    assert detail.status_code == 200, detail.text
    detail_envelope = detail.json()
    _validate_envelope(detail_envelope)
    assert detail_envelope["data"]["trade_id"] == trade_id
    assert detail_envelope["data"]["signal"]["opportunity_id"] == "OPP-MISSING"
    assert detail_envelope["data"]["signal"]["trigger_id"] == "TRG-MISSING"

    assert compare.status_code == 200, compare.text
    compare_envelope = compare.json()
    _validate_envelope(compare_envelope)
    data = compare_envelope["data"]
    # Unavailable sources are a named N/A with reasons, never an invented value.
    assert data["zone"]["status"] == "N/A"
    assert data["zone"]["inside"] is None
    assert data["zone"]["reasons"]
    assert data["slippage"]["status"] == "N/A"
    assert data["slippage"]["bps"] is None
    assert data["decision"]["status"] == "N/A"
    assert data["decision"]["matched"] is None
    assert data["data_quality"] in {"PARTIAL", "MISSING"}

    # The trade metrics and stats remain readable while the comparison degrades.
    assert stats.status_code == 200, stats.text
    assert stats.json()["data"]["status"] in {"DEFINED", "UNDEFINED"}
    assert review.status_code == 200, review.text
    assert review.json()["data"]["status"] == "MISSING"


def test_compare_reports_ambiguous_bar(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        position, trade = _closed_trade(harness)
        trade_id = trade["trade_id"]

        def _span_holding_window(payload: dict[str, Any]) -> None:
            payload["opened_at"] = "2026-10-07T03:00:00.000Z"
            payload["closed_at"] = "2026-10-07T05:00:00.000Z"

        def _both_levels_inside_one_bar(payload: dict[str, Any]) -> None:
            payload["stop_price"] = "70000.00"
            payload["target_prices"] = ["70001.00"]

        _mutate_trade(harness.settings, trade_id, _span_holding_window)
        _mutate_position(harness.settings, position["position_id"], _both_levels_inside_one_bar)
        compare = harness.client.get(f"/api/v1/journal/compare/{trade_id}", headers=harness.auth)
    finally:
        harness.client.close()

    assert compare.status_code == 200, compare.text
    envelope = compare.json()
    _validate_envelope(envelope)
    comparison = envelope["data"]
    first_touch = comparison["first_touch"]
    assert first_touch["status"] == "PARTIAL"
    assert first_touch["ambiguous"] is True
    assert any("AMBIGUOUS_BAR" in reason for reason in first_touch["reasons"])
    assert comparison["data_quality"] == "PARTIAL"


def test_export_file_neutralizes_formula_cells(tmp_path: Path) -> None:
    # A ticking clock keeps the two server-written exports on distinct
    # timestamps; the export filename is derived from the clock.
    harness = _harness(tmp_path, clock=_TickingClock(NOW))
    try:
        _position, trade = _closed_trade(harness)
        trade_id = trade["trade_id"]

        def _formula_tag(payload: dict[str, Any]) -> None:
            payload["outcome_tags"] = ["=2+3", "LOSS"]

        _mutate_trade(harness.settings, trade_id, _formula_tag)
        metadata = harness.client.get(
            "/api/v1/journal/export",
            params={"format": "csv", "period": "8w"},
            headers=harness.auth,
        )
        csv_file = harness.client.get(
            "/api/v1/journal/export/file",
            params={"format": "csv", "period": "8w"},
            headers=harness.auth,
        )
        json_file = harness.client.get(
            "/api/v1/journal/export/file",
            params={"format": "json"},
            headers=harness.auth,
        )
    finally:
        harness.client.close()

    assert metadata.status_code == 200, metadata.text
    metadata_envelope = metadata.json()
    _validate_envelope(metadata_envelope)
    exported = metadata_envelope["data"]
    assert exported["format"] == "csv"
    assert exported["rows"] == 1
    assert exported["neutralized_cells"] >= 1
    assert exported["path"].endswith(".csv")
    assert exported["data_quality"] in {"COMPLETE", "PARTIAL", "MISSING"}

    assert csv_file.status_code == 200, csv_file.text
    assert csv_file.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_file.headers["content-disposition"]
    assert "'=2+3" in csv_file.text
    assert "trade_id" in csv_file.text.splitlines()[0]

    assert json_file.status_code == 200, json_file.text
    assert json_file.headers["content-type"].startswith("application/json")
    document = json.loads(json_file.text)
    assert document["trades"][0]["trade_id"] == trade_id
    assert document["stats"]["status"] in {"DEFINED", "UNDEFINED"}


def test_journal_spa_allowlist_and_web_sources_are_safe() -> None:
    app_source = (ROOT / "src" / "oft" / "api" / "app.py").read_text(encoding="utf-8")
    assert '"/journal"' in app_source
    assert '"/journal/"' in app_source
    assert 'path.startswith("/journal/")' in app_source

    routes_source = (ROOT / "src" / "oft" / "api" / "routes" / "journal.py").read_text(
        encoding="utf-8"
    )
    assert "/api/v1/journal/export/file" in routes_source
    # The download route accepts no path parameter.
    assert "file_path" not in routes_source and "filename: str = Query" not in routes_source

    sources = [
        WEB_SRC / "routes" / "journal.tsx",
        WEB_SRC / "routes" / "journal-detail.tsx",
        WEB_SRC / "components" / "journal-table.tsx",
        WEB_SRC / "components" / "journal-detail-panels.tsx",
        WEB_SRC / "components" / "signal-comparison.tsx",
        WEB_SRC / "components" / "review-band.tsx",
        WEB_SRC / "api" / "journal.ts",
    ]
    for path in sources:
        assert path.exists(), f"missing {path}"
        source = path.read_text(encoding="utf-8")
        assert "dangerouslySetInnerHTML" not in source
        assert "innerHTML" not in source
        assert "javascript:" not in source
        assert "system_prompt" not in source
        assert "raw_prompt" not in source
        assert "prompt_body" not in source
        assert "api_key" not in source
        assert "secret_key" not in source
        assert 'target="_blank"' not in source


def test_journal_web_sources_cover_the_required_ui_states() -> None:
    """The list, detail, comparison, and review band render every brief state."""
    journal = (WEB_SRC / "routes" / "journal.tsx").read_text(encoding="utf-8")
    detail = (WEB_SRC / "routes" / "journal-detail.tsx").read_text(encoding="utf-8")
    panels = (WEB_SRC / "components" / "journal-detail-panels.tsx").read_text(encoding="utf-8")
    table = (WEB_SRC / "components" / "journal-table.tsx").read_text(encoding="utf-8")
    comparison = (WEB_SRC / "components" / "signal-comparison.tsx").read_text(encoding="utf-8")
    band = (WEB_SRC / "components" / "review-band.tsx").read_text(encoding="utf-8")
    api = (WEB_SRC / "api" / "journal.ts").read_text(encoding="utf-8")
    idempotency = (WEB_SRC / "api" / "idempotency.ts").read_text(encoding="utf-8")

    # The stats strip states every metric and its definition, and keeps the
    # honest undefined wording instead of zeroing an empty sample.
    for label in ("Net PnL", "Win rate", "Expectancy", "Avg R", "Sample", "Period"):
        assert label in journal, label
    for marker in (
        'data-testid="journal-stats"',
        'data-testid="journal-definitions"',
        'data-testid="journal-exclusions"',
        "metrics undefined",
        "还没有已平仓交易",
        "定义",
    ):
        assert marker in journal, marker
    # Explicit period/symbol/side/outcome filters with an explicit submit.
    for marker in (
        'id="journal-period"',
        'id="journal-symbol"',
        'id="journal-side"',
        'id="journal-outcome"',
        "应用筛选",
    ):
        assert marker in journal, marker
    # The applied period window filters the closed-trade table exactly like the
    # server stats; there is no copy claiming otherwise.
    assert "periodWindowOf" in journal
    assert "filterClosedRows" in journal
    assert "period 仅作用于统计与导出" not in journal
    assert "表格不受 period 影响" not in journal
    assert 'data-testid="journal-filter-announcement"' in journal
    assert 'data-testid="journal-truncation-note"' in journal
    # 375/768 is a read-only view on both screens.
    assert "data-readonly={compact}" in journal
    assert "data-readonly={compact}" in detail

    # The closed-trade table excludes OPEN rows explicitly and stays keyboard
    # scrollable with a semantic caption.
    assert "OPEN excluded" in table
    assert 'aria-label="交易日志表"' in table
    assert 'role="region"' in table
    assert "caption" in table
    assert 'scope="col"' in table and 'scope="row"' in table

    # The detail renders each required section and the partial/N-A vocabulary.
    for marker in (
        "时间线",
        "signal → trigger → decision → entry → management → exit",
        "Signal snapshot",
        "Fills",
        "LLM 决策记录",
        "Signal vs Outcome",
        "N/A",
        "PARTIAL",
    ):
        assert marker in detail or marker in comparison or marker in panels, marker
    assert "N/A" in comparison
    assert "AMBIGUOUS_BAR" in comparison

    # The review band is visibly advisory, offers generation and retry, and
    # never turns into a rule-change/apply surface.
    for marker in (
        "仅建议，不自动修改规则",
        "生成复盘",
        "重试生成",
        "幂等重放",
        'data-testid="review-band"',
    ):
        assert marker in band, marker
    assert "应用" not in band
    assert "apply" not in band.lower()
    assert "规则" in band  # states that no rule is modified

    for source in (journal, detail, panels, table, comparison, band, api):
        assert "rule_change" not in source.lower()
        assert "applyRule" not in source
        assert "onApply" not in source
        assert "/settings" not in source
        assert "/api/v1/live" not in source

    # The review mutation carries the real CSRF + idempotency contract. The
    # header construction lives in the shared idempotency module (slice-30 fix).
    assert "mutationHeaders" in api
    assert "X-CSRF-Token" in idempotency
    assert "Idempotency-Key" in idempotency
    assert "oft_csrf" in idempotency


def _export_dir(harness: Any) -> Path:
    return harness.settings.data_dir / "exports"


@pytest.mark.parametrize("failure", ["replace", "write", "mkdir"])
def test_export_write_failure_maps_to_storage_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A directory/write/publish OSError is a 503, never a 500, with no leftovers."""
    import oft.application.journal_service as journal_service_module

    harness = _harness(tmp_path)
    try:
        if failure == "replace":

            def _boom(*_args: Any, **_kwargs: Any) -> None:
                raise OSError("disk full")

            monkeypatch.setattr(journal_service_module.os, "replace", _boom)
        elif failure == "write":

            def _boom_fdopen(*_args: Any, **_kwargs: Any) -> Any:
                raise OSError("disk full")

            monkeypatch.setattr(journal_service_module.os, "fdopen", _boom_fdopen)
        else:
            original_mkdir = Path.mkdir

            def _boom_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
                # Only the export directory fails; tmp_path setup is untouched.
                if self.name == "exports":
                    raise OSError("cannot create export dir")
                original_mkdir(self, *args, **kwargs)

            monkeypatch.setattr(Path, "mkdir", _boom_mkdir)

        metadata = harness.client.get(
            "/api/v1/journal/export", params={"format": "csv"}, headers=harness.auth
        )
        file_response = harness.client.get(
            "/api/v1/journal/export/file", params={"format": "json"}, headers=harness.auth
        )
        export_dir = _export_dir(harness)
        leftovers = (
            sorted(path.name for path in export_dir.iterdir()) if export_dir.exists() else []
        )
    finally:
        harness.client.close()

    for response in (metadata, file_response):
        assert response.status_code == 503, response.text
        envelope = response.json()
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "STORAGE_UNAVAILABLE"
        # The absolute data path never leaks to the client.
        assert str(harness.settings.data_dir) not in response.text

    # No partial final file and no leftover temp file.
    assert leftovers == []
