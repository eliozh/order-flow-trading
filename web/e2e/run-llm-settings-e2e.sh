#!/usr/bin/env bash
# Run the slice-35 `/settings/llm` browser journeys against one real daemon.
#
# Starts exactly one `oft serve` with a temporary OFT_DATA dir on a free port.
# OFT_MASTER_KEY is set so the encrypted-file secret fallback is available in a
# headless environment (no OS keyring), and PYTHON_KEYRING_BACKEND=fail keeps the
# journey off any operator keyring.
#
# A local fake Anthropic transport (`fake-llm-provider.mjs`) is started first and
# the daemon's `OFT_LLM_ANTHROPIC_BASE_URL` points at it, so the capability probe
# and any scheduled analysis call never reach a real provider. Only fake keys
# (`sk-test-SENTINEL-35-*`) are used. The daemon, the fake provider, and the temp
# dir are stopped/removed on exit.
#
# All LLM cases from `settings.spec.ts` run (titles start with `LLM `).
# Screenshots and axe JSON land under
# `.claude/ecc-workflow/screens/slice-35-e2e/`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT="$(cd "$WEB_DIR/.." && pwd)"
DEFAULT_SCREENS_DIR="$ROOT/.claude/ecc-workflow/screens/slice-35-e2e"
SCREENS_DIR="${OFT_E2E_LLM_SCREENS_DIR:-${OFT_E2E_SETTINGS_SCREENS_DIR:-$DEFAULT_SCREENS_DIR}}"
REPORT_DIR="${OFT_E2E_LLM_REPORT_DIR:-$SCREENS_DIR/report}"

RESERVED_PORTS="7622 7632 7642 7652 7662"

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
  local candidate="${OFT_E2E_LLM_PORT:-7653}"
  if is_reserved_port "$candidate"; then
    echo "E2E LLM port $candidate is reserved for another suite" >&2
    return 1
  fi
  if ! port_in_use "$candidate"; then
    printf '%s\n' "$candidate"
    return 0
  fi
  local next
  for next in $(seq 7654 7699); do
    if is_reserved_port "$next"; then
      continue
    fi
    if ! port_in_use "$next"; then
      printf '%s\n' "$next"
      return 0
    fi
  done
  echo "no free E2E LLM port in 7654-7699" >&2
  return 1
}

pick_fake_port() {
  local next
  for next in $(seq 7700 7799); do
    if ! port_in_use "$next"; then
      printf '%s\n' "$next"
      return 0
    fi
  done
  echo "no free fake-provider port in 7700-7799" >&2
  return 1
}

if [ ! -f "$WEB_DIR/dist/index.html" ]; then
  echo "web/dist/index.html is missing; build it with 'npm --prefix web run build' first" >&2
  exit 1
fi

PORT="$(pick_port)"
FAKE_PORT="$(pick_fake_port)"
BASE_URL="http://127.0.0.1:${PORT}"
FAKE_BASE_URL="http://127.0.0.1:${FAKE_PORT}"
DATA_DIR="$(mktemp -d -t oft-e2e-llm-XXXXXX)"
LOG="$DATA_DIR/daemon.log"
FAKE_LOG="$DATA_DIR/fake-llm.log"
CONFIG="$DATA_DIR/playwright.llm.config.mjs"

cleanup() {
  if [ -n "${DAEMON_PID:-}" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
    kill -- "-$DAEMON_PID" 2>/dev/null || kill "$DAEMON_PID" 2>/dev/null || true
    wait "$DAEMON_PID" 2>/dev/null || true
  fi
  if [ -n "${FAKE_PID:-}" ] && kill -0 "$FAKE_PID" 2>/dev/null; then
    kill -- "-$FAKE_PID" 2>/dev/null || kill "$FAKE_PID" 2>/dev/null || true
    wait "$FAKE_PID" 2>/dev/null || true
  fi
  rm -rf "$DATA_DIR"
}
trap cleanup EXIT INT TERM

setsid node "$SCRIPT_DIR/fake-llm-provider.mjs" "$FAKE_PORT" >"$FAKE_LOG" 2>&1 &
FAKE_PID=$!

# Isolate the journey from any operator-provided credential or provider in the
# caller's environment: only the fake keys stored through the API are used.
unset OFT_OPENAI_API_KEY OFT_ANTHROPIC_API_KEY OFT_DEEPSEEK_API_KEY OFT_API_TOKEN
unset OFT_LLM_PROVIDER OFT_LLM_ALLOW_CUSTOM_BASE_URL

export OFT_ENV="test"
export OFT_HOST="127.0.0.1"
export OFT_PORT="$PORT"
export OFT_DATA_DIR="$DATA_DIR"
export OFT_LOG_LEVEL="WARNING"
# Use the encrypted-file fallback; never write plaintext and never require a
# desktop keyring in CI. The fail backend makes the keyring unavailable so the
# journey exercises the documented fallback instead of mutating a user keyring.
export PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring"
export OFT_MASTER_KEY="e2e-slice-35-master-key-0123456789ab"
# Every provider call from the daemon goes to the local fake, never a real
# provider. The fake keys are never valid anywhere else.
export OFT_LLM_ANTHROPIC_BASE_URL="$FAKE_BASE_URL"
export OFT_LLM_MODEL_ANALYSIS="claude-e2e-analysis"
export OFT_LLM_MODEL_ENTRY_RECHECK="claude-e2e-entry"
export OFT_LLM_MODEL_MANAGEMENT="claude-e2e-management"
export OFT_LLM_MODEL_REVIEW="claude-e2e-review"

wait_for_fake() {
  local attempt
  for attempt in $(seq 1 100); do
    if curl -fsS "$FAKE_BASE_URL/__health" >/dev/null 2>&1; then
      return 0
    fi
    if ! kill -0 "$FAKE_PID" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
  echo "fake LLM provider failed to start on $FAKE_BASE_URL" >&2
  cat "$FAKE_LOG" >&2
  return 1
}

wait_for_fake

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
  echo "E2E LLM daemon failed to start on $BASE_URL" >&2
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
  echo "E2E LLM daemon did not write $DATA_DIR/run/browser-bootstrap" >&2
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
  projects: [{ name: 'llm-settings', testMatch: /settings\.spec\.ts/ }],
}
EOF

export OFT_E2E_SETTINGS_BASE_URL="$BASE_URL"
export OFT_E2E_SETTINGS_BOOTSTRAP_FILE="$DATA_DIR/run/browser-bootstrap"
export OFT_E2E_SETTINGS_DATA_DIR="$DATA_DIR"
export OFT_E2E_SETTINGS_SCREENS_DIR="$SCREENS_DIR"
export OFT_E2E_SETTINGS_REPORT_DIR="$REPORT_DIR"
export OFT_E2E_LLM_FAKE_BASE_URL="$FAKE_BASE_URL"

echo "llm settings e2e daemon: $BASE_URL · data: $DATA_DIR"
echo "fake llm provider: $FAKE_BASE_URL"
echo "screens: $SCREENS_DIR"

cd "$WEB_DIR"
set +e
./node_modules/.bin/playwright test \
  --config "$CONFIG" \
  --grep "${OFT_E2E_LLM_GREP:-LLM }" \
  e2e/settings.spec.ts "$@"
STATUS=$?
set -e
exit "$STATUS"
