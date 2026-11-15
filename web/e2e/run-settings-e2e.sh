#!/usr/bin/env bash
# Run the slice-32 `/settings/scan` journey against one real daemon.
#
# Starts exactly one `oft serve` with a temporary OFT_DATA dir on a free port
# at 7652 or higher. The existing suites own 7622/7632/7642 and the verifier may
# own 7662, so those are never used. The daemon is stopped and the temp dir is
# removed on exit.
#
# The journey drives the real API and the built `web/dist` shell; it saves
# `scan.period`, replays an idempotent PUT, rejects an invalid value, and
# captures 375/768/1440 screenshots under
# `.claude/ecc-workflow/screens/slice-32-e2e/`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT="$(cd "$WEB_DIR/.." && pwd)"
SCREENS_DIR="$ROOT/.claude/ecc-workflow/screens/slice-32-e2e"
REPORT_DIR="$SCREENS_DIR/report"

RESERVED_PORTS="7622 7632 7642 7662"

port_in_use() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

is_reserved_port() {
  case " $RESERVED_PORTS " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}

pick_port() {
  local candidate="${OFT_E2E_SETTINGS_PORT:-7652}"
  if is_reserved_port "$candidate"; then
    echo "E2E settings port $candidate is reserved for another suite" >&2
    return 1
  fi
  if ! port_in_use "$candidate"; then
    printf '%s\n' "$candidate"
    return 0
  fi
  local next
  for next in $(seq 7653 7699); do
    if is_reserved_port "$next"; then
      continue
    fi
    if ! port_in_use "$next"; then
      printf '%s\n' "$next"
      return 0
    fi
  done
  echo "no free E2E settings port in 7653-7699" >&2
  return 1
}

if [ ! -f "$WEB_DIR/dist/index.html" ]; then
  echo "web/dist/index.html is missing; build it with 'npm --prefix web run build' first" >&2
  exit 1
fi

PORT="$(pick_port)"
BASE_URL="http://127.0.0.1:${PORT}"
DATA_DIR="$(mktemp -d -t oft-e2e-settings-XXXXXX)"
LOG="$DATA_DIR/daemon.log"
CONFIG="$DATA_DIR/playwright.settings.config.mjs"

cleanup() {
  if [ -n "${DAEMON_PID:-}" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
    kill -- "-$DAEMON_PID" 2>/dev/null || kill "$DAEMON_PID" 2>/dev/null || true
    wait "$DAEMON_PID" 2>/dev/null || true
  fi
  rm -rf "$DATA_DIR"
}
trap cleanup EXIT INT TERM

export OFT_ENV="test"
export OFT_HOST="127.0.0.1"
export OFT_PORT="$PORT"
export OFT_DATA_DIR="$DATA_DIR"
export OFT_LOG_LEVEL="WARNING"

cd "$ROOT"
setsid uv run oft serve --port "$PORT" >"$LOG" 2>&1 &
DAEMON_PID=$!

wait_for_health() {
  local attempt
  for attempt in $(seq 1 200); do
    if kill -0 "$DAEMON_PID" 2>/dev/null && curl -fsS "$BASE_URL/health" >/dev/null 2>&1; then
      return 0
    fi
    if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
      break
    fi
    sleep 0.2
  done
  echo "E2E settings daemon failed to start on $BASE_URL" >&2
  cat "$LOG" >&2
  return 1
}

wait_for_bootstrap() {
  local attempt
  for attempt in $(seq 1 200); do
    if [ -f "$DATA_DIR/run/browser-bootstrap" ]; then
      return 0
    fi
    sleep 0.1
  done
  echo "E2E settings daemon did not write $DATA_DIR/run/browser-bootstrap" >&2
  cat "$LOG" >&2
  return 1
}

wait_for_health
wait_for_bootstrap

mkdir -p "$SCREENS_DIR" "$REPORT_DIR"

# The repo Playwright config only matches the existing specs, so this journey
# uses a generated config that points the repo Playwright at the same directory
# with a dedicated `settings` project and its own report output. The config is
# plain ESM with no imports so it can live in the temp dir.
cat >"$CONFIG" <<EOF
export default {
  testDir: '$WEB_DIR/e2e',
  fullyParallel: false,
  workers: 1,
  forbidOnly: false,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  outputDir: '$SCREENS_DIR/artifacts',
  reporter: [
    ['list'],
    ['junit', { outputFile: '$REPORT_DIR/junit.xml' }],
    ['html', { outputFolder: '$REPORT_DIR/html', open: 'never' }],
  ],
  use: {
    baseURL: '$BASE_URL',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [{ name: 'settings', testMatch: /settings\.spec\.ts/ }],
}
EOF

export OFT_E2E_SETTINGS_BASE_URL="$BASE_URL"
export OFT_E2E_SETTINGS_BOOTSTRAP_FILE="$DATA_DIR/run/browser-bootstrap"
export OFT_E2E_SETTINGS_DATA_DIR="$DATA_DIR"
export OFT_E2E_SETTINGS_SCREENS_DIR="$SCREENS_DIR"
export OFT_E2E_SETTINGS_REPORT_DIR="$REPORT_DIR"

echo "settings e2e daemon: $BASE_URL · data: $DATA_DIR"
echo "screens: $SCREENS_DIR"

cd "$WEB_DIR"
set +e
./node_modules/.bin/playwright test \
  --config "$CONFIG" \
  e2e/settings.spec.ts "$@"
STATUS=$?
set -e
exit "$STATUS"
