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

# Slice 21: the deterministic core is listed explicitly so a module cannot drop
# out of the scan by moving packages. Silence is never an allow-list entry.
REQUIRED_DETERMINISTIC_MODULES = (
    "domain/risk.py",
    "domain/trigger.py",
    "domain/oms.py",
    "domain/pnl.py",
    "domain/positions.py",
    "domain/management.py",
    "domain/journal.py",
    "application/entry_service.py",
    "application/execution_gateway.py",
    "application/paper_broker.py",
    "application/budget_ledger.py",
    "application/position_service.py",
    "application/management_service.py",
    "application/management_add.py",
    "application/journal_service.py",
    "application/review_service.py",
    "replay/harness.py",
    "replay/llm_source.py",
    "replay/paper_loop.py",
    "replay/position_management.py",
)
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


def test_every_required_deterministic_module_is_scanned() -> None:
    """Risk, trigger, OMS, and the execution/LLM application services stay pure."""
    violations: list[str] = []
    for relative in REQUIRED_DETERMINISTIC_MODULES:
        path = SRC / relative
        assert path.is_file(), f"deterministic module is missing: {relative}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, expression in _wall_clock_calls(tree):
            violations.append(f"{path.relative_to(ROOT)}:{line}: {expression}")

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
        "jobs.management",
        "funding.idempotency",
        "audit.chain",
        "config.provider",
        "budget.ledger",
        "triggers.stale",
        "kill_switch",
    ):
        assert name in checks, name
    assert checks["storage.sqlite"].status == "ok"
    assert checks["clock"].status == "ok"
    assert checks["manifest.orphans"].status == "ok"
    assert checks["jobs.stale"].status == "ok"
    assert checks["jobs.management"].status == "ok"
    assert checks["funding.idempotency"].status == "ok"
    assert checks["audit.chain"].status == "ok"
    assert "AUDIT_CHAIN_OK" in checks["audit.chain"].detail
    assert checks["config.provider"].status == "ok"
    assert checks["budget.ledger"].status == "ok"
    assert checks["triggers.stale"].status == "ok"
    assert checks["kill_switch"].status == "ok"
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
        "jobs.management",
        "funding.idempotency",
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


# -- slice 21 doctor extensions --------------------------------------------


@pytest.mark.parametrize(
    ("provider", "api_key", "expected"),
    [
        (None, None, "ok"),
        ("stub", None, "ok"),
        ("openai", "sk-test", "ok"),
        ("openai", None, "degraded"),
    ],
)
def test_provider_check_never_echoes_a_key_and_fails_closed(
    tmp_path: Path, provider: str | None, api_key: str | None, expected: str
) -> None:
    from oft.application.health import _provider_check

    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        llm_provider=provider,
        openai_api_key=api_key,
    )

    check = _provider_check(settings)

    assert check.status == expected
    if api_key is not None:
        assert api_key not in check.detail
    if provider is not None:
        assert provider in check.detail


def test_budget_ledger_check_flags_an_invalid_cost() -> None:
    from types import SimpleNamespace

    from oft.application.health import _budget_ledger_check

    class FakeStore:
        def __init__(self, records: tuple[Any, ...]) -> None:
            self._records = records

        def list_budget_records(self) -> tuple[Any, ...]:
            return self._records

    assert _budget_ledger_check(FakeStore(())).status == "ok"
    assert (
        _budget_ledger_check(FakeStore((SimpleNamespace(cost_usd="0.10", unpriced=True),))).status
        == "ok"
    )
    assert (
        _budget_ledger_check(
            FakeStore((SimpleNamespace(cost_usd="not-a-number", unpriced=False),))
        ).status
        == "degraded"
    )
    assert (
        _budget_ledger_check(FakeStore((SimpleNamespace(cost_usd="-1", unpriced=False),))).status
        == "degraded"
    )


