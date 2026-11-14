# Implementation Plan — Milestone 4: 双入口

**Project:** Order-Flow-Trading
**Milestone:** 4 — 双入口 (full-featured CLI; Web UI for configuration, positions, opportunities, journal, and base charts)
**Status:** plan complete for review; **no gate is approved by this document** (Milestone 4 G3 remains pending)
**Date:** 2026-11-15
**Inputs:** `.claude/prds/order-flow-trading.prd.md` (Milestone 4 row, MVP 3/9/10/11/12/13/15, module-boundary constraints, Open Questions); `docs/adr/0001`–`0012` (especially 0003 local API/CLI, 0004 frontend/charting, 0005 LLM providers, 0009 prompt/knowledge, 0010 security/secrets, 0011 testing, 0012 observability); `docs/contracts/` (`api-envelope`, `error-catalog`, `common`, `opportunity`, `trade-log`, `position`, `llm-decision`); `.claude/ecc-workflow/handoffs/05-architecture.md`; `.claude/plans/order-flow-trading.plan.md`, `order-flow-trading-m2.plan.md`, `order-flow-trading-m3.plan.md`; `design/DESIGN.md`; `design/flows.md` (F01, F02, F04, F05, F07–F10, F12); `design/preview/index.html` (approved preview); current `src/oft/` and `web/src/` tree; `CLAUDE.md`.
**Stack:** unchanged — Python 3.12 modular monolith daemon (`oftd`), FastAPI, APScheduler, DuckDB/Parquet, SQLite WAL, Pydantic v2, Typer CLI, React 19 + TypeScript + Vite, Lightweight Charts pinned (the current inline-SVG chart remains the accessible fallback).
**Boundary for this milestone:** exactly the PRD row plus the approved preview's configuration/chart surfaces. **No live broker, no Binance credential, no Telegram delivery, no Live arming, no paper reset, no rule auto-tuning, no news/social text, no multi-user.** Telegram settings (F10f), Live settings (F10g), `/telegram`, live execution, and Binance keys are Milestones 5–6.

---

## 1. Milestone outcome

Milestones 1–3 already delivered the closed paper loop and most of the read surfaces:

- data + metrics pipeline, scan/report/opportunity/trigger/paper loop, position management, journal, advisory review, replay;
- Web routes `/watchlist`, `/watchlist/:symbol`, `/scans`, `/scans/:id`, `/reports`, `/reports/:id`, `/opportunities`, `/opportunities/:id`, `/triggers`, `/triggers/:id`, `/positions`, `/positions/:id`, `/journal`, `/journal/:tradeId`, `/paper`, `/system`;
- the inline-SVG `Chart` component (candles, volume, CVD, volume profile, footprint dock, bar selection, data-table fallback) on report and position detail;
- status bar, kill switch, pause-live-updates primitives, per-screen tests;
- CLI groups `status`, `doctor`, `open`, `serve`, `scan`, `report list|show`, `watchlist list|search|groups|star|unstar`, `backfill`, `daemon start|stop|status|logs`, `opp`, `trigger`, `paper balance|orders|fills|equity`, `pos`, `log`, `prompt`, `knowledge`, `kill`, `llm providers|models|test|usage`, `budget`, `risk`, `replay`.

Milestone 4 closes the remaining dual-entry gaps:

1. **Web configuration** — a settings shell and the five settings screens that do not depend on Milestones 5–6: scan period (F10d), risk caps (F10c), LLM providers/models (F10a), prompt templates (F10b), budget thresholds (F10e). Values are persisted, validated, source-labelled (`default`/`user`), audited, and never expose secrets.
2. **Configuration-adjacent watchlist** — star/unstar and search-add from the Web UI, with `oft watchlist add|remove` aliases; this is the scan-universe configuration required by PRD MVP 3 and preview S02.1/S02.3/S02.4.
3. **Base charts** — the standalone `/reports/:reportId/chart` route, the 15m/1h/4h switch backed by published bars (never fabricated), opportunity/trigger/position overlays, a keyboard-driven crosshair/tooltip, and the data-table alternative.
4. **Opportunity parity** — the remaining preview list/detail deltas (source scan, age, export action); the ledger remains read-only and code-owned.
5. **Full CLI** — `oft config`, `oft init` (paper-only), `oft report export`, `oft system logs|info`, `oft daemon restart`, and `oft llm key set|delete`; missing commands are additive, existing commands and output keep working.
6. **Navigation and accessibility** — the approved preview's global navigation shell, skip links, 375px read-only behavior, keyboard paths, focus management, pause controls, and axe/Playwright coverage.

**End-to-end path delivered by the first slice:** an operator opens `/settings/scan` (or runs `oft config set scan.period 8h`), sees the effective period, its source, and the next run, changes it, receives a success announcement, and the daemon uses the new period at the next UTC boundary — with one audit event and no restart. That path forces the settings store, validation, API, CLI client, Web form, audit, and scheduler integration into place before the other settings screens.

**Non-goals for Milestone 4:** Binance keys/testnet/live broker, Live arming/gate, paper reset, Telegram bot/settings/receipts, `/telegram`, Telegram notifications, LLM budget alerts via Telegram, knowledge-build Web editor, per-role provider routing, temperature/timeout settings, news/social inputs, adaptive strategy tuning, drawing tools/saved layouts/multi-chart grids, live DOM/L2 heatmap, mobile native app.

---

## 2. Binding decisions and contract constraints

| Decision | Plan implication |
|---|---|
| ADR-0003: one API, one CLI client | Settings are exposed under `/api/v1/settings/*`; the CLI uses the same endpoints (or the same application service when the daemon is intentionally absent). No second config path. `GET /api/v1/settings` is an additive section index. |
| ADR-0003: idempotency | Every settings `PUT`/`POST`/`DELETE` requires `Idempotency-Key`; same key + same payload replays the stored response, same key + different payload is `IDEMPOTENCY_CONFLICT`. The existing `PUT /api/v1/budget` gains optional replay without breaking old callers. |
| ADR-0003: CLI contract | `oft config show|get|set|validate`, `oft init`, `oft report export`, `oft system logs|info`, `oft daemon restart`, `oft llm key set|delete` use the shared envelope, stable exit codes (`0/1/2/3/4/5`), `--json`, and no secrets in output. |
| ADR-0004: routes and accessibility | `/settings`, `/settings/llm`, `/settings/prompts`, `/settings/prompts/:promptId`, `/settings/prompts/:promptId/history`, `/settings/risk`, `/settings/scan`, `/settings/budget`, `/onboarding/:step`, `/reports/:reportId/chart` match the preview. 1440/1280/1024/768/375 layouts; 375px read-only except the kill switch; keyboard and data-table alternatives are build requirements. |
| ADR-0004: chart scope | Base chart = candles, volume, CVD, volume profile, footprint for the focused bar, opportunity zones/trigger lines, entry/exit/stop/target markers, crosshair/tooltip, 15m/1h/4h switch, data-quality badges, symbol/report jump. Session shading and advanced drawing remain deferred because the current snapshot writes `underlying_session=UNKNOWN`. |
| ADR-0005: provider abstraction | One active provider (`OFT_LLM_PROVIDER` equivalent) with per-role models. Settings may edit the active provider, the four role models, official base URLs, and concurrency. Per-role provider routing and temperature/timeout are not in the current adapter and are not invented here. |
| ADR-0009: prompts | Prompt edits create a new immutable version; activation is explicit; restore creates a version, never deletes; the active version is pinned; every decision keeps its prompt version/hash provenance. The existing `/api/v1/prompts` routes remain authoritative. |
| ADR-0010: secrets | Provider keys are write-only: write, mask last-4, rotate, delete; there is no reveal endpoint and no key in API/CLI/Web/DB/log/prompt/export. Storage is OS keyring first, encrypted file fallback with `OFT_MASTER_KEY`; if neither is available the write is refused with an explicit error. `secrets_meta` stores fingerprints/last-4 only. |
| ADR-0010: local security | Settings mutations use the existing session + CSRF + Origin/Host checks; no CORS; CSP stays strict; prompt text, LLM narrative, symbol names, and opportunity labels are untrusted data rendered as text. |
| ADR-0011: testing | Deterministic fixtures, injected clock, no wall clock in domain/application, 80% coverage floor, 100% branch on risk caps/validation and settings diff logic. Playwright + axe for UI slices. |
| ADR-0012: observability | Settings/secret/watchlist/export actions write audit events; `oft doctor`/`/api/v1/status` surface config source and last change; no telemetry; logs redact secrets. |
| Contracts | Every response uses `api-envelope.schema.json`; settings, chart, and export payloads get contract fixtures; every new error code must exist in `error-catalog.json` and the envelope enum; prompt/opportunity/trade-log/position fixtures stay valid. |

### 2.1 Existing foundation and compatibility

Milestone 4 extends, not rewrites:

- `src/oft/config.py` owns `Settings` from `OFT_*` environment variables. It has no persisted runtime config, no `config` table, no settings service, and no write API. `Settings` remains the **default source**; persisted user rows overlay it.
- `src/oft/adapters/sqlite_schema.py` is at schema **v25**. New tables/columns are additive and forward-only: `config` (scan settings and onboarding progress), `secrets_meta` (key fingerprints/last-4), and any chart/export indexes. The actual next version number is recorded by the slice; no migration is renumbered.
- `src/oft/adapters/sqlite_store.py` exposes `get_system_state`/`set_system_state` and `save_risk_caps`. Risk caps and budget thresholds keep their existing authoritative storage; the settings service delegates to those services rather than duplicating them.
- `src/oft/application/scheduler.py` supports only `4h`/`8h` (`SCAN_PERIODS`); the daemon builds `ScheduleState` from settings and already records pending period changes. Milestone 4 wires the persisted scan period into that path; it does not add 1h/2h/12h/24h.
- `src/oft/application/llm_orchestrator.py` selects one provider and resolves role models from `Settings.model_for_role_map()`. It has read/test endpoints (`/api/v1/llm/providers|models|test|usage`) but no write path. The container must refresh effective settings/LLM runtime after a successful settings save without a process restart.
- `src/oft/api/routes/prompts.py` already implements list/show/history/diff/version-create/activate/restore with idempotency. Milestone 4 adds the Web editor and a read-only `/api/v1/settings/prompts` aggregate; it does not add a second prompt write path.
- `src/oft/api/routes/budget.py` already implements `GET/PUT /api/v1/budget` and usage. Milestone 4 adds the Web screen and idempotent replay; the threshold model is unchanged.
- `src/oft/api/routes/llm.py` already exposes provider/model catalogs, a capability probe, and usage. Milestone 4 adds settings writes and the secret store.
- `src/oft/api/routes/symbols.py` already has `PATCH /api/v1/symbols/{symbol}/star`; `src/oft/cli/commands/watchlist.py` already has `star|unstar`. `GET /api/v1/watchlist` is read-only and lacks per-row star state. Milestone 4 adds the ADR-0003 watchlist mutations and the Web toggles.
- `web/src/routes/route-match.ts` and `App.tsx` have no `/settings`, `/onboarding`, or `/reports/:id/chart` routes and no global navigation component. `web/src/components/chart.tsx` is reused; it already has the data-table fallback, keyboard bar selection, entry marker, and memoization.
- `src/oft/api/app.py` `_SPA_ENTRY_PATHS` must add `/settings`, `/settings/`, `/onboarding`, `/onboarding/`, and the chart sub-route is already covered by `startswith("/reports/")`.
- The existing entry/trigger/position/journal/paper behavior stays unchanged. No settings change may alter a historical report, decision, trade log, or audit row.

### 2.2 Scope assumptions (narrow reading)

These are recorded because the PRD row is a summary and the approved preview is broader than the current implementation. They are the narrow reading; if G3 disagrees, adjust before implementation.

1. **Scan periods are 4h/8h in Milestone 4.** PRD MVP 3 says “默认 4h/8h”; the preview offers 1h–24h. The scheduler supports only 4h/8h today. The settings screen exposes 4h/8h and shows other preview values as unavailable; expanding the scheduler is a separate change.
2. **No temperature/timeout settings.** The preview's F10a lists them, but `Settings` and the provider adapters have no temperature/timeout fields. Milestone 4 edits provider, role models, official base URLs, concurrency, keys, and test/usage only.
3. **Analysis lookback is read-only in this milestone.** F10d lists a 3–7 day lookback, but no analysis setting or pipeline wiring exists. The screen shows the current effective window as read-only and does not persist a dead value.
4. **Onboarding is paper-only.** F01 includes Binance (Milestone 6) and Telegram (Milestone 5) steps. Milestone 4 delivers environment/local-bind, LLM providers, watchlist + schedule, budget, and review; Binance/Telegram/Live steps are visible but explicitly deferred, never silently skipped.
5. **Watchlist star mutation is in scope.** PRD MVP 3 and preview F02 require user starring; the current screens are read-only. This is configuration of the scan universe, not a new trading capability.
6. **Global navigation is in scope.** It is absent today and is required by the approved preview and WCAG 2.2 AA keyboard navigation; it is not a new product feature.
7. **The prompt editor is the only editor.** The approved preview/flows have F10b but no knowledge Web screen. `oft knowledge` remains CLI-only; the M2 note about a knowledge editor is not turned into an invented Web screen.
8. **Prompt edits never go through a generic settings PUT.** `/api/v1/prompts` remains authoritative; `/api/v1/settings/prompts` is a read aggregate only.
9. **Chart timeframe data is never fabricated.** 15m/1h/4h switch uses published bars/metrics for the requested period. If a period is absent, the control is disabled with a reason; no upsampling or interpolation.
10. **Session shading is deferred.** The current snapshot writes `underlying_session=UNKNOWN`; drawing a session would invent data. Defer to Milestone 7 or a data-pipeline follow-up.
11. **Secrets storage follows ADR-0010.** OS keyring first; encrypted-file fallback requiring `OFT_MASTER_KEY`; refuse the write rather than store plaintext. Adding `keyring`/`cryptography` is a reviewed dependency change.
12. **Settings mutations are idempotent.** New routes require `Idempotency-Key`; the existing budget PUT accepts an optional key and replays when present.
13. **`oft init` is resumable and non-interactive-capable.** Per-step flags mirror the F12/F01 sequence; non-TTY without the required flags exits `2`. No secret is accepted on argv.
14. **`oft report export` is CLI-first.** The Web report screen may link to the export endpoint, but the milestone’s export acceptance is the CLI/API path; the journal CSV export is unchanged.
15. **Opportunity parity is presentation-only.** The ledger keeps code-owned state transitions; Milestone 4 adds source scan, age, and export, not accept/reject/cancel.
16. **Paper reset remains out of scope.** The Milestone 3 plan moved it to Milestone 6 or a dedicated follow-up; Milestone 4 does not implement it.
17. **Telegram/Live settings and routes are deferred.** `/settings/telegram`, `/settings/live`, `/telegram`, live arming, and Binance keys belong to Milestones 5–6. The settings shell may show disabled cards with those milestone labels, but no fake form.
18. **Full CLI format parity is partial.** Existing commands use `--json`/`--format table|json`; Milestone 4 adds missing commands and stable JSON. A global `--format ndjson|csv` refactor across every group is not done here.

---

## 3. Slice overview

Order is dependency-driven. Slice 32 is the first end-to-end path (settings store → API → CLI/Web → scheduler). No slice introduces live execution, Telegram, paper reset, Binance credentials, or a second prompt/risk/config write path.

| # | Slice | Milestone | Depends on | UI | Security | Sec review before merge | Size |
|---|---|---|---|---|---|---|---|
| 32 | `32-settings-scan-walking-skeleton` | 4 | — | yes | yes | **yes** | L |
| 33 | `33-navigation-shell` | 4 | 32 | yes | no | no | M |
| 34 | `34-settings-risk-caps` | 4 | 32 | yes | yes | **yes** | M |
| 35 | `35-settings-llm-providers` | 4 | 32 | yes | yes | **yes** | L |
| 36 | `36-settings-prompt-editor` | 4 | 32 | yes | yes | **yes** | L |
| 37 | `37-settings-budget-thresholds` | 4 | 32 | yes | no | no | S |
| 38 | `38-watchlist-star-config` | 4 | 33 | yes | yes | **yes** | M |
| 39 | `39-chart-panel-route` | 4 | 33 | yes | no | no | L |
| 40 | `40-chart-overlays-crosshair` | 4 | 39 | yes | no | no | M |
| 41 | `41-opportunities-ledger-parity` | 4 | 33 | yes | no | no | S |
| 42 | `42-report-export-cli` | 4 | — | no | yes | **yes** | M |
| 43 | `43-cli-system-diagnostics` | 4 | — | no | yes | **yes** | S |
| 44 | `44-onboarding-web` | 4 | 32, 35, 37, 38 | yes | yes | **yes** | L |
| 45 | `45-cli-init` | 4 | 44 | no | yes | **yes** | M |
| 46 | `46-m4-a11y-e2e-hardening` | 4 | 32–45 | no | no | no | M |

**Security-review rule:** slices 32, 34, 35, 36, 38, 42, 43, 44, and 45 must not merge without a security review. Scope: settings/config mutation and idempotency, secret storage/masking/rotation, prompt text and XSS, watchlist mutation, export path traversal and untrusted export content, log redaction, onboarding key entry, and CSRF/origin behavior. This is a review requirement only; it is not a gate approval.

**Size and PR rule:** every slice is **one issue and exactly one PR**. Do not split a slice across PRs. If a slice grows beyond its scope fence, reduce it to the fence or defer the extra work; do not widen the PR.

---

## 4. Detailed slices

### 32 — `32-settings-scan-walking-skeleton`

