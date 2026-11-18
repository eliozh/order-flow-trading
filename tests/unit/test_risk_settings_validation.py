"""The shared risk-cap settings service (slice 34).

Proves the effective-cap read (persisted over env), field-scoped validation, the
tightening/loosening policy, atomic audit writes, no-op saves, and the read-only
utilization snapshot.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.entry_service import RiskCapsError
from oft.application.paper_account import PaperAccountService
from oft.application.settings_service import SettingsService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.risk import DEFAULT_CAPS, RISK_CAPS_STATE_KEY, RiskCaps, validate_caps
from oft.domain.settings import SettingValidationError
from oft.ports.store import AuditEventDraft

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
NOW_TEXT = "2026-11-06T10:00:00.000Z"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data", **overrides)  # type: ignore[arg-type]


def _service(tmp_path: Path, **overrides: object) -> tuple[SettingsService, SqliteStore, Settings]:
    settings = _settings(tmp_path, **overrides)
    store = SqliteStore(settings)
    store.initialize()
    return SettingsService(store=store, clock=FixedClock(NOW), settings=settings), store, settings


def _seed_caps(store: SqliteStore, payload: dict[str, object]) -> None:
    store.set_system_state(RISK_CAPS_STATE_KEY, json.dumps(payload), updated_at=NOW_TEXT)


def _seed_audit(store: SqliteStore, *, actor: str = "operator") -> None:
    store.append_audit_event(
        AuditEventDraft(
            event_type="RISK_CAPS_UPDATED",
            actor={"kind": "USER", "id": actor},
            subject={"kind": "SYSTEM", "id": RISK_CAPS_STATE_KEY},
            payload={"after": {"per_trade_risk_pct": 0.5}},
            created_at=NOW_TEXT,
        )
    )


def _seed_open_position(
    store: SqliteStore,
    position_id: str,
    *,
    size: str = "2",
    mark: str | None = "100",
    risk_pct: float = 1.5,
    leverage: float = 3.0,
) -> None:
    """Insert one OPEN position payload for the utilization read model."""
    payload: dict[str, object] = {
        "position_id": position_id,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "size": size,
        "risk_pct": risk_pct,
        "leverage": leverage,
    }
    if mark is not None:
        payload["mark_price"] = mark
    with closing(store._connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO positions (
                position_id, account_id, environment, symbol, state, opened_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (position_id, "ACC-PAPER", "PAPER", "BTCUSDT", "OPEN", NOW_TEXT, json.dumps(payload)),
        )


def _insert_raw_position(store: SqliteStore, position_id: str, payload_json: str) -> None:
    """Insert one OPEN position row with the exact raw payload (corruption included)."""
    with closing(store._connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO positions (
                position_id, account_id, environment, symbol, state, opened_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (position_id, "ACC-PAPER", "PAPER", "BTCUSDT", "OPEN", NOW_TEXT, payload_json),
        )


def _seed_account(store: SqliteStore, settings: Settings) -> None:
    PaperAccountService(settings=settings, store=store, clock=FixedClock(NOW)).summary()


@pytest.fixture(autouse=True)
def _clear_risk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OFT_RISK_PER_TRADE_PCT",
        "OFT_RISK_MAX_CONCURRENT_POSITIONS",
        "OFT_RISK_DAILY_LOSS_PCT",
        "OFT_RISK_MAX_LEVERAGE",
        "OFT_RISK_MAX_NOTIONAL_PCT",
        "OFT_RISK_MAX_SLIPPAGE_BPS",
        "OFT_RISK_MAX_SPREAD_BPS",
        "OFT_RISK_STALE_MARK_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_effective_caps_default_to_the_environment_with_source_default(tmp_path: Path) -> None:
    service, store, settings = _service(tmp_path, risk_per_trade_pct=0.5, risk_max_leverage=3.0)

    contract = service.read_risk_settings().to_contract()

    assert contract["source"] == "default"
    assert contract["caps"]["per_trade_risk_pct"] == 0.5
    assert contract["caps"]["max_leverage"] == 3.0
    # ``defaults`` is the configured env base, visible even when the value is
    # currently persisted.
    assert contract["defaults"]["per_trade_risk_pct"] == 0.5
    assert contract["updated_at"] is None
    assert contract["updated_by"] is None
    assert store.list_audit_events() == ()


def test_persisted_caps_override_only_the_named_fields_with_source_user(tmp_path: Path) -> None:
    service, store, _settings_obj = _service(tmp_path, risk_per_trade_pct=0.5)
    # A partial operator row must not reset the env-tightened per-trade cap.
    _seed_caps(store, {"max_leverage": 2.0})
    _seed_audit(store, actor="web")

    contract = service.read_risk_settings().to_contract()

    assert contract["source"] == "user"
    assert contract["caps"]["per_trade_risk_pct"] == 0.5
    assert contract["caps"]["max_leverage"] == 2.0
    assert contract["defaults"]["per_trade_risk_pct"] == 0.5
    assert contract["updated_at"] == NOW_TEXT
    assert contract["updated_by"] == "web"


def test_unreadable_persisted_caps_fail_closed(tmp_path: Path) -> None:
    service, store, _settings_obj = _service(tmp_path)
    store.set_system_state(RISK_CAPS_STATE_KEY, "{not-json", updated_at=NOW_TEXT)

    with pytest.raises(RiskCapsError):
        service.read_risk_settings()


def test_partial_patch_preserves_the_other_effective_caps(tmp_path: Path) -> None:
    service, store, _settings_obj = _service(tmp_path, risk_per_trade_pct=0.5)

    result = service.update_risk_caps({"max_leverage": 3.0}, actor="cli")

    assert result.changed is True
    assert result.before.per_trade_risk_pct == 0.5
    assert result.after.per_trade_risk_pct == 0.5
    assert result.after.max_leverage == 3.0
    row = store.get_system_state(RISK_CAPS_STATE_KEY)
    assert row is not None
    assert json.loads(row)["per_trade_risk_pct"] == 0.5


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"per_trade_risk_pct": 0.5}, "per_trade_risk_pct"),
        ({"max_leverage": 3.0}, "max_leverage"),
        ({"stale_mark_seconds": 30}, "stale_mark_seconds"),
    ],
)
def test_tightening_writes_directly_and_audits_once(
    tmp_path: Path, changes: dict[str, object], field: str
) -> None:
    service, store, _settings_obj = _service(tmp_path)

    result = service.update_risk_caps(changes, actor="cli")

    assert result.changed is True
    assert result.loosened == ()
    assert getattr(result.after, field) == changes[field]
    audits = store.list_audit_events()
    assert len(audits) == 1
    assert audits[0].event_type == "RISK_CAPS_UPDATED"
    payload = json.loads(audits[0].payload_json)["payload"]
    assert payload["before"]["per_trade_risk_pct"] == 0.8
    assert payload["after"] == result.after.to_state()
    assert payload["loosened"] == []
    assert payload["source_before"] == "default"
    assert payload["source_after"] == "user"


def test_loosening_without_confirmation_requires_confirmation_and_writes_nothing(
    tmp_path: Path,
) -> None:
    service, store, _settings_obj = _service(tmp_path)

    with pytest.raises(SettingValidationError) as excinfo:
        service.update_risk_caps({"per_trade_risk_pct": 1.0}, actor="web")

    error = excinfo.value
    assert error.code == "VALIDATION_FAILED"
    assert error.details
    assert all(detail["code"] == "confirmation_required" for detail in error.details)
    assert [detail["field"] for detail in error.details] == ["per_trade_risk_pct"]
    assert "per_trade_risk_pct" in error.message
    assert store.get_system_state(RISK_CAPS_STATE_KEY) is None
    assert store.list_audit_events() == ()


def test_confirmed_loosening_writes_and_audits_the_before_after_diff(tmp_path: Path) -> None:
    service, store, _settings_obj = _service(tmp_path)

    result = service.update_risk_caps(
        {"per_trade_risk_pct": 1.0}, actor="web", reason="operator review", confirm=True
    )

    assert result.changed is True
    assert result.loosened == ("per_trade_risk_pct",)
    assert result.after.per_trade_risk_pct == 1.0
    audits = store.list_audit_events()
    assert len(audits) == 1
    payload = json.loads(audits[0].payload_json)["payload"]
    assert payload["loosened"] == ["per_trade_risk_pct"]
    assert payload["reason"] == "operator review"
    assert payload["before"]["per_trade_risk_pct"] == 0.8
    assert payload["after"]["per_trade_risk_pct"] == 1.0


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"per_trade_risk_pct": -1}, "per_trade_risk_pct"),
        ({"per_trade_risk_pct": 101}, "per_trade_risk_pct"),
        ({"daily_loss_pct": 0.5}, "daily_loss_pct"),
        ({"max_leverage": 126}, "max_leverage"),
        ({"max_concurrent_positions": 0}, "max_concurrent_positions"),
        ({"stale_mark_seconds": 0}, "stale_mark_seconds"),
        ({"max_spread_bps": float("nan")}, "max_spread_bps"),
        ({"per_trade_risk_pct": True}, "per_trade_risk_pct"),
    ],
)
def test_invalid_values_are_field_scoped_and_write_nothing(
    tmp_path: Path, changes: dict[str, object], field: str
) -> None:
    service, store, _settings_obj = _service(tmp_path)

    with pytest.raises(SettingValidationError) as excinfo:
        service.update_risk_caps(changes, actor="web", confirm=True)

    fields = [detail.get("field") for detail in excinfo.value.details]
    assert field in fields
    assert store.get_system_state(RISK_CAPS_STATE_KEY) is None
    assert store.list_audit_events() == ()


def test_empty_patch_is_validation_failed_and_writes_nothing(tmp_path: Path) -> None:
    service, store, _settings_obj = _service(tmp_path)

    with pytest.raises(SettingValidationError) as excinfo:
        service.update_risk_caps({}, actor="web")

    assert excinfo.value.details
    assert excinfo.value.details[0]["code"] == "empty"
    assert store.get_system_state(RISK_CAPS_STATE_KEY) is None
    assert store.list_audit_events() == ()


def test_same_effective_value_is_a_no_op(tmp_path: Path) -> None:
    service, store, _settings_obj = _service(tmp_path)

    result = service.update_risk_caps({"per_trade_risk_pct": 0.8}, actor="web")

    assert result.changed is False
    assert store.get_system_state(RISK_CAPS_STATE_KEY) is None
    assert store.list_audit_events() == ()


def test_utilization_is_read_only_and_labelled_with_source_and_quality(tmp_path: Path) -> None:
    service, store, _settings_obj = _service(tmp_path)
    before_audits = store.list_audit_events()

    contract = service.read_risk_settings().to_contract()
    utilization = contract["utilization"]

    metrics = utilization["metrics"]
    assert set(metrics) == set(DEFAULT_CAPS.to_state())
    # No paper account row exists yet, so a read must not seed one.
    assert store.get_paper_account("ACC-PAPER") is None
    assert utilization["source"] == "none"
    assert utilization["quality"] == "UNAVAILABLE"
    assert utilization["stale_inputs"]
    for field in ("per_trade_risk_pct", "max_concurrent_positions", "daily_loss_pct"):
        assert metrics[field]["used"] is None
        assert metrics[field]["quality"] == "UNAVAILABLE"
        assert metrics[field]["note"]
    # The three unmeasured caps never pretend to be zero.
    for field in ("max_slippage_bps", "max_spread_bps", "stale_mark_seconds"):
        assert metrics[field]["used"] is None
        assert metrics[field]["source"] == "none"
        assert metrics[field]["quality"] == "UNAVAILABLE"
        assert metrics[field]["note"]
    assert store.list_audit_events() == before_audits
    assert store.get_system_state(RISK_CAPS_STATE_KEY) is None


def test_utilization_reads_the_paper_account_never_mutating_caps(tmp_path: Path) -> None:
    service, store, settings = _service(tmp_path)
    # Seed the account out-of-band so the settings read itself stays read-only.
    PaperAccountService(settings=settings, store=store, clock=FixedClock(NOW)).summary()

    contract = service.read_risk_settings().to_contract()

    utilization = contract["utilization"]
    assert utilization["source"] == "paper_account"
    assert utilization["quality"] == "COMPLETE"
    assert utilization["metrics"]["max_concurrent_positions"]["unit"] == "positions"
    assert utilization["metrics"]["max_concurrent_positions"]["used"] == 0
    assert utilization["metrics"]["per_trade_risk_pct"]["used"] == 0
    assert utilization["metrics"]["max_leverage"]["used"] == 0
    assert store.get_system_state(RISK_CAPS_STATE_KEY) is None
    assert store.list_audit_events() == ()


def test_utilization_uses_the_effective_cap_for_every_metric(tmp_path: Path) -> None:
    service, store, settings = _service(tmp_path, risk_per_trade_pct=0.5)
    from oft.application.paper_account import PaperAccountService

    PaperAccountService(settings=settings, store=store, clock=FixedClock(NOW)).summary()

    metrics = service.read_risk_settings().to_contract()["utilization"]["metrics"]

    expected = RiskCaps(per_trade_risk_pct=0.5).to_state()
    for field, cap in expected.items():
        assert metrics[field]["cap"] == cap


def test_unknown_nested_cap_is_field_scoped_and_writes_nothing(tmp_path: Path) -> None:
    service, store, _settings_obj = _service(tmp_path)

    with pytest.raises(SettingValidationError) as excinfo:
        service.update_risk_caps({"bogus": 1}, actor="web")

    details = excinfo.value.details
    assert details == [
        {"field": "bogus", "code": "unknown_field", "message": "unknown risk cap bogus"}
    ]
    assert store.get_system_state(RISK_CAPS_STATE_KEY) is None
    assert store.list_audit_events() == ()


def test_corrupt_persisted_row_on_the_put_path_is_config_invalid_and_writes_nothing(
    tmp_path: Path,
) -> None:
    service, store, _settings_obj = _service(tmp_path)
    store.set_system_state(RISK_CAPS_STATE_KEY, "{not-json", updated_at=NOW_TEXT)

    with pytest.raises(RiskCapsError):
        service.update_risk_caps({"max_leverage": 3.0}, actor="web")

    assert store.get_system_state(RISK_CAPS_STATE_KEY) == "{not-json"
    assert store.list_audit_events() == ()


def test_invalid_persisted_document_on_the_put_path_is_config_invalid(
    tmp_path: Path,
) -> None:
    service, store, _settings_obj = _service(tmp_path)
    _seed_caps(store, {"per_trade_risk_pct": -1})

    with pytest.raises(RiskCapsError):
        service.update_risk_caps({"max_leverage": 3.0}, actor="web")

    assert json.loads(store.get_system_state(RISK_CAPS_STATE_KEY) or "{}") == {
        "per_trade_risk_pct": -1
    }
    assert store.list_audit_events() == ()


def test_risk_read_and_write_never_scan_the_trade_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No utilization metric consumes the lifetime trade-log totals."""
    service, store, settings = _service(tmp_path)
    PaperAccountService(settings=settings, store=store, clock=FixedClock(NOW)).summary()

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("the risk settings path must not read the trade log")

    monkeypatch.setattr(store, "list_trade_logs", _boom)
    monkeypatch.setattr(PaperAccountService, "closed_totals", _boom)
    monkeypatch.setattr(PaperAccountService, "summary", _boom)

    contract = service.read_risk_settings().to_contract()
    result = service.update_risk_caps({"max_leverage": 4.0}, actor="web")

    assert contract["utilization"]["source"] == "paper_account"
    assert contract["utilization"]["metrics"]["max_leverage"]["used"] == 0
    assert result.changed is True
    assert len(store.list_audit_events()) == 1


