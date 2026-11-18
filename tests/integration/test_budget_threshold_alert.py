"""Integration: durable BUDGET_THRESHOLD crossings push one Telegram alert each.

The alert is derived from the audit draft the budget ledger already wrote; no
spend is recomputed and no threshold or call row is changed by the push path.
Every test builds the real container over a hermetic secret store.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_management_flow import _setup

from oft.adapters.llm_stub import LLMStub
from oft.application.budget_ledger import BudgetLedger
from oft.application.jobs import JobLedger, ManagementJobRunner
from oft.application.management_service import ManagementService
from oft.application.notifications import NotificationService
from oft.application.settings_service import SettingsService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.budget_periods import period_parts
from oft.domain.telegram import BUDGET_PUSH_KEY
from oft.domain.time import utc_timestamp
from oft.ports.llm import CostEstimate, LLMResult, LLMRole, LLMUsage
from oft.ports.store import (
    BudgetSettingsRecord,
    ConfigRecord,
    LLMCallRecord,
)

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
NOW_TEXT = utc_timestamp(NOW)
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"
COOLDOWN_PREFIX = "telegram.alert_cooldown."


@pytest.fixture(autouse=True)
def _hermetic_secret_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the encrypted-file store so every test is hermetic in CI.

    CI has no OS keyring, so a container built without a master key raises
    ``SecretStoreUnavailable``. The keyring backend is forced to ``fail`` (in
    the environment and in-process, in case another test already probed a
    desktop backend) and a per-test ``OFT_MASTER_KEY`` selects the encrypted
    file, matching ``tests/integration/test_position_alerts.py``.
    """
    import keyring
    from keyring.backends.fail import Keyring as FailKeyring

    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("OFT_MASTER_KEY", "per-test-master-key-0123456789abcdef")
    previous = keyring.get_keyring()
    keyring.set_keyring(FailKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _container(tmp_path: Path, *, daily: str = "10.00") -> tuple[Any, Settings]:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    container.telegram_service.set_token(SENTINEL, actor="test")
    container.telegram_service.update_allowlist([1001], [2002], actor="test")
    container.store.save_budget_settings(
        BudgetSettingsRecord(
            daily_usd=daily,
            weekly_usd="100.00",
            monthly_usd="1000.00",
            warn_pct=80.0,
            updated_at=NOW_TEXT,
        )
    )
    return container, settings


def _ledger(container: Any, settings: Settings) -> BudgetLedger:
    return BudgetLedger(
        store=container.store,
        settings=settings,
        notifications=container.notifications,
    )


def _result() -> LLMResult:
    return LLMResult(
        provider="openai",
        model="gpt-fixture",
        raw_text="{}",
        usage=LLMUsage(input_tokens=10, output_tokens=10),
    )


def _call_row(call_id: str) -> LLMCallRecord:
    return LLMCallRecord(
        call_id=call_id,
        role="ANALYSIS",
        provider="openai",
        model="gpt-fixture",
        prompt_id="analysis",
        prompt_version=1,
        prompt_hash="ph",
        input_hash="ih",
        attempt=1,
        request_id="req",
        status="OK",
        raw_output_hash="rh",
        usage_json="{}",
        latency_ms=1,
        created_at=NOW_TEXT,
    )


def _record(
    store: Any,
    ledger: BudgetLedger,
    call_id: str,
    cost: str,
    *,
    occurred_at: str = NOW_TEXT,
    unpriced: bool = False,
) -> Any:
    # The budget ledger's FK requires the call row the orchestrator would have
    # written before recording its spend.
    store.save_llm_call(_call_row(call_id))
    return ledger.record_call(
        call_id=call_id,
        decision_id=None,
        role="ANALYSIS",
        result=_result(),
        occurred_at=occurred_at,
        unpriced=unpriced,
        cost_usd=cost,
        price_version="test/v1",
    )


def _receipts(container: Any) -> list[Any]:
    return [
        receipt
        for receipt in container.store.list_telegram_receipts()
        if receipt.kind == "budget_threshold"
    ]


def _keys(container: Any) -> list[str]:
    return sorted(receipt.event_key for receipt in _receipts(container))


def _text(receipt: Any) -> str:
    return json.loads(receipt.payload_json)["text"]


def test_crossing_eighty_and_one_hundred_alerts_once_each_and_never_stops(tmp_path: Path) -> None:
    container, settings = _container(tmp_path)
    ledger = _ledger(container, settings)

    warn = _record(container.store, ledger, "CALL-WARN-1", "8.50")
    assert warn.record.budget_state == "WARN"
    receipts = _receipts(container)
    assert len(receipts) == 1
    assert receipts[0].event_key == "budget:day:WARN:2026-11-17"
    text = _text(receipts[0])
    assert text.splitlines()[0] == "预算告警 · DAY · WARN"
    assert "spend_usd: 8.5 USDT" in text.splitlines()
    assert "threshold_usd: 10 USDT" in text.splitlines()
    assert "percentage: 85 %" in text.splitlines()
    assert "unpriced_count: 0" in text.splitlines()
    assert "达到 80% 与 100% 阈值时经 Telegram 告警；不会停止调用。" in text.splitlines()
    assert SENTINEL not in text

    exceeded = _record(container.store, ledger, "CALL-EXCEEDED-1", "1.75")
    assert exceeded.record.budget_state == "EXCEEDED"
    assert _keys(container) == [
        "budget:day:EXCEEDED:2026-11-17",
        "budget:day:WARN:2026-11-17",
    ]

    # A later call while already above the threshold writes no new alert.
    still_above = _record(container.store, ledger, "CALL-ABOVE-2", "1.00")
    assert still_above.record.budget_state == "EXCEEDED"
    assert len(_receipts(container)) == 2


def test_day_week_and_month_each_alert_once_per_crossing(tmp_path: Path) -> None:
    container, settings = _container(tmp_path)
    container.store.save_budget_settings(
        BudgetSettingsRecord(
            daily_usd="10.00",
            weekly_usd="10.00",
            monthly_usd="10.00",
            warn_pct=80.0,
            updated_at=NOW_TEXT,
        )
    )
    ledger = _ledger(container, settings)
    day, week, month = period_parts(NOW_TEXT)

    _record(container.store, ledger, "CALL-PERIODS-1", "8.50")

    assert _keys(container) == sorted(
        [
            f"budget:day:WARN:{day}",
            f"budget:week:WARN:{week}",
            f"budget:month:WARN:{month}",
        ]
    )
    assert len({receipt.event_key for receipt in _receipts(container)}) == 3

    # Still WARN in every window: no crossing, no duplicate push.
    _record(container.store, ledger, "CALL-PERIODS-2", "1.00")
    assert len(_receipts(container)) == 3
    cooldown_rows = sorted(
        key for key in _all_state_keys(container) if key.startswith(COOLDOWN_PREFIX)
    )
    assert cooldown_rows == sorted(
        [
            f"{COOLDOWN_PREFIX}budget:day:WARN",
            f"{COOLDOWN_PREFIX}budget:week:WARN",
            f"{COOLDOWN_PREFIX}budget:month:WARN",
        ]
    )

    # Cross 100% in every window; each EXCEEDED window alerts exactly once.
    exceeded = _record(container.store, ledger, "CALL-PERIODS-3", "2.00")
    assert exceeded.record.budget_state == "EXCEEDED"
    keys = _keys(container)
    for period, period_key in (("day", day), ("week", week), ("month", month)):
        assert keys.count(f"budget:{period}:EXCEEDED:{period_key}") == 1
    assert len(keys) == 6


def test_a_new_day_period_alerts_again_with_one_bounded_cooldown_row(tmp_path: Path) -> None:
    container, settings = _container(tmp_path)
    ledger = _ledger(container, settings)

    _record(container.store, ledger, "CALL-DAY-1", "8.50")
    next_text = "2026-11-18T09:30:00.000Z"
    _record(container.store, ledger, "CALL-DAY-2", "8.50", occurred_at=next_text)

    assert _keys(container) == [
        "budget:day:WARN:2026-11-17",
        "budget:day:WARN:2026-11-18",
    ]
    assert container.store.get_system_state(f"{COOLDOWN_PREFIX}budget:day:WARN") == "2026-11-18"
    cooldown_rows = [key for key in _all_state_keys(container) if key.startswith(COOLDOWN_PREFIX)]
    assert cooldown_rows == [f"{COOLDOWN_PREFIX}budget:day:WARN"]


def test_restart_and_duplicate_replays_do_not_resend(tmp_path: Path) -> None:
    container, settings = _container(tmp_path)
    ledger = _ledger(container, settings)
    _record(container.store, ledger, "CALL-RESTART-1", "8.50")
    assert len(_receipts(container)) == 1

    assert container.telegram_service is not None
    restarted = NotificationService(
        store=container.store,
        clock=container.clock,
        channel=container.telegram_service,
        settings_service=SettingsService(
            store=container.store, clock=container.clock, settings=container.settings
        ),
    )
    replay = restarted.enqueue_budget_threshold(
        period="day",
        period_key="2026-11-17",
        spend_usd="8.50",
        threshold_usd="10.00",
        state="WARN",
        occurred_at=NOW_TEXT,
    )
    assert replay is None
    assert len(_receipts(container)) == 1

    # A replayed ledger row is also silent: the durable row already exists.
    _record(container.store, ledger, "CALL-RESTART-1", "8.50")
    assert len(_receipts(container)) == 1


def test_the_alert_path_writes_no_call_ledger_or_settings_rows(tmp_path: Path) -> None:
    container, settings = _container(tmp_path)
    ledger = _ledger(container, settings)
    _record(container.store, ledger, "CALL-ISOLATED-1", "8.50")
    assert container.notifications is not None

    settings_before = container.store.get_budget_settings()
    calls_before = len(container.store.list_llm_calls())
    budget_before = len(container.store.list_budget_records())

    record = container.notifications.enqueue_budget_threshold(
        period="day",
        period_key="2026-11-17",
        spend_usd="10.25",
        threshold_usd="10.00",
        state="EXCEEDED",
        occurred_at=NOW_TEXT,
    )

    assert record is not None
    assert len(container.store.list_llm_calls()) == calls_before
    assert len(container.store.list_budget_records()) == budget_before
    assert container.store.get_budget_settings() == settings_before


def test_exceeded_returns_normally_from_record_billed_call(tmp_path: Path) -> None:
    container, settings = _container(tmp_path, daily="1.00")
    ledger = _ledger(container, settings)

    stored_call, entry = ledger.record_billed_call(
        call=_call_row("CALL-BILLED-EXCEEDED"),
        result=_result(),
        occurred_at=NOW_TEXT,
        unpriced=False,
        cost_usd="2.00",
        price_version="test/v1",
    )

    assert stored_call.call_id == "CALL-BILLED-EXCEEDED"
    assert entry.record.budget_state == "EXCEEDED"
    assert _keys(container) == ["budget:day:EXCEEDED:2026-11-17"]
    assert len(container.store.list_llm_calls()) == 1


def test_a_disabled_budget_toggle_writes_no_row_and_no_cooldown_marker(tmp_path: Path) -> None:
    container, settings = _container(tmp_path)
    container.store.set_config(
        ConfigRecord(
            key=BUDGET_PUSH_KEY,
            value_json="false",
            updated_at=NOW_TEXT,
            updated_by="test",
        )
    )
    ledger = _ledger(container, settings)

    entry = _record(container.store, ledger, "CALL-TOGGLED-OFF", "8.50")

    # The crossing did happen; the disabled toggle is what suppresses the push.
    assert entry.record.budget_state == "WARN"
    records = container.store.list_budget_records()
    assert len(records) == 1
    assert records[0].budget_state == "WARN"
    assert _receipts(container) == []
    assert container.store.get_system_state(f"{COOLDOWN_PREFIX}budget:day:WARN") is None


def test_a_telegram_failure_leaves_the_budget_ledger_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, settings = _container(tmp_path)
    ledger = _ledger(container, settings)
    settings_before = container.store.get_budget_settings()

    def _boom(record: Any) -> bool:
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(container.store, "append_telegram_outbox", _boom)

    entry = _record(container.store, ledger, "CALL-OUTBOX-FAILURE", "8.50")

    assert entry.record.budget_state == "WARN"
    assert _receipts(container) == []
    assert container.store.get_budget_settings() == settings_before
    records = container.store.list_budget_records()
    assert len(records) == 1
    assert records[0].budget_state == "WARN"
    assert container.store.get_system_state(f"{COOLDOWN_PREFIX}budget:day:WARN") is None


def test_unpriced_usage_is_surfaced_as_a_real_count(tmp_path: Path) -> None:
    container, settings = _container(tmp_path)
    ledger = _ledger(container, settings)

    _record(container.store, ledger, "CALL-UNPRICED-1", "0", unpriced=True)
    _record(container.store, ledger, "CALL-PRICED-1", "8.50")

    receipts = _receipts(container)
    assert len(receipts) == 1
    assert "unpriced_count: 1" in _text(receipts[0]).splitlines()


def test_an_unreadable_ledger_count_renders_na_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, _settings_value = _container(tmp_path)
    assert container.notifications is not None

    def _boom(period: str, key: str) -> tuple[Any, ...]:
        raise sqlite3.OperationalError("budget ledger unavailable")

    monkeypatch.setattr(container.store, "list_budget_records_for_period", _boom)

    record = container.notifications.enqueue_budget_threshold(
        period="day",
        period_key="2026-11-17",
        spend_usd="10.25",
        threshold_usd="10.00",
        state="EXCEEDED",
        occurred_at=NOW_TEXT,
    )

    assert record is not None
    assert "unpriced_count: N/A" in _text(record).splitlines()


def test_billed_warn_then_exceeded_alerts_once_each_and_never_raises(tmp_path: Path) -> None:
    container, settings = _container(tmp_path, daily="2.50")
    ledger = _ledger(container, settings)

    _, warn = ledger.record_billed_call(
        call=_call_row("CALL-BILLED-WARN"),
        result=_result(),
        occurred_at=NOW_TEXT,
        unpriced=False,
        cost_usd="2.00",
        price_version="test/v1",
    )
    _, exceeded = ledger.record_billed_call(
        call=_call_row("CALL-BILLED-OVER"),
        result=_result(),
        occurred_at=NOW_TEXT,
        unpriced=False,
        cost_usd="2.00",
        price_version="test/v1",
    )

    assert warn.record.budget_state == "WARN"
    # EXCEEDED returned normally: the alert path never raises or hard-stops.
    assert exceeded.record.budget_state == "EXCEEDED"
    assert _keys(container) == [
        "budget:day:EXCEEDED:2026-11-17",
        "budget:day:WARN:2026-11-17",
    ]


def test_a_billed_rollback_enqueues_no_alert_even_after_compute_populates_drafts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, settings = _container(tmp_path, daily="1.00")
    ledger = _ledger(container, settings)

    def _raise_after_compute(call: Any, budget: Any, compute: Any) -> Any:
        # Run the caller's compute so its captured audit drafts are populated,
        # then fail the transaction: the push must not happen before commit.
        compute(
            {
                "day": {"spend_usd": "0"},
                "week": {"spend_usd": "0"},
                "month": {"spend_usd": "0"},
            }
        )
        raise sqlite3.OperationalError("commit failed")

    monkeypatch.setattr(container.store, "save_call_and_budget", _raise_after_compute)

    with pytest.raises(sqlite3.OperationalError):
        ledger.record_billed_call(
            call=_call_row("CALL-BILLED-ROLLBACK"),
            result=_result(),
            occurred_at=NOW_TEXT,
            unpriced=False,
            cost_usd="2.00",
            price_version="test/v1",
        )

    assert _receipts(container) == []
    assert container.store.get_system_state(f"{COOLDOWN_PREFIX}budget:day:EXCEEDED") is None


def test_billed_duplicate_replay_still_alerts_once(tmp_path: Path) -> None:
    container, settings = _container(tmp_path, daily="1.00")
    ledger = _ledger(container, settings)

    for _ in range(2):
        _, entry = ledger.record_billed_call(
            call=_call_row("CALL-BILLED-REPLAY"),
            result=_result(),
            occurred_at=NOW_TEXT,
            unpriced=False,
            cost_usd="2.00",
            price_version="test/v1",
        )
        assert entry.record.budget_state == "EXCEEDED"

    # The adapter reallocates the duplicate call id, so the second transaction
    # is already EXCEEDED and crosses nothing; one alert survives.
    assert len(container.store.list_llm_calls()) == 2
    assert _keys(container) == ["budget:day:EXCEEDED:2026-11-17"]


def test_a_single_jump_from_ok_to_exceeded_alerts_only_exceeded(tmp_path: Path) -> None:
    container, settings = _container(tmp_path, daily="1.00")
    ledger = _ledger(container, settings)

    entry = _record(container.store, ledger, "CALL-JUMP-1", "2.00")

    # The durable audit emits one EXCEEDED draft for a jump, not a WARN first.
    assert entry.record.budget_state == "EXCEEDED"
    assert _keys(container) == ["budget:day:EXCEEDED:2026-11-17"]


class _RaisingBudgetSink:
    def enqueue_budget_threshold(self, **kwargs: Any) -> None:
        raise RuntimeError("sink unavailable")


def test_a_raising_budget_sink_leaves_the_record_call_result_intact(tmp_path: Path) -> None:
    container, settings = _container(tmp_path)
    ledger = BudgetLedger(
        store=container.store,
        settings=settings,
        notifications=_RaisingBudgetSink(),
    )

    entry = _record(container.store, ledger, "CALL-SINK-RAISES", "8.50")

    assert entry.record.budget_state == "WARN"
    records = container.store.list_budget_records()
    assert len(records) == 1
    assert records[0].budget_state == "WARN"
    assert _receipts(container) == []


class _PricedProvider(LLMStub):
    """A provider seam that reports a priced call so a threshold can be crossed."""

    def estimate_cost(self, request: object, model: str) -> CostEstimate:  # type: ignore[override]
        return CostEstimate(amount_usd="2.00", price_version="test/v1", unpriced=False)


def test_a_wired_management_service_forwards_the_budget_sink(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    settings = setup["settings"]
    container.telegram_service.set_token(SENTINEL, actor="test")
    container.telegram_service.update_allowlist([1001], [2002], actor="test")
    container.store.save_budget_settings(
        BudgetSettingsRecord(
            daily_usd="2.50",
            weekly_usd="50.00",
            monthly_usd="150.00",
            warn_pct=80.0,
            updated_at=NOW_TEXT,
        )
    )
    provider = _PricedProvider(
        fixtures_dir=FIXTURES,
        overrides={LLMRole.MANAGEMENT: "management_hold.json"},
    )
    service = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=provider,
        settings=settings,
        kill_switch=setup["kill"],
        runtime=container.llm_runtime,
        notifications=container.notifications,
    )
    runner = ManagementJobRunner(
        service=service,
        ledger=JobLedger(store=container.store, clock=container.clock),
        clock=container.clock,
        period="4h",
    )

    outcome = runner.run_window(container.clock.now())

    # The pass-through cannot be silently dropped: the real wired service
    # forwards its sink and the crossing reaches the outbox.
    assert outcome.status == "DONE"
    receipts = _receipts(container)
    assert [receipt.kind for receipt in receipts] == ["budget_threshold"]


def _all_state_keys(container: Any) -> list[str]:
    with contextlib.closing(sqlite3.connect(container.store.path)) as connection:
        rows = connection.execute("SELECT key FROM system_state ORDER BY key").fetchall()
    return [row[0] for row in rows]
