# Implementation Plan — Milestone 1: 数据与指标地基

**Project:** Order-Flow-Trading
**Milestone:** 1 — 数据与指标地基
**Status:** plan complete; G3 pending
**Date:** 2026-11-06
**Inputs:** `.claude/prds/order-flow-trading.prd.md`; `docs/adr/0001`–`0012`; `docs/contracts/`; `.claude/ecc-workflow/handoffs/05-architecture.md`; `design/flows.md`; `.claude/ecc-workflow/handoffs/02-research.md`.
**Stack:** Option A — Python 3.12 modular monolith daemon (`oftd`), FastAPI, APScheduler, `binance-connector-python` + a thin WS client, DuckDB/Parquet, SQLite WAL, Pydantic v2, Typer CLI, React 19 + TypeScript + Vite SPA, Lightweight Charts.
**Boundary for this milestone:** deterministic data and metrics only. **No LLM calls, no prompt/knowledge work, no risk gate, no orders, no paper/live broker, no Telegram, no Binance credentials.** Public market data only.

---

## 1. Milestone outcome

A single local daemon that can:

1. ingest the USDⓈ-M / TradFi symbol universe and contract specs;
2. backfill history from `data.binance.vision` with checksum verification, resumability, and gap records;
3. ingest live WS market data (aggTrade, kline, markPrice, forceOrder) with checkpoints, dedupe, reconnect, and freshness guards;
4. compute versioned, deterministic CVD/delta, footprint, volume profile (POC/VAH/VAL), OI delta, funding, and live-only liquidations;
5. persist raw/normalized events and computed metrics as Parquet, with SQLite metadata/manifest/quality state;
6. expose one honest read path through the existing API surface, the CLI, and a minimal chart screen for one symbol.

**Non-goals for Milestone 1:** LLM narrative, reports with `LLM` content, opportunities, triggers, risk gate, orders, positions, paper account, Telegram, live arming, drawing tools, multi-chart layouts, full L2/DOM, historical liquidations (they do not exist).

---

## 2. Binding decisions and contract constraints

| Decision | Plan implication |
|---|---|
| ADR-0001: one daemon, one writer | All ingestion/backfill/metric writes go through the daemon service layer; CLI is an API client; `oft status`/`doctor` may read SQLite/Parquet read-only when the daemon is down. |
| ADR-0002: Parquet/DuckDB analytics + SQLite WAL state | Events and metrics are immutable Parquet parts under `$OFT_DATA/market`, `metrics`, `footprint`, `liquidations`; manifests, gaps, job runs, checkpoints, and symbol registry live in SQLite. |
| ADR-0003: REST + SSE, CLI is a client | Use only the route families already defined (`/api/v1/status`, `/doctor`, `/symbols`, `/watchlist`, `/scans`, `/reports`, `/backfill`, `/data/quality`, `/events`). Do not add a second metrics API. |
| ADR-0004: React/Vite + Lightweight Charts | One SPA served by FastAPI; chart scope is candles + volume + CVD + volume profile + footprint dock + quality stamps; data-table alternative required. |
| ADR-0007: APScheduler + durable `job_runs` | UTC-anchored 4h/8h windows; `UNIQUE(job_type, window_start)`; catch-up only the latest missed window; manual scan uses the same claim. |
| ADR-0008: WS + archive + gaps + versioned metrics | Live and archive share one normalizer and one metric engine; every metric carries `metric_version`, `input_manifest_hash`, `data_quality`, `quality_reasons`; liquidations are labelled “since deployment”. |
| ADR-0010: local security model | Loopback bind; bearer token for CLI; session + CSRF + Origin/Host checks for browser mutations; no secrets in this milestone; archive extraction is treated as untrusted. |
| ADR-0011: deterministic fixtures/replay | Injected `ClockPort`; committed small fixtures with source/date/checksum; no network in unit/integration; 80% coverage floor; golden outputs. |
| ADR-0012: observability/operations | `structlog` JSON logs with redaction; `/health` vs `/ready`; `oft doctor`; startup self-check; no telemetry. |
| Contracts | Every REST/CLI JSON response uses `api-envelope.schema.json`; every metric payload validates against `orderflow-metrics.schema.json`; market bars/specs validate against `market-snapshot.schema.json`; error codes come from `error-catalog.json`. |

### CLI compatibility note

The F12 command tree is the target surface. Milestone 1 adds **no new top-level groups**. Metrics are reachable through existing verbs with one additive flag:

- `oft scan run --metrics-only` (existing `scan run`, additive flag) produces a CALC-only scan/report.
- `oft report list|show` (existing verbs) reads it.
- `oft backfill run|status`, `oft watchlist search|groups|list`, `oft daemon *`, `oft status`, `oft doctor`, `oft system info` are used exactly as defined.
- The flag and the CALC-only report state are additive; existing command semantics are unchanged. The F12 documentation line for `scan run` should be extended in the same PR.

No new contract file is required. A metrics-only report embeds the already-validated `market-snapshot` and `orderflow-metrics` objects inside the standard API envelope. If a dedicated report schema is later required, it is additive work in Milestone 2.

---

## 3. Slice overview

Order is dependency-driven. Slice 01 is a genuine end-to-end path before any model-specific metric work begins; no slice introduces LLM or order behavior.

| # | Slice | Depends on | UI | Security-sensitive | Sec review before merge | Size |
|---|---|---|---|---|---|---|
| 01 | `01-walking-skeleton` | — | yes | yes | **yes** | M |
| 02 | `02-symbol-universe` | 01 | yes | no | no | M |
| 03 | `03-archive-backfill` | 01, 02 | no | yes | **yes** | L |
| 04 | `04-metrics-cvd-volume-profile` | 03 | no | no | no | L |
| 05 | `05-metrics-footprint-oi-funding-liquidations` | 04 | no | no | no | L |
| 06 | `06-live-ws-ingestion` | 02, 03, 04 | no | yes | **yes** | L |
| 07 | `07-resampling-and-freshness` | 06 | no | no | no | M |
| 08 | `08-read-api-cli-chart` | 03, 04, 05, 07 | yes | yes | **yes** | L |
| 09 | `09-scheduler-orchestration` | 08 | no | no | no | L |
| 10 | `10-hardening-replay-runbook` | 01–09 | no | no | no | M |

