# Implementation Plan — Milestone 5: Telegram 只读通道

**Project:** Order-Flow-Trading
**Milestone:** 5 — Telegram 只读通道 (report push, opportunity/position/risk alerts, read-only queries, and budget alerts usable from mobile)
**Status:** plan complete for review; **no gate is approved by this document** (Milestone 5 G3 remains pending)
**Date:** 2026-11-17
**Inputs:** `.claude/prds/order-flow-trading.prd.md` (Milestone 5 row, MVP 14/15, the Telegram permission decision, the out-of-scope list, the prompt-injection and secret risks); `.claude/plans/order-flow-trading-m4.plan.md` (plan format, slice sizing, security posture); `docs/adr/0001`–`0012` (especially 0007 scheduler/jobs, 0010 security/secrets, 0011 testing, 0012 observability); `design/flows.md` (`F10e`, `F10f`, `F11`, `S01.4`, `S11.1`, `S11.2`, `F12`); `design/DESIGN.md` (A9/A10/A11/A12, mobile stance, notification hierarchy); current `src/oft/` tree (`api/routes`, `application`, `adapters`, `cli`, `ports`, `domain`); `CLAUDE.md`.
**Stack:** unchanged — Python 3.12 modular monolith daemon (`oftd`), FastAPI, APScheduler `BackgroundScheduler`, SQLite WAL, Pydantic v2, Typer CLI, `httpx` (already a dependency) for the Telegram Bot API. No new framework, no Telegram SDK, no second process.
**Boundary for this milestone:** exactly PRD MVP 14/15 plus the Telegram surfaces already specified by `F10f`/`F11`/`S01.4`. **No order placement, no approvals, no config mutation from Telegram, no Live/Binance work, no native mobile app, no news/social inputs, no multi-user.** Telegram is a read-only channel; the only writes are operator-configured settings made through the local Web UI/CLI, never through a chat command.

---

## 1. Milestone outcome

Milestones 1–4 delivered the paper loop, the deterministic metrics/trigger/risk/OMS path, the full CLI, the Web settings shell, and the read surfaces that the Telegram channel must reuse. In particular:

- `oft status`, `GET /api/v1/status`, and `build_status` are the canonical system read model;
- `positions`, `opportunities`, `reports`, `budget`, `watchlist`, and `risk` services already expose the payloads Telegram should answer with;
- `SecretsService` already stores write-only secrets in the OS keyring or the encrypted-file fallback, stores only `secrets_meta`, and exposes masked status plus in-process redaction values;
- `BudgetService` already emits `BUDGET_THRESHOLD` audit events at the 80% warn and 100% threshold crossings;
- `AuditEventDraft`/`append_audit_event`, the `config` table, `system_state`, `save_config_change`, and the idempotency helper are available;
- the daemon already starts several independent `BackgroundScheduler` instances and a dedicated ingestion supervisor thread, so a Telegram poller/dispatcher can follow the same lifecycle without touching the scan scheduler;
- `src/oft/logging.py` and `src/oft/cli/commands/system.py` already contain secret-redaction filters (including a Telegram-token-shaped regex).

Milestone 5 closes the mobile channel:

1. **A real end-to-end delivery path** — bot token in the existing secret store; an outbox with delivery receipts; a dedicated long-poll worker; an allowlisted `/status` query answered with the same numbers as the Web UI/CLI.
2. **Read-only query surface** — `/status`, `/positions`, `/opportunities`, `/report [symbol]`, `/budget`, `/watchlist`, `/help`; unknown/write-like commands (`/approve`, `/accept`) receive an explicit "MVP 只读，请在桌面端或 CLI 操作" refusal.
3. **Push events** — scan report summary after a scan, opportunity triggered/invalidated, entry re-check awaiting human action, position management executed/blocked, risk alerts (daily loss, leverage, stale data, ingestion/provider degradation), and budget warnings.
4. **Operator configuration** — `F10f` `/settings/telegram` plus `oft telegram`, write-only token, chat/user allowlist, push toggles, test send, receipts; the deferred `S01.4` onboarding step becomes real.
5. **Security and operability** — token never logged/echoed; fail-closed allowlist; inbound text treated strictly as data; outbound redaction reusing the existing design; bounded queue, per-chat rate limiting, backoff, and 429 handling; a poller that cannot block the scan/trigger/management schedulers; CI-safe fake transport and deterministic tests.

**End-to-end path delivered by the first slice:** the operator runs `oft telegram token set --stdin` (or `PUT /api/v1/settings/telegram/token`) and `oft telegram allowlist add --chat-id … --user-id …`, starts the daemon, and sends `/status` from the allowlisted chat. The daemon's dedicated poller reads the update, authorizes the chat/user, renders the same status numbers as `oft status`, enqueues the reply, the dispatcher sends it with the bot token from the secret store, and `oft telegram receipts` shows the `sent` receipt with Telegram `message_id`. An unknown chat receives `未授权` and no data. The token never appears in logs, API responses, CLI output, receipts, or messages. This path forces the secret subject, allowlist, outbox, receipt, fake transport, poller, dispatcher, API, CLI, daemon lifecycle, and redaction into place before any alert/report slice.

**Non-goals for Milestone 5:** order placement or approval through Telegram; `/approve`, `/accept`, `/reject`, `/cancel`; trade actions; config mutation from a chat; LLM calls from a query handler; Binance keys/live broker; Live arming/gate; paper reset; native mobile app; webhook hosting; a second long-running process; news/social ingestion; per-user accounts; message editing/history import; rich media, inline keyboards, or Telegram Mini Apps.

---

## 2. Binding decisions and contract constraints

| Decision | Plan implication |
|---|---|
| PRD: Telegram MVP is read-only | No Telegram-originated write path exists. The command table is a fixed allowlist; a write-like command returns the read-only refusal. Settings (token, allowlist, toggles) are written only through the authenticated local API/CLI. |
| PRD: token is a secret | The bot token is stored through the existing `SecretsService`/secret backend under a `telegram` subject and `bot_token` name. It is never written to SQLite, `config`, logs, reports, audit payloads, API/CLI output, receipts, or messages. `secrets_meta` keeps only fingerprint/last-4/provenance. |
| ADR-0010: one trust boundary | Incoming updates, chat text, usernames, and Telegram error bodies are untrusted data. They cross into the daemon as data only; no text is evaluated, executed, or sent to an LLM. |
| ADR-0010: local API | New routes use the existing session/CSRF/Origin/Host checks, bearer token for the CLI, no CORS, and `Idempotency-Key` on every mutation. The token endpoints have no reveal operation. |
| ADR-0010: audit | Allowlist changes, token add/rotate/delete, push-toggle changes, test sends, and authorization refusals are audited. Message bodies and tokens are not audit payloads. |
| ADR-0007: scheduler | The outbound dispatcher reuses `_IntervalDaemonScheduler` semantics (`max_instances=1`, `coalesce=True`, durable queue). The inbound long poll runs in one dedicated supervisor thread with a bounded HTTP timeout and a stop event. It never runs inside the scan/trigger/management job and never holds their locks. |
| ADR-0007: catch-up/idempotency | Outbox rows carry a unique `event_key`; a retry, restart, scan resume, or duplicate audit event never sends twice. The poller persists its `update_id` offset in `system_state` so a restart does not replay old commands. |
| ADR-0011: testing | Deterministic fake `TelegramPort`/`httpx` transport, injected clock, no real network in CI, no sleeps. 80% overall coverage; 100% branch on authorization, redaction, dedupe, and backoff logic. |
| ADR-0012: observability | `/api/v1/status`, `oft status`, `oft doctor`, and `/system` expose Telegram configured/poller/dispatcher/receipt state. Logs are structured and redacted; Telegram error text is reduced to a stable code and never echoed raw. |
| DESIGN A10: message shape | Plain text only (no `parse_mode`), no emoji/ASCII tables/box drawing, first line `event · symbol`, `key: value` lines, units, ISO-8601 UTC timestamps, CJK/Latin/digits/basic punctuation, length-capped and control-character-stripped. |
| DESIGN mobile stance | Telegram is the mobile product. The 375px Web UI remains read-only except the kill switch; `F10f` follows the same rule and points to desktop/CLI for edits. |
| No new framework | Use the existing `httpx` dependency behind a `TelegramPort`; do not add `aiogram`, `python-telegram-bot`, a webhook server, Redis, or a second daemon. |
| Contracts | New Telegram payloads get a JSON Schema/contract fixture and the shared envelope. New error codes must exist in `docs/contracts/error-catalog.json` and the envelope enum. Existing payload fields are additive only. |

