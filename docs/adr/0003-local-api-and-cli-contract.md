# ADR-0003: Local API and CLI contract

**Date**: 2026-11-06
**Status**: accepted
**Deciders**: architect (Stage 5); human approves at G3

## Context

The PRD requires two first-class entry points with full capability: a CLI and a Web UI (MVP 10). The design adds strict requirements: the CLI is the reference surface for automation; `oft status` and `oft doctor` must work without a running daemon or network; JSON output must be stable; exit codes must be branchable; the Web UI must edit settings, accept/reject triggers, and stream live state. The preview (G2) defines routes and screen states that the API must support.

A single-user local product does not need a public-API versioning burden, but it does need one authoritative contract so the CLI, SPA, and future automation cannot drift. The contract-first skill applies: one machine-checkable artifact per boundary.

## Decision

Expose a **versioned local REST API** at `/api/v1` plus a **server-sent events (SSE)** stream for server-to-client updates. The **CLI is a client of the same API**, with a narrow read-only fallback for diagnostics when the daemon is down. Both are generated from / validated against the JSON Schemas in `docs/contracts/`.

### Transport choice

- **Commands and queries**: HTTP/1.1 REST over `127.0.0.1`, JSON only, UTF-8.
- **Server push**: SSE at `/api/v1/events?topics=...`, one long-lived connection per browser tab. SSE was chosen over WebSocket because the UI only needs server→client push; SSE gives automatic reconnect with `Last-Event-ID`, plain HTTP semantics, simpler origin checks, and no second framing/auth path. The deferred live-DOM heatmap (milestone 7) may add a WebSocket if measured latency demands it, behind the same event contract.
- **No GraphQL, gRPC, tRPC, or browser→Binance calls.** The browser never sees exchange or LLM credentials.

### Envelope

Every response uses the envelope defined in `docs/contracts/api-envelope.schema.json`:

```json
{
  "ok": true,
  "data": { "...": "..." },
  "error": null,
  "meta": {
    "request_id": "REQ-01J...",
    "generated_at": "2026-11-06T04:12:00.000Z",
    "duration_ms": 42,
    "provenance": "CALC",
    "data_quality": "COMPLETE",
    "warnings": [],
    "pagination": null,
    "idempotent_replay": false
  }
}
```

Errors use `ok:false`, `data:null`, and an `error` object with a stable code from `docs/contracts/error-catalog.json`, an HTTP status, `retryable`, `reference_id`, and field-level details. HTTP status is semantic (400/401/403/404/409/412/422/423/429/500/502/503/504), never 200-for-everything.

### Endpoint surface

