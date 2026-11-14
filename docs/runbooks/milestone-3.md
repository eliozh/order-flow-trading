# Milestone 3 runbook — deterministic management/journal/review replay

This runbook reproduces the milestone-3 demo from a brand-new data directory.
Replay runs the same domain code as live — backfill → scan → trigger → LLM entry
re-check → paper fill → recorded MANAGEMENT decisions (PARTIAL_CLOSE, ADD, HOLD,
FULL_CLOSE) → recorded funding accrual → full close → trade log → journal
statistics → recorded REVIEW — against committed market fixtures and
**recorded** LLM outputs, with a fixed clock.

Everything below is offline. No Telegram token, no Binance credential, no live
LLM provider, and no network access is required: the default provider is the
offline fixture stub. Run/attempt stores and summaries are written only under
`$OFT_DATA_DIR/replay/...`; the explicit `--capture`/`--live-llm` recording path
additionally writes `<recordings_dir>/<ROLE>/<input_hash>.json` outside that
namespace by design. Replay never opens the live database, paper account,
orders, positions, trade logs, or budget ledger.

## Prerequisites

```bash
uv sync
```

## 1. Start from a clean data directory

```bash
export OFT_ENV=test
export OFT_DATA_DIR="$(mktemp -d)"
export OFT_PORT=7611

uv run oft serve --port "$OFT_PORT" &
DAEMON_PID=$!
trap 'kill "$DAEMON_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 100); do
  curl -fsS "http://127.0.0.1:$OFT_PORT/health" >/dev/null 2>&1 && break
  sleep 0.2
done
```

The daemon initializes `$OFT_DATA_DIR/oft.db` and the market layout. Every
later command is replay or diagnostics only.

## 2. Check the operator surface with `oft doctor`

```bash
uv run oft doctor
```

Expected section names and statuses (values vary):

```text
config              OK        settings loaded
storage.sqlite      OK        integrity ok
storage.parquet     OK        /tmp/<data-dir>/market
clock               OK        2026-...Z
market_data         OK        3 symbols
storage.disk        OK        ...
manifest.orphans    OK        0 manifest row(s) consistent
jobs.stale          OK        no stale RUNNING job runs
jobs.management     OK        no management job run recorded yet
funding.idempotency OK        0 funding accrual(s) unique and plausible
audit.chain         OK        AUDIT_CHAIN_OK (0 events)
config.provider     OK        no LLM provider configured (CALC-only)
budget.ledger       OK        0 budget record(s), 0 unpriced
triggers.stale      OK        no stale pending triggers (0 waiting)
kill_switch         OK        kill switch clear
config.secrets      OK        no secrets configured
ingest.checkpoint   OK        no ingestion checkpoint yet
ingest.ws           OK        no live WS state yet
daemon              OK        listening on 127.0.0.1:7611

STATUS  READY
```

`jobs.management` aggregates the latest `position_manage` window (one ledger row
per position via `latest_job_summary`) and degrades on `FAILED`, `PARTIAL`, or an
expired `RUNNING` lease; it reports `unknown` when the database cannot be read
and never echoes a persisted message. `funding.idempotency` verifies the recorded
accruals are unique per `(position, settlement_time)` and plausible. Neither
check prints a secret. With the daemon down the `daemon` check reports `OFFLINE`,
`STATUS` becomes `DEGRADED`, and the command still exits 0 (ADR-0003: diagnostics
are informational).

JSON mode keeps the standard envelope:

```bash
uv run oft doctor --json
```

## 3. Capture the recorded LLM outputs (attempt 1 only)

The first run calls the offline fixture stub and records its redacted outputs by
`(role, input_hash)` under a recordings directory you own:

```bash
export OFT_RECORDINGS="$OFT_DATA_DIR/recordings"
uv run oft replay run \
  --scenario position-management \
  --capture \
  --archive-fixture-dir tests/fixtures/archive \
  --recordings-dir "$OFT_RECORDINGS" \
  --run-id capture-management
```

Expected output shape:

```text
replay capture-management · scenario=position-management · deterministic=true
position  POS-...
trade     TRD-... · data_quality=COMPLETE
journal   stats=UNDEFINED
review    RVW-...
summary   /tmp/<data-dir>/replay/position-management/capture-management/summary.json
```

The capture run itself executes the scenario twice. Attempt 2 reads only the
recordings attempt 1 wrote, so the `deterministic=true` line already proves the
recordings are replayable.

