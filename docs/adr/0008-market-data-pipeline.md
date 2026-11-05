# ADR-0008: Market-data pipeline — live ingestion, archive backfill, gaps, metrics

**Date**: 2026-11-06
**Status**: accepted
**Deciders**: architect (Stage 5); human approves at G3

## Context

The system needs USDⓈ-M and TradFi perpetual data from Binance, both live and historical. Research (02-research.md §A) establishes:

- Live: WS streams split by path (`/public`, `/market`, `/private`); a `/market` stream on the bare path fails silently. REST weight limits and `429`/`418` bans.
- History: `data.binance.vision` daily/monthly zips for `aggTrades`, `trades`, `klines`, `markPriceKlines`, `indexPriceKlines`, `premiumIndexKlines`, `bookDepth`, `metrics`, `fundingRate`; checksums; T+1 daily and monthly cadence; 10–30 GB compressed for a 10-symbol watchlist.
- Coverage gaps: no USDⓈ-M historical liquidations (live-only, largest-per-symbol-per-second); no TradFi `bookTicker`; `bookDepth` is coarse ±% bands, not a ladder; TradFi metrics are irregularly sampled; TradFi history starts at listing.
- Metrics: delta/CVD from `aggTrades` `is_buyer_maker`; footprint from time×price taker volume; volume profile/POC/VA; OI delta from metrics/`openInterestHist`; funding; liquidations live-only.

The PRD requires code-owned data acquisition, cleaning, gap detection, and metric computation, with all CALC outputs reproducible.

## Decision

Use a **three-stage pipeline**: normalize → immutable Parquet → versioned metric computation. Live and backfill share the same normalizer and the same metric functions.

### Live ingestion

- One `MarketDataSupervisor` per environment (production/testnet) owns WS connections and REST pollers.
- WS streams (MVP): `<symbol>@aggTrade`, `<symbol>@kline_1m`, `<symbol>@markPrice@1s`, `<symbol>@forceOrder` (or `!forceOrder@arr`), plus `tradingSession` for session flags. `@depth` is optional for a future DOM feature; not required for MVP metrics.
- Correct routing: high-frequency market streams on `/public`, regular market streams on `/market`, user data on `/private`; the client validates the subscription acknowledgment and detects silent failure.
- Normalize each event into the canonical event record (symbol, event_time, source, values, `record_hash`); validate enums/ranges; reject malformed events with a counter and an alert, never a crash.
- Buffer events in a bounded asyncio queue (backpressure: pause reads, spill to a local WAL file, never drop silently). Flush to Parquet part files every 30 s or 50k rows; temp file → fsync → atomic rename → manifest insert.
- Checkpoint per stream (`ingest_checkpoints`: last event time/ID, last flush, gap counters) so restart can detect what was missed.
- WS reconnect: exponential backoff with jitter, resubscribe, then recover the gap from REST `aggTrades` (last 48 h) or archive. Heartbeat/ping with a timeout; a dead connection is a health event.

### Archive backfill

- `BackfillService` downloads from `data.binance.vision` with a per-file manifest: URL, expected checksum, bytes, rows, status, retries.
- Verify every `.CHECKSUM` (SHA256) before conversion; a mismatch marks the file `FAILED` and alerts; never silently skip.
- Resumable per file; parallel downloads capped at 4; exponential backoff on HTTP errors; no scraping of undocumented endpoints.
- Convert CSV → Parquet with the canonical schema; dedupe by `agg_trade_id`/trade id; partition by `symbol/date`; write a manifest row with `input_manifest_hash`.
- Re-check the `data.binance.vision` `updates/` changelog and re-download corrected files; corrections create new parts and a new manifest version (never rewrite an existing part in place).
- Size estimate first (`oft backfill run` and the UI show estimated GB/symbols); require `[y/N]` (design F12).

### Gap detection and data quality

Gap detection runs after ingestion and before every scan/metric computation:

- Expected bars vs present bars per timeframe; missing daily files; live checkpoint age; duplicate/out-of-order event IDs.
- For today's gap: REST `aggTrades` (last 48 h) or klines backfill.
- For older gaps: archive download; if the archive is absent (e.g., TradFi before listing, liquidation history), mark the series `MISSING`/`N/A` with a reason.
- Every metric output carries `data_quality` (`COMPLETE`, `PARTIAL`, `STALE`, `MISSING`, `ESTIMATED`) and `quality_reasons[]`; the UI stamps them (DESIGN §13.2). Never interpolate a missing series and present it as measured.
- Freshness budgets: live mark 60 s, 1m bar 2 min, 4h bar 10 min after close, metrics 15 min after the expected sample. Exceeding the budget marks `STALE` and blocks risk decisions that depend on it.

### Metric computation