**Security-review rule:** slices 01, 03, 06, and 08 must not be merged without a security review. Milestone 1 touches no credentials, no order path, and no LLM boundary; the review scope is the local API/auth/CSRF surface, untrusted archive extraction, untrusted live-stream handling, and user-facing input/XSS. This is a review requirement only; it is not a gate approval.

**Size rule:** every slice is one PR. L slices have an explicit scope fence in the detailed section; if a slice grows beyond that fence, split it rather than widening the PR.

---

## 4. Detailed slices

### 01 — `01-walking-skeleton`

**Title:** Walking skeleton: one fixture through every layer

**Goal:** Stand up the repository, daemon, CLI, SPA shell, config/logging/health, and prove one fixture-backed aggTrade flows source → normalizer → Parquet/SQLite → delta computation → API → CLI/UI.

**Files expected**
- `pyproject.toml`, `uv.lock`, `.python-version`, `README.md`, `Makefile`
- `src/oft/__init__.py`, `src/oft/config.py`, `src/oft/logging.py`, `src/oft/clock.py`
- `src/oft/ports/{market_data,store,clock}.py`
- `src/oft/adapters/{fixture_market_data,sqlite_store,parquet_store}.py`
- `src/oft/domain/events.py`, `src/oft/domain/metrics/{__init__,delta}.py`
- `src/oft/application/{health,fixture_scan}.py`
- `src/oft/api/{app,auth,errors}.py`, `src/oft/api/routes/{system,scans}.py`
- `src/oft/cli/{main,api_client,output}.py`, `src/oft/cli/commands/{status,doctor,scan}.py`
- `web/` Vite React TS shell, `web/src/routes/system.tsx`, `web/src/components/status-bar.tsx`
- `tests/fixtures/market/btcusdt_agg-trades_sample.csv`, `tests/fixtures/market/README.md`, `tests/fixtures/market/CHECKSUMS`
- `tests/unit/test_config.py`, `tests/unit/metrics/test_delta.py`
- `tests/integration/test_walking_skeleton.py`, `tests/contract/test_api_envelope.py`, `tests/contract/test_error_catalog_sync.py`
- `tests/e2e/test_shell_status.py`

**Tests expected**
- Config: defaults, `$OFT_DATA`, invalid config returns `CONFIG_INVALID`; no secret values required.
- Delta: `is_buyer_maker=false` is taker buy, `true` is taker sell; buy/sell volume, delta, cumulative CVD from the fixture.
- Integration: fixture file → normalized rows → Parquet part + manifest row → delta result → `POST /api/v1/scans` → `GET /api/v1/scans/{id}` → CLI JSON. Assert the same value through API and CLI and a valid envelope.
- Contract: every fixture response validates against `api-envelope.schema.json`; `errorCode` enum equals `error-catalog.json`.
- E2E: `/system` renders health, readiness, version, data dir, and the fixture scan result; 375px shell is read-only.
- Security negative: `/api/v1/*` without bearer token is `AUTH_REQUIRED`; a browser mutation without CSRF/Origin is rejected; logs redact bearer tokens.

**UI:** true. **Security:** true. **Security review required:** yes.

**Acceptance criteria**
- [ ] `uv sync && uv run pytest -q` passes with no network.
- [ ] `uv run oft serve` binds `127.0.0.1:7611`; `/health` returns 200; `/ready` returns 200 with named checks.
- [ ] `oft status` and `oft doctor` work with the daemon down and report `OFFLINE`/`DEGRADED`, exit 0.
- [ ] `oft scan run --fixture tests/fixtures/market/btcusdt_agg-trades_sample.csv --symbol BTCUSDT --period 4h --metrics-only` returns a scan ID and a delta matching the golden value.
- [ ] `GET /api/v1/scans/{id}` returns the same delta; `/system` shows the same scan state.
- [ ] Kill the daemon: `oft status` still reads SQLite read-only and never blocks.

**Preview screens**
- `/system` shell and the global status bar (route surface from ADR-0004 / F12 system surface).
- S03.2 `/scans/:scanId` in a metrics-only fixture state; S03.3 `oft scan run`; S03.4 `oft scan status`.
- No chart yet; S04.2 is stubbed as CALC-only.

**Dependencies:** none. **Size:** M.

---

### 02 — `02-symbol-universe`

**Title:** Symbol universe and contract specs

**Goal:** Ingest `exchangeInfo`, persist a searchable symbol registry with contract specs, and expose read-only symbol/inspector surfaces.

**Files expected**
- `src/oft/domain/symbols.py`
- `src/oft/adapters/binance_rest.py` (exchangeInfo only, async, weight-aware)
- `src/oft/application/symbol_registry.py`
- `src/oft/api/routes/{symbols,watchlist}.py`
- `src/oft/cli/commands/watchlist.py` (read-only `list`, `search`, `groups`)
- `web/src/routes/watchlist.tsx`, `web/src/routes/symbol-inspector.tsx`, `web/src/api/symbols.ts`
- `tests/fixtures/binance/exchange_info_sample.json`
- `tests/unit/test_symbol_registry.py`
- `tests/integration/test_exchange_info_ingest.py`
- `tests/contract/test_symbols_api.py`
- `web/src/routes/__tests__/symbol-inspector.test.tsx`

