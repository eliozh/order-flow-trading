"""Integration: a populated v24 database upgrades to v25 == a fresh v25 database."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_review_flow import FIXTURES, NOW, _closed_trade

from oft.adapters import sqlite_schema
from oft.adapters.sqlite_store import SqliteStore, database_path
from oft.application.journal_service import JournalService
from oft.clock import FixedClock
from oft.config import Settings

_REVIEW_PROMPTS = ("review", "review:system")


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]


def _indexes(connection: sqlite3.Connection, table: str) -> list[str]:
    return sorted(str(row[1]) for row in connection.execute(f"PRAGMA index_list({table})"))


def _foreign_keys(connection: sqlite3.Connection, table: str) -> set[tuple[str, str]]:
    return {
        (str(row[3]), str(row[2]))
        for row in connection.execute(f"PRAGMA foreign_key_list({table})")
    }


def _review_prompt_rows(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
    placeholders = ", ".join("?" for _ in _REVIEW_PROMPTS)
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT prompt_id, version, body, body_hash, required_variables_json, "
            f"status, is_active, system_prompt_id, system_prompt_version "
            f"FROM prompt_versions WHERE prompt_id IN ({placeholders}) ORDER BY prompt_id",
            _REVIEW_PROMPTS,
        )
    ]


def test_a_populated_v24_database_upgrades_to_v25_matching_a_fresh_database(
    tmp_path: Path,
) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path / "migrated")
    database = database_path(settings)
    trade_id = trade["trade_id"]
    before = json.loads(container.store.get_trade_log(trade_id).payload_json)  # type: ignore[union-attr]

    # Downgrade the fresh v25 database to the populated v24 shape: no REVIEW
    # prompt family, no review link column, no v25 stamp. The trade row and its
    # payload are deliberately left untouched.
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX IF EXISTS trade_logs_review_decision")
        connection.execute("ALTER TABLE trade_logs DROP COLUMN review_decision_id")
        connection.execute(
            "DELETE FROM prompt_versions WHERE prompt_id IN ('review', 'review:system')"
        )
        connection.execute(
            "DELETE FROM prompt_templates WHERE prompt_id IN ('review', 'review:system')"
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 25")
        connection.commit()
        downgraded_columns = _columns(connection, "trade_logs")
        downgraded_versions = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations")
        }
    assert "review_decision_id" not in downgraded_columns
    assert 25 not in downgraded_versions

    store = SqliteStore(settings)
    store.initialize()
    store.initialize()  # idempotent re-initialize

    fresh_settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "fresh" / "data",
        archive_fixture_dir=settings.archive_fixture_dir,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )
    SqliteStore(fresh_settings).initialize()

    migrated_connection = sqlite3.connect(database)
    fresh_connection = sqlite3.connect(database_path(fresh_settings))
    try:
        assert _columns(migrated_connection, "trade_logs") == _columns(
            fresh_connection, "trade_logs"
        )
        assert _indexes(migrated_connection, "trade_logs") == _indexes(
            fresh_connection, "trade_logs"
        )
        assert _foreign_keys(migrated_connection, "trade_logs") == _foreign_keys(
            fresh_connection, "trade_logs"
        )
        assert ("review_decision_id", "decisions") in _foreign_keys(
            migrated_connection, "trade_logs"
        )
        assert _review_prompt_rows(migrated_connection) == _review_prompt_rows(fresh_connection)
        versions = {
            row[0] for row in migrated_connection.execute("SELECT version FROM schema_migrations")
        }
        assert 24 in versions and 25 in versions
        row = migrated_connection.execute(
            "SELECT review_decision_id, payload_json FROM trade_logs WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
    finally:
        migrated_connection.close()
        fresh_connection.close()

    assert row is not None
    assert row[0] is None
    payload = json.loads(row[1])
    for field in (
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
        "provenance",
    ):
        assert payload[field] == before[field]
    # The migrated trade is still readable through the normal journal read.
    shown = JournalService(store, FixedClock(NOW), settings).show(trade_id)
    assert shown["trade_id"] == trade_id
    assert shown["review_decision_id"] is None


def test_the_review_prompt_is_seeded_as_active_v1_for_both_templates(
    tmp_path: Path,
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()

    with sqlite3.connect(database_path(settings)) as connection:
        rows = _review_prompt_rows(connection)
        templates = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT prompt_id, active_version FROM prompt_templates "
                "WHERE prompt_id IN ('review', 'review:system')"
            )
        }

    assert templates == {"review": 1, "review:system": 1}
    assert [row[0] for row in rows] == ["review", "review:system"]
    for row in rows:
        _prompt_id, version, body, body_hash, required, status, active = row[:7]
        assert version == 1
        assert body
        assert body_hash
        assert required
        assert status == "ACTIVE"
        assert active == 1


def test_migrate_v25_rolls_back_the_column_when_seeding_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    database = database_path(settings)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX IF EXISTS trade_logs_review_decision")
        connection.execute("ALTER TABLE trade_logs DROP COLUMN review_decision_id")
        connection.execute(
            "DELETE FROM prompt_versions WHERE prompt_id IN ('review', 'review:system')"
        )
        connection.execute(
            "DELETE FROM prompt_templates WHERE prompt_id IN ('review', 'review:system')"
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 25")
        connection.commit()

    connection = sqlite3.connect(database)
    try:

        def _fail(_connection: sqlite3.Connection) -> None:
            raise RuntimeError("prompt seed failed")

        monkeypatch.setattr(sqlite_schema, "_seed_prompt_defaults", _fail)
        with pytest.raises(RuntimeError, match="prompt seed failed"):
            sqlite_schema._migrate_v25(connection)

        assert "review_decision_id" not in _columns(connection, "trade_logs")
        review_versions = connection.execute(
            "SELECT COUNT(*) FROM prompt_versions WHERE prompt_id IN ('review', 'review:system')"
        ).fetchone()[0]
        assert review_versions == 0
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        assert 25 not in versions
    finally:
        connection.close()
