# Implementation Plan — Milestone 3: 仓位管理与复盘

**Project:** Order-Flow-Trading
**Milestone:** 3 — 仓位管理与复盘 (position management and review)
**Status:** plan complete for review; **no gate is approved by this document** (Milestone 3 G3 remains pending)
**Date:** 2026-11-14
**Inputs:** `.claude/prds/order-flow-trading.prd.md` (Milestone 3 row, outcomes, open questions, non-goals); `docs/adr/0001`–`0012`; `docs/contracts/` (especially `position`, `trade-log`, `llm-decision`, `order`, `fill`, `api-envelope`, `error-catalog`); `design/DESIGN.md`; `design/flows.md` (F07, F08, F09); `.claude/plans/order-flow-trading-m2.plan.md`; `.claude/ecc-workflow/slices/20-trigger-paper-ui/findings.md`; `.claude/ecc-workflow/slices/22-storage-refactor/findings.md`; the Milestone 2 source tree (`src/oft/`, `tests/`, `web/src/`); `CLAUDE.md`.
**Stack:** Milestone 2 stack unchanged — Python 3.12 modular monolith daemon (`oftd`), FastAPI, APScheduler, DuckDB/Parquet, SQLite WAL, Pydantic v2, Typer CLI, React 19 + TypeScript + Vite, Lightweight Charts.
**Boundary for this milestone:** exactly the PRD row — **自动仓位管理 + 完整交易日志 + 盈亏/胜率/复盘视图**, **paper only**. No live broker, no Binance credential, no Telegram, no paper reset / Live gate, no full settings UI, no strategy auto-tuning, no rule auto-modification.

---

## 1. Milestone outcome

A single local daemon that turns the Milestone 2 `OPEN` paper position into a complete, deterministic management and review loop:

1. **自动仓位管理**：每一条 open paper position 都由代码持续监控：止损/止盈触碰、过期标记、资金费结算、日亏损阈值和定时节奏会触发管理动作。代码先做确定性保护性退出；需要判断时再调用 `MANAGEMENT` LLM 给出 `HOLD | PARTIAL_CLOSE | FULL_CLOSE | ADD`，由代码校验、风控 clamp 并通过唯一 execution gateway 在 paper 中执行。LLM 不能下单、不能改风控、不能跳过校验。
2. **完整交易日志**：每次 partial/full close、add、funding accrual 都原子落库；full close 生成唯一 `trade-log` 记录，含 entry/exit、gross/net PnL、fees、funding、R multiple、MAE/MFE、signal refs、decision quality、data quality、provenance。所有金额是 `Decimal`/decimal string，时间 UTC，结果可重放。
3. **盈亏/胜率/复盘视图**：CLI `oft pos`、`oft log`，REST `/api/v1/positions`、`/api/v1/journal`，Web `/positions`、`/positions/:id`、`/journal`、`/journal/:tradeId`。视图明确展示 PnL/win-rate/R 的定义、样本量、排除项和 `PARTIAL` 状态；LLM 复盘只做定性总结，标注“仅建议，不自动修改规则”。

**End-to-end path delivered by the first slice:** an `OPEN` paper position is closed by a deterministic protective-exit rule, the close goes through the gateway to the paper broker, the position becomes `CLOSED`, and exactly one `trade-log` entry is visible through the API and CLI. Later slices add partial close/add, funding, LLM management, the scheduler, statistics, LLM review, and the Web views.

**Non-goals for Milestone 3:** Binance API key / testnet / live broker、live arming、paper reset 与 4-week Live gate、Telegram 推送/回执、F10 全量设置 Web UI、prompt/knowledge 编辑器 Web UI、图表 drawing tools/L2 DOM、新闻/社媒文本、策略参数优化/自动调参、多用户/账户体系、移动原生 App。

---

## 2. Binding decisions and contract constraints

| Decision | Plan implication |
|---|---|
| ADR-0006: one execution gateway | Every management order (protective exit, partial close, full close, add) goes through `application/execution_gateway.py`; no service calls `PaperBroker` directly. Every order keeps a persisted `risk_verdict_id`, deterministic `client_order_id`, idempotency key, and audit event. |
| ADR-0006: deterministic risk gate | Code recomputes caps for `ADD`; exits are reduce-only. `effective_size = min(LLM proposal, per-trade, notional, leverage, exchange max)` still applies. The verdict records requested vs allowed, the binding cap, stale inputs, and an inputs hash. |
| ADR-0006: kill switch | The gateway classifies every management intent as **risk-increasing** (`ADD`, stop loosening/removal) or **risk-reducing** (`PARTIAL_CLOSE`, `FULL_CLOSE`, stop tightening, cancel). Killing blocks risk-increasing management and all new entries; risk-reducing exits remain allowed and audited (ADR-0006). Killing never auto-closes positions. |
| ADR-0006: paper broker and accounting | The paper broker gains a reduce/add path that applies a fill to an existing position, adjusts cash by realized proceeds minus fees, and never creates a second position for an exit. Defaults stay 10,000 USDT, 2 bps slippage, 5 bps taker, 2 bps maker. Funding accrues at the **recorded** settlement times/rates (default observed 8h schedule; never assume a fixed interval when the data says otherwise). |
| ADR-0006: idempotency | Deterministic `client_order_id` binds `{env, symbol, purpose, decision/action id, intent hash}`; `purpose` is `EXIT`, `PARTIAL_CLOSE`, `FULL_CLOSE`, or `ADD`. Duplicate management requests return the original result; a different payload with the same idempotency key is `IDEMPOTENCY_CONFLICT`. |
| ADR-0006: audit | Append-only hash-chained events for `POSITION_OPENED`, `POSITION_MARKED`, `MANAGEMENT_DECIDED`, `STOP_UPDATED`, `PARTIAL_CLOSE`, `FULL_CLOSE`, `ADD`, `FUNDING_ACCRUED`, `TRADE_LOGGED`, `REVIEW_GENERATED`. `oft doctor` verifies the chain. |
| ADR-0007: durable job ledger | Reuse the job ledger and lease pattern for `position_monitor` (15 s default, configurable, single instance, no catch-up) and for the management cadence (aligned to the scan period). Restart reconstructs from SQLite; overlapping ticks lose the claim and write nothing. |
| ADR-0009: prompt and knowledge versioning | `MANAGEMENT` and `REVIEW` become seeded prompt roles with immutable versions, required variables, hashes, and activation. Every management/review decision records `prompt_id`, `prompt_version`, `prompt_hash`, `knowledge_artifact_hash`, provider/model, usage, and cost. |
| ADR-0010: local security | Loopback bind unchanged; browser mutations use session + CSRF + Origin/Host checks; management mutations require an idempotency key. Position, market, knowledge, and review text are **data, not instructions**. No secret, raw prompt, or provider key is rendered in the journal/positions UI, CLI, API, or export. |
| ADR-0011: deterministic testing | Injected `ClockPort`; no `datetime.now()`/`time.time()` in `domain`/`application`; committed fixtures; stub/recorded LLM outputs; same domain code for live/replay; 80% coverage floor; **100% branch on `domain.risk`, the OMS/position state machine, `domain.pnl`, `domain.management`, and `domain.journal`**. |
| ADR-0012: observability | `structlog` JSON with redaction; management/review job status visible in `/system`, `oft pos`, `oft log`, and logs; no telemetry. |
| Contracts | Positions validate `position.schema.json`; closed trades validate `trade-log.schema.json`; management/review decisions validate `llm-decision.schema.json`; orders/fills validate their schemas; every API response uses `api-envelope.schema.json`; every error code is in `error-catalog.json` (and the envelope enum). |
| PnL definitions (code-owned) | gross PnL = `(exit − entry) × size` for LONG and `(entry − exit) × size` for SHORT; funding is signed (`LONG` pays a positive rate); `net = gross − fees + funding`; `R = net / frozen initial risk`; `return_pct = net / entry notional × 100`; win rate = closed eligible trades with `net > 0` ÷ eligible closed trades (ties are not wins; open/`PARTIAL` excluded and counted). |

### 2.1 Existing Milestone 2 foundation and compatibility

Milestone 3 extends, not rewrites:

- `src/oft/domain/oms.py` currently creates only `OPEN` positions (`Position.__post_init__` rejects any other state) and has no close/reduce/add transition. Milestone 3 adds state-dependent validation for `OPEN → CLOSED` and position mutations; `Order` states remain unchanged.
- `src/oft/application/paper_broker.py::submit()` only opens a new position and always inserts a `positions` row. Milestone 3 adds a reduce/add path that updates the existing position in the same transaction and never creates a duplicate position for an exit.
- `src/oft/application/execution_gateway.py` currently serves the entry accept path. Milestone 3 extends it with `purpose=EXIT|PARTIAL_CLOSE|FULL_CLOSE|ADD`, risk-reducing classification, and the kill-switch policy; the entry path and its idempotency behavior stay intact.
- `src/oft/application/paper_account.py` computes equity from cash + marked positions and reports `unrealized_at_last_mark`; it does not track realized PnL. Milestone 3 adds realized PnL, fees, and funding to the account summary without changing the cash-only fallback for missing marks.
- `src/oft/adapters/sqlite_paper_oms.py` owns orders/fills/positions/accounts. Milestone 3 adds atomic close/partial/add/funding/trade-log writes; `sqlite_store.py` remains the facade and migrations stay in `sqlite_schema.py` (current schema **v21**).
- `src/oft/application/jobs.py` hosts the 15 s `trigger_evaluate` job with the durable ledger. Milestone 3 adds `position_monitor` and the management cadence; the trigger job is unchanged.
- `src/oft/domain/prompts.py` already defines built-in `MANAGEMENT` and `REVIEW` bodies but `SEEDED_ROLES` contains only `ANALYSIS` and `ENTRY_RECHECK`. Milestone 3 seeds `MANAGEMENT` v1 (slice 25) and `REVIEW` v1 (slice 28), with provenance and no silent fallback.
- `docs/contracts/trade-log.schema.json` exists but is unused. Milestone 3 activates it; closed trades validate it on every write and read.
- `src/oft/application/entry_service.py` calls the LLM on every recheck. Milestone 3 adds a persisted-entry-decision read and reuses it by default; `--refresh` is explicit.
- `web/src/routes/route-match.ts` has no positions/journal routes. Milestone 3 adds `/positions`, `/positions/:id`, `/journal`, `/journal/:tradeId`; the existing paper/trigger/opportunity routes are unchanged.
- The Milestone 2 entry path (human paper accept, risk verdict, gateway, paper broker) stays behaviorally unchanged. New SQLite tables/columns are additive and forward-only; old positions, orders, fills, reports, and opportunities remain readable.