**Tests expected**
- Parse the fixture with `PERPETUAL` and `TRADIFI_PERPETUAL` entries; reject malformed payloads; filter `status == TRADING`.
- Assert `tickSize`, `stepSize`, `minQty`, `minNotional`, `pricePrecision`, `quantityPrecision`, `onboardDate`, `contractType`, `underlyingType`.
- Idempotent upsert; a changed spec updates the row and `updated_at`; no duplicate symbols.
- `/api/v1/symbols` and `/api/v1/symbols/{symbol}` validate against the envelope; unknown symbol returns `NOT_FOUND`.
- `oft watchlist search BTCUSDT --format json` returns the same specs; `oft watchlist groups` returns crypto/TradFi counts.
- Inspector renders contract type, tick/step/min notional, listing date, and coverage placeholders; 375px is read-only; no raw HTML injection from symbol metadata.

**UI:** true. **Security:** false (public read-only upstream; still validate untrusted JSON).

**Acceptance criteria**
- [ ] Registry contains all symbols from the fixture, including BTCUSDT and XAUUSDT, with expected specs.
- [ ] An opt-in live smoke can refresh from `GET /fapi/v1/exchangeInfo`; default tests are fixture-only.
- [ ] `GET /api/v1/symbols/{symbol}` returns contract specs and data-coverage placeholders.
- [ ] `/watchlist/:symbol` renders the inspector; search finds a symbol by exact code or substring.
- [ ] No watchlist star mutation is introduced in this slice (that belongs to Milestone 2).

**Preview screens**
- S02.2 `/watchlist/:symbol` (contract specs, listing date, coverage placeholders; read-only).
- S02.3 search overlay (read-only search; add/star deferred).
- S02.4 `oft watchlist list|search|groups`.

**Dependencies:** 01. **Size:** M.

---

### 03 — `03-archive-backfill`

**Title:** Historical backfill from `data.binance.vision` (one symbol, one week)

**Goal:** Download, checksum-verify, convert, manifest, and gap-check a one-week archive slice for one symbol; make the path resumable and safe against hostile archives.

**Files expected**
- `src/oft/ports/archive.py`
- `src/oft/adapters/archive.py` (HTTPS, `.CHECKSUM`, streamed zip extraction in a new empty dir, size limits)
- `src/oft/adapters/parquet_store.py` (canonical event schemas, atomic part publish)
- `src/oft/application/backfill_service.py`, `src/oft/application/gap_detection.py`
- `src/oft/domain/data_quality.py`
- `src/oft/api/routes/data.py`
- `src/oft/cli/commands/backfill.py`
- `tests/fixtures/archive/btcusdt_agg-trades_2026-10-01.zip` (+ `.CHECKSUM`), one deliberately corrupt checksum fixture
- `tests/unit/archive/test_checksum.py`, `tests/unit/archive/test_zip_safety.py`
- `tests/integration/test_backfill_one_week.py`, `tests/integration/test_gap_detection.py`
- `tests/contract/test_backfill_api.py`

**Tests expected**
- Checksum mismatch → `BACKFILL_CHECKSUM_MISMATCH`, no manifest row, no Parquet conversion, alert log, resumable retry.
- Zip-slip path (`../../`) and oversized expansion are rejected before write; extraction never executes archive content.
- One-week fixture converts to partitioned Parquet (`symbol/date` UTC); `agg_trade_id` dedupe; canonical fields plus `source`, `ingested_at`, `record_hash`.
- Manifest rows carry URL, checksum, bytes, rows, status, verified_at; a partial download resumes from the last verified file.
- Gap detection compares expected daily files/bar counts with present data; a missing day creates a `data_gaps` row with start/end/reason/severity and marks the metric input `PARTIAL`.
- `/api/v1/backfill` and `/api/v1/backfill/{run_id}` use the envelope; a running backfill returns `BACKFILL_ALREADY_RUNNING`; size estimate is returned before download.
- CLI exit codes: 0 success, 3 partial, 4 blocked, 5 config.

**UI:** false. **Security:** true (untrusted archive extraction, user-supplied symbol/date, path traversal, zip bomb). **Security review required:** yes.

**Acceptance criteria**
- [ ] `oft backfill run --symbol BTCUSDT --start 2026-10-01 --end 2026-10-07 --yes` completes with all checksums verified (fixture or live opt-in).
- [ ] A corrupt checksum fails closed and leaves no published part or manifest “complete” row.
- [ ] `oft backfill status` shows bytes, rows, verified count, and status per file.
- [ ] Removing one daily file produces exactly one gap row and a `PARTIAL` quality result for the affected window.
- [ ] Re-running the same range is idempotent and does not duplicate Parquet rows.
- [ ] No network is used in default unit/integration tests; the live download test is marked `@pytest.mark.live_smoke`.

**Preview screens**
- None directly; the manifest/gap data feeds S02.2 coverage and S04.2/S04.3 quality stamps.

**Dependencies:** 01, 02. **Size:** L.

---

### 04 — `04-metrics-cvd-volume-profile`

**Title:** Metric engine v1 — CVD/delta and volume profile

**Goal:** Implement the pure, versioned metric core for CVD/delta and volume profile (POC/VAH/VAL), persist schema-valid outputs, and establish golden tests.

**Files expected**
- `src/oft/domain/metrics/{types,cvd,volume_profile,version}.py`
- `src/oft/application/metric_service.py`
- `src/oft/adapters/parquet_store.py` (metrics writer + manifest)
- `tests/unit/metrics/test_cvd.py`, `tests/unit/metrics/test_volume_profile.py`
- `tests/integration/test_metric_persistence.py`
- `tests/contract/test_orderflow_metrics_schema.py`
- `tests/fixtures/golden/cvd_btcusdt_4h.json`, `tests/fixtures/golden/volume_profile_btcusdt.json`

