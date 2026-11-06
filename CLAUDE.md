# Order Flow Trading

Local order-flow trading terminal. Python 3.12 `oft` package (src layout) plus
a React/TypeScript/Vite client under `web/`.

## Commands

```bash
# Install
uv sync
npm --prefix web install

# Dev API (loopback only, 127.0.0.1:7611)
uv run oft serve

# Dev web shell (proxies /api, /health, and /ready to 127.0.0.1:7611)
npm --prefix web run dev

# CLI
uv run oft status
uv run oft status --json

# Test
uv run pytest
npm --prefix web run test

# Lint
uv run ruff check .
uv run ruff format --check .
npm --prefix web run lint

# Typecheck
uv run mypy
npm --prefix web run typecheck

# Production web build (served by FastAPI when web/dist exists)
npm --prefix web run build
```

## Notes

- Settings come from `OFT_*` environment variables (see `.env.example`); safe
  defaults are paper environment, loopback binding, and no credentials.
- The walking skeleton has one fixture-backed symbol universe
  (`src/oft/fixtures/symbol-universe.json`) shared by the API and CLI.
- No exchange, LLM, Telegram, or order code exists yet.
