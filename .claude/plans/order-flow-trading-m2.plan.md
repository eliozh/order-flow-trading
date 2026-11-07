# Implementation Plan — Milestone 2: 分析闭环（paper）

**Project:** Order-Flow-Trading
**Milestone:** 2 — 分析闭环（paper）
**Status:** plan complete for review; **no gate is approved by this document** (Milestone 2 G3 remains pending)
**Date:** 2026-11-08
**Inputs:** `.claude/prds/order-flow-trading.prd.md`; `docs/adr/0001`–`0012`; `docs/contracts/`; `.claude/ecc-workflow/handoffs/05-architecture.md`; `.claude/ecc-workflow/handoffs/02-research.md`; `design/flows.md`; `design/DESIGN.md`; `.claude/plans/order-flow-trading.plan.md`; the Milestone 1 source tree.
**Stack:** Milestone 1 stack unchanged — Python 3.12 modular monolith daemon (`oftd`), FastAPI, APScheduler, DuckDB/Parquet, SQLite WAL, Pydantic v2, Typer CLI, React 19 + TypeScript + Vite, Lightweight Charts.
**Boundary for this milestone:** exactly the PRD row — 星标扫描 → LLM 报告 → 机会台账 → 代码触发 → LLM 复核 → 模拟入场，**paper only**. No live orders, no Binance credentials, no Telegram, no position-management depth (Milestone 3), no full dual-entry UI completion (Milestone 4). Public market data only.

---

## 1. Milestone outcome

A single local daemon that closes the analysis loop on the existing Milestone 1 data/metrics foundation:

1. **星标扫描**：按 4h/8h UTC 边界（或手动）扫描全部星标标的；每个标的有独立 stage，单个标的失败不终止批次；LLM 失败降级为 CALC-only 报告。
2. **LLM 报告**：通过 provider-neutral `LLMPort` 生成 schema-validated 的市场解读与结构化 opportunity 提案；prompt 与 knowledge artifact 版本化；每次调用记账。
3. **机会台账**：opportunity 以结构化条件、失效条件、过期时间、confidence、provenance 持久化；只有代码能改变状态。
4. **代码触发**：确定性 evaluator 在 15 s job 中评估 OPEN opportunities；只使用新鲜 snapshot；同一机会/快照只产生一次 `CODE` trigger。
5. **LLM 复核**：trigger 后调用 `ENTRY_RECHECK`，输出结构化 `ENTER_LONG | ENTER_SHORT | NO_ENTRY`；无效/拒绝输出不得进入执行层。
6. **模拟入场**：确定性 risk gate（取 LLM 提议与用户上限的更严者）→ 人工 paper accept → 单一 execution gateway → deterministic paper broker → order/fill/OPEN position；无 live broker。

**Non-goals for Milestone 2:** Binance API key / testnet / live broker、live arming、Telegram bot 与推送、position management（HOLD/部分平/全平/加仓）、完整 PnL/胜率/journal/review、paper reset 的完整语义、F10 全量设置 Web UI、375px 全量编辑、图表 drawing tools/L2 DOM、新闻/社媒文本。

---

## 2. Binding decisions and contract constraints

| Decision | Plan implication |
|---|---|
| ADR-0005: provider-neutral LLM port | `ports.llm.LLMPort` with `capabilities()`, `complete()`, `estimate_cost()`; OpenAI / Anthropic / DeepSeek adapters; provider SDK objects never enter `oft.domain`; no tools, no execution imports. |
| ADR-0005: one canonical output schema | All providers return `docs/contracts/llm-output.schema.json`; code validates JSON parse → JSON Schema 2020-12 → Pydantic → semantic checks; exactly one repair attempt; otherwise persist `REJECTED`, alert locally, never create an opportunity/order. |
| ADR-0005: cost accounting | Every call writes an `llm_calls` row and a `budget-record` row with provider/model/prompt version/input hash/usage/cost/latency; price table versioned; unknown model → `unpriced=true`, never silently zero; day/week/month rollups; thresholds alert only, no hard stop. |
| ADR-0006: one execution gateway | Paper and (future) live share one `ExecutionGateway`; only code can submit; requires persisted `risk_verdict_id` + human approval for the paper accept path; deterministic `client_order_id`; idempotency key; no blind retry. |
| ADR-0006: deterministic risk gate | Code recomputes caps; `effective_size = min(LLM proposal, per-trade, notional, leverage, exchange max)`; stricter-of; stop required; stale mark 60 s blocks; BLOCK/REDUCE recorded; no LLM numeric trust. |
| ADR-0006: kill switch | `system_state.kill_switch` + `$OFT_DATA/run/KILL_SWITCH` (0600), checked before every submit/retry; blocks new paper orders and LLM-driven execution; does not auto-close positions; activation never prompts. |
| ADR-0006: paper broker | Starts at 10,000 USDT; market fill at last trade/mark + configurable slippage (default 2 bps) and taker fee (5 bps); maker fee 2 bps for non-crossed limits; no queue-position modeling; partial fills off by default; deterministic with fixed seed/clock. |
| ADR-0007: scheduler + durable ledger | Scans remain UTC 4h/8h with `UNIQUE(job_type, window_start)`; add `trigger_evaluate` every 15 s, single instance, no catch-up; restart reconstructs from SQLite; manual and scheduled scans share the same claim. |
| ADR-0009: prompt and knowledge versioning | Prompt templates are immutable versions; required variables validated at save/activation; every decision records `prompt_id`, `prompt_version`, `prompt_hash`, `knowledge_artifact_hash`; full knowledge corpus is never injected; `books/`, `skills/`, `playbook/` stay private. |
| ADR-0010: local security | Loopback bind, bearer token for CLI, session + CSRF + Origin/Host checks for browser mutations; provider keys write-only/masked; no secret in config.toml, SQLite, Parquet, logs, prompts, reports, or API responses; market data and knowledge are **data, not instructions**. |
| ADR-0010: audit trail | Append-only `audit_events` hash chain for startup, config changes, LLM decisions, triggers, risk verdicts, orders, fills, positions, kill switch, paper accept/reject; `oft doctor` verifies the chain. |
| ADR-0011: deterministic testing | Injected `ClockPort`; committed small fixtures; `LLMStub` + recorded provider responses; same domain code for live/replay; 80% coverage floor; **100% branch coverage on `domain.risk`, `domain.trigger`, and the OMS state machine**; replay writes `replay_*` only. |
| ADR-0012: observability | `structlog` JSON with redaction; `/health` vs `/ready`; `oft doctor`; no telemetry; budget/provider health visible without Telegram. |
| Contracts | Every REST/CLI response uses `api-envelope.schema.json`; decisions validate `llm-decision`; opportunities validate `opportunity`; triggers validate `trigger-event`; orders/fills/positions/trade-refs validate their schemas; budget rows validate `budget-record`; error codes come from `error-catalog.json`. |

### 2.1 Existing Milestone 1 foundation and compatibility

Milestone 2 extends, not rewrites:

- `application/scan_service.py` currently implements `DATA → METRICS → REPORT` and explicitly rejects non-metrics-only scans. Milestone 2 adds the `LLM` stage and a `DEGRADED` outcome, preserving the CALC-only path.
- `application/jobs.py` / `application/scheduler.py` already scan starred symbols on 4h/8h windows with durable claims, leases, catch-up, and per-symbol checkpoints. Milestone 2 reuses that machinery and adds the `trigger_evaluate` job.
- `api/routes/symbols.py` already exposes idempotent star/unstar and the `symbols.starred` column exists. Milestone 2 consumes stars; it does not re-implement star storage.
- `reports` currently store `llm_decision_id=null` and a metrics-only snapshot. The report payload gains additive `llm`, `decision`, `opportunity_ids`, and cost fields; existing CALC-only reports remain readable.
- `scan_symbols.stage` must be extended from `QUEUED/DATA/METRICS/REPORT/DONE/FAILED` to include `LLM` (and a `DEGRADED` terminal outcome), via a forward-only SQLite migration.
- `oft scan run` keeps `--metrics-only` as an explicit CALC-only escape hatch. When a provider is configured, the default becomes LLM-enabled; when no provider is configured, the scan returns a CALC-only report with an explicit `LLM_PROVIDER_NOT_CONFIGURED` reason rather than failing the scan.
- No new Binance credential, Telegram token, or live broker is introduced. Public market data and existing fixtures are sufficient for every test.

### 2.2 Scope assumptions (narrow reading)

These are recorded because the PRD milestone row is a summary, not a field-level specification. If G3 disagrees, adjust the plan before implementation. Milestone 2 includes only the minimum supporting capabilities needed by the six-step row: provider abstraction/validation/budget (ADR-0005), prompt/knowledge versioning (ADR-0009; the Milestone 1 plan boundary explicitly deferred prompt/knowledge work to this milestone), and the risk gate/paper OMS (ADR-0006). It does not add features outside the row.

1. **Human paper accept is retained.** The design F06 decision card requires Accept/Reject; Milestone 2 keeps the human action for paper entries. Automatic paper execution without confirmation is not introduced.
2. **Position management is Milestone 3.** Milestone 2 ends at an `OPEN` paper position with entry, size, stop/target, fees, and balance. No HOLD/PARTIAL_CLOSE/FULL_CLOSE/ADD, funding accrual, PnL/win-rate, journal, or review.
3. **Paper reset is Milestone 3.** The initial paper account is created once with the documented 10,000 USDT default; reset semantics (including the 4-week Live gate) are deferred.
4. **Telegram is Milestone 5.** Budget/provider/trigger alerts are recorded in the local ledger, API, CLI, and Web surfaces; no Telegram delivery is attempted. The `telegram_deliveries` table may exist but remains unused.
5. **Full settings UI is Milestone 4.** Milestone 2 ships CLI/API configuration for providers, prompts, knowledge, risk caps, budget, and kill switch. F10a/F10b/F10c/F10e Web screens are completed in Milestone 4.
6. **Model IDs are configuration data, not contracts.** No default provider/model is shipped; the operator configures provider + model per role. Tests use `stub` or recorded responses. Research model IDs/prices are re-verified at implementation time.
7. **Budget defaults come from ADR-0005:** daily $10 / weekly $50 / monthly $150, warn at 80%, alert at 100%, **no hard stop**. They are configurable; the plan does not invent different defaults.
8. **Knowledge injection defaults to Mode A** (compiled playbook + role rules). Mode B deterministic topic retrieval is implemented only if the playbook/topic index is present; otherwise it is an explicit follow-up. Never inject the full corpus.
9. **Replay is event-time deterministic, not provider-deterministic.** Replay uses recorded/stub LLM outputs by `(role, input_hash)`; `--live-llm` is explicit. A missing recorded output fails loudly.

