# ADR-0001: Runtime, language, and process topology

**Date**: 2026-11-06
**Status**: accepted
**Deciders**: architect (Stage 5); human approves at G3

## Context

The PRD requires a single-machine, long-running, single-operator system that ingests Binance USDⓈ-M and TradFi perpetual market data, computes order-flow metrics deterministically, calls three LLM providers, evaluates triggers, manages paper and live positions, serves a local Web UI and a full CLI, and keeps a read-only Telegram channel alive. The hard product constraint is structural: deterministic work belongs to code, subjective judgment belongs to the LLM, and the two meet only through validated structured contracts (PRD §模块职责边界).

The research snapshot (02-research.md) establishes the ecosystem fit: Python has the mature Binance connector, Parquet/DuckDB tooling, LLM SDKs, and Pydantic; the frontend needs a browser SPA because the design preview is a dense, interaction-heavy terminal; charting is constrained to Apache-2.0 Lightweight Charts.

## Decision

Use **Python 3.12** and a **modular monolith in one supervised daemon process** (`oftd`). The daemon hosts, in one asyncio event loop:

- FastAPI/uvicorn bound to `127.0.0.1:7611` (configurable), serving the REST API, SSE event stream, and the built SPA.
- APScheduler with a SQLite job store and durable `job_runs` state (ADR-0007).
- Market-data supervisor: Binance WS clients, REST snapshot pollers, archive backfill worker (ADR-0008).
- Deterministic metrics engine and opportunity/trigger evaluator (pure domain package).
- LLM orchestrator and provider adapters (ADR-0005).
- Risk gate, execution gateway, paper broker, and live Binance broker (ADR-0006).
- aiogram Telegram long-poll worker and delivery outbox.
- The only writer to SQLite and the only process that opens the DuckDB catalog for writes.

The CLI (`oft`) is a **separate process** and a client of the local API (ADR-0003). It never opens the live DuckDB catalog for writes and never talks to Binance directly. For `oft status` and `oft doctor` it may read SQLite read-only and Parquet directly when the daemon is down, so diagnostics work in every state as required by `design/flows.md` F12.

Package layout enforces the boundary:

```
src/oft/
  domain/        pure functions and state machines: metrics, opportunities, trigger evaluation,
                 risk caps, OMS state transitions, PnL. No network, no LLM, no clock side effects.
  ports/         Protocol definitions: MarketDataPort, LLMPort, BrokerPort, ClockPort,
                 NotificationPort, StorePort, SecretsPort.
  adapters/      binance_ws, binance_rest, archive, llm_openai, llm_anthropic, llm_deepseek,
                 paper_broker, live_broker, telegram, sqlite_store, parquet_store, keyring.
  application/   scan orchestrator, prompt builder, trigger service, position service,
                 risk gate, execution gateway, budget service, backfill service.
  api/           FastAPI routes, SSE, schemas generated from docs/contracts.
  cli/           Typer commands, formatting, exit codes, API client.
  web/           built SPA assets served by FastAPI.
tests/
  unit/ integration/ contract/ e2e/ replay/ fixtures/
```

Import rules are enforced in CI with `import-linter` contracts:

- `oft.domain` may import only the standard library and `pydantic`; it must not import `llm_*`, `binance_*`, `sqlite3`, `duckdb`, `fastapi`, `httpx`, or `aiogram`.
- `oft.adapters.llm_*` must not import `oft.application.execution_gateway`, `oft.adapters.live_broker`, `oft.adapters.paper_broker`, or any Binance module.
- `oft.api` must not import `oft.adapters.live_broker` or exchange modules; order submission is reachable only through `application.execution_gateway`.
- `oft.application.execution_gateway` is the only module allowed to import a broker adapter.

Process supervision is a systemd unit (`oft.service`) with `Restart=on-failure`, `RestartSec=5`, `TimeoutStopSec=30`, a dedicated user, `NoNewPrivileges=true`, `ProtectSystem=strict`, and `ReadWritePaths=$OFT_DATA`. Docker Compose is a documented alternative with `restart: unless-stopped`; macOS uses a launchd plist. The process runs in the foreground (`oft serve`) so any supervisor can own it.