**Milestone:** 4
**Title:** Settings walking skeleton: persisted runtime config, `/settings/scan`, and `oft config`
**Goal:** Prove the complete configuration path on one real setting: the scan period. The operator reads the effective period, its source (`default`/`user`), and the next UTC boundary; changes it in the Web UI or CLI; the daemon applies it at the next boundary without a restart; one audit event is written. This establishes the `config` table, typed validation, source precedence, idempotency, audit, API, CLI, Web shell, and scheduler integration that the other settings slices reuse.
**Scope fence for this slice:** `scan.period` only. No risk caps, LLM providers, prompts, budget, watchlist stars, onboarding, secrets, or secret storage. Show F10d's read-only fields with their effective values; do not persist a lookback that no pipeline consumes. No 1h/2h/12h/24h scheduling.
**Files expected**
- `src/oft/domain/settings.py` — new: setting keys, supported scan periods (`4h`, `8h`), validation, source enum (`DEFAULT`/`USER`), scoped diff, deterministic error codes.
- `src/oft/application/settings_service.py` — new: read section, effective settings overlay, validate, persist, audit, idempotent replay; sole write path.
- `src/oft/adapters/sqlite_config_store.py` — new: config rows (`key`, `value_json`, `updated_at`, `updated_by`) and atomic write + audit.
- `src/oft/adapters/sqlite_schema.py` — migration (next free version, recorded by the slice): `config` table with `key` primary key and `updated_at` index.
- `src/oft/ports/store.py` — `ConfigRecord`, `get_config`, `list_config`, `set_config`.
- `src/oft/api/routes/settings.py` — new: `GET /api/v1/settings`, `GET /api/v1/settings/scan`, `PUT /api/v1/settings/scan`; router registration.
- `src/oft/api/schemas/settings.py` — new: scan settings payload, source, supported values, `next_run`, validation error details.
- `src/oft/api/app.py` — include the router; add `/settings`, `/settings/` to `_SPA_ENTRY_PATHS`.
- `src/oft/application/scheduler.py` and/or `src/oft/cli/commands/daemon.py` — resolve the persisted period through `SettingsService`; preserve pending-period semantics.
- `src/oft/cli/commands/config.py` — new: `show`, `get`, `set`, `validate`; register in `src/oft/cli/main.py`.
- `web/src/api/settings.ts` — new: typed fetch/update, CSRF/idempotency handling, envelope validation.
- `web/src/routes/settings.tsx` — settings shell/section index.
- `web/src/routes/settings-scan.tsx` — F10d scan form.
- `web/src/components/settings-field.tsx`, `settings-section-nav.tsx` — new shared field/source/save primitives.
- `web/src/routes/route-match.ts`, `web/src/App.tsx` — settings routes.
- `tests/unit/test_settings_validation.py`, `tests/integration/test_settings_api.py`, `tests/integration/test_settings_audit.py`, `tests/integration/test_scan_settings_scheduler.py`, `tests/contract/test_settings_contract.py`.
- `web/src/routes/__tests__/settings-scan.test.tsx`, `web/src/api/__tests__/settings.test.ts`.
**Tests expected**
- `GET /api/v1/settings/scan` returns the effective period, `source=default` when only the environment is present, `source=user` after a save, the supported values, and the next UTC boundary; no secret field exists in the payload.
- `PUT` with `period=8h` persists one row, writes one `CONFIG_UPDATED` audit event with before/after, and returns the effective value; a repeated identical request with the same idempotency key returns the original response with `idempotent_replay=true`; a different payload with the same key is `IDEMPOTENCY_CONFLICT`.
- Invalid/unsupported periods (`1h`, `3h`, `"eight"`, empty) return `VALIDATION_FAILED` with a field detail and write nothing; a malformed JSON body does the same.
- The daemon reads the persisted period at start and at the next boundary; a period change is pending until the next UTC boundary and never rewrites the current window.
- `oft config get scan.period`, `oft config set scan.period 8h`, `oft config show --json`, and `oft config validate` match the API values and exit codes; a non-TTY mutation without a required confirmation exits `2` only where a confirmation is required (period change needs none).
- The Web form shows the current value, source chip, supported values, next run, a save success announcement (`role="status"`), and an inline field error without losing the old value.
- No source file calls `sqlite3` directly from the API/CLI; all writes go through the settings service and store facade.
- The settings payload validates `api-envelope.schema.json`; no new error code is used unless it is in `error-catalog.json`.
**UI:** yes. **Security:** yes (config mutation, audit, CSRF/idempotency, source precedence). **Security review required:** yes.
**Acceptance criteria**
- [ ] `/settings/scan` and `oft config get|set|validate|show` read and write the same effective scan period.
- [ ] The response carries `value`, `source` (`default`/`user`), `supported`, and `next_run`; the source is never a guess.
- [ ] A save writes exactly one audit event and is idempotent; invalid values write nothing.
- [ ] The daemon applies a saved period at the next boundary without a restart; the current window is unchanged.
- [ ] The Web form is keyboard operable, labels every field, announces save/error, and shows the old value until a save succeeds.
- [ ] No secret, LLM, risk, prompt, budget, watchlist, or onboarding functionality is introduced.
**Preview screens**
- F10 settings shell, F10d `/settings/scan`.
- CLI `oft config get|set|validate` (F10d/F12).
- No chart, watchlist, or onboarding screens yet.
**Dependencies:** none (builds on Milestones 1–3). **Size:** L.

---

### 33 — `33-navigation-shell`

**Milestone:** 4
**Title:** Global navigation shell for the Web terminal
**Goal:** Add the approved preview's persistent navigation (desktop 208px grouped sidebar; compact/mobile navigation at 375px) around every existing route, with skip links, active-route indication, keyboard operation, focus-on-route-change, and a read-only note at 375px. The shell must not change any data page's behavior.
**Scope fence for this slice:** navigation and shell layout only. No new data routes beyond making the existing routes reachable. No settings content, chart content, or onboarding content. No theme switcher.
**Files expected**
- `web/src/components/nav.tsx` — new: grouped nav, `aria-current`, keyboard, 375px behavior, skip links.
- `web/src/routes/nav-items.ts` — new: one route/group source used by desktop and compact nav.
- `web/src/App.tsx` — render the nav around `main`; keep status bar, kill switch, footer.
- `web/src/routes/route-match.ts` — expose the active route group for `/settings` and `/onboarding` (routes may land in later slices; the nav item is present only when the route exists).
- `web/src/styles/app.css` or a new `web/src/styles/nav.css` — desktop/compact layout, focus styles, reduced-motion behavior.
- `web/src/components/__tests__/nav.test.tsx`; adjust `web/src/App.test.ts` only where the shell wrapper changes its output.
- `web/e2e/navigation.spec.ts` — 375/768/1440, keyboard, skip links, active state.
**Tests expected**
- Every existing route renders inside the shell; the nav marks exactly one item `aria-current="page"`; group labels are present; links are same-origin and relative.
- Skip links move focus to `main` and the nav; tab order is logical; Escape does not trap focus; route changes move focus to the page `h1` or a single live region.
- At 375px the nav is compact/read-only, explains “Use a desktop viewport or the CLI for actions”, and the kill switch remains available.
- `prefers-reduced-motion` disables non-essential transitions; no layout shift when the nav mounts.
- `axe` reports no new violations on `/system`, `/watchlist`, `/reports`, `/positions`, `/journal`, and `/settings` (when present).
- No route is removed and no existing test needs a behavioral rewrite beyond wrapping.
**UI:** yes. **Security:** no (same-origin links only; no new mutation). **Security review required:** no.
**Acceptance criteria**
- [ ] A persistent nav reaches every existing screen from every screen.
- [ ] `aria-current`, skip links, focus management, and keyboard order are verified.
- [ ] 375px is read-only with the kill switch available and an explicit explanation.
- [ ] No data page changes behavior, payload, or permissions.
**Preview screens**
- Global shell/navigation for F02–F10; S02.1/S04.1/S05.1/S07.1/S09.1/S10 shell.
- No new data screens.
**Dependencies:** 32 (the `/settings` route exists to link). **Size:** M.

---

### 34 — `34-settings-risk-caps`

**Milestone:** 4
**Title:** Risk-cap settings with tightening/loosening confirmation
**Goal:** Add `/settings/risk` and `GET/PUT /api/v1/settings/risk` over the existing risk-cap domain and storage. The screen shows effective caps, source, utilization, and last change; tightening saves directly; loosening shows a before/after diff and requires an explicit confirmation; every save is audited. The existing `oft risk show|set|validate` behavior stays intact and shares the service.
**Scope fence for this slice:** risk caps only (`per_trade_risk_pct`, `max_concurrent_positions`, `daily_loss_pct`, `max_leverage`, `max_notional_pct`, `max_slippage_bps`, `max_spread_bps`, `stale_mark_seconds`). No leverage-bracket ingestion, no daily-loss engine change, no live caps, no strategy tuning.
**Files expected**
- `src/oft/application/settings_service.py` — risk section: read effective caps/source/defaults, validate, detect loosening, persist via the existing risk store, audit `RISK_CAPS_UPDATED` with before/after.
- `src/oft/api/routes/settings.py`, `src/oft/api/schemas/settings.py` — `GET/PUT /api/v1/settings/risk`; field-level validation errors; utilization snapshot from the existing source.
- `src/oft/cli/commands/risk.py` — refactor to call the shared service; keep flags/exit codes.
- `web/src/routes/settings-risk.tsx` — F10c form, utilization bars, source chips, diff/confirm modal.
- `web/src/components/risk-cap-form.tsx`, `web/src/components/confirm-dialog.tsx` — new.
- `web/src/api/settings.ts` — risk read/write types.
- `tests/unit/test_risk_settings_validation.py`, `tests/integration/test_settings_risk_api.py`, `tests/integration/test_risk_settings_loosening.py`, `tests/contract/test_settings_contract.py` (extend).
- `web/src/routes/__tests__/settings-risk.test.tsx`, `web/e2e/settings.spec.ts` (risk case).
**Tests expected**
- Effective caps come from persisted `system_state.risk_caps` when present, else env defaults; `source` is `default` or `user`; defaults are visible.
- Tightening (e.g. per-trade 0.8→0.5, leverage 5→3, stale 60→30) saves without an extra confirmation and audits one event.
- Loosening (per-trade 0.5→0.8, leverage 3→5, stale 30→60, max positions 2→3) returns a confirmation-required result (`VALIDATION_FAILED` with `details.confirmation_required=true`, no write) unless the scoped diff carries `confirm=true`; with confirmation it writes and audits the before/after; without it nothing changes.
- Invalid values return `VALIDATION_FAILED` with field details: negative, >100%, daily loss below per-trade risk, leverage beyond the configured bound, zero positions.
- The response never includes secrets; utilization values are read-only and labelled with their source/quality.
- `oft risk show|set|validate` produce the same effective values and still require `--confirm` for loosening.
- Concurrent saves are serialized; no partial write; idempotent replay returns the original response.
**UI:** yes. **Security:** yes (risk-cap mutation, stricter-of rule, audit, confirmation). **Security review required:** yes.
**Acceptance criteria**
- [ ] `/settings/risk` shows effective caps, source, defaults, utilization, and last change.
- [ ] Tightening saves directly; loosening requires a diff confirmation and is audited.
- [ ] Invalid values are blocked inline and write nothing.
- [ ] CLI and Web use the same service and produce identical effective caps.
- [ ] No live arming, leverage-bracket fetch, or strategy tuning is introduced.
**Preview screens**
- F10c `/settings/risk`.
- CLI `oft risk show|set|validate` (F12).
**Dependencies:** 32. **Size:** M.