**Tests expected**
- CVD: buy/sell mapping, per-bar delta, cumulative CVD, summary, taker-buy ratio; empty window; zero volume.
- Kline cross-check: `delta_bar == 2 * taker_buy_volume - volume` within exact decimal arithmetic.
- Volume profile: histogram by price bin, POC = max volume, value area = smallest contiguous range containing 70%, VAH/VAL, fixed tie-break rule; no float at the boundary; bin size from tick size or configured multiple.
- Versioning: changing an algorithm or parameter creates a new `metric_version` and a new output path; old versions remain readable.
- Contract: output validates against `orderflow-metrics.schema.json`; decimal fields are strings, timestamps are UTC with `Z`.
- Persistence: rows carry `metric_version`, `input_manifest_hash`, `computed_at`, `data_quality`, `quality_reasons`; atomic temp → fsync → rename → manifest insert.
- Golden files fail on any numeric drift; a golden update requires an explicit version bump.

**UI:** false. **Security:** false.

**Acceptance criteria**
- [ ] A one-week aggTrades fixture produces schema-valid CVD and volume-profile sections for BTCUSDT/4h.
- [ ] The kline cross-check passes exactly on the fixture.
- [ ] POC/VAH/VAL match the golden file and the 70% value-area rule.
- [ ] `metric_version` is recorded and immutable; rerunning produces identical bytes for identical inputs/version.
- [ ] No `datetime.now()` or `float` in the metric path; time and decimals are injected/exact.

**Preview screens**
- None directly; feeds the S04.2 CALC band in slice 08.

**Dependencies:** 03. **Size:** L.

---

### 05 — `05-metrics-footprint-oi-funding-liquidations`

**Title:** Metric engine v2 — footprint, OI, funding, liquidation accumulator

**Goal:** Complete the metric contract: footprint aggregation, OI delta with irregular resampling, funding, and a live-only liquidation accumulator.

**Files expected**
- `src/oft/domain/metrics/{footprint,open_interest,funding,liquidations}.py`
- `src/oft/application/{oi_funding_service,liquidation_recorder}.py`
- `src/oft/adapters/parquet_store.py` (footprint/liquidation writers)
- `tests/unit/metrics/test_footprint.py`, `test_open_interest.py`, `test_funding.py`, `test_liquidations.py`
- `tests/integration/test_metrics_v2_persistence.py`
- `tests/fixtures/golden/{footprint_btcusdt,open_interest_xauusdt,funding_btcusdt,liquidations_btcusdt}.json`
- `tests/fixtures/market/{xauusdt_metrics_sample.csv,liquidations_btcusdt.jsonl}`

**Tests expected**
- Footprint: `bidVol = Σqty[is_buyer_maker=true]`, `askVol = Σqty[is_buyer_maker=false]`, delta/total, price bucket rounding, imbalance ratio at threshold, stacked imbalance detection; `top_imbalances` ordering.
- OI: `OI_end − OI_start`; regular 5m sampling; irregular TradFi timestamps resampled and stamped `ESTIMATED` with a reason; no interpolation presented as measured.
- Funding: current rate, next funding time, interval (4h/8h), mark/index price, history; a missing funding period is `MISSING`, not zero.
- Liquidations: `side=SELL` means a long was liquidated, `side=BUY` means a short was liquidated; totals and buckets; `coverage_start` is deployment time; `coverage=NONE` before any recording; `limitations` always includes the largest-per-second and no-historical-backfill statements.
- No test depends on historical USDⓈ-M liquidations; the fixture is recorded/live-shaped, not claimed historical.
- Contract: the complete `orderflow-metrics` object validates for BTCUSDT and XAUUSDT fixtures.

**UI:** false. **Security:** false.

**Acceptance criteria**
- [ ] One BTCUSDT fixture and one XAUUSDT fixture each produce a complete, schema-valid `orderflow-metrics` object with all six metric groups.
- [ ] XAUUSDT OI is `IRREGULAR_RESAMPLED` + `ESTIMATED` when the source timestamps are irregular.
- [ ] Liquidation output is labelled “since deployment” and never claims historical coverage.
- [ ] Footprint golden matches per-cell bid/ask/delta/total and stacked-imbalance flags.
- [ ] Persisted footprint and liquidation Parquet paths follow ADR-0002 and carry `metric_version`.

**Preview screens**
- None directly; feeds S04.2 CALC band and the liquidation-coverage note in S04.2 partial state.

**Dependencies:** 04. **Size:** L.

---

### 06 — `06-live-ws-ingestion`

**Title:** Live WS ingestion, persistence, checkpoints, and stale guards

**Goal:** Continuously ingest `aggTrade`, `kline_1m`, `markPrice@1s`, and `forceOrder` for the starred symbols with correct routing, durable buffering, dedupe, reconnect, checkpoint recovery, and freshness detection.

**Files expected**
- `src/oft/adapters/binance_ws.py` (explicit `/public` vs `/market` routing, ack validation, ping/timeout)
- `src/oft/adapters/binance_rest.py` (last-48h `aggTrades` gap recovery)
- `src/oft/application/ingestion_supervisor.py`
- `src/oft/application/freshness.py`
- `src/oft/ports/market_data.py`, `src/oft/domain/events.py`
- `src/oft/adapters/parquet_store.py` (30 s/50k-row flush, temp → fsync → rename → manifest)
- `tests/fixtures/ws/{aggtrade,kline,markprice,forceorder}.jsonl`
- `tests/unit/ws/test_routing.py`, `test_reconnect.py`, `test_dedupe.py`, `test_backpressure.py`
- `tests/integration/test_live_ingestion_recorded.py`, `test_stale_guard.py`
- `tests/integration/test_rest_gap_recovery.py`