### 2.1 Existing foundation and compatibility

Milestone 5 extends, not rewrites:

- `src/oft/config.py` already has `telegram_bot_token: SecretStr | None` and includes it in `SECRET_FIELDS`. Environment values remain a CI/dev fallback only; the stored secret wins for production. There is no allowlist, chat id, or push-toggle setting today.
- `src/oft/application/secrets_service.py` is the one secret lifecycle, but it is currently LLM-provider-shaped (`PROVIDERS`, `SECRET_NAME = "api_key"`, `reference()` builds `llm.<provider>.api_key`). Milestone 5 generalizes the subject/name vocabulary and adds `telegram.bot_token`, keeping the existing LLM methods and audit event names (`SECRET_ADDED/ROTATED/DELETED`) as thin wrappers. It does not create a parallel secret store.
- `src/oft/adapters/sqlite_schema.py` is at schema **v29**. New tables are additive and forward-only; the slice records the next free version. No migration is renumbered.
- `src/oft/application/scheduler.py` already provides `_IntervalDaemonScheduler` and `BackgroundScheduler`; the dispatcher is a new subclass, not a new scheduling model.
- `src/oft/application/jobs.py` already claims durable runs and is the place where a completed scan is known. The scan-report push is emitted from the scan outcome, not by tailing logs.
- `src/oft/application/budget_service.py` already emits `BUDGET_THRESHOLD` audit events with period/threshold/state. Milestone 5 maps those durable events to the outbox; it does not add a second budget evaluator.
- `src/oft/application/rate_limiter.py` already exposes an `AlertSink` protocol (429/418 cooldowns). A Telegram alert sink composes with `StructlogAlertSink` rather than replacing it.
- `src/oft/api/app.py` registers routers and hardens responses. A new `routes/telegram.py` is registered; no SPA entry path is needed because `F11` is a chat surface, while `F10f` is covered by the existing `/settings/` SPA entry.
- `src/oft/cli/main.py` has `system`, `budget`, `config`, and no `telegram` group. Milestone 5 adds `telegram`; existing commands and output keep working.
- `src/oft/application/scan_service.py` and `src/oft/api/routes/scans.py` hardcode `delivery: {configured: false, reason: "TELEGRAM_NOT_CONFIGURED…"}`. The unconfigured shape stays; the configured shape becomes a real receipt (additive).
- Several tests currently assert the **absence** of Telegram: `tests/unit/test_security_boundary.py`, `tests/integration/test_management_budget.py`, and `tests/e2e/test_trigger_kill_switch.py`. Milestone 5 narrows these assertions (the Telegram adapter is the only new network egress; no exchange/order module may import it) instead of deleting the security intent. This is an intentional, reviewed boundary change.
- `src/oft/application/onboarding_service.py` and `src/oft/cli/commands/init.py` defer step 4 to Milestone 5. Slice 53 un-defers it through the same settings/secret services.

### 2.2 Scope assumptions (narrow reading)

These are recorded because the PRD row is a summary and the design is broader than the current implementation. They are the narrow reading; if G3 disagrees, adjust before implementation.

1. **The Bot API has no read/delivery receipt.** `sendMessage` returns an accepted message with a `message_id`; it does not prove the client rendered it. The plan's receipt states are `queued → sending → sent → failed/retrying`, where `sent` means "Telegram accepted with a message_id". Surfaces label it `sent`, not `delivered`, unless a future Bot API exposes a real delivery signal. This is an honest correction of the preview's `delivered` label.
2. **One bot, a bounded chat list.** `telegram.chat_ids` supports a small bounded list (default one primary chat, hard cap 10) so the operator can receive pushes on one or a few devices. Per-chat receipt status is stored; one unreachable chat does not stop the others.
3. **Authorization is both chat and user.** A message is accepted only when its `chat_id` is in the configured chat list and, when a user allowlist is configured, its `from.id` is in that list. An empty allowlist fails closed: every chat is refused and the status reports `NO_ALLOWLIST`.
4. **Commands are a closed grammar, not natural language.** Only exact `/command` tokens (with a bounded argument) are recognized. Prose, forwarded text, captions, media, and unknown commands are ignored or answered with the fixed help/refusal text. Message text is never passed to an LLM or used to construct a query.
5. **Query handlers make no LLM calls.** `/report` returns the stored report narrative excerpt and CALC metrics; `/budget` reads the budget read model; none of the handlers resolve a provider or consume LLM budget. This is the primary defense against outbound/query budget blowups.
6. **Outbound is bounded, not hard-capped in business terms.** A bounded queue, per-chat token bucket, global single sender, exponential backoff with jitter, maximum attempts, and 429 `retry_after` handling prevent retry storms. There is no fixed "daily message" business rule; low-priority alerts are coalesced and the queue rejects/drops oldest with a receipt rather than growing without bound.
7. **Daemon-down notification needs an external trigger.** A dead daemon cannot send its own Telegram alert. Slice 51 adds `oft telegram watchdog --once` (plus a systemd `OnFailure=`/cron example in the implementation slice) that reads the token from the secret store, checks daemon health, and sends at most one alert per outage epoch. The daemon does not start a second process; a host-level watchdog remains operator configuration.
8. **Scan push is per completed scan, not per symbol.** The message summarizes `N/M` symbols, status, new opportunities, cost, and report ids/links. Per-symbol push is not in MVP.
9. **Report push contains a summary, not the full narrative.** `F11` `/report` may include a narrative excerpt; the scan push contains status/counts/links only. Both are redacted and length-capped.
10. **F10f and `S01.4` are in scope.** `F10f` is the approved Telegram settings screen; `S01.4` was explicitly deferred from M4 to M5. Both use the same service and never reveal the token.
11. **Onboarding step 4 remains optional.** The operator can finish onboarding without Telegram; the UI says pushes will not arrive. No fake connected state.
12. **All message templates are zh-CN** with English technical terms (`CVD`, `Delta`, `LLM`, `Paper`, `Trigger`, etc.) per DESIGN A10/A11. The renderer is deterministic and unit-tested.
13. **The real adapter is refused under `OFT_ENV=test`.** The composition root constructs the fake/`UnconfiguredTelegramPort` in test environments unless a test injects an explicit transport; CI never reaches `api.telegram.org`. Production/paper uses the real adapter only when a token and allowlist are present.
14. **No webhook, no inline keyboards, no callback queries.** Long polling is the only inbound transport; callback data could imply a write action and is out of scope.
15. **No Telegram token on argv.** `oft telegram token set` reads stdin or prompts with no echo; environment and the secret store are the only other sources.

---

## 3. Slice overview

Order is dependency-driven. Slice 47 is the first end-to-end path (secret → allowlist → poller → query → outbox → receipt). No slice introduces an order/approval path, a second config write path, a second secret store, or a second process.

| # | Slice | Milestone | Depends on | UI | Security | Sec review before merge | Size |
|---|---|---|---|---|---|---|---|
| 47 | `47-telegram-walking-skeleton` | 5 | — | no | yes | **yes** | L |
| 48 | `48-telegram-query-commands` | 5 | 47 | no | yes | **yes** | M |
| 49 | `49-telegram-scan-report-push` | 5 | 47 | no | yes | **yes** | M |
| 50 | `50-telegram-opportunity-alerts` | 5 | 47 | no | yes | **yes** | M |
| 51 | `51-telegram-position-risk-alerts` | 5 | 47 | no | yes | **yes** | M |
| 52 | `52-telegram-budget-alerts` | 5 | 47 | no | yes | **yes** | S |
| 53 | `53-telegram-settings-allowlist` | 5 | 47–52 | yes | yes | **yes** | L |
| 54 | `54-telegram-delivery-reliability` | 5 | 47–52 | yes | yes | **yes** | M |
| 55 | `55-m5-telegram-hardening` | 5 | 47–54 | no | no | no | M |