def test_legacy_cross_field_document_is_readable_and_repairable(tmp_path: Path) -> None:
    """A persisted row predating the cross-field rule must not brick the reads."""
    service, store, _settings_obj = _service(tmp_path)
    _seed_caps(store, {"per_trade_risk_pct": 0.8, "daily_loss_pct": 0.5})

    summary = service.read_risk_summary()
    assert summary.caps.per_trade_risk_pct == 0.8
    assert summary.caps.daily_loss_pct == 0.5
    # The overview and detail reads stay available for the operator.
    assert [section["section"] for section in service.read_sections()] == [
        "scan",
        "risk",
        "telegram",
    ]
    assert service.read_risk_settings().caps.daily_loss_pct == 0.5

    result = service.update_risk_caps({"daily_loss_pct": 3.0}, actor="cli", confirm=True)

    assert result.after.daily_loss_pct == 3.0
    assert validate_caps(result.after) == ()
    audits = store.list_audit_events()
    assert len(audits) == 1
    row = json.loads(store.get_system_state(RISK_CAPS_STATE_KEY) or "{}")
    assert row["daily_loss_pct"] == 3.0
    assert row["per_trade_risk_pct"] == 0.8


def test_proposed_cross_field_violation_is_rejected_with_daily_loss_code(
    tmp_path: Path,
) -> None:
    service, store, _settings_obj = _service(tmp_path)
    _seed_caps(store, {"per_trade_risk_pct": 0.8, "daily_loss_pct": 0.5})
    before = store.get_system_state(RISK_CAPS_STATE_KEY)

    with pytest.raises(SettingValidationError) as excinfo:
        service.update_risk_caps({"per_trade_risk_pct": 2.0}, actor="cli", confirm=True)

    assert {
        "field": "daily_loss_pct",
        "code": "daily_loss_below_per_trade",
        "message": "daily_loss_pct must be greater than or equal to per_trade_risk_pct",
    } in excinfo.value.details
    assert store.get_system_state(RISK_CAPS_STATE_KEY) == before
    assert store.list_audit_events() == ()