---

## 3. Slice overview

Order is dependency-driven. Slice 11 is a genuine end-to-end path through all six loop steps using a deterministic stub provider, before any real-provider refinement. No slice may introduce a live broker, Binance credential, Telegram path, or management action.

| # | Slice | Depends on | UI | Security-sensitive | Sec review before merge | Size |
|---|---|---|---|---|---|---|
| 11 | `11-paper-loop-walking-skeleton` | — | no | yes | **yes** | L |
| 12 | `12-starred-batch-scan` | 11 | yes | no | no | M |
| 13 | `13-prompt-versioning` | 11 | no | yes | **yes** | M |
| 14 | `14-knowledge-playbook` | 13 | no | yes | **yes** | L |
| 15 | `15-llm-providers-and-budget` | 11, 13, 14 | no | yes | **yes** | L |
| 16 | `16-opportunity-conditions-ledger` | 11, 12 | no | no | no | L |
| 17 | `17-trigger-scheduler` | 16 | no | no | no | M |
| 18 | `18-entry-recheck-risk-paper-oms` | 15, 17 | no | yes | **yes** | L |
| 19 | `19-report-opportunity-ui` | 12, 16 | yes | yes | **yes** | L |
| 20 | `20-trigger-paper-ui` | 18, 19 | yes | yes | **yes** | L |
| 21 | `21-replay-hardening` | 11–20 | no | no | no | M |
| 22 | `22-storage-refactor` | 11–21 | no | no | no | L |

Slice 22 is a follow-up hardening slice added after the analysis loop is complete; it changes storage structure only and adds no product behavior.

**Security-review rule:** slices 11, 13, 14, 15, 18, 19, and 20 must not merge without a security review. Scope: the LLM boundary (untrusted market/knowledge text, prompt injection, provider keys, output rejection), the paper order path (risk gate, gateway, kill switch, idempotency), and the Web mutation/XSS/CSP surface. This is a review requirement only; it is not a gate approval.

**Size rule:** every slice is one PR. L slices have an explicit scope fence in the detailed section; if a slice grows beyond that fence, split it along the stated seam rather than widening the PR.

---

## 4. Detailed slices

### 11 — `11-paper-loop-walking-skeleton`

**Title:** Paper loop walking skeleton: stub LLM through report, trigger, and paper entry

**Goal:** Prove the whole six-step loop on one symbol with a deterministic stub provider and one condition type, and establish the ports, persistence, validation, risk gate, gateway, paper broker, and CLI/API surfaces that later slices complete. This is the "walking skeleton" — real providers, knowledge, full conditions, scheduler-driven triggers, and Web UI arrive later.

**Scope fence for this slice:** one symbol; manual scan; `stub` provider only; one trigger condition (`PRICE_TOUCH_ZONE`) and one entry type (`MARKET`); PAPER only; CLI/API only; no Web UI; no knowledge artifact; no budget rollups (per-call ledger only); no `trigger_evaluate` scheduler (a manual `oft trigger evaluate` is enough); no PnL/management/funding.

**Files expected**
- `src/oft/ports/llm.py` — `LLMPort`, `LLMRequest`, `LLMResult`, `ProviderCapabilities`, role enum.
- `src/oft/adapters/llm_stub.py` — deterministic fixture-backed provider, records `provider="stub"`, `model="fixture"`.
- `src/oft/domain/llm_output.py` — Pydantic model + semantic checks (confidence, stop side, zone order, decision-type/payload match, oversized rationale).
- `src/oft/application/llm_validation.py` — canonical JSON Schema 2020-12 load/validate → Pydantic → semantic-check pipeline, repair/reject outcome.
- `src/oft/domain/opportunities.py` — opportunity record + `OPEN → TRIGGERED/EXPIRED/INVALIDATED` state machine.
- `src/oft/domain/trigger.py` — `PRICE_TOUCH_ZONE` evaluator, snapshot hash, no-stale rule.
- `src/oft/domain/risk.py` — minimal risk gate (stop required, per-trade, notional, leverage, max positions, exchange filters, stricter-of).
- `src/oft/domain/oms.py` — order/fill/position state machine; deterministic `client_order_id`.
- `src/oft/application/analysis_service.py` — analysis role, built-in prompt v1, decision persistence, opportunity draft validation.
- `src/oft/application/opportunity_service.py` — create/list/show/expire.
- `src/oft/application/trigger_service.py` — manual evaluation, trigger event, idempotency.
- `src/oft/application/entry_service.py` — entry re-check, risk verdict, accept/reject.
- `src/oft/application/execution_gateway.py` — sole submit path; kill-switch check; risk-verdict freshness; idempotency; audit.
- `src/oft/application/paper_broker.py` — deterministic market fill, slippage/fee, order/fill/position write.
- `src/oft/application/budget_ledger.py` — one `budget-record` row per LLM call; `unpriced=true` for stub.
- `src/oft/application/kill_switch.py` — DB + file sentinel state (minimal in this slice).
- `src/oft/application/scan_service.py` — allow LLM-enabled single-symbol scan; write CALC + LLM report; keep `metrics_only` path.
- `src/oft/adapters/sqlite_store.py` — migration v10: `llm_calls`, `decisions`, `opportunities`, `opportunity_events`, `trigger_events`, `risk_verdicts`, `orders`, `fills`, `positions`, `paper_accounts`, `budget_ledger`, `audit_events`, `prompt_templates`, `prompt_versions`.
- `src/oft/ports/store.py` — record dataclasses + methods.
- `src/oft/api/routes/{reports,opportunities,triggers,paper}.py`; `src/oft/api/schemas/{llm,opportunity,trigger,paper}.py`.
- `src/oft/cli/commands/{opp,trigger,paper,kill}.py`; `src/oft/cli/main.py`; `src/oft/cli/api_client.py`.
- `src/oft/cli/commands/report.py` — print CALC first, then `LLM ›` narrative, decision, confidence, and opportunity IDs, or the explicit rejection/absent state.
- `tests/fixtures/llm/{analysis_valid,opportunity_valid,entry_recheck_enter_long,entry_recheck_no_entry,invalid_schema,refusal,truncated}.json`.
- `tests/unit/test_llm_validation.py`, `test_opportunity_lifecycle.py`, `test_trigger_evaluator.py`, `test_risk_gate.py`, `test_oms_state_machine.py`, `test_paper_broker.py`, `test_kill_switch.py`.
- `tests/integration/test_paper_loop.py`, `test_paper_accept_idempotency.py`.
- `tests/contract/test_llm_decision_contract.py`, `test_opportunity_trigger_contract.py`, `test_order_fill_position_contracts.py`.
- `tests/replay/test_paper_loop_replay.py` (initial deterministic check; full harness in slice 21).

**Tests expected**
- LLM validation: valid fixture → `VALID`; malformed/refusal/truncated → `REJECTED`, no opportunity, no order; one repair creates a second `llm_calls` row (`attempt=2`); provider-supplied provenance is ignored/overwritten by code.
- Opportunity: LLM draft → code-assigned condition IDs; invalid params reject the draft; state history is append-only; expiry/invalidation reasons recorded.
- Trigger: `PRICE_TOUCH_ZONE` met/not met; stale snapshot → no trigger; `trigger_source=CODE`; snapshot hash/condition results persisted; idempotency key unique.
- Risk gate: stop required; per-trade/notional/leverage/max-positions/min-notional/step/tick; `effective_size` takes the stricter side; BLOCK/REDUCE recorded with binding cap.
- Paper broker: deterministic market fill at mark/last + 2 bps slippage, 5 bps taker fee; order `FILLED`, fill row, position `OPEN`, balance debited; same fixed clock/seed → same IDs/values.
- Gateway: rejects an order without `risk_verdict_id` or human acceptance; kill switch blocks submit; duplicate idempotency key returns the original response; no live path exists.
- E2E: one-symbol stub scan → report with CALC + LLM + opportunity → `oft trigger evaluate` → `oft trigger accept` → order/fill/position visible through API and CLI with identical values.
- Contract: decisions validate `llm-decision`; opportunities validate `opportunity`; triggers validate `trigger-event`; orders/fills/positions validate their schemas; budget rows validate `budget-record`.
- Security negative: an output containing instructions/markdown fences is rejected; no secret is written to prompts/DB/logs/API; import-linter forbids `adapters.llm_* → execution/Binance`; audit events are written.

**UI:** false. **Security:** true. **Security review required:** yes.

**Acceptance criteria**
- [ ] `uv run pytest -q` passes offline; no new default test uses the network.
- [ ] `oft scan run --symbol BTCUSDT --period 4h` with the stub provider produces a report containing CALC metrics, an `LLM` band, and at least one opportunity; `--metrics-only` still produces the Milestone 1 CALC-only report.
- [ ] `oft opp list --state OPEN` and `oft opp show <id>` show the opportunity, conditions, confidence, source report/decision, and state timeline.
- [ ] `oft trigger evaluate <opportunity_id>` creates a trigger only when the fixture price touches the zone; the trigger records `trigger_source=CODE`, snapshot hash, condition results, and an idempotency key.
- [ ] `oft trigger accept <trigger_id>` creates exactly one paper order, one fill, and one `OPEN` position; a repeat accept with the same idempotency key creates nothing new and returns the original result.
- [ ] A stopless or oversized entry is `BLOCKED`/`REDUCE`, the risk verdict is persisted, and no order is created.
- [ ] `oft kill on` blocks new paper orders and retries; `oft kill off` requires the typed `RESUME` confirmation (non-interactive token form is accepted only with the explicit token).
- [ ] `oft report show <id>` prints CALC before `LLM ›`; a rejected narrative renders the explicit rejection state, never fabricated prose.
- [ ] No provider key, Binance credential, live broker, or Telegram path is imported or required.
- [ ] Determinism: the same fixture + fixed clock + stub produces identical decision/order/fill/position values and hashes.

**Preview screens**
- S04.4 `oft report show`（CALC first，`LLM ›` narrative，opportunity IDs）.
- S05.3 `oft opp list|show|timeline|export`.
- S06.3 `oft trigger list|show`.
- S06.4 `oft trigger accept|reject|recheck`.
- S08.2 `oft paper balance|orders|fills|equity`.
- No Web UI in this slice.

**Dependencies:** none (builds on Milestone 1). **Size:** L.

