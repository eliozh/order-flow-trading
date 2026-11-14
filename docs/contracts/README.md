# Contracts — Order Flow Trading System

Machine-checkable boundaries for the local API, LLM decisions, market data, and the trading ledger. All schemas are **JSON Schema draft 2020-12**. The canonical `$id` values are logical identifiers under `https://schemas.oft.local/v1/`; validators resolve `$ref` from the local `docs/contracts/` directory, never over the network.

## Contract index

| File | Contract | Consumed by |
|---|---|---|
| [`common.schema.json`](common.schema.json) | Shared primitives, enums, conditions, risk request/caps/verdict, provenance, usage/cost, data gaps, pagination | All contracts |
| [`llm-output.schema.json`](llm-output.schema.json) | **Provider-facing** LLM output. The only shape a model may generate. Closed objects, all properties required. | OpenAI/Anthropic/DeepSeek adapter, validation |
| [`llm-decision.schema.json`](llm-decision.schema.json) | **Persisted** LLM decision envelope: model output + code-assigned IDs, subject refs, provenance, validation result | Orchestrator, API, reports, replay |
| [`market-snapshot.schema.json`](market-snapshot.schema.json) | The bounded market input built by code and sent to an LLM | Prompt builder, report, replay |
| [`orderflow-metrics.schema.json`](orderflow-metrics.schema.json) | CVD, footprint cells, volume profile, OI, funding, liquidations, data quality | Metrics engine, report, chart, LLM payload |
| [`opportunity.schema.json`](opportunity.schema.json) | Opportunity ledger entry and state history | Trigger evaluator, API, UI |
| [`trigger-event.schema.json`](trigger-event.schema.json) | Code-decided trigger with condition results and snapshot hash | Execution gateway, API, journal |
| [`position.schema.json`](position.schema.json) | Open/closed position, risk basis, management history | OMS, API, journal |
| [`order.schema.json`](order.schema.json) | Order state machine, idempotency, risk verdict link | Execution gateway, live/paper brokers |
| [`fill.schema.json`](fill.schema.json) | Execution/fill record | OMS, journal, PnL |
| [`trade-log.schema.json`](trade-log.schema.json) | Closed trade, PnL definitions, signal-vs-outcome, advisory review | Journal, review |
| [`budget-record.schema.json`](budget-record.schema.json) | Per-call LLM usage, unit prices, cost, thresholds | Budget ledger, Telegram alerts, settings |
| [`api-envelope.schema.json`](api-envelope.schema.json) | Request/response envelope, error object, SSE event | REST API, CLI, SPA |
| [`error-catalog.json`](error-catalog.json) | Stable error codes, HTTP status, category, retryability, user action | API, CLI, tests |
| [`error-catalog.schema.json`](error-catalog.schema.json) | Validates the error catalog structure | CI, contract tests |

## The code/LLM boundary

The PRD's hard rule is enforced by four contracts plus the architecture:

1. **`llm-output.schema.json` is output-only.** It has no tool names, URLs, credentials, order IDs, or executable fields. It can propose an `entry_recheck.order_intent`, but that object is a *proposal*; only the risk gate can turn it into an order.
2. **`llm-decision.schema.json` adds provenance after validation.** The provider never supplies `provider`, `model`, `prompt_version`, `usage`, or `cost`; the orchestrator fills them. A model cannot forge its provenance.
3. **`trigger-event.trigger_source` is always `CODE`.** An LLM cannot emit a trigger event or change an opportunity state.
4. **`order.risk_verdict_id` is required.** No order exists without a persisted deterministic risk verdict; the execution gateway rejects anything else.

`llm-output.schema.json` is provider-safe: every object lists all properties in `required` and sets `additionalProperties: false`; it uses a flat `decision_type` with nullable role payloads instead of deep unions; it avoids numeric/string-length/array constraints that providers cannot enforce. Provider adapters may strip unsupported keywords (e.g. `format`) but may never add fields or relax `additionalProperties`. Semantic checks that JSON Schema cannot express (confidence range, stop side, zone order, decision-type/payload match) run in code after schema validation.

### Report payload (composed read model)

`GET /api/v1/reports/{id}` is a composition, not a fifth contract: `snapshot` is `market-snapshot.schema.json`, `metrics` is `orderflow-metrics.schema.json`, and `llm` reuses the persisted decision/provenance fields from `llm-decision.schema.json` behind a `configured` flag. A metrics-only report returns `llm = {configured: false, reason}` while the CALC report stays valid. The web report renders the LLM narrative as untrusted text and never renders raw prompts, provider keys, or HTML.

### Opportunity list read model