### 2.2 Scope assumptions (narrow reading)

These are recorded because the PRD Milestone 3 row is a summary, not a field-level specification. If G3 disagrees, adjust the plan before implementation.

1. **Paper management executes automatically after code validation.** Design F07 permits `oft pos manage` to execute without an extra prompt in paper; PRD MVP 8 says code validates and executes. No human accept is added to the paper management path. Live management confirmation remains Milestone 6.
2. **Kill-switch classification follows ADR-0006.** Risk-reducing actions (partial/full close, stop tightening, cancel) remain allowed and audited; risk-increasing actions (ADD, stop loosening/removal) are blocked. The design summary “blocks all LLM-driven management execution” is read as “blocks risk-increasing LLM-driven management”; activation never auto-closes positions.
3. **One trade log per closed position.** Partial closes update the open position and accumulate realized PnL/fees; the `trade-log` row is created exactly once on `FULL_CLOSE` (or a future liquidation). A new entry after a full close creates a new position and a new trade log.
4. **Funding uses recorded data.** Accrual happens at the funding settlement times/rates recorded by the metrics pipeline. A missing rate or mark makes the trade `PARTIAL`; the code never fabricates a funding amount or a zero.
5. **R multiple uses frozen initial risk.** Initial risk is the stop-based risk captured at entry plus the incremental stop-based risk of each `ADD`, frozen at the time of the add. `R = net PnL / initial risk`; zero risk yields `null` (an entry without a stop is blocked, so this is a defensive case).
6. **MAE/MFE use recorded bars.** They are computed from the recorded bars covering the position window. If stop and target are both inside one bar, the code assumes **stop first** (conservative) and records an `AMBIGUOUS_BAR` quality reason; it never claims an intrabar sequence it cannot prove.
7. **Journal completeness is explicit.** A legacy/edge position without a source decision remains readable but is `PARTIAL` and excluded from statistics; the UI says why. The code never invents provenance.
8. **Review is advisory.** The `REVIEW` LLM output is stored and linked from the trade log, but there is no endpoint or control that applies a rule change, edits a prompt, or changes risk caps.
9. **Paper reset and the 4-week Live gate are not in the Milestone 3 PRD row.** The Milestone 2 plan assigned reset semantics to Milestone 3, but the authoritative PRD row covers management/journal/review only. Reset and the Live gate move to Milestone 6 (live arming) or a dedicated follow-up. No reset is implemented in this milestone.
10. **Positions/journal Web views are in scope.** The Milestone 3 outcome names “复盘视图”; the plan delivers the named F07/F09 screens. Full settings, prompt/knowledge editors, and advanced chart work remain Milestone 4.
11. **No new provider/model is shipped.** `MANAGEMENT` and `REVIEW` use the existing provider abstraction and per-role model configuration. Tests use `stub` or recorded responses; provider/model/pricing details are re-verified at implementation time.
12. **No Telegram delivery.** Management/review failures and budget thresholds remain local (ledger, API, CLI, Web); Telegram is Milestone 5.

---

## 3. Slice overview

Order is dependency-driven. Slice 23 is a genuine end-to-end path — `OPEN` position → deterministic protective exit → paper close → trade-log entry — before partial closes, funding, LLM management, statistics, review, or Web views. No slice may introduce a live broker, Binance credential, Telegram path, paper reset, or rule auto-modification.

| # | Slice | Depends on | UI | Security-sensitive | Sec review before merge | Size |
|---|---|---|---|---|---|---|
| 23 | `23-position-close-walking-skeleton` | — | no | yes | **yes** | L |
| 24 | `24-pnl-funding-trade-log` | 23 | no | yes | **yes** | L |
| 25 | `25-management-llm-gateway` | 24 | no | yes | **yes** | L |
| 26 | `26-position-monitor-scheduler` | 25 | no | yes | **yes** | M |
| 27 | `27-journal-stats-read-models` | 23, 24, 25 | no | yes | **yes** | L |
| 28 | `28-llm-trade-review` | 27 | no | yes | **yes** | M |
| 29 | `29-positions-ui` | 23, 24, 25, 26 | yes | yes | **yes** | L |
| 30 | `30-journal-review-ui` | 27, 28 | yes | yes | **yes** | L |
| 31 | `31-m3-replay-hardening` | 23–30 | no | no | no | M |

**Security-review rule:** slices 23, 24, 25, 26, 27, 28, 29, and 30 must not merge without a security review. Scope: the OMS/position mutation path, risk gate and kill-switch classification, idempotency, LLM management/review boundary (untrusted text, schema rejection, no execution imports), provenance exposure, CSV formula injection, and the Web mutation/XSS/CSP surface. This is a review requirement only; it is not a gate approval.

**Size and PR rule:** every slice is **one issue and exactly one PR**. Do not split a slice across more than one PR. If a slice grows beyond its scope fence, reduce the scope to the fence or defer the extra work to a follow-up; do not widen the PR.

---

## 4. Detailed slices

### 23 — `23-position-close-walking-skeleton`

**Title:** Position close walking skeleton: deterministic protective exit to a trade-log entry

**Goal:** Prove the full management path on one existing `OPEN` paper position with one code-only rule: when the recorded mark touches the stop, submit one reduce-only `FULL_CLOSE` through the existing execution gateway to the paper broker, close the position, update the account, and create exactly one `trade-log` row. This establishes the position lifecycle, the close path, the trade-log storage/read model, and the CLI/API surfaces that later slices complete.

**Scope fence for this slice:** one open position; one code rule (stop touch); `FULL_CLOSE` only; manual `oft pos monitor` tick; no target/partial/add; no LLM; no funding; no statistics; no Web UI; no scheduler; no paper reset. PnL is limited to entry/exit price and fees; funding is `0` with an explicit reason (funding accrual arrives in slice 24).

**Files expected**
- `src/oft/domain/pnl.py` — new: decimal money helpers, gross/net PnL for a full close, fee totals, deterministic 8-dp quantization; pure functions.
- `src/oft/domain/positions.py` — new: `evaluate_protective_exit(position, mark, *, at)`, `close_full(position, exit_fill, *, at)`, closed-position payload, stop-side validation, action/event IDs.
- `src/oft/domain/oms.py` — extend position validation to allow `OPEN → CLOSED` with `closed_at`, `exit_price`, `realized_pnl_usd`, and accumulated fees; order states unchanged.
- `src/oft/application/position_service.py` — new: list/show, mark lookup, protective rule evaluation, close intent, gateway call, trade-log creation.
- `src/oft/application/execution_gateway.py` — accept `purpose=EXIT`, reduce-only intents; risk-reducing classification; kill-switch policy; risk-verdict freshness; idempotency.
- `src/oft/application/paper_broker.py` — add a reduce path that applies a fill to an existing position and changes cash by proceeds minus fee; never creates a second position.
- `src/oft/application/paper_account.py` — include realized PnL/fees in the summary; preserve the cash-only fallback for a missing mark.
- `src/oft/adapters/sqlite_paper_oms.py` — atomic `save_close` (account + order + fill + position update + position event + trade log + audit); no partial transaction.
- `src/oft/adapters/sqlite_schema.py` — migration v22 (actual number recorded by the slice): `trade_logs` (unique `position_id`), `position_events` (append-only timeline), indexes on `closed_at`/`symbol`.
- `src/oft/ports/store.py` — `TradeLogRecord`, `PositionEventRecord`, `ClosedPositionRecord`, and methods.
- `src/oft/api/routes/positions.py`, `src/oft/api/schemas/position.py` — list/show.
- `src/oft/api/routes/journal.py`, `src/oft/api/schemas/journal.py` — list/show (minimal projection).
- `src/oft/cli/commands/pos.py` — `list|show|monitor`; `src/oft/cli/commands/log.py` — `list|show`; `src/oft/cli/main.py` — register.
- `tests/unit/test_pnl.py`, `tests/unit/test_position_lifecycle.py`.
- `tests/integration/test_position_close_flow.py`, `tests/integration/test_position_close_idempotency.py`.
- `tests/contract/test_trade_log_contract.py`.
- `tests/replay/test_position_close_replay.py` (initial deterministic check).
- `tests/e2e/test_position_journal_cli.py`.

**Tests expected**
- An `OPEN` LONG with a stop and a recorded mark at/below the stop produces one `FULL_CLOSE` intent; a mark above the stop produces none.
- A SHORT stop touch is symmetric (`mark >= stop`).
- The close creates exactly one order, one fill, one `CLOSED` position update, one trade-log row, and one `POSITION_CLOSED`/`TRADE_LOGGED` audit pair; no second position row is created.
- Net PnL equals gross minus entry/exit fees to 8 dp; the trade log validates `trade-log.schema.json`; funding is `0` and the reason is recorded as “funding accrual not in this slice” rather than silently omitted.
- Re-running with the same fixed clock/inputs creates nothing new and returns the original result; a duplicate `client_order_id`/idempotency key is enforced by SQLite.
- A stale mark (older than `risk_stale_mark_seconds`) produces no order and a visible `DATA_STALE` reason; no fabricated fill.
- The gateway rejects the close if the risk verdict is missing/expired; the broker is never called directly.
- Kill switch on blocks the new-order path but permits the reduce-only close (classification test) and audits the classification.
- `oft pos list|show` and `oft log list|show` show the same values as the API; all envelopes validate.
- No Web UI, no LLM call, no live path, no funding/statistics/reset in this slice.

