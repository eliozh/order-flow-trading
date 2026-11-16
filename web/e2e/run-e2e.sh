#!/usr/bin/env bash
# Run the slice-01 /system journey, the slice-02 read-only symbol journeys, the
# slice-08 report journeys, the slice-29 positions journey, and the slice-30
# journal journeys against real daemons.
#
# Starts `oft serve` three times with temporary OFT_DATA_DIRs:
#   - 127.0.0.1:7622 for the walking-skeleton /system journey;
#   - 127.0.0.1:7632 for the symbol registry, seeded from the committed
#     exchangeInfo fixture by the daemon bootstrap;
#   - 127.0.0.1:7642 for reports/positions/journal, seeded with a backfill,
#     reports, one OPEN paper position, and one CLOSED paper trade created
#     through evaluate -> recheck -> accept -> stop-mark -> monitor.
# The fixture market scan is applied to the first daemon, then the daemons are
# driven by Playwright. Every daemon is stopped before this script exits.
#
# The positions journey performs one real paper management POST; the journal
# journey performs one real advisory review POST (with CSRF + Idempotency-Key).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT="$(cd "$WEB_DIR/.." && pwd)"

SYSTEM_PORT="${OFT_E2E_PORT:-7622}"
SYMBOLS_PORT="${OFT_E2E_SYMBOLS_PORT:-7632}"
REPORTS_PORT="${OFT_E2E_REPORTS_PORT:-7642}"
SYSTEM_BASE_URL="http://127.0.0.1:${SYSTEM_PORT}"
SYMBOLS_BASE_URL="http://127.0.0.1:${SYMBOLS_PORT}"
REPORTS_BASE_URL="http://127.0.0.1:${REPORTS_PORT}"

SYSTEM_SCREENS_DIR="$ROOT/.claude/ecc-workflow/screens/e2e"
SYMBOLS_SCREENS_DIR="$ROOT/.claude/ecc-workflow/screens/e2e02"
REPORTS_SCREENS_DIR="$ROOT/.claude/ecc-workflow/screens/e2e08"
JOURNAL_SCREENS_DIR="$ROOT/.claude/ecc-workflow/screens/slice-30"
CHART_SCREENS_DIR="$ROOT/.claude/ecc-workflow/screens/e2e40"
OPPORTUNITIES_SCREENS_DIR="$ROOT/.claude/ecc-workflow/screens/e2e41"
MARKET_FIXTURE="$ROOT/tests/fixtures/market/btcusdt_agg-trades_sample.csv"
EXCHANGE_INFO_FIXTURE="$ROOT/tests/fixtures/binance/exchange_info_sample.json"
ARCHIVE_FIXTURE_DIR="$ROOT/tests/fixtures/archive"