`GET /api/v1/opportunities?state=…` returns a read-only projection, not the full contract: each item carries `opportunity_id`, `symbol`, `side`, `state`, `confidence`, `zone {low, high}`, `entry_preference`, `trigger_conditions`, `expires_at`, `created_at`, `updated_at`, and `source`, plus the filtered `total`. A `counts` object gives the server-side COUNT per state (`OPEN`, `TRIGGERED`, `EXPIRED`, `INVALIDATED`) so the ledger's tab counts never depend on the fetched page. Rows render from this projection; they never fetch one full contract per row. The opportunity detail still reuses `opportunity.schema.json`; the ledger's stored provenance has no validation verdict, so the UI labels it "from ledger · not re-validated" rather than inventing one.

### Trigger persisted entry decision and re-check

`GET /api/v1/triggers/{id}/decision` returns the trigger's persisted entry decision
(trigger → `entry_decision_id` → the LLM ledger row) with the decision's structured
`provenance` (provider, model, prompt id/version/hash, knowledge artifact, input hash,
usage, cost, `recorded_at`). It is **provider-free**: it never resolves a provider
adapter and never calls an LLM, so a removed/rotated key cannot turn a pure read into an
error. A missing/unreadable row is reported as `status = MISSING`; a present row without
`entry_recheck` or provenance is `status = FOUND` with `data_quality = PARTIAL` and a
named reason.

`POST /api/v1/triggers/{id}/recheck` reuses the **persisted** decision when one is
recorded and never refreshes it. A trigger with **no** recorded `entry_decision_id` has
nothing to reuse, so the default path bootstraps its first decision through the provider
(the same path `accept` uses) and audits `ENTRY_RECHECK`, not
`ENTRY_RECHECK_REFRESHED`. `?refresh=true` explicitly re-runs the provider and persists a
**new** decision row (even when the inputs are unchanged), retains the previous row, and
writes an `ENTRY_RECHECK_REFRESHED` audit event carrying `previous_decision_id`. A
first-ever refresh with no previous row is still the bootstrap path (`refreshed` false).
The trigger's `entry_decision_id` moves to the new row; the card's `refreshed` flag and
wording describe the supersede-and-retain semantics rather than a silent replacement.

### Journal statistics and comparison read models

`GET /api/v1/journal/stats` returns a deterministic statistics read model, not a
record contract: `status` (`DEFINED`/`UNDEFINED`), `sample_size`, wins/losses/ties,
`win_rate`, `expectancy_usd`, `avg_r`, `totals`, per-reason `exclusions`, the resolved
`period {label,start,end,basis}`, and the code-owned `definitions`. Eligible trades are
persisted trade logs with `data_quality.status = COMPLETE`, no `PARTIAL`/`DATA_PARTIAL`
outcome tag, a parseable `closed_at` inside the period, and a parseable finite
`net_pnl_usd`; open positions and excluded rows are counted. With no eligible trade the
metrics are `UNDEFINED` and every metric is `null` (never zero); the envelope
`data_quality` is then `MISSING`.

`GET /api/v1/journal/compare/{trade_id}` is a composition of the trade log, the source
opportunity zone, the trigger-time report snapshot, recorded bars, and the persisted
entry decision. The zone side is `INSIDE`/`ABOVE_ZONE`/`BELOW_ZONE`/`N/A`; the slippage,
first-touch, and decision sides are `OK`/`N/A`/`PARTIAL`, each with named reasons. The
overall `data_quality` is `COMPLETE` only when every side resolved (a measured zone
status counts as resolved), `MISSING` when every side is `N/A`, and `PARTIAL` otherwise.
`AMBIGUOUS_BAR` is explicit.

`GET /api/v1/journal/export?format=csv|json` writes under
`<data_dir>/exports/journal-<UTC stamp>.<ext>`. CSV contains only trade-log read fields
and neutralizes `=`, `+`, `-`, and `@` prefixes with a leading `'`; JSON is a versioned
`{schema_version, generated_at, period, trades, stats}` document. The result's
`neutralized_cells` counts neutralized **cells** (not rows). A readable row whose
`closed_at` is unparseable is counted in `skipped_unreadable` (and logged with its trade
id) rather than silently dropped, so it agrees with the statistics'
`unreadable_trades`. Neither export includes raw prompts, decision payloads, or secrets.

## Validation pipeline

```
provider text
  -> JSON parse (reject markdown fences/empty/truncated)
  -> llm-output.schema.json (Draft 2020-12)
  -> Pydantic model generated from the schema
  -> semantic validators (code)
  -> one repair retry with the validator error
  -> VALID | REPAIRED | REJECTED (+ alert)
  -> persist llm-decision.schema.json with provenance
```