---

### 35 — `35-settings-llm-providers`

**Milestone:** 4
**Title:** LLM provider/model settings with write-only keys and a secret store
**Goal:** Add `/settings/llm` and the settings/secret APIs needed to configure one active provider, four role models, official base URLs, and concurrency; write/rotate/delete provider keys without ever revealing them; test a provider/role; show usage. The daemon must use the new values without a restart.
**Scope fence for this slice:** active provider, role models, official base URLs, custom-host opt-in, concurrency, keys, test, usage. No per-role provider routing, temperature, timeout, model auto-discovery, provider SDK change, or live trading. No Binance/Telegram keys.
**Files expected**
- `src/oft/application/secrets_service.py` — new: write-only secret lifecycle, keyring adapter, encrypted-file fallback (`OFT_MASTER_KEY`), masked metadata, redaction helpers, refusal when no secure store exists.
- `src/oft/adapters/keyring_secrets.py` — new optional adapter; no secret in logs.
- `src/oft/adapters/sqlite_schema.py` — migration: `secrets_meta` (provider/name, fingerprint, last4, updated_at, updated_by); no secret value.
- `src/oft/application/settings_service.py` — LLM section: effective provider/role models/base URLs/concurrency, validation (official hosts, https, no userinfo/query/fragment, custom-host opt-in), runtime refresh hook.
- `src/oft/application/llm_orchestrator.py` / `src/oft/bootstrap.py` — resolve keys/models from the effective settings overlay; refresh the runtime after a successful save.
- `src/oft/api/routes/settings.py`, `src/oft/api/schemas/settings.py` — `GET/PUT /api/v1/settings/llm`, `PUT/DELETE /api/v1/settings/llm/{provider}/key`; masked responses only.
- `src/oft/cli/commands/llm.py` — add `key set --provider <p> --stdin` and `key delete --provider <p>`; keep providers/models/test/usage; never accept a key on argv.
- `web/src/routes/settings-llm.tsx` — provider rows, masked key, role mapping, base URL, concurrency, test panel, usage.
- `web/src/components/secret-field.tsx` — write-only password field, mask, replace/delete actions, no reveal.
- `web/src/api/settings.ts` — LLM types.
- `tests/unit/test_secret_masking.py`, `tests/unit/test_llm_settings_validation.py`, `tests/integration/test_secrets_write_only.py`, `tests/integration/test_llm_settings_runtime.py`, `tests/contract/test_settings_contract.py` (extend).
- `web/src/routes/__tests__/settings-llm.test.tsx`, `web/e2e/settings.spec.ts` (key masking case).
- Dependency change: add the reviewed keyring/encryption dependency; document why and the fallback.
**Tests expected**
- `GET /api/v1/settings/llm` returns provider rows, masked last-4, fingerprint metadata, role models, base URLs, concurrency, source, and test status; no key value, prefix, or full secret appears anywhere in the response.
- `PUT .../{provider}/key` stores a key, returns only `masked` and `updated_at`, and writes an audit event; the same key is not echoed in the response, logs, error messages, SQLite, or the Web DOM.
- `DELETE .../{provider}/key` removes the key and marks the provider unconfigured; a decision that needs that provider returns `LLM_PROVIDER_NOT_CONFIGURED`/`LLM_MODEL_NOT_CONFIGURED` rather than falling back to another key.
- Base URL validation rejects http non-loopback, userinfo, query/fragment, and unofficial hosts unless `llm.allow_custom_base_url` is explicitly true; the error names the rule.
- Role model selection is applied at runtime: after saving a new model for `ANALYSIS`, the next analysis call uses it; a missing model returns the stable configuration error (no default model shipped).
- The capability probe (`POST /api/v1/llm/test`) works with a saved key and returns the recorded strict-schema/degraded status; provider error text is redacted.
- If no secure store is available, the write is refused with an explicit error and no plaintext file is created.
- `oft llm key set --provider anthropic --stdin` reads the key from stdin without echo; `oft llm providers` shows only the mask.
**UI:** yes. **Security:** yes (CRITICAL: secrets, provider base URL exfiltration, runtime refresh, redaction). **Security review required:** yes.
**Acceptance criteria**
- [ ] Provider, role models, official base URLs, and concurrency are editable and persisted.
- [ ] Keys are write-only: add, rotate, delete, mask; no reveal endpoint or control exists.
- [ ] The daemon uses the new model/key without a restart and never logs the key.
- [ ] Invalid base URLs and missing models fail with stable errors; no silent fallback.
- [ ] The test panel and usage read model work for the configured provider.
- [ ] No temperature/timeout/per-role-provider fields are invented.
**Preview screens**
- F10a `/settings/llm`; S01.2 provider step (read-only/reused in slice 44).
- CLI `oft llm providers|models|test|usage` plus key add/delete.
**Dependencies:** 32. **Size:** L.

---

### 36 — `36-settings-prompt-editor`

**Milestone:** 4
**Title:** Prompt template list, editor, history, diff, restore, and activation
**Goal:** Deliver F10b over the existing versioned prompt API: list templates by role, open an editor, see required variables and the last rendered prompt (redacted), create a new immutable version, diff versions, restore the built-in default (as a new version), activate a version, and inspect history. The editor must never edit a version in place, never execute template text, and never expose secrets.
**Scope fence for this slice:** prompt templates only. No knowledge editor, model/provider selection, prompt execution from the UI, rule changes, or template auto-tuning. No change to the prompt content/seed logic except additive read fields if needed.
**Files expected**
- `web/src/routes/settings-prompts.tsx` — F10b list.
- `web/src/routes/settings-prompt-detail.tsx` — F10b.1 editor.
- `web/src/routes/settings-prompt-history.tsx` — F10b.2 history/diff.
- `web/src/api/prompts.ts` — new typed client for list/show/history/diff/versions/activate/restore; idempotency keys.
- `web/src/components/prompt-editor.tsx`, `prompt-variable-panel.tsx`, `prompt-diff.tsx` — new.
- `src/oft/api/routes/settings.py`, `src/oft/api/schemas/settings.py` — `GET /api/v1/settings/prompts` read aggregate (list only; writes stay on `/api/v1/prompts`).
- `src/oft/application/prompt_service.py` — only additive read fields if the UI needs usage counts; no write-path change.
- `tests/unit/test_prompt_editor_helpers.py`, `tests/integration/test_prompt_settings_aggregate.py`.
- `web/src/routes/__tests__/settings-prompts.test.tsx`, `web/e2e/settings.spec.ts` (prompt case).
**Tests expected**
- The list shows the four roles, active version, author/time, and a clear “built-in default v1” state when no custom version exists.
- The editor shows the required variables (`{{symbol}}`, `{{metrics}}`, `{{schema}}`, `{{timeframe}}`, `{{knowledge}}` as applicable), highlights missing variables, and blocks save with inline errors and no write.
- Saving creates a new version (never edits in place), marks it DRAFT/active per the API contract, and announces the new version; a decision already made with an older version remains linked to that version.
- Diff and history render version bodies as text (no `dangerouslySetInnerHTML`, no link execution); restore creates a new version and shows what it replaced; activation is explicit and audited.
- The last-rendered-prompt preview is redacted and does not contain any configured secret; a hostile template body renders as literal text.
- 375px is read-only; editor actions are disabled with a desktop/CLI instruction.
- Keyboard: list → editor → variables → save; dialogs trap/restore focus; errors focus; axe passes.
**UI:** yes. **Security:** yes (untrusted template text, XSS, version activation, secret redaction). **Security review required:** yes.
**Acceptance criteria**
- [ ] `/settings/prompts`, `/settings/prompts/:promptId`, and `/settings/prompts/:promptId/history` match F10b.
- [ ] Every save creates a new immutable version; no in-place edit path exists.
- [ ] Restore default and activate require explicit actions and are audited; history stays complete.
- [ ] Missing required variables and invalid templates are blocked inline.
- [ ] Prompt text renders as text, never HTML; no secret is shown.
- [ ] The existing `/api/v1/prompts` behavior and fixtures are unchanged.
**Preview screens**
- F10b `/settings/prompts`, F10b.1 editor, F10b.2 history.
- CLI `oft prompt list|show|edit|diff|history|restore|activate` already delivered (F12).
**Dependencies:** 32. **Size:** L.

