"""Unit tests for the server-derived chart overlays (slice 40).

The chart never infers overlays in the client: every zone and marker below is
derived from the report's linked opportunity and position/trigger records by
``derive_overlays``. These tests use a tiny in-memory fake store so the
derivation rules (verbatim prices, state pass-through, defensive parsing, and
the marker omission rules) are exercised without a database.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from oft.api.routes.reports import read_report_chart
from oft.application.chart_service import derive_overlays, resolve_chart
from oft.ports.store import (
    OpportunityRecord,
    PositionRecord,
    ReportHeader,
    ReportRecord,
    TriggerEventRecord,
)

REPORT_ID = "RPT-LINKED"


def _report(symbol: str = "BTCUSDT", report_id: str = REPORT_ID) -> ReportRecord:
    return ReportRecord(
        report_id=report_id,
        scan_id="SCAN-1",
        symbol=symbol,
        timeframe="4h",
        generated_at="2026-10-08T00:00:00.000Z",
        metrics_manifest_json="[]",
        llm_decision_id=None,
        narrative_ref=None,
        data_quality="COMPLETE",
        snapshot_json="{}",
        metrics_json="{}",
    )


def _opportunity(
    opportunity_id: str = "OPP-1",
    *,
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    state: str = "TRIGGERED",
    low: str = "69000.00",
    high: str = "71000.00",
    confidence: Any = 0.72,
    report_id: str = REPORT_ID,
    payload_override: Any = None,
) -> OpportunityRecord:
    payload: Any = (
        payload_override
        if payload_override is not None
        else {
            "opportunity_id": opportunity_id,
            "symbol": symbol,
            "side": side,
            "state": state,
            "zone": {"low": low, "high": high},
            "confidence": confidence,
            "source": {
                "scan_id": "SCAN-1",
                "report_id": report_id,
                "decision_id": "DEC-1",
            },
        }
    )
    return OpportunityRecord(
        opportunity_id=opportunity_id,
        symbol=symbol,
        state=state,
        created_at="2026-10-01T00:00:00.000Z",
        updated_at="2026-10-02T00:00:00.000Z",
        payload_json=payload if isinstance(payload, str) else json.dumps(payload),
    )


def _trigger(trigger_id: str = "TRG-1", opportunity_id: str = "OPP-1") -> TriggerEventRecord:
    return TriggerEventRecord(
        trigger_id=trigger_id,
        opportunity_id=opportunity_id,
        symbol="BTCUSDT",
        status="ACCEPTED",
        idempotency_key=f"key-{trigger_id}",
        triggered_at="2026-10-02T00:00:00.000Z",
        payload_json="{}",
    )


def _position(
    position_id: str = "POS-1",
    *,
    symbol: str = "BTCUSDT",
    source_trigger_id: Any = "TRG-1",
    entry: Any = "70000.00",
    stop: Any = "69000.00",
    targets: Sequence[Any] = ("72000.00",),
    exit_price: Any = None,
    payload_override: Any = None,
) -> PositionRecord:
    payload: Any = (
        payload_override
        if payload_override is not None
        else {
            "position_id": position_id,
            "symbol": symbol,
            "source_trigger_id": source_trigger_id,
            "avg_entry_price": entry,
            "entry_price": entry,
            "stop_price": stop,
            "target_prices": list(targets),
            "exit_price": exit_price,
        }
    )
    return PositionRecord(
        position_id=position_id,
        account_id="ACC-PAPER",
        environment="PAPER",
        symbol=symbol,
        state="CLOSED" if exit_price else "OPEN",
        opened_at="2026-10-02T00:00:00.000Z",
        payload_json=payload if isinstance(payload, str) else json.dumps(payload),
    )


def _payload(record_json: str) -> Mapping[str, Any] | None:
    """Mirror the adapter's defensive JSON read: corrupt rows are skipped."""
    try:
        payload = json.loads(record_json)
    except (TypeError, ValueError, RecursionError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _opportunity_report_id(record: OpportunityRecord) -> Any:
    payload = _payload(record.payload_json)
    if payload is None:
        return None
    source = payload.get("source")
    return source.get("report_id") if isinstance(source, Mapping) else None


class FakeStore:
    """The scoped reads ``derive_overlays`` performs, plus a MISSING chart read.

    ``list_opportunities``/``list_positions`` honour the same optional
    ``symbol``/``report_id`` filters the SQLite adapter applies, including the
    position linkage through trigger -> opportunity -> report.
    """

    def __init__(
        self,
        *,
        opportunities: Sequence[OpportunityRecord] = (),
        positions: Sequence[PositionRecord] = (),
        triggers: Sequence[TriggerEventRecord] = (),
        headers: Sequence[ReportHeader] = (),
    ) -> None:
        self.opportunities = tuple(opportunities)
        self.positions = tuple(positions)
        self.triggers = {trigger.trigger_id: trigger for trigger in triggers}
        self.headers = tuple(headers)

    def list_opportunities(
        self,
        *,
        state: str | None = None,
        symbol: str | None = None,
        report_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[OpportunityRecord, ...]:
        rows = list(self.opportunities)
        if state is not None:
            rows = [record for record in rows if record.state == state]
        if symbol is not None:
            rows = [record for record in rows if record.symbol == symbol]
        if report_id is not None:
            rows = [record for record in rows if _opportunity_report_id(record) == report_id]
        return tuple(rows[offset : offset + limit])

    def list_positions(
        self,
        *,
        state: str | None = None,
        symbol: str | None = None,
        report_id: str | None = None,
        limit: int | None = 100,
        offset: int = 0,
    ) -> tuple[PositionRecord, ...]:
        rows = list(self.positions)
        if state is not None:
            rows = [record for record in rows if record.state == state]
        if symbol is not None:
            rows = [record for record in rows if record.symbol == symbol]
        if report_id is not None:
            linked_ids = {
                record.opportunity_id
                for record in self.opportunities
                if _opportunity_report_id(record) == report_id
            }
            trigger_opportunities = {
                trigger.trigger_id: trigger.opportunity_id for trigger in self.triggers.values()
            }

            def linked(record: PositionRecord) -> bool:
                payload = _payload(record.payload_json)
                if payload is None:
                    return False
                trigger_id = payload.get("source_trigger_id")
                if not isinstance(trigger_id, str) or not trigger_id:
                    return False
                return trigger_opportunities.get(trigger_id) in linked_ids

            rows = [record for record in rows if linked(record)]
        rows = rows[offset : offset + limit] if limit is not None else rows[offset:]
        return tuple(rows)

    def get_opportunity(self, opportunity_id: str) -> OpportunityRecord | None:
        return next(
            (record for record in self.opportunities if record.opportunity_id == opportunity_id),
            None,
        )

    def list_report_headers_for_symbol(self, symbol: str) -> tuple[ReportHeader, ...]:
        return self.headers

    def get_report(self, report_id: str) -> ReportRecord | None:
        return None


def _overlays(store: FakeStore, report: ReportRecord | None = None):
    return derive_overlays(report or _report(), store)


def _zone_by_id(zones: list[dict[str, Any]], opportunity_id: str) -> dict[str, Any]:
    return next(zone for zone in zones if zone["opportunity_id"] == opportunity_id)


def _markers(kind: str, markers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [marker for marker in markers if marker["kind"] == kind]


# -- opportunity zones -------------------------------------------------------


def test_triggered_long_zone_keeps_prices_verbatim_and_triggers_at_zone_high() -> None:
    store = FakeStore(opportunities=[_opportunity(side="LONG", low="69000.123", high="71000.456")])

    zones, _, _ = _overlays(store)

    assert len(zones) == 1
    zone = zones[0]
    assert zone["opportunity_id"] == "OPP-1"
    assert zone["side"] == "LONG"
    assert zone["state"] == "TRIGGERED"
    assert zone["low"] == "69000.123"
    assert zone["high"] == "71000.456"
    assert zone["trigger_price"] == "71000.456"
    assert zone["label"] == "OPP-1 · LONG · 0.72"


def test_short_zone_triggers_at_zone_low_verbatim() -> None:
    store = FakeStore(opportunities=[_opportunity(side="SHORT", low="69000.123", high="71000.456")])

    zones, _, _ = _overlays(store)

    assert zones[0]["side"] == "SHORT"
    assert zones[0]["trigger_price"] == "69000.123"


@pytest.mark.parametrize("state", ["EXPIRED", "INVALIDATED", "OPEN"])
def test_opportunity_state_passes_through_unchanged(state: str) -> None:
    store = FakeStore(opportunities=[_opportunity(state=state)])

    zones, _, _ = _overlays(store)

    assert zones[0]["state"] == state


def test_unknown_side_and_corrupt_payloads_are_skipped() -> None:
    store = FakeStore(
        opportunities=[
            _opportunity("OPP-FLAT", side="FLAT"),
            _opportunity("OPP-BROKEN", payload_override="{not json"),
            _opportunity("OPP-LIST", payload_override="[1, 2, 3]"),
            _opportunity("OPP-BAD-ZONE", payload_override={"symbol": "BTCUSDT", "side": "LONG"}),
            _opportunity("OPP-LONG"),
        ]
    )

    zones, markers, _ = _overlays(store)

    assert [zone["opportunity_id"] for zone in zones] == ["OPP-LONG"]
    assert markers == []


def test_opportunity_for_another_report_or_symbol_is_skipped() -> None:
    store = FakeStore(
        opportunities=[
            _opportunity("OPP-OTHER-REPORT", report_id="RPT-OTHER"),
            _opportunity("OPP-OTHER-SYMBOL", symbol="ETHUSDT"),
            _opportunity("OPP-LINKED"),
        ]
    )

    zones, _, _ = _overlays(store)

    assert [zone["opportunity_id"] for zone in zones] == ["OPP-LINKED"]


def test_label_is_bounded_to_80_characters() -> None:
    long_id = "OPP-" + "9" * 120
    store = FakeStore(opportunities=[_opportunity(long_id)])

    zones, _, _ = _overlays(store)

    assert len(zones[0]["label"]) == 80
    assert zones[0]["label"].startswith("OPP-999")


def test_label_carries_untrusted_text_verbatim_without_markup() -> None:
    hostile = '<img src=x onerror="alert(1)">'
    store = FakeStore(opportunities=[_opportunity(hostile)])

    zones, _, _ = _overlays(store)

    label = zones[0]["label"]
    assert label.startswith(hostile)
    # The server never escapes or injects markup; escaping is the renderer's job.
    assert "&lt;" not in label
    assert len(label) <= 80


def test_label_omits_confidence_when_it_is_not_numeric() -> None:
    store = FakeStore(opportunities=[_opportunity(confidence="high")])

    zones, _, _ = _overlays(store)

    assert zones[0]["label"] == "OPP-1 · LONG"


@pytest.mark.parametrize(
    "low, high, side",
    [
        ("0", "71000.00", "LONG"),
        ("69000.00", "-1", "LONG"),
        ("not-a-price", "71000.00", "SHORT"),
        ("69000.00", "NaN", "SHORT"),
        (None, "71000.00", "SHORT"),
        ("69000.00", None, "LONG"),
    ],
)
def test_zone_with_corrupt_bounds_is_skipped_not_advertised(low: Any, high: Any, side: str) -> None:
    """A non-positive or unparseable bound skips the zone, like a marker price."""
    store = FakeStore(opportunities=[_opportunity(low=low, high=high, side=side)])

    zones, _, _ = _overlays(store)

    assert zones == []


# -- deeply nested corrupt JSON ------------------------------------------------

_DEEP_JSON = "[" * 10_000 + "]" * 10_000


def test_deeply_nested_opportunity_payload_is_skipped() -> None:
    """A RecursionError from json.loads is a skip, never raised out of a read."""
    store = FakeStore(
        opportunities=[_opportunity("OPP-DEEP", payload_override=_DEEP_JSON)],
    )

    zones, markers, _ = _overlays(store)

    assert zones == []
    assert markers == []


def test_deeply_nested_position_payload_is_skipped() -> None:
    store = FakeStore(
        opportunities=[_opportunity()],
        positions=[_position(payload_override=_DEEP_JSON)],
        triggers=[_trigger()],
    )

    _, markers, _ = _overlays(store)

    assert markers == []


def test_deeply_nested_frozen_snapshot_resolves_to_a_missing_payload() -> None:
    store = FakeStore()
    report = replace(_report(), snapshot_json=_DEEP_JSON)

    payload = resolve_chart(report, "4h", store)

    assert payload["status"] == "MISSING"
    assert payload["bars"] == []
    assert payload["metrics"] is None
    assert "frozen JSON" in payload["reason"]


# -- position markers --------------------------------------------------------


def test_markers_keep_exact_prices_and_carry_the_position_id() -> None:
    store = FakeStore(
        opportunities=[_opportunity()],
        positions=[_position(entry="70000.123", stop="69000.456", targets=("72000.789",))],
        triggers=[_trigger()],
    )

    _, markers, _ = _overlays(store)

    entry = _markers("entry", markers)[0]
    stop = _markers("stop", markers)[0]
    target = _markers("target", markers)[0]
    assert entry["price"] == "70000.123"
    assert entry["label"] == "ENTRY 70000.123"
    assert entry["position_id"] == "POS-1"
    assert stop["price"] == "69000.456"
    assert stop["label"] == "STOP 69000.456"
    assert target["price"] == "72000.789"
    assert target["label"] == "TARGET 72000.789"


def test_closed_position_adds_an_exit_marker() -> None:
    store = FakeStore(
        opportunities=[_opportunity()],
        positions=[_position(exit_price="70500.00")],
        triggers=[_trigger()],
    )

    _, markers, _ = _overlays(store)

    exit_marker = _markers("exit", markers)[0]
    assert exit_marker["price"] == "70500.00"
    assert exit_marker["label"] == "EXIT 70500.00"


@pytest.mark.parametrize("stop", [None, "0", "0.0", "not-a-price", "-1", "NaN", "Infinity", 69000])
def test_missing_zero_or_unparseable_stop_is_omitted_not_placed_at_zero(
    stop: Any,
) -> None:
    store = FakeStore(
        opportunities=[_opportunity()],
        positions=[_position(stop=stop)],
        triggers=[_trigger()],
    )

    _, markers, _ = _overlays(store)

    assert _markers("stop", markers) == []
    assert all(marker["price"] != "0" for marker in markers)


@pytest.mark.parametrize("entry", [None, "", "0", "bad", 70000])
def test_missing_or_unparseable_entry_is_omitted(entry: Any) -> None:
    store = FakeStore(
        opportunities=[_opportunity()],
        positions=[_position(entry=entry)],
        triggers=[_trigger()],
    )

    _, markers, _ = _overlays(store)

    assert _markers("entry", markers) == []


def test_entry_falls_back_to_entry_price_when_average_is_missing() -> None:
    payload = {
        "position_id": "POS-1",
        "symbol": "BTCUSDT",
        "source_trigger_id": "TRG-1",
        "avg_entry_price": None,
        "entry_price": "70123.45",
        "stop_price": None,
        "target_prices": [],
        "exit_price": None,
    }
    store = FakeStore(
        opportunities=[_opportunity()],
        positions=[_position(payload_override=payload)],
        triggers=[_trigger()],
    )

    _, markers, _ = _overlays(store)

    assert _markers("entry", markers)[0]["price"] == "70123.45"


@pytest.mark.parametrize(
    "trigger_id, trigger_opportunity, symbol",
    [
        ("TRG-UNLINKED", "OPP-OTHER", "BTCUSDT"),
        ("TRG-1", "OPP-1", "ETHUSDT"),
    ],
)
def test_position_not_linked_to_the_report_is_omitted(
    trigger_id: str, trigger_opportunity: str, symbol: str
) -> None:
    store = FakeStore(
        opportunities=[_opportunity()],
        positions=[_position(source_trigger_id=trigger_id, symbol=symbol)],
        triggers=[_trigger(trigger_id=trigger_id, opportunity_id=trigger_opportunity)],
    )

    _, markers, _ = _overlays(store)

    assert markers == []


def test_position_whose_trigger_row_is_missing_is_omitted() -> None:
    store = FakeStore(
        opportunities=[_opportunity()],
        positions=[_position(source_trigger_id="TRG-GONE")],
        triggers=[],
    )

    _, markers, _ = _overlays(store)

    assert markers == []


def test_a_position_without_a_source_trigger_is_omitted() -> None:
    store = FakeStore(
        opportunities=[_opportunity()],
        positions=[_position(source_trigger_id=None)],
        triggers=[_trigger()],
    )

    _, markers, _ = _overlays(store)

    assert markers == []


def test_each_target_price_expands_to_one_marker() -> None:
    store = FakeStore(
        opportunities=[_opportunity()],
        positions=[_position(targets=("72000.00", "73000.00", "74000.00"))],
        triggers=[_trigger()],
    )

    _, markers, _ = _overlays(store)

    targets = _markers("target", markers)
    assert [marker["price"] for marker in targets] == ["72000.00", "73000.00", "74000.00"]
    assert len({marker["label"] for marker in targets}) == 3


def test_duplicate_kind_and_price_markers_are_deduped() -> None:
    store = FakeStore(
        opportunities=[_opportunity()],
        positions=[
            _position("POS-1", entry="70000.00", stop="69000.00", targets=("72000.00",)),
            _position("POS-2", entry="70000.00", stop="69000.00", targets=("72000.00",)),
        ],
        triggers=[_trigger("TRG-1"), _trigger("TRG-2")],
    )

    _, markers, _ = _overlays(store)

    keys = [(marker["kind"], marker["price"]) for marker in markers]
    assert len(keys) == len(set(keys))
    assert len(markers) == 3


def test_markers_are_capped_at_40_and_report_truncation() -> None:
    targets = tuple(f"{72000 + index}.00" for index in range(60))
    store = FakeStore(
        opportunities=[_opportunity()],
        positions=[_position(targets=targets)],
        triggers=[_trigger()],
    )

    _, markers, truncated = _overlays(store)

    assert len(markers) == 40
    assert truncated is True


def test_linked_read_over_the_bound_sets_truncated_and_slices_to_the_limit() -> None:
    opportunities = [_opportunity(f"OPP-{index:03d}") for index in range(201)]
    store = FakeStore(opportunities=opportunities)

    zones, _, truncated = _overlays(store)

    assert len(zones) == 200
    assert truncated is True


def test_linked_read_within_the_bound_is_not_truncated() -> None:
    store = FakeStore(opportunities=[_opportunity()])

    _, _, truncated = _overlays(store)

    assert truncated is False


def test_corrupt_position_payload_is_skipped() -> None:
    store = FakeStore(
        opportunities=[_opportunity()],
        positions=[_position(payload_override="{broken")],
        triggers=[_trigger()],
    )

    _, markers, _ = _overlays(store)

    assert markers == []


# -- resolution ---------------------------------------------------------------


def test_missing_payload_returns_empty_overlays() -> None:
    store = FakeStore()

    payload = resolve_chart(
        _report(),
        "15m",
        store,
        zones=[{"opportunity_id": "OPP-1"}],
        markers=[{"kind": "entry", "price": "1"}],
    )

    assert payload["status"] == "MISSING"
    assert payload["zones"] == []
    assert payload["markers"] == []
    assert payload["overlays_truncated"] is False


class _MissingChartStore:
    """A store whose overlay reads fail loudly if a MISSING chart asks for them."""

    def __init__(self, report: ReportRecord) -> None:
        self.report = report
        self.overlay_reads = 0

    def get_report(self, report_id: str) -> ReportRecord | None:
        return self.report if report_id == self.report.report_id else None

    def list_report_headers_for_symbol(self, symbol: str) -> tuple[ReportHeader, ...]:
        return ()

    def list_opportunities(self, **kwargs: Any) -> tuple[OpportunityRecord, ...]:
        self.overlay_reads += 1
        raise AssertionError("a MISSING chart must not read overlay opportunities")

    def list_positions(self, **kwargs: Any) -> tuple[PositionRecord, ...]:
        self.overlay_reads += 1
        raise AssertionError("a MISSING chart must not read overlay positions")


def _request_for(store: Any) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(store=store)))
    )


def test_missing_chart_never_derives_overlays() -> None:
    store = _MissingChartStore(_report())

    body = read_report_chart(_request_for(store), report_id=REPORT_ID, period="15m")

    data = body["data"]
    assert data["status"] == "MISSING"
    assert data["zones"] == []
    assert data["markers"] == []
    assert data["overlays_truncated"] is False
    assert store.overlay_reads == 0
