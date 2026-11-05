# ADR-0012: Observability and operations — logs, supervision, backup/restore, upgrades

**Date**: 2026-11-06
**Status**: accepted
**Deciders**: architect (Stage 5); human approves at G3

## Context

The system must run continuously for at least 4 weeks of paper trading before Live, and then unattended. The PRD success metrics include "continuous 7 days without an unrecovered crash", "scan timeliness", "Telegram reachability with receipts", and "LLM cost visibility". Research warns about WS disconnects, rate-limit bans, archive corrections, and single-machine reliability. The design requires a persistent status bar with daemon health, a `/system` screen, delivery receipts, and an audit trail.

A single-machine install cannot justify a full observability stack, but "no logs until it breaks" is unacceptable for a money-moving system.

## Decision

Use **structured local logs + a health/status API + supervised process + tested backup/restore + versioned upgrades**. Everything stays on the machine; no telemetry leaves it.

### Structured logs

- `structlog` JSON lines to `$OFT_DATA/logs/oft.jsonl` with rotation (default 50 MB × 5) and a human-readable TTY renderer in development.
- Mandatory fields: `ts` (ISO-8601 UTC), `level`, `event`, `component`, `request_id`, `scan_id`, `symbol`, `decision_id`, `order_id`, `trade_id`, `duration_ms`, `error_code`, `data_quality`.
- Redaction filter for keys, `Authorization`, `X-MBX-APIKEY`, cookies, and prompt bodies. LLM prompt/response bodies are logged only at `DEBUG`, redacted, and marked `untrusted`.
- Log levels: `INFO` for lifecycle and business events; `WARNING` for degraded data/rate limits/retries; `ERROR` for failed stages/commands; `CRITICAL` for audit-chain breaks, corruption, kill-switch anomalies.
- No ANSI, no in-place rewriting in non-TTY mode (design A9).
- Logs are `0600`; the `/api/v1/system/logs` endpoint supports `since`, `level`, `follow` (SSE), and redacts secrets.

### Health and status

- `GET /health` (liveness): process is up; no dependencies required.
- `GET /ready` (readiness): SQLite open, migrations current, DuckDB catalog open, disk above the floor, clock within tolerance, config valid, secrets loaded, scheduler running. Returns 503 with a `checks[]` list otherwise.
- `GET /api/v1/status`: environment, daemon state, next scan, last scan, open positions, budget usage, WS state, Telegram state, kill switch, live gate.
- `oft doctor`: the same checks without the daemon, plus audit-chain verification, stale job runs, orphan Parquet parts, backup age, and key metadata (never values).
- Internal counters: scan duration and per-stage failures, symbol failures, LLM calls/latency/cost, schema rejection rate, WS reconnects, REST weight usage, `429`/`418` events, data gaps, trigger evaluations, risk blocks, order rejects, reconciliation drift, Telegram delivery success, disk usage.
- Optional Prometheus text endpoint (`/api/v1/system/metrics?format=prometheus`) for users who already run a local Prometheus; no exporter/agent is required.

### Process supervision

