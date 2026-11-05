# ADR-0002: Storage — DuckDB/Parquet analytics plus SQLite transactional state

**Date**: 2026-11-06
**Status**: accepted
**Deciders**: architect (Stage 5); human approves at G3

## Context

The system stores three very different kinds of data:

1. **Bulk analytical data**: 10–30 GB compressed of `aggTrades`/`trades`, klines, book depth, metrics, and funding for a 10-symbol watchlist over 6 months (02-research.md §A3). Queries are columnar, range-scanned, and mostly append-only.
2. **Computed order-flow metrics**: CVD, footprint cells, volume profile, OI deltas, funding, liquidation summaries. These are derived, versioned, and must be reproducible for replay.
3. **Transactional application state**: scans, reports, opportunities, triggers, decisions, orders, fills, positions, budget ledger, prompt versions, audit events. These need ACID writes, uniqueness constraints, and small point reads.

A single store optimized for one kind is wrong for the other. The PRD also requires replay of historical inputs, recorded LLM provenance, and a 4-week paper record that must survive restarts.

## Decision

Use a **two-store architecture**:

- **Analytical store — Parquet files queried through DuckDB.** Raw archives are kept as downloaded (checksummed); curated event data and computed metrics are Parquet, partitioned by `data_type/symbol/date` (UTC), with hive-style partitioning. DuckDB is used in-process by the daemon for writes and by CLI/replay for read-only queries. No DuckDB server runs.
- **Transactional store — SQLite 3 (WAL mode)** at `$OFT_DATA/oft.db` for all application state, constraints, idempotency keys, and audit events.

The daemon is the only process that writes either store. Parquet part files are immutable once published; corrections create a new part and update the manifest, never rewrite in place.

### Directory layout

```
$OFT_DATA/                       # 0700
  config.toml
  secrets.enc                    # 0600; only if OS keyring is unavailable (ADR-0010)
  oft.db                         # SQLite WAL
  oft.db-wal / oft.db-shm
  duckdb/oft.duckdb              # catalog + views; rebuildable
  raw/um/daily/{type}/{SYMBOL}/{SYMBOL}-{type}-YYYY-MM-DD.zip(.CHECKSUM)
  raw/um/monthly/{type}/{SYMBOL}/{SYMBOL}-{type}-YYYY-MM.zip(.CHECKSUM)
  market/um/{type}/symbol={SYMBOL}/date=YYYY-MM-DD/part-{seq}.parquet
  metrics/{metric}/symbol={SYMBOL}/period={1m|5m|15m|1h|4h|8h|1d}/date=YYYY-MM-DD/part-{seq}.parquet
  footprint/symbol={SYMBOL}/period={5m|15m|1h|4h|8h|1d}/date=YYYY-MM-DD/part-{seq}.parquet
  liquidations/symbol={SYMBOL}/date=YYYY-MM-DD/part-{seq}.parquet
  knowledge/                     # compiled playbook artifacts (gitignored; ADR-0009)
  backups/
  logs/
  run/                           # pid, kill-switch sentinel, locks
```

`data_type` values: `agg_trades`, `trades`, `klines_1m`, `klines_1h`, `mark_price_klines`, `metrics_5m`, `funding`, `book_depth`. A `manifest.parquet` (or `manifest` table) records each published file: path, rows, bytes, checksum, source, `metric_version`, `input_manifest_hash`, created_at, status.

### SQLite schema (transactional state)

All IDs are opaque strings (ULID for new rows, human-readable prefixes for externally visible objects, e.g. `OPP-20261106-014`). All money/quantity values are stored as `TEXT` decimals or scaled integers—never `REAL`—so SQL and JSON agree exactly. All timestamps are ISO-8601 UTC text (millisecond precision by default). `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL`.

