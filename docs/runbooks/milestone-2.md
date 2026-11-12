# Milestone 2 runbook — deterministic paper-loop replay

This runbook reproduces the milestone-2 paper demo from a brand-new data
directory. Replay runs the same domain code as live (backfill → scan → trigger →
LLM entry re-check → risk gate → paper fill) against committed market fixtures
and **recorded** LLM outputs, with a fixed clock. It needs no Telegram token, no
Binance credential, and no live LLM provider: the default provider is the
offline fixture stub.

Every command below is offline. Replay writes only
`$OFT_DATA_DIR/replay/...`; it never opens the live database, paper account,
orders, positions, or budget ledger.

## Prerequisites

```bash
uv sync
```

## 1. Start from a clean data directory

```bash
export OFT_ENV=test
export OFT_DATA_DIR="$(mktemp -d)"
export OFT_PORT=7611
export OFT_LLM_PROVIDER=stub
export OFT_ARCHIVE_FIXTURE_DIR=tests/fixtures/archive

uv run oft serve --port "$OFT_PORT" &
DAEMON_PID=$!
trap 'kill "$DAEMON_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 100); do
  curl -fsS "http://127.0.0.1:$OFT_PORT/health" >/dev/null 2>&1 && break
  sleep 0.2
done
```

The daemon initializes `$OFT_DATA_DIR/oft.db` and the market layout. Everything
else in this runbook is replay and diagnostic only.

## 2. Check the operator surface with `oft doctor`

```bash
uv run oft doctor
```

Expected section names and statuses (values vary):

```text
config             OK        settings loaded
storage.sqlite     OK        integrity ok
storage.parquet    OK        /tmp/<data-dir>/market
clock              OK        2026-...Z
market_data        OK        3 symbols
storage.disk       OK        ...
manifest.orphans   OK        0 manifest row(s) consistent
jobs.stale         OK        no stale RUNNING job runs
audit.chain        OK        AUDIT_CHAIN_OK (0 events)
config.provider    OK        provider=stub (deterministic fixture provider)
budget.ledger      OK        0 budget record(s), 0 unpriced
triggers.stale     OK        no stale pending triggers (0 waiting)
kill_switch        OK        kill switch clear
config.secrets     OK        no secrets configured
ingest.checkpoint  OK        no ingestion checkpoint yet
ingest.ws          OK        no live WS state yet
daemon             OK        listening on 127.0.0.1:7611

STATUS  READY
```

`doctor` verifies the append-only audit hash chain, the provider configuration,
the budget ledger, stale pending triggers, the kill switch, SQLite integrity,
and disk health. It never prints a secret value. With the daemon down the
`daemon` check reports `OFFLINE`, `STATUS` becomes `DEGRADED`, and the command
still exits 0 (ADR-0003: diagnostics are informational).

JSON mode keeps the standard envelope:

```bash
uv run oft doctor --json
```

## 3. Capture the recorded LLM outputs (offline stub)

The capture step calls the fixture provider and writes redacted recordings keyed
by `(role, input_hash)` under the replay namespace. No network is used.

```bash
uv run oft replay run \
  --symbol BTCUSDT --period 4h \
  --start 2026-10-01 --end 2026-10-07 \
  --archive-fixture-dir tests/fixtures/archive \
  --recordings-dir "$OFT_DATA_DIR/replay/recordings" \
  --capture \
  --run-id capture \
  --now 2026-11-06T12:00:00Z \
  --json
```

Expected `data` fields (ids are deterministic for this fixture):

```text
deterministic: true
decision_id:   DEC-ENTRY-<16-hex>
order_id:      ORD-<16-hex>
fill_id:       FIL-<16-hex>
position_id:   POS-<16-hex>
entry_price:   <paper fill price>
summary_path:  /tmp/<data-dir>/replay/paper-loop/capture/summary.json
```

Each recording contains `role`, `input_hash`, `provider`, `model`, `raw_text`,
`content_hash`, and `redacted`; secrets are replaced with `[REDACTED]` before the
file is written. The provider/model recorded here (`stub`/`fixture`) is preserved
in the replay provenance — replay invents no provider.

## 4. Replay the recorded loop deterministically

```bash
uv run oft replay run \
  --symbol BTCUSDT --period 4h \
  --start 2026-10-01 --end 2026-10-07 \
  --archive-fixture-dir tests/fixtures/archive \
  --recordings-dir "$OFT_DATA_DIR/replay/recordings" \
  --run-id demo-a \
  --now 2026-11-06T12:00:00Z \
  --json
```

The command runs the whole loop twice in two fresh stores under
`$OFT_DATA_DIR/replay/paper-loop/demo-a/` and compares decisions, triggers,
orders, fills, positions, entry prices, fees, position values, and the account.
Any difference raises `ReplayNonDeterministicError` and exits non-zero.

To prove a fresh machine reproduces the run, replay the identical scenario with
the **same** run id in a second clean data directory and compare the `outcome`
blocks. The run id stays fixed because it is part of the deterministic identity:
it seeds the paper order's idempotency key and approval id. A different run id
is a different replay namespace by design, so its ids differ.