def test_utilization_measures_populated_positions(tmp_path: Path) -> None:
    service, store, settings = _service(tmp_path)
    PaperAccountService(settings=settings, store=store, clock=FixedClock(NOW)).summary()
    _seed_open_position(store, "POS-1", size="2", mark="100", risk_pct=1.5, leverage=3.0)
    _seed_open_position(store, "POS-2", size="1", mark="100", risk_pct=0.5, leverage=2.0)

    metrics = service.read_risk_settings().to_contract()["utilization"]["metrics"]

    # equity = 10,000 cash + (2×100 + 1×100) marked = 10,300.
    assert metrics["max_concurrent_positions"]["used"] == 2
    assert metrics["per_trade_risk_pct"]["used"] == pytest.approx(1.5)
    assert metrics["max_leverage"]["used"] == pytest.approx(3.0)
    assert metrics["max_notional_pct"]["used"] == pytest.approx(300 / 10_300 * 100)
    assert metrics["max_notional_pct"]["quality"] == "COMPLETE"
    # The daily figure is never the lifetime realized PnL.
    assert metrics["daily_loss_pct"]["used"] is None
    assert metrics["daily_loss_pct"]["quality"] == "UNAVAILABLE"
    assert "per-UTC-day" in (metrics["daily_loss_pct"]["note"] or "")