- systemd unit `oft.service`:
  - `Type=simple`, `ExecStart=/path/to/oft serve --config $OFT_DATA/config.toml`
  - `Restart=on-failure`, `RestartSec=5`, `TimeoutStopSec=30`, `KillMode=mixed`
  - dedicated `User=oft`, `Group=oft`, `UMask=0077`
  - `NoNewPrivileges=true`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths=$OFT_DATA`, `PrivateTmp=true`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `LockPersonality=true`, `MemoryDenyWriteExecute=true` (drop this last one if a pinned dependency requires executable memory; verify during packaging)
  - `WatchdogSec=60` with an sd_notify heartbeat from the daemon (optional)
  - `EnvironmentFile=-$OFT_DATA/oft.env` (for `OFT_MASTER_KEY` only if the keyring is unavailable)
- Docker Compose alternative: one service, `restart: unless-stopped`, volume for `$OFT_DATA`, healthcheck `/health`, read-only root filesystem with a tmpfs.
- macOS launchd plist; Windows service via NSSM is documented but not first-class.
- The daemon installs a graceful shutdown: stop accepting new jobs, finish in-flight transactions, flush Parquet buffers, close WS, checkpoint, close DB. It never aborts mid-order-write.

### Startup self-check

Run before serving traffic: config parse, secret load (keyring/encrypted), DB open + migrations, audit-chain verify, Parquet manifest integrity sample, disk space, clock offset vs Binance, port availability, prompt/knowledge version presence, kill-switch state, scheduler recovery, WS/Telegram connectivity (non-fatal for Paper). Live arming refuses if any live-critical check fails. The result is logged and shown in `/system` and `oft doctor`.

### Backup and restore

- **SQLite**: daily online backup via `VACUUM INTO` (or the backup API) to `$OFT_DATA/backups/oft-YYYYMMDD.db`, encrypted at rest, with an integrity check (`PRAGMA integrity_check`) and a hash. Keep 7 daily, 4 weekly, 12 monthly.
- **Parquet**: immutable; back up the tree + `manifest` using `rsync`/`restic` to an external disk or user-chosen encrypted remote. Checksums are verified. Raw zips are re-downloadable and excluded by default.
- **Secrets**: never included in backups; restore re-enters them via keyring or `OFT_MASTER_KEY`.
- **Restore drill**: `oft restore --from <backup>` verifies the DB, manifest, and audit chain; it refuses to run if the current daemon is live-armed. A documented quarterly drill restores into a scratch directory and runs `oft doctor`.
- **RPO/RTO**: RPO 24 h (or 1 h if the user schedules more frequent backups); RTO < 30 min for a single-machine restore. The 4-week paper record is included in the DB backup.

### Upgrade path

- Semantic versioning for the application; `/api/v1` compatibility.
- SQLite migrations are forward-only, numbered, checksummed, and applied inside a transaction; a backup is taken before migration. A failed migration rolls back and refuses to start rather than running on a partial schema.
- Parquet/data-format version and `metric_version` are independent; a new version writes new paths and leaves old data readable. There is no in-place rewrite.
- Prompt versions are immutable; a new app version may ship new defaults, but activation is explicit and recorded.
- Upgrade procedure: stop daemon → backup → install new version → migrate (automatic on start) → `oft doctor` → start → verify a paper scan → resume. Rollback: stop, restore the pre-upgrade backup, reinstall the previous version. No auto-update; no silent migration.
- Dependency upgrades are deliberate and covered by the test suite; lockfiles are committed.

### Disk and capacity

- Backfill is 10–30 GB compressed; raw archives are pruned after verified conversion (ADR-0002).
- Disk guard: warn at 80%, block backfill at 95%, alert at 98%; never auto-delete curated data or the ledger.
- Monthly retention job logs what it would delete and requires `--apply` for destructive pruning (default dry-run).

### Incident runbook (summary)

| Symptom | First action | Then |
|---|---|---|
| Daemon down | `oft status`, `systemctl status oft` | inspect logs; restart; verify `oft doctor` |
| WS disconnected / stale data | `oft doctor` | check network; confirm reconnect/backfill; data marked `STALE`; risk blocks stale entries |
| Binance `429`/`418` | stop retries; wait out the ban | check weight budget; reduce concurrency; alert |
| LLM outage / schema failures | switch provider if configured | reject decisions, metrics-only reports; no orders |
| Order `UNKNOWN` / timeout | stop new orders for the position | reconcile by `client_order_id`/user stream; do not blind-retry |
| Suspected bad behavior | `oft kill` | inspect audit/decision logs; reconcile; resume with `RESUME` |
| Audit chain break / DB corruption | stop; do not trade | restore verified backup; investigate |
| Telegram failing | check token/chat/allowlist | pushes queue/retry; UI shows receipts |

## Alternatives Considered

### Alternative 1: Full observability stack (Prometheus + Grafana + Loki)

- **Pros**: dashboards, alerting, long-term metrics.
- **Cons**: three services to run, secure, back up, and upgrade on the same machine; disproportionate for one user.
- **Why not**: structured logs + a status API + optional Prometheus text cover the need. The stack can be added externally because the metrics endpoint is standard.

### Alternative 2: Cloud APM / error tracking

- **Pros**: easy dashboards, alerting.
- **Cons**: sends trading metadata and possibly secrets/positions off-machine; contradicts local-first; another account/dependency.
- **Why not**: explicit non-goal. No telemetry leaves the machine.

### Alternative 3: Plain text logs only

- **Pros**: zero setup.
- **Cons**: no correlation IDs, no machine parsing, no receipt/quality fields, hard to audit.
- **Why not**: structured logs are a small cost and directly support the required audit and status surfaces.

### Alternative 4: Hot standby / replicated database

- **Pros**: high availability.
- **Cons**: two machines, replication, split-brain risk, and the single-execution-gateway invariant becomes much harder; the product is explicitly single-machine.
- **Why not**: backups + supervision meet the reliability target. A standby is not MVP.

## Consequences

### Positive

- Every failure has a correlation ID, a log line, and a status surface.
- Backup/restore and migrations are tested procedures, not hopes.
- The process is hardened and supervised with a small footprint.
- No telemetry leaves the machine.

### Negative

- Structured logging and health checks are code that must be maintained.
- Backup encryption and restore drills are operator work.
- A single machine remains a single point of failure; RTO depends on the operator.

### Risks

- **Risk**: logs grow unbounded. **Mitigation**: rotation, retention, disk guard.
- **Risk**: a migration fails on a large `oft.db`. **Mitigation**: pre-migration backup, transaction, `oft doctor`, and a restore path.
- **Risk**: backup is stale or unverified. **Mitigation**: backup age in `oft doctor`, hash/integrity check, quarterly restore drill.
- **Risk**: the daemon restarts in a loop. **Mitigation**: `RestartSec`, crash-loop detection, startup self-check, and a `CRITICAL` alert after N restarts.

## Compliance checks

- `/health` and `/ready` distinguish liveness from readiness; `/ready` fails on DB/clock/disk problems.
- Every log line has a correlation ID where applicable and is secret-redacted.
- A pre-migration backup is mandatory; a failed migration refuses to start.
- Restore verifies DB integrity and the audit chain.
- No telemetry leaves the machine.
