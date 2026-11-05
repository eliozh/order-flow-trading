# Order Flow Trading

Single-operator, local order-flow trading terminal. This repository is at the
walking-skeleton stage: typed configuration, structured logging, a FastAPI
status surface, a Typer CLI, and a React/Vite shell all share one fixture-backed
symbol universe. No exchange connection, LLM call, or order path exists yet.

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

## Run the API

```bash
uv run uvicorn oft.api.app:create_app --factory --host 127.0.0.1 --port 8080
```

- `GET /health` — liveness
- `GET /api/v1/status` — envelope-format status built from the fixture universe

## Run the CLI

```bash
uv run oft status
uv run oft status --json
```

The CLI works without a running daemon and calls the same application status
function as the API.

## Run the web shell

```bash
npm --prefix web run dev
```

Vite proxies `/api` and `/health` to `http://127.0.0.1:8080`
(override with `VITE_DEV_API_TARGET`).

## Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
npm --prefix web run lint
npm --prefix web run test
npm --prefix web run build
```

## Layout

```
src/oft/
  domain/        pure models and functions
  ports/         Protocol definitions
  adapters/      fixture market data, SQLite/Parquet placeholders
  application/   status and health services
  api/           FastAPI app, envelope, routes
  cli/           Typer command tree
web/             React + TypeScript + Vite shell
tests/           pytest unit and integration tests
```