def test_utilization_reports_partial_quality_for_stale_marks(tmp_path: Path) -> None:
    service, store, settings = _service(tmp_path)
    PaperAccountService(settings=settings, store=store, clock=FixedClock(NOW)).summary()
    _seed_open_position(store, "POS-STALE", size="2", mark=None, risk_pct=1.2, leverage=4.0)

    utilization = service.read_risk_settings().to_contract()["utilization"]

    assert utilization["quality"] == "PARTIAL"
    assert "mark" in utilization["stale_inputs"]
    metrics = utilization["metrics"]
    assert metrics["per_trade_risk_pct"]["quality"] == "PARTIAL"
    assert metrics["per_trade_risk_pct"]["used"] == pytest.approx(1.2)
    assert metrics["max_leverage"]["used"] == pytest.approx(4.0)
    # Without a readable mark the notional basis is unknown, never a zero.
    assert metrics["max_notional_pct"]["used"] is None
    assert metrics["max_notional_pct"]["quality"] == "UNAVAILABLE"


def test_utilization_reports_zero_and_complete_for_no_open_positions(tmp_path: Path) -> None:
    service, store, settings = _service(tmp_path)
    _seed_account(store, settings)

    utilization = service.read_risk_settings().to_contract()["utilization"]

    assert utilization["quality"] == "COMPLETE"
    assert utilization["stale_inputs"] == []
    metrics = utilization["metrics"]
    assert metrics["per_trade_risk_pct"]["used"] == 0.0
    assert metrics["per_trade_risk_pct"]["quality"] == "COMPLETE"
    assert metrics["max_leverage"]["used"] == 0.0
    assert metrics["max_leverage"]["quality"] == "COMPLETE"
    assert metrics["max_notional_pct"]["used"] == 0.0
    assert metrics["max_notional_pct"]["quality"] == "COMPLETE"
    assert metrics["max_concurrent_positions"]["used"] == 0
    assert metrics["max_concurrent_positions"]["quality"] == "COMPLETE"