---

### 37 — `37-settings-budget-thresholds`

**Milestone:** 4
**Title:** Budget threshold settings screen
**Goal:** Deliver F10e over the existing budget API: daily/weekly/monthly thresholds, warn percentage, current spend, projected month-end, unpriced calls, last alert, and the “alert only, never stops calls” contract. Saves are validated and audited; there is no hard cap.
**Scope fence for this slice:** budget thresholds and usage display only. No Telegram alerts (Milestone 5), no hard stop, no price-table editor, no provider/model pricing changes.
**Files expected**
- `web/src/routes/settings-budget.tsx` — F10e form and usage bars.
- `web/src/api/settings.ts` / `web/src/api/budget.ts` — new typed client or extend the existing one.
- `src/oft/api/routes/settings.py` — optional `GET /api/v1/settings/budget` aggregate over the existing service.
- `src/oft/api/routes/budget.py` — optional idempotency replay for `PUT` (no schema break).
- `web/src/routes/__tests__/settings-budget.test.tsx`, `web/e2e/settings.spec.ts` (budget case).
**Tests expected**
- The screen shows the three thresholds, warn %, current spend/state, projected month-end, and `unpriced_count`; missing usage is `N/A`, never zero.
- Invalid/threshold ordering values are rejected inline with field details; valid values save and audit; the copy states alerts do not stop calls.
- The usage refresh does not clear the last good values; it marks them `STALE` while loading.
- The existing `oft budget show|set|usage` behavior is unchanged.
- 375px is read-only; keyboard and axe pass.
**UI:** yes. **Security:** no (no secrets; config-only). **Security review required:** no.
**Acceptance criteria**
- [ ] `/settings/budget` reads and writes the existing thresholds and shows usage/unpriced/projection.
- [ ] No hard cap or Telegram delivery is introduced.
- [ ] Invalid values are blocked; valid saves are audited.
- [ ] CLI and Web show the same numbers.
**Preview screens**
- F10e `/settings/budget`; CLI `oft budget show|set|usage`.
**Dependencies:** 32. **Size:** S.

---

### 38 — `38-watchlist-star-config`

**Milestone:** 4
**Title:** Watchlist star/unstar and search-add configuration
**Goal:** Make the scan universe configurable from the Web UI: star/unstar rows in `/watchlist`, add from `/watchlist/search`, show the next-scan inclusion count, and use optimistic updates with a pending indicator and rollback on failure. Add `oft watchlist add|remove` aliases over the existing star state. The registry remains upstream/read-only; the change is only the star flag.
**Scope fence for this slice:** star flag and add/remove aliases. No exchangeInfo refresh implementation, no symbol metadata editing, no TradFi agreement flow, no live-trading enablement, no scan triggering from the watchlist.
**Files expected**
- `src/oft/api/routes/watchlist.py` — add `POST /api/v1/watchlist/{symbol}` and `DELETE /api/v1/watchlist/{symbol}` (delegating to the existing star write); extend `GET /api/v1/watchlist` additively with per-row `starred`/`starred_at` and next-scan count.
- `src/oft/api/routes/symbols.py` — keep `PATCH /api/v1/symbols/{symbol}/star` behavior compatible.
- `src/oft/application/symbol_registry.py` — star service + audit event (`WATCHLIST_STAR_TOGGLED`) if not already audited.
- `src/oft/cli/commands/watchlist.py` — add `add`/`remove` aliases; keep `star|unstar|list|search|groups`.
- `web/src/api/watchlist.ts` — new typed client.
- `web/src/routes/watchlist.tsx` — star toggles, pending/rollback, count, read-only at 375.
- `web/src/routes/watchlist-search.tsx` — add from search (or extend the existing search route).
- `web/src/components/star-toggle.tsx` — accessible toggle with label/pending state.
- `tests/integration/test_watchlist_star.py`, `tests/unit/test_watchlist_star_state.py`.
- `web/src/routes/__tests__/watchlist.test.tsx`, `web/e2e/watchlist.spec.ts`.
**Tests expected**
- `POST`/`DELETE` are idempotent: starring twice yields one star and `starred_at` is stable/updated per the documented rule; unstarring twice is a no-op.
- Unknown symbols return `NOT_FOUND`; non-TRADING/fixture-limited symbols show a reason and cannot be added where the preview says they cannot.
- `GET /api/v1/watchlist` returns per-row star state and the next-scan inclusion count; the count is consistent with `list_starred()`.
- The Web toggle updates optimistically, announces the change, and rolls back with an error on failure; a pending row is not clickable twice.
- 375px shows the star state but disables mutation with a desktop/CLI instruction; the kill switch remains available.
- `oft watchlist add|remove` are aliases for star/unstar and return the same envelope; no exchange call is made.
- Audit contains the star change with the symbol and before/after.
**UI:** yes. **Security:** yes (mutation, CSRF/idempotency, audit). **Security review required:** yes.
**Acceptance criteria**
- [ ] Stars can be toggled from the Web UI and from `oft watchlist star|unstar|add|remove`.
- [ ] The next-scan inclusion count updates and is consistent with the registry.
- [ ] Optimistic updates roll back visibly on failure; 375px is read-only.
- [ ] No exchange call, metadata edit, or scan trigger is introduced.
**Preview screens**
- S02.1 `/watchlist`, S02.3 `/watchlist/search`, S02.4 `oft watchlist`.
**Dependencies:** 33 (navigation entry). **Size:** M.

---

### 39 — `39-chart-panel-route`

**Milestone:** 4
**Title:** Standalone chart panel with 15m/1h/4h switch
**Goal:** Deliver S04.3 as a full route: `/reports/:reportId/chart` renders the existing chart anatomy at full width, with a 15m/1h/4h switch backed by published bars/metrics, an honest unavailable state for missing periods, the data-table alternative, quality/provenance stamps, and links from report detail and the symbol inspector.
**Scope fence for this slice:** route, chart read API, timeframe switch, layout, data table, quality states. No new overlays (slice 40), no drawing tools, no session shading, no multi-chart grids, no live streaming.
**Files expected**
- `src/oft/api/routes/reports.py` or a new `src/oft/api/routes/charts.py` — `GET /api/v1/reports/{report_id}/chart?period=15m|1h|4h`; returns symbol, native period, requested period, `available_periods`, bars, metrics (when available), gaps, data quality, and an explicit reason for unavailable periods.
- `src/oft/application/chart_service.py` — new: resolve the report's metric version/frozen snapshot for the native period; resolve published bar/metric versions for other periods; never upsample/interpolate; return `MISSING` with a reason.
- `src/oft/adapters/parquet_store.py` — additive read helper if the service needs the latest published run per period; no storage format change.
- `src/oft/api/schemas/chart.py` — new payload schema/contract fixture.
- `web/src/api/charts.ts` — new typed client.
- `web/src/routes/chart-panel.tsx` — new route and layout.
- `web/src/components/chart.tsx` — accept the chart payload/timeframe; keep the existing data-table fallback and entry marker.
- `web/src/routes/report-detail.tsx` — link to the chart route.
- `web/src/routes/symbol-inspector.tsx` — “open latest report chart” link when a report exists.
- `web/src/routes/route-match.ts`, `web/src/App.tsx` — `/reports/:reportId/chart`.
- `tests/integration/test_chart_api.py`, `tests/contract/test_chart_contract.py`.
- `web/src/routes/__tests__/chart-panel.test.tsx`, `web/e2e/chart.spec.ts`.
**Tests expected**
- The native report period returns the frozen snapshot/metrics and matches the embedded report chart values exactly.
- A requested period with published bars/metrics returns those values; a period with no published data returns `MISSING`, a reason, and `available_periods`; the UI disables that control and never draws an invented series.
- The route validates the report ID and period; unknown report → `NOT_FOUND`, invalid period → `VALIDATION_FAILED`.
- The data table is present, the chart has an accessible name/summary, keyboard bar selection works, and the selected bar's footprint matches the table.
- The page renders at 375/768/1440; 375 is read-only; the chart pane is not clipped; reduced motion disables transitions.
- The report detail and symbol inspector links reach the route; no auth/SPA regression.
**UI:** yes. **Security:** no (read-only; all labels rendered as text). **Security review required:** no.
**Acceptance criteria**
- [ ] `/reports/:reportId/chart` renders the full chart with the native report period.
- [ ] 15m/1h/4h switch works only when data exists and is disabled with a reason otherwise.
- [ ] The data-table alternative and keyboard selection work.
- [ ] No fabricated/upsampled series, no drawing tools, no session shading.
**Preview screens**
- S04.3 `/reports/:reportId/chart`; S04.2 link; S02.2 “open chart” link.
**Dependencies:** 33. **Size:** L.

---

