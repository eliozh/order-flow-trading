# Order Flow Trading

Single-operator, local order-flow trading terminal. This repository is at the
walking-skeleton stage: typed configuration, structured logging, a FastAPI
daemon, a Typer CLI, and a React/Vite shell all share one deterministic,
fixture-backed scan path. No exchange connection, LLM call, or order path
exists yet.

One fixture-backed `aggTrade` sample flows through:

```
CSV -> normalized rows -> Parquet part + SQLite manifest -> delta/CVD -> API/CLI/UI
```

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Node.js 20+ and npm (web client only)

## Setup

```bash
cp .env.example .env
uv sync
npm --prefix web install
```

Settings come from `OFT_*` environment variables; `OFT_DATA` and
`OFT_DATA_DIR` are both accepted for the data directory. Defaults are paper
environment, loopback binding, and no credentials.

## Run the daemon

```bash
uv run oft serve
```

Binds `127.0.0.1:7611` by default.

- `GET /health` — liveness (unauthenticated)
- `GET /ready` — readiness with named checks (unauthenticated)
- `GET /api/v1/status` — envelope status (bearer token required)
- `POST /api/v1/scans`, `GET /api/v1/scans/{scan_id}` — fixture scans

The daemon writes a bearer token to `$OFT_DATA/run/api-token` (0600) on first
start; `OFT_API_TOKEN` overrides it. The built SPA under `/system` reads only
the unauthenticated health/readiness surfaces.

## Run the CLI

```bash
uv run oft status
uv run oft status --json
uv run oft doctor
uv run oft scan run --fixture tests/fixtures/market/btcusdt_agg-trades_sample.csv \
  --symbol BTCUSDT --period 4h --metrics-only --json
uv run oft scan status <scan-id> --json
```

`oft status` and `oft doctor` work without the daemon (reporting `OFFLINE` /
`DEGRADED`) and read SQLite read-only. `oft scan run` uses the daemon when it
is up and falls back to the same application service for local fixture runs.
The fixture's golden delta is `1.50` (buy `4.25`, sell `2.75`).

## Telegram channel (read-only)

The optional Telegram bot is a read-only notification and query surface. It
accepts exactly seven executable commands:

```
/status  /positions  /opportunities  /report [SYMBOL]  /budget  /watchlist  /help
```

Write-like tokens (`/approve`, `/accept`, `/reject`, `/cancel`, `/buy`, `/sell`,
`/close`) are recognized only to return the fixed read-only refusal. Prose that
merely mentions a command is ignored, and only an explicitly allowlisted chat
**and** user may query.

```bash
# The token is accepted only on stdin: no argv, no reveal command.
printf '%s\n' "$TELEGRAM_BOT_TOKEN" | uv run oft telegram token set --stdin
uv run oft telegram status
uv run oft telegram allowlist show
uv run oft telegram allowlist add --chat-id 1001 --user-id 2002
uv run oft telegram toggles show
uv run oft telegram toggles set --no-scan-report --trigger
uv run oft telegram test
uv run oft telegram receipts --limit 20
uv run oft telegram watchdog --once   # one-shot cron/systemd check
```

`status`, `allowlist`, `toggles`, `receipts`, and `watchdog` never print the
token; only the fixed mask and last four characters are rendered. With
`OFT_ENV=test` the composition root selects the deterministic in-process
`TelegramFake`, so these commands and the test suite never touch
`api.telegram.org`. See [`docs/runbooks/telegram.md`](docs/runbooks/telegram.md)
for the cron/systemd watchdog examples.

## Run the web shell

```bash
npm --prefix web run dev
```

Vite proxies `/api`, `/health`, and `/ready` to `http://127.0.0.1:7611`
(override with `VITE_DEV_API_TARGET`). `/system` is read-only at 375px.

## Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=oft --cov-fail-under=80
npm --prefix web run lint
npm --prefix web run typecheck
npm --prefix web run test -- --run
npm --prefix web run build
```

## Layout

```
src/oft/
  domain/        pure models, delta/CVD metrics, events
  ports/         Protocol definitions (market_data, store, clock)
  adapters/      fixture market data, SQLite store, Parquet store
  application/   status, health/readiness/doctor, fixture scan
  api/           FastAPI app, auth/CSRF, envelope, errors, routes
  cli/           Typer command tree + API client + output
web/             React + TypeScript + Vite shell
tests/           unit, integration, contract, e2e, fixtures
```