def test_utilization_never_seeds_a_missing_paper_account(tmp_path: Path) -> None:
    service, store, _settings_obj = _service(tmp_path)

    service.read_risk_settings()
    service.read_risk_settings()

    assert store.get_paper_account("ACC-PAPER") is None
    assert store.list_positions(state="OPEN", limit=None) == ()
    assert store.list_audit_events() == ()


def test_utilization_survives_a_corrupt_open_position_payload(tmp_path: Path) -> None:
    service, store, settings = _service(tmp_path)
    _seed_account(store, settings)
    _insert_raw_position(store, "POS-BAD", "{not-json")

    utilization = service.read_risk_settings().to_contract()["utilization"]

    assert utilization["quality"] == "UNAVAILABLE"
    assert "position_payload" in utilization["stale_inputs"]
    metrics = utilization["metrics"]
    for field in ("per_trade_risk_pct", "max_leverage", "max_notional_pct"):
        assert metrics[field]["used"] is None
        assert metrics[field]["quality"] == "UNAVAILABLE"
        assert "payload" in (metrics[field]["note"] or "")
    # The OPEN row count stays knowable even when its payload is not.
    assert metrics["max_concurrent_positions"]["used"] == 1
    assert metrics["max_concurrent_positions"]["quality"] == "PARTIAL"
    assert store.list_audit_events() == ()