---

### 12 — `12-starred-batch-scan`

**Title:** Starred-symbol batch scan with LLM stage and per-symbol reports

**Goal:** Turn the single-symbol skeleton into the PRD's 星标扫描: scan all starred symbols on the 4h/8h schedule and on manual trigger, with per-symbol stages `QUEUED → DATA → METRICS → LLM → REPORT → DONE`, partial failures, cost totals, and report links. Use the built-in prompt v1 and whatever provider is configured (stub in tests; real adapters arrive in slice 15).

**Scope fence for this slice:** batch orchestration, stage/ledger changes, scan UI extension, live/backfill source selection, and window policy. No new provider, prompt-editing, condition, or execution behavior.

**Files expected**
- `src/oft/application/jobs.py` — scheduled runner uses the LLM-enabled scan; per-symbol `LLM`/`DEGRADED`; total cost; no batch abort on one symbol.
- `src/oft/application/scan_service.py` — stage transitions `DATA → METRICS → LLM → REPORT`; `DEGRADED` when narrative fails; window policy (main window = scan period; 3–7 day lookback for volume profile/structure) recorded in the report.
- `src/oft/application/freshness.py` — live-source freshness check used by the scan before LLM.
- `src/oft/adapters/sqlite_store.py` — migration v11: add `LLM` to the `scan_symbols.stage` CHECK; add `DEGRADED`/cost fields where needed.
- `src/oft/api/routes/scans.py`, `src/oft/api/schemas/report.py` — scan detail includes per-symbol stages, LLM state, cost, report IDs.
- `src/oft/cli/commands/scan.py` — default LLM scan; `--metrics-only` escape hatch; `scan show` stage list; exit 3 on partial.
- `src/oft/cli/commands/daemon.py` (only if scheduler wiring changes).
- `web/src/routes/scans.tsx`, `scan-detail.tsx`, `web/src/api/scans.ts` — LLM stage, DEGRADED/FAILED chips, report links.
- `docs/contracts/report.schema.json` (new, additive) if the report payload is promoted to a dedicated contract; otherwise extend the existing embedded-snapshot contract notes.
- `tests/integration/test_starred_batch_scan.py`, `test_scan_partial_failure.py`, `test_scan_live_source_freshness.py`.
- `tests/e2e/test_scan_llm_ui.py`.
- `web/src/routes/__tests__/scans.test.tsx`.

**Tests expected**
- Two starred symbols + stub provider → one scan with two symbol rows and two reports; scan status `COMPLETE`.
- One symbol has a data gap → that symbol `FAILED` with the exact gap window; the other completes; scan status `PARTIAL`, CLI exit 3.
- One symbol's LLM output is rejected → that symbol `DEGRADED`, a CALC-only report still exists, and the batch continues.
- Manual scan while a scan is running → 409 `SCAN_ALREADY_RUNNING`; no second run.
- A reclaimed run resumes only unfinished symbols; completed reports are not re-run.
- `source=live` uses the freshness budget and is labelled `LIVE`; a stale live input degrades or skips the LLM with an explicit reason, never silently.
- The scan record carries trigger source, period, per-symbol stages, total tokens/cost, and report links; no fake percentage.
- No Telegram delivery is attempted; the report/API marks delivery as not configured.
- `/scans` and `/scans/:id` show the LLM stage and report links; 375px remains a read-only summary.

**UI:** true. **Security:** false (no new external input surface; market data validation and API auth already exist). **Security review required:** no.

**Acceptance criteria**
- [ ] With two starred symbols, `oft scan run` creates one scan, two per-symbol rows, and two reports (or a documented DEGRADED/FAILED reason per symbol).
- [ ] A data gap on one symbol does not abort the batch; the scan completes `PARTIAL` and the exact gap is visible.
- [ ] A rejected/refused LLM output produces a `DEGRADED` symbol with a CALC-only report and no opportunity.
- [ ] The scheduled 4h/8h runner uses the same claim/overlap rules as the manual path; restart resumes unfinished symbols from SQLite.
- [ ] `source=live` is available only when fresh data exists; stale data cannot drive an LLM report or a trigger.
- [ ] `/scans` and `/scans/:id` show LLM stage, quality, cost, and report links; no Telegram field is required.
- [ ] The report payload validates against its contract (existing embedded snapshot/metrics, plus the new LLM/decision fields).

**Preview screens**
- S03.1 `/scans`（schedule state, history, manual trigger）.
- S03.2 `/scans/:scanId`（per-symbol stages including `LLM`, DEGRADED/FAILED, report links）.
- S03.3 `oft scan run`; S03.4 `oft scan status|history|show`.

**Dependencies:** 11. **Size:** M.

---

### 13 — `13-prompt-versioning`

**Title:** Prompt templates, immutable versions, activation, and restore-default

**Goal:** Implement the ADR-0009 prompt lifecycle for `ANALYSIS` and `ENTRY_RECHECK` roles: built-in defaults, required-variable validation, immutable versions, hashes, explicit activation, non-destructive restore-default, and decision provenance. CLI first; the F10b Web editor belongs to Milestone 4.

**Scope fence for this slice:** prompt storage/versioning/CLI and prompt rendering; no knowledge compiler yet; no provider adapters; no Web editor.

**Files expected**
- `src/oft/domain/prompts.py` — role/template model, required variables, version immutability, hash.
- `src/oft/application/prompt_service.py` — list/show/edit/create-version/activate/restore-default; required-variable validation; system-prompt separate versioning.
- `src/oft/adapters/sqlite_store.py` — seed default templates as v1; `prompt_templates`, `prompt_versions` (if not fully created in slice 11).
- `src/oft/api/routes/prompts.py` — read/activate; mutations use idempotency key.
- `src/oft/cli/commands/prompt.py`; `src/oft/cli/main.py`.
- `src/oft/application/analysis_service.py`, `entry_service.py` — render the active version, hash the rendered prompt, persist `prompt_id/version/hash`.
- `tests/unit/test_prompt_versions.py`, `test_prompt_required_variables.py`.
- `tests/integration/test_prompt_activation.py`.
- `tests/contract/test_prompt_provenance.py`.

**Tests expected**
- Default `ANALYSIS`/`ENTRY_RECHECK` templates exist as v1 and are used when no custom version is active.
- Editing creates a new immutable version; the old version is still readable; restore-default creates a new version instead of deleting history.
- Missing `{{symbol}}`, `{{timeframe}}`, `{{metrics}}`, `{{schema}}`, `{{knowledge}}`, `{{trigger}}`, or `{{position}}` where required blocks activation with `PROMPT_VALIDATION_FAILED` and the variable name.
- Activation is explicit; a draft is not used by a scan.
- Every decision records `prompt_id`, `prompt_version`, `prompt_hash`; changing the body changes the hash.
- A historical decision whose version is missing is shown as `missing` and disables exact replay; it never silently falls back to the current version.
- The rendered prompt redacts secret-like strings; a template cannot interpolate a provider key or the API token.
- The system prompt (role boundary, untrusted-data warning) is versioned separately and recorded.

**UI:** false. **Security:** true (editable prompt text feeds the LLM boundary; required-variable and secret-redaction rules). **Security review required:** yes.

**Acceptance criteria**
- [ ] `oft prompt list|show` shows roles, active version, and history.
- [ ] `oft prompt edit` creates a new version; `oft prompt activate --version N` switches the active version; `oft prompt restore` creates a new version that restores the default body.
- [ ] A missing required variable is rejected at save/activation and named in the error.
- [ ] A new scan uses the active version and records its version/hash on the decision.
- [ ] A replay of an old decision with a missing version reports `missing`, not a substituted prompt.
- [ ] No secret appears in a rendered prompt or its persisted hash input.

**Preview screens**
- None in the Web UI; the CLI surface is F12 (`oft prompt list|show|edit|diff|history|restore|activate`). F10b `/settings/prompts` is Milestone 4.

**Dependencies:** 11. **Size:** M.

---

### 14 — `14-knowledge-playbook`

**Title:** Compiled order-flow playbook and deterministic knowledge injection

**Goal:** Implement ADR-0009's private knowledge pipeline: ingest `book-to-skill` outputs, compile a 6–10K-token order-flow playbook with stable section IDs, `topics.json`/`rules.json`, and a manifest; require human review; inject Mode A deterministically within budget; record `knowledge_artifact_hash` on every decision. Never inject the full corpus and never commit book-derived content.

**Scope fence for this slice:** knowledge build/compile/validate/review and Mode A injection. Mode B topic retrieval is implemented only if the topic index and deterministic mapping are present; otherwise it is a documented follow-up.

**Files expected**
- `src/oft/domain/knowledge.py` — artifact model, section IDs, token budget, topic mapping, manifest validation.
- `src/oft/application/knowledge_service.py` — ingest, normalize, select taxonomy, compile, budget, review/approve, activate.
- `src/oft/application/prompt_builder.py` — delimit `<knowledge>...</knowledge>` as untrusted data; enforce the knowledge budget; record selected section IDs.
- `src/oft/cli/commands/knowledge.py`; `src/oft/cli/main.py`.
- `.gitignore` — ensure `$OFT_DATA/knowledge/`, `books/`, `skills/`, and `playbook/` remain private (verify the existing rules; no book text in the repo).
- `tests/fixtures/knowledge/synthetic_playbook.md`, `topics.json`, `rules.json`, `manifest.json` (synthetic, not copyrighted).
- `tests/unit/test_knowledge_compiler.py`, `test_knowledge_budget.py`, `test_knowledge_injection.py`.
- `tests/integration/test_knowledge_build.py`.
- `tests/contract/test_decision_knowledge_provenance.py`.

**Tests expected**
- Compiling the synthetic fixture produces a playbook ≤10K tokens, a manifest with `artifact_id`/`compiler_version`/`source_skills[]`/`created_at`/`license_note`/`reviewed_by`. The token budget is measured with a documented method (a configured tokenizer or a recorded chars/4 approximation) and the method is stored in the manifest.
- A corpus that exceeds the budget is selected/summarized, never concatenated; the compiler records what was dropped.
- Mode A injects the playbook + role `rules.json` within budget; section IDs are cited; Mode B (if implemented) maps CALC context deterministically to 2–5 topics and records the selected sections.
- Knowledge text is delimited as data; imperative text inside it cannot change the schema, add tools, or produce an order.
- Every decision records `knowledge_artifact_hash`; a missing artifact disables exact replay but does not fabricate knowledge.
- No book text or derived skill is committed; the open-source tree contains only the compiler workflow, a converter adapter, an empty `books/` placeholder, and documentation.
- Export/share warns that the knowledge artifact is private.