| Table | Primary key | Purpose / key columns |
|---|---|---|
| `schema_migrations` | `version` | Applied migration, checksum, applied_at |
| `system_state` | `key` | `environment`, `kill_switch`, `live_armed`, `scan_lock`, `clock_offset_ms`, `last_startup_at` |
| `paper_accounts` | `account_id` | started_at, reset_at, initial_balance, status |
| `symbols` | `symbol` | contract_type, underlying_type, base/quote, tick_size, step_size, min_notional, onboard_date, status, tradifi, updated_at |
| `watchlist` | `symbol` | starred, group (CRYPTO/TRADFI), sort_order, added_at |
| `config` | `key` | value_json, updated_at, source (DEFAULT/USER/CLI/API) |
| `secrets_meta` | `name` | backend (KEYRING/FILE/ENV), fingerprint, last4, created_at, rotated_at. **No secret value.** |
| `scans` | `scan_id` | period, window_start/window_end, scheduled_at, started_at, finished_at, trigger_source (SCHEDULED/MANUAL), status, stats_json, cost_usd, error_code |
| `scan_symbols` | `(scan_id, symbol)` | stage (QUEUED/DATA/METRICS/LLM/REPORT/DONE/FAILED/DEGRADED), started_at, finished_at, error_code, report_id, data_quality |
| `reports` | `report_id` | scan_id, symbol, timeframe, generated_at, metrics_manifest_json, llm_decision_id, narrative_ref, data_quality |
| `opportunities` | `opportunity_id` | symbol, side, state (OPEN/TRIGGERED/EXPIRED/INVALIDATED), zone_low/high, created_at, expires_at, conditions_json, invalidation_json, confidence, source_scan_id, source_report_id, source_decision_id, version |
| `opportunity_events` | `event_id` | opportunity_id, from_state, to_state, at, reason_code, trigger_id |
| `trigger_events` | `trigger_id` | opportunity_id, symbol, side, triggered_at, condition_results_json, snapshot_hash, status, risk_verdict_json, entry_decision_id, idempotency_key UNIQUE, human_action_json |
| `decisions` | `decision_id` | decision_type, trigger_id, position_id, scan_id, report_id, symbol, created_at, provider, model, prompt_id, prompt_version, prompt_hash, input_hash, schema_version, output_json, validation_status, cost_usd, latency_ms |
| `llm_calls` | `call_id` | decision_id, provider, model, attempt, request_hash, usage_json, unit_prices_json, cost_usd, price_version, latency_ms, http_status, error_code, created_at |
| `positions` | `position_id` | account_id, environment, symbol, side, state, size, entry_price, avg_entry, stop_price, targets_json, leverage, margin_mode, margin, risk_amount, opened_at, updated_at, closed_at, exit_price, realized_pnl, fees, funding, source_trigger_id, source_decision_id, version |
| `orders` | `order_id` | client_order_id UNIQUE, exchange_order_id, environment, account_id, position_id, symbol, side, type, tif, quantity, price, stop_price, reduce_only, status, filled_quantity, avg_fill_price, risk_verdict_id, approval_id, intent_hash, idempotency_key, created_at, submitted_at, updated_at, error_json |
| `fills` | `fill_id` | order_id, exchange_trade_id, environment, symbol, side, price, quantity, quote_quantity, fee, fee_asset, maker, filled_at, received_at, source |
| `trades` | `trade_id` | position_id, account_id, environment, symbol, side, opened_at, closed_at, entry_avg, exit_avg, size, gross_pnl, fees, funding, net_pnl, r_multiple, mae, mfe, signal_refs_json, outcome_tags_json, review_decision_id, data_quality |
| `idempotency_keys` | `(scope, key)` | request_hash, response_status, response_json, created_at, expires_at |
| `budget_ledger` | `record_id` | call_id, decision_id, occurred_at, provider, model, usage_json, unit_prices_json, cost_usd, price_version, unpriced, period_day/week/month |
| `budget_thresholds` | `scope` (DAY/WEEK/MONTH) | amount_usd, warn_pct, updated_at |
| `prompt_templates` | `prompt_id` | role (ANALYSIS/ENTRY_RECHECK/MANAGEMENT/REVIEW), name, created_at |
| `prompt_versions` | `(prompt_id, version)` | body, body_hash, variables_json, knowledge_artifact_hash, status (DRAFT/ACTIVE/ARCHIVED), created_at, created_by, reason |
| `audit_events` | `event_id` | at, actor_kind, actor_id, action, object_type, object_id, before_hash, after_hash, metadata_json, prev_hash, hash |
| `job_runs` | `run_id` | job_type, window_start, status, started_at, finished_at, error_code, lease_owner, lease_expires_at, UNIQUE(job_type, window_start) |
| `job_locks` | `lock_name` | owner, acquired_at, expires_at |
| `backfill_manifest` | `file_key` | data_type, symbol, period, date, source_url, checksum, checksum_verified, status, bytes, rows, downloaded_at, verified_at, error_code |
| `data_gaps` | `gap_id` | symbol, data_type, start, end, detected_at, source, resolved_at, resolution |
| `metric_versions` | `(metric_name, version)` | algorithm_hash, params_json, created_at, status |
| `metric_runs` | `run_id` | metric_name, version, symbol, period, window_start, window_end, input_manifest_hash, output_path, row_count, status, started_at, finished_at |
| `liquidation_coverage` | `symbol` | coverage_start, last_event_at, source (WS_SINCE_DEPLOYMENT), note |
| `reconciliation_runs` | `run_id` | environment, started_at, finished_at, status, drift_json |
| `telegram_deliveries` | `delivery_id` | event_type, chat_id, message_id, status (QUEUED/SENDING/SENT/DELIVERED/RETRYING/FAILED), attempts, last_error, created_at, delivered_at |
| `telegram_allowlist` | `chat_id` | label, added_at |
| `replay_runs` | `replay_id` | scenario, start/end, metric_version, prompt_version, status, created_at, summary_json |