def test_update_over_a_corrupt_position_commits_and_returns_the_caps(tmp_path: Path) -> None:
    service, store, settings = _service(tmp_path)
    _seed_account(store, settings)
    _insert_raw_position(store, "POS-BAD", "{not-json")

    result = service.update_risk_caps({"max_leverage": 4.0}, actor="web")

    assert result.changed is True
    assert result.after.max_leverage == 4.0
    assert result.settings.caps.max_leverage == 4.0
    assert result.settings.source.value == "user"
    assert result.settings.updated_at == NOW_TEXT
    assert result.settings.updated_by == "web"
    assert result.settings.utilization.metrics["per_trade_risk_pct"].used is None
    row = store.get_system_state(RISK_CAPS_STATE_KEY)
    assert row is not None
    assert json.loads(row)["max_leverage"] == 4.0
    assert len(store.list_audit_events()) == 1


def test_utilization_reports_unavailable_for_an_unreadable_account_balance(tmp_path: Path) -> None:
    service, store, settings = _service(tmp_path)
    _seed_account(store, settings)
    with closing(store._connect()) as connection, connection:
        connection.execute(
            "UPDATE paper_accounts SET balance = 'not-a-number' WHERE account_id = ?",
            ("ACC-PAPER",),
        )

    utilization = service.read_risk_settings().to_contract()["utilization"]

    assert utilization["quality"] == "UNAVAILABLE"
    assert any("unreadable" in item for item in utilization["stale_inputs"])
    assert all(metric["used"] is None for metric in utilization["metrics"].values())
    assert store.list_audit_events() == ()

    result = service.update_risk_caps({"max_leverage": 4.0}, actor="web")

    assert result.changed is True
    assert result.after.max_leverage == 4.0
    assert result.settings.utilization.quality == "UNAVAILABLE"
    assert len(store.list_audit_events()) == 1


def test_utilization_marks_per_trade_unavailable_when_a_position_omits_risk_pct(
    tmp_path: Path,
) -> None:
    service, store, settings = _service(tmp_path)
    _seed_account(store, settings)
    _insert_raw_position(
        store,
        "POS-1",
        json.dumps({"size": "2", "mark_price": "100", "leverage": 2.0}),
    )
    _insert_raw_position(
        store,
        "POS-2",
        json.dumps({"size": "1", "mark_price": "100", "risk_pct": 0.5, "leverage": 3.0}),
    )

    utilization = service.read_risk_settings().to_contract()["utilization"]
    metrics = utilization["metrics"]

    assert metrics["per_trade_risk_pct"]["used"] is None
    assert metrics["per_trade_risk_pct"]["quality"] == "UNAVAILABLE"
    assert "risk_pct" in (metrics["per_trade_risk_pct"]["note"] or "")
    # A missing per-trade value never hides the leverage that is measurable.
    assert metrics["max_leverage"]["used"] == pytest.approx(3.0)
    assert metrics["max_leverage"]["quality"] == "COMPLETE"
    assert "position_payload" not in utilization["stale_inputs"]


def test_utilization_marks_per_trade_unavailable_for_a_non_finite_risk_pct(
    tmp_path: Path,
) -> None:
    service, store, settings = _service(tmp_path)
    _seed_account(store, settings)
    _insert_raw_position(
        store,
        "POS-NAN",
        json.dumps({"size": "2", "mark_price": "100", "risk_pct": "nan", "leverage": 2.0}),
    )

    metrics = service.read_risk_settings().to_contract()["utilization"]["metrics"]

    assert metrics["per_trade_risk_pct"]["used"] is None
    assert metrics["per_trade_risk_pct"]["quality"] == "UNAVAILABLE"
    assert "risk_pct" in (metrics["per_trade_risk_pct"]["note"] or "")
    assert metrics["max_leverage"]["used"] == pytest.approx(2.0)