**Security-review rule:** slices 47–54 must not merge without a security review. Scope: token storage/masking/rotation, allowlist authorization, inbound untrusted text/injection, outbound redaction, secret leakage in logs/API/CLI/receipts, CSRF/origin/idempotency on settings, and rate-limit/backoff behavior. This is a review requirement only; it is not a gate approval.

**Size and PR rule:** every slice is **one issue and exactly one PR**. Do not split a slice across PRs. If a slice grows beyond its scope fence, reduce it to the fence or defer the extra work; do not widen the PR.

---

## 4. Detailed slices

### 47 — `47-telegram-walking-skeleton`

**Milestone:** 5
**Title:** Telegram walking skeleton: secret-backed token, allowlist, outbox/receipt, long-poll `/status`
**Goal:** Prove the complete read-only path on one real command. The operator stores the bot token through the existing secret store, configures one allowed chat/user, starts the daemon, and sends `/status` from Telegram; the dedicated poller authorizes the sender, renders the same status numbers as `oft status`, enqueues the reply, and the dispatcher sends it with a receipt. An unknown chat gets `未授权` and no data. The token never appears in logs, API/CLI output, receipts, or messages. This establishes the secret subject, allowlist config, outbox, receipt, fake transport, poller, dispatcher, API, CLI, and daemon lifecycle that every later slice reuses.
**Scope fence for this slice:** `/status` and `/help` only, plus the infrastructure they require (token lifecycle, allowlist, outbox/receipts, dispatcher, poller, redaction, safe default). No positions/opportunities/report/budget/watchlist commands (slice 48), no scan/trigger/position/risk/budget pushes (49–52), no `F10f` Web screen (53), no retry/429 hardening beyond the minimal bounded sender (54). No `/approve`, `/accept`, or any write command exists, not even a stub that mutates.
**Files expected**
- `src/oft/domain/telegram.py` — new: command grammar (closed token table), `TelegramCommand`/`Update` DTOs, allowlist decision, control-character/zero-width sanitizer, 512-char argument cap, 4096-char message cap, receipt state enum, `event_key` helpers, message-shape constants.
- `src/oft/ports/telegram.py` — new: `TelegramPort` Protocol (`get_me`, `get_updates`, `send_message`), DTOs, typed `TelegramError` with stable codes (`TELEGRAM_NOT_CONFIGURED`, `TELEGRAM_UNAUTHORIZED`, `TELEGRAM_UPSTREAM_ERROR`, `TELEGRAM_RATE_LIMITED`, `TELEGRAM_CONFLICT`, `TELEGRAM_TOKEN_INVALID`).
- `src/oft/adapters/telegram_bot_api.py` — new: Bot API adapter using the existing `httpx` dependency with an injectable `httpx.BaseTransport`; fixed `https://api.telegram.org` host; timeout; token in the request path but never in logged URLs or exception text; no webhook use.
- `src/oft/adapters/telegram_fake.py` — new: deterministic no-network port for `OFT_ENV=test` and compose-time wiring; records sent messages and scripts updates.
- `src/oft/application/telegram_service.py` — new: token status/resolve through `SecretsService`, allowlist read/write through `SettingsService`, authorization, command dispatch, `/status`/`/help` rendering via `build_status`, enqueue, test send, receipt queries.
- `src/oft/application/telegram_dispatch.py` — new: outbox drain (minimal happy path + a bounded retry counter), receipt state machine, `run_once()` for tests; no scheduler ownership yet.
- `src/oft/application/telegram_poller.py` — new: dedicated supervisor thread with stop event, bounded `getUpdates` timeout, offset persistence in `system_state`, `poll_once()` for tests; never runs on the scan scheduler.
- `src/oft/application/telegram_messages.py` — new: pure `/status` and `/help` renderers (first line event, `key: value`, ISO-8601 UTC, units, no emoji).
- `src/oft/application/secrets_service.py` — generalize the subject/name vocabulary; add `telegram.bot_token`; keep existing LLM references and audit events unchanged.
- `src/oft/application/settings_service.py` — `telegram.chat_ids` and `telegram.allowed_user_ids` sections with validation (bounded count, integer ids) and audit `TELEGRAM_ALLOWLIST_UPDATED`; no toggles yet.
- `src/oft/adapters/sqlite_schema.py` — additive migration (next free version, recorded by the slice): `telegram_outbox` (`event_key` unique, kind, chat_id, reply_to_message_id, payload_json, priority, state, attempts, next_attempt_at, message_id, last_error_code, created_at, sent_at) plus state/indexes.
- `src/oft/adapters/sqlite_store.py`, `src/oft/ports/store.py` — outbox append/claim/mark methods, `system_state` offset helpers.
- `src/oft/api/routes/telegram.py` — new: `GET /api/v1/telegram/status`, `POST /api/v1/telegram/test`, `GET /api/v1/telegram/receipts`, `PUT/DELETE /api/v1/settings/telegram/token`, `GET/PUT /api/v1/settings/telegram/allowlist`; idempotency on mutations.
- `src/oft/api/schemas/telegram.py` — new: masked token status (`masked`, `last4`, `source`, `updated_at`; never a value), allowlist, status, receipt.
- `src/oft/api/app.py` — register the router.
- `src/oft/cli/commands/telegram.py` — new: `status`, `test`, `receipts [--limit]`, `token set` (stdin/no echo), `token delete`, `allowlist show|add|remove`; register in `src/oft/cli/main.py`.
- `src/oft/bootstrap.py` — build the Telegram port/service; choose fake in `OFT_ENV=test`; expose it through `AppContainer` (new field).
- `src/oft/cli/commands/daemon.py` — start/stop the poller thread and the 1 s dispatch scheduler with the other daemon components.
- `src/oft/application/health.py` — a `telegram` check for configured/allowlist/poller/dispatcher state (basic; full reliability in 54).
- `src/oft/logging.py` (or a shared redaction helper) — apply the existing Telegram-token-shaped regex and `SecretsService.redaction_values()` to structured logs.
- `tests/unit/test_telegram_commands.py`, `test_telegram_allowlist.py`, `test_telegram_messages.py`, `test_telegram_redaction.py`.
- `tests/integration/test_telegram_secret_api.py`, `test_telegram_outbox_dispatch.py`, `test_telegram_poller.py`, `test_telegram_api.py`, `test_telegram_cli.py`, `test_telegram_daemon_lifecycle.py`.
- `tests/contract/test_telegram_contract.py`.
- `tests/fakes/telegram.py` — scripted fake transport with a call log.
- Update `tests/unit/test_security_boundary.py`, `tests/integration/test_management_budget.py`, `tests/e2e/test_trigger_kill_switch.py` to narrow the "Telegram must not be imported" assertions rather than delete them.
**Tests expected**
- Token set via stdin/API is stored in the secret backend; `secrets_meta` has `telegram/bot_token` with only fingerprint/last-4/backend; the token is absent from SQLite rows, API responses, CLI output, audit payloads, logs, receipts, and outbound messages (assert against a sentinel value).
- `GET /api/v1/telegram/status` reports `NOT_CONFIGURED`, `NO_ALLOWLIST`, `READY`, or `DEGRADED` with no secret; `POST /test` returns a receipt or a stable error code; an unknown/invalid token is `TELEGRAM_TOKEN_INVALID`, never a raw Telegram body.
- `poll_once()` with an allowlisted `/status` enqueues exactly one `query_reply`; the dispatcher sends it with no `parse_mode`, records `message_id`, and leaves state `sent`; the reply numbers equal `build_status`/`oft status`.
- An unknown chat/user gets the fixed `未授权` refusal, no status data, an audit `TELEGRAM_UNAUTHORIZED` event, and no subsequent data even if it repeats or embeds a `/status` inside prose.
- Inbound text with control characters, zero-width characters, an overlong argument, or an unknown command is sanitized/capped/ignored and never reaches an LLM, shell, SQL, or dynamic dispatch.
- The poller offset advances and is persisted; a simulated restart does not replay the same update; duplicate `event_key` enqueue is a no-op.
- Daemon start/stop starts and stops the poller thread and dispatch scheduler; a blocked/failed Telegram call does not prevent a scan/trigger scheduler tick (test with an injected blocking fake transport).
- No source module imports `aiogram`, `telegram`, `python-telegram-bot`, or an exchange/order path from the Telegram adapter; the adapter's only egress is the injected transport.
**UI:** no. **Security:** yes (secret storage/rotation, allowlist, untrusted inbound text, outbound redaction, local API mutation, log redaction). **Security review required:** yes.
**Acceptance criteria**
- [ ] `/status` and `/help` work end to end from an allowlisted chat with the token stored only in the secret store.
- [ ] An unknown chat/user receives a refusal and no data; the refusal is audited and rate-limited.
- [ ] The token never appears in SQLite, API, CLI, logs, receipts, or messages; there is no reveal endpoint.
- [ ] The outbox is idempotent and the receipt records Telegram acceptance with a `message_id`.
- [ ] The poller and dispatcher are independent of the scan/trigger/management schedulers and stop cleanly with the daemon.
- [ ] No write/approval command exists and no query handler calls an LLM.
**Preview screens**
- F11 `S11.2` query reply for `/status` and `/help`.
- F12 `oft telegram status|test|receipts`, token/allowlist configuration.
- F10f data needed for the later screen (token/allowlist only; no Web screen yet).
**Dependencies:** none (builds on Milestones 1–4). **Size:** L.