## Alternatives Considered

### Alternative 1: Split data plane and control plane (two or more Python processes)

- **Pros**: a WS/ingest crash cannot take down the API; independent restarts; cleaner scaling story if the product ever grows.
- **Cons**: two writers to state, an IPC/queue (Redis/NATS/DB polling), reconciliation between planes, duplicate config, and two supervisor units. At 10 symbols and a 4h/8h cadence the load does not justify it. It increases the number of ways to double-submit an order or lose a scan checkpoint.
- **Why not**: operability and safety dominate. The single-user, single-machine constraint favors one process and one execution gateway. The architecture can split later behind the same `ports/` if measurements demand it.

### Alternative 2: TypeScript/Node end-to-end (Fastify + React + DuckDB node)

- **Pros**: one language across backend and frontend; excellent charting ecosystem; fast UI iteration.
- **Cons**: weaker data/quant ecosystem for tick aggregation, Parquet, and order-flow reference implementations; no official Binance futures connector with the same maturity; LLM/provider and Pydantic-equivalent schema tooling is less proven for strict structured outputs; DuckDB/Parquet bindings are less mature than Python's; the team would hand-roll more numerical code.
- **Why not**: the differentiating work is deterministic tick math and provider-neutral structured LLM output, both stronger in Python. The UI remains TypeScript.

### Alternative 3: Rust core with a TypeScript UI

- **Pros**: best runtime safety and performance; strong single-binary deployment.
- **Cons**: materially slower to build the data/LLM/Telegram surface; fewer ready SDKs; order-flow code must be written from scratch anyway; iteration speed is the binding constraint for a solo operator validating a hypothesis over 8 weeks.
- **Why not**: performance is not the bottleneck (10 symbols, 4h/8h decisions). Safety is better served by process and schema boundaries than by language choice at this stage.

## Consequences

### Positive

- One writer, one scheduler, one execution gateway: idempotency, risk gating, and kill-switch checks have a single choke point.
- Python gives the best available fit for `binance-connector-python`, pyarrow/DuckDB, Pydantic, and all three LLM SDKs.
- The CLI and Web UI share one API and one contract set; the CLI remains scriptable.
- The daemon can be supervised by systemd with a small operational surface.

### Negative

- A crash in any subsystem restarts the whole daemon. Startup must be fast and self-checking (ADR-0012).
- A single asyncio loop means a blocking call (e.g. a CPU-heavy footprint recompute or a synchronous SDK) can stall ingestion. Mitigation: CPU-heavy metric jobs run in a bounded `ProcessPoolExecutor`/thread pool; all network calls are async; SQLite writes are short and serialized through one writer task.
- The Web UI is served by the same process; a UI request cannot be isolated from trading. Mitigation: API handlers are read-mostly, writes are queued, and the event loop never performs blocking I/O.

### Risks

- **Risk**: SQLite/DuckDB lock contention between the daemon and CLI diagnostics. **Mitigation**: daemon owns writes; CLI opens SQLite read-only; Parquet is immutable; diagnostics never request a write lock.
- **Risk**: long-running Python process leaks or degrades. **Mitigation**: supervision, `resource` limits, memory/disk health checks, scheduled restart window documented in ADR-0012.
- **Risk**: import-boundary rules erode under delivery pressure. **Mitigation**: `import-linter` runs in CI and is a blocking check; the boundary is also enforced at runtime by capability tokens (ADR-0006).

## Compliance checks

- `docs/contracts/llm-output.schema.json` is the only shape an LLM may return; `llm-decision.schema.json` is the persisted envelope after validation. The LLM adapter has no execution imports.
- `ExecutionGateway` is the only path to `BrokerPort`; it requires a persisted `risk_verdict_id` and an approval record.
- The daemon is the sole writer; the CLI does not open DuckDB for writes.
- The process binds `127.0.0.1` by default; `--host` requires an explicit warning and is outside the supported single-user model.