Indexes: `positions(environment, state)`, `orders(environment, status, symbol)`, `orders(client_order_id)`, `trigger_events(status, triggered_at)`, `opportunities(state, symbol)`, `budget_ledger(period_day)`, `audit_events(at)`, `job_runs(job_type, window_start)`, `scan_symbols(scan_id, stage)`.

### Parquet schema conventions

- Event tables keep the Binance source fields verbatim plus `source`, `ingested_at`, `event_time`, and a `record_hash`.
- `agg_trades`: `agg_trade_id, price, quantity, first_trade_id, last_trade_id, transact_time, is_buyer_maker, source, ingested_at, record_hash`.
- `trades`: source trade fields plus `source`, `ingested_at`, `record_hash`.
- `klines`: `open_time, open, high, low, close, volume, close_time, quote_volume, count, taker_buy_volume, taker_buy_quote_volume, source, ingested_at`.
- `metrics_5m`: source metrics fields plus `sampling_quality` (`REGULAR` | `IRREGULAR_RESAMPLED`).
- `book_depth`: `timestamp, percentage, depth, notional` (aggregated bands only; not a ladder).
- Liquidations: `received_at, trade_time, symbol, side, order_type, time_in_force, original_quantity, price, average_price, order_status, last_fill_quantity, accumulated_fill_quantity, source_seq`. Coverage is recorded in `liquidation_coverage`; there is no historical backfill for USDⓈ-M.
- Metrics: one row per bar/level, with `metric_version`, `input_manifest_hash`, `computed_at`, `data_quality`, and `quality_reasons` repeated fields.
- Footprint: long format per `(bar_start, price)`: `bar_start, bar_end, price, bid_volume, ask_volume, delta, total_volume, imbalance_ratio, metric_version`.
- Volume profile: per window: `window_start, window_end, bin_size, price_low, price_high, volume, buy_volume, sell_volume, is_poc, is_value_area`; POC/VAH/VAL are also recorded as a summary row.

DuckDB views are created idempotently at daemon startup:

```sql
CREATE OR REPLACE VIEW v_agg_trades AS
SELECT * FROM read_parquet('$OFT_DATA/market/um/agg_trades/**/*.parquet',
                           hive_partitioning = true, union_by_name = true);
```

### Retention defaults (user-configurable)

| Data | Default retention | Rationale |
|---|---|---|
| Raw downloaded archives | 30 days after verified Parquet conversion | Re-downloadable; saves 10–30 GB |
| Curated `agg_trades`/`trades` | 24 months | Replay and footprint recomputation |
| `klines_1m`, `book_depth`, `metrics_5m` | 24 months | OI/DOM history and microstructure |
| Computed `1m`/`5m` metrics and footprint | 12 months | Expensive to recompute; covers review window |
| Computed `>=15m` metrics | 60 months | Small; long-horizon review |
| Liquidation events | Indefinite (since deployment) | No historical source exists |
| SQLite app state / ledger / audit | Indefinite | Small; required for 4-week gate and audit |
| Backups | 7 daily, 4 weekly, 12 monthly | Restore and upgrade safety |

Disk guard: warn at 80% of the configured data-volume budget, block backfill at 95%, never delete automatically without a logged retention job.

### Replay

Replay is a first-class read model, not a separate database:

1. `replay_runs` names a scenario (symbol set, window, `metric_version`, `prompt_version`, LLM stub mode).
2. A `ReplayClock` replaces the wall clock. A `ParquetEventSource` emits events in event-time order with no lookahead.
3. The **same** `domain.metrics` functions and the same `domain.trigger` evaluator run as live. The `metric_version` pinned in the run determines the algorithm; a new algorithm gets a new version, never an in-place change.
4. The paper broker runs with deterministic fill rules and a fixed seed; LLM calls use recorded outputs (by `input_hash`) or a stub. Replay writes `replay_*` tables/namespaces and never touches live positions, orders, or the budget ledger.
5. A replay summary compares triggers, entry decisions, fills, PnL, and data-quality outcomes against the original run; differences are reported, not auto-accepted.

## Alternatives Considered

### Alternative 1: SQLite for everything

- **Pros**: one file, ACID, zero operations, excellent for app state.
- **Cons**: row-oriented; scanning 10–30 GB of ticks for footprint/replay is slow; no columnar compression; large analytical queries block the write path; no native Parquet.
- **Why not**: it makes the core replay/backfill workload painful and risks lock contention with order writes.

### Alternative 2: ClickHouse (or TimescaleDB) as the only store

- **Pros**: fast aggregations, SQL, compression, concurrent reads.
- **Cons**: a server to operate and secure; memory-hungry; another upgrade/migration surface; overkill for one user and 10 symbols. TimescaleDB adds Postgres operations and a source-available license; ClickHouse adds a daemon and a second backup story.
- **Why not**: DuckDB gives columnar performance in-process with zero server operations. ClickHouse remains the escape hatch if single-machine latency degrades (documented trigger: p95 report query > 5 s at the final watchlist).

### Alternative 3: Embedded TSDB (QuestDB/InfluxDB)

- **Pros**: time-series ingest and downsampling built in.
- **Cons**: poor fit for relational ledger state; another server/format; SQL dialect differences; replay across Parquet archives still needed; operational burden.
- **Why not**: the workload is batch analytics plus a small relational ledger, not high-cardinality time-series monitoring.

### Alternative 4: Parquet only, no SQLite

- **Pros**: simplest storage.
- **Cons**: no transactions, no unique constraints, no idempotency, no safe concurrent reads during writes; order state would need a hand-rolled WAL.
- **Why not**: order/fill/position integrity requires ACID and uniqueness constraints. Parquet is a data lake, not a ledger.

## Consequences

### Positive

- Analytical scans use columnar Parquet and DuckDB vectorized execution; 10–30 GB is tractable on one machine.
- SQLite gives exact, constraint-backed state for orders, idempotency, positions, and audit.
- Replay reuses the same metric code and pinned versions; no separate backtest codebase.
- Backup is two simple artifacts: `oft.db` and the Parquet tree + manifest.

### Negative

- Two stores must stay consistent. The rule is: Parquet is derived/immutable; SQLite is authoritative for app state; a `metric_runs`/manifest row is written before a report references it.
- DuckDB is single-writer. The daemon owns it; CLI read-only diagnostics use Parquet or SQLite, not a write connection.
- Schema migrations now exist in two places: SQLite DDL and Parquet/metric-version conventions. Both are versioned (ADR-0012).
- Decimal values are strings, so application code must use `Decimal`, never `float`, at the boundary.

### Risks

- **Risk**: a crash between writing a Parquet part and its manifest row leaves an orphan. **Mitigation**: write temp part → fsync → atomic rename → insert manifest; startup scans for orphan parts and quarantines them.
- **Risk**: `aggTrades` volume grows faster than the retention budget. **Mitigation**: retention job, disk guard, monthly rollup, and the documented ClickHouse escape hatch.
- **Risk**: SQLite WAL grows during a long scan. **Mitigation**: short transactions, batched writes, periodic `wal_checkpoint(TRUNCATE)` outside scan windows.
- **Risk**: clock/timezone errors create wrong `date=` partitions. **Mitigation**: all partitioning uses UTC; startup checks clock drift; replay uses event time.

## Data model notes

- **Environment is explicit everywhere**: `PAPER` and `LIVE` rows never mix. Paper reset creates a new `paper_accounts` row; the Live gate counts from `paper_started_at` and resets on reset (design assumption 4).
- **Provenance is relational**: a report points to a `decision_id`; a decision points to `llm_calls`; a trigger points to a snapshot hash and a `risk_verdict`; an order points to a `risk_verdict_id` and `approval_id`.
- **No raw LLM prompt bodies in SQLite by default**: store `input_hash`, redacted input summary, prompt version, and output. Full prompts can be exported to a user-controlled file for debugging with secrets redacted.
- **Audit hash chain**: `audit_events.prev_hash` + `hash` makes silent edits detectable. The chain is verified on startup and by `oft doctor`.