**UI:** false. **Security:** true (position mutation, gateway, kill switch, idempotency, audit). **Security review required:** yes.

**Acceptance criteria**
- [ ] Given an `OPEN` paper position with a stop, `oft pos monitor --position <id>` creates exactly one paper `FULL_CLOSE` order and fill, sets the position `CLOSED`, and creates exactly one trade-log row.
- [ ] The trade log validates `trade-log.schema.json`; `net_pnl_usd = gross_pnl_usd − fees_usd + funding_usd` holds to 8 dp with `funding_usd = "0"` in this slice.
- [ ] No new position row is created for the exit; the existing position payload carries `closed_at`, `exit_price`, and `realized_pnl_usd`.
- [ ] A mark that does not touch the stop creates no order; a stale mark creates no order and a visible reason.
- [ ] The close goes through `ExecutionGateway` and persists a risk verdict id; no direct broker call exists.
- [ ] Duplicate evaluation/close is idempotent; a different payload with the same key is `IDEMPOTENCY_CONFLICT`.
- [ ] Kill-switch on allows the risk-reducing close and blocks a risk-increasing test intent; the audit records the classification.
- [ ] `oft pos list|show` and `oft log list|show` render the position/trade; API responses use the envelope.
- [ ] No LLM, Web UI, funding, statistics, paper reset, or live path is introduced.

**Preview screens**
- S07.3 `oft pos list|show` (terminal equivalents; Web S07.1/S07.2 in slice 29).
- S09.3 `oft log list|show` (terminal; Web S09.1/S09.2 in slice 30).
- S08.2 `oft paper equity` updated with realized PnL.
- No Web UI in this slice.

**Dependencies:** none (builds on Milestone 2). **Size:** L.

---

### 24 — `24-pnl-funding-trade-log`

**Title:** Partial close, add accounting, funding accrual, and the complete trade log

**Goal:** Complete the deterministic accounting model behind the journal: partial closes and adds update the open position; full close finalizes exactly one `trade-log` row; funding accrues at recorded settlement times; MAE/MFE, R multiple, return, outcome tags, and data quality are computed by code. No LLM and no scheduler yet.

**Scope fence for this slice:** PnL/funding/accounting and trade-log completeness only. Stop/target partial rules may be triggered manually; LLM management is slice 25; scheduling is slice 26; statistics aggregation is slice 27; Web UI is slices 29–30.

**Files expected**
- `src/oft/domain/pnl.py` — extend: partial-close realized PnL, add weighted-average entry, funding accrual math, MAE/MFE from bars, R multiple, return percent, outcome tags.
- `src/oft/domain/positions.py` — extend: `partial_close`, `apply_add`, target/protective rule evaluation, weighted average entry, accumulated realized PnL/fees/funding, stop/target update validation.
- `src/oft/application/position_service.py` — target/partial rule, funding accrual at recorded settlement times, bar-backed MAE/MFE, trade-log finalization, history read model.
- `src/oft/application/paper_broker.py` — reduce and add fills applied to an existing position; cash deltas for partial/full/add; no duplicate position.
- `src/oft/adapters/sqlite_paper_oms.py` — atomic partial-close/add/funding/trade-log writes; funding idempotency constraint.
- `src/oft/adapters/sqlite_schema.py` — migration v23: `funding_accruals` (unique `position_id + settlement_time`), trade-log indexes/columns as needed, any position-event indexes.
- `src/oft/ports/store.py` — funding/position-event/trade-log records and methods.
- `src/oft/api/routes/positions.py`, `journal.py` — PnL/funding breakdown fields.
- `src/oft/cli/commands/pos.py` — `history`; `log.py` — `show` with the breakdown.
- `tests/unit/test_pnl.py` (extend), `tests/unit/test_position_partial_close.py`, `tests/unit/test_funding_accrual.py`, `tests/unit/test_pnl_first_touch.py`.
- `tests/integration/test_partial_close_flow.py`, `tests/integration/test_funding_idempotency.py`, `tests/integration/test_trade_log_persistence.py`.
- `tests/contract/test_trade_log_contract.py` (extend).

**Tests expected**
- A 50% partial close reduces size by exactly half (step-normalized), realizes half the price PnL and fees, keeps the position `OPEN`, and does not create a trade log; a later full close creates exactly one trade log with the cumulative realized PnL.
- An `ADD` updates size and weighted-average entry deterministically; the frozen initial risk increases by the add’s stop-based risk; the position remains `OPEN`; no trade log yet.
- Funding accrues for LONG when the recorded rate is positive (long pays) and for SHORT when positive (short receives); funding is idempotent across restart and replay; a missing rate/mark marks the trade `PARTIAL` and never accrues a fabricated amount.
- MAE/MFE use recorded bars and the documented sign convention (MAE ≤ 0, MFE ≥ 0); a bar that contains both stop and target yields `AMBIGUOUS_BAR` and assumes stop first.
- `R = net / frozen initial risk`; a zero/missing risk yields `null`; `return_pct = net / entry notional × 100`; all are decimal strings or numbers as the contract requires.
- The trade log records `decision_quality` (matched rules null unless reviewed), `outcome_tags` (`WIN|LOSS|BE|STOPPED|TARGET|PARTIAL|DATA_PARTIAL|MANUAL_CLOSE` as applicable), `data_quality`, and provenance copied from the entry decision; a missing decision/provenance yields `PARTIAL` and an explicit reason.
- Full close writes the trade log, position event, audit event, and account update in one transaction; a mid-transaction failure leaves none of them.
- No LLM, scheduler, statistics, or Web UI is introduced.

**UI:** false. **Security:** true (position/balance mutation, accounting, audit). **Security review required:** yes.

**Acceptance criteria**
- [ ] A partial close reduces size and accumulates realized PnL/fees; it does not create a trade log.
- [ ] A full close after partial closes creates exactly one trade-log row whose PnL equals the sum of realized slices minus fees plus funding.
- [ ] Funding accrual is idempotent at the recorded settlement times and respects LONG/SHORT sign; a missing rate is `PARTIAL`, not zero.
- [ ] MAE/MFE and the stop-first `AMBIGUOUS_BAR` convention are computed from recorded bars and covered by tests.
- [ ] R multiple uses the frozen initial risk; the trade log exposes `gross_pnl_usd`, `fees_usd`, `funding_usd`, `net_pnl_usd`, `r_multiple`, `mae_usd`, `mfe_usd`, `return_pct`, `outcome_tags`, `data_quality`, and `provenance`.
- [ ] The full close is atomic: account/order/fill/position/trade-log/audit either all commit or none do.
- [ ] `oft pos history` and `oft log show` expose the PnL/funding breakdown.
- [ ] No LLM, scheduler, statistics, or Web UI is introduced.

**Preview screens**
- S07.3 `oft pos show|history` (Web in slice 29).
- S09.3 `oft log show` with PnL/funding definitions (Web in slice 30).
- S08.2 `oft paper equity` with realized/unrealized values.

**Dependencies:** 23. **Size:** L.

---

### 25 — `25-management-llm-gateway`

**Title:** LLM position-management advice, deterministic validation, and paper execution

**Goal:** For an `OPEN` position, obtain a schema-validated `MANAGEMENT` decision (`HOLD | PARTIAL_CLOSE | FULL_CLOSE | ADD`), validate it in code against position state, stop side, fraction, and risk caps, then execute it through the single gateway in paper. `HOLD` writes no order; a blocked action writes no order and records the binding cap. The LLM never supplies order IDs, risk caps, or execution authority.

**Scope fence for this slice:** management decision + manual execution path. No scheduler (`position_monitor`/cadence is slice 26); no review (slice 28); no Web UI (slice 29); no live management; no rule changes.

**Files expected**
- `src/oft/application/management_service.py` — new: build the position payload and prompt, call the orchestrator with role `MANAGEMENT`, validate, risk-check, execute, persist the decision/action/events.
- `src/oft/domain/management.py` — new: semantic validation of the `position_management` payload (action/fraction/stop side/target order/urgency, `HOLD` with no changes, `PARTIAL_CLOSE` fraction range, `ADD` requirements).
- `src/oft/domain/risk.py` — management intent classification (risk-increasing/reducing), `ADD` caps, exit allowance, daily-loss behavior; property tests.
- `src/oft/application/execution_gateway.py` — `purpose=PARTIAL_CLOSE|FULL_CLOSE|ADD`, classification, kill-switch policy, risk-verdict freshness, idempotency, audit.
- `src/oft/application/paper_broker.py` — management fills applied to an existing position (partial/full/add).
- `src/oft/application/llm_orchestrator.py` — dispatch `MANAGEMENT`; one repair; rejection alert; usage/cost.
- `src/oft/domain/prompts.py` — add `MANAGEMENT` to `SEEDED_ROLES`; keep the built-in body.
- `src/oft/application/prompt_service.py` — seed/lookup `MANAGEMENT` v1; required variables `symbol`, `timeframe`, `position`, `schema`, `knowledge`.
- `src/oft/adapters/sqlite_schema.py` — migration v24 seeds `MANAGEMENT` v1 (idempotent) and any management-event indexes.
- `src/oft/application/position_service.py` — persist management history, stop/target updates.
- `src/oft/adapters/sqlite_paper_oms.py` — atomic management action + fill + position update + trade log (on full close).
- `src/oft/api/routes/positions.py` — `POST /api/v1/positions/{position_id}/manage` with idempotency key.
- `src/oft/cli/commands/pos.py` — `manage`.
- `tests/unit/test_management_validation.py`, `tests/unit/test_risk_management.py`, `tests/unit/test_management_actions.py`.
- `tests/integration/test_management_flow.py`, `tests/integration/test_management_idempotency.py`, `tests/integration/test_kill_switch_management.py`, `tests/integration/test_management_rejection.py`.
- `tests/contract/test_management_decision_contract.py`.
- `tests/replay/test_management_replay.py` (initial).

