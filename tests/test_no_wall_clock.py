"""Deterministic time discipline and doctor operability.

* No wall-clock call may appear in ``oft.domain`` or ``oft.application``;
  time is injected through ``ClockPort`` (ADR-0011).
* ``oft doctor`` reports DB integrity, disk, clock, manifest orphans, stale
  job runs, and a verified audit-chain check, and never echoes a secret.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from oft.adapters.fixture_market_data import FixtureMarketData
from oft.adapters.parquet_store import ParquetStore
from oft.adapters.sqlite_store import SqliteStore
from oft.application.health import DaemonProbe, build_doctor
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.ports.store import JobRunRecord, ManifestEntry

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "oft"
DETERMINISTIC_PACKAGES = ("domain", "application", "replay")
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)

_WALL_CLOCK_MODULES = {"datetime", "date", "time"}
_WALL_CLOCK_ATTRIBUTES = {
    "now",
    "utcnow",
    "today",
    "time",
    "time_ns",
    "monotonic",
    "monotonic_ns",
    "perf_counter",
    "perf_counter_ns",
    "clock_gettime",
    "clock_gettime_ns",
}


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _assignments(tree: ast.AST, aliases: dict[str, str]) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for _ in range(3):
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            raw = _dotted(value) if value is not None else None
            if not raw:
                continue
            first, _, rest = raw.partition(".")
            resolved = aliases.get(first, bindings.get(first, first)) + (f".{rest}" if rest else "")
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = resolved
    return bindings


def _resolve(raw: str, aliases: dict[str, str], bindings: dict[str, str]) -> str:
    first, _, rest = raw.partition(".")
    base = aliases.get(first, bindings.get(first, first))
    return base + (f".{rest}" if rest else "")


def _wall_clock_calls(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(line, expression)`` for every wall-clock call in a module."""
    aliases = _import_aliases(tree)
    bindings = _assignments(tree, aliases)
    violations: list[tuple[int, str]] = []
    call_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    called_attributes = {
        id(node.func) for node in call_nodes if isinstance(node.func, ast.Attribute)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and id(node) not in called_attributes:
            raw = _dotted(node)
            if not raw:
                continue
            resolved = _resolve(raw, aliases, bindings)
            parts = resolved.split(".")
            attr = parts[-1]
            if attr in _WALL_CLOCK_ATTRIBUTES and any(
                module in _WALL_CLOCK_MODULES for module in parts[:-1]
            ):
                violations.append((node.lineno, f"{attr} (attribute)"))
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "getattr":
            if len(node.args) >= 2:
                obj = _dotted(node.args[0])
                attr_node = node.args[1]
                if obj and isinstance(attr_node, ast.Constant) and isinstance(attr_node.value, str):
                    resolved = _resolve(obj, aliases, bindings)
                    parts = resolved.split(".")
                    if attr_node.value in _WALL_CLOCK_ATTRIBUTES and any(
                        module in _WALL_CLOCK_MODULES for module in parts
                    ):
                        violations.append((node.lineno, f"getattr({attr_node.value})"))
            continue
        raw = _dotted(node.func)
        if not raw:
            continue
        resolved = _resolve(raw, aliases, bindings)
        parts = resolved.split(".")
        attr = parts[-1]
        if attr not in _WALL_CLOCK_ATTRIBUTES:
            continue
        if attr in {"time", "time_ns"} and not resolved.startswith("time."):
            # ``datetime.min.time()`` is a constructor accessor, not a clock.
            continue
        if any(module in _WALL_CLOCK_MODULES for module in parts[:-1]):
            violations.append((node.lineno, f"{attr}()"))
    return violations


def test_wall_clock_detector_recognizes_direct_calls() -> None:
    tree = ast.parse(
        "from datetime import datetime, UTC\n"
        "from time import monotonic\n"
        "datetime.now(UTC)\n"
        "monotonic()\n"
    )

    found = {expression for _, expression in _wall_clock_calls(tree)}
    assert found == {"now()", "monotonic()"}


def test_wall_clock_detector_recognizes_uncalled_attributes_and_clock_gettime() -> None:
    tree = ast.parse(
        "from datetime import datetime\n"
        "import time\n"
        "factory = datetime.now\n"
        "time.clock_gettime(time.CLOCK_MONOTONIC)\n"
    )

    found = {(line, expression) for line, expression in _wall_clock_calls(tree)}
    assert (3, "now (attribute)") in found
    assert any(expression == "clock_gettime()" for _, expression in found)


def test_wall_clock_detector_recognizes_import_aliases_and_chains() -> None:
    tree = ast.parse(
        "import datetime as dt\n"
        "import time as t\n"
        "from datetime import date as d\n"
        "dt.datetime.utcnow()\n"
        "dt.datetime.now(dt.UTC)\n"
        "t.time()\n"
        "t.monotonic()\n"
        "d.today()\n"
        "datetime.datetime.now()\n"
    )

    found = _wall_clock_calls(tree)
    assert {expression for _, expression in found} == {
        "utcnow()",
        "now()",
        "time()",
        "monotonic()",
        "today()",
    }


def test_domain_and_application_never_call_the_wall_clock() -> None:
    violations: list[str] = []
    checked = 0
    for package in DETERMINISTIC_PACKAGES:
        for path in sorted((SRC / package).rglob("*.py")):
            checked += 1
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for line, expression in _wall_clock_calls(tree):
                violations.append(f"{path.relative_to(ROOT)}:{line}: {expression}")

    assert checked > 0
    assert not violations, "wall-clock calls must move behind ClockPort:\n" + "\n".join(violations)


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _doctor(settings: Settings, store: SqliteStore) -> Any:
    return build_doctor(
        settings,
        store,
        FixtureMarketData(),
        FixedClock(NOW),
        daemon=DaemonProbe(state="UP", detail="test listener"),
    )


def test_doctor_reports_every_required_section(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    ParquetStore(settings).ensure_layout()

    report = _doctor(settings, store)
    checks = {check.name: check for check in report.checks}

    for name in (
        "storage.sqlite",  # DB integrity
        "storage.disk",
        "clock",
        "manifest.orphans",
        "jobs.stale",
        "audit.chain",
    ):
        assert name in checks, name
    assert checks["storage.sqlite"].status == "ok"
    assert checks["clock"].status == "ok"
    assert checks["manifest.orphans"].status == "ok"
    assert checks["jobs.stale"].status == "ok"
    assert checks["audit.chain"].status == "ok"
    assert "AUDIT_CHAIN_OK" in checks["audit.chain"].detail
    assert report.status == "READY"


def test_doctor_flags_manifest_orphans(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    missing = settings.data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT"
    store.record_manifest(
        ManifestEntry(
            path=str(missing / "part-0.parquet"),
            data_type="agg_trades",
            symbol="BTCUSDT",
            date="2025-11-05",
            rows=1,
            bytes=1,
            checksum="0" * 64,
            source="test",
            created_at=utc_timestamp(NOW),
        )
    )

    report = _doctor(settings, store)
    checks = {check.name: check for check in report.checks}

    assert checks["manifest.orphans"].status == "degraded"
    assert "1" in checks["manifest.orphans"].detail
    assert report.status == "DEGRADED"


def test_doctor_flags_stale_running_job_runs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    started = utc_timestamp(NOW - timedelta(hours=1))
    claimed, _, _ = store.claim_job_run(
        JobRunRecord(
            run_id="JOB-SCAN-STALE",
            job_type="scan",
            window_start="2026-11-06T08:00:00.000Z",
            status="RUNNING",
            trigger_source="SCHEDULED",
            started_at=started,
            finished_at=None,
            error_code=None,
            message=None,
            lease_owner="test-owner",
            lease_expires_at=utc_timestamp(NOW - timedelta(minutes=5)),
            heartbeat_at=started,
        )
    )
    assert claimed

    report = _doctor(settings, store)
    checks = {check.name: check for check in report.checks}

    assert checks["jobs.stale"].status == "degraded"
    assert "1" in checks["jobs.stale"].detail
    assert report.status == "DEGRADED"


def test_cli_doctor_never_echoes_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from oft.cli.main import app as cli_app

    secret = "sk-live-super-secret-value"
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("OFT_BINANCE_API_KEY", secret)
    monkeypatch.setenv("OFT_OPENAI_API_KEY", secret)

    result = CliRunner().invoke(cli_app, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert secret not in result.output
    payload = json.loads(result.stdout)
    names = {check["name"] for check in payload["data"]["checks"]}
    assert {
        "storage.sqlite",
        "storage.disk",
        "clock",
        "manifest.orphans",
        "jobs.stale",
        "audit.chain",
    } <= names


def test_manifest_scan_roots_include_bars_and_resolve_paths(tmp_path: Path) -> None:
    from oft.application.health import MANIFEST_SCAN_ROOTS

    assert "bars" in MANIFEST_SCAN_ROOTS
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    bars_dir = settings.data_dir / "bars" / "symbol=BTCUSDT"
    bars_dir.mkdir(parents=True)
    part = bars_dir / "part-0.parquet"
    part.write_bytes(b"not-read")
    # A relative manifest path must resolve against data_dir, not the CWD.
    store.record_manifest(
        ManifestEntry(
            path=str(part.relative_to(settings.data_dir)),
            data_type="bars",
            symbol="BTCUSDT",
            date="2026-10-01",
            rows=1,
            bytes=1,
            checksum="0" * 64,
            source="test",
            created_at=utc_timestamp(NOW),
        )
    )
    report = _doctor(settings, store)
    check = {item.name: item for item in report.checks}["manifest.orphans"]
    assert check.status == "ok"


def test_stale_job_check_reads_all_matches_not_only_the_newest_page(tmp_path: Path) -> None:
    from oft.application.health import _stale_jobs_check

    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    for index in range(205):
        claimed, _, _ = store.claim_job_run(
            JobRunRecord(
                run_id=f"JOB-{index}",
                job_type="scan",
                window_start=f"2026-11-06T08:{index:02d}:00.000Z",
                status="RUNNING",
                trigger_source="SCHEDULED",
                started_at=utc_timestamp(NOW - timedelta(hours=1)),
                finished_at=None,
                error_code=None,
                message=None,
                lease_owner="owner",
                lease_expires_at=utc_timestamp(NOW - timedelta(minutes=5)),
                heartbeat_at=utc_timestamp(NOW - timedelta(hours=1)),
            )
        )
        assert claimed
    check = _stale_jobs_check(store, FixedClock(NOW))
    assert check.status == "degraded"
    assert "205" in check.detail


def test_disk_check_uses_injected_disk_usage(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from oft.application.health import MIN_FREE_DISK_BYTES, _disk_check

    settings = _settings(tmp_path)
    healthy = SimpleNamespace(free=MIN_FREE_DISK_BYTES + 1, used=1, total=2 * MIN_FREE_DISK_BYTES)
    low = SimpleNamespace(free=0, used=10, total=10)
    assert _disk_check(settings, disk_usage=lambda _path: healthy).status == "ok"
    assert _disk_check(settings, disk_usage=lambda _path: low).status == "degraded"


def test_wall_clock_detector_tracks_rebinding_and_getattr() -> None:
    tree = ast.parse(
        "import datetime\n"
        "import time\n"
        "clock = datetime\n"
        "clock.now()\n"
        "clock2 = datetime.datetime\n"
        "clock2.utcnow()\n"
        "factory = time.time\n"
        "factory()\n"
        "getattr(datetime.datetime, 'now')\n"
        "getattr(clock, 'today')()\n"
    )

    found = {expression for _, expression in _wall_clock_calls(tree)}
    assert {"now()", "utcnow()", "time()", "getattr(now)", "getattr(today)"} <= found
