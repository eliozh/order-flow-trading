# Order Flow Trading

Single-operator, local **paper-trading terminal**. The Python 3.12 `oft`
package ingests Binance USDⓈ-M market data, computes deterministic order-flow
metrics and triggers, asks an LLM for analysis, and manages simulated positions.
A React/TypeScript/Vite shell and a Typer CLI share one local FastAPI API.

Live order submission does not exist: `application/execution_gateway.py`
refuses every intent whose environment is not `PAPER`. Binance credentials are
accepted by the settings model but are not used by the shipped market-data
path; the public WS and REST endpoints need no key.

## What runs

```
Binance WS/REST -> normalized rows -> Parquet + SQLite manifest -> metrics
  -> opportunities -> LLM analysis -> triggers -> entry re-check
  -> paper OMS/execution -> position monitor -> management
  -> journal, reports, audit chain -> API/CLI/web/Telegram
```

- **Market data** — live WS ingestion on `wss://fstream.binance.com` (aggTrade,
  kline, markPrice, forceOrder) plus public REST on
  `https://fapi.binance.com` for `exchangeInfo` and 48 h `aggTrades` gap
  recovery. Archived daily files come from `https://data.binance.vision` when a
  backfill or replay uses the HTTPS archive source.
- **Decisions** — deterministic metrics, opportunity and trigger evaluation,
  LLM analysis for the `analysis` / `entry_recheck` / `management` / `review`
  roles, and a code-only entry re-check. Provider adapters exist for OpenAI,
  Anthropic, and DeepSeek, plus a deterministic `stub`. The budget ledger
  records every call and alerts on thresholds; it never stops a call.
- **Execution** — paper account, paper OMS, position monitor, management,
  journal, exports, reports, and the append-only audit chain. Paper only.
- **Telegram** — a read-only channel with exactly seven commands (`/status`,
  `/positions`, `/opportunities`, `/report [SYMBOL]`, `/budget`, `/watchlist`,
  `/help`). It requires an allowlisted chat **and** user. Write-like tokens are
  refused. `OFT_ENV=test` selects the deterministic, network-free
  `TelegramFake`.
- **Local surfaces** — FastAPI on `127.0.0.1:7611` (bearer token or bounded
  session, loopback Host allowlist) and the Vite web shell.

## `daemon start` vs `serve`

```bash
uv run oft daemon start          # full runtime
uv run oft serve                 # HTTP API only
```

`oft daemon start` runs the API, the scan and trigger schedulers, the position
monitor and management cadence, live WS ingestion, and the Telegram runtime.
`oft serve` runs only the FastAPI app: no schedulers, no ingestion, and no
Telegram. Because `/ready` requires a fresh scheduler heartbeat outside
`OFT_ENV=test`, an API-only `oft serve` reports not-ready in real environments.

Both bind `127.0.0.1:7611` by default. `oft daemon start` accepts only
`--host`, `--port`, `--allow-non-loopback`, `--replay-file`, and `--symbols`.

## Capability matrix

| Capability | Shipped behavior |
|---|---|
| Paper trading | Yes — paper account, OMS, position management, journal |
| Live trading | Absent — non-`PAPER` intents are refused |
| Binance market data | Public WS + REST ingestion; no API key used |
| LLM providers | OpenAI, Anthropic, DeepSeek, deterministic stub |
| Telegram | Read-only, seven commands, allowlisted chat + user |
| Local API / web shell | Authenticated read and write surfaces |
| Backups | Manual `oft backup` / `oft restore`; automatic pre-migration and pre-restore snapshots |

External endpoints contacted (only when the corresponding feature is used):

| Endpoint | Used by |
|---|---|
| `wss://fstream.binance.com` | Live market-data ingestion |
| `https://fapi.binance.com` | Exchange info and aggTrade gap recovery |
| `https://data.binance.vision` | Archive backfill / replay (HTTPS source) |
| `https://api.openai.com` | LLM provider `openai` |
| `https://api.anthropic.com` | LLM provider `anthropic` |
| `https://api.deepseek.com` | LLM provider `deepseek` |
| `https://api.telegram.org` | Telegram channel, when configured |

## Secrets

No secret is needed for paper market data, local scans without an LLM, or the
loopback API. The API bearer token is generated into
`$OFT_DATA/run/api-token` (0600) on first start unless `OFT_API_TOKEN` is set.

