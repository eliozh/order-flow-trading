#!/usr/bin/env bash
# Run the walking-skeleton /system E2E journey against a real daemon.
#
# Starts `oft serve` on 127.0.0.1:7622 with a temporary OFT_DATA_DIR, runs the
# fixture scan through the CLI/API, then drives the built SPA with Playwright.
# The daemon is always stopped before this script exits.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT="$(cd "$WEB_DIR/.." && pwd)"

PORT="${OFT_E2E_PORT:-7622}"
BASE_URL="http://127.0.0.1:${PORT}"
SCREENS_DIR="$ROOT/.claude/ecc-workflow/screens/e2e"
FIXTURE="$ROOT/tests/fixtures/market/btcusdt_agg-trades_sample.csv"

DATA_DIR="$(mktemp -d -t oft-e2e-XXXXXX)"
DAEMON_LOG="$DATA_DIR/daemon.log"

export OFT_DATA_DIR="$DATA_DIR"
export OFT_HOST="127.0.0.1"
export OFT_PORT="$PORT"
export OFT_LOG_LEVEL="WARNING"

cd "$ROOT"
setsid uv run oft serve --port "$PORT" >"$DAEMON_LOG" 2>&1 &
DAEMON_PID=$!

cleanup() {
  if kill -0 "$DAEMON_PID" 2>/dev/null; then
    kill -- "-$DAEMON_PID" 2>/dev/null || kill "$DAEMON_PID" 2>/dev/null || true
    wait "$DAEMON_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

started=0
for _ in $(seq 1 150); do
  if curl -fsS "$BASE_URL/health" >/dev/null 2>&1; then
    started=1
    break
  fi
  if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
    break
  fi
  sleep 0.2
done
if [ "$started" -ne 1 ]; then
  echo "E2E daemon failed to start on $BASE_URL" >&2
  cat "$DAEMON_LOG" >&2
  exit 1
fi

SCAN_JSON="$(uv run oft scan run \
  --fixture "$FIXTURE" \
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
if ! curl -fsS "$BASE_URL/ready" | jq -e --arg id "$SCAN_ID" \
  '.ok == true and .data.latest_scan.scan_id == $id' >/dev/null; then
  echo "E2E scan $SCAN_ID is not visible through /ready" >&2
  exit 1
fi

export OFT_E2E_BASE_URL="$BASE_URL"
export OFT_E2E_DATA_DIR="$DATA_DIR"
export OFT_E2E_SCAN_ID="$SCAN_ID"
export OFT_E2E_SCAN_DELTA="$SCAN_DELTA"
export OFT_E2E_SCREENS_DIR="$SCREENS_DIR"

mkdir -p "$SCREENS_DIR"
echo "e2e daemon: $BASE_URL · data: $DATA_DIR · scan: $SCAN_ID · delta: $SCAN_DELTA"

cd "$WEB_DIR"
./node_modules/.bin/playwright test "$@"