def test_confirmation_details_carry_the_server_before_and_after(tmp_path: Path) -> None:
    service, store, _settings_obj = _service(tmp_path)

    with pytest.raises(SettingValidationError) as excinfo:
        service.update_risk_caps(
            {"per_trade_risk_pct": 1.0, "max_leverage": 6.0},
            actor="web",
        )

    details = {detail["field"]: detail for detail in excinfo.value.details}
    assert set(details) == {"per_trade_risk_pct", "max_leverage"}
    assert all(detail["code"] == "confirmation_required" for detail in details.values())
    assert details["per_trade_risk_pct"]["before"] == pytest.approx(0.8)
    assert details["per_trade_risk_pct"]["after"] == pytest.approx(1.0)
    assert details["max_leverage"]["before"] == pytest.approx(5.0)
    assert details["max_leverage"]["after"] == pytest.approx(6.0)
    assert store.get_system_state(RISK_CAPS_STATE_KEY) is None
    assert store.list_audit_events() == ()


def test_a_noop_after_a_save_returns_the_committed_summary(tmp_path: Path) -> None:
    service, store, _settings_obj = _service(tmp_path)
    service.update_risk_caps({"per_trade_risk_pct": 0.5}, actor="web")
    before_audits = store.list_audit_events()

    result = service.update_risk_caps({"per_trade_risk_pct": 0.5}, actor="cli")

    assert result.changed is False
    assert result.settings.source.value == "user"
    assert result.settings.updated_at == NOW_TEXT
    assert result.settings.updated_by == "web"
    assert result.settings.caps.per_trade_risk_pct == 0.5
    assert store.list_audit_events() == before_audits


def test_confirmed_loosening_without_a_reason_records_the_default_reason(tmp_path: Path) -> None:
    service, store, _settings_obj = _service(tmp_path)

    service.update_risk_caps({"per_trade_risk_pct": 1.0}, actor="web", confirm=True)

    payload = json.loads(store.list_audit_events()[0].payload_json)["payload"]
    assert payload["reason"] == "loosening confirmed by operator"


def test_tightening_without_a_reason_records_an_empty_reason(tmp_path: Path) -> None:
    service, store, _settings_obj = _service(tmp_path)

    service.update_risk_caps({"per_trade_risk_pct": 0.5}, actor="web")

    payload = json.loads(store.list_audit_events()[0].payload_json)["payload"]
    assert payload["reason"] == ""


def test_read_sections_never_computes_utilization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _store, _settings_obj = _service(tmp_path)

    def _boom(caps: RiskCaps) -> object:
        raise AssertionError("read_sections must not compute utilization")

    monkeypatch.setattr(service, "_utilization", _boom)

    sections = service.read_sections()

    assert [section["section"] for section in sections] == ["scan", "risk", "telegram"]


def test_utilization_survives_a_position_number_beyond_the_float_range(
    tmp_path: Path,
) -> None:
    service, store, settings = _service(tmp_path)
    _seed_account(store, settings)
    huge = 10**400
    _insert_raw_position(
        store,
        "POS-HUGE",
        json.dumps({"size": "2", "mark_price": "100", "risk_pct": huge, "leverage": huge}),
    )

    metrics = service.read_risk_settings().to_contract()["utilization"]["metrics"]

    assert metrics["per_trade_risk_pct"]["used"] is None
    assert metrics["per_trade_risk_pct"]["quality"] == "UNAVAILABLE"
    assert "risk_pct" in (metrics["per_trade_risk_pct"]["note"] or "")
    assert metrics["max_leverage"]["used"] is None
    assert metrics["max_leverage"]["quality"] == "UNAVAILABLE"
    assert "leverage" in (metrics["max_leverage"]["note"] or "")


def test_utilization_survives_an_unrepresentable_equity_total(tmp_path: Path) -> None:
    service, store, settings = _service(tmp_path)
    _seed_account(store, settings)
    _insert_raw_position(
        store,
        "POS-BIG",
        json.dumps({"size": "1e15", "mark_price": "1e5", "risk_pct": 1.0, "leverage": 2.0}),
    )

    utilization = service.read_risk_settings().to_contract()["utilization"]
    metrics = utilization["metrics"]

    assert utilization["quality"] == "PARTIAL"
    assert "equity" in utilization["stale_inputs"]
    assert metrics["max_notional_pct"]["used"] is None
    assert metrics["max_notional_pct"]["quality"] == "UNAVAILABLE"
    assert metrics["max_notional_pct"]["note"] == "equity could not be computed"
    # The readable per-position metrics and the exact row count keep reporting.
    assert metrics["per_trade_risk_pct"]["used"] == pytest.approx(1.0)
    assert metrics["max_leverage"]["used"] == pytest.approx(2.0)
    assert metrics["max_concurrent_positions"]["used"] == 1