**Tests expected**
- `HOLD` creates no order and persists a management action/event with the LLM decision provenance.
- `PARTIAL_CLOSE` creates one reduce-only order, reduces size, realizes PnL, and keeps the position `OPEN`; a remainder below min notional/step escalates to `FULL_CLOSE` and records the reason.
- `FULL_CLOSE` closes the position and creates exactly one trade log.
- `ADD` is allowed only when per-trade, notional, leverage, max-positions, and daily-loss checks pass; otherwise no order is created and the verdict names the binding cap.
- Code recomputes all numbers; a larger LLM proposal is clamped to the stricter cap; the requested/allowed/binding-cap values are persisted.
- A refused/truncated/schema-invalid management output creates no order, shows the failing field/reason, and performs at most one repair.
- Stop updates: a new stop on the correct side and risk-reducing direction is applied; a stop removal/loosening is risk-increasing and blocked by the kill switch; invalid stop side is rejected.
- Kill switch: `PARTIAL_CLOSE`/`FULL_CLOSE`/stop-tightening remain allowed and audited; `ADD`/stop-loosening are blocked with `KILL_SWITCH_ACTIVE`.
- Duplicate manage requests with the same idempotency key create no second order/fill; a different payload is `IDEMPOTENCY_CONFLICT`.
- Every order has a risk verdict, deterministic `client_order_id`, `purpose`, and audit event; no direct broker call and no live path.
- Management decision provenance includes provider/model/prompt version/hash/knowledge hash/cost; no secret or raw prompt is stored/returned.

**UI:** false (CLI/API; Web S07 in slice 29). **Security:** true (LLM boundary, OMS, risk gate, kill switch, idempotency). **Security review required:** yes.

**Acceptance criteria**
- [ ] `oft pos manage <position_id>` produces a validated `HOLD|PARTIAL_CLOSE|FULL_CLOSE|ADD` decision; `HOLD` and every blocked action create no order.
- [ ] `PARTIAL_CLOSE`/`FULL_CLOSE`/`ADD` execute in paper only through the gateway; the resulting order/fill/position/trade-log values match the API.
- [ ] The risk gate recomputes caps for `ADD` and records requested vs allowed and the binding cap; the LLM cannot exceed a cap.
- [ ] Invalid/refused/truncated output creates no order, names the failure, and performs one repair at most.
- [ ] Kill-switch classification allows risk-reducing actions and blocks risk-increasing ones; the classification is audited.
- [ ] Duplicate manage is idempotent; a conflicting payload returns `IDEMPOTENCY_CONFLICT`.
- [ ] Every management decision records provider/model/prompt version/knowledge hash/cost; no secret/raw prompt is exposed.
- [ ] No scheduler, review, Web UI, live path, paper reset, or rule-change action is introduced.

**Preview screens**
- S07.3 `oft pos manage <id>` (LLM proposal → CALC validation → executed result).
- S06.2-style decision-card content for management in API/CLI (Web rendering in slice 29).

**Dependencies:** 24. **Size:** L.

---

### 26 — `26-position-monitor-scheduler`

**Title:** Position monitor and management cadence: automatic paper management with no catch-up

**Goal:** Run the management loop automatically: a 15 s `position_monitor` tick evaluates every open position for protective exits and stale marks with code only; the management cadence (aligned to the scan period) asks the `MANAGEMENT` LLM once per position per window and executes the validated result. The durable job ledger guarantees single-instance execution, overlap rejection, restart reconstruction, and no catch-up.

**Scope fence for this slice:** scheduling, due/condition logic, job ledger wiring, and settings. No new LLM semantics, no new PnL math, no Web UI, no paper reset.

**Files expected**
- `src/oft/application/jobs.py` — `PositionMonitorRunner` (protective/stale tick) and `ManagementJobRunner` (cadence), reusing `JobLedger.claim`/`finish`; new job types `position_monitor` and `position_manage`.
- `src/oft/application/scheduler.py` — register the interval/cadence jobs; no catch-up.
- `src/oft/cli/commands/daemon.py` — start the new schedulers.
- `src/oft/config.py` — `position_monitor_interval_seconds` (default 15, bounds 5–60), `management_cadence` (scan period default; explicit override), management condition toggles (`FUNDING_SETTLEMENT`, `RISK_BREACH`), no secrets.
- `src/oft/application/management_service.py` — due/condition evaluation: one decision per `(position_id, window_start)`; funding settlement; daily-loss/risk breach.
- `src/oft/application/position_service.py` — protective exit and stale-mark handling run without an LLM.
- `src/oft/adapters/sqlite_schema.py` — any job-ledger/index changes needed; otherwise no migration.
- `src/oft/api/routes/system.py` — job status if the existing status payload needs the new job names.
- `tests/unit/test_schedule_alignment.py` (extend), `tests/unit/test_management_due.py`.
- `tests/integration/test_position_monitor_job.py`, `tests/integration/test_management_job_cadence.py`, `tests/integration/test_management_job_no_catchup.py`, `tests/integration/test_protective_exit_without_llm.py`, `tests/integration/test_management_budget.py`.

**Tests expected**
- A stop-hit position closes on the next `position_monitor` tick even when no LLM provider is configured; the reason is code (`PROTECTIVE_STOP`).
- The `MANAGEMENT` LLM is called at most once per position per cadence window; a second tick in the same window is a no-op; a restart does not duplicate the decision.
- Overlapping ticks lose the ledger claim and write nothing; missed windows are not replayed (no catch-up).
- `FUNDING_SETTLEMENT` and `RISK_BREACH` conditions can trigger a management decision once per applicable window; no position means no call.
- The global budget ledger records each management call; a threshold crossing alerts locally and does not hard-stop.
- Kill switch blocks risk-increasing automated management; risk-reducing protective exits remain allowed.
- All scheduling uses `ClockPort`/UTC and fixed-clock tests; no wall clock.
- No Web UI, reset, or live path is introduced.

**UI:** false (job status only). **Security:** true (automated OMS/risk/kill-switch path). **Security review required:** yes.

**Acceptance criteria**
- [ ] A stop-hit open position is closed by the 15 s monitor without any LLM call; a stale mark produces no action and a visible reason.
- [ ] The management cadence calls the LLM at most once per position per window and never replays missed windows.
- [ ] Restart reconstructs open positions and the next due window from SQLite; no duplicate decision/order.
- [ ] Funding-settlement and risk-breach conditions can request a management decision once per applicable window.
- [ ] Budget/usage rows are written for every management call; no hard stop and no Telegram delivery.
- [ ] Kill-switch classification and the code-only protective path are tested.
- [ ] No Web UI, paper reset, or live path is introduced.

**Preview screens**
- None directly; the job outcomes appear in `/system` and in `oft pos history`/`oft log`.

**Dependencies:** 25. **Size:** M.

---

### 27 — `27-journal-stats-read-models`

**Title:** Positions, journal, statistics, comparison/export, and the persisted entry-decision read

**Goal:** Expose the complete read model: positions list/detail/history; journal list/detail with the signal-vs-outcome comparison; deterministic PnL/win-rate/expectancy/avg-R statistics with explicit definitions and exclusions; CSV/JSON export with formula-injection neutralization; and the deferred slice-20 backend reads (persisted entry decision with provenance, trigger-list provenance fields).

**Scope fence for this slice:** read models, aggregation, CLI/API, and the persisted entry-decision read. No new execution, no LLM review, no Web UI, no paper reset.

**Files expected**
- `src/oft/domain/journal.py` — new: eligible-trade filter, win rate, expectancy, avg R, sample size, period boundaries, exclusion counts, comparison math (zone vs fill, trigger→fill slippage, first-touch, decision-vs-outcome).
- `src/oft/application/journal_service.py` — new: list/detail/stats/compare/export; loads closed trade logs and computes in the domain; never SQL-aggregates money.
- `src/oft/application/position_service.py` — list/detail/history read models.
- `src/oft/api/routes/positions.py`, `journal.py` — list/detail/stats/compare/export.
- `src/oft/api/routes/triggers.py` — add `environment`, `risk_verdict`, `confidence` to the list projection; add `GET /api/v1/triggers/{trigger_id}/decision` returning the persisted entry decision with provenance and no provider call.
- `src/oft/application/entry_service.py` — `get_persisted_entry_decision(trigger_id)`; `recheck` reuses it by default and only calls the provider with an explicit refresh; the recheck envelope returns the decision's own provenance.
- `src/oft/cli/commands/pos.py` — `list|show|history`; `log.py` — `list|show|stats|compare|export`; `trigger.py` — `show --decision`.
- `src/oft/api/schemas/position.py`, `journal.py`; `docs/contracts/README.md` note for the trade-log read model if needed.
- `tests/unit/test_journal_stats.py`, `tests/unit/test_journal_comparison.py`.
- `tests/integration/test_trade_journal.py`, `tests/integration/test_entry_decision_read.py`, `tests/integration/test_trigger_list_provenance.py`.
- `tests/contract/test_trade_log_contract.py` (extend), `tests/contract/test_trigger_list_contract.py`.
- `tests/e2e/test_journal_cli.py`.

