"""Integration: journal statistics, comparison, and export over persisted trades."""

from __future__ import annotations

import json
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oft.application import recorded_bars
from oft.application.entry_service import EntryService
from oft.application.journal_service import JournalError, JournalNotFound, JournalService
from oft.application.kill_switch import KillSwitch
from oft.application.position_service import PositionService
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.store import StoreUnavailableError
from position_support import set_position_mark

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = datetime(2026, 10, 1, tzinfo=UTC).date()
END = datetime(2026, 10, 7, tzinfo=UTC).date()
NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _closed_trade(
    tmp_path: Path,
) -> tuple[Settings, Any, dict[str, Any], dict[str, Any]]:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert run.trigger is not None
    kill = KillSwitch(settings=settings, store=container.store, clock=container.clock)
    entry = EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=settings,
        kill_switch=kill,
    )
    accepted = entry.accept(run.trigger.trigger_id, idempotency_key="journal-accept")
    assert accepted.position is not None
    position = accepted.position
    # The fixed clock makes the stored mark (``updated_at`` = NOW) fresh.
    set_position_mark(settings, position["position_id"], position["stop_price"])
    monitored = PositionService(
        store=container.store, clock=container.clock, settings=settings
    ).monitor(position["position_id"])
    assert monitored.trade_log is not None
    return settings, container, position, monitored.trade_log


def _service(container: Any) -> JournalService:
    return JournalService(container.store, container.clock, container.settings)


def _patch_trade(tmp_path: Path, trade_id: str, **changes: Any) -> None:
    database = (tmp_path / "data") / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM trade_logs WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload.update(changes)
        connection.execute(
            "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
            (json.dumps(payload, sort_keys=True), trade_id),
        )
        connection.commit()


def test_statistics_exclude_a_partial_trade_and_are_undefined(
    tmp_path: Path,
) -> None:
    _settings_value, container, _position, trade = _closed_trade(tmp_path)
    assert trade["data_quality"]["status"] == "PARTIAL"

    stats = _service(container).stats()

    assert stats["status"] == "UNDEFINED"
    assert stats["sample_size"] == 0
    assert stats["exclusions"]["partial_trades"] == 1
    assert stats["win_rate"] is None
    assert stats["expectancy_usd"] is None
    assert stats["avg_r"]["value"] is None
    assert stats["totals"]["net_pnl_usd"] is None
    assert str(stats["reason"]).startswith("metrics undefined")


def test_statistics_consume_a_complete_trade_and_its_net_pnl(tmp_path: Path) -> None:
    _settings_value, container, _position, trade = _closed_trade(tmp_path)
    _patch_trade(
        tmp_path,
        trade["trade_id"],
        data_quality={
            "status": "COMPLETE",
            "reasons": [],
            "gaps": [],
            "last_event_at": trade["closed_at"],
            "coverage_start": trade["opened_at"],
        },
        outcome_tags=["LOSS", "STOPPED"],
    )

    stats = _service(container).stats()
    net = trade["net_pnl_usd"]

    assert stats["status"] == "DEFINED"
    assert stats["sample_size"] == 1
    assert stats["win_rate"] == (1.0 if float(net) > 0 else 0.0)
    assert stats["wins"] + stats["losses"] + stats["ties"] == 1
    assert stats["totals"]["net_pnl_usd"] == net
    assert stats["expectancy_usd"] == net