A run id is used at most once per data directory. Replay never deletes,
resets, or overwrites an existing run: a second invocation with the same
`--run-id` fails before writing with `run '<id>' already exists ... pass a new
--run-id`. Pass `--run-id demo-b` (or delete the whole data directory yourself)
to run again. Run ids must contain at least one letter or digit and match
`^[A-Za-z0-9._-]{1,64}$`; `.` and `..` are refused, and the resolved run root
must stay under `$OFT_DATA_DIR/replay/paper-loop/` (symlinked components are
rejected).

```bash
export OFT_DATA_DIR_2="$(mktemp -d)"
mkdir -p "$OFT_DATA_DIR_2/replay"
cp -r "$OFT_DATA_DIR/replay/recordings" "$OFT_DATA_DIR_2/replay/recordings"

OFT_DATA_DIR="$OFT_DATA_DIR_2" uv run oft replay run \
  --symbol BTCUSDT --period 4h \
  --start 2026-10-01 --end 2026-10-07 \
  --archive-fixture-dir tests/fixtures/archive \
  --recordings-dir "$OFT_DATA_DIR_2/replay/recordings" \
  --run-id demo-a \
  --now 2026-11-06T12:00:00Z \
  --json

uv run python - <<'PY'
import json, os
a = json.load(open(os.path.join(
    os.environ["OFT_DATA_DIR"], "replay", "paper-loop", "demo-a", "summary.json")))["outcome"]
b = json.load(open(os.path.join(
    os.environ["OFT_DATA_DIR_2"], "replay", "paper-loop", "demo-a", "summary.json")))["outcome"]
assert a == b, "replay is not deterministic"
print("identical outcome:", a["decision_id"], a["order"]["order_id"])
PY
```

Expected: `identical outcome: DEC-ENTRY-<16-hex> ORD-<16-hex>`.

## 5. Prove the live path is refused

```bash
uv run oft replay run \
  --archive-fixture-dir tests/fixtures/archive \
  --recordings-dir "$OFT_DATA_DIR/replay/recordings" \
  --environment live
echo "exit=$?"
```

Expected: a clear `replay refuses environment 'live'` message and a non-zero
exit (2). Replay never opens a live order path.

## 6. Prove a missing recording fails clearly

```bash
uv run oft replay run \
  --archive-fixture-dir tests/fixtures/archive \
  --recordings-dir "$OFT_DATA_DIR/replay/empty-recordings" \
  --run-id missing
echo "exit=$?"
```

Expected: `no recorded LLM output ... record one under ... or pass --live-llm`
and a non-zero exit. Replay never synthesises an LLM output. `--live-llm`
explicitly calls the configured provider (the stub above) and records the result
before use.

## 7. Confirm replay left live state untouched

```bash
uv run python - <<'PY'
import os
from oft.adapters.sqlite_store import SqliteStore
from oft.config import load_settings

store = SqliteStore(load_settings())
print("orders     :", store.list_orders())
print("fills      :", store.list_fills())
print("positions  :", store.list_positions())
print("budget rows:", store.list_budget_records())
PY
```

Expected: four empty tuples. Every replay file lives under
`$OFT_DATA_DIR/replay/`; the live `oft.db`, paper account, OMS ledger, and budget
ledger are unchanged.

## 8. Stop the daemon

```bash
kill "$DAEMON_PID"
```

Confirm no listener remains:

```bash
curl -fsS "http://127.0.0.1:$OFT_PORT/health" || echo "daemon stopped"
```

## Offline CI gate

```bash
make ci
```

Runs, with no network or daemon: `ruff` check/format, `mypy`, `import-linter`
(including the LLM-adapter ⇸ execution/Binance contract), fixture checksums,
`pytest` unit/integration/contract/replay with the 80% overall coverage floor
and the 100% metric branch gate, then web lint, typecheck, test, and production
build. The 100% branch gate is enforced by `scripts/check-metric-coverage` for
the metric core **and** `domain/risk.py`, `domain/trigger.py`, and
`domain/oms.py`, so a partial branch in those state machines fails `make
coverage`.

## Troubleshooting

- **`run '<id>' already exists`**: replay never deletes or resets a run
  directory. Re-run with a new `--run-id`, or remove the whole data directory
  yourself if you intend a clean start.
- **`ReplayNonDeterministicError`**: two identical attempts differed. A new
  provider, prompt, knowledge artifact, or metric version entered the loop
  without being pinned; investigate rather than editing a published replay.
- **`no recorded LLM output`**: the prompt input hash changed (for example a
  knowledge artifact or prompt version changed). Re-capture with `--capture`
  (offline stub) or `--live-llm` (explicit provider), review the redacted diff,
  then replay again.
- **`doctor` says `config.provider` DEGRADED**: a provider name is configured
  without its API key. Replay still uses `stub`/recorded mode.
- **`doctor` says `triggers.stale` DEGRADED**: pending triggers are older than
  `OFT_TRIGGER_STALE_AFTER_SECONDS`. Re-check or reject them.
- **`doctor` says `kill_switch` DEGRADED**: the kill switch is active and blocks
  new paper orders. Resume with the typed `RESUME` token when appropriate.