**Tests expected**
- Routing: `@aggTrade`, `@kline_1m`, `@markPrice@1s` use `/market`; the client rejects a silent subscription failure; a bare `/ws` path for a `/market` stream is detected and alerted.
- Normalization: canonical event fields, enum/range validation, control-character stripping, malformed events increment a counter and never crash.
- Dedupe: duplicate/out-of-order `agg_trade_id`/event times are dropped or quarantined; no duplicate Parquet rows.
- Backpressure: bounded queue pauses reads and spills to a local WAL file; no silent drop; the counter is exported.
- Flush: 30 s or 50k rows; temp file → fsync → rename → manifest; an orphan part is quarantined on startup.
- Reconnect: exponential backoff + jitter, resubscribe, recover the last 48 h via REST, record a `data_gaps` row if recovery is incomplete.
- Checkpoint: restart resumes from the last event time/ID; a stale checkpoint is visible in `oft doctor`.
- Freshness: mark > 60 s, 1m bar > 2 min after close, metrics > 15 min after expected sample → `STALE`; no risk/trigger path exists yet, so the guard only stamps and logs.
- Opt-in live smoke for BTCUSDT is marked `@pytest.mark.live_smoke` and is not part of default CI.

**UI:** false. **Security:** true (untrusted external stream, log injection, resource exhaustion). **Security review required:** yes.

**Acceptance criteria**
- [ ] A recorded WS session replays through the supervisor and produces the expected Parquet rows with no duplicates.
- [ ] A simulated disconnect reconnects, resubscribes, and records recovery/gap state.
- [ ] Restart from checkpoint does not lose or duplicate events in the recorded test.
- [ ] Malformed and oversized events are rejected with counters, not crashes.
- [ ] The stale guard flips after the configured budget and clears when fresh data arrives.
- [ ] `oft daemon start` + `oft status` show WS connection state and last event times.

**Preview screens**
- None directly; WS/system health feeds the existing `/system` and S03.5 status surfaces.

**Dependencies:** 02, 03, 04. **Size:** L.

---

### 07 — `07-resampling-and-freshness`

**Title:** Event-to-bar resampling and freshness budgets

**Goal:** Produce UTC-aligned 1m/5m/15m/1h/4h/8h/1d bars from normalized events, handle late events deterministically, and make freshness/gaps explicit to every metric consumer.

**Files expected**
- `src/oft/domain/resampling.py`
- `src/oft/application/resampling_service.py`
- `src/oft/application/freshness.py` (extend)
- `src/oft/adapters/parquet_store.py` (bar writer)
- `tests/unit/test_resampling.py`, `tests/unit/test_freshness.py`
- `tests/integration/test_resample_from_events.py`
- `tests/fixtures/golden/bars_btcusdt_{1m,5m,15m,1h,4h,8h,1d}.json`

**Tests expected**
- UTC alignment: 4h boundaries at `00/04/08/12/16/20`, 8h at `00/08/16`; no local timezone.
- OHLCV correctness from aggTrades: open/high/low/close, volume, quote volume, trade count, taker-buy volume; no lookahead.
- Late event: recompute only the last N bars (default 2), create a new metric run, never edit a published part in place.
- Missing bars: emitted as absent/`MISSING`, never interpolated; gaps are carried into the metric input.
- Freshness: budgets per data type; `STALE` when exceeded; clock skew detected via injected clock.
- Golden outputs for all seven periods on a one-day fixture.

**UI:** false. **Security:** false.

**Acceptance criteria**
- [ ] One day of aggTrades produces deterministic 1m/5m/15m/1h/4h/8h/1d bars matching golden files.
- [ ] Late-event recomputation is idempotent and versioned.
- [ ] A missing bar is not invented; the downstream metric reports `PARTIAL`/`MISSING`.
- [ ] Freshness budgets are configurable and unit-tested at the boundary.
- [ ] Bars carry `source_mode` and the input manifest hash used by metrics.

**Preview screens**
- None directly; feeds S04.2/S04.3 quality badges.

**Dependencies:** 06. **Size:** M.

---

### 08 — `08-read-api-cli-chart`

**Title:** Metrics-only read path: API, CLI, and minimal chart for one symbol

**Goal:** Turn the data/metrics foundation into a usable read path: a manual metrics-only scan, a CALC-only report, the existing REST/CLI surfaces, and one chart screen.

**Files expected**
- `src/oft/application/scan_service.py` (manual `DATA → METRICS → REPORT`, no LLM)
- `src/oft/api/routes/{scans,reports}.py`
- `src/oft/api/schemas/report.py`
- `src/oft/cli/commands/{scan,report}.py`
- `web/src/routes/{scans,scan-detail,reports,report-detail}.tsx`
- `web/src/components/{chart,provenance,data-quality,footprint-dock}.tsx`
- `web/src/api/{scans,reports}.ts`
- `tests/integration/test_metrics_only_scan_api.py`
- `tests/contract/test_report_response.py`
- `tests/e2e/test_chart_metrics_only.py`, `tests/e2e/test_accessibility_chart.py`
- `web/src/components/__tests__/chart.test.tsx`

**Tests expected**
- `POST /api/v1/scans` requires an `Idempotency-Key`; the same key returns the original response with `meta.idempotent_replay=true`; a different hash returns `IDEMPOTENCY_CONFLICT`.
- Metrics-only scan: `DATA → METRICS → REPORT`; no `llm_calls` row is created; the report records `llm_decision_id=null`, quality, and the metric manifest.
- `GET /api/v1/scans/{id}` and `/api/v1/reports/{id}` return valid envelopes; a running scan returns `SCAN_ALREADY_RUNNING` on a second manual request.
- Report response embeds a valid `market-snapshot` and `orderflow-metrics`; bars and metrics agree.
- CLI: `oft scan run --metrics-only`, `oft scan status`, `oft report list`, `oft report show`; JSON envelope and exit codes; `oft report show` prints CALC metrics first and an explicit “LLM not configured” line.
- Chart: candles + volume + CVD + volume-profile overlay + footprint dock for one symbol/4h; selected bar footprint matches the CALC table; data-table alternative renders the same values.
- E2E: 1440/768/375; chart is read-only; keyboard navigation; no raw HTML; quality stamps visible; no chart when data is `MISSING` (explanation instead).
- Contract: hostile symbol metadata and error strings are escaped; CSP has no `unsafe-inline`/`unsafe-eval`.