---

### 48 — `48-telegram-query-commands`

**Milestone:** 5
**Title:** Read-only Telegram query commands over existing read models
**Goal:** Add the remaining `F11` commands — `/positions`, `/opportunities`, `/report [symbol]`, `/budget`, `/watchlist` — using the existing application services/payloads, with deterministic message rendering, honest empty/PARTIAL/STALE states, and an explicit read-only refusal for write-like commands. Query handlers must be fast, authorization-gated, and LLM-free.
**Scope fence for this slice:** read-only command handlers and renderers only. No push events, no settings screen, no Telegram-originated mutation, no `/approve`/`/accept` implementation. `/report` returns the stored report summary and narrative reference; it does not re-run analysis or call a provider.
**Files expected**
- `src/oft/application/telegram_queries.py` — new: pure handlers for positions/opportunities/report/budget/watchlist; each returns a render model, not transport calls.
- `src/oft/application/telegram_messages.py` — extend with renderers for the new commands; shared number/unit/timestamp formatting.
- `src/oft/application/telegram_service.py` — dispatch the new command tokens; keep the closed table; add the write-like refusal (`/approve`, `/accept`, `/reject`, `/cancel`, `/buy`, `/sell`, `/close` → fixed "MVP 只读…" text) without any mutation branch.
- `src/oft/domain/telegram.py` — command arguments (symbol normalization), reply length budgeting, empty/partial stamps.
- `tests/unit/test_telegram_query_renderers.py`, `test_telegram_readonly_refusals.py`, `test_telegram_injection_corpus.py`.
- `tests/integration/test_telegram_queries.py` (fixture DB + fake transport), `test_telegram_query_no_llm.py` (assert no provider resolution/call).
- `tests/contract/test_telegram_query_payloads.py` (rendered shapes and numeric parity with the API).
**Tests expected**
- `/positions` uses `PositionService`/the same open-position payload as `/api/v1/positions`; empty → `没有未平仓持仓。`; a stale mark is labelled `STALE`, not silently shown as fresh.
- `/opportunities` lists open + triggered opportunities with ids and states; empty → the design's empty copy; no ledger transition occurs.
- `/report BTCUSDT` resolves the latest stored report for the symbol; it includes CALC metrics, `data_quality`, and the stored `LLM` narrative excerpt/reference; unknown symbol/report → the exact empty/error copy with a reference id, not a traceback.
- `/budget` reads `BudgetService`/the budget read model and shows day/week/month spend vs thresholds with the `USAGE_UNAVAILABLE`/`unpriced` nuance; it never records a call or changes thresholds.
- `/watchlist` shows starred symbols grouped by contract type, matching `oft watchlist list`.
- Every query is rejected for an unallowlisted chat/user before any read-model work; the refusal does not leak counts or ids.
- `/approve`, `/accept`, `/reject`, `/cancel`, `/buy`, `/sell`, `/close` return only the read-only refusal and leave the database byte-for-byte unchanged (assert audit/order/opportunity tables unchanged).
- An injection corpus (`ignore previous instructions…`, fake `/status` inside prose, a 10k-character message, Unicode bidi/zero-width, a symbol containing markup) is treated as data: no command executes unless it is an exact allowlisted token; output is plain text and control characters are stripped.
- No handler resolves an LLM provider or writes a `llm_calls` row; tests assert `"llm"` is not imported/used on the query path.
- A query whose render exceeds the message cap is truncated with an explicit "…" marker and a link/id to the desktop view; it never splits into an unbounded series of messages.
**UI:** no. **Security:** yes (authorization before reads, untrusted text, read-only enforcement, redaction, no LLM/budget use). **Security review required:** yes.
**Acceptance criteria**
- [ ] All seven `F11` commands answer from the same read models as the Web/CLI.
- [ ] Empty, PARTIAL, STALE, and unavailable states are explicit; nothing is fabricated.
- [ ] Write-like commands are refused and mutate nothing.
- [ ] No query handler calls an LLM or consumes LLM budget.
- [ ] Injection-shaped input cannot change command dispatch or output structure.
**Preview screens**
- F11 command table (`/positions`, `/opportunities`, `/report`, `/budget`, `/watchlist`, `/help`).
- `S11.2` query-reply states (empty/loading/error/partial/success/permission).
- `S11.2` "MVP 只读" refusal for write-like commands.
**Dependencies:** 47. **Size:** M.

---

### 49 — `49-telegram-scan-report-push`

**Milestone:** 5
**Title:** Scan-completion report push with receipts
**Goal:** After each completed scan (scheduled or manual), enqueue exactly one summary push per scan: status (`COMPLETE`/`PARTIAL`/`FAILED`), `N/M` symbols, new-opportunity count, cost, and report ids/links. The scan/report payload's hardcoded `delivery` stub becomes a real receipt when configured, while the unconfigured shape stays stable. Idempotency is keyed by `scan_id`, so resume/retry/duplicate completion never sends twice.
**Scope fence for this slice:** scan completion only. No opportunity/trigger/position/risk/budget pushes (50–52). No per-symbol push. No message editing. No report body/narrative in the push.
**Files expected**
- `src/oft/application/notifications.py` — new: `NotificationService.enqueue(event_type, subject, dedupe_key, priority, payload)` and event builders; one write path to the outbox.
- `src/oft/application/telegram_events.py` — new: scan-summary event builder (pure, redacted payload).
- `src/oft/application/jobs.py` — after the scheduled scan finishes, enqueue once when status is `COMPLETE`/`PARTIAL`/`FAILED`; never block the scan outcome on a Telegram failure.
- `src/oft/api/routes/scans.py` — manual scan completion uses the same notification service.
- `src/oft/application/scan_service.py` — `report_payload()`/`report_summary()` delivery becomes a receipt lookup when configured; keep `configured: false` and the existing reason when unconfigured.
- `src/oft/application/telegram_messages.py` — scan summary renderer with `PARTIAL`/`FAILED` stamps and links.
- `src/oft/application/settings_service.py` — read the `scan_report` push toggle (read-only in this slice; the screen lands in 53).
- `tests/unit/test_telegram_scan_message.py`, `tests/integration/test_scan_telegram_push.py`, `test_scan_delivery_receipt.py`.
**Tests expected**
- A completed scan enqueues exactly one outbox row with `dedupe_key` containing the `scan_id`; resume, retry, duplicate finish, and a second dispatcher pass do not enqueue/send again.
- `COMPLETE`, `PARTIAL` (with degraded/failed symbol counts), and `FAILED` (with the exact failure stage/reason code, no stack trace) produce the documented message shapes.
- The message contains no secrets, raw prompts, provider error bodies, or full narratives; it is length-capped and plain text.
- The scan/report payload still returns `configured: false` and the existing reason when Telegram is unconfigured; when configured it returns the receipt state and `message_id` additively. Existing consumers/fixtures remain valid.
- A Telegram enqueue/dispatch failure leaves the scan `COMPLETE`/`PARTIAL` and records a failed receipt; it never changes the scan's status or report contents.
- A manual scan through `POST /api/v1/scans` follows the same path; an unconfigured/toggled-off channel does not enqueue.
- No message is sent when the scan produced no reports (e.g., an early failure before report persistence), unless the failed-scan alert is explicitly enabled.
**UI:** no (payload only). **Security:** yes (outbound data, redaction, idempotency, no secret leakage). **Security review required:** yes.
**Acceptance criteria**
- [ ] One scan produces at most one Telegram summary, ever, across retries/restarts.
- [ ] The summary matches the scan record and links to the existing report ids.
- [ ] `delivery` is real when configured and unchanged when not.
- [ ] Telegram failure cannot affect scan status, reports, positions, or audit integrity.
**Preview screens**
- F11 push event 1 (scan report summary).
- `S03.3` "已推送 Telegram" and `S03.4` receipt chip (data source only; Web card in 54).
**Dependencies:** 47. **Size:** M.