### 40 — `40-chart-overlays-crosshair`

**Milestone:** 4
**Title:** Chart overlays and keyboard crosshair/tooltip
**Goal:** Complete the base chart anatomy: draw opportunity zone bands and trigger lines, entry/exit/stop/target markers, gap breaks, and a keyboard/pointer crosshair with a linked tooltip (O/H/L/C, volume, delta, CVD, data-quality/provenance). Overlay data comes from the server payload, never inferred in the client.
**Scope fence for this slice:** overlays and crosshair on the existing report/position charts. No session shading, no drawing tools, no indicator scripting, no multi-chart layouts, no live streaming, no new market-data indicators.
**Files expected**
- `src/oft/api/routes/charts.py` / `src/oft/api/schemas/chart.py` — add `zones` (opportunity zone/trigger/state/label) and `markers` (entry/exit/stop/target) to the chart payload; derive them from the report's linked opportunities and position/trade records, not client inference.
- `src/oft/api/routes/opportunities.py` — optional `report_id` filter (additive) if the chart service reuses the ledger read model.
- `web/src/components/chart.tsx` / `chart-overlays.tsx` — zone bands, trigger lines, markers, gap breaks.
- `web/src/components/chart-crosshair.tsx` — keyboard/pointer crosshair, tooltip, `aria-live` summary.
- `web/src/routes/position-detail.tsx` — pass stop/target/exit markers where the payload provides them.
- `web/src/styles/app.css` or `chart.css` — overlay styles; non-color encoding for direction/state.
- `tests/unit/test_chart_overlays.py`, `tests/contract/test_chart_contract.py` (extend).
- `web/src/components/__tests__/chart-overlays.test.tsx`, `web/e2e/chart.spec.ts` (extend).
**Tests expected**
- A triggered LONG opportunity draws a zone band and a trigger line at the exact price; an expired/invalidated opportunity uses its state label and a non-color channel; labels are text, escaped, and truncated safely.
- Entry/exit/stop/target markers are inside the price domain and positioned at their prices; a missing marker is omitted, not placed at zero.
- Gap windows are drawn as breaks, never interpolated; the tooltip marks `STALE`/`MISSING`/`ESTIMATED` from server fields.
- Left/Right/Home/End move the crosshair; the tooltip updates; the data table and `aria-live` summary match the focused bar; Escape clears the crosshair.
- Pointer hover works without breaking keyboard focus; no color-only state; reduced motion disables animations.
- The chart remains memoized enough that a 5s position revalidation does not rebuild the SVG tree (existing behavior preserved).
**UI:** yes. **Security:** no (read-only; untrusted labels rendered as text). **Security review required:** no.
**Acceptance criteria**
- [ ] Opportunity zones/trigger lines and position markers match the server payload.
- [ ] Keyboard and pointer crosshair/tooltip work with the same values as the data table.
- [ ] Gaps are breaks; missing data is stamped, not drawn.
- [ ] No session shading or advanced drawing is introduced.
**Preview screens**
- S04.3 chart anatomy; S07.2 position chart markers.
**Dependencies:** 39. **Size:** M.

---

### 41 — `41-opportunities-ledger-parity`

**Milestone:** 4
**Title:** Opportunity ledger preview parity (source, age, export)
**Goal:** Close the remaining presentation deltas between the delivered ledger and preview S05.1/S05.2: show the source scan and age in the list, link the source scan/report, and expose the existing read-only export from the detail. No state actions are added.
**Scope fence for this slice:** list columns, detail source/export link, small empty/partial copy. No new ledger state, no manual trigger/accept/reject/cancel, no strategy edits.
**Files expected**
- `web/src/routes/opportunities.tsx` — add source scan and age columns; keep counts/filters/empty states.
- `web/src/routes/opportunity-detail.tsx` — add an export link to `/api/v1/opportunities/{id}/export`; show source scan/report links (already partly present).
- `web/src/api/opportunities.ts` — expose the fields already returned by `opportunity_summary` (`created_at`, `updated_at`, `source`).
- `web/src/routes/__tests__/opportunities.test.tsx` — extend.
- `web/e2e/opportunities.spec.ts` — new or extend the existing flow.
**Tests expected**
- The list shows source scan ID and age computed from `created_at` with a fixed test clock; missing source is `N/A`, never blank or invented.
- The detail export link returns the contract attachment; the ledger still has no state-changing control.
- Counts, filters, per-state empty panels, 375 read-only, keyboard tabs, and axe remain correct.
- No API contract change beyond additive fields already present.
**UI:** yes. **Security:** no (read-only). **Security review required:** no.
**Acceptance criteria**
- [ ] Source scan and age are visible in the list; source links work.
- [ ] The detail exposes export and the existing source/timeline/conditions.
- [ ] No manual state transition or new write path exists.
**Preview screens**
- S05.1 `/opportunities`, S05.2 `/opportunities/:oppId`.
**Dependencies:** 33. **Size:** S.

---

### 42 — `42-report-export-cli`

**Milestone:** 4
**Title:** Report export API and `oft report export`
**Goal:** Add the ADR-0003 report export endpoint and the F12/F04 CLI command. Export preserves CALC/LLM provenance, `N/A`/`STALE` stamps, and the report's frozen content; it never includes secrets or raw prompt bodies. The path is validated and an existing file is not silently overwritten.
**Scope fence for this slice:** report export only (md/json; stdout or a user-specified path). No PDF/HTML rendering, no journal export change, no bulk export, no cloud upload.
**Files expected**
- `src/oft/api/routes/reports.py` — `GET /api/v1/reports/{report_id}/export?format=md|json`; attachment response; path validation is CLI-side for file output.
- `src/oft/application/report_export.py` — new: deterministic Markdown/JSON rendering from `report_payload`, provenance counts, redaction, no HTML.
- `src/oft/cli/commands/report.py` — `export` command with `--format md|json`, `--output`, `--stdout`; exit codes; overwrite refusal.
- `tests/unit/test_report_export.py`, `tests/integration/test_report_export_api.py`, `tests/contract/test_report_export_contract.py`, `tests/e2e/test_report_export_cli.py`.
**Tests expected**
- Markdown export contains the CALC metrics, the `LLM ›` narrative, provenance/prompt version/cost, and the opportunity IDs; it preserves `N/A`/`STALE`/`PARTIAL` stamps and never injects unescaped HTML.
- JSON export uses the shared envelope/contract and includes the same sections; a hostile narrative string remains data and cannot break the JSON.
- A missing report is `NOT_FOUND`/exit `1`; an invalid format is `VALIDATION_FAILED`/exit `2`; an unwritable path or path traversal is refused with a reference and no partial file.
- `--stdout` writes only the export to stdout; stage/error lines go to stderr.
- No secret, API key, or raw prompt body appears in either format.
**UI:** no. **Security:** yes (export path, untrusted narrative, redaction). **Security review required:** yes.
**Acceptance criteria**
- [ ] `GET /api/v1/reports/{id}/export?format=md|json` works.
- [ ] `oft report export <id> --format md|json` writes to stdout or a safe path.
- [ ] Provenance and data-quality stamps are preserved; secrets/prompts are not.
- [ ] Invalid paths/formats/IDs fail with stable codes and exit codes.
**Preview screens**
- S04.4/S04.5 `oft report show|export` (export part).
**Dependencies:** none beyond Milestones 1–3. **Size:** M.

---

### 43 — `43-cli-system-diagnostics`

**Milestone:** 4
**Title:** CLI diagnostics: `oft system info|logs` and `oft daemon restart`
**Goal:** Complete the F12 diagnostic surface without the Web UI: `oft system info` prints version/environment/paths/uptime/job status; `oft system logs` reads structured logs with `--follow`, `--since`, `--level`; `oft daemon restart` stops and starts with the documented open-positions warning. Redaction is preserved.
**Scope fence for this slice:** read-only diagnostics and daemon restart. No log upload, no telemetry, no systemd installer, no backup/restore (existing `oft doctor`/`daemon logs` remain).
**Files expected**
- `src/oft/cli/commands/system.py` — new: `info`, `logs`.
- `src/oft/cli/commands/daemon.py` — `restart` command reusing stop/start; open-positions warning; no auto-close.
- `src/oft/cli/main.py` — register `system`.
- `tests/integration/test_cli_system.py`, `tests/e2e/test_cli_system.py`.
**Tests expected**
- `oft system info --json` returns version, environment, data dir, config source, scheduler state, and job summaries without requiring the daemon; secrets are absent.
- `oft system logs --since/--level` filters deterministically; redaction removes key-like strings, cookies, and prompt bodies; `--follow` is bounded/clean on SIGINT.
- `oft daemon restart` prints the open-positions warning, refuses without the documented confirmation where required, and never closes positions; exit codes match F12.
- Non-TTY behavior is honest: no prompt hang, required confirmations exit `2`.
**UI:** no. **Security:** yes (log redaction/secret exposure, daemon control). **Security review required:** yes.
**Acceptance criteria**
- [ ] `oft system info|logs` work offline/with the daemon and redact secrets.
- [ ] `oft daemon restart` preserves positions and documents the consequence.
- [ ] Exit codes and JSON envelopes match the CLI contract.
**Preview screens**
- F12 `oft daemon *`, `oft system logs|info`; S03/F01 diagnostics references.
**Dependencies:** none beyond Milestones 1–3. **Size:** S.