**UI:** false. **Security:** true (untrusted knowledge text, prompt injection, copyright/private artifacts). **Security review required:** yes.

**Acceptance criteria**
- [ ] `oft knowledge build --source <synthetic-fixture>` produces `playbook.md`, `topics.json`, `rules.json`, and `manifest.json` with a deterministic artifact hash.
- [ ] The playbook is ≤10K tokens and contains stable `K-<topic>-<n>` section IDs.
- [ ] Mode A injection is within the configured budget and records the selected sections/hash on the decision.
- [ ] A missing playbook or a missing artifact hash is explicit; the scan still produces a report and marks the knowledge state as absent.
- [ ] A prompt-injection corpus in the knowledge text cannot alter the output schema or create a trigger/order.
- [ ] No copyrighted book text or generated skill is committed; `.gitignore` and the runbook document the private-artifact rule.

**Preview screens**
- None; knowledge state appears as provenance in S04.2 (slice 19) and in CLI report output.

**Dependencies:** 13. **Size:** L.

---

### 15 — `15-llm-providers-and-budget`

**Title:** Real LLM providers, strict schema translation, repair/reject, and budget ledger

**Goal:** Replace the stub with real OpenAI, Anthropic, and DeepSeek adapters behind `LLMPort`; implement provider-specific schema translation, capability probing, one repair, reject-and-record behavior, timeouts/semaphores/circuit breaker, usage/cost accounting, versioned pricing, and day/week/month budget rollups with threshold alerts (local only; Telegram in Milestone 5). No default model is shipped.

**Scope fence for this slice:** provider adapters, capability probe, pricing/budget, and CLI/API surfaces. No new prompt/knowledge behavior (slices 13–14), no condition/trigger changes, no OMS changes.

**Files expected**
- `src/oft/adapters/llm_common.py` — shared schema translation/redaction.
- `src/oft/adapters/llm_openai.py`, `llm_anthropic.py`, `llm_deepseek.py`.
- `src/oft/domain/pricing.py` — versioned price table, `Decimal` cost math, unknown-model handling.
- `src/oft/application/llm_orchestrator.py` — role dispatch, one repair, validation outcome, rejection alert sink, request ids.
- `src/oft/application/budget_service.py` — per-call ledger, day/week/month rollups, threshold state.
- `src/oft/application/concurrency.py` or `rate_limiter.py` extension — per-provider semaphores (default 2; DeepSeek 1 if beta is flaky), timeouts (120 s analysis, 45 s re-check), circuit breaker after repeated 5xx/429.
- `src/oft/config.py` — provider/model/role configuration; keys already exist; no default model.
- `src/oft/adapters/sqlite_store.py` — `provider_capabilities`, budget rollups (if not derived), `price_versions`.
- `src/oft/api/routes/llm.py`, `budget.py`; `src/oft/cli/commands/llm.py`, `budget.py`; `src/oft/cli/main.py`.
- `docs/contracts/` — no new schema required; budget rows use `budget-record.schema.json`.
- `tests/fixtures/llm/providers/{openai,anthropic,deepseek}/*.json` — valid, refusal, truncated, empty-content, malformed, schema-unsupported cases (redacted, recorded shape).
- `tests/unit/test_provider_schema_translation.py`, `test_cost_math.py`, `test_repair_once.py`, `test_circuit_breaker.py`, `test_provider_capabilities.py`.
- `tests/contract/test_provider_fixtures.py`.
- `tests/integration/test_budget_ledger.py`, `test_provider_fallback.py`, `test_llm_provider_not_configured.py`.
- `tests/e2e/test_llm_cli.py`.

**Tests expected**
- Each adapter translates the canonical schema to its real mechanism (OpenAI strict `json_schema`; Anthropic `output_config.format`/strict tool use; DeepSeek strict tool calling on `/beta`, with JSON-mode fallback); unsupported keywords are stripped; fields are never added or `additionalProperties` relaxed.
- Recorded valid/refusal/truncated/empty/malformed outputs produce `VALID` or `REJECTED` with the correct `llm_calls` rows; exactly one repair on schema failure; refusals are not retried; transport/5xx/429 retries are max 2 with backoff/jitter.
- `usage` is read from the provider response (input/output/cache read/write/reasoning); cost uses the versioned price table; an unknown model sets `unpriced=true` and surfaces the record.
- Budget rollups use the ADR-0005 defaults (daily $10 / weekly $50 / monthly $150, warn 80%, alert 100%) and never hard-stop; `budget_state` is visible through API/CLI; no Telegram send is attempted.
- Capability probe records strict-schema vs degraded JSON mode; DeepSeek degradation is shown as a provider status chip in CLI/API.
- No default model ID is shipped; a scan with no configured provider returns `LLM_PROVIDER_NOT_CONFIGURED` and a CALC-only report.
- Provider keys are read from settings/secret storage only; they never appear in prompts, logs, SQLite, Parquet, reports, or API responses; tests assert redaction.
- Provider adapters have no execution/Binance imports (import-linter); the LLM layer cannot import the gateway/broker.

**UI:** false. **Security:** true (provider keys, untrusted provider output, reject behavior). **Security review required:** yes.

**Acceptance criteria**
- [ ] `oft llm providers` lists configured providers/status; `oft llm models --provider <p>` lists model + price; `oft llm test --provider <p> --role <r>` records a capability result; `oft llm usage --period day|week|month` shows spend and `unpriced` counts.
- [ ] All three adapters pass recorded contract tests for valid/refusal/truncated/empty/malformed outputs; no network in default CI.
- [ ] A schema-invalid output is repaired once; a second failure is `REJECTED`, an alert is recorded locally, and no opportunity/order exists.
- [ ] Every call has a `budget-record` row with usage, unit prices, cost, price version, and `budget_state`; unknown models are `unpriced`.
- [ ] Threshold crossings are visible in API/CLI and logs; they do not stop calls.
- [ ] `oft llm test` and scans never log or return a key; the API returns masked values only.
- [ ] A clean data dir with no provider configured still runs a CALC-only scan and reports the explicit provider-not-configured reason.

**Preview screens**
- None in the Web UI; the CLI surfaces are F12 (`oft llm providers|models|test|usage`, `oft budget show|set|usage`). F10a `/settings/llm` and F10e `/settings/budget` are Milestone 4.

**Dependencies:** 11, 13, 14. **Size:** L.

---

### 16 — `16-opportunity-conditions-ledger`

**Title:** Opportunity ledger and the full deterministic condition set

**Goal:** Complete the structured opportunity lifecycle and condition model used by the LLM proposals and the trigger evaluator: all condition types from `llm-output`/`common.schema.json`, code-assigned condition IDs, required-parameter validation, `evaluable`/`unavailable_reason`, last value/result, expiry, invalidation, and append-only state history. This slice stores/evaluates conditions but does not create trigger events yet.

**Scope fence for this slice:** opportunity + condition domain/API/CLI; one-off condition evaluation for display. The 15 s trigger job is slice 17; paper execution is slice 18.

**Files expected**
- `src/oft/domain/conditions.py` — condition registry for `PRICE_CROSS_ABOVE/BELOW`, `PRICE_INSIDE_ZONE`, `PRICE_TOUCH_ZONE`, `PRICE_OUTSIDE_ZONE`, `DELTA_ABOVE/BELOW`, `CVD_ABOVE/BELOW`, `CVD_DIVERGENCE`, `OI_DELTA_ABOVE/BELOW`, `FUNDING_ABOVE/BELOW`, `LIQUIDATION_NOTIONAL_ABOVE`, `VOLUME_ABOVE`, `FOOTPRINT_IMBALANCE_ABOVE`, `SESSION_STATE`, `TIME_WINDOW`; required params per type; evaluability rules.
- `src/oft/domain/opportunities.py` — full lifecycle, expiry, invalidation, state history.
- `src/oft/application/opportunity_service.py` — map LLM `triggerConditionDraft`/`invalidationConditionDraft` to persisted conditions; validate; assign IDs; list/show/timeline; expire/invalidate via code.
- `src/oft/api/routes/opportunities.py` — list/show/timeline/export.
- `src/oft/cli/commands/opp.py` — list/show/timeline/export.
- `src/oft/adapters/sqlite_store.py` — opportunity/condition tables, state history, indexes.
- `tests/unit/test_conditions.py`, `test_opportunity_lifecycle.py`, `test_condition_evaluability.py`.
- `tests/integration/test_opportunity_ledger.py`.
- `tests/contract/test_opportunity_contract.py`.
- `tests/fixtures/llm/opportunity_*.json` — price, delta/CVD, OI/funding, footprint, time/session cases.

**Tests expected**
- Every condition type has a required-parameter validator; missing/extra-invalid params reject the draft with a named field and no partial opportunity.
- `evaluable=false` is set when the required data is absent (e.g. historical liquidations); such a condition is never treated as met.
- Price/zone conditions use exact decimals and tick-size rounding; no float at the boundary.
- Delta/CVD/OI/funding/volume/footprint conditions compare against the recorded metric values and window; no lookahead; missing series are `MISSING`/`PARTIAL`, not zero.
- `SESSION_STATE` uses the recorded trading-session state (TradFi `NO_TRADING` is a regime flag, not a halt); `TIME_WINDOW` uses UTC.
- State transitions are code-only: `OPEN → TRIGGERED/EXPIRED/INVALIDATED`; history is append-only with `from_state`, `to_state`, `at`, `reason_code`, `note`.
- Expiry is UTC and enforced; an expired opportunity cannot be evaluated into a trigger.
- API/CLI list filters by state; JSON validates `opportunity.schema.json`; no manual "trigger" or state-override command exists.

**UI:** false (CLI `oft opp`; Web S05.1/S05.2 in slice 19). **Security:** false. **Security review required:** no.

**Acceptance criteria**
- [ ] A report with a multi-condition opportunity persists every condition with a code-assigned ID and required params; an invalid draft is rejected with a field-level error.
- [ ] A condition whose required data is unavailable is `evaluable=false` with a reason and is never displayed as met.
- [ ] Expiry and invalidation move the opportunity to `EXPIRED`/`INVALIDATED` with a timeline entry; no LLM call can change state.
- [ ] `oft opp list --state triggered|open|expired|invalidated` and `oft opp show` work; JSON validates the contract.
- [ ] The API is read-only for the ledger; no manual trigger/override route exists.
- [ ] Condition tests cover every enum value and exact-decimal boundaries.