def test_stale_trigger_check_flags_old_pending_triggers() -> None:
    from types import SimpleNamespace

    from oft.application.health import _stale_triggers_check

    def record(trigger_id: str, triggered_at: str) -> SimpleNamespace:
        return SimpleNamespace(trigger_id=trigger_id, triggered_at=triggered_at)

    class FakeStore:
        def __init__(self, records: tuple[SimpleNamespace, ...]) -> None:
            self._records = records

        def list_trigger_events(
            self, *, status: str, limit: int, offset: int
        ) -> tuple[SimpleNamespace, ...]:
            if status != "PENDING_RECHECK" or offset:
                return ()
            return self._records[:limit]

    old = record("TRG-OLD", utc_timestamp(NOW - timedelta(hours=1)))
    fresh = record("TRG-NEW", utc_timestamp(NOW))

    stale = _stale_triggers_check(FakeStore((old,)), FixedClock(NOW), 60)  # type: ignore[arg-type]
    healthy = _stale_triggers_check(FakeStore((fresh,)), FixedClock(NOW), 60)  # type: ignore[arg-type]

    assert stale.status == "degraded"
    assert "1" in stale.detail
    assert healthy.status == "ok"
    assert "1 waiting" in healthy.detail


def test_stale_trigger_check_ignores_rechecked_triggers() -> None:
    from types import SimpleNamespace

    from oft.application.health import _stale_triggers_check

    queried: list[str] = []

    class FakeStore:
        def list_trigger_events(
            self, *, status: str, limit: int, offset: int
        ) -> tuple[SimpleNamespace, ...]:
            queried.append(status)
            if status == "RECHECKED" and not offset:
                # A finished re-check awaiting human action is not stale.
                return (
                    SimpleNamespace(
                        trigger_id="TRG-OLD",
                        triggered_at=utc_timestamp(NOW - timedelta(hours=1)),
                    ),
                )
            return ()

    check = _stale_triggers_check(FakeStore(), FixedClock(NOW), 60)  # type: ignore[arg-type]

    assert check.status == "ok"
    assert "RECHECKED" not in queried


def test_doctor_flags_an_active_kill_switch(tmp_path: Path) -> None:
    from oft.application.kill_switch import KillSwitch

    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    KillSwitch(settings=settings, store=store, clock=FixedClock(NOW)).activate(actor_id="test")

    report = _doctor(settings, store)
    checks = {check.name: check for check in report.checks}

    assert checks["kill_switch"].status == "degraded"
    assert "ACTIVE" in checks["kill_switch"].detail
    assert report.status == "DEGRADED"


def test_doctor_reports_an_unreadable_budget_ledger(tmp_path: Path) -> None:
    from oft.application.health import _budget_ledger_check

    class BrokenStore:
        def list_budget_records(self) -> tuple[Any, ...]:
            raise RuntimeError("ledger is corrupt")

    check = _budget_ledger_check(BrokenStore())  # type: ignore[arg-type]

    assert check.status == "degraded"
    assert "unreadable" in check.detail


# -- slice 31 doctor extensions --------------------------------------------


def _job_run(
    *,
    job_type: str,
    status: str,
    window_start: str = "2026-11-06T12:00:00.000Z",
    lease_expires_in: timedelta = timedelta(minutes=5),
    error_code: str | None = None,
    message: str | None = None,
) -> JobRunRecord:
    started = utc_timestamp(NOW - timedelta(minutes=1))
    return JobRunRecord(
        run_id=f"JOB-{job_type.upper()}-{status}",
        job_type=job_type,
        window_start=window_start,
        status=status,
        trigger_source="SCHEDULED",
        started_at=started,
        finished_at=None if status == "RUNNING" else utc_timestamp(NOW),
        error_code=error_code,
        message=message,
        lease_owner="test-owner",
        lease_expires_at=utc_timestamp(NOW + lease_expires_in),
        heartbeat_at=started,
    )


