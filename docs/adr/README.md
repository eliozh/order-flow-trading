# Architecture Decision Records

Status legend: `proposed` = awaiting G3 approval; `accepted` = approved at G3; `superseded` = replaced by a later ADR.

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [0001](0001-runtime-and-process-topology.md) | Python modular monolith in one supervised daemon | accepted | 2026-11-06 |
| [0002](0002-storage-duckdb-parquet-sqlite.md) | DuckDB/Parquet analytical store plus SQLite transactional state | accepted | 2026-11-06 |
| [0003](0003-local-api-and-cli-contract.md) | Local REST + SSE API with the CLI as a first-class client | accepted | 2026-11-06 |
| [0004](0004-frontend-and-charting.md) | React/Vite SPA with Lightweight Charts custom overlays | accepted | 2026-11-06 |
| [0005](0005-llm-provider-abstraction.md) | Provider-neutral LLM adapter with strict structured outputs | accepted | 2026-11-06 |
| [0006](0006-execution-and-oms.md) | Paper/live OMS behind one risk-gated execution gateway | accepted | 2026-11-06 |
| [0007](0007-scheduler-and-jobs.md) | UTC-anchored APScheduler jobs with durable run ledger | accepted | 2026-11-06 |
| [0008](0008-market-data-pipeline.md) | Live WS ingestion, archive backfill, gap detection, versioned metrics | accepted | 2026-11-06 |
| [0009](0009-knowledge-pipeline.md) | Compiled order-flow playbook with versioned prompt injection | accepted | 2026-11-06 |
| [0010](0010-security-and-secrets.md) | Local-only security model, secret handling, and threat model | accepted | 2026-11-06 |
| [0011](0011-testing-and-replay.md) | Deterministic fixtures, LLM stubs, and event-time replay | accepted | 2026-11-06 |
| [0012](0012-observability-and-operations.md) | Structured logs, supervision, backup/restore, and upgrades | accepted | 2026-11-06 |

## Stack recommendation

One Python 3.12 daemon (`oftd`) hosting FastAPI, APScheduler, the Binance WS/REST adapters, the deterministic metrics engine, the LLM orchestrator, the risk gate, the paper/live OMS, and the aiogram Telegram bot. Analytics live in Parquet queried through DuckDB; transactional state lives in SQLite (WAL). The CLI is a Typer client of the local API. The Web UI is a React/TypeScript/Vite SPA served by the same daemon, with TradingView Lightweight Charts and custom footprint/volume-profile overlays.

## Contract index

Machine-readable contracts live in [`docs/contracts/`](../contracts/README.md). All are JSON Schema draft 2020-12; the LLM decision envelope is additionally provider-safe (every object closed, every property required, no tool access).

## Decision sequence

1. ADR-0001 fixes the process boundary that makes every later safety claim possible: one writer, one execution gateway, one scheduler.
2. ADR-0002 decides where truth lives; the split between analytical and transactional storage is the basis of replay.
3. ADR-0003 defines the only ways a human or the CLI can change state.
4. ADRs 0004–0009 define the replaceable edges: UI, LLM, execution, schedule, data, knowledge.
5. ADRs 0010–0012 make the system operable and testable without weakening the code/LLM boundary.

## Review notes

- No ADR is accepted by this document. G3 is the approval gate.
- The approved preview (`design/preview/index.html`, G2) is treated as a UI/flow constraint, not as an architecture. One preview content drift is recorded in the handoff: example model IDs in the preview predate the research snapshot and are configuration data, not contracts.
- The PRD's hard boundary — deterministic belongs to code, subjective belongs to LLM — is enforced in ADR-0001 (package/import rules), ADR-0005 (output-only LLM), ADR-0006 (risk token + gateway), and ADR-0010 (threat model).