**Preview screens**
- S05.3 `oft opp list|show|timeline|export` (Web S05.1/S05.2 in slice 19).

**Dependencies:** 11, 12. **Size:** L.

---

### 17 — `17-trigger-scheduler`

**Title:** Deterministic trigger evaluator, 15 s job, and re-check queue

**Goal:** Evaluate OPEN opportunities on the ADR-0007 `trigger_evaluate` cadence (15 s default, configurable 5–60 s) and on demand; freeze the exact market snapshot; compute condition results; create exactly one `CODE` trigger per opportunity state change; enforce freshness/no-stale; move the opportunity to `TRIGGERED`; enqueue it for entry re-check. No order is created in this slice.

**Scope fence for this slice:** trigger evaluation, snapshot freezing, idempotency, job wiring, and the read/re-check queue. Entry re-check LLM call and accept/reject are slice 18.

**Files expected**
- `src/oft/domain/trigger.py` — deterministic evaluator over the condition set; snapshot hash; no lookahead; `CODE` source.
- `src/oft/application/trigger_service.py` — snapshot builder from bars/metrics, evaluate all OPEN opportunities, trigger event write, opportunity transition, queue.
- `src/oft/application/jobs.py` — register `trigger_evaluate` every 15 s (5–60 configurable), single instance, skip overlapping ticks, no catch-up.
- `src/oft/application/scheduler.py` / `src/oft/cli/commands/daemon.py` — job wiring.
- `src/oft/api/routes/triggers.py` — list/show; no accept/reject yet.
- `src/oft/cli/commands/trigger.py` — list/show/evaluate.
- `src/oft/adapters/sqlite_store.py` — `trigger_events` snapshot/condition fields, unique idempotency, indexes.
- `tests/unit/test_trigger_conditions.py`, `test_trigger_idempotency.py`, `test_trigger_no_lookahead.py`.
- `tests/integration/test_trigger_job.py`, `test_stale_snapshot_blocks_trigger.py`, `test_trigger_expiry_invalidation.py`.
- `tests/contract/test_trigger_event_contract.py`.

**Tests expected**
- The evaluator is deterministic for the same snapshot/condition set; property tests assert `evaluable=false` is never met and no condition reads future data.
- A stale mark (>60 s) or stale bar/metrics beyond the budget produces no trigger and a `STALE` reason; the trigger path never runs on stale data.
- `trigger_events.idempotency_key` is unique; repeated evaluation of the same opportunity/snapshot creates no duplicate trigger.
- Only code creates a trigger; `trigger_source=CODE`; the event records condition results (observed/threshold/evaluated_at/quality/evaluable), snapshot id/hash/as_of/quality, and `status=PENDING_RECHECK`.
- A trigger moves the opportunity to `TRIGGERED` and appends the state history; an expired/invalidated opportunity produces no trigger.
- The 15 s job is single-instance, skips an overlapping tick, and has no catch-up; restart reconstructs open opportunities from SQLite.
- No order/fill/position is created in this slice.

**UI:** false (CLI `oft trigger list|show|evaluate`). **Security:** false. **Security review required:** no.

**Acceptance criteria**
- [ ] `oft trigger evaluate` and the 15 s job produce the same trigger for the same snapshot/conditions.
- [ ] A stale snapshot yields no trigger and a visible `STALE` reason.
- [ ] Duplicate evaluation does not create a second trigger; the unique idempotency key is enforced by SQLite.
- [ ] A trigger is visible through `oft trigger show` and the API with condition results and snapshot provenance.
- [ ] The opportunity timeline shows `OPEN → TRIGGERED` with the trigger ID.
- [ ] Restart does not lose or duplicate an open opportunity or trigger.
- [ ] No order or execution gateway call occurs in this slice.

**Preview screens**
- S06.3 `oft trigger list|show`（read-only queue; accept/reject in slice 18）.

**Dependencies:** 16. **Size:** M.

---

### 18 — `18-entry-recheck-risk-paper-oms`

**Title:** LLM entry re-check, risk gate, and paper execution

**Goal:** For each trigger, obtain a schema-validated `ENTRY_RECHECK` decision from the configured provider/prompt; run the deterministic risk gate (stricter-of caps); require a human paper accept/reject with idempotency; submit through the single execution gateway to the deterministic paper broker; persist order/fill/position/trade references and audit events. No live broker and no management actions.

**Scope fence for this slice:** entry re-check, risk gate, accept/reject, gateway, paper broker, paper account basics, kill switch, audit. No PnL/funding/journal/management/paper reset.

**Files expected**
- `src/oft/application/entry_service.py` — trigger → `ENTRY_RECHECK` call, validation, decision card, risk verdict, accept/reject.
- `src/oft/domain/risk.py` — full caps and property tests: per-trade 0.8%, max concurrent 3, daily loss 3%, leverage 5x, max notional 20%, max slippage 25 bps, max spread 15 bps, stale mark 60 s, stop required, min notional/step/tick.
- `src/oft/application/execution_gateway.py` — sole submit path; kill switch; risk verdict freshness/consistency; deterministic `client_order_id = oft-{env}-{symbol}-{purpose}-{hash(decision_id|intent_hash)[:16]}`; idempotency; audit.
- `src/oft/application/paper_broker.py` — market and limit fill model; 2 bps slippage, 5 bps taker, 2 bps maker; no partial fills by default; deterministic seed/clock.
- `src/oft/domain/oms.py` — `CREATED → SUBMITTED → PARTIALLY_FILLED → FILLED`, plus `CANCELED/REJECTED/EXPIRED/UNKNOWN`; position `OPEN`; no management.
- `src/oft/application/paper_account.py` — 10,000 USDT initial balance; balance/equity summary; orders/fills/positions read models.
- `src/oft/application/kill_switch.py` — DB + file sentinel (0600), restart test, risk-reducing action classification.
- `src/oft/api/routes/triggers.py` — `recheck`, `accept`, `reject` with idempotency key; `paper.py` read models.
- `src/oft/cli/commands/trigger.py` — `recheck|accept|reject`; `paper.py` — `balance|orders|fills|equity`; `kill.py`; `risk.py` — `show|set|validate`.
- `src/oft/adapters/sqlite_store.py` — `risk_verdicts`, `orders`, `fills`, `positions`, `paper_accounts`, `audit_events`, `system_state`.
- `src/oft/config.py` — risk caps and paper broker defaults (ADR-0006), no secrets.
- `tests/unit/test_risk_gate.py` (property tests), `test_oms_state_machine.py`, `test_paper_broker.py`, `test_kill_switch.py`.
- `tests/integration/test_entry_recheck_flow.py`, `test_paper_accept_idempotency.py`, `test_risk_block_no_order.py`, `test_kill_switch_blocks.py`, `test_restart_kill_switch.py`.
- `tests/contract/test_order_fill_position_contracts.py`.
- `tests/replay/test_paper_entry_replay.py`.

**Tests expected**
- A trigger creates a `VALID`/`REJECTED` entry decision; a rejected/refused/truncated output creates no order, shows the failing field, and records the reason; one repair max.
- The risk gate recomputes from current account/snapshot and records requested vs allowed, binding cap, `inputs_hash`, TTL, stale inputs; `effective_size` is the stricter side; BLOCK/REDUCE never creates an order.
- A stopless intent, stale mark, spread/slippage breach, max positions, daily loss, leverage, min notional, or step/tick violation blocks or reduces with a named cap.
- `oft trigger accept` in PAPER creates one order/fill/OPEN position; the same idempotency key returns the original and creates nothing; a different payload returns `IDEMPOTENCY_CONFLICT`.
- Every order has a persisted `risk_verdict_id`, deterministic `client_order_id`, `intent_hash`, and `environment=PAPER`; no order exists without a verdict; no live path is reachable.
- The kill switch blocks new paper orders and every retry; the file sentinel survives restart; `oft kill off` requires `RESUME`; paper positions are not auto-closed.
- Paper defaults: 10,000 USDT, mark/last + 2 bps slippage, 5 bps taker fee; decimal strings; identical fixed clock/seed → identical fills.
- `oft trigger reject --reason` invalidates the opportunity, records the reason, and creates no order; the decision remains for review.
- Audit events for decision/verdict/order/fill/position are append-only and hash-chained; `oft doctor` verifies the chain.
- No management action (HOLD/PARTIAL/FULL/ADD), PnL/win-rate, funding, or paper reset exists in this slice.

**UI:** false (CLI `oft trigger`/`oft paper`/`oft risk`/`oft kill`; Web S06/S08 in slice 20). **Security:** true (paper order path, risk gate, kill switch, idempotency, audit). **Security review required:** yes.

**Acceptance criteria**
- [ ] A trigger produces an entry decision; `NO_ENTRY` or `REJECTED` creates no order and shows the reason.
- [ ] The decision card (API/CLI) shows Trigger CALC → LLM decision → Risk gate CALC → human action in order.
- [ ] `oft trigger accept` creates exactly one paper order/fill/OPEN position; duplicate accept is idempotent; a second distinct intent is a new order with a new id.
- [ ] Every order has a risk verdict and deterministic client order ID; an order without them is rejected by the gateway.
- [ ] Kill switch on blocks new paper orders; off requires `RESUME`; restart preserves state; positions remain open.
- [ ] `oft paper balance|orders|fills|equity` shows the simulated account; no fake fills; values are decimal strings.
- [ ] `oft risk show|set|validate` works with the documented defaults; loosening a cap is explicit and audited.
- [ ] `oft doctor` reports a valid audit chain and no secrets.
- [ ] No live broker, Binance key, Telegram, or position-management path exists.

**Preview screens**
- S06.3 `oft trigger list|show`（decision queue）.
- S06.4 `oft trigger accept|reject|recheck`（paper `[y/N]`, typed token only where a future live path would require it）.
- S08.2 `oft paper balance|orders|fills|equity`.
- S06.2 decision-card content is implemented in API/CLI; Web rendering is slice 20.

**Dependencies:** 15, 17. **Size:** L.

---

### 19 — `19-report-opportunity-ui`

**Title:** Web report LLM band and opportunity ledger

**Goal:** Extend the existing report detail with a distinct `LLM` band and opportunity cards, and add the `/opportunities` ledger list/detail/timeline. Keep CALC and LLM provenance visually separate, preserve partial-data states, and never allow an accept action from the report.

**Scope fence for this slice:** report LLM/opportunity UI and opportunity ledger UI only. Trigger review, paper account, and settings UI are slice 20 / Milestone 4.