---

### 50 — `50-telegram-opportunity-alerts`

**Milestone:** 5
**Title:** Opportunity triggered/invalidated and entry re-check alerts
**Goal:** Push the two time-sensitive human-attention events: an opportunity becoming `TRIGGERED` or `INVALIDATED`, and an entry re-check result awaiting human action. The entry alert explicitly says the action must happen on the desktop or CLI; it never exposes an approval shortcut. Events are idempotent per state transition and coalesced to avoid a burst.
**Scope fence for this slice:** opportunity lifecycle and entry re-check notifications only. No position/risk/budget alerts (51–52), no Telegram action, no ledger transition from Telegram, no re-run of the entry LLM.
**Files expected**
- `src/oft/application/trigger_service.py` — enqueue an event after a durable trigger state change (triggered/invalidated), with `dedupe_key` from `(opportunity_id, state, state_version)`.
- `src/oft/application/entry_service.py` (or the existing entry-recheck completion path) — enqueue the "awaiting human action" event with direction, symbol, confidence, and the read-only instruction.
- `src/oft/application/telegram_events.py` — opportunity/entry event builders; payloads contain only what the message renders.
- `src/oft/application/telegram_messages.py` — renderers: first line `机会触发 · BTCUSDT · LONG · 置信度 0.72`, zones/trigger/invalidations, `请在桌面端或 CLI 处理` for entry re-checks.
- `src/oft/application/settings_service.py` — `trigger` and `entry_recheck` push toggles (read-only until 53).
- `tests/unit/test_telegram_opportunity_message.py`, `tests/integration/test_opportunity_alerts.py`, `test_entry_recheck_alert.py`.
**Tests expected**
- A state transition enqueues one event; replaying the same transition, a duplicate trigger tick, or a concurrent evaluator tick cannot enqueue/send twice.
- `TRIGGERED` includes the id, side, zone/trigger levels, invalidation, and confidence with units; `INVALIDATED` includes the invalidation reason code.
- Entry re-check includes the LLM decision id/confidence and the desktop/CLI instruction; there is no `/approve` link or inline keyboard.
- The outbound text is redacted and contains no raw prompt, provider text, or secrets; symbol/label text is sanitized as data.
- A disabled toggle suppresses the event at enqueue time; no orphan row is left behind.
- A Telegram failure does not roll back or delay the opportunity/trigger state change.
- Bursts (multiple symbols in one tick) are coalesced per scan/tick when configured, with a bounded count and a link to `/opportunities`.
**UI:** no. **Security:** yes (outbound content, read-only enforcement, idempotency, redaction). **Security review required:** yes.
**Acceptance criteria**
- [ ] Triggered/invalidated opportunities and entry re-checks arrive once per transition.
- [ ] The entry alert cannot approve, accept, or mutate anything.
- [ ] Disabled toggles suppress sends without affecting the ledger.
- [ ] Telegram failure has no effect on trigger/entry outcomes.
**Preview screens**
- F11 push events 2–3.
- `S05` opportunity states and `S06` entry-recheck "请在桌面端处理" copy (data source only).
**Dependencies:** 47. **Size:** M.

---

### 51 — `51-telegram-position-risk-alerts`

**Milestone:** 5
**Title:** Position management and risk alerts, including an optional external watchdog
**Goal:** Push position-management decisions that executed or were blocked, plus risk alerts: daily-loss breach/approach, leverage/notional cap block, stale market data/WS, provider 429/418 degradation, and a recovery notice. Alerts are deduplicated/cooldowned per kind+subject+period so a repeating condition cannot become a message storm. Add `oft telegram watchdog --once` for a host-level "daemon is down" check that runs outside the daemon.
**Scope fence for this slice:** position/risk/degradation notifications and the outbound watchdog command. No order action from Telegram, no new risk calculation, no change to the risk gate's stricter-of rule. The watchdog is a one-shot CLI command intended for cron/systemd `OnFailure=`; it is not a second long-running process and is not started by `daemon start`.
**Files expected**
- `src/oft/application/management_service.py` — enqueue executed/blocked outcomes after the durable management result.
- `src/oft/application/position_service.py` — enqueue protective/stale/close events that already produce a position event.
- `src/oft/application/risk_utilization.py` and/or the risk-gate call site — enqueue daily-loss/leverage/notional blocks once per crossing; no recalculation.
- `src/oft/application/freshness.py` / `health.py` — enqueue stale→recovered transitions for WS/market data and provider cooldowns; one alert per episode.
- `src/oft/application/rate_limiter.py` — compose a `TelegramAlertSink` with the existing `StructlogAlertSink` for 429/418; keep the log alert.
- `src/oft/application/telegram_events.py` — position/risk/degradation event builders and cooldown keys.
- `src/oft/application/telegram_watchdog.py` — new: read `/health` or the PID/daemon probe once, compare with a persisted outage epoch, send at most one alert and one recovery per epoch.
- `src/oft/cli/commands/telegram.py` — `watchdog --once [--json]`.
- `src/oft/application/settings_service.py` — `position` and `risk` push toggles (read-only until 53).
- `tests/unit/test_telegram_risk_message.py`, `test_telegram_cooldown.py`, `test_telegram_watchdog.py`; `tests/integration/test_position_alerts.py`, `test_risk_alerts.py`, `test_rate_limit_alert_sink.py`.
**Tests expected**
- Executed and blocked management outcomes each enqueue one message with position id, symbol, action, size/price, status, and reason code; no order fields that imply a Telegram action.
- Daily-loss approach/breach and leverage/notional blocks enqueue once per crossing per day/period; a repeated tick is a no-op; a new period can alert again.
- Stale data/WS and provider cooldown produce one `DEGRADED` alert and one `RECOVERED` alert per episode; the structlog alert still fires.
- The `AlertSink` composition does not block the rate limiter; a Telegram failure is recorded and swallowed at the alert boundary.
- `oft telegram watchdog --once` when the daemon is up sends nothing; when down sends one alert; repeated runs within the same outage send nothing; recovery sends one notice only after a prior outage. It never prints the token.
- No alert path imports an exchange/order module or offers a Telegram action; position/order tables are unchanged by a failed send.
- Cooldown/dedupe state survives a restart (persisted, not in-memory).
**UI:** no. **Security:** yes (outbound risk data, read-only enforcement, dedupe/cooldown, secret handling in the watchdog). **Security review required:** yes.
**Acceptance criteria**
- [ ] Position-management and risk events arrive with the documented codes and no action affordance.
- [ ] Alert storms are prevented by durable cooldown/dedupe.
- [ ] A dead daemon can be reported by the optional external watchdog, with no second long-running process.
- [ ] No Telegram path can place, approve, or change an order/position.
**Preview screens**
- F11 push events 4–5, including "daemon down/stale data" risk copy.
- `S07` position states and `S09`/`S10` risk/system states (data source only).
**Dependencies:** 47. **Size:** M.

---

### 52 — `52-telegram-budget-alerts`