- `oft.domain.metrics` is pure: input is a bounded event/bar collection plus parameters; output is metric objects with a `metric_version`.
- Incremental computation on bar close; late events trigger recomputation of the last N bars (configurable, default 2 bars) and a new metric run; the report records the `metric_version` and `input_manifest_hash`.
- Metrics:
  - **Delta/CVD**: `buyVol = Σqty[is_buyer_maker=false]`, `sellVol = Σqty[is_buyer_maker=true]`, `delta = buy − sell`, `CVD(t) = Σ delta`; cross-check with klines `delta = 2×taker_buy_volume − volume`.
  - **Footprint**: per (bar, price bucket): `bidVol = Σqty[is_buyer_maker=true]`, `askVol = Σqty[is_buyer_maker=false]`, delta, total, imbalance ratio; price bucket = tick size or a configured multiple.
  - **Volume profile**: histogram over the lookback; POC = max-volume price; value area = smallest contiguous range containing the configured share (default 70%); the tie-breaking rule is fixed and versioned.
  - **OI delta**: from `metrics_5m` (or `openInterestHist` last 30 days); TradFi metrics are irregular → resample to the target period and stamp `ESTIMATED`; `OI_end − OI_start`.
  - **Funding**: current rate, next funding time, interval, mark/index price, history; 4h vs 8h intervals per symbol.
  - **Liquidations**: live-only from `@forceOrder`; record every event from deployment; `liquidation_coverage.coverage_start`; totals and buckets; label "since deployment"; no historical backfill, no backtest against nonexistent history.
- Metric versions: any algorithm/parameter change creates a new `metric_versions` row and a new output path; old versions remain readable for replay. A report/decision records the version used.

### TradFi specifics

- Treat `contractType`/`underlyingType` as data; maintain a symbol registry because no client understands `TRADIFI_PERPETUAL` semantics.
- History starts at listing (`onboardDate`); surface "历史自 YYYY-MM-DD".
- No `bookTicker` history; no DOM ladder; `bookDepth` is coarse bands only.
- `NO_TRADING` is a liquidity/mark-price regime flag, not a contract halt; the perp continues matching (research §A2). UI shows session shading and a flag; risk may still trade unless the user configures otherwise.
- Funding interval and leverage brackets are fetched per symbol; brackets are signed-only and cached.

## Alternatives Considered

### Alternative 1: ccxt / third-party aggregator for data

- **Pros**: one API, many venues.
- **Cons**: WS is paid (ccxt.pro); no TradFi semantics; less control over Binance-specific archive paths and checksums; extra dependency.
- **Why not**: the product is Binance-only. Official connector + own WS + archive is more direct and auditable.

### Alternative 2: Store raw CSV and query it directly

- **Pros**: no conversion step.
- **Cons**: 4–6× larger, slow scans, no columnar predicate pushdown, repeated parsing.
- **Why not**: Parquet conversion is the point of the storage design (ADR-0002).

### Alternative 3: Use a streaming engine (Kafka/Flink)

- **Pros**: exactly-once, replay, scalability.
- **Cons**: massive operational overhead for one machine and 10 symbols.
- **Why not**: asyncio + Parquet + checkpoints is sufficient; the design values operability.

### Alternative 4: Interpolate or synthesize missing data

- **Pros**: complete-looking charts and metrics.
- **Cons**: fabricates precision; violates the design's "partial data is normal" rule and the PRD's honesty requirement.
- **Why not**: missing data is marked, never invented. `ESTIMATED` is used only for a documented resampling of irregular samples.

## Consequences

### Positive

- Live and backfill produce the same canonical events and the same metrics; replay is meaningful.
- Checksums, manifests, and gap records make data quality auditable.
- Metric versioning allows algorithm changes without invalidating history.
- Liquidation limitations are explicit rather than silently wrong.

### Negative

- Two ingestion paths (WS and archive) must converge; dedupe and ordering need care.
- Parquet part files and manifest rows add bookkeeping.
- TradFi coverage is inherently partial; the UI must carry that complexity.

### Risks

- **Risk**: silent WS routing failure (the `/market` trap). **Mitigation**: explicit path routing, subscription acknowledgment checks, expected-event heartbeat, health alert.
- **Risk**: archive corrections change history after a report. **Mitigation**: manifest versioning, re-download on changelog, and a report that records the input hash.
- **Risk**: liquidation totals are undercounted even live (largest per second). **Mitigation**: label the limitation in the UI/contract; never present totals as complete.
- **Risk**: metric recomputation changes a past report. **Mitigation**: immutable metric versions; a recompute creates a new run/report, never edits the old one.

## Compliance checks

- Every metric has `metric_version`, `input_manifest_hash`, `data_quality`, and `quality_reasons`.
- No missing series is interpolated or silently drawn as measured.
- Liquidations carry `coverage_start` and "since deployment".
- Backfill verifies checksums and is resumable.
- WS routing and rate-limit weights are explicit in code and tested.