| Group | Endpoints | Notes |
|---|---|---|
| System | `GET /health`, `GET /ready`, `GET /api/v1/status`, `GET /api/v1/doctor`, `GET /api/v1/system/metrics`, `GET /api/v1/system/logs` | `health`/`ready` unauthenticated on loopback; logs support `since`, `level`, `follow` via SSE |
| Symbols / watchlist | `GET /api/v1/symbols`, `GET /api/v1/symbols/{symbol}`, `GET/POST/PATCH/DELETE /api/v1/watchlist[/{symbol}]` | Symbol registry cached from `exchangeInfo`; delete is `star=false`, not destructive |
| Scans | `POST /api/v1/scans` (manual run), `GET /api/v1/scans`, `GET /api/v1/scans/{scan_id}` | `POST` requires `Idempotency-Key`; refuses if a scan is running (409 `SCAN_ALREADY_RUNNING`) |
| Reports | `GET /api/v1/reports`, `GET /api/v1/reports/{report_id}`, `GET /api/v1/reports/{report_id}/export?format=md|json` | Export preserves CALC/LLM provenance stamps |
| Opportunities | `GET /api/v1/opportunities`, `GET /api/v1/opportunities/{id}`, `GET /api/v1/opportunities/{id}/timeline` | Read-only; code owns state transitions |
| Triggers | `GET /api/v1/triggers`, `GET /api/v1/triggers/{id}`, `POST /api/v1/triggers/{id}/recheck`, `POST /api/v1/triggers/{id}/accept`, `POST /api/v1/triggers/{id}/reject` | Accept/reject require `Idempotency-Key` and a confirmation record; live accept requires the arming gate |
| Positions | `GET /api/v1/positions`, `GET /api/v1/positions/{id}`, `POST /api/v1/positions/{id}/manage` | `manage` requests a new LLM recommendation; execution still passes the risk gate |
| Paper | `GET /api/v1/paper/{account,orders,fills,equity}`, `POST /api/v1/paper/reset` | Reset requires typed `RESET`; resets the Live gate |
| Journal | `GET /api/v1/journal`, `GET /api/v1/journal/{trade_id}`, `GET /api/v1/journal/stats`, `GET /api/v1/journal/compare/{trade_id}` | Read-only; LLM review is advisory |
| Settings | `GET/PUT /api/v1/settings/{llm,prompts,risk,scan,budget,telegram,live}` | PUT is a scoped diff; prompt edits create a new version |
| Live / kill | `GET /api/v1/live/status`, `POST /api/v1/live/enable`, `POST /api/v1/live/disable`, `POST /api/v1/kill-switch`, `DELETE /api/v1/kill-switch` | Kill switch activation is always allowed and never prompts; deactivation requires `RESUME` |
| Data | `POST /api/v1/backfill`, `GET /api/v1/backfill/{run_id}`, `GET /api/v1/data/quality` | Backfill returns a size estimate first; resumable |
| Events | `GET /api/v1/events?topics=scan,trigger,position,order,budget,system,telegram` | SSE; each event has `id`, `type`, `occurred_at`, `data`, `sequence` |

### Idempotency

Any request that can create state or move money (`POST`/`PUT`/`DELETE` except reads) requires an `Idempotency-Key` header; the CLI generates one and supports `--idempotency-key` for scripted retries.

- Key scope: `(actor, method, path, environment)`.
- The daemon stores `request_hash` and the full response in `idempotency_keys` (TTL 24 h; order-related 30 days).
- Same key + same hash → return the stored response with `meta.idempotent_replay=true`.
- Same key + different hash → 409 `IDEMPOTENCY_CONFLICT`.
- The order path has a second, independent key: `client_order_id` (ADR-0006). The API idempotency key is not a substitute.

### Authentication and CSRF

- The daemon binds `127.0.0.1` by default. `--host` requires an explicit warning and is unsupported for shared use.
- **Browser**: a random session token is set as an `HttpOnly`, `SameSite=Strict`, `Secure` (when HTTPS) cookie by `GET /`; a CSRF token is issued in a non-HttpOnly cookie/response and must be echoed in `X-CSRF-Token` on mutations. Mutations also require `Origin`/`Sec-Fetch-Site` to match the served origin; `Host` must be `127.0.0.1:<port>` or `localhost:<port>`. No CORS headers are emitted.
- **CLI**: a local API bearer token in `$OFT_DATA/run/api-token` (0600), created at first start; `OFT_API_TOKEN` can override for automation. The token is never printed.
- **Telegram**: the bot has no API access; it reads through a read-only service interface (ADR-0010).

### CLI contract

- Binary `oft`, built with Typer. Command tree matches `design/flows.md` F12 exactly.
- Output formats: `--format table|json|ndjson|csv|lines`; `--no-color`, `--color=always`, `--ascii`, `--quiet`, `--verbose`, `--local`, `--non-interactive`, `--confirm-token`.
- JSON output uses the same envelope as the API; `data` is the API `data` object, and `meta.provenance` is `CALC`, `LLM`, or `MIXED`.
- Exit codes: `0` success, `1` runtime, `2` usage, `3` partial success, `4` blocked by risk/guard, `5` auth/config.
- Long-running commands stream stage lines to stderr; `--format ndjson` emits structured events on stdout.
- `oft status` and `oft doctor` never require the daemon: they read `oft.db` read-only, the Parquet manifest, and the filesystem, and report `OFFLINE`/`DEGRADED` rather than failing. Any command that mutates state requires the daemon and returns `DAEMON_NOT_RUNNING` (503) with `oft daemon start`.
- `oft daemon *` is a supervisor control wrapper: `start` runs the daemon in the foreground unless `--detach` is supported by the supervisor; `status` reads the PID/lock files; `logs` reads structured log files.
- The CLI never calls Binance or an LLM provider directly. It has no exchange credentials; it cannot place an order except through `/api/v1/triggers/{id}/accept` or `/api/v1/positions/{id}/manage`.