**UI:** true. **Security:** true (user input, API mutation surface, XSS/CSP). **Security review required:** yes.

**Acceptance criteria**
- [ ] From a clean data dir and a completed backfill, one `oft scan run --metrics-only --symbol BTCUSDT --period 4h --source backfill` produces a report ID.
- [ ] `oft report show <id>` and `GET /api/v1/reports/{id}` show identical CVD, VP, footprint, OI, funding, and liquidation sections.
- [ ] `/reports/<id>` renders the metrics-only report and chart; the LLM band is absent/disabled with an explicit reason, never fabricated.
- [ ] The chart data-table alternative matches the chart values.
- [ ] No LLM call, no order, no credential is required or recorded.
- [ ] A second manual scan while one is running returns 409 `SCAN_ALREADY_RUNNING`.

**Preview screens**
- S03.2 `/scans/:scanId` (metrics-only run detail).
- S03.3 `oft scan run`; S03.4 `oft scan status|history|show`.
- S04.1 `/reports`; S04.2 `/reports/:reportId` (CALC-only report state).
- S04.3 chart panel: candles, volume, CVD, volume profile, footprint dock, quality badges, data-table fallback.
- S04.4 `oft report show`.

**Dependencies:** 03, 04, 05, 07. **Size:** L.

---

### 09 — `09-scheduler-orchestration`

**Title:** Backfill/scan job orchestration, rate limiting, and catch-up

**Goal:** Run metrics-only scans and backfill/gap jobs on UTC 4h/8h boundaries with a durable job ledger, overlap prevention, per-symbol rate limits, and an explicit catch-up policy.

**Files expected**
- `src/oft/application/scheduler.py`, `src/oft/application/jobs.py`
- `src/oft/application/rate_limiter.py`
- `src/oft/adapters/sqlite_store.py` (job tables, leases, locks)
- `src/oft/api/routes/scans.py` (manual path shares the claim)
- `src/oft/cli/commands/daemon.py`
- `tests/integration/test_job_ledger.py`, `test_scheduler_catchup.py`, `test_rate_limiter.py`, `test_manual_scan_conflict.py`
- `tests/unit/test_schedule_alignment.py`

**Tests expected**
- `UNIQUE(job_type, window_start)`: duplicate claim is skipped; a stale `RUNNING` lease is reclaimed; the reclaimed run restarts safely.
- Manual scan and scheduled scan cannot overlap; manual returns 409 with the running scan ID.
- UTC 4h/8h boundaries and jitter; changing the period takes effect at the next boundary.
- Catch-up: only the latest missed window runs; older missed windows are recorded `MISSED` with a reason.
- Rate limiting: global Binance token bucket at a conservative ceiling; per-endpoint weights; 429/418 triggers cooldown and an alert; backfill concurrency ≤ 4; WS subscription budget.
- Restart: scheduler reconstructs state from `job_runs`/`scan_symbols`; no in-memory assumptions.
- `oft daemon start|stop|status|logs` behavior; `oft scan status` shows next/last run.

**UI:** false. **Security:** false.

**Acceptance criteria**
- [ ] The daemon starts the scheduler; the next 4h/8h run is visible and UTC-labelled.
- [ ] A duplicate job claim never creates a second run.
- [ ] Manual scan while a scan is running returns 409 and does not create a second run.
- [ ] After downtime, only the latest missed window runs; older windows are `MISSED`.
- [ ] Rate-limit and 429 tests show backoff/cooldown and no retry storm.
- [ ] No LLM, trigger, position, or order jobs are registered in this milestone.

**Preview screens**
- S03.1 `/scans` schedule state (next run / last run), rendered through the existing shell; no new UI in this slice.

**Dependencies:** 08. **Size:** L.

---

### 10 — `10-hardening-replay-runbook`

**Title:** Hardening: replay, deterministic clock, coverage, and runbook

**Goal:** Make the milestone reproducible and operable: replay harness, fixed-clock enforcement, complete fixtures, contract coverage, 80% gate, and a runbook a human can follow.

**Files expected**
- `src/oft/replay/{clock,event_source,harness}.py`
- `scripts/fetch-fixtures`
- `tests/conftest.py`
- `tests/replay/test_metrics_replay.py`
- `tests/test_no_wall_clock.py`
- `tests/contract/test_all_contract_fixtures.py`
- `docs/runbooks/milestone-1.md`
- CI config (`.github/workflows/ci.yml` or the repository’s chosen CI file)
- `pyproject.toml` tool config (`ruff`, `mypy`, `pytest`, coverage, `import-linter`)

**Tests expected**
- Replay the same fixture window twice with the same `metric_version`/manifest hash → identical metric hash; a different version produces a new path, not an edit.
- Replay writes only replay namespace/output; it cannot touch the live daemon state.
- `tests/test_no_wall_clock.py` asserts no `datetime.now()`/`time.time()` in `domain`/`application` (injected clock only).
- All committed fixtures validate against their schemas; the error catalog enum stays in sync.
- `import-linter` contracts: `domain` imports only stdlib/Pydantic; no LLM/execution imports exist yet; `api` does not import broker modules.
- Coverage: overall ≥ 80%; 100% branch on the pure metric functions; CI fails below the floor.
- `oft doctor` reports DB integrity, disk, clock, manifest orphans, stale job runs, and audit-chain placeholder; no secrets.
- Runbook commands are executable in a temp data dir and produce the recorded output shape.