**Milestone:** 5
**Title:** LLM budget warning and threshold alerts over Telegram
**Goal:** Map the existing `BUDGET_THRESHOLD` audit events (80% warn, 100% threshold, day/week/month) to Telegram pushes exactly once per crossing per period. The alert states the spend/threshold, the period, and that calls continue (no hard stop). The alert itself must not record an LLM call or alter thresholds.
**Scope fence for this slice:** budget-threshold alerts only. No new budget evaluator, no hard stop, no threshold mutation, no `/budget` command (already in 48), no spend projection changes.
**Files expected**
- `src/oft/application/telegram_events.py` — budget event builder keyed by `(period, threshold_state, period_start)`.
- `src/oft/application/notifications.py` or `budget_service.py` — map the durable `BUDGET_THRESHOLD` audit event to an outbox enqueue without re-evaluating spend.
- `src/oft/application/telegram_messages.py` — budget renderer with spend, threshold, percentage, period, and "不会停止调用".
- `src/oft/application/settings_service.py` — `budget` push toggle (read-only until 53).
- `tests/unit/test_telegram_budget_message.py`, `tests/integration/test_budget_threshold_alert.py`.
**Tests expected**
- Crossing 80% enqueues one warning; crossing 100% enqueues one threshold alert; repeated calls while above the threshold do not enqueue again; a new day/week/month can alert.
- A restart or a duplicate audit row does not resend (unique `event_key`/read of the prior crossing).
- The alert path does not write `llm_calls`, `budget_ledger`, or `budget_settings`; thresholds and the no-hard-stop behavior are unchanged.
- Missing price/unpriced usage is surfaced honestly (`unpriced` count) rather than as zero.
- A disabled toggle suppresses the alert with no orphan row; a Telegram failure leaves the budget ledger unchanged.
**UI:** no. **Security:** yes (outbound spend data, idempotency, no secret/prompt leakage). **Security review required:** yes.
**Acceptance criteria**
- [ ] 80%/100% alerts arrive once per crossing per day/week/month.
- [ ] Restarts/retries do not resend.
- [ ] Budget thresholds remain alert-only; no call is stopped.
- [ ] The alert is derived from the existing ledger/audit event, not a second calculation.
**Preview screens**
- F11 push event 6.
- `F10e` "达到 80% 与 100% 阈值时经 Telegram 告警；不会停止调用" (copy parity).
**Dependencies:** 47 (and the Milestone 4 budget thresholds). **Size:** S.

---

### 53 — `53-telegram-settings-allowlist`

**Milestone:** 5
**Title:** `/settings/telegram` (F10f) and real onboarding step 4
**Goal:** Deliver the approved Telegram settings screen and the CLI equivalents: write-only bot token (set/rotate/delete, masked last-4), chat id, chat/user allowlist, per-event push toggles, test send with a receipt, and recent receipts. Un-defer onboarding step 4 for both Web and `oft init`, using the same services and refusing fake success.
**Scope fence for this slice:** Telegram configuration surfaces only. No Live/Binance settings, no bot command changes, no new push event. Token storage reuses `SecretsService`; non-secret settings reuse `SettingsService`. No duplicate settings implementation in the Web or CLI.
**Files expected**
- `src/oft/api/routes/telegram.py`, `src/oft/api/schemas/telegram.py` — `GET/PUT /api/v1/settings/telegram` (token status, chat ids, allowlist, toggles, test result, recent receipts), `POST /api/v1/telegram/test`, token PUT/DELETE; idempotency and masked-only responses.
- `src/oft/application/settings_service.py` — `telegram` section: chat ids, user allowlist, toggles (`scan_report`, `trigger`, `entry_recheck`, `position`, `risk`, `budget`), validation, source (`default`/`user`), audit `TELEGRAM_SETTINGS_UPDATED`.
- `src/oft/application/secrets_service.py` — token status/rotation wrappers; no reveal.
- `src/oft/api/routes/settings.py` — add the Telegram section to the `GET /api/v1/settings` aggregate (`source`, `last_changed`, configured/masked only).
- `web/src/api/telegram.ts` — typed read/write, CSRF/idempotency, envelope validation.
- `web/src/routes/settings-telegram.tsx` — F10f: token field (write-only, masked after save), chat id, allowlist chips, push toggles, test button/receipt, recent receipts, read-only badge.
- `web/src/components/telegram-receipt.tsx`, `web/src/components/allowlist-editor.tsx` — new.
- `web/src/routes/route-match.ts`, `web/src/App.tsx`, `web/src/components/nav.tsx` (`nav-items.ts`) — `/settings/telegram` route/nav.
- `src/oft/application/onboarding_service.py` — step 4 becomes an optional real step (token/chat/allowlist/test), no longer `deferred`.
- `web/src/routes/onboarding.tsx` — Telegram step component; skip-with-consequence copy.
- `src/oft/cli/commands/init.py` — real step 4 with stdin token, no argv secret; `--non-interactive` flags for chat/allowlist; test send is a separate explicit action.
- `tests/integration/test_telegram_settings_api.py`, `test_telegram_token_lifecycle.py`, `test_telegram_onboarding.py`; `tests/e2e/test_cli_init_telegram.py`.
- `web/src/routes/__tests__/settings-telegram.test.tsx`, `web/e2e/settings-telegram.spec.ts`, `web/e2e/onboarding.spec.ts` (Telegram case).
**Tests expected**
- The screen shows `未配置` with the consequence ("不会推送 Telegram") and keeps test disabled with a reason; after a token is saved it shows the mask/last-4 and never the value or a prefix.
- Token rotate/delete works; delete leaves pushes disabled and the status honest; no endpoint returns the token; the DOM/API/CLI/audit/logs never contain it.
- Allowlist validation rejects malformed/non-integer/over-limit ids, requires at least one chat for pushes, and the "unknown chat gets a refusal" behavior still holds.
- Toggles save idempotently and suppress the corresponding event at enqueue time; the response is masked and source-labelled.
- Test send returns a receipt or a stable failure reason; one unreachable chat does not mark the others failed.
- Onboarding step 4 is optional, resumable, uses the real services, and states that skipping leaves pushes disabled; `oft init --non-interactive` never accepts the token on argv and exits 2 on a missing required step flag.
- 375px is read-only with a desktop/CLI instruction; labels/hints/errors, focus order, and axe pass; no token in the DOM after save.
**UI:** yes. **Security:** yes (token write-only lifecycle, allowlist, CSRF/idempotency, untrusted ids, no secret echo). **Security review required:** yes.
**Acceptance criteria**
- [ ] `/settings/telegram` and `oft telegram` read/write the same settings and secret.
- [ ] The token is write-only, masked, rotatable, deletable, and never echoed.
- [ ] Toggles and allowlist are validated, audited, and effective at enqueue time.
- [ ] `S01.4` is a real optional onboarding step in Web and CLI.
- [ ] The screen is keyboard/a11y compliant and read-only at 375px.
**Preview screens**
- `F10f` `/settings/telegram`.
- `S01.4` Telegram onboarding step.
- F12 `oft telegram status|test|receipts` plus token/allowlist/toggle commands.
**Dependencies:** 47–52 (the toggles cover the events those slices emit). **Size:** L.

---

### 54 — `54-telegram-delivery-reliability`

