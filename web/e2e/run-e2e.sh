#!/usr/bin/env bash
# Run the slice-01 /system journey and the slice-02 read-only symbol journeys
# against real daemons.
#
# Starts `oft serve` twice with temporary OFT_DATA_DIRs:
#   - 127.0.0.1:7622 for the walking-skeleton /system journey;
#   - 127.0.0.1:7632 for the symbol registry, seeded from the committed
#     exchangeInfo fixture by the daemon bootstrap.
# The fixture market scan is applied to the first daemon, then both daemons are
# driven by Playwright. Every daemon is stopped before this script exits.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT="$(cd "$WEB_DIR/.." && pwd)"

SYSTEM_PORT="${OFT_E2E_PORT:-7622}"
SYMBOLS_PORT="${OFT_E2E_SYMBOLS_PORT:-7632}"
SYSTEM_BASE_URL="http://127.0.0.1:${SYSTEM_PORT}"
SYMBOLS_BASE_URL="http://127.0.0.1:${SYMBOLS_PORT}"

SYSTEM_SCREENS_DIR="$ROOT/.claude/ecc-workflow/screens/e2e"
SYMBOLS_SCREENS_DIR="$ROOT/.claude/ecc-workflow/screens/e2e02"
MARKET_FIXTURE="$ROOT/tests/fixtures/market/btcusdt_agg-trades_sample.csv"
EXCHANGE_INFO_FIXTURE="$ROOT/tests/fixtures/binance/exchange_info_sample.json"

SYSTEM_DATA_DIR="$(mktemp -d -t oft-e2e-system-XXXXXX)"
SYMBOLS_DATA_DIR="$(mktemp -d -t oft-e2e-symbols-XXXXXX)"
SYSTEM_LOG="$SYSTEM_DATA_DIR/daemon.log"
SYMBOLS_LOG="$SYMBOLS_DATA_DIR/daemon.log"

# The system daemon inherits these exported settings (CLI scan calls use them).
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

cleanup() {
  for pid in "${DAEMON_PID:-}" "${SYMBOLS_PID:-}"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
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

wait_for_health "$SYSTEM_BASE_URL" "$DAEMON_PID" "system" "$SYSTEM_LOG"
wait_for_health "$SYMBOLS_BASE_URL" "$SYMBOLS_PID" "symbols" "$SYMBOLS_LOG"

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

export OFT_E2E_BASE_URL="$SYSTEM_BASE_URL"
export OFT_E2E_SYMBOLS_BASE_URL="$SYMBOLS_BASE_URL"
export OFT_E2E_SYMBOLS_API_TOKEN="$SYMBOLS_TOKEN"
export OFT_E2E_DATA_DIR="$SYSTEM_DATA_DIR"
export OFT_E2E_SCAN_ID="$SCAN_ID"
export OFT_E2E_SCAN_DELTA="$SCAN_DELTA"
export OFT_E2E_SYSTEM_SCREENS_DIR="$SYSTEM_SCREENS_DIR"
export OFT_E2E_SYMBOLS_SCREENS_DIR="$SYMBOLS_SCREENS_DIR"
export OFT_E2E_SCREENS_DIR="$SYMBOLS_SCREENS_DIR"
export OFT_E2E_REPORT_DIR="$SYMBOLS_SCREENS_DIR/report"

mkdir -p "$SYSTEM_SCREENS_DIR" "$SYMBOLS_SCREENS_DIR"
echo "system e2e daemon: $SYSTEM_BASE_URL · data: $SYSTEM_DATA_DIR · scan: $SCAN_ID · delta: $SCAN_DELTA"
echo "symbol e2e daemon: $SYMBOLS_BASE_URL · data: $SYMBOLS_DATA_DIR · seeded from $EXCHANGE_INFO_FIXTURE"

cd "$WEB_DIR"
./node_modules/.bin/playwright test "$@"
