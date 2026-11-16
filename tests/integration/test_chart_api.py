"""Chart-panel API: published periods resolve verbatim; missing periods stay MISSING.

The chart read path never resamples, upsamples, or interpolates. A frozen
report already is published bar and metric data, so the service either returns
one report's frozen values exactly or returns ``MISSING`` with a reason.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.bootstrap import build_container
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)


@dataclass
class _Harness:
    settings: Settings
    app: Any
    client: TestClient
    auth: dict[str, str]

    def close(self) -> None:
        self.client.close()


def _harness(tmp_path: Path) -> _Harness:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
    )
    container = build_container(settings, initialize=True)
    result = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert result.run.status == "COMPLETE"
    app = create_app(settings)
    client = TestClient(app)
    return _Harness(
        settings=settings,
        app=app,
        client=client,
        auth={"Authorization": f"Bearer {app.state.api_token}"},
    )


def _scan(harness: _Harness, period: str, key: str) -> dict[str, Any]:
    response = harness.client.post(
        "/api/v1/scans",
        json={
            "symbol": "BTCUSDT",
            "period": period,
            "source": "backfill",
            "metrics_only": True,
        },
        headers={**harness.auth, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _report(harness: _Harness, report_id: str) -> dict[str, Any]:
    response = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _chart(harness: _Harness, report_id: str, *, period: str | None = None) -> Any:
    params = {} if period is None else {"period": period}
    return harness.client.get(
        f"/api/v1/reports/{report_id}/chart", params=params, headers=harness.auth
    )


def test_native_period_returns_the_frozen_snapshot_and_metrics_verbatim(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _scan(harness, "4h", "chart-native")["report_id"]
        report = _report(harness, report_id)
        for period in (None, "4h"):
            response = _chart(harness, report_id, period=period)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["ok"] is True
            assert body["error"] is None
            assert body["meta"]["provenance"] == "CALC"
            data = body["data"]
            assert data["status"] == "AVAILABLE"
            assert data["report_id"] == report_id
            assert data["symbol"] == "BTCUSDT"
            assert data["native_period"] == "4h"
            assert data["requested_period"] == "4h"
            assert data["source_report_id"] == report_id
            assert data["generated_at"] == report["generated_at"]
            assert data["bars"] == report["snapshot"]["bars"]
            assert data["metrics"] == report["metrics"]
            assert data["reason"] is None
            assert data["data_quality"] == report["data_quality"]
            assert data["quality_reasons"] == report["metrics"]["quality_reasons"]
            assert body["meta"]["data_quality"] == report["data_quality"]
    finally:
        harness.close()


def test_unpublished_period_is_missing_with_an_explicit_reason(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _scan(harness, "4h", "chart-missing")["report_id"]
        response = _chart(harness, report_id, period="1h")
        assert response.status_code == 200, response.text
        body = response.json()
        data = body["data"]
        assert data["status"] == "MISSING"
        assert data["requested_period"] == "1h"
        assert data["native_period"] == "4h"
        assert data["source_report_id"] is None
        assert data["generated_at"] is None
        assert data["bars"] == []
        assert data["metrics"] is None
        assert data["gaps"] == []
        assert data["data_quality"] == "MISSING"
        assert data["reason"]
        assert "1h" in data["reason"]
        assert "upsamples" in data["reason"] or "interpolates" in data["reason"]
        # The full available list is still returned while the series is missing.
        assert data["available_periods"] == ["4h"]
        assert body["meta"]["provenance"] == "CALC"
        assert body["meta"]["data_quality"] == "MISSING"
    finally:
        harness.close()


def test_a_second_published_period_resolves_to_its_own_report(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        native_id = _scan(harness, "4h", "chart-period-4h")["report_id"]
        hourly_id = _scan(harness, "1h", "chart-period-1h")["report_id"]
        assert hourly_id != native_id
        hourly = _report(harness, hourly_id)

        response = _chart(harness, native_id, period="1h")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["status"] == "AVAILABLE"
        assert data["requested_period"] == "1h"
        assert data["native_period"] == "4h"
        assert data["source_report_id"] == hourly_id
        assert data["generated_at"] == hourly["generated_at"]
        assert data["bars"] == hourly["snapshot"]["bars"]
        assert data["metrics"] == hourly["metrics"]
        assert data["bars"]
        # Native first, then 15m, 1h, 4h with the duplicate native removed.
        assert data["available_periods"] == ["4h", "1h"]

        # The native request still uses the requested report's own frozen data.
        native = _chart(harness, native_id).json()["data"]
        assert native["source_report_id"] == native_id
    finally:
        harness.close()


def test_unknown_report_is_not_found(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        response = _chart(harness, "RPT-DOES-NOT-EXIST")
        assert response.status_code == 404
        body = response.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "NOT_FOUND"
    finally:
        harness.close()


def test_invalid_period_is_validation_failed(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _scan(harness, "4h", "chart-invalid-period")["report_id"]
        response = _chart(harness, report_id, period="5m")
        assert response.status_code == 422
        body = response.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "VALIDATION_FAILED"
        message = body["error"]["message"]
        assert "15m" in message
        assert "1h" in message
        assert "4h" in message
    finally:
        harness.close()


def _store(harness: _Harness) -> Any:
    return harness.app.state.container.store


def _copy_report(
    harness: _Harness,
    report_id: str,
    *,
    new_report_id: str,
    **changes: Any,
) -> Any:
    """Persist a frozen-report copy with copied snapshot/metrics (no new data)."""
    template = _store(harness).get_report(report_id)
    assert template is not None
    record = replace(template, report_id=new_report_id, **changes)
    _store(harness).save_report(record)
    return record


@pytest.mark.parametrize(
    ("field", "value", "key"),
    [
        ("snapshot_json", "{not json", "corrupt-snapshot"),
        ("snapshot_json", "[]", "non-object-snapshot"),
        ("metrics_json", "{not json", "corrupt-metrics"),
        ("metrics_json", "null", "non-object-metrics"),
    ],
)
def test_corrupt_frozen_json_returns_missing_instead_of_a_500(
    tmp_path: Path, field: str, value: str, key: str
) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _scan(harness, "4h", f"chart-{key}")["report_id"]
        corrupt = _copy_report(
            harness,
            report_id,
            new_report_id=f"RPT-CORRUPT-{key.upper()}",
            **{field: value},
        )

        response = _chart(harness, corrupt.report_id)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        data = body["data"]
        assert data["status"] == "MISSING"
        assert data["bars"] == []
        assert data["metrics"] is None
        assert data["reason"]
        assert "storage corruption" in data["reason"]
        assert body["meta"]["data_quality"] == "MISSING"
    finally:
        harness.close()


def _set(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def apply(target: dict[str, Any]) -> None:
        target[key] = value

    return apply


def _delete(key: str) -> Callable[[dict[str, Any]], None]:
    def apply(target: dict[str, Any]) -> None:
        target.pop(key, None)

    return apply


def _snapshot_mutator(
    mutate: Callable[[dict[str, Any]], None],
) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    def apply(snapshot: dict[str, Any], _metrics: dict[str, Any]) -> None:
        mutate(snapshot)

    return apply


def _metrics_mutator(
    mutate: Callable[[dict[str, Any]], None],
) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    def apply(_snapshot: dict[str, Any], metrics: dict[str, Any]) -> None:
        mutate(metrics)

    return apply


def _footprint_mutator(
    mutate: Callable[[dict[str, Any]], None],
) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    def apply(_snapshot: dict[str, Any], metrics: dict[str, Any]) -> None:
        mutate(metrics["footprint"])

    return apply


def _volume_profile_mutator(
    mutate: Callable[[dict[str, Any]], None],
) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    def apply(_snapshot: dict[str, Any], metrics: dict[str, Any]) -> None:
        mutate(metrics["volume_profile"])

    return apply


def _first_footprint_bar_mutator(
    mutate: Callable[[dict[str, Any]], None],
) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    """Corrupt the first footprint bar, which the frozen fixture always has."""

    def apply(_snapshot: dict[str, Any], metrics: dict[str, Any]) -> None:
        mutate(metrics["footprint"]["bars"][0])

    return apply


#: ``(case name, corrupt part named in the reason, mutation)`` pairs. Every
#: mutation keeps the root documents parseable so the structural validator (not
#: the JSON parser) is what flags the report.
CORRUPT_STRUCTURES: list[tuple[str, str, Callable[[dict[str, Any], dict[str, Any]], None]]] = [
    ("snapshot-bars-missing", "snapshot.bars", _snapshot_mutator(_delete("bars"))),
    (
        "snapshot-bars-not-list",
        "snapshot.bars",
        _snapshot_mutator(_set("bars", {"one": 1})),
    ),
    (
        "snapshot-bars-element-not-object",
        "snapshot.bars",
        _snapshot_mutator(_set("bars", ["not-an-object"])),
    ),
    (
        "metrics-footprint-missing",
        "metrics.footprint",
        _metrics_mutator(_delete("footprint")),
    ),
    (
        "metrics-footprint-not-object",
        "metrics.footprint",
        _metrics_mutator(_set("footprint", [])),
    ),
    (
        "footprint-bars-missing",
        "metrics.footprint.bars",
        _footprint_mutator(_delete("bars")),
    ),
    (
        "footprint-bars-not-list",
        "metrics.footprint.bars",
        _footprint_mutator(_set("bars", {"bars": []})),
    ),
    (
        "footprint-bars-element-not-object",
        "metrics.footprint.bars",
        _footprint_mutator(_set("bars", [1])),
    ),
    (
        "metrics-volume-profile-missing",
        "metrics.volume_profile",
        _metrics_mutator(_delete("volume_profile")),
    ),
    (
        "volume-profile-not-object",
        "metrics.volume_profile",
        _metrics_mutator(_set("volume_profile", "nope")),
    ),
    (
        "volume-profile-bins-missing",
        "metrics.volume_profile.bins",
        _volume_profile_mutator(_delete("bins")),
    ),
    (
        "volume-profile-bins-not-list",
        "metrics.volume_profile.bins",
        _volume_profile_mutator(_set("bins", {"bins": []})),
    ),
    (
        "volume-profile-bins-element-not-object",
        "metrics.volume_profile.bins",
        _volume_profile_mutator(_set("bins", [1])),
    ),
    (
        "volume-profile-poc-missing",
        "metrics.volume_profile.poc",
        _volume_profile_mutator(_delete("poc")),
    ),
    (
        "footprint-bar-cells-missing",
        "metrics.footprint.bars[0].cells",
        _first_footprint_bar_mutator(_delete("cells")),
    ),
    (
        "footprint-bar-cells-not-list",
        "metrics.footprint.bars[0].cells",
        _first_footprint_bar_mutator(_set("cells", {"cells": []})),
    ),
    (
        "footprint-bar-cells-element-not-object",
        "metrics.footprint.bars[0].cells",
        _first_footprint_bar_mutator(_set("cells", ["not-an-object"])),
    ),
    (
        "gaps-missing",
        "metrics.gaps",
        _metrics_mutator(_delete("gaps")),
    ),
    (
        "gaps-null",
        "metrics.gaps",
        _metrics_mutator(_set("gaps", None)),
    ),
    (
        "gaps-not-list",
        "metrics.gaps",
        _metrics_mutator(_set("gaps", {"gaps": []})),
    ),
    (
        "gaps-element-not-object",
        "metrics.gaps",
        _metrics_mutator(_set("gaps", ["gap"])),
    ),
    (
        "quality-reasons-missing",
        "metrics.quality_reasons",
        _metrics_mutator(_delete("quality_reasons")),
    ),
    (
        "quality-reasons-null",
        "metrics.quality_reasons",
        _metrics_mutator(_set("quality_reasons", None)),
    ),
    (
        "quality-reasons-not-list",
        "metrics.quality_reasons",
        _metrics_mutator(_set("quality_reasons", "reasons")),
    ),
    (
        "quality-reasons-element-not-string",
        "metrics.quality_reasons",
        _metrics_mutator(_set("quality_reasons", [1])),
    ),
]


@pytest.mark.parametrize(("name", "part", "mutate"), CORRUPT_STRUCTURES)
def test_corrupt_frozen_structure_returns_missing_naming_the_part(
    tmp_path: Path,
    name: str,
    part: str,
    mutate: Callable[[dict[str, Any], dict[str, Any]], None],
) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _scan(harness, "4h", f"chart-struct-{name}")["report_id"]
        template = _store(harness).get_report(report_id)
        assert template is not None
        snapshot = json.loads(template.snapshot_json)
        metrics = json.loads(template.metrics_json)
        mutate(snapshot, metrics)
        corrupt = _copy_report(
            harness,
            report_id,
            new_report_id=f"RPT-STRUCT-{name.upper()}",
            snapshot_json=json.dumps(snapshot),
            metrics_json=json.dumps(metrics),
        )

        response = _chart(harness, corrupt.report_id)

        assert response.status_code == 200, response.text
        body = response.json()
        data = body["data"]
        assert data["status"] == "MISSING"
        assert data["bars"] == []
        assert data["metrics"] is None
        assert data["gaps"] == []
        assert data["quality_reasons"] == []
        assert data["source_report_id"] is None
        assert data["generated_at"] is None
        assert data["data_quality"] == "MISSING"
        assert data["reason"]
        assert "storage corruption" in data["reason"]
        assert corrupt.report_id in data["reason"]
        assert part in data["reason"]
        assert body["meta"]["data_quality"] == "MISSING"
    finally:
        harness.close()


def test_switched_period_uses_the_newest_published_report(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        native_id = _scan(harness, "4h", "chart-newest-native")["report_id"]
        hourly_id = _scan(harness, "1h", "chart-newest-1h")["report_id"]
        older = _copy_report(
            harness,
            hourly_id,
            new_report_id="RPT-NEWEST-OLDER",
            generated_at="2098-01-01T00:00:00.000Z",
        )
        newer = _copy_report(
            harness,
            hourly_id,
            new_report_id="RPT-NEWEST-NEWER",
            generated_at="2099-01-01T00:00:00.000Z",
        )

        response = _chart(harness, native_id, period="1h")

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["status"] == "AVAILABLE"
        assert data["source_report_id"] == newer.report_id
        assert data["source_report_id"] != older.report_id
        assert data["bars"] == json.loads(newer.snapshot_json)["bars"]
        assert data["metrics"] == json.loads(newer.metrics_json)
    finally:
        harness.close()


def test_same_generated_at_breaks_the_tie_on_report_id(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        native_id = _scan(harness, "4h", "chart-tie-native")["report_id"]
        hourly_id = _scan(harness, "1h", "chart-tie-1h")["report_id"]
        same_instant = "2099-02-01T00:00:00.000Z"
        _copy_report(
            harness,
            hourly_id,
            new_report_id="RPT-TIE-0001",
            generated_at=same_instant,
        )
        higher = _copy_report(
            harness,
            hourly_id,
            new_report_id="RPT-TIE-0002",
            generated_at=same_instant,
        )

        response = _chart(harness, native_id, period="1h")

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["source_report_id"] == higher.report_id
        assert data["generated_at"] == same_instant
    finally:
        harness.close()


def test_chart_request_reads_bounded_headers_and_one_source_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    try:
        native_id = _scan(harness, "4h", "chart-read-once-native")["report_id"]
        hourly_id = _scan(harness, "1h", "chart-read-once-1h")["report_id"]
        store = _store(harness)

        projection_calls: list[str] = []
        report_reads: list[str] = []
        original_headers = store.list_report_headers_for_symbol
        original_get = store.get_report

        def counted_headers(symbol: str) -> Any:
            projection_calls.append(symbol)
            return original_headers(symbol)

        def forbidden_full_list(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("the chart path must not read every full report")

        def counted_get(report_id: str) -> Any:
            report_reads.append(report_id)
            return original_get(report_id)

        monkeypatch.setattr(store, "list_report_headers_for_symbol", counted_headers)
        monkeypatch.setattr(store, "list_reports_for_symbol", forbidden_full_list)
        monkeypatch.setattr(store, "get_report", counted_get)

        switched = _chart(harness, native_id, period="1h")
        assert switched.status_code == 200, switched.text
        assert switched.json()["data"]["source_report_id"] == hourly_id
        assert projection_calls == ["BTCUSDT"]
        # One bounded header projection, then only the requested report and the
        # single published 1h source are read as full frozen documents.
        assert report_reads == [native_id, hourly_id]

        projection_calls.clear()
        report_reads.clear()

        native = _chart(harness, native_id)
        assert native.status_code == 200, native.text
        assert native.json()["data"]["source_report_id"] == native_id
        assert projection_calls == ["BTCUSDT"]
        # The native period adds no full document read beyond the requested one.
        assert report_reads == [native_id]
    finally:
        harness.close()


def test_switched_period_whose_source_vanished_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    try:
        native_id = _scan(harness, "4h", "chart-vanished-native")["report_id"]
        hourly_id = _scan(harness, "1h", "chart-vanished-1h")["report_id"]
        store = _store(harness)
        original_get = store.get_report

        def vanished_hourly(report_id: str) -> Any:
            # The native report still loads; only the published 1h source is gone.
            if report_id == hourly_id:
                return None
            return original_get(report_id)

        monkeypatch.setattr(store, "get_report", vanished_hourly)

        response = _chart(harness, native_id, period="1h")

        assert response.status_code == 200, response.text
        body = response.json()
        data = body["data"]
        assert data["status"] == "MISSING"
        assert data["requested_period"] == "1h"
        assert data["native_period"] == "4h"
        assert data["source_report_id"] is None
        assert data["generated_at"] is None
        assert data["bars"] == []
        assert data["metrics"] is None
        assert data["gaps"] == []
        assert data["quality_reasons"] == []
        assert data["data_quality"] == "MISSING"
        assert body["meta"]["data_quality"] == "MISSING"
        assert data["reason"]
        assert hourly_id in data["reason"]
        assert "disappeared" in data["reason"]
    finally:
        harness.close()


def test_report_list_symbol_filter_uses_bounded_sql_with_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    try:
        _scan(harness, "4h", "chart-page-1")
        second = _scan(harness, "1h", "chart-page-2")
        _scan(harness, "4h", "chart-page-3")
        store = _store(harness)
        expected = [record.report_id for record in store.list_reports(limit=10)]
        assert len(expected) == 3

        def forbidden_full_list(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("the symbol filter must not read every report")

        monkeypatch.setattr(store, "list_reports_for_symbol", forbidden_full_list)

        first_page = harness.client.get(
            "/api/v1/reports",
            params={"symbol": "BTCUSDT", "limit": 2},
            headers=harness.auth,
        )
        assert first_page.status_code == 200, first_page.text
        body = first_page.json()
        assert body["data"]["total"] == 3
        assert [item["report_id"] for item in body["data"]["items"]] == expected[:2]
        assert body["meta"]["pagination"] == {
            "limit": 2,
            "cursor": "0",
            "has_next": True,
            "next_cursor": "2",
            "total": 3,
        }

        second_page = harness.client.get(
            "/api/v1/reports",
            params={"symbol": "BTCUSDT", "limit": 2, "offset": 2},
            headers=harness.auth,
        ).json()
        assert [item["report_id"] for item in second_page["data"]["items"]] == expected[2:]
        assert second_page["data"]["total"] == 3
        assert second_page["meta"]["pagination"]["has_next"] is False
        assert second_page["meta"]["pagination"]["next_cursor"] is None

        scoped = harness.client.get(
            "/api/v1/reports",
            params={"symbol": "BTCUSDT", "scan_id": second["scan"]["scan_id"]},
            headers=harness.auth,
        ).json()
        assert scoped["data"]["total"] == 1
        assert scoped["data"]["items"][0]["report_id"] == second["report_id"]
    finally:
        harness.close()


def test_report_list_symbol_filter_returns_only_that_symbol_newest_first(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    try:
        first = _scan(harness, "4h", "chart-list-1")
        second = _scan(harness, "4h", "chart-list-2")
        store = harness.app.state.container.store
        pub = store.list_reports_for_symbol("BTCUSDT")
        expected = sorted(
            pub, key=lambda record: (record.generated_at, record.report_id), reverse=True
        )
        newest = expected[0].report_id
        assert {first["report_id"], second["report_id"]} == {record.report_id for record in pub}

        response = harness.client.get(
            "/api/v1/reports", params={"symbol": "BTCUSDT", "limit": 1}, headers=harness.auth
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["data"]["total"] == 2
        assert [item["report_id"] for item in body["data"]["items"]] == [newest]
        assert body["meta"]["pagination"]["has_next"] is True
        assert body["meta"]["pagination"]["next_cursor"] == "1"

        scoped = harness.client.get(
            "/api/v1/reports",
            params={"symbol": "BTCUSDT", "scan_id": first["scan"]["scan_id"]},
            headers=harness.auth,
        ).json()
        assert scoped["data"]["total"] == 1
        assert scoped["data"]["items"][0]["report_id"] == first["report_id"]

        empty = harness.client.get(
            "/api/v1/reports", params={"symbol": "ETHUSDT"}, headers=harness.auth
        ).json()
        assert empty["data"]["total"] == 0
        assert empty["data"]["items"] == []
    finally:
        harness.close()


def test_report_list_symbol_filter_validates_the_symbol(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        response = harness.client.get(
            "/api/v1/reports", params={"symbol": "btcusdt"}, headers=harness.auth
        )
    finally:
        harness.close()

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