**Tests expected**
- `stats` with no closed trades prints “metrics undefined” and a reason, never zeros.
- Win rate = `net > 0` ÷ eligible closed trades; ties are not wins; open trades and `PARTIAL` trades are excluded and counted; the denominator and sample size are shown.
- Expectancy = mean net PnL over eligible trades; avg R = mean R over eligible trades with non-null R; excluded null-R trades are counted.
- Period filters use UTC boundaries and `ClockPort`; `--period 8w` is deterministic.
- `compare` shows proposed zone vs actual entry, trigger→fill slippage in bps, whether stop or target was hit first (with `AMBIGUOUS_BAR`), and whether the entry decision matched the outcome (`N/A` when the signal snapshot is missing).
- `export --format csv|json` writes a file path; CSV values starting with `=`, `+`, `-`, or `@` are prefixed with `'`; no secret/raw prompt is exported.
- `GET /api/v1/triggers` includes `environment`, `risk_verdict`, and `confidence`; `GET /api/v1/triggers/{id}/decision` returns the persisted entry decision with provider/model/prompt/cost and makes **no** provider call.
- `oft trigger recheck` returns the persisted decision by default and calls the provider only with `--refresh`; the card shows the entry decision’s own provenance (absorbs the slice-20 finding).
- Position history/detail and journal detail share the same trade-log values; all responses validate the envelope/contracts.
- No Web UI, LLM review, or execution mutation is introduced.

**UI:** false (CLI/API; Web in slices 29–30). **Security:** true (provenance exposure, CSV formula injection, export of local trading data). **Security review required:** yes.

**Acceptance criteria**
- [ ] `oft log stats --period 8w` prints the PnL/win-rate/R definitions, sample size, excluded counts, and “metrics undefined” when no eligible trades exist.
- [ ] Win rate, expectancy, and avg R match the documented formulas and exclude open/`PARTIAL` trades.
- [ ] `oft log compare <trade_id>` shows signal vs outcome and marks unavailable sides `N/A`/`PARTIAL`.
- [ ] `oft log export --format csv|json` neutralizes spreadsheet formulas and writes a path.
- [ ] `GET /api/v1/triggers` includes environment/risk verdict/confidence; `GET /api/v1/triggers/{id}/decision` returns the persisted decision with provenance and no provider call.
- [ ] `oft trigger recheck` reuses the persisted decision by default; `--refresh` is explicit and audited.
- [ ] All new reads validate their contracts/envelope; no secret/raw prompt is exposed.
- [ ] No LLM review, Web UI, execution mutation, or paper reset is introduced.

**Preview screens**
- S07.3 `oft pos list|show|history`.
- S09.3 `oft log list|show|stats|compare|export`.
- S06.3 trigger list provenance fields.

**Dependencies:** 23, 24, 25. **Size:** L.

---

### 28 — `28-llm-trade-review`

**Title:** Advisory LLM trade review with an additive `REVIEW` decision contract

**Goal:** For a closed trade, call the `REVIEW` role with the trade log, fills, management history, and knowledge; store a schema-validated advisory review; link it from the trade log. The review can summarize and suggest, but it cannot change a rule, prompt, risk cap, or order. Add `REVIEW` to the decision-type enum additively so the existing canonical `llm-output`/`llm-decision` shape still works.

**Scope fence for this slice:** review decision + contract amendment + prompt seeding. No review UI (slice 30); no rule application; no management changes; no paper reset.

**Files expected**
- `docs/contracts/llm-output.schema.json` — add `REVIEW` to `decision_type` (additive enum value); keep the existing required fields; no new executable field.
- `docs/contracts/llm-decision.schema.json` — add `REVIEW` to `decision_type`; role `REVIEW` already exists.
- `docs/contracts/README.md` — document that a review uses `rationale`/`caveats` and `subject.position_id`, is advisory, and cannot apply changes.
- `src/oft/domain/llm_output.py` — REVIEW semantic checks (no payload requirement beyond the canonical nulls; rationale required; confidence range).
- `src/oft/application/llm_validation.py` — decision-type mapping for REVIEW; no relaxation of the canonical schema.
- `src/oft/application/review_service.py` — new: build the review context from the trade log, call the orchestrator with role `REVIEW`, store the decision, update `trade_logs.review_decision_id`.
- `src/oft/domain/prompts.py` — add `REVIEW` to `SEEDED_ROLES`; keep the built-in body.
- `src/oft/application/prompt_service.py` — seed/lookup `REVIEW` v1; required variables `symbol`, `timeframe`, `position`, `schema`, `knowledge`.
- `src/oft/adapters/sqlite_schema.py` — migration v25/v26 seeds `REVIEW` v1 and any review index.
- `src/oft/api/routes/journal.py` — `POST /api/v1/journal/{trade_id}/review` (idempotent, `--refresh` equivalent) and `GET`.
- `src/oft/cli/commands/log.py` — `review <trade_id>`.
- `src/oft/adapters/sqlite_llm_ledger.py` — no shape change; decisions/budget rows reuse the existing tables.
- `tests/unit/test_review_semantics.py`.
- `tests/integration/test_review_flow.py`, `tests/integration/test_review_idempotency.py`, `tests/integration/test_review_failure_keeps_metrics.py`.
- `tests/contract/test_review_contract.py`.
- `tests/replay/test_review_replay.py` (initial).

**Tests expected**
- A valid review fixture creates one decision with `role=REVIEW`, `decision_type=REVIEW`, provenance, usage/cost, and links `trade_logs.review_decision_id`; the trade metrics are unchanged.
- A refused/truncated/schema-invalid review creates no review text, leaves the trade/metrics intact, and exposes the failing reason with retry.
- The review is idempotent per trade; `--refresh` creates a new decision and updates the link while the old decision remains readable.
- The review output cannot create/modify an order, position, risk cap, prompt, or rule; static import checks prove `review_service`/LLM adapters cannot import execution.
- The review prompt contains only the trade data and knowledge as delimited data; market/knowledge text cannot change the schema or add actions.
- A missing REVIEW prompt version/artifact is explicit and disables exact replay; no silent fallback.
- No secret/raw prompt is stored/returned; the review is labelled advisory.
- No Web UI or rule-application endpoint is introduced.

**UI:** false (CLI/API; Web in slice 30). **Security:** true (LLM boundary, untrusted trade/knowledge text, advisory-only enforcement). **Security review required:** yes.

**Acceptance criteria**
- [ ] `oft log review <trade_id>` creates a persisted `REVIEW` decision with provenance and links it from the trade log.
- [ ] The review cannot create or change an order, position, risk cap, prompt, or rule; no such endpoint/import exists.
- [ ] Invalid/refused output leaves the trade metrics intact and shows a retryable reason; one repair at most.
- [ ] Review idempotency and `--refresh` behavior are tested; old reviews remain readable.
- [ ] The contract enum change is additive; existing provider fixtures still validate; new REVIEW fixtures validate.
- [ ] Missing prompt/knowledge versions are explicit and disable exact replay.
- [ ] No secret/raw prompt is exposed; the API/CLI label the review advisory.
- [ ] No review Web UI or rule application is introduced.

**Preview screens**
- S09.3 `oft log review <trade_id>` (Web S09.2 in slice 30).

**Dependencies:** 27. **Size:** M.

---

### 29 — `29-positions-ui`

**Title:** Web positions list, position detail, and management timeline

**Goal:** Add `/positions` and `/positions/:positionId` with open positions, exposure, risk utilization, uPnL/R, stops/targets, age, environment, PnL breakdown, management timeline, linked signal/report, stale/partial states, and a paper-only “Request management” action that shows the validated result. No live controls.

**Scope fence for this slice:** positions UI and the paper management request only. Journal/review UI is slice 30; settings/chart polish are Milestone 4; no live management.

**Files expected**
- `web/src/routes/positions.tsx`, `web/src/routes/position-detail.tsx`, `web/src/api/positions.ts`.
- `web/src/components/position-table.tsx`, `web/src/components/pnl-breakdown.tsx`, `web/src/components/management-timeline.tsx`; extend `web/src/components/risk-panel.tsx` and `kill-switch.tsx` if needed.
- `web/src/App.tsx`, `web/src/routes/route-match.ts`.
- `web/src/routes/__tests__/positions.test.tsx`, `web/src/components/__tests__/position-table.test.tsx`, `pnl-breakdown.test.tsx`.
- `tests/e2e/test_positions_view.py`, `tests/e2e/test_position_manage.py`.

**Tests expected**
- `/positions` shows symbol, side, size, entry, mark, uPnL (USDT + %), R, stop, target, leverage, margin, risk %, age, environment, and risk utilization; the empty state says there are no open positions and explains paper vs live.
- A stale mark renders `STALE`, disables management, and keeps the last values; partial data renders `PARTIAL`; no fabricated PnL.
- `/positions/:id` shows the PnL breakdown (price, fees, funding), stops/targets, risk caps applied at entry, the management timeline (LLM proposal → CALC validation → execution), the linked report/opportunity/trigger, and the chart with entry markers (existing chart component; no new chart features).
- “Request management” uses CSRF + idempotency, shows a confirmation with the consequence, renders the decision/result or the binding cap/kill-switch reason, and cannot create a second order on a duplicate click.
- 375px is read-only except the kill switch (consistent with Milestone 2); 768/1440 full; keyboard and axe checks pass.
- No raw HTML, no raw prompt, no provider key, no secret; no live controls; no management action while STALE/BLOCKED.
- No journal/review UI is introduced.

**UI:** true. **Security:** true (paper management mutation, CSRF/idempotency, XSS/CSP, kill switch). **Security review required:** yes.

**Acceptance criteria**
- [ ] `/positions` and `/positions/:id` render the documented columns, PnL breakdown, management timeline, and links at 1440/768/375.
- [ ] A stale mark shows `STALE` and disables management; partial data shows `PARTIAL`.
- [ ] A paper “Request management” action requires confirmation, is idempotent, and shows the executed result or the binding cap/kill-switch reason.
- [ ] 375px is read-only except the kill switch; keyboard and axe checks pass.
- [ ] No raw HTML/secret/raw prompt is rendered; no live path is reachable.
- [ ] No journal/review UI is introduced.

**Preview screens**
- S07.1 `/positions`.
- S07.2 `/positions/:positionId`.
- S07.3 CLI surfaces were delivered in slices 23–27.

**Dependencies:** 23, 24, 25, 26. **Size:** L.

---

### 30 — `30-journal-review-ui`

