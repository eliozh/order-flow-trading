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