**Milestone:** 5
**Title:** Delivery reliability, rate limits, conflict handling, and receipt surfaces
**Goal:** Harden the channel: durable queue draining after restart, per-chat ordering, a per-chat token bucket, 429 `retry_after`, exponential backoff with jitter and a maximum attempt count, 401/404 token-invalid handling, 409 another-poller conflict handling, queue bound/coalescing, degraded status in `/system`/`oft doctor`, and a `/system` receipt card. Prove the poller/dispatcher still cannot block other schedulers.
**Scope fence for this slice:** reliability and observability of the existing channel. No new event type, no new command, no settings redesign, no Live/Binance work.
**Files expected**
- `src/oft/application/telegram_dispatch.py` — priority queue, per-chat serialization, token bucket, retry/backoff/jitter, max attempts, 429 `retry_after`, queue cap/coalescing, terminal `failed` with stable codes, restart drain.
- `src/oft/application/telegram_poller.py` — 401/404 → `TOKEN_INVALID` with backoff (no tight loop); 409 → `CONFLICT` with a clear reason; network errors → backoff; stop responsiveness.
- `src/oft/application/health.py` — richer `telegram` check (configured, allowlist, poller state, last successful poll/send, failing-receipt count); `oft doctor` output.
- `src/oft/api/routes/system.py` / `status` — Telegram state in `/api/v1/status` or the system payload; no secret.
- `web/src/routes/system.tsx` (or the existing system screen) — persistent "Telegram pushes failing" alert and a receipt card (`sent/failed/retrying`, last error code, message id).
- `src/oft/cli/commands/system.py` — include the Telegram state in `oft system info`; `oft telegram receipts --limit` remains the detailed view.
- `tests/unit/test_telegram_backoff.py`, `test_telegram_queue.py`, `test_telegram_rate_limit.py`; `tests/integration/test_telegram_restart_drain.py`, `test_telegram_conflict.py`, `test_telegram_degraded_health.py`; `web/src/routes/__tests__/system-telegram.test.tsx`, `web/e2e/system.spec.ts`.
**Tests expected**
- A 429 with `retry_after` sets the next attempt no earlier than that instant (injected clock; no sleep); a 5xx retries with increasing bounded backoff; after max attempts the row is `failed` with a stable code and no further sends.
- Per-chat ordering: messages for one chat are sent one at a time in order; different chats may proceed independently; the global sender count is bounded.
- A blocked/slow poller call and a slow dispatcher send do not delay a scheduler tick (injected blocking fake; assert the scan/trigger scheduler still runs).
- An unauthorized response (401/404) marks the channel `DEGRADED`/`TOKEN_INVALID` and backs off; a 409 marks `CONFLICT` and names another poller without a tight retry loop.
- Restart: queued unsent rows are drained; already-`sent` rows are not resent; duplicate `event_key` is a no-op.
- Queue bound: an event beyond the cap is coalesced or rejected with a receipt; memory/DB does not grow without bound under a sustained burst.
- `/system` and `oft doctor` show the degraded state and receipts; no token, raw Telegram body, or secret appears.
- Failure to send one chat does not mark another chat failed; per-chat status is accurate.
**UI:** yes (`/system` receipt/degraded card). **Security:** yes (rate limits, backoff, error-body sanitization, secret leakage). **Security review required:** yes.
**Acceptance criteria**
- [ ] Retries respect 429/backoff and stop at a terminal `failed`; no retry storm.
- [ ] Per-chat ordering and a bounded queue are enforced.
- [ ] 401/409/network failures are explicit, backed off, and visible in `/system`/doctor.
- [ ] Restart drains the outbox without duplicates.
- [ ] Telegram I/O can never stall the scan/trigger/management schedulers.
**Preview screens**
- `S11.1` delivery states (`sent/failed/retrying`), per-chat status.
- `S03.4` receipt chip and `/system` Telegram card.
- F12 `oft doctor`/`oft system info`.
**Dependencies:** 47–52. **Size:** M.

---

### 55 — `55-m5-telegram-hardening`

**Milestone:** 5
**Title:** Milestone 5 security-regression, determinism, contract, and end-to-end hardening
**Goal:** Prove the milestone as a system: fake-transport end-to-end journeys for every command and push, secret-leak and authorization negatives, injection corpus, redaction coverage, rate-limit/backoff determinism, contract fixtures, receipt/status parity, and documentation of the new commands. This slice adds tests and fixes only; it does not add a feature.
**Scope fence for this slice:** tests, fixtures, documentation, and fixes required to satisfy the milestone's acceptance criteria. No new command, event, setting, or route.
**Files expected**
- `tests/fixtures/telegram/` — scripted updates, allowlisted/unknown chats, injection corpus, report/position/opportunity/budget fixtures, failure responses (401/409/429/5xx/timeout).
- `tests/e2e/test_telegram_channel.py` — one temporary daemon with a fake transport: configure token/allowlist, send each command, trigger each push, assert receipts and no network.
- `tests/integration/test_telegram_secret_leakage.py` — sentinel token scan over SQLite, API responses, CLI stdout/stderr, logs, audit, receipts, messages.
- `tests/unit/test_telegram_no_wall_clock.py` or additions to `tests/test_no_wall_clock.py` — new deterministic modules.
- `tests/contract/test_telegram_contract.py` — status/receipt/settings/message-shape fixtures; error-catalog coverage for every new code.
- `web/e2e/settings-telegram.spec.ts` — 375/768/1440, keyboard, axe, token masking, toggles, receipts (complete any matrix started in 53).
- `docs/contracts/telegram-*.schema.json` and `error-catalog.json` updates, if not fully landed earlier.
- Documentation required by the implementation (README/CLAUDE.md command list, systemd `OnFailure=`/cron watchdog example); no unrelated docs.
**Tests expected**
- Every `F11` command (allowlisted) and every push event (49–52) has one end-to-end test with the fake transport; every unknown chat/user is refused before any data read.
- Secret-leak sentinel: the bot token, LLM keys, raw prompts, and provider error bodies never appear in DB rows, API/CLI output, logs, audit, receipts, or messages. A logs grep and a DOM assertion are included.
- Injection corpus: prose, fake commands, bidi/zero-width, overlong text, symbol/label markup, and a malicious report label cannot alter dispatch, query structure, or output; no LLM is invoked.
- Rate-limit/backoff tests use an injected clock and assert exact next-attempt instants; no sleeps; 429/409/401/timeout are deterministic.
- Contract: status/receipt/settings payloads validate; existing report/position/opportunity/budget fixtures still validate; every new error code is in the catalog and envelope enum.
- Playwright: F10f and `/system` at 375/768/1440; token never in the DOM after save; toggles/receipts; keyboard; axe no new violations.
- Coverage: ≥80% overall; 100% branch on authorization, allowlist, redaction, dedupe/event keys, and backoff.
**UI:** no (test-only; the Web assertions cover 53/54). **Security:** no (but includes security-regression assertions). **Security review required:** no.
**Acceptance criteria**
- [ ] Every MVP 14/15 Telegram path has deterministic CI-safe coverage with no network.
- [ ] Token/secret leakage, allowlist bypass, injection, and retry-storm negatives pass.
- [ ] Contract/error-catalog/fixture coverage is complete.
- [ ] Coverage and no-wall-clock gates pass.
**Preview screens**
- `A12` Telegram criteria (no emoji/ASCII art; first line event+symbol; units; ISO-8601 UTC).
- `S11.1`/`S11.2` acceptance states and `F10f`.
**Dependencies:** 47–54. **Size:** M.

---

## 5. Cross-slice engineering requirements

### 5.1 Testing matrix

- **Unit:** command grammar/sanitizer; allowlist decision; message rendering (shape/length/units/timestamps/no emoji); redaction; receipt state machine; event keys/dedupe; backoff/429 math; watchdog epoch logic.
- **Integration:** token lifecycle through the secret backend; allowlist/settings audit/idempotency; outbox transaction and restart drain; poller offset/authorization/conflict; dispatcher with fake transport; scan/trigger/entry/management/risk/budget event emission; `/system`/doctor degraded state; daemon start/stop lifecycle.
- **Contract:** `api-envelope`; Telegram status/receipt/settings; message-shape fixtures; every new error code in the catalog and envelope enum; existing report/position/opportunity/budget fixtures remain valid.
- **Replay/determinism:** no wall clock in new domain/application modules; injected clock for backoff/cooldowns; fake transport and scripted updates; no sleeps; Telegram sends never alter replay outputs.
- **E2E:** temporary daemon + fake transport for commands and pushes; CLI token/allowlist/init/watchdog; Web F10f and `/system` at 375/768/1440; keyboard/axe; no console errors; no real network.
- **Coverage:** ≥80% overall; 100% branch on authorization, redaction, dedupe/event keys, and backoff.

### 5.2 Security triggers and controls (all slices)