**Files expected**
- `web/src/routes/report-detail.tsx` — CALC band first, then `LLM` band (narrative, decision, confidence, model/provider/prompt version, cost, knowledge hash), rejection/absent state, opportunity cards.
- `web/src/routes/reports.tsx` — `CALC only` / `LLM` / `REJECTED` chips.
- `web/src/routes/opportunities.tsx`, `opportunity-detail.tsx`, `web/src/api/opportunities.ts`.
- `web/src/components/{llm-band,opportunity-card,provenance,data-quality}.tsx` (extend existing components).
- `web/src/App.tsx`, route matching.
- `web/src/routes/__tests__/report-detail.test.tsx`, `opportunities.test.tsx`.
- `tests/e2e/test_report_llm.py`, `test_opportunities_flow.py`.
- `docs/contracts/report.schema.json` if the report payload is promoted to a dedicated contract; otherwise update the contract notes.

**Tests expected**
- `/reports/:id` renders CALC metrics first, then the `LLM` band with cyan provenance; a rejected narrative shows the failing schema field and "no narrative", while metrics/chart remain usable.
- The LLM band shows provider/model/prompt version/confidence band/cost/knowledge hash; it never claims an LLM sentence is a measurement.
- Opportunity cards link to `/opportunities/:id`; the report has no accept/reject action.
- `/opportunities` filters by state; empty states distinguish "no scan yet" vs "no opportunity in this filter"; counts are neutral/cyan/gray/red per design.
- `evaluable=false` conditions are labelled and never displayed as met; a missing source report shows `N/A` with a link.
- No raw HTML injection from narrative/knowledge; CSP unchanged; 1440/768/375 keyboard and axe checks pass; 375px remains read-only.
- Narrative failure is not a scan failure; the CALC report remains readable.

**UI:** true. **Security:** true (XSS/CSP, untrusted LLM narrative). **Security review required:** yes.

**Acceptance criteria**
- [ ] `/reports/:id` shows CALC and LLM bands in the mandatory order; a rejected/absent narrative is explicit and does not blank the report.
- [ ] `/opportunities` and `/opportunities/:id` show direction, zone, conditions, invalidation, expiry, confidence, source report/scan, model/prompt version, and the state timeline.
- [ ] Partial-data/`evaluable=false` states are visible and never treated as met.
- [ ] The report and ledger render at 1440/768/375; keyboard navigation and axe checks pass; no raw HTML.
- [ ] No accept/reject action exists on the report or ledger.
- [ ] No secret, raw prompt, or provider key is rendered.

**Preview screens**
- S04.1 `/reports`（LLM/CALC-only/rejected chips）.
- S04.2 `/reports/:reportId`（CALC band, chart, `LLM` band, opportunity cards）.
- S04.3 chart panel unchanged from Milestone 1.
- S05.1 `/opportunities`; S05.2 `/opportunities/:oppId`.

**Dependencies:** 12, 16. **Size:** L.

---

### 20 — `20-trigger-paper-ui`

**Title:** Web trigger review and paper account

**Goal:** Add the decision queue (`/triggers`), the decision card (`/triggers/:id`) in the mandatory order Trigger CALC → LLM → Risk gate CALC → human action, paper accept/reject with confirmation, and the paper account view (`/paper`) with orders/fills/positions. No live controls, no management actions.

**Scope fence for this slice:** trigger queue/detail and paper account read/accept UI only. No live toggle, no position management, no paper reset, no settings pages.

**Files expected**
- `web/src/routes/triggers.tsx`, `trigger-detail.tsx`, `paper.tsx`.
- `web/src/api/triggers.ts`, `paper.ts`.
- `web/src/components/{decision-card,risk-panel,paper-tables,kill-switch}.tsx`.
- `web/src/App.tsx`, route matching.
- `web/src/routes/__tests__/triggers.test.tsx`, `paper.test.tsx`.
- `tests/e2e/test_trigger_accept_paper.py`, `test_trigger_kill_switch.py`, `test_paper_view.py`.

**Tests expected**
- `/triggers` shows pending/resolved, symbol/side/age/confidence/risk verdict/environment `PAPER`; pending count is visible; the kill switch remains available.
- `/triggers/:id` shows the decision card in the mandatory order; requested vs allowed caps and the binding cap; confidence numeric + band; model/prompt/cost; accept disabled when BLOCKED/stale with the reason.
- Paper accept opens a confirmation showing final size, caps applied, and that the LLM cannot bypass risk caps; reject requires a reason; duplicate accept is idempotent and produces no second order.
- `/paper` shows equity, orders, fills, and open positions; no fake fills; stale marks are labelled; paper reset is absent.
- Kill switch on/off works from every state; off requires the typed `RESUME` token; 375px is read-only except the kill switch.
- Keyboard and axe checks pass; no raw HTML; no secret/prompt body is rendered.

**UI:** true. **Security:** true (paper order action, idempotency, kill switch, XSS/CSP). **Security review required:** yes.

**Acceptance criteria**
- [ ] A pending paper trigger can be accepted from `/triggers/:id`; the resulting `/paper` orders/fills/position match the CLI/API values.
- [ ] The decision card shows the mandatory order and the binding cap; a BLOCKED verdict disables accept with the reason.
- [ ] Reject records a reason and moves the opportunity to `INVALIDATED`; the decision remains visible.
- [ ] Duplicate accept does not create a second order.
- [ ] The kill switch is available in every state; `oft kill on` is immediate; `off` requires `RESUME`.
- [ ] `/paper` renders orders/fills/positions without fake data; 375px is read-only except the kill switch.
- [ ] No live order, Binance credential, Telegram, or management action is reachable.

**Preview screens**
- S06.1 `/triggers`; S06.2 `/triggers/:triggerId`.
- S08.1 `/paper`.
- S06.3/S06.4/S08.2 CLI surfaces were delivered in slice 18.

**Dependencies:** 18, 19. **Size:** L.

---

### 21 — `21-replay-hardening`

**Title:** Replay, determinism, coverage, and runbook

**Goal:** Make the paper loop reproducible and operable: replay recorded market data and recorded/stub LLM responses with the same domain code; fixed clock/seed; replay namespace only; no live path; coverage gates; import-linter; audit-chain verification; and a runbook a human can follow.

**Scope fence for this slice:** replay/testing/operations only. No new product behavior, no new provider/condition/execution semantics.

**Files expected**
- `src/oft/replay/harness.py` — extend to the paper loop.
- `src/oft/replay/llm_source.py` — recorded responses by `(role, input_hash)`; explicit `--live-llm`.
- `src/oft/cli/commands/replay.py`; `src/oft/cli/main.py`.
- `scripts/fetch-fixtures` — recorded LLM fixture capture (redacted) if needed.
- `tests/replay/test_paper_loop_replay.py`, `test_llm_replay.py`, `test_replay_namespace.py`, `test_non_determinism_fails.py`.
- `tests/test_no_wall_clock.py` — extend to `domain.risk`, `domain.trigger`, `domain.oms`, and the new application services.
- `tests/contract/test_all_contract_fixtures.py` — extend to LLM/provider/opportunity/trigger/order/fill/position/budget fixtures.
- `pyproject.toml` — import-linter contracts for `oft.adapters.llm_*` and `oft.application.execution_gateway`/`paper_broker`.
- `docs/runbooks/milestone-2.md`.
- CI config if a new test marker/job is required.

**Tests expected**
- Replaying the same scenario twice with the same `metric_version`, `prompt_version`, `knowledge_artifact_hash`, and recorded LLM outputs produces identical decisions, triggers, orders, fills, positions, and entry-level prices/fees/position values (no PnL calculation in this milestone).
- Replay writes only `replay_*` tables/IDs; it cannot touch the paper account, live orders, positions, or budget; an attempted live path exits non-zero.
- A missing recorded LLM output fails with a clear error unless `--live-llm` is explicitly set; replay never invents an output.
- `tests/test_no_wall_clock.py` finds no `datetime.now()`/`time.time()` in the deterministic path; all time enters through `ClockPort`.
- Coverage ≥80% overall; 100% branch on `domain.risk`, `domain.trigger`, and the OMS state machine.
- Import-linter: `domain` imports only stdlib/Pydantic; `adapters.llm_*` cannot import execution/Binance; `api` cannot import broker/LLM adapters directly.
- `oft doctor` verifies the audit hash chain, provider configuration, budget ledger, stale triggers, kill switch, and disk/DB health; it reports no secrets.
- The runbook reproduces the demo path from a clean data dir without Telegram, Binance credentials, or a live provider (stub/recorded mode).

**UI:** false. **Security:** false (but includes negative security/import tests). **Security review required:** no.

**Acceptance criteria**
- [ ] `uv run pytest -q` passes offline, including replay and new contract tests.
- [ ] Replay is deterministic and fails loudly on non-determinism or an attempted live path.
- [ ] The no-wall-clock test covers all deterministic modules; `ClockPort` is the only time source.
- [ ] Coverage is ≥80% overall and 100% branch on risk/trigger/OMS.
- [ ] Import-linter proves the LLM layer cannot reach execution and the API does not import brokers/providers directly.
- [ ] `oft doctor` verifies the audit chain and reports no secrets; `docs/runbooks/milestone-2.md` reproduces the demo.
- [ ] No new product behavior is introduced in this slice.

**Preview screens**
- None.

**Dependencies:** 11–20. **Size:** M.

---

### 22 — `22-storage-refactor`

**Title:** Storage hardening: split the SQLite store and add residual foreign keys and enum CHECK constraints

**Goal:** Close the three storage items deferred from slice 11 (`fix-mediums.md` M13, M22 residual, M26) without changing behavior:

- **M13 — split `src/oft/adapters/sqlite_store.py`** (3,571 lines at review time) into cohesive store modules behind `StorePort`, using the two-pass mixin plan in `fix-mediums.md`: first extract `SqliteConnectionMixin` (`_connect`, `_connect_read_only`), `LLMStoreMixin` (LLM calls/decisions/budget plus row mappers), and `TradingStoreMixin` (opportunities/events, triggers, risk verdicts, OMS, paper account, `_insert_audit_event`, row mappers); then extract the scan/report/job stores so the remaining file is below the 800-line soft ceiling. `SqliteStore` composes the mixins and keeps the schema DDL and migrations.
- **M22 residual — add the missing foreign keys** `orders.risk_verdict_id → risk_verdicts(risk_verdict_id)`, `risk_verdicts.decision_id → decisions(decision_id)`, and `positions.account_id → paper_accounts(account_id)`, and reorder `save_order_fill_position` so the account row exists before the position within the same transaction.
- **M26 — add CHECK constraints** for the v10 status/enum columns: `llm_calls.status`, `decisions.validation_status`, `trigger_events.status`, `orders.status`, and `positions.state`, using the existing additive/rebuild migration pattern.