---

### 44 — `44-onboarding-web`

**Milestone:** 4
**Title:** Paper-only onboarding wizard at `/onboarding/:step`
**Goal:** Deliver F01's Web flow for the capabilities that exist in Milestone 4: environment/local bind, LLM providers, watchlist + scan period, budget, and review. The step sequence is resumable, shows completed steps, validates each step, never echoes secrets, and marks Binance/Telegram/Live steps as deferred to Milestones 5–6 rather than pretending to configure them.
**Scope fence for this slice:** Web onboarding only. No Binance key form, no Telegram token, no Live arming, no exchange permission read-back, no paper reset. No duplicate settings implementation: steps call the settings/ watchlist services built in slices 32–38.
**Files expected**
- `web/src/routes/onboarding.tsx`, `onboarding-step.tsx` — step shell, progress, resume, review.
- `web/src/api/onboarding.ts` — step state/progress client; uses settings/watchlist APIs.
- `src/oft/api/routes/settings.py` — `GET/PUT /api/v1/settings/onboarding` for completed-step state (persisted in `config`); no secrets in progress.
- `src/oft/application/settings_service.py` — onboarding progress read/write; no duplicate config.
- `web/src/routes/route-match.ts`, `web/src/App.tsx`, `src/oft/api/app.py` — `/onboarding` SPA entry.
- `web/src/routes/__tests__/onboarding.test.tsx`, `web/e2e/onboarding.spec.ts`.
**Tests expected**
- A fresh install shows step 1 with paper selected; no config is required to render; writability/port/clock checks are presented as states, not fake success.
- Completing a step persists progress; a reload or `?resume` returns to the first incomplete step; completed steps are listed; a bad step value is rejected.
- LLM step uses write-only key fields and the saved mask; no key is echoed or stored in client state after save; a missing provider blocks “finish” with the documented consequence.
- Watchlist/schedule and budget steps call the same services and show source/validation; Binance/Telegram/Live steps show “Milestone 5/6” and do not claim to save.
- 375px is read-only with a desktop/CLI instruction; keyboard focus moves to the step heading; errors focus; axe passes.
- Finish shows a review with the effective settings and a next action (`/scans` or `oft scan run`), no Live enablement.
**UI:** yes. **Security:** yes (key entry, progress state, CSRF/idempotency, no secret echo). **Security review required:** yes.
**Acceptance criteria**
- [ ] `/onboarding/1..5` works and is resumable.
- [ ] Settings steps reuse the real services and persist effective values.
- [ ] Binance/Telegram/Live are explicitly deferred, not faked.
- [ ] No secret is echoed or persisted in progress.
- [ ] 375px is read-only; keyboard and axe pass.
**Preview screens**
- S01.1 environment, S01.2 LLM providers, S01.5 watchlist/schedule/budget/review; S01.3/S01.4 deferred labels.
**Dependencies:** 32, 35, 37, 38. **Size:** L.

---

### 45 — `45-cli-init`

**Milestone:** 4
**Title:** `oft init` paper-only resumable onboarding
**Goal:** Deliver F01's CLI equivalent over the same onboarding/settings services: an interactive wizard with resume and non-interactive flags, paper-only, no secret on argv, and honest deferred Binance/Telegram/Live steps. `--non-interactive` requires every unset step flag and exits `2` otherwise.
**Scope fence for this slice:** CLI init only. No duplicate step logic (reuse the Web/service step definitions), no Binance/Telegram token prompts, no Live, no paper reset.
**Files expected**
- `src/oft/cli/commands/init.py` — new: step prompts, `--resume`, `--non-interactive`, per-step flags, review; register in `main.py`.
- `src/oft/application/settings_service.py` — shared onboarding step definitions/progress if not fully extracted in slice 44.
- `tests/e2e/test_cli_init.py`, `tests/integration/test_cli_init_noninteractive.py`.
**Tests expected**
- A fresh run walks the steps, persists progress, and resumes at the first incomplete step; existing values are shown with source.
- `--non-interactive` with all required flags succeeds without prompts; a missing flag exits `2` and names it; no partial “finished” state.
- No secret is accepted on argv; key entry uses stdin with no echo; the resulting `oft llm providers` shows only the mask.
- Deferred steps print “Milestone 5/6” and do not block a paper-only finish.
- Non-TTY interactive without flags exits `2`; SIGINT leaves resumable progress.
**UI:** no. **Security:** yes (key entry, non-interactive validation, no secret echo). **Security review required:** yes.
**Acceptance criteria**
- [ ] `oft init` and `oft init --resume` complete the paper-only sequence.
- [ ] `--non-interactive` is deterministic and exits `2` on missing flags.
- [ ] Deferred steps are explicit; no Live/Binance/Telegram config is faked.
- [ ] No secret is printed, stored in progress, or accepted on argv.
**Preview screens**
- S01.6 `oft init`.
**Dependencies:** 44. **Size:** M.

---

### 46 — `46-m4-a11y-e2e-hardening`

**Milestone:** 4
**Title:** Milestone 4 accessibility, E2E, security-regression, and determinism hardening
**Goal:** Prove the milestone as a system: Playwright journeys for settings, watchlist, chart, opportunities, and onboarding at 375/768/1440; axe on every new screen; keyboard/focus/pause/reduced-motion checks; secret-leak assertions; deterministic replay/no-wall-clock checks; contract fixtures and docs. This slice adds tests and small fixes only; it does not add features.
**Scope fence for this slice:** tests, fixtures, documentation, and fixes strictly required to make the milestone's acceptance criteria pass. No new settings, no new routes, no live/Telegram.
**Files expected**
- `web/e2e/settings.spec.ts`, `watchlist.spec.ts`, `chart.spec.ts`, `opportunities.spec.ts`, `onboarding.spec.ts`, `navigation.spec.ts` (some may be created in earlier slices; this slice completes the matrix).
- `web/e2e/run-e2e.sh`, `web/playwright.config.ts` — add projects/fixtures for the new suites.
- `tests/e2e/test_cli_config.py`, `test_cli_init.py`, `test_report_export.py`, `test_cli_system.py` (complete/extend).
- `tests/contract/test_settings_contract.py`, `test_chart_contract.py`, `test_report_export_contract.py` — fixture validation.
- `tests/test_no_wall_clock.py` — cover new deterministic modules.
- `.claude/ecc-workflow/screens/...` results artifact (report path; not a state/guard edit).
- Documentation updates required by the slice (README/CLAUDE.md only if the implementation changes commands; no new docs are written by this plan).
**Tests expected**
- Playwright: every new route loads, saves/errors/key-masks, and never exposes a key; 375/768/1440 screenshots; keyboard-only paths; `prefers-reduced-motion`; pause controls on live tables/charts; axe passes (no new violations).
- CLI: `oft config`, `oft init`, `oft report export`, `oft system`, `oft daemon restart`, `oft watchlist add|remove`, `oft llm key` exit codes and JSON envelopes.
- Contract: every new payload validates `api-envelope`/chart/settings/export schemas; prompt/opportunity/trade-log/position fixtures still validate.
- Replay: settings/watchlist changes do not alter replay outputs; no wall-clock calls in new deterministic modules.
- Security regression: grep/log/DOM assertions prove no key, raw prompt, or provider header appears in responses, DOM, logs, or exports.
- Coverage: ≥80% overall; 100% branch on settings validation, risk loosening, and secret masking.
**UI:** no (test-only). **Security:** no (but includes security-regression assertions). **Security review required:** no.
**Acceptance criteria**
- [ ] All new routes have Playwright coverage at 375/768/1440 and axe.
- [ ] CLI commands have deterministic E2E/unit coverage and stable exit codes.
- [ ] No secret appears in DOM, logs, exports, or API responses.
- [ ] Replay/no-wall-clock/contract/coverage gates pass.
**Preview screens**
- A12 accessibility acceptance; all F02/F04/F05/F10/F12 screens touched by the milestone.
**Dependencies:** 32–45. **Size:** M.

---

## 5. Cross-slice engineering requirements

### 5.1 Testing matrix

- **Unit:** settings definitions/validation/source precedence; risk loosening; secret masking/fingerprints; prompt variable/diff helpers; watchlist star state; chart scale/overlay math; export formatting.
- **Integration:** settings persistence + audit + idempotency; daemon period pickup; LLM runtime refresh; key write/rotate/delete with no reveal; prompt version create/activate/restore; watchlist star; chart API period availability; report export path safety; onboarding progress/resume.
- **Contract:** `api-envelope`, settings, chart, export; existing prompt/opportunity/trade-log/position fixtures remain valid; every new error code exists in the catalog and envelope enum.
- **Replay/determinism:** no wall clock in new domain/application modules; settings/watchlist changes do not alter historical replay; fixed clocks in tests.
- **E2E/Playwright:** settings save/error/key masking; watchlist optimistic/rollback; chart timeframe/data table; opportunities parity; onboarding resume; 375/768/1440; keyboard; axe; no console errors.
- **Coverage:** ≥80% overall; 100% branch on settings validation, risk-cap loosening, secret masking, and chart availability logic.

### 5.2 Accessibility requirements (all UI slices)