- **Secret:** token only in the keyring/encrypted fallback; `secrets_meta` fingerprint/last-4/backend only; no reveal; no token in argv/stdin echo; logs/API/CLI/receipts/messages redacted; redaction values come from `SecretsService` plus the existing key-like patterns.
- **Authorization:** fail-closed chat+user allowlist; authorize before any read-model call; refusals audited and rate-limited; unknown chats get a fixed refusal with no counts/ids.
- **Untrusted inbound text:** closed command grammar; exact-token dispatch; control/zero-width/bidi stripping; length caps; plain text; no LLM, shell, SQL, `eval`, dynamic import, or tool dispatch; media/captions ignored.
- **Outbound:** plain text (no `parse_mode`); report content through `redact_report_payload`; secrets/prompts/provider bodies never included; length cap; no inline keyboards/callback data.
- **Read-only fence:** no Telegram path can create/modify an order, position, opportunity, trigger, risk verdict, budget setting, or non-Telegram config. Tests assert table immutability for write-like commands.
- **Local API:** session+CSRF+Origin/Host for browser, bearer for CLI, idempotency on mutations, no CORS, strict CSP; token endpoints masked-only.
- **Delivery:** bounded queue, per-chat single sender, token bucket, 429 `retry_after`, exponential backoff+jitter, max attempts, terminal failure receipt, per-chat isolation, restart idempotency.
- **Isolation:** the poller is a dedicated thread; the dispatcher is its own interval scheduler; neither holds scan/trigger/management locks nor runs LLM/exchange calls.
- **Audit:** `TELEGRAM_ALLOWLIST_UPDATED`, `TELEGRAM_SETTINGS_UPDATED`, `SECRET_ADDED/ROTATED/DELETED` (telegram subject), `TELEGRAM_TEST_SENT`, `TELEGRAM_UNAUTHORIZED`, `TELEGRAM_DELIVERY_FAILED`; no message body or token in payloads.

### 5.3 Contract and observability

- New payloads get a JSON Schema or contract fixture before implementation; Pydantic/TS types are not handwritten duplicates of a different shape.
- Every response uses the shared envelope; provenance is `CALC` for status/queries and `CALC` for receipts; `data_quality` is `COMPLETE`/`PARTIAL`/`MISSING` honestly.
- `GET /api/v1/telegram/status` and `/api/v1/status` expose configured/allowlist/poller/dispatcher/receipt state; no secret.
- `oft doctor` reports Telegram token/allowlist/poller/conflict/failing-receipt state.
- Logs are structured JSON with redaction; the token is redacted in URLs, exception text, and provider/Telegram bodies.
- Receipts distinguish `sent` (Telegram accepted, with `message_id`) from an unobservable client delivery; labels do not overclaim.

---

## 6. Delivery order and PR rules

1. 47 (first end-to-end path), then 48 (queries), 49/50/51/52 (push events; 49–52 may proceed in parallel after 47), then 53 (settings/onboarding; needs the toggles from 49–52), 54 (reliability), 55 (hardening).
2. Every slice is one issue and one PR. Do not split a slice across PRs; do not renumber earlier slices (Milestones 1–4 use ids 01–46).
3. No slice may add order placement/approval, Binance credentials, Live arming, a second secret store, a second Telegram process, or a second settings write path.
4. Keep existing tests green. Additive API fields only; do not rename/remove existing fields. Existing CLI commands keep their flags/exit codes; `oft telegram` is additive. The narrowed "no Telegram import" security tests must preserve the no-exchange/no-order intent.
5. Security review is required before merge for slices 47–54. The review is not a gate approval.
6. If a slice grows beyond its fence, reduce scope or defer the extra work; do not widen the PR.
7. The real Telegram adapter must not be constructed under `OFT_ENV=test`; CI uses the fake transport. No test depends on a real bot token or network.

---

## 7. Explicitly out of scope / deferred

| Item | Deferred to | Reason |
|---|---|---|
| Telegram-originated orders/approvals (`/approve`, `/accept`, inline keyboards) | Milestone 6+ or never | PRD Decision: Telegram MVP is read-only; an approval path is a money-moving write and needs the Live gate |
| Telegram-driven config mutation (risk, scan, prompts, budget) | Not planned | Read-only decision; local Web/CLI remain the only write surfaces |
| Binance keys, Live arming/gate, live broker | Milestone 6 | M5 has no exchange credentials or order path |
| Native mobile app / Telegram Mini App | Not planned | PRD out of scope; Telegram covers mobile |
| Webhook inbound transport | Not planned | Local single machine; long polling only; no public TLS endpoint |
| Message editing/history import/read receipts | Not planned | Bot API cannot provide them honestly; `sent` is the receipt |
| Host-level dead-daemon watchdog daemon | Operator cron/systemd | M5 provides one-shot `oft telegram watchdog`; no second long-running process |
| Per-symbol scan push, per-user subscriptions, multi-bot | Not planned | PRD single user, one scan summary |
| News/social text ingestion | Not planned | PRD out of scope; would widen the prompt-injection surface |
| Telegram media/rich formatting/inline keyboards | Not planned | Plain text is the constrained, safe surface |
| Browser push/email/other channels | Not planned | PRD mobile channel is Telegram |
| LLM-generated Telegram replies | Not planned | Query replies are deterministic read models; avoids cost/injection |

---

## 8. Traceability

| PRD / flow | Slices |
|---|---|
| MVP 14: report push, opportunity/position/risk alerts, read-only queries | 47–51, 53, 54 |
| MVP 14: budget alerts | 52 |
| MVP 15: budget thresholds via Telegram | 52 (existing M4 evaluator) |
| Decision: Telegram read-only; no approval | 47/48/50/51 (fences and refusal tests) |
| Risk: prompt injection via untrusted text | 47/48/55 (closed grammar, injection corpus) |
| Risk: secret leakage | 47/53/54/55 (secret store, masking, sentinel scan) |
| Success metric: notification reachability + receipts | 47/49/54/55 |
| F11 command surface / push events / S11.1 / S11.2 | 47/48/49/50/51/52/54 |
| F10f Telegram settings | 53 |
| S01.4 Telegram onboarding | 53 |
| F12 `oft telegram` / `oft doctor` | 47/51/53/54 |
| ADR-0007 scheduler/jobs | 47/49/54 |
| ADR-0010 security/secrets | 47–55 |
| ADR-0011 testing/determinism | 47–55 |
| ADR-0012 observability | 51/54 |

---

## 9. Validation commands

Default tests are offline:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
npm --prefix web run lint
npm --prefix web run typecheck
npm --prefix web run test
npm --prefix web run test:e2e   # Playwright with temporary daemons and a fake Telegram transport
```

Coverage: `uv run pytest --cov=oft --cov-report=term-missing`; ≥80% overall; 100% branch on authorization, allowlist, redaction, dedupe/event keys, and backoff.

No real Telegram bot token is required in CI. Telegram tests use a scripted fake port/`httpx` transport and temporary data dirs. LLM tests keep using `stub`/recorded responses. The real adapter is never constructed under `OFT_ENV=test`.

---

## 10. Open questions (non-blocking, recorded for G3)

These are assumptions fenced in §2.2; they do not block implementation because the plan explicitly limits or defers them. They are repeated in the handoff.

1. **Delivery receipt label:** the Bot API returns acceptance with a `message_id`, not a client delivery receipt. Approve labelling the state `sent` (and treating the preview's `delivered` as accepted) rather than claiming delivery.
2. **Daemon-down alert:** approve the optional one-shot `oft telegram watchdog --once` for cron/systemd `OnFailure=` instead of a second long-running watchdog process.
3. **Chat count:** approve a bounded chat list (default one, cap 10) rather than exactly one chat.
4. **Push defaults:** approve all six toggles on when the channel is configured (scan, trigger, entry re-check, position, risk, budget).
5. **No Telegram SDK:** approve raw Bot API over the existing `httpx` dependency instead of adding `aiogram`/`python-telegram-bot`.
6. **Onboarding:** approve un-deferring `S01.4` in M5 as an optional real step.
7. **Scan push content:** approve a summary (status/counts/links) and keeping the full narrative in `/report`, not in the push.
8. **Language:** approve zh-CN templates with English technical terms per DESIGN A10/A11.
9. **Outbound cap:** approve a bounded queue/rate limit with no fixed business daily message cap.
10. **Allowlist semantics:** approve requiring both chat and (when configured) user authorization, failing closed when empty.

---

*End of plan. No gate is approved by this document; it defines ordered, thin vertical slices for Milestone 5. No application code, ADR, design, state, approval, or guard file is changed by this plan.*