**Migration version note:** `fix-mediums.md` scoped the upgrade test as **v14 → v15** because v14 was current when slice 11 was reviewed. Slice 12 has since applied v15 (`scan_symbols.failed_stage`), so slice 22 uses the next free version — **v15 → v16** unless an intervening slice adds a migration — and the test records the actual `from`/`to` pair. A populated v14 DB still exercises the whole migration chain.

**Scope fence for this slice:** store module extraction, the residual-FK/CHECK migration, and tests only. No SQL semantics change, no transaction-boundary change, no API/CLI/Web change, no new product behavior, no new tables or columns beyond constraints and keys.

**Files expected**
- `src/oft/adapters/sqlite_store.py` — keeps schema DDL and migrations; composes the mixins; shrinks below the 800-line soft ceiling after the second pass.
- `src/oft/adapters/sqlite_connections.py` — `SqliteConnectionMixin` with `_connect`, `_connect_read_only`.
- `src/oft/adapters/sqlite_llm_ledger.py` — `LLMStoreMixin`: `llm_calls`, `decisions`, `budget_ledger`, and their row mappers.
- `src/oft/adapters/sqlite_trading_ledger.py` — `TradingStoreMixin`: opportunities/events, triggers, risk verdicts, OMS, paper account, audit-insert helper, and row mappers.
- `src/oft/adapters/sqlite_scan_store.py` — second-pass mixin for scans/reports/job runs and their row mappers.
- `src/oft/ports/store.py` — only if a type/protocol re-export is needed; no method signature changes.
- `tests/integration/test_store_atomicity.py` — seed parent rows for synthetic verdicts, orders, and positions.
- `tests/integration/test_store_migrations.py` (or the existing upgrade test) — populated v14 → next-version upgrade, `PRAGMA foreign_key_check`, and constraint negatives.
- Cross-trigger replay tests — seed the parent rows they previously omitted.

**Tests expected**
- **Behavior unchanged:** the full existing suite passes with only migration-version assertions updated; no test expectation about stored rows, values, or SQL results changes.
- **Populated upgrade:** build a v14 DB with representative rows in `llm_calls`, `decisions`, `trigger_events`, `risk_verdicts`, `orders`, `fills`, `positions`, `paper_accounts`, and opportunity/event tables, then upgrade; every row is preserved, `PRAGMA foreign_key_check` returns no rows, and no data is lost.
- **Constraint negatives:** an orphan `orders.risk_verdict_id`, `risk_verdicts.decision_id`, or `positions.account_id` is rejected; an invalid status/enum in each of the five columns is rejected; every value the application writes is accepted.
- **Atomicity:** `save_order_fill_position` inserts the account before the position and keeps the existing single-transaction and balance behavior (lost-update and duplicate-accept tests pass unchanged).
- **Structure:** a check confirms `sqlite_store.py` is ≤800 lines after the second pass, or documents each extracted module as cohesive and the remaining file below the ceiling.
- `uv run pytest -q`, `uv run mypy`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run lint-imports` all pass.

**UI:** false. **Security:** false (schema/store refactor; no new external input surface). **Security review required:** no.

**Acceptance criteria**
- [ ] No behavior change: all existing tests pass unchanged except migration-version assertions; SQL text, transaction boundaries, and public method signatures are unchanged by the move.
- [ ] A populated v14 DB upgrades to the new schema with the three residual FKs and the five CHECK constraints; `PRAGMA foreign_key_check` is empty and no data is lost.
- [ ] Orphan-key and invalid-enum negative cases are rejected by the database.
- [ ] `src/oft/adapters/sqlite_store.py` no longer exceeds the 800-line soft ceiling after the split, or each extracted module is cohesive and the remaining file is below it; schema DDL and migrations stay in one place.
- [ ] `uv run lint-imports` still passes (5 kept / 0 broken) and `mypy`, `ruff`, and `pytest` are green.
- [ ] The two-PR path is acceptable: the orchestrator may split the slice into two behavior-preserving PRs (mixin extraction first, then scan/report/job extraction), and each PR passes the full suite on its own.

**Preview screens**
- None (no user-visible surface changes).

**Dependencies:** 11–21. **Size:** L.

---

## 5. Test strategy for the milestone

### 5.1 Levels and tools

| Level | Scope | Tools | Rules |
|---|---|---|---|
| Unit | LLM schema/semantic validation, prompt versioning, conditions, trigger evaluator, risk gate, OMS, paper broker, cost math, kill switch | pytest, Hypothesis | Pure functions; no I/O; injected `ClockPort`; no network. |
| Contract | `llm-output`, `llm-decision`, `opportunity`, `trigger-event`, `order`, `fill`, `position`, `budget-record`, `api-envelope`, error catalog; every provider fixture | pytest + `jsonschema` Draft 2020-12 | Every fixture and route response validates; the error enum equals `error-catalog.json`; generated Pydantic/TS types, no handwritten duplicate shapes. |
| Integration | SQLite/Parquet persistence, scan stages, job ledger, trigger job, entry re-check, risk verdict, paper accept, kill switch, audit chain | pytest, `tmp_path` | Real SQLite/Parquet; no network; recorded market data and LLM responses. |
| Replay | Recorded window → bars → metrics → LLM stub/recorded → trigger → entry re-check → paper fill | replay harness | Same domain code as live; pinned metric/prompt/knowledge versions; fixed clock/seed; `replay_*` only. |
| E2E | Starred scan → report → opportunity → trigger → accept → paper position; scan/report/opportunity/trigger/paper screens; 375/768/1440; keyboard; axe | pytest + HTTPX, Playwright, Vitest/RTL, axe | Recorded data + LLM stub; no real order; no live credential. |
| Provider contract | OpenAI/Anthropic/DeepSeek valid/refusal/truncated/empty/malformed outputs | recorded responses; optional nightly live | Schema-valid rate and repair behavior; no network in default CI. |
| Security | Prompt injection via market/knowledge text, secret redaction, risk-cap bypass, duplicate accept, stale trigger, kill switch, XSS/CSP | pytest, Playwright | Negative cases are mandatory. |
| Live smoke (opt-in) | Public market data only (no credentials) | `@pytest.mark.live_smoke` | Never in default CI; not a golden test. |

### 5.2 LLM fixtures and stubs

- `tests/fixtures/llm/` contains small, committed, redacted provider-shaped responses for every outcome: valid, refusal, truncated, empty content, malformed JSON, schema-unsupported construct.
- `LLMStub` implements `LLMPort` and returns a fixture by `(role, input_hash)`; the decision provenance records `provider="stub"`, `model="fixture"` so a paper run cannot be mistaken for a real provider call.
- Provider adapters are tested against recorded responses, not the network. `--live-llm` is explicit and never part of default tests.
- Recorded responses contain no real key, account data, or prompt body with secrets; fixtures are checksummed/README-tagged like the market fixtures.
- No copyrighted book text or generated skill is committed; the knowledge tests use a synthetic playbook with the same structure.

### 5.3 Deterministic time, IDs, and paper fills

- All time enters through `ClockPort`; tests use `FixedClock`/`ReplayClock`.
- No `datetime.now()`, `time.time()`, or local timezone in `domain`/`application`; `tests/test_no_wall_clock.py` fails the build on a violation.
- All windows, expiry, trigger cadence, and comparisons use UTC.
- Decision/order/fill/position IDs and `client_order_id` are deterministic given the fixture, versions, fixed clock, and seed.
- Paper broker slippage/fee/seed are part of the replay run; no hidden randomness.

### 5.4 Coverage gate

- Overall line coverage ≥80%.
- 100% branch coverage on `src/oft/domain/risk.py`, `src/oft/domain/trigger.py`, and the OMS state machine in `src/oft/domain/oms.py` (ADR-0011).
- Coverage is a floor: missing branches must be tested or explicitly justified in the PR.
- Frontend: Vitest/RTL for the new report/opportunity/trigger/paper components; Playwright for the paper loop screens; axe-core for WCAG checks.

### 5.5 Explicitly not tested yet

- Live Binance order placement, testnet, reconciliation, user-data stream, live arming, leverage brackets.
- Telegram delivery, receipts, allowlist, budget pushes.
- Position management (HOLD/PARTIAL/FULL/ADD), funding accrual, PnL/win-rate/R-multiple, journal/review, paper reset.
- Full settings Web UI (F10a–F10g), prompt editor Web UI, knowledge review UI, 375px editing beyond read-only.
- News/social text; historical USDⓈ-M liquidations; full L2/DOM.
- Real provider nightly baselines (opt-in only; no CI gate until a baseline exists).

---

## 6. Milestone exit criteria

A milestone is complete only when all of the following are true:

- [ ] The 4h/8h scheduled scan and the manual scan both scan all starred symbols with per-symbol `DATA → METRICS → LLM → REPORT` stages; one symbol failure does not abort the batch.
- [ ] A symbol whose narrative fails is `DEGRADED` and still has a usable CALC-only report; the scan status is honest (`COMPLETE`/`PARTIAL`/`FAILED`).
- [ ] The LLM provider abstraction works with all three providers through recorded contract tests; valid/refusal/truncated/empty/malformed outputs are handled by one validation path; invalid output never creates an opportunity or order.
- [ ] Every LLM call writes usage/cost/budget rows; unknown models are `unpriced`; thresholds alert locally without a hard stop; no Telegram is required.
- [ ] Prompt versions and the knowledge artifact hash are recorded on every decision; missing versions/artifacts are explicit and disable exact replay rather than silently falling back.
- [ ] The knowledge playbook compiles within budget, is private/gitignored, and is injected as delimited data; no full corpus or copyrighted text is committed.
- [ ] The opportunity ledger persists every structured condition, invalidation, expiry, confidence, and state transition; only code can change state; domain data is never invented.
- [ ] The deterministic trigger evaluator creates exactly one `CODE` trigger per opportunity state change on a fresh snapshot; stale data cannot trigger; duplicate evaluation is idempotent.
- [ ] The LLM entry re-check is schema-validated; `NO_ENTRY`/rejected output creates no order; the decision card shows Trigger CALC → LLM → Risk gate CALC → human action.
- [ ] The risk gate takes the stricter of the LLM proposal and the user caps, records requested/allowed/binding cap, and blocks stopless/oversized/stale intents.
- [ ] A paper accept creates exactly one order/fill/OPEN position through the single execution gateway; duplicate accepts are idempotent; every order has a risk verdict and deterministic client order ID.
- [ ] The kill switch blocks new paper orders and retries and survives restart; `off` requires `RESUME`.
- [ ] The audit hash chain covers decisions, verdicts, triggers, orders, fills, positions, and kill-switch events; `oft doctor` verifies it.
- [ ] Contract tests pass for the API envelope, error catalog, LLM/provider fixtures, opportunity, trigger, order, fill, position, and budget rows.
- [ ] Replay is deterministic and writes only the replay namespace; the no-wall-clock test covers the deterministic path; coverage ≥80% with 100% branch on risk/trigger/OMS.
- [ ] The Web report/opportunity/trigger/paper surfaces implement the named preview screens with CALC/LLM provenance, partial-data states, keyboard/axe checks, and 375px read-only behavior.
- [ ] **No Binance credential, Telegram token, live broker, or secret is required or recorded.** Public market data and fixtures are sufficient.
- [ ] The runbook demo path succeeds from a clean data directory.

---

## 7. Human demo path (local, paper only)

```bash
# 1. Install and test
uv sync
uv run pytest -q