### Versioning

- URL major version `/api/v1`. Additive fields are allowed without a version bump; clients must ignore unknown fields.
- The payload `schema_version` (`1.x.y`) is carried in every contract object. Breaking changes bump the major URL and the payload major.
- Event types are additive; unknown event types are ignored by clients.
- Contract files are the source of truth; Pydantic models and TypeScript types are generated/validated from them in CI. Handwritten duplicate shapes are prohibited.

## Alternatives Considered

### Alternative 1: WebSocket for all realtime

- **Pros**: bidirectional, low latency, established for trading UIs.
- **Cons**: a second auth/CSRF surface, manual reconnect/heartbeat, more client code, and no benefit because the browser never sends realtime commands (those are REST POSTs).
- **Why not**: SSE is simpler and satisfies the actual direction of data flow. WebSocket remains an option for the deferred DOM heatmap.

### Alternative 2: CLI talks directly to SQLite/Binance, no local API

- **Pros**: fewer moving parts; CLI works with the daemon down.
- **Cons**: two writers, two risk paths, two order paths, and the UI would still need an API. It violates the single-execution-gateway invariant and makes idempotency/audit unenforceable.
- **Why not**: it duplicates the most dangerous code path. The CLI is a client; only the daemon executes.

### Alternative 3: GraphQL

- **Pros**: flexible queries for a dense UI.
- **Cons**: a schema/query surface much larger than needed; harder to enforce command semantics, idempotency, and error codes; extra tooling. The UI's read models are few and stable.
- **Why not**: REST resources map directly to the preview routes and CLI commands; contract-first is simpler.

### Alternative 4: gRPC / tRPC

- **Pros**: typed, efficient.
- **Cons**: browser/gRPC-Web tooling, codegen, and a second protocol; tRPC ties the client to TypeScript and excludes a Python CLI and future automation.
- **Why not**: JSON over loopback is fast enough; the CLI must be a language-neutral client.

## Consequences

### Positive

- One contract artifact serves the CLI, SPA, replay, and tests.
- Stable error codes and exit codes make scripting and alerting reliable.
- The CLI works for diagnostics when the daemon is down, matching F12.
- SSE keeps the push path small and secure.

### Negative

- SSE is one-way; any future two-way realtime feature needs a WebSocket.
- The API adds a process boundary the CLI must handle (timeouts, daemon down, stale cache).
- Local session/CSRF machinery is non-trivial for a single-user product, but it is required to stop a malicious local web page from POSTing to `127.0.0.1`.

### Risks

- **Risk**: contract drift between schema, Pydantic, and TypeScript. **Mitigation**: generate types from schemas; CI validates fixtures and live route responses; no handwritten duplicates.
- **Risk**: CLI retries duplicate a trigger accept or order. **Mitigation**: mandatory `Idempotency-Key` plus `client_order_id`; replay returns the original response.
- **Risk**: SSE connection leaks or stalls. **Mitigation**: heartbeat every 15 s, `Last-Event-ID` replay buffer, per-tab connection limit, disconnect on daemon shutdown.
- **Risk**: a browser extension or local malware reads the session cookie. **Mitigation**: HttpOnly, SameSite=Strict, no CORS, strict CSP, no secrets in the SPA; the threat is documented as residual in ADR-0010.

## Compliance checks

- Every mutating route is idempotent or explicitly documented as non-idempotent (none are).
- Every error response uses a code from `error-catalog.json`.
- The CLI has no exchange/LLM credentials and no direct order path.
- The SPA never calls Binance or an LLM provider directly.
- Route coverage matches the preview's F01–F12 surfaces; the route map is tested against `design/flows.md` in CI.
