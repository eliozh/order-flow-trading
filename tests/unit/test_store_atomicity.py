"""Scan + manifest persistence is a single transaction (unit of work)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.config import Settings
from oft.ports.store import (
    AuditEventDraft,
    FillRecord,
    IdempotencyRecord,
    ManifestEntry,
    OrderRecord,
    PaperAccountRecord,
    PositionRecord,
    ScanRecord,
)


def _record(scan_id: str) -> ScanRecord:
    return ScanRecord(
        scan_id=scan_id,
        symbol="BTCUSDT",
        period="4h",
        status="DONE",
        trigger_source="MANUAL",
        metrics_only=True,
        fixture_path="fixture.csv",
        started_at="2026-11-06T00:00:00.000Z",
        finished_at="2026-11-06T00:00:01.000Z",
        window_start="2025-11-05T00:00:00.000Z",
        window_end="2025-11-05T04:00:00.000Z",
        trade_count=1,
        buy_volume="1",
        sell_volume="0",
        delta="1",
        cvd="1",
        data_quality="COMPLETE",
        manifest_path="/tmp/part.parquet",
        parquet_rows=1,
        created_at="2026-11-06T00:00:01.000Z",
    )


def _entry(path: str) -> ManifestEntry:
    return ManifestEntry(
        path=path,
        data_type="agg_trades",
        symbol="BTCUSDT",
        date="2025-11-05",
        rows=1,
        bytes=10,
        checksum="abc",
        source="test",
        created_at="2026-11-06T00:00:01.000Z",
    )


def test_scan_and_manifest_roll_back_together(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    entry = _entry("/tmp/part.parquet")
    store.save_scan_with_manifest(_record("SCAN-1"), entry)

    with pytest.raises(sqlite3.IntegrityError):
        # Duplicate manifest path fails the second insert; the scan row from the
        # same unit of work must not be committed either.
        store.save_scan_with_manifest(_record("SCAN-2"), entry)

    assert store.get_scan("SCAN-2") is None
    assert len(store.list_manifest()) == 1


def _oms(suffix: str) -> tuple[OrderRecord, FillRecord, PositionRecord, PaperAccountRecord]:
    stamp = "2026-11-06T12:00:00.000Z"
    order = OrderRecord(
        order_id=f"ORD-{suffix}",
        client_order_id=f"cli-{suffix}",
        environment="PAPER",
        symbol="BTCUSDT",
        status="FILLED",
        risk_verdict_id="RISK-1",
        idempotency_key=f"idem-{suffix}",
        created_at=stamp,
        updated_at=stamp,
        payload_json="{}",
    )
    fill = FillRecord(
        fill_id=f"FIL-{suffix}",
        order_id=order.order_id,
        environment="PAPER",
        symbol="BTCUSDT",
        filled_at=stamp,
        payload_json="{}",
    )
    position = PositionRecord(
        position_id=f"POS-{suffix}",
        account_id="ACC-PAPER",
        environment="PAPER",
        symbol="BTCUSDT",
        state="OPEN",
        opened_at=stamp,
        payload_json="{}",
    )
    account = PaperAccountRecord(
        account_id="ACC-PAPER",
        environment="PAPER",
        currency="USDT",
        initial_balance="10000.00",
        balance="9990.00",
        updated_at=stamp,
    )
    return order, fill, position, account


def _audit(suffix: str) -> AuditEventDraft:
    return AuditEventDraft(
        event_type="ORDER_FILLED",
        actor={"kind": "USER", "id": "tester"},
        subject={"kind": "ORDER", "id": f"ORD-{suffix}"},
        payload={"suffix": suffix},
        created_at="2026-11-06T12:00:00.000Z",
    )


def test_order_fill_position_and_audit_commit_together(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    order, fill, position, account = _oms("1")

    commit = store.save_order_fill_position(
        order=order,
        fill=fill,
        position=position,
        account=account,
        audit=_audit("1"),
    )

    assert commit.audit is not None
    assert commit.audit.seq == 1
    assert commit.audit.event_type == "ORDER_FILLED"
    assert commit.account.balance == "10000.00000000"
    assert store.get_order(order.order_id) is not None
    assert store.get_fill_by_order_id(order.order_id) is not None
    assert len(store.list_audit_events()) == 1

    # A failed OMS write must roll back the audit row written in the same unit.
    with pytest.raises(sqlite3.IntegrityError):
        store.save_order_fill_position(
            order=order,
            fill=fill,
            position=position,
            account=account,
            audit=_audit("2"),
        )

    audit_events = store.list_audit_events()
    assert len(audit_events) == 1
    assert audit_events[0].seq == 1
    assert len(store.list_orders()) == 1


def test_balance_delta_is_applied_inside_the_write_transaction(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()

    # Both calls carry the same stale absolute balance; only the in-transaction
    # delta application can keep both debits.
    order_one, fill_one, position_one, account = _oms("1")
    store.save_order_fill_position(
        order=order_one,
        fill=fill_one,
        position=position_one,
        account=account,
        balance_delta="-700.00",
    )
    order_two, fill_two, position_two, _ = _oms("2")
    store.save_order_fill_position(
        order=order_two,
        fill=fill_two,
        position=position_two,
        account=account,
        balance_delta="-700.00",
    )

    stored = store.get_paper_account("ACC-PAPER")
    assert stored is not None
    assert stored.balance == "8600.00000000"


def test_completing_a_purged_idempotency_lease_reinserts_the_response(
    tmp_path: Path,
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    reservation = IdempotencyRecord(
        scope="test:POST:/api/v1/prompts/analysis/versions",
        key="lease-lost",
        request_hash="REQUEST",
        response_status=0,
        response_json="",
        created_at="2026-11-08T12:00:00.000Z",
        expires_at="2026-11-08T12:00:30.000Z",
    )
    assert store.reserve_idempotency(reservation) is True
    # A concurrent purge (or an expired lease reclaimed by another writer)
    # removes the reservation before the first request completes.
    store.delete_idempotency(scope=reservation.scope, key=reservation.key)
    assert store.get_idempotency(scope=reservation.scope, key=reservation.key) is None

    store.complete_idempotency(
        IdempotencyRecord(
            scope=reservation.scope,
            key=reservation.key,
            request_hash="REQUEST",
            response_status=201,
            response_json='{"ok": true}',
            created_at="2026-11-08T12:00:01.000Z",
            expires_at="2026-11-09T12:00:01.000Z",
        )
    )

    # The lost lease must not silently lose the recorded response.
    stored = store.get_idempotency(scope=reservation.scope, key=reservation.key)
    assert stored is not None
    assert stored.response_status == 201
    assert stored.response_json == '{"ok": true}'
