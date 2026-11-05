# ADR-0007: Scheduler and scan/trigger job model

**Date**: 2026-11-06
**Status**: accepted
**Deciders**: architect (Stage 5); human approves at G3

## Context

The system runs continuously on one machine and must:

- scan all starred symbols every 4h/8h (configurable), aligned to the decision period;
- evaluate open opportunity triggers continuously or at a short cadence, independent of the scan;
- request position-management decisions on a schedule and on conditions;
- ingest live data continuously and run archive backfill;
- deliver Telegram pushes with retries;
- catch up after downtime without duplicating scans or orders;
- prevent overlapping runs and respect Binance/LLM rate limits;
- recover per-symbol progress after a crash.

The design requires a manual scan trigger, a scan history, per-symbol stages (`QUEUED → DATA → METRICS → LLM → REPORT → DONE`), and a refusal when a scan is already running (F03).

## Decision

Use **APScheduler `AsyncIOScheduler`** for timers combined with a **durable job ledger in SQLite** (`job_runs`, `job_locks`) for correctness. APScheduler provides the clock; SQLite provides the truth.

### Job catalog

| Job | Cadence | Overlap policy | Catch-up policy |
|---|---|---|---|
| `scan` | At each 4h/8h UTC boundary + 0–30 s jitter | `max_instances=1`, `coalesce=True`; unique `(job_type, window_start)` | On startup, if the latest missed window is within one period and no newer scan exists, run it once; older missed windows are recorded `MISSED`, not replayed |
| `trigger_evaluate` | Every 15 s (configurable 5–60 s) | Single instance; skipped tick if the previous is still running | No catch-up; evaluate against the current snapshot |
| `position_manage` | At each period boundary, staggered after scans; plus event-driven on configured price/OI/funding conditions | Per-position lock; one decision at a time | Run once for open positions after a restart |
| `reconcile_live` | Every 60 s (live only) | Single instance | Mandatory on startup before any live order |
| `health_check` | Every 30 s | Single instance | Immediate on startup |
| `telegram_dispatch` | Queue worker, 1 s poll | Single instance; per-chat ordering | Drain the outbox |
| `backfill` | Manual/startup/missing-data | Single instance; unique run | Resumable per file |
| `retention` | Daily 03:00 UTC | Single instance | Run at next start if missed |
| `backup` | Daily 03:30 UTC | Single instance | Run at next start if missed |

APScheduler job definitions use `misfire_grace_time` (scan: 300 s; others: 60 s), `coalesce=True`, and `max_instances=1`. Cron triggers are UTC. The schedule is anchored to `00:00 UTC` so a 4h period is `00,04,08,12,16,20` and an 8h period is `00,08,16`; changing the period takes effect at the next boundary and is shown in the UI (F10d).

### Durable run ledger and overlap prevention

`job_runs` has `UNIQUE(job_type, window_start)`. A run is claimed in a short transaction: insert `RUNNING` with a lease owner/expiry. If the insert conflicts, the run is already claimed and the scheduler logs `skipped_duplicate`. A stale `RUNNING` row (lease expired) is reclaimed after `lease_expires_at`; the reclaimed run must be safe to restart because every stage is idempotent.

`job_locks` provides named advisory locks for non-periodic work (`backfill`, `paper_reset`, `live_arm`, `reconcile`). Locks are leased, not held forever; a crashed owner's lock expires.

The manual `POST /api/v1/scans` path uses the same claim mechanism. If a scan is `RUNNING`, the API returns 409 `SCAN_ALREADY_RUNNING` with the running scan ID (design F03.3). If the daemon is down, the CLI returns 503 `DAEMON_NOT_RUNNING`.

### Scan job model

A scan is a parent row plus per-symbol child rows. Each symbol advances through stages with checkpointed state:

1. `DATA`: ensure the window is present; detect gaps; trigger/await backfill; if insufficient, mark `FAILED` with the exact gap window and continue other symbols.
2. `METRICS`: compute/load the versioned metrics; if metrics fail, mark `FAILED`; if only some series are missing, produce `PARTIAL`.
3. `LLM`: build the prompt from CALC metrics; call the provider; validate the schema; on rejection, mark `DEGRADED` (metrics-only report) or `FAILED` per configuration.
4. `REPORT`: persist the report; link opportunities.
5. `DONE`.

A symbol failure never aborts the batch. The scan completes with `COMPLETE`, `PARTIAL`, or `FAILED` and a cost total. Progress is exposed as named stages, not a fabricated percentage.

### Rate limiting and concurrency