def test_statistics_period_filter_uses_the_fixed_clock(tmp_path: Path) -> None:
    _settings_value, container, _position, trade = _closed_trade(tmp_path)
    _patch_trade(
        tmp_path,
        trade["trade_id"],
        data_quality={
            "status": "COMPLETE",
            "reasons": [],
            "gaps": [],
            "last_event_at": trade["closed_at"],
            "coverage_start": trade["opened_at"],
        },
        outcome_tags=["LOSS", "STOPPED"],
    )

    inside = _service(container).stats(period="8w")
    assert inside["period"]["label"] == "8w"
    assert inside["sample_size"] == 1
    assert inside["exclusions"]["outside_period"] == 0

    # Move the trade outside a one-day window.
    _patch_trade(
        tmp_path,
        trade["trade_id"],
        closed_at=(NOW - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    )
    outside = _service(container).stats(period="1d")
    assert outside["status"] == "UNDEFINED"
    assert outside["exclusions"]["outside_period"] == 1
    assert outside["exclusions"]["partial_trades"] == 0


def test_open_positions_are_counted_as_exclusions(tmp_path: Path) -> None:
    _settings_value, container, _position, trade = _closed_trade(tmp_path)
    _patch_trade(
        tmp_path,
        trade["trade_id"],
        data_quality={
            "status": "COMPLETE",
            "reasons": [],
            "gaps": [],
            "last_event_at": trade["closed_at"],
            "coverage_start": trade["opened_at"],
        },
        outcome_tags=["LOSS", "STOPPED"],
    )
    # The accepted position closed, so no OPEN row remains; report one explicitly.
    with sqlite3.connect((tmp_path / "data") / "oft.db") as connection:
        connection.execute(
            "UPDATE positions SET state = 'OPEN' WHERE position_id = ?",
            (trade["position_id"],),
        )
        connection.commit()

    stats = _service(container).stats()

    assert stats["exclusions"]["open_positions"] == 1
    assert stats["sample_size"] == 1


def test_compare_reports_signal_vs_outcome_and_marks_unavailable_sides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings_value, container, _position, trade = _closed_trade(tmp_path)

    comparison = _service(container).compare(trade["trade_id"])

    assert comparison["trade_id"] == trade["trade_id"]
    assert comparison["zone"]["status"] in {"INSIDE", "ABOVE_ZONE", "BELOW_ZONE"}
    assert comparison["slippage"]["status"] == "OK"
    assert comparison["decision"]["status"] == "OK"
    assert comparison["decision"]["decision"] in {"ENTER_LONG", "ENTER_SHORT", "NO_ENTRY"}
    # The review window has no recorded bars at the fixed close instant.
    assert comparison["first_touch"]["status"] == "N/A"
    assert comparison["first_touch"]["reasons"]
    assert comparison["data_quality"] == "PARTIAL"

    monkeypatch.setattr(container.store, "get_opportunity", lambda *_args: None)
    missing_zone = _service(container).compare(trade["trade_id"])
    assert missing_zone["zone"]["status"] == "N/A"
    assert missing_zone["slippage"]["status"] == "N/A"

    monkeypatch.setattr(container.store, "get_decision", lambda *_args: None)
    missing_decision = _service(container).compare(trade["trade_id"])
    assert missing_decision["decision"]["status"] == "N/A"
    assert missing_decision["decision"]["matched"] is None


def test_compare_reports_an_ambiguous_first_touch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings_value, container, _position, trade = _closed_trade(tmp_path)
    service = _service(container)
    bar = {
        "bar_end": trade["closed_at"],
        "low": "0.01",
        "high": "999999",
    }
    monkeypatch.setattr(
        service,
        "_window_bars",
        lambda *_args, **_kwargs: recorded_bars.WindowBars(rows=(bar,)),
    )

    comparison = service.compare(trade["trade_id"])

    assert comparison["first_touch"]["ambiguous"] is True
    assert comparison["first_touch"]["kind"] == "STOP"
    assert comparison["first_touch"]["status"] == "PARTIAL"
    assert any("AMBIGUOUS_BAR" in reason for reason in comparison["first_touch"]["reasons"])


def test_csv_export_neutralizes_formulas_and_never_exports_prompts(
    tmp_path: Path,
) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    _patch_trade(tmp_path, trade["trade_id"], symbol="=BTCUSDT")

    result = _service(container).export("csv")

    path = Path(result["path"])
    assert path.is_file()
    assert result["format"] == "csv"
    assert result["rows"] == 1
    assert result["bytes"] > 0
    assert result["neutralized"] >= 1
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("trade_id,position_id")
    assert "'=BTCUSDT" in text
    assert "system_prompt" not in text
    assert "api_key" not in text.lower()
    assert "raw_output" not in text


def test_json_export_is_versioned_and_validates_each_trade(tmp_path: Path) -> None:
    _settings_value, container, _position, trade = _closed_trade(tmp_path)

    result = _service(container).export("json")

    path = Path(result["path"])
    assert path.is_file()
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == "1.0.0"
    assert document["generated_at"].endswith("Z")
    assert [item["trade_id"] for item in document["trades"]] == [trade["trade_id"]]
    assert document["stats"]["sample_size"] == 0  # the PARTIAL trade is excluded
    dumped = json.dumps(document).lower()
    assert "system_prompt" not in dumped
    assert "raw_output" not in dumped
    assert "api_key" not in dumped


def test_an_unknown_export_format_is_a_typed_validation_failure(
    tmp_path: Path,
) -> None:
    _settings_value, container, _position, _trade = _closed_trade(tmp_path)

    with pytest.raises(JournalError) as excinfo:
        _service(container).export("parquet")

    assert excinfo.value.code == "VALIDATION_FAILED"


def test_a_huge_period_is_a_typed_validation_failure_on_stats_and_export(
    tmp_path: Path,
) -> None:
    _settings_value, container, _position, _trade = _closed_trade(tmp_path)
    service = _service(container)

    for period in ("999999d", "999999999999999m"):
        with pytest.raises(JournalError) as stats_error:
            service.stats(period=period)
        assert stats_error.value.code == "VALIDATION_FAILED"
        with pytest.raises(JournalError) as export_error:
            service.export("csv", period=period)
        assert export_error.value.code == "VALIDATION_FAILED"


def test_list_returns_summaries_and_counts_unreadable_rows(
    tmp_path: Path,
) -> None:
    _settings_value, container, _position, trade = _closed_trade(tmp_path)
    service = _service(container)

    listed = service.list()

    assert listed["total"] == 1
    assert [item["trade_id"] for item in listed["items"]] == [trade["trade_id"]]

    _corrupt_trade(tmp_path, trade["trade_id"])
    degraded = service.list()

    # The unreadable row is dropped from the page but still counted in the total.
    assert degraded["items"] == []
    assert degraded["total"] == 1


def test_show_rejects_an_unknown_or_unreadable_trade(tmp_path: Path) -> None:
    _settings_value, container, _position, trade = _closed_trade(tmp_path)
    service = _service(container)

    with pytest.raises(JournalNotFound) as missing:
        service.show("TRD-MISSING")
    assert missing.value.code == "NOT_FOUND"

    _corrupt_trade(tmp_path, trade["trade_id"])
    with pytest.raises(JournalError) as unreadable:
        service.show(trade["trade_id"])
    assert unreadable.value.code == "DATA_UNAVAILABLE"


def test_compare_degrades_a_missing_or_corrupt_report_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings_value, container, _position, trade = _closed_trade(tmp_path)
    service = _service(container)

    monkeypatch.setattr(container.store, "get_report", lambda *_args: None)
    missing = service.compare(trade["trade_id"])
    assert missing["slippage"]["status"] == "N/A"
    assert any("snapshot" in reason for reason in missing["slippage"]["reasons"])

    monkeypatch.setattr(
        container.store,
        "get_report",
        lambda *_args: SimpleNamespace(snapshot_json="{not-json"),
    )
    corrupt = service.compare(trade["trade_id"])
    assert corrupt["slippage"]["status"] == "N/A"

    monkeypatch.setattr(
        container.store,
        "get_report",
        lambda *_args: SimpleNamespace(snapshot_json="[]"),
    )
    non_object = service.compare(trade["trade_id"])
    assert non_object["slippage"]["status"] == "N/A"


def test_compare_reports_a_manifest_failure_as_unavailable_bars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings_value, container, _position, trade = _closed_trade(tmp_path)

    def explode(**_kwargs: object) -> tuple[Any, ...]:
        raise StoreUnavailableError("manifest unavailable")

    monkeypatch.setattr(container.store, "list_manifest", explode)
    comparison = _service(container).compare(trade["trade_id"])

    assert comparison["first_touch"]["status"] == "N/A"
    assert comparison["first_touch"]["kind"] is None
    assert comparison["first_touch"]["reasons"] == ["recorded bars are unavailable"]


def test_compare_never_labels_a_surviving_series_as_a_measured_touch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings_value, container, _position, trade = _closed_trade(tmp_path)
    entry = SimpleNamespace(
        data_type="bars",
        symbol=trade["symbol"],
        status="PUBLISHED",
        path="data/bars/symbol=BTCUSDT/period=5m/metric_version=ofi.v1.x/date=x/p.parquet",
        metric_version="ofi.v1.x",
    )

    def explode(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        raise OSError("part unreadable")

    monkeypatch.setattr(container.store, "list_manifest", lambda **_kwargs: (entry,))
    monkeypatch.setattr(recorded_bars, "read_bars", explode)
    comparison = _service(container).compare(trade["trade_id"])

    assert comparison["first_touch"]["status"] == "N/A"
    assert comparison["first_touch"]["reasons"] == ["recorded bars are unavailable"]


def test_export_skips_an_unreadable_row_and_reports_the_worse_quality(
    tmp_path: Path,
) -> None:
    _settings_value, container, _position, trade = _closed_trade(tmp_path)
    _corrupt_trade(tmp_path, trade["trade_id"])

    result = _service(container).export("csv")

    assert result["rows"] == 0
    assert result["skipped_unreadable"] == 1
    # "PARTIAL" (a skipped row) is worse than "COMPLETE" but "MISSING" (the
    # embedded stats are undefined) is worse still.
    assert result["data_quality"] == "MISSING"


def test_export_quality_is_not_complete_when_the_embedded_stats_are_missing(
    tmp_path: Path,
) -> None:
    _settings_value, container, _position, _trade = _closed_trade(tmp_path)

    # The only trade is PARTIAL, so it is exported but excluded from the stats:
    # the envelope must not claim COMPLETE while its stats say MISSING.
    result = _service(container).export("csv")

    assert result["rows"] == 1
    assert result["skipped_unreadable"] == 0
    assert result["data_quality"] == "MISSING"


def test_export_writes_an_owner_only_file(tmp_path: Path) -> None:
    _settings_value, container, _position, _trade = _closed_trade(tmp_path)

    result = _service(container).export("csv")

    path = Path(result["path"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _corrupt_trade(tmp_path: Path, trade_id: str) -> None:
    database = (tmp_path / "data") / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
            ("{not-json", trade_id),
        )
        connection.commit()
