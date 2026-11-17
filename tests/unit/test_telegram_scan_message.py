"""Scan-summary push event payloads and their deterministic renderer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from oft.application.notifications import NotificationService
from oft.application.telegram_events import (
    MAX_SCAN_REPORT_IDS,
    SCAN_SUMMARY_EVENT_TYPE,
    ScanSummary,
    build_scan_summary_event,
    scan_summary_dedupe_key,
    stage_for_error_code,
)
from oft.application.telegram_messages import render_scan_summary
from oft.clock import FixedClock
from oft.domain.telegram import MAX_OUTBOUND_CHARS, ReceiptState
from oft.ports.store import (
    JobRunRecord,
    ReportRecord,
    ScanSymbolRecord,
    TelegramOutboxRecord,
)


def _summary(**overrides: object) -> ScanSummary:
    values: dict[str, object] = {
        "scan_id": "SCAN-1",
        "status": "COMPLETE",
        "trigger_source": "SCHEDULED",
        "period": "4h",
        "symbols_total": 2,
        "symbols_completed": 2,
        "symbols_degraded": 0,
        "symbols_failed": 0,
        "new_opportunities": 1,
        "cost_usd": "0.25",
        "cost_unpriced": False,
        "report_ids": ("RPT-B", "RPT-A"),
        "reports_total": 2,
        "failed_stage": None,
        "reason_code": None,
        "generated_at": "2026-11-17T09:30:00.000Z",
    }
    values.update(overrides)
    return ScanSummary(**values)  # type: ignore[arg-type]


def _payload(summary: ScanSummary) -> dict[str, object]:
    return dict(build_scan_summary_event(summary).payload)


def test_complete_scan_renders_the_documented_shape() -> None:
    text = render_scan_summary(_payload(_summary()))

    assert text == "\n".join(
        [
            "SCAN COMPLETE",
            "status: COMPLETE",
            "scan_id: SCAN-1",
            "trigger: SCHEDULED",
            "period: 4h",
            "symbols: 2/2",
            "degraded: 0",
            "failed: 0",
            "new_opportunities: 1",
            "cost: 0.25 USDT",
            "reports: 2",
            "generated_at: 2026-11-17T09:30:00.000Z",
            "report_ids: RPT-A, RPT-B",
        ]
    )


def test_partial_scan_carries_degraded_and_failed_counts_and_the_unpriced_marker() -> None:
    text = render_scan_summary(
        _payload(
            _summary(
                status="PARTIAL",
                symbols_total=3,
                symbols_completed=1,
                symbols_degraded=1,
                symbols_failed=1,
                new_opportunities=0,
                cost_usd="0.5000",
                cost_unpriced=True,
                report_ids=("RPT-A",),
                reports_total=1,
            )
        )
    )

    lines = text.splitlines()
    assert lines[0] == "SCAN PARTIAL"
    assert "status: PARTIAL" in lines
    assert "symbols: 1/3" in lines
    assert "degraded: 1" in lines
    assert "failed: 1" in lines
    assert "new_opportunities: 0" in lines
    assert "cost: 0.5000 USDT (unpriced)" in lines
    assert "reports: 1" in lines
    assert "report_ids: RPT-A" in lines


def test_failed_scan_names_the_stage_and_reason_without_a_traceback_or_raw_message() -> None:
    text = render_scan_summary(
        _payload(
            _summary(
                status="FAILED",
                symbols_total=1,
                symbols_completed=0,
                symbols_degraded=0,
                symbols_failed=1,
                report_ids=(),
                reports_total=0,
                failed_stage="DATA",
                reason_code="DATA_UNAVAILABLE",
            )
        )
    )

    lines = text.splitlines()
    assert lines[0] == "SCAN FAILED"
    assert "stage: DATA" in lines
    assert "reason_code: DATA_UNAVAILABLE" in lines
    assert "report_ids: none" in lines
    assert "Traceback" not in text
    assert "boom" not in text


def test_hostile_values_are_sanitized_to_one_plain_line_each() -> None:
    hostile = "SCAN-\nstage: DATA\x00​‮RTL"
    text = render_scan_summary(
        _payload(
            _summary(
                status="FAILED",
                scan_id=hostile,
                reason_code="BAD\r\nREASON",
                failed_stage="DATA\nMETRICS",
            )
        )
    )

    assert "\x00" not in text
    assert "​" not in text
    assert "‮" not in text
    # No injected line can masquerade as a second stage/reason line.
    assert text.count("\nstage: ") == 1
    assert text.count("\nreason_code: ") == 1
    scan_line = next(line for line in text.splitlines() if line.startswith("scan_id: "))
    assert scan_line.startswith("scan_id: SCAN- stage: DATA")
    assert "\n" not in scan_line


def test_report_ids_are_sorted_deduplicated_and_bounded() -> None:
    many = tuple(f"RPT-{index:03d}" + "X" * 300 for index in range(50))
    summary = _summary(report_ids=many, reports_total=len(many))

    payload = _payload(summary)
    text = render_scan_summary(payload)

    assert payload["report_ids"] == sorted(many)[:MAX_SCAN_REPORT_IDS]
    assert payload["reports_total"] == 50
    assert len(payload["report_ids"]) == MAX_SCAN_REPORT_IDS
    report_line = next(line for line in text.splitlines() if line.startswith("report_ids: "))
    assert len(report_line.split(", ")) == MAX_SCAN_REPORT_IDS
    # The count is the uncapped total, not the bounded list length.
    assert "reports: 50" in text.splitlines()
    assert len(text) <= MAX_OUTBOUND_CHARS


def test_hostile_report_ids_cannot_break_the_line_structure() -> None:
    payload = _payload(_summary(report_ids=("RPT-A\nstage: DATA", "​RPT-B")))

    text = render_scan_summary(payload)

    report_line = next(line for line in text.splitlines() if line.startswith("report_ids: "))
    assert "​" not in report_line
    assert report_line.count("stage:") == 1  # only the value text, still one line
    assert text.count("\nstage: ") == 0


def test_builder_emits_only_the_allowlisted_payload_fields() -> None:
    event = build_scan_summary_event(_summary())

    assert event.event_type == SCAN_SUMMARY_EVENT_TYPE
    assert event.subject == {"kind": "SCAN", "id": "SCAN-1"}
    assert event.dedupe_key == "scan:SCAN-1:summary"
    assert event.text == render_scan_summary(event.payload)
    assert set(event.payload) == {
        "status",
        "scan_id",
        "trigger_source",
        "period",
        "symbols_total",
        "symbols_completed",
        "symbols_degraded",
        "symbols_failed",
        "new_opportunities",
        "cost_usd",
        "cost_unpriced",
        "report_ids",
        "reports_total",
        "failed_stage",
        "reason_code",
        "generated_at",
    }
    for forbidden in ("error_message", "narrative", "prompt", "llm", "token"):
        assert forbidden not in event.payload


def test_dedupe_key_contains_the_scan_id_and_maps_error_codes_to_stages() -> None:
    assert scan_summary_dedupe_key("SCAN-XYZ") == "scan:SCAN-XYZ:summary"
    assert stage_for_error_code("DATA_UNAVAILABLE") == "DATA"
    assert stage_for_error_code("METRICS_INCOMPLETE") == "METRICS"
    assert stage_for_error_code("SCAN_FAILED") == "REPORT"
    assert stage_for_error_code(None) == "REPORT"


def test_builder_bounds_a_negative_opportunity_count() -> None:
    payload = _payload(_summary(new_opportunities=-4))

    assert payload["new_opportunities"] == 0


class _FakeChannel:
    def status_state(self) -> object:
        return type("State", (), {"state": "READY"})()

    def allowlist(self) -> object:
        return type("Allowlist", (), {"chat_ids": (1001,)})()


class _FakeSettings:
    def read_scan_report_push(self) -> bool:
        return True

    def read_failed_scan_alert(self) -> bool:
        return False


class _FakeStore:
    def __init__(self) -> None:
        self.appended: list[TelegramOutboxRecord] = []
        self.rows = (
            ScanSymbolRecord(
                scan_id="SCAN-1",
                symbol="BTCUSDT",
                stage="DONE",
                started_at="t",
                finished_at="t",
                error_code=None,
                report_id="RPT-1",
                data_quality="COMPLETE",
                llm_status="VALID",
                cost_usd="0.10",
                cost_unpriced=False,
            ),
            ScanSymbolRecord(
                scan_id="SCAN-1",
                symbol="ETHUSDT",
                stage="DEGRADED",
                started_at="t",
                finished_at="t",
                error_code=None,
                report_id="RPT-2",
                data_quality="PARTIAL",
                llm_status="REJECTED",
                cost_usd="0.20",
                cost_unpriced=True,
            ),
        )
        self.reports = {
            "RPT-1": ReportRecord(
                report_id="RPT-1",
                scan_id="SCAN-CHILD-1",
                symbol="BTCUSDT",
                timeframe="4h",
                generated_at="t",
                metrics_manifest_json="[]",
                llm_decision_id=None,
                narrative_ref=None,
                data_quality="COMPLETE",
                snapshot_json="{}",
                metrics_json="{}",
                llm_json='{"opportunity_ids": ["OPP-1", "OPP-2"]}',
            ),
            "RPT-2": ReportRecord(
                report_id="RPT-2",
                scan_id="SCAN-CHILD-2",
                symbol="ETHUSDT",
                timeframe="4h",
                generated_at="t",
                metrics_manifest_json="[]",
                llm_decision_id=None,
                narrative_ref=None,
                data_quality="PARTIAL",
                snapshot_json="{}",
                metrics_json="{}",
                llm_json='{"opportunity_ids": ["OPP-2", "OPP-3"]}',
            ),
        }
        self.run = JobRunRecord(
            run_id="SCAN-1",
            job_type="scan",
            window_start="2026-11-17T08:00:00.000Z",
            status="DONE",
            trigger_source="MANUAL",
            started_at="2026-11-17T08:00:00.000Z",
            finished_at="2026-11-17T08:05:00.000Z",
            error_code=None,
            message=None,
            lease_owner="test",
            lease_expires_at="t",
            heartbeat_at="t",
            period="4h",
        )

    def list_scan_symbols(self, scan_id: str) -> tuple[object, ...]:
        assert scan_id == "SCAN-1"
        return self.rows

    def get_report(self, report_id: str) -> object:
        return self.reports.get(report_id)

    def get_job_run(self, scan_id: str) -> object:
        return self.run

    def get_scan(self, scan_id: str) -> None:
        return None

    def append_telegram_outbox(self, record: object) -> bool:
        self.appended.append(record)  # type: ignore[arg-type]
        return True


def _row(symbol: str, **overrides: object) -> ScanSymbolRecord:
    values: dict[str, object] = {
        "scan_id": "SCAN-1",
        "symbol": symbol,
        "stage": "DONE",
        "started_at": "t",
        "finished_at": "t",
        "error_code": None,
        "report_id": None,
        "data_quality": "COMPLETE",
        "llm_status": None,
        "cost_usd": "0",
        "cost_unpriced": False,
    }
    values.update(overrides)
    return ScanSymbolRecord(**values)  # type: ignore[arg-type]


def _service(
    store: object,
    *,
    channel: object | None = None,
    settings: object | None = None,
) -> NotificationService:
    return NotificationService(
        store=store,  # type: ignore[arg-type]
        clock=FixedClock(datetime(2026, 11, 17, 9, 30, tzinfo=UTC)),
        channel=channel or _FakeChannel(),  # type: ignore[arg-type]
        settings_service=settings or _FakeSettings(),  # type: ignore[arg-type]
    )


class _QuietStore:
    """A tiny store exposing only the reads a push may probe."""

    def __init__(
        self,
        *,
        rows: tuple[ScanSymbolRecord, ...] = (),
        run: object | None = None,
        receipt: TelegramOutboxRecord | None = None,
    ) -> None:
        self.rows = rows
        self.run = run
        self.receipt = receipt
        self.appended: list[TelegramOutboxRecord] = []

    def list_scan_symbols(self, scan_id: str) -> tuple[ScanSymbolRecord, ...]:
        return self.rows

    def get_job_run(self, scan_id: str) -> object | None:
        return self.run

    def append_telegram_outbox(self, record: TelegramOutboxRecord) -> bool:
        self.appended.append(record)
        return True

    def get_telegram_outbox(self, event_key: str) -> TelegramOutboxRecord | None:
        return self.receipt


def test_enqueue_is_a_noop_for_an_unknown_event_or_a_non_ready_channel() -> None:
    store = _FakeStore()
    assert _service(store).enqueue("other", {}, "k", 0, {}) is None

    not_ready = SimpleNamespace(state="NOT_CONFIGURED")
    channel = SimpleNamespace(
        status_state=lambda: not_ready,
        allowlist=lambda: SimpleNamespace(chat_ids=(1001,)),
    )
    assert (
        _service(store, channel=channel).enqueue(
            SCAN_SUMMARY_EVENT_TYPE, {"kind": "SCAN", "id": "S"}, "k", 0, {}
        )
        is None
    )
    assert store.appended == []


def test_enqueue_is_a_noop_without_a_valid_chat_or_with_the_toggle_off() -> None:
    store = _FakeStore()
    no_chat = SimpleNamespace(
        status_state=lambda: SimpleNamespace(state="READY"),
        allowlist=lambda: SimpleNamespace(chat_ids=()),
    )
    assert _service(store, channel=no_chat).notify_scan("SCAN-1") is None
    non_int_chat = SimpleNamespace(
        status_state=lambda: SimpleNamespace(state="READY"),
        allowlist=lambda: SimpleNamespace(chat_ids=("chat",)),
    )
    assert _service(store, channel=non_int_chat).notify_scan("SCAN-1") is None

    class _Off:
        def read_scan_report_push(self) -> bool:
            return False

        def read_failed_scan_alert(self) -> bool:
            return False

    assert _service(store, settings=_Off()).notify_scan("SCAN-1") is None
    assert store.appended == []


def test_notify_scan_swallows_a_store_failure_or_an_unknown_status() -> None:
    class _Raising(_QuietStore):
        def list_scan_symbols(self, scan_id: str) -> tuple[ScanSymbolRecord, ...]:
            raise RuntimeError("store down")

    assert _service(_Raising()).notify_scan("SCAN-1") is None

    class _Missed(_QuietStore):
        def get_job_run(self, scan_id: str) -> object:
            return SimpleNamespace(status="MISSED")

    assert _service(_Missed()).notify_scan("SCAN-1") is None


def test_a_corrupt_cost_is_reported_as_unpriced_and_context_falls_back() -> None:
    store = _QuietStore(
        rows=(
            _row(
                "BTCUSDT",
                report_id="RPT-1",
                llm_status="VALID",
                cost_usd="not-a-number",
            ),
        )
    )

    record = _service(store).notify_scan("SCAN-1")

    assert record is not None
    text = json.loads(record.payload_json)["text"]
    assert "cost: 0 USDT (unpriced)" in text
    assert "trigger: MANUAL" in text
    assert "period: 4h" in text


def test_opportunity_ids_tolerate_missing_or_malformed_reports() -> None:
    from oft.ports.store import ReportRecord

    store = _FakeStore()
    store.reports = {
        "RPT-BAD": ReportRecord(
            report_id="RPT-BAD",
            scan_id="S",
            symbol="BTCUSDT",
            timeframe="4h",
            generated_at="t",
            metrics_manifest_json="[]",
            llm_decision_id=None,
            narrative_ref=None,
            data_quality="COMPLETE",
            snapshot_json="{}",
            metrics_json="{}",
            llm_json="not-json",
        ),
        "RPT-SHAPE": ReportRecord(
            report_id="RPT-SHAPE",
            scan_id="S",
            symbol="ETHUSDT",
            timeframe="4h",
            generated_at="t",
            metrics_manifest_json="[]",
            llm_decision_id=None,
            narrative_ref=None,
            data_quality="COMPLETE",
            snapshot_json="{}",
            metrics_json="{}",
            llm_json='{"opportunity_ids": "OPP-1"}',
        ),
    }
    store.rows = (
        _row("BTCUSDT", report_id="RPT-BAD"),
        _row("ETHUSDT", report_id="RPT-SHAPE"),
        _row("XAUUSDT", report_id="RPT-MISSING"),
    )

    record = _service(store).notify_scan("SCAN-1")

    assert record is not None
    assert "new_opportunities: 0" in json.loads(record.payload_json)["text"]


def test_delivery_contract_stays_the_stub_when_the_receipt_store_is_unavailable() -> None:
    from oft.application.notifications import delivery_contract, not_configured_delivery

    assert delivery_contract(_QuietStore(), "SCAN-1") == not_configured_delivery()

    class _Broken:
        def get_telegram_outbox(self, event_key: str) -> object:
            raise RuntimeError("outbox down")

    assert delivery_contract(_Broken(), "SCAN-1") == not_configured_delivery()


def test_notify_scan_recomputes_a_degraded_summary_from_persisted_rows() -> None:
    store = _FakeStore()
    service = NotificationService(
        store=store,  # type: ignore[arg-type]
        clock=FixedClock(datetime(2026, 11, 17, 9, 30, tzinfo=UTC)),
        channel=_FakeChannel(),
        settings_service=_FakeSettings(),
    )

    record = service.notify_scan("SCAN-1")

    assert record is not None
    payload = json.loads(record.payload_json)
    assert record.event_key == "scan:SCAN-1:summary"
    assert record.chat_id == 1001
    assert payload["text"].splitlines()[0] == "SCAN PARTIAL"
    assert "symbols: 1/2" in payload["text"]
    assert "degraded: 1" in payload["text"]
    assert "failed: 0" in payload["text"]
    assert "new_opportunities: 3" in payload["text"]
    assert "cost: 0.30 USDT (unpriced)" in payload["text"]
    assert "trigger: MANUAL" in payload["text"]
    assert "period: 4h" in payload["text"]
    # L-9: the run's own finish time is the message timestamp, not the clock.
    assert "generated_at: 2026-11-17T08:05:00.000Z" in payload["text"]


def test_a_done_row_with_a_failed_run_still_renders_scan_failed() -> None:
    # Regression (M-1): the reclaimed-lock path persists FAILED after a prior
    # owner's DONE row; the push must not claim the scan completed.
    store = _QuietStore(
        rows=(_row("BTCUSDT", report_id="RPT-1", llm_status="VALID"),),
        run=SimpleNamespace(
            status="FAILED",
            trigger_source="SCHEDULED",
            period="4h",
            finished_at="2026-11-17T08:05:00.000Z",
            error_code="SCAN_ALREADY_RUNNING",
        ),
    )

    record = _service(store).notify_scan("SCAN-1")

    assert record is not None
    text = json.loads(record.payload_json)["text"]
    assert text.splitlines()[0] == "SCAN FAILED"
    assert "stage: REPORT" in text
    assert "reason_code: SCAN_ALREADY_RUNNING" in text


def test_an_absurd_cost_exponent_is_bounded_and_marked_unpriced() -> None:
    # S1/L-10: ``format(cost, "f")`` on 1e100000000 would allocate ~100 MB.
    store = _QuietStore(
        rows=(_row("BTCUSDT", report_id="RPT-1", llm_status="VALID", cost_usd="1e100000000"),)
    )

    record = _service(store).notify_scan("SCAN-1")

    assert record is not None
    text = json.loads(record.payload_json)["text"]
    assert "cost: N/A USDT (unpriced)" in text
    assert len(text) <= MAX_OUTBOUND_CHARS


def test_notify_scan_contains_a_render_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import oft.application.notifications as notifications_module

    def _boom(summary: object) -> object:
        raise RuntimeError("render down")

    monkeypatch.setattr(notifications_module, "build_scan_summary_event", _boom)

    assert _service(_FakeStore()).notify_scan("SCAN-1") is None


def test_a_duplicate_event_returns_the_existing_row_not_a_fabricated_one() -> None:
    existing = TelegramOutboxRecord(
        event_key="scan:SCAN-1:summary",
        kind=SCAN_SUMMARY_EVENT_TYPE,
        chat_id=1001,
        reply_to_message_id=None,
        payload_json='{"text": "already queued"}',
        priority=0,
        state=ReceiptState.SENT,
        attempts=1,
        next_attempt_at="2026-11-17T09:30:00.000Z",
        message_id=77,
        last_error_code=None,
        created_at="2026-11-17T09:29:00.000Z",
        sent_at="2026-11-17T09:29:30.000Z",
    )

    class _DuplicateStore(_QuietStore):
        def append_telegram_outbox(self, record: TelegramOutboxRecord) -> bool:
            return False

    store = _DuplicateStore(rows=(_row("BTCUSDT", report_id="RPT-1"),), receipt=existing)
    record = _service(store).notify_scan("SCAN-1")

    assert record is existing
    assert record.state == ReceiptState.SENT