Capture layout: the recording files live under `<recordings_dir>/<ROLE>/` for
the four roles `ANALYSIS`, `ENTRY_RECHECK`, `MANAGEMENT`, and `REVIEW`, each
named `<input_hash>.json`. A recording is redacted JSON carrying
`recording_format_version` and a `content_hash` of the recorded text.
Re-capturing identical content is an idempotent no-op (the file is not
rewritten); a different result for the same `(role, input_hash)` is refused
unless you explicitly pass `--force-recordings` to overwrite it deliberately.

The journal reports `stats=UNDEFINED` on purpose: this scenario performs a
partial close, and the documented eligibility rule excludes `PARTIAL` trades
from the metrics while counting them. Open/partial trades are never counted as
zero.

## 4. Replay the recorded scenario offline

The read-only replay never calls a provider. A missing recording for
`MANAGEMENT`, `REVIEW`, or `ENTRY_RECHECK` fails closed with a typed error naming
the role and input hash; a tampered or unreadable recording fails closed with a
typed error naming the file. An `ANALYSIS` recording failure is surfaced through the
scenario's "no OPEN opportunity" error on either scenario (a typed recording
error whose message names `role=ANALYSIS`), not as a per-trade error naming the
input hash:

```bash
uv run oft replay run \
  --scenario position-management \
  --archive-fixture-dir tests/fixtures/archive \
  --recordings-dir "$OFT_RECORDINGS" \
  --run-id demo-management
```

Repeat with a second run id. Each run writes its own isolated stores:

```bash
uv run oft replay run \
  --scenario position-management \
  --archive-fixture-dir tests/fixtures/archive \
  --recordings-dir "$OFT_RECORDINGS" \
  --run-id demo-management-2
```

## 5. Verify determinism

Both summaries contain only stable values; run-generated `scan_id`/`report_id`
values are projected out, so the documents are byte-identical:

```bash
jq -S '.outcome' \
  "$OFT_DATA_DIR/replay/position-management/demo-management/summary.json" \
  > /tmp/m3-a.json
jq -S '.outcome' \
  "$OFT_DATA_DIR/replay/position-management/demo-management-2/summary.json" \
  > /tmp/m3-b.json
diff -u /tmp/m3-a.json /tmp/m3-b.json && echo "DETERMINISTIC"
```

The comparison covers the management actions, funding accrual, closed position,
trade log, PnL terms, journal statistics, and the advisory review. A third run
with the same `--run-id` fails on purpose: replay never deletes or overwrites a
previous run.

## 6. The entry-only scenario still works

`--scenario entry` is the default and preserves the milestone-2 behaviour:

```bash
uv run oft replay run \
  --scenario entry \
  --archive-fixture-dir tests/fixtures/archive \
  --recordings-dir "$OFT_RECORDINGS" \
  --run-id demo-entry
```

Note: the entry-only recordings live under the same directory and are keyed by
the entry/re-check role and input hash; capturing either scenario does not
invalidate the other.

## Troubleshooting

- `no recorded LLM output for role=MANAGEMENT input_hash=...` — the recordings
  directory is incomplete. Re-run step 3 with `--capture` (offline) or
  `--live-llm` (explicit provider) and do not hand-edit a recording.
- `recorded LLM output was tampered with (content hash mismatch)` — delete the
  tampered file deliberately and re-capture; replay will not read it.
- `replay refuses environment 'live'` / `LIVE_DISABLED` — replay supports
  PAPER/TEST only. A live order path is never opened.
- `run '<id>' already exists` — pass a new `--run-id`; replay never resets a
  run directory.
- `run_id must contain at least one letter or digit` — `.`, `..`, and path-like
  ids are refused before any filesystem write.
- `the partial-close remainder is below the recorded step or minimum notional` —
  the recorded sizing escalated the partial close to a full close. This is not
  expected: escalation closes the whole position, so the next ADD aborts with
  `POSITION_STATE_INVALID`, the scenario fails, and no summary is written.
  Re-capture the recordings with a non-escalating partial-close fraction.
- `a different recorded LLM output already exists for role=... input_hash=...` —
  a re-capture produced different text for a published recording. Inspect it and,
  only if the change is intended, capture again with `--force-recordings`.

## What is intentionally out of scope

- No live Binance order placement, testnet, reconciliation, user-data stream,
  live arming, or leverage brackets.
- No Telegram delivery, receipts, allowlist, or budget pushes.
- No paper reset, live gate, rule auto-tuning, or prompt/knowledge editor.
- No news/social text, historical liquidations beyond the recorded fixture, or
  full L2/DOM data.
- No real-provider nightly baseline; `--live-llm` is an explicit, opt-in
  recording path and is never used by CI.