**UI:** false. **Security:** false (but includes negative security tests).

**Acceptance criteria**
- [ ] `make ci` (or the documented equivalent) passes offline: lint, type, import-linter, unit, integration, contract, replay, frontend tests/build.
- [ ] Coverage is ≥ 80% and metric primitives meet the branch gate.
- [ ] Replay is deterministic and fails loudly on non-determinism.
- [ ] No wall-clock call remains in the deterministic path.
- [ ] `docs/runbooks/milestone-1.md` reproduces the demo path from a clean data dir.
- [ ] `oft doctor` output matches the runbook’s expected sections.

**Preview screens**
- None.

**Dependencies:** 01–09. **Size:** M.

---

## 5. Test strategy for the milestone

### 5.1 Levels and tools

| Level | Scope | Tools | Rules |
|---|---|---|---|
| Unit | Delta/CVD, volume profile, footprint, OI/funding/liquidations, resampling, freshness, config, schedule alignment | pytest, Hypothesis | Pure functions, no I/O, injected `ClockPort`; no network. |
| Contract | `api-envelope`, `orderflow-metrics`, `market-snapshot`, error catalog, and every route/fixture payload | pytest + `jsonschema` Draft 2020-12 | Every fixture and every route response validates; error enum equals the catalog; generated Pydantic/TS types, no handwritten duplicate shapes. |
| Integration | SQLite/Parquet repositories, backfill conversion, gap detection, metric persistence, job ledger, command bus | pytest, `tmp_path` | Real SQLite/Parquet; no network; recorded archives/WS frames only. |
| Replay | Fixture window → bars → metrics → metrics-only report | replay harness | Same domain code as live; pinned metric versions; deterministic clock/seed; replay namespace only. |
| E2E | `/system`, symbol inspector, metrics-only scan/report, chart, 375/768/1440, keyboard, data-table fallback | pytest + HTTPX, Playwright, Vitest/RTL, axe | No real exchange; fixture data; no LLM. |
| Live smoke (opt-in) | Public `exchangeInfo`, one archive week, one short WS capture | pytest marker `live_smoke` | Never in default CI; no credentials; results are not golden tests. |

### 5.2 Recorded market-data fixtures

- `tests/fixtures/market/` contains small, committed samples: a few hundred `aggTrades`, 1m/5m klines, metrics (regular and irregular TradFi), funding, and forceOrder events.
- Every fixture has a `README.md` with source URL (public `data.binance.vision` or a recorded public WS capture), retrieval date, and SHA256 in `CHECKSUMS`.
- Larger windows are fetched by `scripts/fetch-fixtures` and marked `@pytest.mark.fixture_large`; they run in a separate opt-in job.
- No copyrighted book text, no secrets, no real account data.
- Golden outputs live in `tests/fixtures/golden/`; changing a golden requires an explicit metric-version bump or a documented test-only correction.

### 5.3 Deterministic time

- All time enters through `ClockPort`; tests use `FixedClock` or `ReplayClock`.
- No `datetime.now()`, `time.time()`, or local timezone in `domain`/`application`.
- All partitioning, scheduling, and comparisons use UTC; display-only local time is labelled.
- Late events and “last N bars” are evaluated against event time, not wall time.
- A static test (`tests/test_no_wall_clock.py`) fails the build on a wall-clock call in the deterministic path.

### 5.4 Coverage gate

- Overall line coverage ≥ 80%.
- 100% branch coverage on the pure metric functions in `src/oft/domain/metrics/` (the milestone’s deterministic core). ADR-0011’s 100% branch requirement for `domain.risk`, `domain.trigger`, and the OMS applies to later milestones.
- Coverage is a floor: missing branches must be either tested or explicitly justified in the PR.
- Frontend: Vitest/RTL for components/hooks; Playwright for the chart/inspector flows; axe-core for automated WCAG checks.

### 5.5 Explicitly not tested yet

- LLM provider behavior, schema repair/rejection, prompt templates, knowledge injection, cost ledger.
- Risk gate, trigger evaluator, opportunity ledger, entry re-check, position management.
- OMS/paper broker/live broker, idempotent order submission, kill switch, reconciliation.
- Telegram delivery/receipts and allowlist.
- Historical USDⓈ-M liquidations (they do not exist); liquidation tests use recorded live-shaped events and label coverage.
- Full TradFi symbol coverage and per-symbol archive completeness; only a small XAUUSDT sample is in CI.
- Full historical L2/DOM ladders, bookTicker history, queue position, iceberg detection.
- 10–30 GB multi-symbol backfill performance; the milestone validates one symbol/week and a small multi-symbol smoke.
- Production WS reliability at scale, real Binance keys, testnet orders, systemd packaging, backup/restore, and long-running soak tests.

---

## 6. Milestone exit criteria

A milestone is complete only when all of the following are true:

- [ ] The daemon starts, `/health` and `/ready` pass, and `oft status`/`oft doctor` work with the daemon down.
- [ ] `exchangeInfo` is ingested into the symbol registry; BTCUSDT and XAUUSDT expose contract specs and listing dates.
- [ ] One-week backfill for one symbol completes with checksum verification, Parquet conversion, manifest rows, and a demonstrated gap record on a missing file.
- [ ] Live ingestion for one symbol captures aggTrade/kline/markPrice/forceOrder, checkpoints advance, reconnect/restart/dedupe tests pass, and the stale guard works.
- [ ] All six metric groups are implemented and schema-valid: CVD/delta, footprint, volume profile, OI, funding, liquidations.
- [ ] Liquidation output is labelled “since deployment”; no historical backfill is claimed.
- [ ] Metrics are versioned, reproducible, and persisted with `metric_version`, `input_manifest_hash`, `data_quality`, and `quality_reasons`.
- [ ] A metrics-only scan/report is reachable through CLI, API, and the minimal chart; all three show the same CALC values.
- [ ] The chart for one symbol renders candles, volume, CVD, volume profile, and footprint with a data-table alternative and quality stamps.
- [ ] Scheduler jobs are durable, non-overlapping, UTC-aligned, rate-limited, and catch up only the latest missed window.
- [ ] Contract tests pass: API envelope, error catalog sync, `orderflow-metrics`, `market-snapshot`, and every route/fixture payload.
- [ ] Coverage ≥ 80%; metric primitives meet the branch gate; lint/type/import-linter/frontend build pass offline.
- [ ] **No LLM call, no order path, no Binance credential, no Telegram token, and no secret is required or recorded.**
- [ ] The runbook demo path succeeds from a clean data directory.

---

## 7. Human demo path (local)

```bash
# 1. Install and test
uv sync
uv run pytest -q

# 2. Initialise a local data dir and start the daemon
uv run oft init --data-dir ./.oft-data --non-interactive
uv run oft serve
# bound to http://127.0.0.1:7611

# 3. In a second terminal: health and diagnostics
uv run oft status
uv run oft doctor

# 4. Ingest the symbol universe (public exchangeInfo; no key)
uv run oft watchlist search BTCUSDT --format json

# 5. Backfill one symbol/week from data.binance.vision
uv run oft backfill run --symbol BTCUSDT --start 2026-10-01 --end 2026-10-07 --yes
uv run oft backfill status

# 6. Compute and inspect metrics without any LLM
uv run oft scan run --metrics-only --symbol BTCUSDT --period 4h --source backfill
uv run oft report list
uv run oft report show <report_id>
uv run oft report show <report_id> --format json

# 7. Open the read-only surfaces
#    http://127.0.0.1:7611/system
#    http://127.0.0.1:7611/watchlist/BTCUSDT
#    http://127.0.0.1:7611/reports/<report_id>

# 8. Optional live smoke (public market data only)
#    Leave `oft serve` running; wait for the WS checkpoints to advance.
uv run oft status
uv run oft scan run --metrics-only --symbol BTCUSDT --period 4h --source live
```

Expected: every command exits 0 (or 3 on a deliberately missing day), the report shows `CALC` values only, the LLM band is explicitly disabled, the chart matches the CLI values, and `oft doctor` reports no secrets and no order path.

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Archive corrections change history | Immutable Parquet parts + manifest version; re-download on the `updates/` changelog; never rewrite in place. |
| Malicious archive (path traversal, zip bomb) | New empty directory, checksum first, streamed extraction with size limits, no execution, security review (slice 03). |
| WS `/market` routing trap / silent failure | Explicit path table, subscription ack validation, expected-event heartbeat, health alert, recorded routing tests (slice 06). |
| Duplicate/out-of-order events | ID/time dedupe, checkpoints, quarantine, no blind overwrite; replay tests. |
| Missing data presented as measured | First-class `MISSING`/`STALE`/`PARTIAL`/`ESTIMATED`; no interpolation; gap rows carried into metrics. |
| Historical liquidations do not exist | Record forward from deployment; `coverage_start`; `limitations`; never backtest against nonexistent data. |
| TradFi irregular metrics | Resample and stamp `ESTIMATED` with a reason; keep raw timestamps; do not claim measured precision. |
| Metric drift / silent regressions | Golden files, metric versions, immutable outputs, replay hashes. |
| SQLite/DuckDB lock contention | Daemon is the only writer; CLI diagnostics are read-only; short SQLite transactions; Parquet immutable. |
| Scope creep into LLM/orders | Milestone boundary is explicit; slices register no LLM/order jobs or imports; CI import-linter and a test asserting no `llm_calls` rows. |
| CLI contract drift | No new top-level groups; only the additive `--metrics-only` flag; update F12 docs in the same PR. |
| Fixture staleness | Source/date/checksum metadata; opt-in refresh; large-window job separate from default CI. |

---

## 9. Open questions for implementation (non-blocking)

1. Exact backfill window and watchlist size for the first real run; validate one week per symbol before scaling.
2. Whether the `forceOrder` stream belongs on `/public` or `/market` in the current exchange revision; resolve with a subscription-ack test, not by assumption. Research confirms the stream names and payload, not the path for this specific stream.
3. The dedicated report read model: milestone 1 embeds `market-snapshot` + `orderflow-metrics` in the API envelope. If a `report.schema.json` is wanted, add it additively in Milestone 2.
4. The exact value-area tie-break rule and price-bin multiple; fix them in the metric version and golden files (default: 70%, tick-size bins, deterministic lower-price tie-break).
5. Whether to keep `bookDepth` archives in Milestone 1. Plan: ingest/store the coarse bands for quality context but do not claim a DOM ladder.

---

## 10. Slice-to-preview traceability

| Slice | Preview surfaces |
|---|---|
| 01 | `/system` shell + global status bar; S03.2/S03.3/S03.4 (fixture metrics-only) |
| 02 | S02.2 `/watchlist/:symbol`; S02.3 search; S02.4 `oft watchlist` (read-only) |
| 03 | None; feeds S02.2 coverage and S04.2/S04.3 quality |
| 04 | None; feeds S04.2 CALC band |
| 05 | None; feeds S04.2 CALC band and liquidation-coverage note |
| 06 | None; feeds `/system` and S03.5 status |
| 07 | None; feeds S04.2/S04.3 quality badges |
| 08 | S03.2, S03.3, S03.4, S04.1, S04.2, S04.3, S04.4 |
| 09 | S03.1 schedule state (read-only) |
| 10 | None |

---

*End of plan. No gate is approved by this document. G3 remains pending; this plan is the input to G3.*