A rejected output never creates an opportunity, never reaches the risk gate, and never becomes an order. A metrics-only report remains valid and usable.

## Numeric and time rules

- Prices, quantities, notional, cost, PnL, and rates are **decimal strings**, never JSON numbers. Application code uses `Decimal`, never `float`, at these boundaries.
- Timestamps are ISO-8601 UTC with a trailing `Z`. Storage and operational display are UTC; local time is display-only and must be labelled.
- IDs are opaque strings; never parse as a number.
- Confidence is 0–1; the UI renders a numeric value plus a band, never "the AI is confident".

## Data-quality rules

- Every metric and snapshot carries `data_quality` and `quality_reasons`.
- `MISSING` and `STALE` are first-class states, not errors. Missing series are never interpolated and presented as measured.
- `ESTIMATED` is only for a documented resampling of irregular samples (e.g. TradFi metrics); the reason is recorded.
- Liquidations carry `coverage_start` and limitations; USDⓈ-M historical liquidations do not exist and must be labelled "since deployment".

## API envelope and errors

- Every REST/CLI JSON response: `{ok, data, error, meta}`.
- Every error: stable `code` from `error-catalog.json`, semantic HTTP status, `category`, `retryable`, `reference_id`, field-level `details`.
- `error-catalog.json` is the source of truth for codes. `api-envelope.schema.json` contains the same enum; CI must assert the two lists are identical. Adding a code requires both files and a test fixture.
- A successful idempotent replay returns the original response with `meta.idempotent_replay=true`; it is not an error.
- `retryable` never authorizes a blind order retry; order retries require reconciliation (ADR-0006).

## Versioning

- URL major `/api/v1`; payload `schema_version` `1.x.y`.
- Additive fields are compatible; clients ignore unknown fields.
- Removing/renaming a field, changing a type, or repurposing an enum value is a breaking change and requires a new URL/payload major.
- Prompt and metric versions are independent of the contract version.

## Local API route groups (summary)

| Group | Representative routes |
|---|---|
| System | `GET /health`, `GET /ready`, `GET /api/v1/status`, `GET /api/v1/doctor`, `GET /api/v1/system/{metrics,logs}` |
| Symbols/watchlist | `GET /api/v1/symbols[/{symbol}]`, `GET/POST/PATCH/DELETE /api/v1/watchlist[/{symbol}]` |
| Scans | `POST /api/v1/scans`, `GET /api/v1/scans[/{scan_id}]` |
| Reports | `GET /api/v1/reports[/{report_id}]`, `GET /api/v1/reports/{report_id}/export` |
| Opportunities | `GET /api/v1/opportunities[/{id}][/timeline]` |
| Triggers | `GET /api/v1/triggers[/{id}]`, `GET /api/v1/triggers/{id}/decision`, `POST /api/v1/triggers/{id}/{recheck,accept,reject}` |
| Positions | `GET /api/v1/positions[/{id}]`, `POST /api/v1/positions/{id}/manage` |
| Paper | `GET /api/v1/paper/{account,orders,fills,equity}`, `POST /api/v1/paper/reset` |
| Journal | `GET /api/v1/journal[/{trade_id}]`, `/stats`, `/compare/{trade_id}`, `/export` |
| Settings | `GET/PUT /api/v1/settings/{llm,prompts,risk,scan,budget,telegram,live}` |
| Live/kill | `GET /api/v1/live/status`, `POST /api/v1/live/{enable,disable}`, `POST/DELETE /api/v1/kill-switch` |
| Data | `POST /api/v1/backfill`, `GET /api/v1/backfill/{run_id}`, `GET /api/v1/data/quality` |
| Events | `GET /api/v1/events?topics=...` (SSE) |

Mutating routes require an `Idempotency-Key` (401/400/409 per catalog) except where explicitly documented. Browser mutations require the session cookie + CSRF token + same-origin checks. The CLI sends a bearer token and never talks to Binance or an LLM provider.

## Contract tests (required in CI)

- Validate every fixture and every real route response against its schema.
- Assert the `errorCode` enum equals `error-catalog.json`.
- Validate every LLM provider fixture (valid, refusal, truncated, empty, malformed) against `llm-output.schema.json`.
- Validate `llm-decision.schema.json` records produced by the orchestrator, including provenance.
- Validate that an order cannot be serialized without `risk_verdict_id`.
- Validate a replay report records `metric_version`, `prompt_version`, and `input_hash`.
- Generate Pydantic/TypeScript types from the schemas; no handwritten duplicate shapes.
