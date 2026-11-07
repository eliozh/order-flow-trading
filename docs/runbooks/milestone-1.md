# Milestone 1 runbook — clean-data-dir demo

This runbook reproduces the milestone-1 path from a brand-new data directory:
initialize the daemon, run `oft doctor`, run a deterministic fixture scan, and
replay the same fixture window. Every command is offline: no exchange, LLM,
Telegram, or package-manager network access is used.

## Prerequisites

```bash
uv sync
npm --prefix web install
```

## 1. Start from a clean data directory

```bash
export OFT_ENV=test
export OFT_ALLOW_FIXTURE_SCAN_DEBUG=true
export OFT_DATA_DIR="$(mktemp -d)"
export OFT_PORT=7611

uv run oft serve --port "$OFT_PORT" &
DAEMON_PID=$!
trap 'kill "$DAEMON_PID" 2>/dev/null || true' EXIT

# Wait for readiness (the daemon creates $OFT_DATA_DIR/oft.db on startup).
for _ in $(seq 1 100); do
  curl -fsS "http://127.0.0.1:$OFT_PORT/health" >/dev/null 2>&1 && break
  sleep 0.2
done
```

Expected: `curl` exits 0 and `$OFT_DATA_DIR/oft.db` exists.

## 2. Check the daemon with `oft doctor`

```bash
uv run oft doctor
```

Expected output shape (values vary; the section names and statuses do not):

```text
oft doctor · 0.1.0 · 2026-11-07T15:50:10.377Z

config             OK        settings loaded
storage.sqlite     OK        integrity ok
storage.parquet    OK        /tmp/<data-dir>/market
clock              OK        2026-11-07T15:50:10.377Z
market_data        OK        3 symbols
storage.disk       OK        15.3 GiB free (1% used)
manifest.orphans   OK        0 manifest row(s) consistent
jobs.stale         OK        no stale RUNNING job runs
audit.chain        UNKNOWN   audit_events hash chain is not implemented in milestone 1
ingest.checkpoint  OK        no ingestion checkpoint yet
ingest.ws          OK        no live WS state yet
daemon             OK        listening on 127.0.0.1:7611

STATUS  READY
```

With the daemon down, the `daemon` check reports `OFFLINE`, `STATUS` becomes
`DEGRADED`, and the command still exits 0 (ADR-0003: diagnostics are
informational). Doctor never prints a secret value.

JSON mode prints the same checks under `data.checks`:

```bash
uv run oft doctor --json
```

## 3. Run the deterministic fixture scan

```bash
uv run oft scan run \
  --fixture tests/fixtures/market/btcusdt_agg-trades_sample.csv \
  --symbol BTCUSDT --period 4h --metrics-only --json
```

Expected `data` fields (the golden fixture values):

| Field | Value |
|---|---|
| `symbol` | `BTCUSDT` |
| `period` | `4h` |
| `trade_count` | `6` |
| `buy_volume` | `4.25` |
| `sell_volume` | `2.75` |
| `delta` | `1.50` |
| `cvd` | `1.50` |
| `data_quality` | `COMPLETE` |
| `scan_id` | `SCAN-YYYYMMDD-XXXXXXXX` |

`uv run oft status --json` reports the fixture universe and the latest scan.

## 4. Replay the same window deterministically

Replay uses the same pure metric functions as live but writes only under
`$OFT_DATA_DIR/replay`; it never opens `oft.db`.

```bash
uv run python - <<'PY'
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import os

from oft.replay.event_source import FixtureEventSource, ReplayWindow
from oft.replay.harness import ReplayConfig, ReplayHarness

window = ReplayWindow(
    symbol="BTCUSDT",
    start=datetime(2025, 11, 5, tzinfo=UTC),
    end=datetime(2025, 11, 5, 4, tzinfo=UTC),
)
source = FixtureEventSource.from_csv(
    Path("tests/fixtures/market/btcusdt_agg-trades_sample.csv"), window=window
)
harness = ReplayHarness(
    data_dir=Path(os.environ["OFT_DATA_DIR"]),
    source=source,
    config=ReplayConfig(
        symbol=window.symbol,
        period="4h",
        window_start=window.start,
        window_end=window.end,
        tick_size=Decimal("0.10"),
    ),
)
result = harness.run()
print("metric_version:", result.metric_version)
print("metric_hash:   ", result.metric_hash)
print("output:        ", result.output_path)
PY
```

Expected output shape (the hashes are stable for this fixture and version):

```text
metric_version: orderflow_metrics.v1.<16-hex>
metric_hash:    <64-hex>
output:         /tmp/<data-dir>/replay/BTCUSDT/4h/orderflow_metrics.v1.<16-hex>/<64-hex>.json
```

Run the block twice: the second run prints the same `metric_hash`, the same
output path, and does not create a second file (`idempotent=True`).

To demonstrate immutable metric versioning, change `cvd_algorithm_version=2`
in `ReplayConfig` and run again: a new directory and file appear; the first
document is byte-for-byte unchanged. A tampered existing document is rejected
with `ReplayNonDeterministicError` rather than overwritten.

## 5. Stop the daemon

The `trap` in step 1 stops the daemon on exit; to stop explicitly:

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

Runs, with no network or daemon: `ruff` check/format, `mypy`, `import-linter`,
committed-fixture checksums, `pytest` unit/integration/contract/replay with an
80% overall coverage floor and the 100% metric-primitive branch gate, then web
lint, typecheck, test, and production build. `make e2e` runs the Playwright
journeys against temporary loopback daemons.

## Troubleshooting

- **`doctor` says `storage.sqlite` DEGRADED**: start the daemon once so it
  creates `oft.db`, or check `$OFT_DATA_DIR` permissions (0700 expected).
- **`scan run` returns `DAEMON_NOT_RUNNING`**: the CLI is an API client; start
  `uv run oft serve` first.
- **Replay raises `EventSourceError`**: the CSV has an out-of-window row, a
  duplicate `agg_trade_id`, or an unknown column. Replay fails closed instead
  of guessing.
- **Replay raises `ReplayNonDeterministicError`**: the metric computation
  changed between two identical runs, or an existing file has a different
  metric hash. Investigate the algorithm; never edit a published replay.