SYSTEM_DATA_DIR="$(mktemp -d -t oft-e2e-system-XXXXXX)"
SYMBOLS_DATA_DIR="$(mktemp -d -t oft-e2e-symbols-XXXXXX)"
REPORTS_DATA_DIR="$(mktemp -d -t oft-e2e-reports-XXXXXX)"
# Slice 12: a writable copy of the deterministic LLM fixtures. The ANALYSIS
# fixture is flipped to a schema-invalid payload later so the batch DEGRADED
# path is exercised by a real daemon, never by a live provider.
LLM_FIXTURES_DIR="$(mktemp -d -t oft-e2e-llm-XXXXXX)"
cp "$ROOT"/tests/fixtures/llm/*.json "$LLM_FIXTURES_DIR"/
SYSTEM_LOG="$SYSTEM_DATA_DIR/daemon.log"
SYMBOLS_LOG="$SYMBOLS_DATA_DIR/daemon.log"
REPORTS_LOG="$REPORTS_DATA_DIR/daemon.log"

# The system daemon inherits these exported settings (CLI scan calls use them).
export OFT_ENV="test"
export OFT_ALLOW_FIXTURE_SCAN_DEBUG="true"
export OFT_DATA_DIR="$SYSTEM_DATA_DIR"
export OFT_HOST="127.0.0.1"
export OFT_PORT="$SYSTEM_PORT"
export OFT_LOG_LEVEL="WARNING"

cd "$ROOT"
setsid uv run oft serve --port "$SYSTEM_PORT" >"$SYSTEM_LOG" 2>&1 &
DAEMON_PID=$!

# The symbol daemon gets its own data dir and the explicit exchangeInfo fixture.
OFT_DATA_DIR="$SYMBOLS_DATA_DIR" \
  OFT_HOST="127.0.0.1" \
  OFT_PORT="$SYMBOLS_PORT" \
  OFT_LOG_LEVEL="WARNING" \
  OFT_EXCHANGE_INFO_FIXTURE="$EXCHANGE_INFO_FIXTURE" \
  setsid uv run oft serve --port "$SYMBOLS_PORT" >"$SYMBOLS_LOG" 2>&1 &
SYMBOLS_PID=$!

OFT_DATA_DIR="$REPORTS_DATA_DIR" \
  OFT_HOST="127.0.0.1" \
  OFT_PORT="$REPORTS_PORT" \
  OFT_LOG_LEVEL="WARNING" \
  OFT_EXCHANGE_INFO_FIXTURE="$EXCHANGE_INFO_FIXTURE" \
  OFT_ARCHIVE_FIXTURE_DIR="$ARCHIVE_FIXTURE_DIR" \
  OFT_LLM_PROVIDER="stub" \
  OFT_LLM_FIXTURES_DIR="$LLM_FIXTURES_DIR" \
  setsid uv run oft serve --port "$REPORTS_PORT" >"$REPORTS_LOG" 2>&1 &
REPORTS_PID=$!

cleanup() {
  for pid in "${DAEMON_PID:-}" "${SYMBOLS_PID:-}" "${REPORTS_PID:-}"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  rm -rf "$LLM_FIXTURES_DIR"
}
trap cleanup EXIT INT TERM

wait_for_health() {
  local base_url="$1"
  local pid="$2"
  local name="$3"
  local log="$4"
  for _ in $(seq 1 150); do
    if kill -0 "$pid" 2>/dev/null && curl -fsS "$base_url/health" >/dev/null 2>&1; then
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.2
  done
  echo "E2E ${name} daemon failed to start on $base_url" >&2
  cat "$log" >&2
  return 1
}

port_in_use() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

# Fail fast instead of mistaking a foreign daemon's /health for our own.
if port_in_use "$SYSTEM_PORT"; then
  echo "E2E system port $SYSTEM_PORT is already in use (set OFT_E2E_PORT)" >&2
  exit 1
fi
if port_in_use "$SYMBOLS_PORT"; then
  echo "E2E symbol port $SYMBOLS_PORT is already in use (set OFT_E2E_SYMBOLS_PORT)" >&2
  exit 1
fi
if port_in_use "$REPORTS_PORT"; then
  echo "E2E reports port $REPORTS_PORT is already in use (set OFT_E2E_REPORTS_PORT)" >&2
  exit 1
fi

wait_for_health "$SYSTEM_BASE_URL" "$DAEMON_PID" "system" "$SYSTEM_LOG"
wait_for_health "$SYMBOLS_BASE_URL" "$SYMBOLS_PID" "symbols" "$SYMBOLS_LOG"
wait_for_health "$REPORTS_BASE_URL" "$REPORTS_PID" "reports" "$REPORTS_LOG"

SCAN_JSON="$(uv run oft scan run \
  --fixture "$MARKET_FIXTURE" \
  --symbol BTCUSDT \
  --period 4h \
  --metrics-only \
  --json)"
SCAN_ID="$(printf '%s' "$SCAN_JSON" | jq -r '.data.scan_id')"
SCAN_DELTA="$(printf '%s' "$SCAN_JSON" | jq -r '.data.delta')"

if [ -z "$SCAN_ID" ] || [ "$SCAN_ID" = "null" ]; then
  echo "E2E fixture scan did not return a scan id" >&2
  printf '%s\n' "$SCAN_JSON" >&2
  exit 1
fi
if ! curl -fsS "$SYSTEM_BASE_URL/ready" | jq -e --arg id "$SCAN_ID" \
  '.ok == true and .data.latest_scan.scan_id == $id' >/dev/null; then
  echo "E2E scan $SCAN_ID is not visible through /ready" >&2
  exit 1
fi

# Prove the exchangeInfo fixture was seeded before the browser suite runs.
SYMBOLS_TOKEN="$(cat "$SYMBOLS_DATA_DIR/run/api-token" 2>/dev/null || true)"
if [ -z "$SYMBOLS_TOKEN" ]; then
  echo "E2E symbol daemon did not create an API token" >&2
  cat "$SYMBOLS_LOG" >&2
  exit 1
fi
if ! curl -fsS -H "Authorization: Bearer $SYMBOLS_TOKEN" \
  "$SYMBOLS_BASE_URL/api/v1/symbols" |
  jq -e '.ok == true and .data.total >= 3 and
    ([.data.items[].symbol] | index("BTCUSDT") != null) and
    ([.data.items[].symbol] | index("XAUUSDT") != null)' >/dev/null; then
  echo "E2E symbol registry was not seeded from the exchangeInfo fixture" >&2
  exit 1
fi

# Slice 38: `/watchlist` now renders only starred rows, so seed the three
# fixture symbols as starred on the SYMBOLS daemon before the symbols suite
# runs. The watchlist suite mutates this state afterwards (it is the last
# project), and each of its tests resets the stars it touches.
for symbol in BTCUSDT ETHUSDT XAUUSDT; do
  curl -fsS -X PATCH \
    -H "Authorization: Bearer $SYMBOLS_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"starred":true}' \
    "$SYMBOLS_BASE_URL/api/v1/symbols/$symbol/star" >/dev/null
done
if ! curl -fsS -H "Authorization: Bearer $SYMBOLS_TOKEN" \
  "$SYMBOLS_BASE_URL/api/v1/watchlist" |
  jq -e '.ok == true and .data.next_scan_count == 3 and .data.truncated == false' >/dev/null; then
  echo "E2E watchlist stars were not seeded on the symbol daemon" >&2
  exit 1
fi

# Slice 08: one-week backfill -> metrics-only scan -> frozen CALC report.
REPORTS_TOKEN="$(cat "$REPORTS_DATA_DIR/run/api-token" 2>/dev/null || true)"
if [ -z "$REPORTS_TOKEN" ]; then
  echo "E2E report daemon did not create an API token" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi
BACKFILL_JSON="$(OFT_DATA_DIR="$REPORTS_DATA_DIR" \
  OFT_HOST="127.0.0.1" \
  OFT_PORT="$REPORTS_PORT" \
  OFT_ARCHIVE_FIXTURE_DIR="$ARCHIVE_FIXTURE_DIR" \
  uv run oft backfill run --symbol BTCUSDT --start 2026-10-01 --end 2026-10-07 \
  --yes --json)"
if ! printf '%s' "$BACKFILL_JSON" | jq -e '.ok == true and .data.run.status == "COMPLETE"' >/dev/null; then
  echo "E2E report backfill did not complete" >&2
  printf '%s\n' "$BACKFILL_JSON" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi
SCAN_JSON="$(OFT_DATA_DIR="$REPORTS_DATA_DIR" \
  OFT_HOST="127.0.0.1" \
  OFT_PORT="$REPORTS_PORT" \
  uv run oft scan run --metrics-only --symbol BTCUSDT --period 4h --source backfill --json)"
REPORT_ID="$(printf '%s' "$SCAN_JSON" | jq -r '.data.report_id')"
REPORT_SCAN_ID="$(printf '%s' "$SCAN_JSON" | jq -r '.data.scan.scan_id')"
if [ -z "$REPORT_ID" ] || [ "$REPORT_ID" = "null" ]; then
  echo "E2E metrics-only scan did not return a report id" >&2
  printf '%s\n' "$SCAN_JSON" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi
if [ -z "$REPORT_SCAN_ID" ] || [ "$REPORT_SCAN_ID" = "null" ]; then
  echo "E2E metrics-only scan did not return a scan id" >&2
  printf '%s\n' "$SCAN_JSON" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi
if ! curl -fsS -H "Authorization: Bearer $REPORTS_TOKEN" \
  "$REPORTS_BASE_URL/api/v1/reports/$REPORT_ID" |
  jq -e --arg id "$REPORT_ID" \
    '.ok == true and .data.report_id == $id and
     .data.llm.configured == false and
     (.data.metrics.cvd.points | length) > 0 and
     (.data.snapshot.bars | length) > 0' >/dev/null; then
  echo "E2E report $REPORT_ID is not readable through the API" >&2
  exit 1
fi

# Round 2: a second scan run immediately after the first must produce a
# distinct, readable report id (rapid sequential scans never collide).
SECOND_SCAN_JSON="$(OFT_DATA_DIR="$REPORTS_DATA_DIR" \
  OFT_HOST="127.0.0.1" \
  OFT_PORT="$REPORTS_PORT" \
  uv run oft scan run --metrics-only --symbol BTCUSDT --period 4h --source backfill --json)"
SECOND_REPORT_ID="$(printf '%s' "$SECOND_SCAN_JSON" | jq -r '.data.report_id')"
SECOND_SCAN_ID="$(printf '%s' "$SECOND_SCAN_JSON" | jq -r '.data.scan.scan_id')"
if [ -z "$SECOND_REPORT_ID" ] || [ "$SECOND_REPORT_ID" = "null" ] ||
  [ "$SECOND_REPORT_ID" = "$REPORT_ID" ]; then
  echo "E2E second consecutive scan did not produce a distinct report id" >&2
  printf '%s\n' "$SECOND_SCAN_JSON" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi
if [ "$SECOND_SCAN_ID" = "$REPORT_SCAN_ID" ]; then
  echo "E2E second consecutive scan reused the first scan id" >&2
  exit 1
fi
if ! curl -fsS -H "Authorization: Bearer $REPORTS_TOKEN" \
  "$REPORTS_BASE_URL/api/v1/reports/$SECOND_REPORT_ID" |
  jq -e --arg id "$SECOND_REPORT_ID" \
    '.ok == true and .data.report_id == $id and
     (.data.snapshot.bars | length) > 0' >/dev/null; then
  echo "E2E second report $SECOND_REPORT_ID is not readable through the API" >&2
  exit 1
fi

# Slice 12: starred-symbol batch scan through the real daemon and the
# deterministic stub provider (no live credential, no network). BTCUSDT has a
# week of recorded aggTrades; XAUUSDT is registry-only and must fail without
# aborting the batch.
star_symbol() {
  curl -fsS -X PATCH \
    -H "Authorization: Bearer $REPORTS_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"starred":true}' \
    "$REPORTS_BASE_URL/api/v1/symbols/$1/star" >/dev/null
}
star_symbol BTCUSDT
star_symbol XAUUSDT

BATCH_JSON="$(curl -fsS -X POST \
  -H "Authorization: Bearer $REPORTS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: e2e-batch-valid" \
  -d '{"period":"4h","source":"backfill","metrics_only":false,"provider":"stub"}' \
  "$REPORTS_BASE_URL/api/v1/scans")"
BATCH_ID="$(printf '%s' "$BATCH_JSON" | jq -r '.data.scan_id // empty')"
if [ -z "$BATCH_ID" ]; then
  echo "E2E valid batch scan did not return a batch id" >&2
  printf '%s\n' "$BATCH_JSON" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi
if ! printf '%s' "$BATCH_JSON" | jq -e '
  .ok == true and
  .data.status == "PARTIAL" and
  .data.trigger_source == "MANUAL" and
  .data.period == "4h" and
  .data.symbols_total == 2 and
  .data.symbols_completed == 1 and
  .data.symbols_failed == 1 and
  .data.symbols_degraded == 0 and
  ([.data.symbols[] | select(.symbol == "BTCUSDT") | .stage] | .[0] == "DONE") and
  ([.data.symbols[] | select(.symbol == "BTCUSDT") | .llm_status] | .[0] | IN("VALID", "REPAIRED")) and
  ([.data.symbols[] | select(.symbol == "BTCUSDT") | .report_id] | .[0] != null) and
  ([.data.symbols[] | select(.symbol == "XAUUSDT") | .stage] | .[0] == "FAILED") and
  ([.data.symbols[] | select(.symbol == "XAUUSDT") | .report_id] | .[0] == null) and
  (.data.report_links | length) == 1 and
  .data.delivery.configured == false' >/dev/null; then
  echo "E2E valid batch scan did not produce the expected per-symbol outcome" >&2
  printf '%s\n' "$BATCH_JSON" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi

# Slice 29: seed one OPEN paper position for the browser journey before the
# degraded fixture flip. Restore the valid ANALYSIS fixture first (the batch
# scan above consumed it and the re-check must use it), then drive the real
# trigger -> recheck -> accept path over HTTP with the reports bearer token.
cp "$ROOT/tests/fixtures/llm/opportunity_valid.json" "$LLM_FIXTURES_DIR/opportunity_valid.json"
BATCH_REPORT_ID="$(printf '%s' "$BATCH_JSON" |
  jq -r '[.data.symbols[] | select(.symbol == "BTCUSDT") | .report_id] | .[0]')"
if [ -z "$BATCH_REPORT_ID" ] || [ "$BATCH_REPORT_ID" = "null" ]; then
  echo "E2E valid batch scan has no BTCUSDT report for the positions journey" >&2
  printf '%s\n' "$BATCH_JSON" >&2
  exit 1
fi
POSITION_OPPORTUNITY_ID="$(curl -fsS -H "Authorization: Bearer $REPORTS_TOKEN" \
  "$REPORTS_BASE_URL/api/v1/reports/$BATCH_REPORT_ID" |
  jq -r '.data.llm.opportunity_ids[0] // empty')"
if [ -z "$POSITION_OPPORTUNITY_ID" ]; then
  echo "E2E batch report has no opportunity for the positions journey" >&2
  exit 1
fi
TRIGGER_JSON="$(curl -fsS -X POST \
  -H "Authorization: Bearer $REPORTS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: e2e-position-evaluate" \
  -d "{\"opportunity_id\":\"$POSITION_OPPORTUNITY_ID\"}" \
  "$REPORTS_BASE_URL/api/v1/triggers/evaluate")"
TRIGGER_ID="$(printf '%s' "$TRIGGER_JSON" | jq -r '.data.trigger.trigger_id // empty')"
if [ -z "$TRIGGER_ID" ]; then
  echo "E2E positions journey could not create a trigger" >&2
  printf '%s\n' "$TRIGGER_JSON" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi
curl -fsS -X POST \
  -H "Authorization: Bearer $REPORTS_TOKEN" \
  -H "Idempotency-Key: e2e-position-recheck" \
  "$REPORTS_BASE_URL/api/v1/triggers/$TRIGGER_ID/recheck" >/dev/null
ACCEPT_JSON="$(curl -fsS -X POST \
  -H "Authorization: Bearer $REPORTS_TOKEN" \
  -H "Idempotency-Key: e2e-position-accept" \
  "$REPORTS_BASE_URL/api/v1/triggers/$TRIGGER_ID/accept")"
POSITION_ID="$(printf '%s' "$ACCEPT_JSON" | jq -r '.data.position.position_id // empty')"
if [ -z "$POSITION_ID" ]; then
  echo "E2E positions journey did not create a paper position" >&2
  printf '%s\n' "$ACCEPT_JSON" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi
if ! curl -fsS -H "Authorization: Bearer $REPORTS_TOKEN" \
  "$REPORTS_BASE_URL/api/v1/positions/$POSITION_ID" |
  jq -e --arg id "$POSITION_ID" '.ok == true and .data.position_id == $id' >/dev/null; then
  echo "E2E position $POSITION_ID is not readable through the API" >&2
  exit 1
fi

# Slice 40: the batch report's chart must expose the linked opportunity zone and
# the accepted paper position's markers, so the browser suite can assert
# overlays against the real server payload (never client-inferred).
if ! curl -fsS -H "Authorization: Bearer $REPORTS_TOKEN" \
  "$REPORTS_BASE_URL/api/v1/reports/$BATCH_REPORT_ID/chart" |
  jq -e '.ok == true and .data.status == "AVAILABLE" and
    (.data.zones | length) > 0 and (.data.markers | length) > 0' >/dev/null; then
  echo "E2E overlay report $BATCH_REPORT_ID has no server-derived zones/markers" >&2
  exit 1
fi

# Slice 30: seed one CLOSED trade for the journal journey on the same reports
# daemon without touching the OPEN position above. A second starred batch scan
# yields a fresh opportunity on the still-valid ANALYSIS fixture; it is
# accepted through the real paper path, closed with a stop mark, and its trade
# log is marked COMPLETE (a seeded position has no holding-window bars, so its
# own quality would be PARTIAL). One outcome tag starts with a formula prefix so
# the browser export proves server-side neutralization.
JOURNAL_BATCH_JSON="$(curl -fsS -X POST \
  -H "Authorization: Bearer $REPORTS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: e2e-journal-batch" \
  -d '{"period":"4h","source":"backfill","metrics_only":false,"provider":"stub"}' \
  "$REPORTS_BASE_URL/api/v1/scans")"
JOURNAL_REPORT_ID="$(printf '%s' "$JOURNAL_BATCH_JSON" |
  jq -r '[.data.symbols[] | select(.symbol == "BTCUSDT") | .report_id] | .[0] // empty')"
if [ -z "$JOURNAL_REPORT_ID" ]; then
  echo "E2E journal batch scan did not produce a BTCUSDT report" >&2
  printf '%s\n' "$JOURNAL_BATCH_JSON" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi
JOURNAL_OPPORTUNITY_ID="$(curl -fsS -H "Authorization: Bearer $REPORTS_TOKEN" \
  "$REPORTS_BASE_URL/api/v1/reports/$JOURNAL_REPORT_ID" |
  jq -r '.data.llm.opportunity_ids[0] // empty')"
if [ -z "$JOURNAL_OPPORTUNITY_ID" ]; then
  echo "E2E journal report has no opportunity" >&2
  exit 1
fi
JOURNAL_TRIGGER_JSON="$(curl -fsS -X POST \
  -H "Authorization: Bearer $REPORTS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: e2e-journal-evaluate" \
  -d "{\"opportunity_id\":\"$JOURNAL_OPPORTUNITY_ID\"}" \
  "$REPORTS_BASE_URL/api/v1/triggers/evaluate")"
JOURNAL_TRIGGER_ID="$(printf '%s' "$JOURNAL_TRIGGER_JSON" |
  jq -r '.data.trigger.trigger_id // empty')"
if [ -z "$JOURNAL_TRIGGER_ID" ]; then
  echo "E2E journal journey could not create a trigger" >&2
  printf '%s\n' "$JOURNAL_TRIGGER_JSON" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi
curl -fsS -X POST \
  -H "Authorization: Bearer $REPORTS_TOKEN" \
  -H "Idempotency-Key: e2e-journal-recheck" \
  "$REPORTS_BASE_URL/api/v1/triggers/$JOURNAL_TRIGGER_ID/recheck" >/dev/null
JOURNAL_ACCEPT_JSON="$(curl -fsS -X POST \
  -H "Authorization: Bearer $REPORTS_TOKEN" \
  -H "Idempotency-Key: e2e-journal-accept" \
  "$REPORTS_BASE_URL/api/v1/triggers/$JOURNAL_TRIGGER_ID/accept")"
JOURNAL_POSITION_ID="$(printf '%s' "$JOURNAL_ACCEPT_JSON" |
  jq -r '.data.position.position_id // empty')"
if [ -z "$JOURNAL_POSITION_ID" ]; then
  echo "E2E journal journey did not create a paper position" >&2
  printf '%s\n' "$JOURNAL_ACCEPT_JSON" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi

# Touch the stop through SQLite (the same fallback the Python e2e uses) and run
# the real deterministic monitor tick so the trade closes through the app path.
OFT_DATA_DIR="$REPORTS_DATA_DIR" uv run python - "$JOURNAL_POSITION_ID" <<'PY'
import json
import sqlite3
import sys
from datetime import UTC, datetime

from oft.config import load_settings

settings = load_settings()
database = settings.data_dir / "oft.db"
with sqlite3.connect(database) as connection:
    row = connection.execute(
        "SELECT payload_json FROM positions WHERE position_id = ?", (sys.argv[1],)
    ).fetchone()
    if row is None:
        raise SystemExit(f"position {sys.argv[1]} not found")
    payload = json.loads(row[0])
    payload["mark_price"] = payload["stop_price"]
    payload["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    connection.execute(
        "UPDATE positions SET payload_json = ? WHERE position_id = ?",
        (json.dumps(payload, sort_keys=True), sys.argv[1]),
    )
    connection.commit()
PY

JOURNAL_MONITOR_JSON="$(curl -fsS -X POST \
  -H "Authorization: Bearer $REPORTS_TOKEN" \
  "$REPORTS_BASE_URL/api/v1/positions/$JOURNAL_POSITION_ID/monitor")"
JOURNAL_TRADE_ID="$(printf '%s' "$JOURNAL_MONITOR_JSON" |
  jq -r '.data.trade_log.trade_id // empty')"
if [ -z "$JOURNAL_TRADE_ID" ]; then
  echo "E2E journal position did not close into a trade log" >&2
  printf '%s\n' "$JOURNAL_MONITOR_JSON" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi

# Mark the seeded trade COMPLETE so the stats sample is defined, keep one
# formula-prefixed outcome tag for the export-neutralization assertion, and
# backdate the close 30 days so the 4w window excludes it while 8w includes it
# (the period filter must move the table, not only the stats strip).
OFT_DATA_DIR="$REPORTS_DATA_DIR" uv run python - "$JOURNAL_TRADE_ID" <<'PY'
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

from oft.config import load_settings

settings = load_settings()
database = settings.data_dir / "oft.db"
closed_at = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
opened_at = (datetime.now(UTC) - timedelta(days=32)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
with sqlite3.connect(database) as connection:
    row = connection.execute(
        "SELECT payload_json FROM trade_logs WHERE trade_id = ?", (sys.argv[1],)
    ).fetchone()
    if row is None:
        raise SystemExit(f"trade {sys.argv[1]} not found")
    payload = json.loads(row[0])
    payload["data_quality"] = {
        "status": "COMPLETE",
        "reasons": [],
        "gaps": [],
        "last_event_at": closed_at,
        "coverage_start": opened_at,
    }
    payload["outcome_tags"] = ["=SUM(1,1)", "STOPPED"]
    payload["opened_at"] = opened_at
    payload["closed_at"] = closed_at
    connection.execute(
        "UPDATE trade_logs SET payload_json = ?, opened_at = ?, closed_at = ? WHERE trade_id = ?",
        (json.dumps(payload, sort_keys=True), opened_at, closed_at, sys.argv[1]),
    )
    connection.commit()
PY

if ! curl -fsS -H "Authorization: Bearer $REPORTS_TOKEN" \
  "$REPORTS_BASE_URL/api/v1/journal/$JOURNAL_TRADE_ID" |
  jq -e --arg id "$JOURNAL_TRADE_ID" \
    '.ok == true and .data.trade_id == $id and .data.data_quality.status == "COMPLETE"' >/dev/null; then
  echo "E2E journal trade $JOURNAL_TRADE_ID is not readable through the API" >&2
  exit 1
fi
if ! curl -fsS -H "Authorization: Bearer $REPORTS_TOKEN" \
  "$REPORTS_BASE_URL/api/v1/journal/stats?period=8w" |
  jq -e '.ok == true and .data.status == "DEFINED" and .data.sample_size >= 1' >/dev/null; then
  echo "E2E journal stats are not defined over the seeded trade" >&2
  exit 1
fi
if ! curl -fsS -H "Authorization: Bearer $REPORTS_TOKEN" \
  "$REPORTS_BASE_URL/api/v1/journal/$JOURNAL_TRADE_ID/review" |
  jq -e '.ok == true and .data.status == "MISSING"' >/dev/null; then
  echo "E2E journal trade unexpectedly already has a review" >&2
  exit 1
fi
if ! curl -fsS -H "Authorization: Bearer $REPORTS_TOKEN" \
  "$REPORTS_BASE_URL/api/v1/journal/export/file?format=csv" |
  grep -qF "'=SUM(1,1)"; then
  echo "E2E journal CSV export did not neutralize the formula tag" >&2
  exit 1
fi

# Flip the ANALYSIS fixture to a schema-invalid payload: the next batch must
# mark the data-bearing symbol DEGRADED with a CALC-only report and continue.
cp "$ROOT/tests/fixtures/llm/invalid_schema.json" \
  "$LLM_FIXTURES_DIR/opportunity_valid.json"
BATCH_DEGRADED_JSON="$(curl -fsS -X POST \
  -H "Authorization: Bearer $REPORTS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: e2e-batch-degraded" \
  -d '{"period":"4h","source":"backfill","metrics_only":false,"provider":"stub"}' \
  "$REPORTS_BASE_URL/api/v1/scans")"
BATCH_DEGRADED_ID="$(printf '%s' "$BATCH_DEGRADED_JSON" | jq -r '.data.scan_id // empty')"
if [ -z "$BATCH_DEGRADED_ID" ]; then
  echo "E2E degraded batch scan did not return a batch id" >&2
  printf '%s\n' "$BATCH_DEGRADED_JSON" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi
if ! printf '%s' "$BATCH_DEGRADED_JSON" | jq -e '
  .ok == true and
  .data.status == "PARTIAL" and
  .data.symbols_total == 2 and
  .data.symbols_completed == 0 and
  .data.symbols_degraded == 1 and
  .data.symbols_failed == 1 and
  ([.data.symbols[] | select(.symbol == "BTCUSDT") | .stage] | .[0] == "DEGRADED") and
  ([.data.symbols[] | select(.symbol == "BTCUSDT") | .llm_status] | .[0] == "REJECTED") and
  ([.data.symbols[] | select(.symbol == "BTCUSDT") | .report_id] | .[0] != null) and
  ([.data.symbols[] | select(.symbol == "XAUUSDT") | .stage] | .[0] == "FAILED")' >/dev/null; then
  echo "E2E degraded batch scan did not produce the expected per-symbol outcome" >&2
  printf '%s\n' "$BATCH_DEGRADED_JSON" >&2
  cat "$REPORTS_LOG" >&2
  exit 1
fi

# Slice 08 fixtures: MISSING and hostile-symbol reports. They are written to the
# real SQLite store through the project's own store port (no browser route
# interception) so the daemon serves them exactly like a scan-produced report.
MISSING_REPORT_ID="$(OFT_DATA_DIR="$REPORTS_DATA_DIR" uv run python - "$REPORT_ID" <<'PY'
import json
import sys
from dataclasses import replace

from oft.adapters.sqlite_store import SqliteStore
from oft.config import load_settings

store = SqliteStore(load_settings())
record = store.get_report(sys.argv[1])
if record is None:
    raise SystemExit(f"report {sys.argv[1]} not found")
snapshot = json.loads(record.snapshot_json)
snapshot["bars"] = []
snapshot["data_quality"] = "MISSING"
snapshot["quality_reasons"] = ["fixture: no market data was recorded for this report"]
missing_id = "RPT-FIXTURE-MISSING"
store.save_report(
    replace(record, report_id=missing_id, data_quality="MISSING", snapshot_json=json.dumps(snapshot))
)
print(missing_id)
PY
)"
HOSTILE_REPORT_ID="$(OFT_DATA_DIR="$REPORTS_DATA_DIR" uv run python - "$REPORT_ID" <<'PY'
import sys
from dataclasses import replace

from oft.adapters.sqlite_store import SqliteStore
from oft.config import load_settings

store = SqliteStore(load_settings())
record = store.get_report(sys.argv[1])
if record is None:
    raise SystemExit(f"report {sys.argv[1]} not found")
hostile_id = "RPT-FIXTURE-HOSTILE"
store.save_report(
    replace(record, report_id=hostile_id, symbol='<img src=x onerror="alert(1)">')
)
print(hostile_id)
PY
)"
if ! curl -fsS -H "Authorization: Bearer $REPORTS_TOKEN" \
  "$REPORTS_BASE_URL/api/v1/reports/$MISSING_REPORT_ID" |
  jq -e --arg id "$MISSING_REPORT_ID" \
    '.ok == true and .data.report_id == $id and
     .data.data_quality == "MISSING" and
     (.data.snapshot.bars | length) == 0' >/dev/null; then
  echo "E2E MISSING fixture report is not readable through the API" >&2
  exit 1
fi
if ! curl -fsS -H "Authorization: Bearer $REPORTS_TOKEN" \
  "$REPORTS_BASE_URL/api/v1/reports/$HOSTILE_REPORT_ID" |
  jq -e '.ok == true and (.data.symbol | startswith("<img"))' >/dev/null; then
  echo "E2E hostile fixture report is not readable through the API" >&2
  exit 1
fi

export OFT_E2E_BASE_URL="$SYSTEM_BASE_URL"
export OFT_E2E_SYMBOLS_BASE_URL="$SYMBOLS_BASE_URL"
export OFT_E2E_REPORT_BASE_URL="$REPORTS_BASE_URL"
export OFT_E2E_SYSTEM_BOOTSTRAP_FILE="$SYSTEM_DATA_DIR/run/browser-bootstrap"
export OFT_E2E_SYMBOLS_BOOTSTRAP_FILE="$SYMBOLS_DATA_DIR/run/browser-bootstrap"
export OFT_E2E_REPORT_BOOTSTRAP_FILE="$REPORTS_DATA_DIR/run/browser-bootstrap"
export OFT_E2E_SYMBOLS_API_TOKEN="$SYMBOLS_TOKEN"
export OFT_E2E_REPORT_ID="$REPORT_ID"
export OFT_E2E_REPORT_SCAN_ID="$REPORT_SCAN_ID"
export OFT_E2E_SECOND_REPORT_ID="$SECOND_REPORT_ID"
export OFT_E2E_SECOND_SCAN_ID="$SECOND_SCAN_ID"
export OFT_E2E_BATCH_ID="$BATCH_ID"
export OFT_E2E_BATCH_DEGRADED_ID="$BATCH_DEGRADED_ID"
export OFT_E2E_MISSING_REPORT_ID="$MISSING_REPORT_ID"
export OFT_E2E_HOSTILE_REPORT_ID="$HOSTILE_REPORT_ID"
export OFT_E2E_POSITION_ID="$POSITION_ID"
export OFT_E2E_JOURNAL_TRADE_ID="$JOURNAL_TRADE_ID"
export OFT_E2E_JOURNAL_POSITION_ID="$JOURNAL_POSITION_ID"
export OFT_E2E_JOURNAL_BATCH_ID="$(printf '%s' "$JOURNAL_BATCH_JSON" |
  jq -r '.data.scan_id // empty')"
export OFT_E2E_JOURNAL_REPORT_ID="$JOURNAL_REPORT_ID"
export OFT_E2E_JOURNAL_SCREENS_DIR="$JOURNAL_SCREENS_DIR"
# Slice 40: the report whose chart carries server-derived overlays.
export OFT_E2E_CHART_OVERLAY_REPORT_ID="$BATCH_REPORT_ID"
export OFT_E2E_CHART_OVERLAY_SCREENS_DIR="$CHART_SCREENS_DIR"
# Slice 41: the read-only opportunity ledger screenshots (the spec discovers the
# seeded opportunity through the API; no id export is needed).
export OFT_E2E_OPPORTUNITIES_SCREENS_DIR="$OPPORTUNITIES_SCREENS_DIR"
export OFT_E2E_REPORT_API_TOKEN="$REPORTS_TOKEN"
export OFT_E2E_DATA_DIR="$SYSTEM_DATA_DIR"
export OFT_E2E_SCAN_ID="$SCAN_ID"
export OFT_E2E_SCAN_DELTA="$SCAN_DELTA"
export OFT_E2E_SYSTEM_SCREENS_DIR="$SYSTEM_SCREENS_DIR"
export OFT_E2E_SYMBOLS_SCREENS_DIR="$SYMBOLS_SCREENS_DIR"
export OFT_E2E_REPORT_SCREENS_DIR="$REPORTS_SCREENS_DIR"
# Global Playwright report/artifact output for this run lands under e2e08,
# except a chart-only run, whose report, traces, and artifacts land under
# e2e40 (the slice-40 screens directory).
CHART_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --project=chart) CHART_ONLY=true ;;
  esac
done
if [ "$CHART_ONLY" = true ]; then
  export OFT_E2E_SCREENS_DIR="$CHART_SCREENS_DIR"
  export OFT_E2E_REPORT_DIR="$CHART_SCREENS_DIR/report"
else
  export OFT_E2E_SCREENS_DIR="$REPORTS_SCREENS_DIR"
  export OFT_E2E_REPORT_DIR="$REPORTS_SCREENS_DIR/report"
fi

mkdir -p "$SYSTEM_SCREENS_DIR" "$SYMBOLS_SCREENS_DIR" "$REPORTS_SCREENS_DIR" "$JOURNAL_SCREENS_DIR" "$CHART_SCREENS_DIR" "$OPPORTUNITIES_SCREENS_DIR"
echo "system e2e daemon: $SYSTEM_BASE_URL · data: $SYSTEM_DATA_DIR · scan: $SCAN_ID · delta: $SCAN_DELTA"
echo "symbol e2e daemon: $SYMBOLS_BASE_URL · data: $SYMBOLS_DATA_DIR · seeded from $EXCHANGE_INFO_FIXTURE"
echo "report e2e daemon: $REPORTS_BASE_URL · data: $REPORTS_DATA_DIR · report: $REPORT_ID"

cd "$WEB_DIR"
./node_modules/.bin/playwright test "$@"

# Slice 32: the `/settings/scan` journey owns its own daemon and port, so it is
# not a project in `playwright.config.ts` (that config targets the three daemons
# above). Running its own runner here keeps `make e2e` / `npm --prefix web run
# test:e2e` gating the browser-level settings guarantees; its exit code
# propagates through `set -e`.
bash "$SCRIPT_DIR/run-settings-e2e.sh"

# Slice 35: `/settings/llm` needs the encrypted-file secret store and a local
# fake provider, so it owns its own daemon/runner as well. Its exit code
# propagates through `set -e`.
bash "$SCRIPT_DIR/run-llm-settings-e2e.sh"