- 375/768/1440 are required breakpoints; 1280/1024 must not overflow. The 375px surface is read-only except the kill switch; every disabled action explains the desktop/CLI path.
- Every form field has a visible label, programmatic name, source/hint text, and inline error association (`aria-describedby`, `aria-invalid`).
- Save/error/status messages use `role="status"`/`role="alert"` and focus the error summary or first invalid field.
- Dialogs (loosening confirmation, restore, delete key) use a focus trap, `aria-modal`, initial focus, and focus restoration.
- Tables keep row/column semantics, captions, sort/selection ARIA where used, and a focusable scroll region.
- Charts keep the data-table alternative, a text summary, keyboard crosshair/bar selection, and non-color direction/state encoding.
- Live-updating tables/charts have a pause control; `prefers-reduced-motion` disables non-essential motion.
- Skip links, `aria-current`, logical tab order, and route-change focus are required.
- Run axe via Playwright for every new screen; manual keyboard walkthrough recorded in the slice findings.

### 5.3 Security triggers and controls

- **Settings/config mutation:** session + CSRF + Origin/Host checks; `Idempotency-Key`; one atomic write + audit; no direct SQL from API/CLI.
- **Secrets:** write-only; keyring/encrypted fallback; no value in SQLite/API/log/export/prompt/DOM; mask last-4 only; no reveal; refuse plaintext.
- **Prompt/knowledge editors:** prompt text is untrusted data; render in textarea/pre, never `dangerouslySetInnerHTML`; no link execution; redact the rendered preview.
- **Export:** validate output paths (no traversal), refuse silent overwrite, neutralize spreadsheet formula cells where applicable, preserve provenance, never include secrets/prompts.
- **Untrusted text:** LLM narrative, prompt bodies, symbol names, opportunity labels, and provider error text render as text; CSP stays `default-src 'self'` with no `unsafe-inline`/`unsafe-eval`.
- **Watchlist mutation:** same CSRF/idempotency/audit controls; star only, no exchange call.
- **CLI:** no secret on argv; stdin for keys; redacted logs; stable exit codes; no `--yes` for typed-token confirmations.
- **Audit:** `CONFIG_UPDATED`, `RISK_CAPS_UPDATED`, `PROMPT_VERSION_*`, `SECRET_ADDED/ROTATED/DELETED`, `WATCHLIST_STAR_TOGGLED`, `ONBOARDING_STEP_COMPLETED`, `REPORT_EXPORTED`; hash chain verified by `oft doctor`.

### 5.4 Contract and observability

- New payloads get a JSON Schema or contract fixture before implementation; Pydantic/TS types are not handwritten duplicates of a different shape.
- Every response uses the shared envelope; `provenance` is `CALC` for settings/watchlist/chart/export; `data_quality` is `COMPLETE`/`PARTIAL`/`MISSING` honestly.
- `GET /api/v1/settings` returns section summaries with `source`, `last_changed`, and redacted key status (no values).
- `/api/v1/status` and `oft doctor` add config source/last change and secret-store availability; no telemetry.
- Logs are structured JSON with redaction; provider error text is sanitized before storage/output.

---

## 6. Delivery order and PR rules

1. 32 (first end-to-end path), then 33 (shell), then 34/35/36/37 (settings screens), then 38 (watchlist), 39/40 (charts), 41 (opportunities), 42/43 (CLI), 44/45 (onboarding), 46 (hardening).
2. Every slice is one issue and one PR. Do not split a slice across PRs; do not renumber earlier slices.
3. No slice may add live execution, Binance credentials, Telegram delivery, paper reset, or a second write path for config/prompts/risk.
4. Keep existing tests green. Additive API fields only; do not rename/remove existing fields. Existing CLI commands keep their flags/exit codes; new aliases are additive.
5. Security review is required before merge for the slices marked in §3. The review is not a gate approval.
6. If a slice grows beyond its fence, reduce scope or defer the extra work; do not widen the PR.

---

## 7. Explicitly out of scope / deferred

| Item | Deferred to | Reason |
|---|---|---|
| Binance keys, permission read-back, TradFi agreement | Milestone 6 | Live capability not in M4 |
| Live arming/gate, typed `LIVE`, live management | Milestone 6 | M4 is paper-only; no exchange credentials |
| Telegram bot, settings (F10f), receipts, budget alerts | Milestone 5 | PRD Milestone 5 |
| `/telegram`, `/settings/telegram`, `/settings/live` | Milestones 5–6 | Approved preview screens depend on later capabilities |
| Paper reset (`oft paper reset`) | Milestone 6 or follow-up | M3 assumption moved it out |
| 1h/2h/12h/24h scan periods | Follow-up | Scheduler currently supports 4h/8h |
| Temperature/timeout settings | Follow-up | Not in `Settings` or provider adapters |
| Analysis lookback 3–7 day mutation | Follow-up | No pipeline wiring |
| Knowledge Web editor | Follow-up | No approved preview/flows screen |
| Session shading / `NO_TRADING` hatch | Milestone 7 or data follow-up | Snapshot writes `UNKNOWN` |
| Drawing tools, saved layouts, multi-chart grids, bar replay UI | Milestone 7 | DESIGN §13.3 |
| Live DOM/L2 heatmap | Milestone 7 | Deferred |
| Global `--format ndjson|csv` refactor | Follow-up | Not required for the M4 user-visible paths |
| SSE `/api/v1/events` | Milestone 5/6 or follow-up | Not required for settings/chart snapshots |

---

## 8. Traceability

| PRD / flow | Slices |
|---|---|
| MVP 3 watchlist + scan period | 32, 38, 44, 45 |
| MVP 9 paper trading | Existing M2/M3; no reset in M4 |
| MVP 10 dual entry (full CLI + Web) | 32–46; CLI gaps in 42, 43, 45 |
| MVP 11 LLM provider abstraction | 35, 44, 45 |
| MVP 12 prompt templates + versioning | 36 |
| MVP 13 trade log/review (Web) | Existing M3; opportunity parity in 41 |
| MVP 15 LLM budget | 37 |
| F01 onboarding | 44, 45 |
| F02 watchlist | 38 |
| F04 report/chart | 39, 40, 42 |
| F05 opportunities | 41 |
| F10a/F10b/F10c/F10d/F10e | 35/36/34/32/37 |
| F12 CLI | 32, 42, 43, 45 |
| ADR-0004 chart MVP | 39, 40 |
| ADR-0010 security | 32, 34, 35, 36, 38, 42, 43, 44, 45 |

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
npm --prefix web run test:e2e   # Playwright suites with temporary daemons
```

Coverage: `uv run pytest --cov=oft --cov-report=term-missing`; ≥80% overall; 100% branch on settings validation, risk loosening, secret masking, and chart availability logic.

No live Binance credential, Telegram token, or real provider key is required in CI. LLM tests use `stub`/recorded responses. Playwright uses temporary data dirs and fixture reports/positions/trades.

---

## 10. Open questions (non-blocking, recorded for G3)

These are assumptions fenced in §2.2; they do not block implementation because the plan explicitly limits or defers them. They are repeated in the handoff.

1. Scan periods: approve 4h/8h for M4, or schedule a scheduler expansion for 1h/2h/12h/24h (preview)?
2. F10a temperature/timeout: are they required in M4, or can the adapters continue to use their internal constants?
3. F10d analysis lookback: should M4 display a read-only effective window, or wire a new analysis setting?
4. Knowledge editor: M2 mentioned it for M4, but the approved preview has no knowledge screen. Confirm knowledge stays CLI-only.
5. Secrets backend: is adding the reviewed keyring/encryption dependency acceptable, with an encrypted-file fallback requiring `OFT_MASTER_KEY`?
6. Onboarding: is paper-only F01 with Binance/Telegram/Live deferred acceptable, or should `/onboarding` wait for Milestone 5/6?
7. Chart timeframe source: confirm that 15m/1h must come from published bars, and unavailable periods should be disabled rather than resampled.
8. Session shading: confirm deferral to Milestone 7 because the snapshot's `underlying_session` is `UNKNOWN`.
9. CLI format flags: confirm the milestone’s “full-featured CLI” means command/capability parity, not a global `--format` refactor.
10. Opportunities parity: confirm source/age/export are the intended remaining deltas, with no new ledger state actions.

---

## 11. G3 decisions (auto mode)

Recorded when G3 was approved in autonomous mode; each takes the fenced recommendation above.

1. Scan periods stay 4h/8h for M4; a scheduler expansion is a later milestone.
2. Temperature/timeout stay at the adapters' internal constants (not exposed in M4).
3. The analysis lookback is shown read-only as the effective window; no new setting.
4. Knowledge editing stays CLI-only (`oft knowledge`); no Web knowledge screen.
5. The reviewed keyring/encryption dependency is approved, with the encrypted-file fallback requiring `OFT_MASTER_KEY`; plaintext key storage is refused.
6. Onboarding is paper-only F01; Binance/Telegram/Live steps are labelled deferred.
7. Chart timeframes use published bars only; unavailable periods are disabled, never resampled for display.
8. Session shading is deferred to Milestone 7 while the snapshot records `underlying_session=UNKNOWN`.
9. "Full-featured CLI" means command/capability parity; no global `--format` refactor in M4.
10. Opportunity parity is source/age/export only; the ledger stays read-only and code-owned.

---

*End of plan. No gate is approved by this document; it defines ordered, thin vertical slices for Milestone 4.*