**Title:** Web trade journal, signal-vs-outcome comparison, and advisory review band

**Goal:** Add `/journal` and `/journal/:tradeId`: summary statistics with definitions, filters, the closed-trade table, the trade detail timeline, signal snapshot, entry decision, fills, comparison panel, and the advisory LLM review band with generate/retry. The review is visibly advisory and never offers a rule-change action.

**Scope fence for this slice:** journal UI only. No settings, no rule-consistency scoring editor, no paper reset, no live path.

**Files expected**
- `web/src/routes/journal.tsx`, `web/src/routes/journal-detail.tsx`, `web/src/api/journal.ts`.
- `web/src/components/journal-table.tsx`, `web/src/components/signal-comparison.tsx`, `web/src/components/review-band.tsx`; extend `provenance.tsx`/`data-quality.tsx`.
- `web/src/App.tsx`, `web/src/routes/route-match.ts`.
- `web/src/routes/__tests__/journal.test.tsx`, `web/src/components/__tests__/signal-comparison.test.tsx`, `review-band.test.tsx`.
- `tests/e2e/test_journal_view.py`, `tests/e2e/test_journal_review.py`.

**Tests expected**
- `/journal` shows net PnL, win rate, expectancy, avg R, sample size, and period with the definitions; filters by period/symbol/side/outcome; the empty state says no closed trades and does not show zeroed stats; open trades show `OPEN` and are excluded.
- `/journal/:tradeId` shows the timeline signal → trigger → decision → entry → management → exit, the signal snapshot, the entry decision, fills, the comparison panel, and the `LLM` review band labelled “仅建议，不自动修改规则”.
- Missing signal snapshot renders `N/A`; incomplete fills render `PARTIAL` with the reason; a review failure leaves metrics/comparison usable and shows a retry.
- Review generation uses the API with CSRF/idempotency, is idempotent, and re-renders the linked decision; no raw HTML.
- Export links the CSV/JSON file produced by the API; CSV formula neutralization is server-side.
- 375/768/1440, keyboard, axe; 375px read-only.
- No rule-change/apply action exists; no secret/raw prompt is rendered.

**UI:** true. **Security:** true (untrusted LLM review narrative, XSS/CSP, export). **Security review required:** yes.

**Acceptance criteria**
- [ ] `/journal` renders the stats strip, definitions, sample size, filters, and the closed-trade table; no closed trades shows an empty state, not zeros.
- [ ] `/journal/:tradeId` renders the timeline, signal snapshot, decision, fills, comparison, and advisory review band at 1440/768/375.
- [ ] Missing signal/fills/review states are explicit (`N/A`/`PARTIAL`/error+retry) and do not blank the page.
- [ ] Review generation is idempotent and advisory; there is no apply/rule-change action.
- [ ] Export links the server-generated file; CSV formula injection is neutralized.
- [ ] Keyboard and axe checks pass; 375px is read-only; no raw HTML/secret/raw prompt.
- [ ] No settings, reset, or live path is introduced.

**Preview screens**
- S09.1 `/journal`.
- S09.2 `/journal/:tradeId`.
- S09.3 CLI surfaces were delivered in slices 24, 27, and 28.

**Dependencies:** 27, 28. **Size:** L.

---

### 31 — `31-m3-replay-hardening`

**Title:** Replay, determinism, coverage, and the Milestone 3 runbook

**Goal:** Make the management/journal/review loop reproducible and operable: replay recorded bars, metrics, and stub/recorded MANAGEMENT/REVIEW outputs with the same domain code; fixed clock; replay namespace only; no live path; coverage gates; import-linter; audit-chain verification; and a runbook a human can follow from a clean data dir.

**Scope fence for this slice:** replay/testing/operations only. No new product behavior, no new provider/management/review semantics.

**Files expected**
- `src/oft/replay/harness.py` — extend to position close/partial/add/funding/journal/review.
- `src/oft/replay/llm_source.py` — recorded responses by `(role, input_hash)` for MANAGEMENT/REVIEW; explicit `--live-llm`.
- `src/oft/replay/paper_loop.py` / `event_source.py` — position-management scenario wiring.
- `src/oft/cli/commands/replay.py` — `--scenario position-management` / journal/review.
- `tests/replay/test_management_replay.py`, `tests/replay/test_pnl_replay.py`, `tests/replay/test_journal_replay.py`, `tests/replay/test_review_replay.py` (final), `tests/replay/test_replay_namespace.py` (extend), `tests/replay/test_non_determinism_fails.py` (extend).
- `tests/test_no_wall_clock.py` — extend to `domain.pnl`, `domain.positions`, `domain.management`, `domain.journal`, and the new application services.
- `tests/contract/test_all_contract_fixtures.py` — extend to trade-log/REVIEW/management fixtures.
- `pyproject.toml` — import-linter contracts for `oft.application.review_service`/`management_service` and LLM adapters.
- `docs/runbooks/milestone-3.md`.
- CI config only if a new test marker/job is required.

**Tests expected**
- Replaying the same scenario twice with the same metric version, bar fixture, prompt versions, knowledge hash, and recorded LLM outputs produces identical closed positions, trade logs, PnL, statistics, management actions, and reviews.
- Replay writes only `replay_*` tables/IDs; it cannot mutate paper positions, trade logs, the paper account, or the budget; an attempted live path exits non-zero.
- A missing recorded MANAGEMENT/REVIEW output fails with a clear error unless `--live-llm` is explicit; replay never invents a decision.
- `tests/test_no_wall_clock.py` finds no `datetime.now()`/`time.time()` in the deterministic path; all time enters through `ClockPort`.
- Coverage ≥80% overall; 100% branch on `domain.risk`, the OMS/position state machine, `domain.pnl`, `domain.management`, and `domain.journal`.
- Import-linter: `domain` imports only stdlib/Pydantic; management/review services and LLM adapters cannot import the gateway/broker/Binance; `api` cannot import brokers/providers directly.
- `oft doctor` verifies the audit hash chain, management job state, funding idempotency, kill switch, and disk/DB health; it reports no secrets.
- The runbook reproduces the Milestone 3 demo from a clean data dir with no Telegram, Binance credential, or live provider (stub/recorded mode).
- No new product behavior is introduced.

**UI:** false. **Security:** false (but includes negative security/import tests). **Security review required:** no.

**Acceptance criteria**
- [ ] `uv run pytest -q` passes offline, including replay, management, PnL, journal, and review tests.
- [ ] Replay is deterministic and fails loudly on non-determinism, a missing recorded LLM output, or an attempted live path.
- [ ] The no-wall-clock test covers all deterministic modules; `ClockPort` is the only time source.
- [ ] Coverage is ≥80% overall and 100% branch on risk, OMS/positions, PnL, management, and journal.
- [ ] Import-linter proves the management/review LLM layer cannot reach execution and the API does not import brokers/providers directly.
- [ ] `oft doctor` verifies the audit chain and reports no secrets; `docs/runbooks/milestone-3.md` reproduces the demo.
- [ ] No new product behavior is introduced.

**Preview screens**
- None.

**Dependencies:** 23–30. **Size:** M.

---

## 5. Test strategy for the milestone

### 5.1 Levels and tools

| Level | Scope | Tools | Rules |
|---|---|---|---|
| Unit | PnL/funding math, position lifecycle, management/review semantic validation, risk classification, journal statistics/comparison | pytest, Hypothesis | Pure functions; no I/O; injected `ClockPort`; no network; `Decimal` only at money boundaries. |
| Contract | `trade-log`, `position`, `order`, `fill`, `llm-decision`/`llm-output` (REVIEW), `trigger-event`, `api-envelope`, error catalog | pytest + `jsonschema` Draft 2020-12 | Every fixture and route response validates; the error enum equals `error-catalog.json`; additive REVIEW enum changes keep old fixtures valid. |
| Integration | SQLite/Parquet persistence, close/partial/add/funding atomicity, management flow, idempotency, job ledger, journal stats, persisted entry-decision read | pytest, `tmp_path` | Real SQLite; no network; recorded market data and LLM responses; populated migration upgrades. |
| Replay | Recorded bars/metrics → protective exit/LLM management → partial/full close → trade log → stats → review | replay harness | Same domain code as live; pinned metric/prompt/knowledge versions; fixed clock; `replay_*` only. |
| E2E | CLI/API management and journal flows; positions/journal Web screens; 375/768/1440; keyboard; axe | pytest + HTTPX, Playwright, Vitest/RTL, axe | Recorded data + stub/recorded LLM; no real order; no live credential. |
| Provider contract | MANAGEMENT/REVIEW valid/refusal/truncated/empty/malformed outputs; REVIEW enum compatibility | recorded responses; optional nightly live | Schema-valid rate and repair behavior; no network in default CI. |
| Security | Kill-switch classification, risk-cap bypass, duplicate close/add/manage, stale marks, LLM output cannot execute, advisory-only review, CSV injection, XSS/CSP | pytest, Playwright | Negative cases are mandatory. |
| Live smoke (opt-in) | Public market data only (no credentials) | `@pytest.mark.live_smoke` | Never in default CI; not a golden test. |

### 5.2 LLM fixtures and stubs

- `tests/fixtures/llm/` gains `management_hold.json`, `management_partial_close.json`, `management_full_close.json`, `management_add.json`, `management_invalid_stop.json`, `management_oversized_add.json`, `review_valid.json`, `review_refusal.json`, `review_truncated.json`, and a REVIEW-shaped malformed case.
- `LLMStub` returns a fixture by `(role, input_hash)` for `MANAGEMENT` and `REVIEW`; provenance records `provider="stub"`, `model="fixture"` so a paper run cannot be mistaken for a real provider call.
- Recorded responses contain no real key, account data, or prompt body with secrets; fixtures are checksummed/tagged like the market fixtures.
- No copyrighted book text or generated skill is committed; the knowledge tests keep using the synthetic playbook.

### 5.3 Deterministic time, IDs, and PnL

