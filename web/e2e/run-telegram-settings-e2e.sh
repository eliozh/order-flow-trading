#!/usr/bin/env bash
# Run the slice-53 `/settings/telegram` browser journey against one real daemon.
#
# Starts exactly one `oft serve` with a temporary OFT_DATA dir on a free port.
# OFT_MASTER_KEY and the fail keyring backend make the encrypted-file secret
# fallback available headlessly, so the write-only bot token can be saved
# without touching any operator keyring or the real Bot API.
#
# The daemon is stopped and the temp dir removed on exit. Screenshots and axe
# results land under `.claude/ecc-workflow/screens/slice-53-e2e/`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT="$(cd "$WEB_DIR/.." && pwd)"
DEFAULT_SCREENS_DIR="$ROOT/.claude/ecc-workflow/screens/slice-53-e2e"
SCREENS_DIR="${OFT_E2E_TELEGRAM_SCREENS_DIR:-$DEFAULT_SCREENS_DIR}"
REPORT_DIR="${OFT_E2E_TELEGRAM_REPORT_DIR:-$SCREENS_DIR/report}"

RESERVED_PORTS="7622 7632 7642 7652 7653 7654 7662"

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
  local candidate="${OFT_E2E_TELEGRAM_SETTINGS_PORT:-7670}"
  if is_reserved_port "$candidate"; then
    echo "E2E Telegram settings port $candidate is reserved for another suite" >&2
    return 1
  fi
  if ! port_in_use "$candidate"; then
    printf '%s\n' "$candidate"
    return 0
  fi
  local next
  for next in $(seq 7671 7699); do
    if is_reserved_port "$next"; then
      continue
    fi
    if ! port_in_use "$next"; then
      printf '%s\n' "$next"
      return 0
    fi
  done
  echo "no free E2E Telegram settings port in 7671-7699" >&2
  return 1
}

if [ ! -f "$WEB_DIR/dist/index.html" ]; then
  echo "web/dist/index.html is missing; build it with 'npm --prefix web run build' first" >&2
  exit 1
fi

PORT="$(pick_port)"
BASE_URL="http://127.0.0.1:${PORT}"
DATA_DIR="$(mktemp -d -t oft-e2e-telegram-settings-XXXXXX)"
LOG="$DATA_DIR/daemon.log"
CONFIG="$DATA_DIR/playwright.telegram-settings.config.mjs"

cleanup() {
  if [ -n "${DAEMON_PID:-}" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
    kill -- "-$DAEMON_PID" 2>/dev/null || kill "$DAEMON_PID" 2>/dev/null || true
    wait "$DAEMON_PID" 2>/dev/null || true
  fi
  rm -rf "$DATA_DIR"
}
trap cleanup EXIT INT TERM

# Isolate the journey from any operator-provided credential or provider token.
unset OFT_API_TOKEN OFT_TELEGRAM_BOT_TOKEN

export OFT_ENV="test"
export OFT_HOST="127.0.0.1"
export OFT_PORT="$PORT"
export OFT_DATA_DIR="$DATA_DIR"
export OFT_LOG_LEVEL="WARNING"
# Encrypted-file fallback; never write a plaintext key and never require a
# desktop keyring in CI. The fail backend keeps the journey off a user keyring.
export PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring"
# Generate a throwaway per-run master key (never an operator secret).
export OFT_MASTER_KEY="${OFT_MASTER_KEY:-$(head -c 32 /dev/urandom | base64 | tr -d '\n')}"

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
  echo "E2E Telegram settings daemon failed to start on $BASE_URL" >&2
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
  echo "E2E Telegram settings daemon did not write $DATA_DIR/run/browser-bootstrap" >&2
  cat "$LOG" >&2
  return 1
}

wait_for_health
wait_for_bootstrap

mkdir -p "$SCREENS_DIR" "$REPORT_DIR"

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
  projects: [{ name: 'telegram-settings', testMatch: /settings-telegram\.spec\.ts/ }],
}
EOF

export OFT_E2E_TELEGRAM_BASE_URL="$BASE_URL"
export OFT_E2E_TELEGRAM_BOOTSTRAP_FILE="$DATA_DIR/run/browser-bootstrap"
export OFT_E2E_TELEGRAM_DATA_DIR="$DATA_DIR"
export OFT_E2E_TELEGRAM_SCREENS_DIR="$SCREENS_DIR"
export OFT_E2E_TELEGRAM_REPORT_DIR="$REPORT_DIR"

echo "telegram settings e2e daemon: $BASE_URL · data: $DATA_DIR"
echo "screens: $SCREENS_DIR"

cd "$WEB_DIR"
set +e
./node_modules/.bin/playwright test \
  --config "$CONFIG" \
  e2e/settings-telegram.spec.ts "$@"
STATUS=$?
set -e
exit "$STATUS"
