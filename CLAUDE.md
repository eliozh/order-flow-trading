# Order Flow Trading

Local order-flow trading terminal. Python 3.12 `oft` package (src layout) plus
a React/TypeScript/Vite client under `web/`.

## Commands

```bash
# Install
uv sync
npm --prefix web install

# Dev daemon (API + schedulers + live ingestion + Telegram runtime;
# loopback only, 127.0.0.1:7611)
uv run oft daemon start

# Dev API only (no schedulers, ingestion, or Telegram)
uv run oft serve

# Dev web shell (proxies /api, /health, and /ready to 127.0.0.1:7611;
# changeOrigin rewrites Host so the daemon's loopback Host allowlist accepts it)
npm --prefix web run dev

# CLI
uv run oft status
uv run oft status --json
uv run oft backup [--out DIR] [--json]
uv run oft restore <file> [--yes] [--json]

# Telegram (read-only; the token is accepted only on stdin)
uv run oft telegram status
uv run oft telegram test
uv run oft telegram receipts
printf '%s\n' "$OFT_TELEGRAM_BOT_TOKEN" | uv run oft telegram token set --stdin
uv run oft telegram token delete
uv run oft telegram allowlist show
uv run oft telegram allowlist add --chat-id 1001 --user-id 2002
uv run oft telegram allowlist remove --chat-id 1001
uv run oft telegram toggles show
uv run oft telegram toggles set --no-scan-report --trigger
uv run oft telegram watchdog --once

# Test
uv run pytest
uv run pytest tests/e2e/test_telegram_channel.py
uv run pytest tests/integration/test_telegram_secret_leakage.py
npm --prefix web run test
make ci

# Lint
uv run ruff check .
uv run ruff format --check .
uv run lint-imports
npm --prefix web run lint

# Typecheck
uv run mypy
npm --prefix web run typecheck

# Production web build (served by FastAPI when web/dist exists)
npm --prefix web run build
```

## Browser automation

For ad-hoc browser verification of the running web shell (UI smoke test,
screenshot, click-path), use the `agent-browser` CLI rather than a one-off
script. Before driving it, load the version-matched guide:
`agent-browser skills get core`.

- Start each task with its own session (`agent-browser session id --scope
  worktree --prefix task`) and pass `--session <name>` on every command; the
  default session is shared machine-wide.
- Point it at the running dev server, e.g. `agent-browser open
  http://127.0.0.1:<dev-port> --session <name>`, then `snapshot -i` for `@eN`
  refs, and re-snapshot after the page changes.
- Keep screenshots and recordings out of the repo (`/tmp/...`), and
  `agent-browser close --session <name>` when done.
- This is for manual exploration only. Committed E2E coverage stays in
  Playwright (`web/playwright.config.ts`, wired into CI) — do not replace
  Playwright tests with agent-browser runs.

## Pull requests

- Write the PR description as plain Markdown. Never wrap the whole body in a
  triple-backtick fence: an unclosed fence makes GitHub read the body as code,
  so `Closes #N` keywords are ignored and the issue is never linked or closed.
- Pass the body with `gh pr create --body-file <path>` (or a heredoc), and
  check that fences are balanced before submitting.

## Notes

- Settings come from `OFT_*` environment variables (see `.env.example`); safe
  defaults are paper environment, loopback binding, and no credentials. Stored
  secrets prefer the OS keyring; the encrypted-file fallback requires
  `OFT_MASTER_KEY` (min 32 chars), and key writes are refused when neither is
  available.
- Execution is paper-only: `application/execution_gateway.py` refuses every
  intent whose environment is not `PAPER`, and no live order-placement code
  exists. Market data is the public Binance USDⓈ-M WS/REST feed (no key used);
  LLM provider adapters exist for OpenAI, Anthropic, DeepSeek, and the
  deterministic stub. The budget ledger alerts but never stops a call.
- `oft daemon start` is the full runtime (API + schedulers + live ingestion +
  Telegram runtime); `oft serve` is the API-only shell.
- One fixture-backed symbol universe (`src/oft/fixtures/symbol-universe.json`)
  is shared by the API and CLI; live ingestion follows starred symbols.
- Telegram is a read-only channel: seven bot commands (`/status`, `/positions`,
  `/opportunities`, `/report [SYMBOL]`, `/budget`, `/watchlist`, `/help`) plus
  read-only pushes. Write-like tokens are refused, the bot token is stdin-only
  and masked on every surface, and `OFT_ENV=test` selects the deterministic
  network-free `TelegramFake`.
- `oft telegram watchdog --once` is the one-shot host check for cron/systemd
  `OnFailure=`; see `docs/runbooks/telegram.md`.
- `oft backup` / `oft restore` are manual; backups are not encrypted at rest,
  and daily scheduling/retention are not automated.