# 2. Initialise a local data dir and start the daemon
mkdir -p ./.oft-data
export OFT_DATA=./.oft-data
uv run oft serve

# 3. Configure an LLM provider/model (CLI/API; no default model is shipped).
#    Provider/model/role and keys come from the local settings surface or
#    OFT_* environment variables; keys are write-only and never returned.
#    For an offline demo, use the stub/recorded mode instead of a real key.
uv run oft llm providers
uv run oft llm test --provider stub --role ANALYSIS
# With a real provider, configure the provider/model first (for example via
# the settings surface introduced for this milestone), then:
# uv run oft llm test --provider openai --role ANALYSIS

# 4. Star the symbols to scan (public market data; no Binance credential)
uv run oft watchlist star BTCUSDT
uv run oft watchlist star XAUUSDT

# 5. Ensure history is present (public data.binance.vision; no key)
uv run oft backfill run --symbol BTCUSDT --start 2026-10-01 --end 2026-10-07 --yes

# 6. Run the loop
uv run oft scan run --period 4h
uv run oft scan status
uv run oft report list
uv run oft report show <report_id>
uv run oft opp list --state open
uv run oft opp show <opportunity_id>
uv run oft trigger evaluate <opportunity_id>
uv run oft trigger show <trigger_id>
uv run oft trigger accept <trigger_id>
uv run oft paper orders
uv run oft paper fills
uv run oft paper equity

# 7. Safety and replay
uv run oft kill status
uv run oft kill on
uv run oft kill off          # requires typed RESUME
uv run oft replay run --scenario paper-loop

# 8. Open the Web surfaces (read-only report/opportunity/trigger/paper)
#    http://127.0.0.1:7611/reports/<report_id>
#    http://127.0.0.1:7611/opportunities/<opportunity_id>
#    http://127.0.0.1:7611/triggers/<trigger_id>
#    http://127.0.0.1:7611/paper
```

Expected: the scan produces CALC + LLM reports, the opportunity ledger is populated, the trigger is created by code, the paper accept creates a simulated order/fill/position, the kill switch blocks new paper orders, and no Binance credential, Telegram token, live order, or secret is required. A narrative failure shows a DEGRADED/CALC-only report instead of failing the scan.

---

## 8. Risks and rollback

| Risk | Mitigation |
|---|---|
| Provider schema quirks (OpenAI/Anthropic/DeepSeek) | One canonical provider-safe schema, recorded contract fixtures, capability probe, adapter translation only, one repair, reject-and-record. |
| Prompt injection via market data, knowledge text, or symbol metadata | Delimit untrusted data; state precedence in the system prompt; no tools; output schema whitelist; control-character stripping/length caps; injection tests; no external news/social text. |
| LLM output accepted as truth | Code validates schema + semantics; risk gate recomputes; human paper accept; provenance/`CALC` vs `LLM` labels; no LLM numeric trust. |
| Duplicate paper order / double accept | Deterministic `client_order_id`, unique constraint, API idempotency key, single execution gateway, duplicate-accept tests. |
| Risk-cap bypass | Code-only caps, stricter-of, recompute at submit, verdict TTL, stale-mark block, property tests that never exceed caps. |
| Stale data drives a trigger/entry | Freshness budgets, no trigger on stale snapshots, risk gate stale-mark block, UI `STALE` stamps, tests. |
| Trigger storm / LLM cost runaway | 15 s single-instance job, no catch-up, per-provider semaphores, per-scan call budget, circuit breaker, budget ledger/rollups. |
| Narrative failure fails the scan | `DEGRADED` symbol + valid CALC-only report; scan-level status distinguishes PARTIAL/FAILED. |
| Prompt/knowledge version drift | Immutable versions, hashes on every decision, missing-version state, no silent fallback; replay reports non-reproducibility. |
| Copyright / private knowledge leak | `books/`, `skills/`, `playbook/` gitignored; no derived content committed; synthetic knowledge fixtures; export warnings. |
| Audit chain overhead / tampering | Append-only hash chain, verified at startup and by `oft doctor`; backups; a broken chain blocks trading actions. |
| Scope creep into live/Telegram/management/full UI | Explicit slice fences; import-linter; tests assert no live broker/Telegram/management path; milestone exit criteria. |
| Provider model IDs/prices change | Model IDs/prices are configuration data, no defaults shipped; re-verify at implementation time; `unpriced` records. |

**Rollback**
- Keep the Milestone 1 CALC-only scan/report path intact; setting `metrics_only=true` or having no provider configured falls back to it without data migration.
- All new SQLite tables are additive and forward-only; old reports/rows remain readable. If a migration must be reverted, restore the pre-migration backup rather than editing published rows.
- Trigger/entry jobs can be disabled by configuration; the kill switch blocks new paper orders immediately. No live order can exist, so rollback risk is bounded to local paper data.
- Provider rollback is configuration-only: switch the active provider/model or return to stub/recorded mode; prompt/knowledge rollback activates a previous immutable version.
- If a Web slice regresses, the CLI/API loop remains fully usable; the report/opportunity/trigger data is unaffected.

---

## 9. Follow-ups and open questions

1. **Knowledge Mode B:** deterministic topic retrieval is implemented only if the compiled `topics.json` is available; otherwise Mode A remains the only injection mode. The playbook taxonomy needs human review.
2. **Provider/model/pricing snapshot:** re-verify model IDs, prices, strict-schema behavior, and DeepSeek beta stability at implementation time. No default model is shipped.
3. **Budget defaults:** validate the ADR-0005 daily $10 / weekly $50 / monthly $150 and 80% warn threshold after two weeks of real logs; Telegram budget alerts are Milestone 5.
4. **Paper reset / Live gate:** reset semantics and the 4-week Live gate are Milestone 3. Milestone 2 creates the paper account once and never resets it.
5. **Position management depth:** HOLD/PARTIAL_CLOSE/FULL_CLOSE/ADD, funding accrual, PnL/win-rate/R-multiple, journal/review are Milestone 3.
6. **Full dual-entry UI:** F10 settings, prompt/knowledge editors, risk/budget settings UI, 375px editing, and chart polish are Milestone 4.
7. **Telegram:** report/trigger/risk/budget pushes, receipts, and allowlist are Milestone 5.
8. **Live execution:** Binance testnet/live broker, reconciliation, arming, typed confirmations, and the live kill-switch UI are Milestone 6.
9. **Dedicated report contract:** if the report payload grows beyond the embedded `market-snapshot` + `orderflow-metrics` + LLM/decision fields, add `docs/contracts/report.schema.json` additively; do not break existing CALC-only reports.
10. **Paper fill realism:** no queue-position modeling; partial fills off by default. The fill model (slippage/fees) is documented in the journal/runbook and revisited if paper results diverge from live.
11. **Session/`NO_TRADING` handling:** treat it as a regime flag per research, not a halt; the human validation of order-type rejection during `NO_TRADING` remains a pre-Live item.
12. **No `needs-human` blocker:** the plan proceeds on the PRD/ADR assumptions above. If G3 wants automatic paper entry, a different paper-account reset policy, or a different knowledge scope, that is a product decision to record before implementation.
13. **Storage hardening moved to slice 22:** the three storage items deferred from slice 11 are no longer open follow-ups — M13 (`sqlite_store.py` split), M22 residual (`orders.risk_verdict_id`, `risk_verdicts.decision_id`, `positions.account_id` FKs), and M26 (CHECK constraints on the v10 status/enum columns) are slice `22-storage-refactor`. The migration numbers in `fix-mediums.md` are stale: slice 12 consumed schema v15, so slice 22 uses the next free version (v15 → v16 unless an intervening slice adds a migration).

---

## 10. Slice-to-preview traceability

| Slice | Preview surfaces |
|---|---|
| 11 | S04.4 `oft report show`; S05.3 `oft opp`; S06.3/S06.4 `oft trigger`; S08.2 `oft paper`; no Web UI |
| 12 | S03.1 `/scans`; S03.2 `/scans/:scanId`; S03.3 `oft scan run`; S03.4 `oft scan status|history|show` |
| 13 | None in Web; CLI `oft prompt` (F12); F10b in Milestone 4 |
| 14 | None directly; knowledge provenance in S04.2 (slice 19) and CLI report output |
| 15 | None in Web; CLI `oft llm`, `oft budget` (F12); F10a/F10e in Milestone 4 |
| 16 | S05.3 `oft opp list|show|timeline|export`; Web S05.1/S05.2 in slice 19 |
| 17 | S06.3 `oft trigger list|show` (read-only queue) |
| 18 | S06.3 `oft trigger list|show`; S06.4 `oft trigger accept|reject|recheck`; S08.2 `oft paper balance|orders|fills|equity`; decision-card content in API/CLI |
| 19 | S04.1 `/reports`; S04.2 `/reports/:reportId`; S04.3 chart unchanged; S05.1 `/opportunities`; S05.2 `/opportunities/:oppId` |
| 20 | S06.1 `/triggers`; S06.2 `/triggers/:triggerId`; S08.1 `/paper` |
| 21 | None |
| 22 | None (storage refactor; no user-visible surface) |

---

*End of plan. No gate is approved by this document. Milestone 2 G3 remains pending; this plan is the input to G3.*
