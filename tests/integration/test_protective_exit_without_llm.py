"""The code-only protective exit closes a stop-hit position with no LLM at all."""

from __future__ import annotations

from pathlib import Path

from test_position_monitor_job import (
    NOW,
    NOW_TEXT,
    _container,
    _open_long,
    _result_for,
    _runner,
)

from oft.application.jobs import PROTECTIVE_STOP
from oft.application.position_service import PositionService
from oft.clock import FixedClock
from position_support import set_position_mark


def test_a_stop_hit_position_closes_with_no_provider_and_no_llm_rows(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    # No OFT_LLM_PROVIDER: the composition root resolves no adapter.
    assert container.settings.llm_provider is None
    assert container.llm_provider is None

    position_id = _open_long(container, "CODELY")
    set_position_mark(container, position_id, "69000", as_of=NOW_TEXT)

    service = PositionService(
        store=container.store, clock=FixedClock(NOW), settings=container.settings
    )
    monitor_result = service.monitor(position_id)

    assert monitor_result.status == "CLOSED"
    # Slice 23 contract: the monitor result's reason code stays STOP_TOUCH.
    assert monitor_result.reason_code == "STOP_TOUCH"
    assert container.store.list_trade_logs(limit=None) != ()
    assert container.store.list_llm_calls() == ()
    assert container.store.list_decisions(role="MANAGEMENT") == ()


def test_the_running_tick_records_protective_stop_without_any_llm_call(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    position_id = _open_long(container, "TICKCODELY")
    set_position_mark(container, position_id, "69000", as_of=NOW_TEXT)

    outcome = _runner(container).run_tick(NOW)

    result = _result_for(outcome, position_id)
    assert result.status == "CLOSED"
    assert result.reason_code == PROTECTIVE_STOP
    assert container.store.list_positions(state="OPEN") == ()
    assert container.store.list_llm_calls() == ()
    assert container.store.list_budget_records() == ()
    assert container.store.list_decisions(role="MANAGEMENT") == ()