- **Binance REST**: a global token bucket for request weight, calibrated from `X-MBX-USED-WEIGHT-1m`, with a conservative ceiling (default 70% of 2400/min). Each endpoint declares its weight. Order endpoints have a separate limiter (default 50% of 1200/min and 300/10s).
- **Binance WS**: subscription budget per connection; backpressure on the ingest queue; drop-to-disk rather than drop-events.
- **LLM**: per-provider semaphores (default 2 concurrent calls per provider; 1 for DeepSeek if the beta strict mode is flaky), global scan concurrency (default 4 symbols), and a per-scan call budget.
- **Backfill**: at most 4 concurrent downloads; checksum verification; resumable.
- **Telegram**: one send at a time per chat, retry with backoff, respect 429 `retry_after`.

### Catch-up and missed runs

- Scan: only the most recent missed period is eligible. The rationale is that a 4h/8h analysis of an old window is not actionable; the historical data can still be backfilled for replay. The `job_runs` row records `MISSED` with the reason.
- Trigger evaluation: no catch-up; a trigger is evaluated only against a fresh snapshot, never a stale one.
- Position management: on restart, run once per open position after reconciliation.
- Backfill: a separate job fills historical gaps regardless of missed scans.

### Time and clock

All scheduling and comparison use UTC. The daemon checks `GET /fapi/v1/time` against local time at startup and periodically; clock offset > 5 s warns, > 60 s blocks live ordering and marks all data `STALE`. The analysis window is aligned to the scan period; the lookback (3–7 days) is a separate parameter.

### Failure handling

- Each stage has bounded retries (3) with exponential backoff and jitter; a stage that exhausts retries marks the symbol/stage failed.
- Provider `429`/`418` triggers a cooldown and a Telegram risk alert.
- Data gaps trigger backfill; if backfill cannot fill, the report is `PARTIAL` with the gap listed.
- The daemon on restart reconstructs state from `job_runs`, `scan_symbols`, `orders`, and `positions`; it does not trust in-memory state.

## Alternatives Considered

### Alternative 1: system cron / systemd timers

- **Pros**: simple, OS-native, no library.
- **Cons**: no in-process visibility, no per-symbol checkpoints, no catch-up policy beyond `Persistent=true`, harder to trigger manually from the UI, and no shared rate-limit state.
- **Why not**: the scan model needs durable stage state and a single scheduler that knows about data/LLM/order limits.

### Alternative 2: Celery/RQ with Redis

- **Pros**: mature distributed task queue, retries, visibility.
- **Cons**: a broker to run and back up; serialization of domain objects; another process to supervise; overkill for one machine and 10 symbols.
- **Why not**: APScheduler + SQLite gives the needed durability without a broker.

### Alternative 3: Custom asyncio loop

- **Pros**: no dependency; full control.
- **Cons**: reinvents cron parsing, misfire handling, job persistence, and shutdown; easy to get wrong around DST/UTC and overlap.
- **Why not**: APScheduler is small, mature, and replaceable behind the job service.

### Alternative 4: Temporal / Airflow

- **Pros**: durable workflows, retries, observability.
- **Cons**: heavy infrastructure, operational burden, and a programming model that would dominate a single-user tool.
- **Why not**: disproportionate.

## Consequences

### Positive

- Runs are durable and idempotent; a crash mid-scan resumes per symbol.
- Overlap is prevented by a database constraint, not just an in-memory flag.
- Rate limits are centralized, so scans, backfill, and orders cannot unknowingly exhaust the same budget.
- Catch-up policy is explicit and avoids stale actionable scans.

### Negative

- Two sources of scheduling truth (APScheduler timers + SQLite ledger) must agree; the ledger wins.
- A long-running symbol can delay the rest of the scan unless the worker pool is bounded; the design chooses bounded concurrency and continues other symbols.
- Missed old windows are not scanned, which is a deliberate product decision.

### Risks

- **Risk**: a stale `RUNNING` row blocks all future runs of a job type. **Mitigation**: lease expiry, `oft doctor` reports stale runs, and a safe `job unlock` maintenance command.
- **Risk**: clock drift causes boundary misses. **Mitigation**: NTP check, Binance server time, and a 300 s misfire grace.
- **Risk**: rate-limit miscalibration. **Mitigation**: weight accounting per endpoint, conservative ceiling, and adaptive backoff from response headers.
- **Risk**: per-symbol retries multiply LLM cost. **Mitigation**: per-scan call budget and a hard per-symbol attempt cap.

## Compliance checks

- A manual scan while one is running returns 409 and does not create a second run.
- A restart resumes `scan_symbols` from the last completed stage.
- Trigger evaluation never uses a stale snapshot.
- The scheduler is UTC-anchored; the UI displays UTC with an explicit label.
- Binance/LLM concurrency limits are shared across all jobs.