def test_management_job_check_reports_no_run_yet(tmp_path: Path) -> None:
    from oft.application.health import _management_jobs_check

    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()

    check = _management_jobs_check(store, FixedClock(NOW))

    assert check.status == "ok"
    assert "no management job run" in check.detail


def test_management_job_check_reads_the_position_manage_ledger(tmp_path: Path) -> None:
    from oft.application.health import _management_jobs_check
    from oft.application.jobs import POSITION_MANAGE_JOB_TYPE

    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    claimed, _, _ = store.claim_job_run(_job_run(job_type=POSITION_MANAGE_JOB_TYPE, status="DONE"))
    assert claimed

    check = _management_jobs_check(store, FixedClock(NOW))

    assert check.status == "ok"
    assert "DONE" in check.detail


def test_management_job_check_flags_a_failed_run_without_echoing_its_message(
    tmp_path: Path,
) -> None:
    from oft.application.health import _management_jobs_check
    from oft.application.jobs import POSITION_MANAGE_JOB_TYPE

    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    secret_message = "provider key sk-live-should-not-echo"
    claimed, _, _ = store.claim_job_run(
        _job_run(
            job_type=POSITION_MANAGE_JOB_TYPE,
            status="FAILED",
            error_code="LLM_PROVIDER_ERROR",
            message=secret_message,
        )
    )
    assert claimed

    check = _management_jobs_check(store, FixedClock(NOW))

    assert check.status == "degraded"
    assert "FAILED" in check.detail
    assert "LLM_PROVIDER_ERROR" in check.detail
    assert secret_message not in check.detail


def test_management_job_check_flags_an_expired_running_lease(tmp_path: Path) -> None:
    from oft.application.health import _management_jobs_check
    from oft.application.jobs import POSITION_MANAGE_JOB_TYPE

    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    claimed, _, _ = store.claim_job_run(
        _job_run(
            job_type=POSITION_MANAGE_JOB_TYPE,
            status="RUNNING",
            lease_expires_in=timedelta(minutes=-5),
        )
    )
    assert claimed

    check = _management_jobs_check(store, FixedClock(NOW))

    assert check.status == "degraded"
    assert "expired lease" in check.detail


def test_management_job_check_accepts_a_fresh_running_lease(tmp_path: Path) -> None:
    from oft.application.health import _management_jobs_check
    from oft.application.jobs import POSITION_MANAGE_JOB_TYPE

    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    claimed, _, _ = store.claim_job_run(
        _job_run(job_type=POSITION_MANAGE_JOB_TYPE, status="RUNNING")
    )
    assert claimed

    check = _management_jobs_check(store, FixedClock(NOW))

    assert check.status == "ok"
    assert "RUNNING" in check.detail


def _accrual(
    settlement_time: str = "2026-11-06T18:00:00.000Z",
    *,
    amount_usd: str = "-0.07",
    mark_price: str = "70100",
    size: str = "0.01",
) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        settlement_time=settlement_time,
        amount_usd=amount_usd,
        mark_price=mark_price,
        size=size,
    )


class _FundingStore:
    def __init__(
        self,
        accruals: dict[str, tuple[Any, ...]],
        *,
        states: dict[str, str] | None = None,
    ) -> None:
        self._accruals = accruals
        self._states = states or {position_id: "OPEN" for position_id in accruals}
        self.requested_states: list[str | None] = []

    def list_positions(
        self, *, state: str | None = None, limit: int | None = 100
    ) -> tuple[Any, ...]:
        from types import SimpleNamespace

        self.requested_states.append(state)
        return tuple(
            SimpleNamespace(position_id=position_id)
            for position_id in self._accruals
            if state is None or self._states[position_id] == state
        )

    def list_funding_accruals(self, position_id: str) -> tuple[Any, ...]:
        return self._accruals.get(position_id, ())