- All time enters through `ClockPort`; tests use `FixedClock`/`ReplayClock`.
- No `datetime.now()`, `time.time()`, or local timezone in `domain`/`application`; `tests/test_no_wall_clock.py` fails the build on a violation and is extended in slice 31.
- All windows, funding settlements, expiry, cadence, and comparisons use UTC.
- Funding accrual uses the recorded settlement times/rates; the unique `(position_id, settlement_time)` row makes replay/restart idempotent.
- Position/trade/action/order/fill IDs and `client_order_id` are deterministic given the fixture, versions, fixed clock, and intent hash.
- Money math is `Decimal` with the repository’s 8-dp quantization; no float at a money boundary; the same inputs produce byte-identical decimal strings.
- MAE/MFE first-touch is deterministic: stop-first when both levels are inside one bar, with `AMBIGUOUS_BAR` recorded.
- Journal statistics are computed in `domain/journal.py` from the trade-log rows, not by SQL aggregates, so the same rows produce the same numbers in live and replay.

### 5.4 Coverage gate

- Overall line coverage ≥80%.
- 100% branch coverage on `src/oft/domain/risk.py`, the OMS/position state machine (`src/oft/domain/oms.py` + `src/oft/domain/positions.py`), `src/oft/domain/pnl.py`, `src/oft/domain/management.py`, and `src/oft/domain/journal.py`.
- Coverage is a floor: missing branches must be tested or explicitly justified in the PR.
- Frontend: Vitest/RTL for positions/journal components; Playwright for the positions/journal screens; axe-core for WCAG checks.

### 5.5 Explicitly not tested yet

- Live Binance order placement, testnet, reconciliation, user-data stream, live arming, leverage brackets.
- Telegram delivery, receipts, allowlist, budget pushes.
- Paper reset and the 4-week Live gate (Milestone 6 / follow-up).
- Full settings Web UI (F10), prompt/knowledge editors, knowledge review UI, 375px editing beyond read-only.
- News/social text; historical USDⓈ-M liquidations; full L2/DOM.
- Strategy parameter optimization, automatic rule changes, or a rule-consistency scoring editor.
- Real-provider nightly baselines (opt-in only; no CI gate until a baseline exists).

---

## 6. Milestone exit criteria

A milestone is complete only when all of the following are true:

- [ ] An `OPEN` paper position is managed automatically: code-only protective exits (stop/target/stale) work without an LLM; the `MANAGEMENT` cadence produces `HOLD|PARTIAL_CLOSE|FULL_CLOSE|ADD` decisions at most once per position per window, with no catch-up and restart reconstruction.
- [ ] Every management action is code-validated, risk-clamped, idempotent, executed through the single gateway in paper, and audited; the LLM cannot create an order, change a cap, or skip validation.
- [ ] Kill-switch classification works: risk-reducing exits/stop-tightening remain allowed; risk-increasing `ADD`/stop-loosening are blocked; activation never auto-closes positions.
- [ ] A full close creates exactly one `trade-log` row with gross/net PnL, fees, funding, R multiple, MAE/MFE, return, signal refs, outcome tags, data quality, and provenance; partial closes/adds accumulate correctly and do not create a trade log.
- [ ] Funding accrual is deterministic, idempotent, and sourced from recorded rates/times; missing data is `PARTIAL`, never fabricated.
- [ ] PnL/win-rate/expectancy/avg-R definitions are fixed by code and visible in CLI/API/Web; open/`PARTIAL` trades are excluded and counted; no-closed-trades shows “metrics undefined”.
- [ ] The journal comparison shows proposed zone vs actual entry, trigger→fill slippage, stop/target first touch (with `AMBIGUOUS_BAR`), and decision-vs-outcome; export is CSV/JSON and neutralizes spreadsheet formulas.
- [ ] The persisted entry decision is reused by default; the recheck card/API returns its provenance; `--refresh` is explicit. The trigger list includes environment/risk verdict/confidence.
- [ ] The `REVIEW` LLM output is schema-validated, provenance-recorded, advisory-only, idempotent, and cannot change a rule, prompt, cap, order, or position.
- [ ] Web `/positions`, `/positions/:id`, `/journal`, `/journal/:tradeId` implement the named preview screens with CALC/LLM provenance, definitions, partial states, keyboard/axe checks, and 375px read-only behavior.
- [ ] Replay is deterministic and writes only the replay namespace; the no-wall-clock test covers the deterministic path; coverage ≥80% with 100% branch on risk/OMS/positions/PnL/management/journal.
- [ ] Import-linter proves management/review LLM code cannot reach execution; every API response uses the envelope; the error catalog and envelope enum stay in sync.
- [ ] `oft doctor` verifies the audit chain and reports no secrets.
- [ ] **No Binance credential, Telegram token, live broker, paper reset, rule auto-tuning, or secret is required or recorded.** Public market data and fixtures are sufficient.
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

# 3. Produce a paper position using the Milestone 2 loop
#    (stub/recorded provider; public market data; no Binance credential)
uv run oft watchlist star BTCUSDT
uv run oft scan run --period 4h
uv run oft opp list --state open
uv run oft trigger evaluate <opportunity_id>
uv run oft trigger accept <trigger_id>

# 4. Manage the position (paper only; code validates the LLM proposal)
uv run oft pos list
uv run oft pos show <position_id>
uv run oft pos history <position_id>
uv run oft pos manage <position_id>
uv run oft paper balance
uv run oft paper equity

# 5. Close and review the trade
#    The 15 s monitor (slice 26) or a manual monitor tick closes a stop-hit
#    position without an LLM:
uv run oft pos monitor --position <position_id>
#    Or request a management decision (paper; code validates):
uv run oft pos manage <position_id>
uv run oft log list
uv run oft log show <trade_id>
uv run oft log stats --period 8w
uv run oft log compare <trade_id>
uv run oft log export --format csv --out ./journal.csv
uv run oft log review <trade_id>

# 6. Safety and replay
uv run oft kill status
uv run oft kill on
uv run oft kill off          # requires typed RESUME
uv run oft replay run --scenario position-management