def test_utilization_survives_an_unrepresentable_notional_total(tmp_path: Path) -> None:
    service, store, settings = _service(tmp_path)
    _seed_account(store, settings)
    _insert_raw_position(
        store,
        "POS-ASTRONOMICAL",
        json.dumps(
            {"size": "1e600000", "mark_price": "1e600000", "risk_pct": 1.0, "leverage": 2.0}
        ),
    )

    metrics = service.read_risk_settings().to_contract()["utilization"]["metrics"]

    assert metrics["max_notional_pct"]["used"] is None
    assert metrics["max_notional_pct"]["quality"] == "UNAVAILABLE"
    assert metrics["max_notional_pct"]["note"] == "position notional could not be computed"
    assert metrics["per_trade_risk_pct"]["used"] == pytest.approx(1.0)


def test_utilization_marks_leverage_unavailable_when_a_position_omits_it(
    tmp_path: Path,
) -> None:
    service, store, settings = _service(tmp_path)
    _seed_account(store, settings)
    _insert_raw_position(
        store,
        "POS-1",
        json.dumps({"size": "2", "mark_price": "100", "risk_pct": 1.5}),
    )
    _insert_raw_position(
        store,
        "POS-2",
        json.dumps({"size": "1", "mark_price": "100", "risk_pct": 0.5, "leverage": 2.0}),
    )

    metrics = service.read_risk_settings().to_contract()["utilization"]["metrics"]

    assert metrics["max_leverage"]["used"] is None
    assert metrics["max_leverage"]["quality"] == "UNAVAILABLE"
    assert "leverage" in (metrics["max_leverage"]["note"] or "")
    assert metrics["per_trade_risk_pct"]["used"] == pytest.approx(1.5)
    assert metrics["per_trade_risk_pct"]["quality"] == "COMPLETE"


def test_utilization_marks_leverage_unavailable_for_non_finite_or_bool_leverage(
    tmp_path: Path,
) -> None:
    service, store, settings = _service(tmp_path)
    _seed_account(store, settings)
    _insert_raw_position(
        store,
        "POS-NAN",
        json.dumps({"size": "2", "mark_price": "100", "risk_pct": 1.5, "leverage": "NaN"}),
    )
    _insert_raw_position(
        store,
        "POS-BOOL",
        json.dumps({"size": "1", "mark_price": "100", "risk_pct": 0.5, "leverage": True}),
    )

    metrics = service.read_risk_settings().to_contract()["utilization"]["metrics"]

    assert metrics["max_leverage"]["used"] is None
    assert metrics["max_leverage"]["quality"] == "UNAVAILABLE"
    assert "leverage" in (metrics["max_leverage"]["note"] or "")
    assert metrics["per_trade_risk_pct"]["used"] == pytest.approx(1.5)
    assert metrics["per_trade_risk_pct"]["quality"] == "COMPLETE"


def test_notional_note_names_the_missing_mark(tmp_path: Path) -> None:
    service, store, settings = _service(tmp_path)
    _seed_account(store, settings)
    _insert_raw_position(
        store, "POS-NO-MARK", json.dumps({"size": "2", "risk_pct": 1.0, "leverage": 2.0})
    )

    metrics = service.read_risk_settings().to_contract()["utilization"]["metrics"]

    assert metrics["max_notional_pct"]["used"] is None
    assert metrics["max_notional_pct"]["note"] == (
        "mark_price is missing or not finite on an open position"
    )
    assert metrics["per_trade_risk_pct"]["used"] == pytest.approx(1.0)


def test_notional_note_reports_non_positive_equity(tmp_path: Path) -> None:
    service, store, settings = _service(tmp_path)
    _seed_account(store, settings)
    with closing(store._connect()) as connection, connection:
        connection.execute(
            "UPDATE paper_accounts SET balance = '0' WHERE account_id = ?", ("ACC-PAPER",)
        )

    metrics = service.read_risk_settings().to_contract()["utilization"]["metrics"]

    assert metrics["max_notional_pct"]["used"] is None
    assert metrics["max_notional_pct"]["note"] == "equity is not positive"