def test_funding_idempotency_accepts_unique_plausible_settlements() -> None:
    from oft.application.health import _funding_idempotency_check

    check = _funding_idempotency_check(
        _FundingStore(
            {
                "POS-1": (
                    _accrual("2026-11-06T18:00:00.000Z"),
                    _accrual("2026-11-07T02:00:00.000Z", amount_usd="0.05"),
                )
            }
        )  # type: ignore[arg-type]
    )

    assert check.status == "ok"
    assert "2 funding accrual(s)" in check.detail


def test_funding_idempotency_flags_a_duplicate_settlement() -> None:
    from oft.application.health import _funding_idempotency_check

    check = _funding_idempotency_check(
        _FundingStore({"POS-1": (_accrual(), _accrual())})  # type: ignore[arg-type]
    )

    assert check.status == "degraded"
    assert "1 duplicate settlement" in check.detail


def test_funding_idempotency_scans_only_open_positions() -> None:
    from oft.application.health import _funding_idempotency_check

    store = _FundingStore(
        {
            "POS-OPEN": (_accrual(), _accrual()),
            "POS-CLOSED": (_accrual(), _accrual()),
        },
        states={"POS-OPEN": "OPEN", "POS-CLOSED": "CLOSED"},
    )

    check = _funding_idempotency_check(store)  # type: ignore[arg-type]

    # Bounded scan: one query over OPEN positions, not every historical row.
    assert store.requested_states == ["OPEN"]
    assert check.status == "degraded"
    assert "1 duplicate settlement" in check.detail


def test_funding_idempotency_treats_closed_position_accruals_as_history() -> None:
    from oft.application.health import _funding_idempotency_check

    store = _FundingStore(
        {"POS-CLOSED": (_accrual(), _accrual())},
        states={"POS-CLOSED": "CLOSED"},
    )

    check = _funding_idempotency_check(store)  # type: ignore[arg-type]

    assert check.status == "ok"
    assert "0 funding accrual(s)" in check.detail


@pytest.mark.parametrize(
    "accrual",
    [
        pytest.param(_accrual(amount_usd="not-money"), id="amount"),
        pytest.param(_accrual(amount_usd="1e100"), id="amount-range"),
        pytest.param(_accrual(mark_price="0"), id="mark"),
        pytest.param(_accrual(mark_price="1e100"), id="mark-range"),
        pytest.param(_accrual(size="-1"), id="size"),
    ],
)
def test_funding_idempotency_flags_implausible_values(accrual: Any) -> None:
    from oft.application.health import _funding_idempotency_check

    check = _funding_idempotency_check(
        _FundingStore({"POS-1": (accrual,)})  # type: ignore[arg-type]
    )

    assert check.status == "degraded"
    assert "1 invalid amount" in check.detail


def test_funding_idempotency_reports_unreadable_stores() -> None:
    from oft.application.health import _funding_idempotency_check

    class BrokenPositions:
        def list_positions(self, **_kwargs: Any) -> tuple[Any, ...]:
            raise RuntimeError("positions are corrupt")

    class BrokenAccruals(_FundingStore):
        def list_funding_accruals(self, position_id: str) -> tuple[Any, ...]:
            raise RuntimeError("accruals are corrupt")

    assert (
        _funding_idempotency_check(BrokenPositions()).status  # type: ignore[arg-type]
        == "degraded"
    )
    assert (
        _funding_idempotency_check(
            BrokenAccruals({"POS-1": ()})  # type: ignore[arg-type]
        ).status
        == "degraded"
    )


def test_management_job_check_reports_an_unreadable_ledger() -> None:
    from oft.application.health import _management_jobs_check

    class BrokenStore:
        def latest_job_run(self, job_type: str) -> Any:
            raise RuntimeError("job ledger is corrupt")

    check = _management_jobs_check(BrokenStore(), FixedClock(NOW))  # type: ignore[arg-type]

    assert check.status == "degraded"
    assert "unreadable" in check.detail