# 7. Open the Web surfaces
#    http://127.0.0.1:7611/positions
#    http://127.0.0.1:7611/positions/<position_id>
#    http://127.0.0.1:7611/journal
#    http://127.0.0.1:7611/journal/<trade_id>
```

Expected: the position is managed/closed in paper, the journal entry has deterministic PnL/fees/funding/R/MAE/MFE, the stats show definitions and exclusions, the review is advisory, the kill switch blocks risk-increasing actions, and no Binance credential, Telegram token, live order, paper reset, or secret is required. A management/review failure leaves the position/trade metrics intact and shows an explicit reason.

---

## 8. Risks and rollback

| Risk | Mitigation |
|---|---|
| Wrong PnL/funding/R math silently misleads review | Pure `domain/pnl.py` + property tests; `Decimal` only; documented definitions; replay; UI/CLI state the definitions; `PARTIAL` excluded from stats. |
| LLM management executes a harmful action | Schema + semantic validation, deterministic risk gate, reduce-only exits, kill-switch classification, paper only, audit, security review; no live path. |
| Kill-switch bypass or misclassification | Explicit risk-increasing/reducing test matrix; gateway checks the classification before submit; audit event; security review. |
| Duplicate close/add/manage | Deterministic `client_order_id`, unique constraints, API idempotency key, single gateway, duplicate tests. |
| Double funding accrual on restart/replay | Unique `(position_id, settlement_time)`; idempotent upsert; funding replay test. |
| Stale mark/bar drives a management action | `risk_stale_mark_seconds` and data-quality checks; no action on stale data; UI `STALE`; tests. |
| Journal statistics mislead (zeros, open trades, partial data) | Fixed definitions, eligible-trade filter, exclusion counts, sample size, “metrics undefined” empty state; domain tests. |
| MAE/MFE or first-touch claims an unprovable intrabar sequence | Bar-based computation; stop-first convention; `AMBIGUOUS_BAR`; UI labels the limitation. |
| Review hallucination or prompt injection | Advisory-only, delimited data, canonical schema, no tools/execution imports, provenance, rejection tests, no rule application. |
| REVIEW contract change breaks existing providers/fixtures | Additive enum value; no new required provider field; old fixtures still validate; contract tests and recorded fixtures. |
| CSV formula injection / XSS from untrusted text | Server-side CSV neutralization; React text rendering; CSP unchanged; security tests; no raw HTML. |
| Scope creep into live/reset/settings/auto-tuning | Explicit slice fences; tests assert no live/reset/rule-change/live-UI path; milestone exit criteria. |
| Management LLM cost grows with many positions/windows | One decision per position per cadence window; no catch-up; global budget ledger/thresholds; per-role model configuration. |

**Rollback**
- The whole milestone is paper-only; no live order can exist. The kill switch blocks risk-increasing actions immediately.
- All new SQLite tables/columns are additive and forward-only. If a migration fails, restore the pre-migration backup rather than editing published rows; the failure path leaves the previous schema intact.
- Management automation can be disabled by configuration/job flags; protective exits remain code-only and can also be disabled while manual `oft pos` remains available.
- If a Web slice regresses, the CLI/API management/journal remains fully usable; the stored data is unaffected.
- Review can be disabled by clearing the REVIEW role/model configuration; the trade journal and statistics remain available with `review_decision_id=null`.
- Prompt/knowledge rollback activates a previous immutable version; missing versions are explicit and never silently substituted.

---

## 9. Follow-ups and deferred findings

### 9.1 Deferred findings absorbed by this milestone

| Source | Deferred item | Disposition |
|---|---|---|
| Slice 20 backend follow-up | Trigger list contract lacks `environment`/`risk_verdict`/confidence, so resolved queue rows show “—”. | **Absorbed in slice 27**: extend the trigger list projection and contract test. |
| Slice 20 backend follow-up | The recheck card envelope drops LLM provenance; the card cannot show the entry decision's own model/prompt/cost. | **Absorbed in slice 27**: the persisted entry-decision read returns provenance; the card/API uses it. |
| Slice 20 backend follow-up | Reusing a persisted entry decision instead of paying for a fresh recheck (read endpoint). | **Absorbed in slice 27**: `recheck` reuses the persisted decision by default; `--refresh` is explicit. |
| Milestone 2 §9.5 | Position management depth, funding, PnL/win-rate/R-multiple, journal/review. | **Absorbed by slices 23–30.** |

### 9.2 Deferred findings and items **not** absorbed

| Source | Item | Disposition |
|---|---|---|
| Slice 20 LOW | Shared `api/http.ts` client (deduplicate CSRF/cookie helpers). | Not absorbed; the new positions/journal API clients stay consistent with the existing helpers, but the shared refactor remains a follow-up. |
| Slice 20 LOW | Non-crypto idempotency-key fallback hardening. | Not absorbed; remains a follow-up. |
| Slice 20 LOW | Resume token in a JSON body instead of the query string. | Not absorbed; remains a follow-up. |
| Slice 20 LOW | RESUME-token rejection live hint / `aria-invalid`. | Not absorbed; remains a follow-up. |
| Slice 20 LOW | Status-bar text-zoom clipping (min-height/wrap). | Not absorbed; remains a follow-up. |
| Slice 20 LOW | Skip links. | Not absorbed; new screens keep keyboard access, but skip links are a follow-up. |
| Slice 20 LOW | Explicit LONG/SHORT side with fallback. | Not absorbed; remains a follow-up. |
| Slice 20 LOW | Single resolved status source in the trigger queue. | Not absorbed; remains a follow-up. |
| Slice 20 LOW | Replace source-scraping E2E assertions with rendered-behavior checks. | Not absorbed; new tests use rendered behavior, but existing tests are not rewritten in this milestone. |
| Slice 20 LOW | Remove the generated `.vite/vitest/results.json` artifact. | Not absorbed; remains a follow-up. |
| Slice 22 Finding 4 | Logger namespace change when code moved to `sqlite_trading_ledger`; external log filters keyed to the old name stop matching. | No code action; recorded in the slice-22 PR body. Not absorbed (nothing to absorb). |
| Slice 22 Finding 6 | Private `_BUSY_TIMEOUT_MS` imported across modules; optional rename to `BUSY_TIMEOUT_MS`. | Not absorbed; it is a behavior-free cleanup outside the Milestone 3 product scope and remains a LOW follow-up. |
| Milestone 2 §9.1 | Knowledge Mode B deterministic topic retrieval. | Not absorbed; Mode A remains the injection mode. |
| Milestone 2 §9.2 | Provider/model/pricing snapshot re-verification. | Not absorbed; implementation-time task for every provider change, not a Milestone 3 slice. |
| Milestone 2 §9.3 | Budget default validation after two weeks of logs. | Not absorbed; Telegram budget alerts remain Milestone 5. |
| Milestone 2 §9.4 | Paper reset / 4-week Live gate. | Not absorbed; the Milestone 3 PRD row does not include it. It moves to Milestone 6 (live arming) or a dedicated follow-up. This supersedes the Milestone 2 plan's assignment of reset to Milestone 3. |
| Milestone 2 §9.6 | Full dual-entry settings UI (F10a–F10g), prompt/knowledge editors, 375px editing. | Not absorbed; Milestone 4. |
| Milestone 2 §9.7 | Telegram report/trigger/risk/budget pushes, receipts, allowlist. | Not absorbed; Milestone 5. |
| Milestone 2 §9.8 | Live execution, reconciliation, arming, typed confirmations. | Not absorbed; Milestone 6. |
| Milestone 2 §9.9 | Dedicated report contract. | Not absorbed; only add `report.schema.json` if the report payload grows beyond the documented composition. |
| Milestone 2 §9.10 | Paper fill realism (queue position, partial fills). | Partially documented in the journal, but not changed; remains a follow-up. |
| Milestone 2 §9.11 | Session/`NO_TRADING` handling. | Not absorbed; pre-Live item. |

### 9.3 New follow-ups from this milestone

1. **Rule-consistency scoring workflow.** The journal exports the comparison and leaves `decision_quality.matched_rules=null` unless a human review marks it; a scoring editor and the ≥70% metric workflow are follow-ups.
2. **Finer first-touch evidence.** If 4h/8h bars produce many `AMBIGUOUS_BAR` cases, evaluate finer recorded bars or tick/agg-trade evidence in a later milestone; do not guess.
3. **Optional human confirmation for paper management.** Design permits automatic paper management; if the operator later wants a confirmation toggle, add it as a configuration option, not a default behavior change.
4. **Manual emergency flatten.** ADR-0006 keeps manual flatten out of MVP; the management `FULL_CLOSE` path is not an emergency-flatten substitute. Revisit before Live.
5. **Funding interval variants.** Use the recorded schedule; validate 4h/1h funding symbols and TradFi windows before relying on the journal for those symbols.
6. **Management cost ceiling.** The cadence is the primary control; if management calls dominate spend, add a per-position/day cap in a follow-up without changing the risk behavior.
7. **Open-trade LLM review.** Milestone 3 reviews closed trades only; reviewing an open position is a separate advisory feature.
8. **Chart integration depth.** The position detail reuses the existing chart with entry markers; drawing tools/multi-timeframe overlays remain Milestone 7.

### 9.4 Open questions (not blockers)

- Funding rate/interval completeness for TradFi perpetuals; the journal marks missing funding `PARTIAL` until validated.
- Whether `target-first`/`stop-first` ambiguity is acceptable at 4h/8h or needs finer bars; the plan chooses stop-first plus `AMBIGUOUS_BAR` for determinism.
- Whether the review prompt should receive management history by default; the plan includes it as data and records the selected sections/hash, with a budget bound.

**No `needs-human` blocker.** The plan proceeds on the PRD row, ADRs, and the assumptions in §2.2. If G3 wants human confirmation for paper management, a different kill-switch interpretation, or paper reset inside Milestone 3, record that product decision before implementation.

---

## 10. Slice-to-preview traceability

| Slice | Preview surfaces |
|---|---|
| 23 | S07.3 `oft pos list|show`; S09.3 `oft log list|show`; S08.2 `oft paper equity`; no Web UI |
| 24 | S07.3 `oft pos show|history`; S09.3 `oft log show`; S08.2 `oft paper equity` |
| 25 | S07.3 `oft pos manage`; management decision-card content in API/CLI |
| 26 | No direct screen; job status in `/system`, `oft pos history`, `oft log` |
| 27 | S07.3 `oft pos list|show|history`; S09.3 `oft log list|show|stats|compare|export`; S06.3 trigger list provenance |
| 28 | S09.3 `oft log review` |
| 29 | S07.1 `/positions`; S07.2 `/positions/:positionId` |
| 30 | S09.1 `/journal`; S09.2 `/journal/:tradeId` |
| 31 | None |

---

## 11. PRD traceability

| PRD requirement / constraint | Where addressed | Slice(s) | Verification |
|---|---|---|---|
| Milestone 3: 自动仓位管理 + 完整交易日志 + 盈亏/胜率/复盘视图 | Whole plan | 23–31 | Milestone exit criteria; replay; E2E. |
| MVP 8: 定时或条件触发时调用 LLM 给出全平/部分平/加仓建议，由代码校验并执行 | Protective rules + `MANAGEMENT` + risk/gateway + scheduler | 23, 24, 25, 26 | Management tests; kill-switch/risk tests; scheduler tests. |
| MVP 9: Paper trading 完整模拟成交与记账 | Paper close/add/partial/funding/accounting | 23, 24, 31 | Trade-log/accounting tests; replay. |
| MVP 13: 历史持仓、交易、盈亏、胜率、信号与实际结果的对照 | Journal list/detail/stats/compare/export; Web views | 24, 27, 30 | Stats/comparison tests; E2E/a11y. |
| Module boundary: 确定性归代码，主观性归 LLM，结构化契约交互 | Code-owned PnL/risk/orders/journal; LLM management/review validated and advisory | 24, 25, 28 | Contract tests; import-linter; negative security tests. |
| Module boundary: LLM 无权下单/改配置/跳过触发 | Management/review services cannot import gateway/broker; code validates every action | 25, 28, 31 | Import-linter; invalid/oversized/refused output tests. |
| Risk: LLM 幻觉或不合规导致错误交易 | Schema + semantic validation; risk gate; kill switch; paper only; advisory review | 25, 26, 28 | Rejection tests; risk-cap tests; kill-switch tests. |
| Risk: 主观判断不一致、无法复现 | Prompt versioning; provenance; replay; deterministic stats | 24, 25, 27, 28, 31 | Replay; no-wall-clock; provenance contract tests. |
| Risk: 实盘资金损失 / Live default off | No live path; paper-only gateway; risk-reducing classification | 23–31 | Milestone exit criteria; no-live-path tests. |
| Success metric: 决策规则一致性 ≥70% | Journal comparison/export; advisory review; `matched_rules` left for human review | 27, 28, 30 | Comparison tests; export tests. |
| Success metric: Live 前至少 4 周 paper 记录 | Complete paper trade journal and stats provide the evidence; reset/Live gate remains Milestone 6 | 24, 27, 30 | Journal continuity tests. |
| Decisions: LLM 提议 + 用户配置上限，代码取更严者 | Risk gate for ADD and exits; requested vs allowed recorded | 25, 26 | Risk property tests; verdict persistence. |
| Non-goal: 多用户/SaaS/移动原生 App/高频/跨所/自动调参/新闻社媒 | Explicitly out of scope | — | Slice fences; milestone non-goals. |
| Open question: TradFi session/funding windows | Journal marks missing funding/session data `PARTIAL`; no silent assumption | 24, 30 | Data-quality tests; follow-up. |

---

*End of plan. No gate is approved by this document. Milestone 3 G3 remains pending; this plan is the input to G3.*