| Secret | When required |
|---|---|
| `OFT_TELEGRAM_BOT_TOKEN`, or the token stored with `oft telegram token set --stdin` | Telegram channel |
| `OFT_OPENAI_API_KEY` / `OFT_ANTHROPIC_API_KEY` / `OFT_DEEPSEEK_API_KEY` | The selected LLM provider (`stub` needs none) |
| `OFT_MASTER_KEY` (min 32 chars) | Only for the encrypted-file secret fallback when no OS keyring is available |
| `OFT_BINANCE_API_KEY` / `OFT_BINANCE_API_SECRET` | Not used by the shipped code; reserved for a future live path |

Stored secrets prefer the OS keyring. Without a usable keyring, the encrypted
file needs `OFT_MASTER_KEY`; otherwise key writes are refused. Secrets are
write-only through the API/CLI, masked to a fixed prefix plus last four
characters, and redacted from logs.

Read-only surfaces: the Telegram channel, `GET /health`, `GET /ready`, and the
CLI diagnostics (`oft status`, `oft doctor`, `oft daemon status`). The
authenticated web shell and API expose write surfaces — scans and triggers,
settings and risk caps, watchlist stars, the kill switch, and paper
position monitor/manage actions.

## Setup

```bash
cp .env.example .env
uv sync
npm --prefix web install
```

Python 3.12, [uv](https://docs.astral.sh/uv/), and Node.js 20+/npm are required.
Settings come from `OFT_*` environment variables; `OFT_DATA` and `OFT_DATA_DIR`
are both accepted for the data directory. Safe defaults are paper environment,
loopback binding, and no credentials.

## CLI

```bash
uv run oft status
uv run oft doctor
uv run oft backup [--out DIR] [--json]
uv run oft restore <file> [--yes] [--json] [--no-snapshot]
uv run oft scan run --symbol BTCUSDT --metrics-only --json
uv run oft scan status --json
```

`oft status` and `oft doctor` work without the daemon and read SQLite
read-only. Manual scans and watchlist/position actions go through the daemon
API, so they need a running daemon. `oft config set`, `oft risk set`,
`oft init`, `oft backup`, and `oft restore` write SQLite directly; `oft backup`
writes a verified `VACUUM INTO` copy plus a SHA-256
sidecar under `$OFT_DATA/backups/`; `oft restore` refuses while the daemon runs
and keeps a pre-restore safety snapshot. Backups are not encrypted, and daily
scheduling/retention remain operator work.

## Telegram (read-only)

```bash
printf '%s\n' "$TELEGRAM_BOT_TOKEN" | uv run oft telegram token set --stdin
uv run oft telegram status
uv run oft telegram allowlist add --chat-id 1001 --user-id 2002
uv run oft telegram toggles show
uv run oft telegram watchdog --once
```

Only an explicitly allowlisted chat **and** user may query. Write-like tokens
(`/approve`, `/accept`, `/reject`, `/cancel`, `/buy`, `/sell`, `/close`) return
a fixed read-only refusal. Prose that merely mentions a command is ignored. See
[`docs/runbooks/telegram.md`](docs/runbooks/telegram.md).

## Web shell

```bash
npm --prefix web run dev
```

Vite proxies `/api`, `/health`, and `/ready` to `http://127.0.0.1:7611`
(override with `VITE_DEV_API_TARGET`). The built SPA is served by FastAPI when
`web/dist` exists.

## Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
uv run pytest --cov=oft --cov-fail-under=80
npm --prefix web run lint
npm --prefix web run typecheck
npm --prefix web run test
npm --prefix web run build
```

## Layout

```
src/oft/
  domain/        pure models, metrics, opportunities, risk, OMS state machines
  ports/         Protocol definitions (market data, store, clock, broker, LLM)
  adapters/      Binance WS/REST, archive, LLM providers, SQLite/Parquet,
                 keyring, Telegram Bot API
  application/   scans, triggers, entry, paper OMS, positions, management,
                 journal, reports, budget, health/readiness/doctor, scheduler
  api/           FastAPI app, auth/CSRF, envelope, errors, routes
  cli/           Typer command tree, API client, output, daemon lifecycle
web/             React + TypeScript + Vite shell
tests/           unit, integration, contract, e2e, replay, fixtures
```
