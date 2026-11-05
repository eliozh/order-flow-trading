# Flow Map — Order Flow LLM Trading System

**Stage:** 3 — design direction
**Status:** complete
**Date:** 2026-11-06
**Companion files:** `design/DESIGN.md`, `design/design-tokens.json`, `design/design-preview.html`
**Scope:** every MVP flow in the PRD, for both entry points (CLI + Web UI). No implementation, no framework.

---

## How to read this map

Every flow has:

- **Goal** — the operator outcome.
- **PRD trace** — the exact PRD scope item(s) it satisfies.
- **Entry points** — CLI command and/or Web route.
- **Screens / commands** — the surface inventory.
- **States** — per screen, always the same six: `default`, `empty`, `loading`, `error`, `partial-data`, `success`. Where a state is genuinely inapplicable, it says why instead of being omitted.

### Global state definitions

| State | Meaning | Visual contract |
|---|---|---|
| Default | Valid data, no exceptional condition | Full layout, last-updated stamp |
| Empty | Query succeeded, zero records | One explanatory row/panel; cause; one next action |
| Loading | Work in flight | Stable skeleton geometry or named stage list; no fake percentages |
| Error | Work failed | Specific cause, scope, reference ID, retry path |
| Partial-data | Some data unavailable, rest usable | Show what exists; mark missing as `N/A`/`STALE`/`ESTIMATED`; never blank the screen |
| Success | A user action completed | Scope + timestamp + next action; undo where applicable |
| Permission | Action not allowed | Explain the boundary and the safe path; do not silently disable |

### Permission model (global)

Single-user local install, no account system. "Permission" states come from four boundaries:

1. **Environment**: Paper vs Live. Live is default-off and requires the 4-week paper gate plus typed confirmation.
2. **Channel**: Telegram is read-only in MVP. The 375px Web UI is read-only except the kill switch.
3. **Secrets**: API keys are write-only after entry; they are never displayed again, only masked last-4.
4. **Kill switch**: always available, in every state, including error and read-only. Stopping is never gated.

### Route and command index

| Flow | Web routes | CLI commands |
|---|---|---|
| F01 Onboarding | `/onboarding`, `/onboarding/:step` | `oft init` |
| F02 Watchlist | `/watchlist`, `/watchlist/:symbol` | `oft watchlist *` |
| F03 Scan | `/scans`, `/scans/:scanId` | `oft scan *`, `oft daemon *` |
| F04 Report | `/reports`, `/reports/:reportId` | `oft report *` |
| F05 Opportunities | `/opportunities`, `/opportunities/:oppId` | `oft opp *` |
| F06 Trigger review | `/triggers`, `/triggers/:triggerId` | `oft trigger *` |
| F07 Positions | `/positions`, `/positions/:positionId` | `oft pos *` |
| F08 Paper account | `/paper` | `oft paper *` |
| F09 Journal | `/journal`, `/journal/:tradeId` | `oft log *` |
| F10 Settings | `/settings/*` | `oft llm`, `oft prompt`, `oft risk`, `oft config`, `oft budget`, `oft telegram`, `oft live`, `oft kill` |
| F11 Telegram | Telegram chat (no route) | `oft telegram status\|test\|receipts` |
| F12 CLI surface | — | all groups |

---

## Flow index

| ID | Flow | Primary action | PRD trace |
|---|---|---|---|
| F01 | First run / onboarding | Configure keys, environment, watchlist, schedule | MVP 9, 10, 11, 12, 14, 15; Risks (Live default off) |
| F02 | Watchlist management | Star/unstar and group symbols | MVP 3 |
| F03 | Scheduled scan run | Trigger or monitor the 4h/8h scan | MVP 1, 3, 5 |
| F04 | Analysis report | Read narrative, verify metrics, open opportunities | MVP 2, 4, 5 |
| F05 | Opportunity ledger | Track structured opportunity states | MVP 6 |
| F06 | Trigger → LLM entry re-check | Accept or reject entry | MVP 6, 7; risk caps; human confirmation |
| F07 | Position management | Review/execute hold, partial, full, add | MVP 8 |
| F08 | Paper trading account | Inspect balances, orders, fills; reset | MVP 9 |
| F09 | Trade log and review | Compare signal vs outcome; read review | MVP 13 |
| F10 | Settings | Configure providers, prompts, risk, scan, budget, Telegram, Live | MVP 11, 12, 15, 14, 3; Milestone 6 |
| F11 | Telegram read-only channel | Receive pushes, run queries | MVP 14 |
| F12 | CLI surface | Operate everything from the terminal | MVP 10 (full CLI) |

---

# F01 — First run / onboarding

**Goal:** From a fresh clone to a configured, paper-only system with keys validated, a watchlist, a schedule, and a first scan understood — without exposing secrets and without accidentally enabling Live.

**PRD trace:** MVP 9 (paper trading), MVP 10 (dual entry), MVP 11 (LLM providers), MVP 12 (prompt templates), MVP 14 (Telegram token), MVP 15 (budget thresholds); Risk "Live default off"; Success metric "paper ≥ 4 weeks before Live"; module boundary (keys never enter LLM context).

**Entry points:** `oft init` (interactive; `oft init --resume --non-interactive` for scripted re-runs); Web `/onboarding`.

### Screens / commands

| ID | Surface | Purpose |
|---|---|---|
| S01.1 | `/onboarding` step 1 — Environment | Data directory, timezone, paper vs live (live unavailable), confirm local-only bind |
| S01.2 | Step 2 — LLM providers | Add OpenAI / Anthropic / DeepSeek keys, choose default model per role, test connection |
| S01.3 | Step 3 — Binance | API key/secret, permission read-back, TradFi agreement status, leverage brackets cache |
| S01.4 | Step 4 — Telegram | Bot token, chat ID, allowlist, send test, delivery receipt shown |
| S01.5 | Step 5 — Watchlist + schedule | Seed crypto/TradFi symbols, scan period 4h/8h, budget thresholds, review |
| S01.6 | CLI `oft init` | Same sequence as prompts; resumable; non-interactive flags for automation |

### States

**S01.1 Environment**

- **Default:** paper selected and locked; data directory suggestion; timezone detected; "本地单机，仅 127.0.0.1" notice.
- **Empty:** no config file found — this is the expected first-run state; CTA "开始配置".
- **Loading:** filesystem writability check; daemon port check; exchange reachability probe.
- **Error:** data directory not writable; port already in use; system clock drift > 60s (warn, allow continue).
- **Partial-data:** existing partial config detected — offer "从上次中断继续" with a list of completed steps.
- **Success:** step saved; progress indicator advances; no secrets echoed.
- **Permission:** Live is disabled with explanation: "Live 需要至少 4 周 paper 记录，且需在设置中单独启用". Mobile: onboarding editing is blocked; banner directs to desktop/CLI.

**S01.2 LLM providers**

- **Default:** provider list with status chips `未配置 / 已验证 / 验证失败`; default model selector; role mapping (analysis / entry re-check / management / review).
- **Empty:** no provider configured; inline help on which key is needed; cannot finish onboarding without at least one provider (schema-valid decisions require one).
- **Loading:** `测试连接` shows per-provider spinner and the exact model being tested.
- **Error:** 401 invalid key; model not available for the account; network timeout. Error names the provider and keeps the key field editable; key is never logged.
- **Partial-data:** key valid but the chosen strict-schema mode is unavailable (DeepSeek beta tool calling) → show "将使用 JSON mode + 校验重试" and mark degraded; user may choose another provider.
- **Success:** `已验证 · <model> · 延迟 842ms`; mask shows `sk-…4f2a`; next enabled.
- **Permission:** keys are write-only; after save the field shows mask + "替换密钥" only. No reveal action exists.

**S01.3 Binance**

- **Default:** key/secret fields; permission checkboxes; TradFi agreement status; "不使用提现权限" callout.
- **Empty:** no key; explain read + trade permissions required, withdrawal must be disabled.
- **Loading:** signed ping; permissions read; TradFi agreement status; leverage brackets fetch (signed).
- **Error:** invalid key, IP not allowed, futures not enabled, region blocked, TradFi agreement rejected. Each error names the next action in the Binance UI.
- **Partial-data:** key valid but TradFi agreement unsigned → crypto works, TradFi symbols will be read-only; leverage brackets unavailable → risk validation will use conservative defaults and flag it; show both consequences.
- **Success:** key validated; permissions summary; masked key; leverage brackets cached with timestamp.
- **Permission:** read-only key is allowed for market data but blocks paper/live order simulation from Binance; paper engine can still simulate locally. Explain the distinction.

**S01.4 Telegram**

- **Default:** token field, chat ID, allowlist IDs, push toggles; "只读通道" badge.
- **Empty:** no token; onboarding can defer Telegram (marked optional but pushes disabled).
- **Loading:** bot token validation, `getMe`, test message send, receipt polling.
- **Error:** 401 token, chat not found, bot blocked by user, network timeout; retry available.
- **Partial-data:** token valid but test message not delivered → show receipt `failed` with reason; onboarding continues but warns that alerts will not arrive.
- **Success:** receipt `delivered` with message ID and timestamp; test message visible in chat.
- **Permission:** allowlist explains that unknown chats receive a refusal, not data; Telegram remains read-only.

**S01.5 Watchlist + schedule**

- **Default:** suggested crypto list + TradFi list with search; scan period 4h/8h; budget defaults daily $10 / weekly $50 / monthly $150 (editable, alert-only).
- **Empty:** no symbols selected; cannot finish with zero starred symbols; CTA "至少选择一个标的".
- **Loading:** exchangeInfo symbol lookup; schedule estimate; budget validation.
- **Error:** exchangeInfo unavailable → allow manual symbol entry with a STALE warning; invalid budget value → inline error.
- **Partial-data:** selected TradFi symbol has no history before listing; show "历史自 2025-12-11"; symbol not TRADING → block star with reason.
- **Success:** summary of N crypto + M TradFi, scan cadence, next run time, budget thresholds; "完成" creates config and queues the first scan.
- **Permission:** Live remains off; final screen explicitly says "当前为 Paper。Live 需 4 周 paper 后手动开启".

**S01.6 CLI `oft init`**

- **Default:** numbered prompts mirroring the five steps; secret input is masked; `Ctrl-C` saves progress and prints `oft init --resume`.
- **Empty:** no config → this is the normal entry; prints preflight checks first.
- **Loading:** inline stage lines (`验证 Binance 密钥 … ok`); no spinner art.
- **Error:** non-zero exit with code 5 (auth/config) or 1 (runtime); prints the exact failing check and next command. Non-TTY without flags prints usage error (code 2).
- **Partial-data:** prints `TRADFI 未签署协议：crypto 可用，TradFi 只读` and asks whether to continue; completed steps are persisted.
- **Success:** prints masked summary, config path, and next commands (`oft scan run`, `oft serve`); exit 0.
- **Permission:** Live selection in `init` is not offered; the only prompt is "Paper (默认) — Live 需后续在 `oft live enable` 开启".

---

# F02 — Watchlist management

**Goal:** Maintain the exact set of symbols the scheduled scan will analyze, grouped by crypto vs TradFi, with data-coverage consequences visible before the next scan.

**PRD trace:** MVP 3 (star symbols, configurable scan period); TradFi decision (watchlist maintained in settings); research gaps (TradFi history from listing, session flags).

**Entry points:** Web `/watchlist`, `/watchlist/:symbol`; CLI `oft watchlist list|add|remove|star|unstar|search|groups`.

### Screens / commands

| ID | Surface | Purpose |
|---|---|---|
| S02.1 | `/watchlist` | Grouped table: Crypto / TradFi; star state; search; counts; next-scan inclusion |
| S02.2 | `/watchlist/:symbol` | Inspector: contract specs, listing date, data coverage, session schedule, leverage brackets |
| S02.3 | Search overlay (within S02.1) | Symbol search across 942 USDⓈ-M symbols with filters |
| S02.4 | `oft watchlist` | List/add/remove/star/unstar/search/groups in table or JSON |

### States

**S02.1 Watchlist table**

- **Default:** two collapsible groups (Crypto, TradFi) with counts; columns: star, symbol, type, last, 24h Δ, next session, data coverage, last scan; starred symbols sorted by group then symbol.
- **Empty:** no starred symbols — "下次扫描将不会运行。添加至少一个标的。" CTA opens search; suggests BTCUSDT/XAUUSDT but does not auto-add.
- **Loading:** skeleton rows with stable column widths on first load; star toggles are optimistic and show a pending dot until confirmed.
- **Error:** exchangeInfo/symbol registry unavailable → render last cached list with a `STALE` stamp and retry; add/remove disabled with explanation; existing scan inclusion still shown.
- **Partial-data:** delisted or non-`TRADING` symbol marked red and excluded from the next scan; TradFi agreement unsigned → tradable data read-only, symbol marked; recent listing → "历史自 YYYY-MM-DD"; missing metrics → per-row `PARTIAL`.
- **Success:** star/unstar confirmed — toast "BTCUSDT 已加入下次扫描（4h，02:14 UTC）" or "已移出下次扫描"; counts update.
- **Permission:** local single-user; no per-record permissions. TradFi agreement and API-key limits are shown as capability boundaries, not access control.

**S02.2 Symbol inspector**

- **Default:** symbol header, contract type, tick/step/min notional, listing date, data coverage, session schedule, leverage brackets, latest metric freshness, link to chart/report.
- **Empty:** symbol not found → 404 panel with search.
- **Loading:** spec + coverage + session fetch.
- **Error:** signed leverage brackets unavailable → section shows `N/A` with reason and "使用保守默认值"; rest of inspector renders.
- **Partial-data:** no bookTicker for TradFi; no liquidation history; irregular metrics timestamps → each capability row carries a stamp, never a silent blank.
- **Success:** star toggle confirmed; "加入扫描" changes to "已在下次扫描".
- **Permission:** same as S02.1; Live capability is not implied by a tradable symbol.

**S02.3 Search overlay**

- **Default:** empty query shows recent searches and group shortcuts; results grouped Crypto/TradFi.
- **Empty:** query with no matches — "无匹配标的。检查代码后缀（均为 USDT 永续）。"
- **Loading:** debounced search with a non-blocking inline progress; previous results stay visible.
- **Error:** registry fetch failed — search unavailable, with retry; manual exact-symbol add still offered.
- **Partial-data:** result has `status != TRADING` → disabled with reason; TradFi without agreement → addable but marked read-only.
- **Success:** select adds to watchlist with the same confirmation as S02.1.
- **Permission:** none beyond environment capability flags.

**S02.4 `oft watchlist`**

- **Default:** `oft watchlist list` prints grouped box-drawing table; `--group crypto|tradfi|all`, `--format table|json`.
- **Empty:** prints `No starred symbols. Next scan will not run.` and a hint, exit 0 (empty is not an error).
- **Loading:** stage lines for registry refresh; `oft watchlist add` waits for confirmation.
- **Error:** exit 1 with the failing endpoint; exit 5 if Binance auth is required for the operation.
- **Partial-data:** rows carry a `DATA` column with `PARTIAL`/`STALE`; `--format json` includes `dataQuality` per symbol.
- **Success:** prints the confirmed row and the next-scan sentence; exit 0.
- **Permission:** `remove` asks `[y/N]` default No and shows the impact ("移出下次扫描"); no typed token required.

---

# F03 — Scheduled scan run

**Goal:** Run the full starred-symbol analysis on a 4h/8h cadence (or manually), show honest per-symbol progress, and produce a report per symbol without letting one failure kill the batch.

**PRD trace:** MVP 1 (data + backfill), MVP 3 (schedule), MVP 5 (periodic analysis for all starred symbols); Success metric "扫描时效"; Risk "限流/数据缺口" and "单机长期运行".

**Entry points:** Web `/scans`, `/scans/:scanId`; CLI `oft scan run|status|history|show`; daemon `oft daemon start|stop|restart|status|logs`.

### Screens / commands

| ID | Surface | Purpose |
|---|---|---|
| S03.1 | `/scans` | Schedule status, next run countdown, scan history, manual trigger |
| S03.2 | `/scans/:scanId` | Run detail: overall stages + per-symbol stage list + results |
| S03.3 | `oft scan run` | Manually trigger a scan (refuses if one is already running) |
| S03.4 | `oft scan status\|history\|show` | Daemon/schedule state, history, single run detail |
| S03.5 | `oft daemon *` | Start/stop/restart/status/logs for the long-running scheduler + Telegram process |

### Scan stages (per symbol)

`QUEUED → DATA → METRICS → LLM → REPORT → DONE`

Failure at any stage marks that symbol `FAILED` with a stage-specific reason; other symbols continue. A symbol with usable metrics but failed LLM is `DEGRADED` and still produces a metrics-only report.

### States

**S03.1 Scan list**

- **Default:** schedule card (4h/8h, next run countdown, timezone UTC), last run summary (N/M complete, opportunities found), history table (time, duration, symbols, status, cost, Telegram receipt).
- **Empty:** no scans yet — "尚未运行扫描。首次运行会回补所需历史数据。" CTA "立即运行" (disabled if watchlist empty, with a link).
- **Loading:** history skeleton; schedule state refreshes independently.
- **Error:** daemon not running → banner "扫描调度未启动" with `oft daemon start` and a local start action; history still renders from stored scans.
- **Partial-data:** last scan completed with degraded/failed symbols → summary chip `2 degraded / 1 failed` and a link to the detail; budget near threshold shown as amber but not an error.
- **Success:** manual trigger accepted → row appears as `RUNNING`; toast includes scan ID.
- **Permission:** manual scan is allowed in paper and live; live scans still produce paper decisions unless order sending is armed (see F06/F10g). Mobile: trigger hidden with explanation.

**S03.2 Scan detail**

- **Default:** header (scan ID, trigger source `scheduled|manual`, start/end, period, total cost); progress summary; per-symbol rows with stage and result link.
- **Empty:** invalid scan ID → not-found panel with history link.
- **Loading:** named stages with elapsed timers; overall counts (`DATA 4/10`, `LLM 2/10`); no fake percentage; per-symbol stage list updates in place. A scan can run for minutes — the page must remain usable.
- **Error:** whole-run failure (daemon crash, DB write failure) → error banner with reference ID and log link; completed symbol outputs remain readable. Per-symbol errors are inline, not page-level.
- **Partial-data:** symbol skipped for a data gap with the exact gap window; LLM schema rejection shown as `REJECTED` with the failing field and "no report narrative"; TradFi `NO_TRADING` flag noted as a regime, not a failure; report exists for degraded symbols.
- **Success:** all symbols complete → "完成：10/10，2 个新机会，已推送 Telegram" with links to reports/opportunities and delivery receipt.
- **Permission:** none; read-only detail for all users of this local install.

**S03.3 `oft scan run`**

- **Default:** queues a manual run and streams stage lines; prints scan ID.
- **Empty:** watchlist empty → exit 4 (blocked) with "No starred symbols"; no scan created.
- **Loading:** live stage lines (`BTCUSDT data ✓ metrics ✓ llm …`); `--format ndjson` emits one event per stage change.
- **Error:** already running → exit 4 with the running scan ID; daemon down → exit 1 with restart hint; provider auth failure → per-symbol failures, overall exit 3 (partial) if any symbol completed.
- **Partial-data:** exit 3; final table marks `DEGRADED`/`FAILED`; prints the exact skipped windows.
- **Success:** exit 0; prints scan ID, counts, cost, and the report command.
- **Permission:** none; live order sending is not triggered by a scan.

**S03.4 `oft scan status|history|show`**

- **Default:** aligned tables; `status` prints next run, last run, daemon health.
- **Empty:** `history` prints "No scans." exit 0.
- **Loading:** short request; no progress needed.
- **Error:** exit 1 on storage failure; exit 5 if daemon control requires config.
- **Partial-data:** `show` includes per-symbol quality columns; `--format json` preserves stage timings and error codes.
- **Success:** exit 0; `show` prints a per-symbol summary and links (as text paths/IDs).
- **Permission:** `daemon stop` asks `[y/N]`; stopping does not close positions, and the prompt says so.

**S03.5 `oft daemon *`**

- **Default:** `status` shows PID, uptime, scheduler next fire, Telegram long-poll state, DB writer state.
- **Empty:** not running → clear message, exit 0 for `status`, exit 1 for stop.
- **Loading:** start/stop wait with a bounded timeout and print the resulting state.
- **Error:** port conflict, DB lock held by another process, Telegram conflict (another poller) → precise reason and the owning PID if known.
- **Partial-data:** daemon up but Telegram disconnected → `DEGRADED` with retry count; scanner still runs.
- **Success:** started/stopped; prints next scheduled run and where logs live.
- **Permission:** stop/restart asks `[y/N]`; no typed token.

---

# F04 — Analysis report view

**Goal:** Read the LLM's market narrative while verifying every quantitative claim against deterministic metrics, then move to the opportunities it produced. The report must remain useful when the narrative fails.

**PRD trace:** MVP 2 (deterministic metrics), MVP 4 (knowledge injection — reflected in the narrative's `LLM` provenance), MVP 5 (analysis report), module boundary (CALC vs LLM).

**Entry points:** Web `/reports`, `/reports/:reportId`; CLI `oft report list|show|export|open`.

### Screens / commands

| ID | Surface | Purpose |
|---|---|---|
| S04.1 | `/reports` | Filterable report list (symbol, date, scan, quality) |
| S04.2 | `/reports/:reportId` | Report: header, CALC metrics, chart, LLM narrative, opportunity cards |
| S04.3 | Chart panel (embedded in S04.2) | Candles + volume + CVD + footprint/VP + zones + markers (see DESIGN §13) |
| S04.4 | `oft report show` | Terminal report: metrics first, `LLM ›` narrative, opportunity IDs |
| S04.5 | `oft report export` | Markdown or JSON export preserving provenance |

### Report anatomy

1. **Header:** symbol, scan window, generated at, model/provider/prompt version, token cost, data-quality stamp.
2. **CALC band:** CVD/delta series summary, footprint highlights, POC/VAH/VAL, OI Δ, funding, liquidations (since deployment only), gaps.
3. **Chart:** embedded panel per DESIGN §13.
4. **LLM band:** structure narrative, absorption/exhaustion read, key zones, proposed opportunities with confidence. Always stamped `LLM`.
5. **Opportunity cards:** link to the ledger; no accept action here (accept lives in F06).
6. **Data provenance:** `LLM` narrative is untrusted text; no market text is injected into it (MVP).

### States

**S04.1 Report list**

- **Default:** table (symbol, scan time, quality, opportunities, model, cost); filters by symbol/period.
- **Empty:** no reports yet — "报告由扫描生成。先运行一次扫描。" link to `/scans`.
- **Loading:** skeleton; filters remain interactive.
- **Error:** storage unavailable → error panel with retry; no partial list without a `STALE` stamp.
- **Partial-data:** report exists but narrative rejected → row shows `CALC only`; missing series → `PARTIAL` chip.
- **Success:** filter applied; result count announced.
- **Permission:** report list uses global allowlisted Telegram and local-only Web; no per-report permission.

**S04.2 Report detail**

- **Default:** all sections rendered; chart interactive; opportunity cards link to F05.
- **Empty:** report ID not found → 404 panel with back link.
- **Loading:** metrics and chart load independently of narrative. Metrics may render first; the LLM band shows "LLM 分析进行中" with a named stage, not a fake text skeleton.
- **Error:** **narrative failure must not fail the report.** Metrics + chart render; LLM band shows a red error with the failing schema field and a retry action. If metrics failed, the report shows an error panel with the scan log reference. If the chart library fails, a bordered "图表不可用" panel replaces only the chart.
- **Partial-data:** liquidation panel says "自部署起记录，无历史回补"; TradFi DOM absent ("无 bookTicker 历史"); resampled metrics stamped `ESTIMATED`; gaps listed with windows; opportunity referencing an unavailable metric marked `不可用`.
- **Success:** narrative is complete and stamped; export available; "查看机会" moves to F05.
- **Permission:** no accept/reject here; the report is read-only by design so a decision cannot be made from narrative alone.

**S04.3 Chart panel**

- **Default:** price + volume + CVD; volume profile; footprint for the selected bar; zones/markers; crosshair tooltip with data-quality flag.
- **Empty:** no bars in range → "该标的在所选窗口无数据（上市于 YYYY-MM-DD）" with a range suggestion.
- **Loading:** pane skeletons preserve pane heights; axes appear before series; no layout shift when data lands.
- **Error:** series fetch failed → per-pane error, other panes remain; overlay computation failed → overlay disabled with a label, base chart stays.
- **Partial-data:** gaps drawn as breaks (not interpolated); `STALE` stamp; liquidation overlay absent with `N/A`; TradFi `NO_TRADING` hatch explained in the legend.
- **Success:** crosshair/tooltip works; selected bar's footprint matches the CALC table.
- **Permission:** none; chart is read-only.

**S04.4 `oft report show`**

- **Default:** metrics table first, then `LLM ›` narrative, then opportunity IDs; `--format json` splits `calc` and `llm` blocks.
- **Empty:** report not found → exit 1.
- **Loading:** prints "Loading report…" only if >300ms; otherwise immediate.
- **Error:** exit 1 with reference ID.
- **Partial-data:** prints `N/A` rows with reasons; narrative header `LLM · schema ok · confidence 0.72 · prompt v3`.
- **Success:** exit 0.
- **Permission:** export includes all data but never API keys or raw prompt secrets.

**S04.5 `oft report export`**

- **Default:** `--format md|json`; prints output path or stdout with `--stdout`.
- **Empty:** not-found → exit 1.
- **Loading:** stage line.
- **Error:** unwritable path / disk full → exit 1 with path.
- **Partial-data:** export preserves `N/A`/`STALE` stamps; never silently drops missing series.
- **Success:** prints path, size, and the provenance counts (`CALC blocks: 12 · LLM blocks: 1`).
- **Permission:** none.

---

# F05 — Opportunity ledger

**Goal:** Keep every structured opportunity, its trigger conditions, and its state transitions auditable, so a triggered zone can be reviewed rather than remembered.

**PRD trace:** MVP 6 (structured opportunity ledger, code-decided triggers); Decisions "Telegram read-only", "risk caps stricter side"; module boundary (LLM proposes, code triggers).

**Entry points:** Web `/opportunities`, `/opportunities/:oppId`; CLI `oft opp list|show|timeline|export`.

### Screens / commands

| ID | Surface | Purpose |
|---|---|---|
| S05.1 | `/opportunities` | Ledger with state filters (open/triggered/expired/invalidated), counts, source scan |
| S05.2 | `/opportunities/:oppId` | Detail: direction, zone, conditions, invalidation, expiry, confidence, timeline |
| S05.3 | `oft opp list\|show\|timeline\|export` | Same in terminal |

### Opportunity contract (what the detail must show)

- Direction (`LONG`/`SHORT`) and price zone (low/high, tick precision).
- Trigger conditions (structured: price cross, delta threshold, time window, OI/funding conditions) — each with current evaluation state.
- Invalidation conditions and expiry.
- Confidence (0–1) and the LLM rationale (stamped `LLM`).
- Source report + scan ID + model/prompt version.
- State timeline: created → triggered/expired/invalidated, with timestamps.
- Linked trigger decision (F06) once triggered.

### States

**S05.1 Ledger**

- **Default:** tabs/counts for `open`, `triggered`, `expired`, `invalidated`; columns: ID, symbol, side, zone, trigger, confidence, age, state; state chips use neutral (open), cyan (triggered), gray (expired), red (invalidated).
- **Empty:** per-filter empty: "当前没有开放中的机会。最近一次扫描未提出机会。" vs "还没有扫描记录。" — the cause must differ. CTA to scan/report.
- **Loading:** skeleton rows; counts keep last known values with a subtle `STALE` marker until refreshed.
- **Error:** ledger storage unavailable → error panel with retry; no destructive actions.
- **Partial-data:** a condition depending on unavailable data (e.g. historical liquidation) is marked `不可用`; the opportunity is still visible but shows "该条件无法评估" and cannot be silently treated as met.
- **Success:** state transition reflected immediately with a toast and a link to the trigger/decision.
- **Permission:** none; ledger is read-only except state transitions performed by code.

**S05.2 Opportunity detail**

- **Default:** full contract above; condition rows show current value vs threshold; the timeline is chronological with provenance stamps.
- **Empty:** not-found panel; or a valid opportunity with no trigger evaluation yet ("等待价格进入区域").
- **Loading:** condition evaluation refresh; old values remain with a timestamp.
- **Error:** evaluation engine unavailable → banner "触发判定暂停" and no state change; reason + daemon link.
- **Partial-data:** condition-level `N/A` with why; expiry display uses UTC; if the source report is missing, show `N/A` with a link to the scan error.
- **Success:** triggered → banner "已触发，等待入场复核" with a link to F06; expired/invalidated → banner states the code-evaluated reason.
- **Permission:** user cannot manually trigger an opportunity (code owns triggers). A manual "invalidate" action is not in MVP scope and must not be added.

**S05.3 `oft opp`**

- **Default:** `list --state ...` table; `show` prints the contract in sections; `timeline` prints one event per line with UTC timestamps; JSON mirrors the contract.
- **Empty:** exit 0 with the correct cause sentence.
- **Loading:** short; no progress needed.
- **Error:** exit 1 with reference ID.
- **Partial-data:** condition table includes `AVAILABLE|UNAVAILABLE` per row; JSON includes `evaluable: false`.
- **Success:** exit 0.
- **Permission:** read-only; no `opp cancel` command exists in MVP.

---

# F06 — Trigger event and LLM entry re-check

**Goal:** When code decides an opportunity triggered, obtain a schema-validated LLM entry judgment, display its rationale and confidence next to the code-enforced risk result, and let the human accept or reject without ambiguity.

**PRD trace:** MVP 6 (code trigger), MVP 7 (LLM second entry judgment), module boundary (structured decision output; risk caps take the stricter side; LLM cannot call orders), Risk "LLM hallucination → schema + rejection + human confirmation", Decision "Telegram read-only" (no approval via Telegram in MVP), Risk "paper before live".

**Entry points:** Web `/triggers`, `/triggers/:triggerId`; CLI `oft trigger list|show|recheck|accept|reject`.

### Screens / commands

| ID | Surface | Purpose |
|---|---|---|
| S06.1 | `/triggers` | Decision queue: pending, accepted, rejected, failed; priority-sorted |
| S06.2 | `/triggers/:triggerId` | Review: trigger snapshot, LLM decision, CALC risk gate, accept/reject |
| S06.3 | `oft trigger list\|show` | Queue and detail |
| S06.4 | `oft trigger accept\|reject\|recheck` | Human action, with confirmation |

### Decision card anatomy (mandatory order)

1. **Trigger (CALC):** opportunity ID, symbol, condition values at trigger time, timestamp, data-quality stamp.
2. **LLM decision:** `ENTER_LONG | ENTER_SHORT | NO_ENTRY`, rationale, confidence, proposed entry/stop/target/size/leverage, model + prompt version + cost.
3. **Risk gate (CALC):** each cap (per-trade risk %, max positions, daily loss, leverage, max notional) with requested vs allowed; final size after the stricter rule; which cap bound the decision; `BLOCKED` if final size is zero.
4. **Human action:** Accept / Reject. Reject requires a reason (free text or preset). Accept is disabled when the risk gate blocked or the environment forbids it, with the reason shown.

### States

**S06.1 Trigger queue**

- **Default:** pending decisions first (count in nav badge), then resolved; each row shows symbol, side, age, confidence, risk verdict, environment.
- **Empty:** "没有待处理的入场复核。最近一次触发：…" or "还没有触发事件。" — distinguish causes.
- **Loading:** queue refresh; badge does not flash.
- **Error:** queue load failed → error panel; the kill switch remains visible.
- **Partial-data:** decision exists but risk data incomplete (e.g. leverage brackets unavailable) → risk chip `PARTIAL`; accept disabled with explanation.
- **Success:** accept/reject removes the row from pending and shows the resulting position or ledger state.
- **Permission:** Live off → accept can only create a paper position; the button says "接受（Paper）". Live on → typed confirmation before sending. Mobile → read-only, shows "请在桌面端或 CLI 处理".

**S06.2 Trigger review**

- **Default:** decision card in the mandatory order; confidence with band; cost and prompt version visible; accept/reject in the footer.
- **Empty:** not-found; or decision still generating → loading state.
- **Loading:** stages `schema validation → risk gate`; the previous decision (if any) is not shown as current. No fake streaming of rationale.
- **Error:** **LLM output invalid or refused** → decision card is red, "未执行任何订单", failing schema field shown, retry `recheck` available; manual reject available. **Provider timeout** → same, with provider and latency. **Risk gate blocked** → not an error: amber panel, final size 0, accept disabled, reject enabled.
- **Partial-data:** missing leverage brackets → conservative cap applied and labeled; stale mark price → risk gate uses last price with `STALE` and may block; funding/OI fields missing do not block entry but are marked.
- **Success:** **Accept** → order submitted to the paper engine (or live after confirmation), fills appear, timeline event, link to position. **Reject** → ledger state invalidated with reason, decision retained for review, link to journal. Confirmation copy states the final size and that the LLM cannot bypass risk caps.
- **Permission:** Live disabled → the only executable path is paper; the accept button must not pretend a live order will be sent. Paper ≥ 4 weeks is enforced before Live arming; show the gate status in the footer.

**S06.3 `oft trigger list|show`**

- **Default:** pending and resolved tables; `show` prints the decision card sections in order with `CALC`/`LLM` labels.
- **Empty:** exit 0 with cause.
- **Loading:** none for local reads.
- **Error:** exit 1.
- **Partial-data:** prints `RISK: PARTIAL — leverage brackets unavailable`; JSON includes per-cap requested/allowed.
- **Success:** exit 0.
- **Permission:** read-only.

**S06.4 `oft trigger accept|reject|recheck`**

- **Default:** `accept` prints the final size, caps applied, and environment; `reject --reason` records the reason; `recheck` creates a new decision linked to the trigger (does not overwrite the old one).
- **Empty:** ID not found / not pending → exit 1 with state.
- **Loading:** stage lines (`validating schema → risk gate → executing`).
- **Error:** schema invalid → exit 4 (blocked) with no order; execution failure → exit 1 with order reference; live disabled → exit 4.
- **Partial-data:** risk gate conservative; prints `PARTIAL` and the applied cap; accepts only if final size > 0.
- **Success:** exit 0; prints fill/position ID or rejection record; audit entry written.
- **Permission:** **Live accept** requires the environment armed **and** a typed confirmation token; `--yes` is ignored. Paper accept uses `[y/N]` default No. Reject is never gated.

---

# F07 — Position management

**Goal:** Keep open risk visible and let the LLM propose hold/partial/full/add decisions that code validates against caps and executes, with a complete timeline.

**PRD trace:** MVP 8 (scheduled/conditional LLM management advice, validated and executed by code); module boundary (risk caps stricter side; LLM cannot order directly); Risk "Live default off"; Success metric "decision consistency".

**Entry points:** Web `/positions`, `/positions/:positionId`; CLI `oft pos list|show|manage|history`.

### Screens / commands

| ID | Surface | Purpose |
|---|---|---|
| S07.1 | `/positions` | Open positions, exposure, risk utilization, PnL |
| S07.2 | `/positions/:positionId` | Detail: entry, stops, PnL, funding, management timeline |
| S07.3 | `oft pos list\|show\|manage\|history` | Terminal equivalents; `manage` requests a new LLM decision |

### States

**S07.1 Positions table**

- **Default:** columns: symbol, side, size, entry, mark, uPnL (USDT + %), R multiple, stop, target, leverage, margin, risk %, age, environment; risk utilization bar.
- **Empty:** "没有未平仓持仓。" Links to triggered opportunities and journal; explains Paper vs Live scope.
- **Loading:** skeleton rows; mark prices refresh independently with a flash, not a layout shift.
- **Error:** mark/PnL feed failed → last values with `STALE`; management actions disabled; kill switch visible.
- **Partial-data:** stale mark; funding unavailable; risk data incomplete → `PARTIAL` and conservative display; TradFi session flag; no liquidation history is not a per-position gap but is stamped where used.
- **Success:** management decision executed → row updates, toast + timeline link; partial close reduces size without reordering rows.
- **Permission:** read-only for viewing; management can be requested by any local operator; Live order sending is gated by F10g.

**S07.2 Position detail**

- **Default:** position summary; PnL breakdown (price, fees, funding); stop/target; risk caps applied at entry; management decision history (LLM proposal → CALC validation → execution); chart with entry/markers; linked signal/report.
- **Empty:** not-found; or closed position → redirect/link to journal entry.
- **Loading:** timeline + chart load independently; current position values never disappear.
- **Error:** management decision failed → red event in the timeline, position unchanged, retry; execution rejected by exchange → error with order reference; no silent retry that could double-fill.
- **Partial-data:** funding next-settlement unknown; mark stale; TradFi `NO_TRADING` regime; missing chart series marked per overlay.
- **Success:** **HOLD** → timeline entry "持有，无订单"; **PARTIAL/FULL** → fill(s), realized PnL, size update; **ADD** → only if caps allow; if blocked, amber panel shows which cap and no order is sent.
- **Permission:** Live mode shows a persistent "订单将发送至 Binance" banner; paper shows "模拟执行". The distinction is never only a color.

**S07.3 `oft pos`**

- **Default:** `list` table; `show` sections; `manage <id>` streams `LLM ›` proposal then `CALC` validation and the executed result; `history` timeline.
- **Empty:** exit 0 with "No open positions."
- **Loading:** `manage` stage lines; no decorative spinner.
- **Error:** LLM refusal/schema failure → exit 4, no order, prints reason; provider timeout → exit 1 with retry.
- **Partial-data:** JSON includes `dataQuality` and per-cap requested/allowed; prints `PARTIAL` when risk inputs are incomplete.
- **Success:** exit 0; prints fill ID, new size, realized PnL (if any).
- **Permission:** `manage` may execute without an extra prompt in paper (PRD allows automatic execution after code validation); **live** requires the environment armed and typed confirmation when the decision changes size or closes. Manual emergency close is not in MVP; the kill switch is the safety mechanism.

---

# F08 — Paper trading account

**Goal:** Inspect the simulated account that validates the loop for at least four weeks: balances, simulated orders, fills, and the reset path.

**PRD trace:** MVP 9 (paper trading with full simulated fills and accounting); Risk "paper ≥ 4 weeks before Live"; Success metric "paper validation".

**Entry points:** Web `/paper`; CLI `oft paper balance|orders|fills|equity|reset`.

### Screens / commands

| ID | Surface | Purpose |
|---|---|---|
| S08.1 | `/paper` | Account summary + simulated orders + fills |
| S08.2 | `oft paper balance\|orders\|fills\|equity` | Terminal views |
| S08.3 | `oft paper reset` | Dangerous reset with typed confirmation |

### States

**S08.1 `/paper`**

- **Default:** equity summary (total, available, margin used, unrealized, realized), daily realized PnL vs daily loss cap, orders table (new/partially filled/filled/canceled/rejected), fills table (time, symbol, side, qty, price, fee, order ID), and the paper start date/duration.
- **Empty:** "模拟账户尚未产生交易。" Display initial balance; CTA to scan and review opportunities. Do not show fake fills.
- **Loading:** summary skeleton; orders/fills load independently.
- **Error:** paper engine unavailable → error banner, last snapshot with `STALE`, trading disabled; reset disabled.
- **Partial-data:** mark price stale → uPnL `STALE`; simulated fill price vs mark mismatch noted; missing fee data shown `N/A`.
- **Success:** reset complete → new balance, reset timestamp, audit event, and a warning that the 4-week Live gate restarts.
- **Permission:** paper account is always usable when Live is off; in Live mode it remains available for comparison but is clearly labeled.

**S08.2 CLI paper views**

- **Default:** tables; `equity` prints a one-line summary; JSON/CSV available.
- **Empty:** prints "Paper account has no trades." exit 0.
- **Loading:** none (local reads).
- **Error:** exit 1 with storage reference.
- **Partial-data:** `DATA` column flags stale marks; JSON includes quality.
- **Success:** exit 0.
- **Permission:** read-only.

**S08.3 `oft paper reset`**

- **Default:** prints the consequence (orders/fills/balances cleared; Live gate restarts) and requires typed `RESET`.
- **Empty:** no account → still allow creating a clean account after confirmation.
- **Loading:** bounded operation with stage lines.
- **Error:** reset refused while positions are open unless `--force` is also typed; prints how to close first; DB error → exit 1.
- **Partial-data:** `--force` with open positions is allowed only after a second typed token; the prompt names the open positions.
- **Success:** exit 0; prints the new account ID and reset timestamp; audit entry.
- **Permission:** typed token mandatory; `--yes` is not accepted.

---

# F09 — Trade log and review

**Goal:** Close the learning loop: compare the original signal and LLM decision with what actually happened, compute PnL/win rate honestly, and read the LLM's qualitative review without letting it change rules.

**PRD trace:** MVP 13 (history, trades, PnL, win rate, signal vs outcome); MVP 8/9 (positions/paper source data); module boundary (LLM review is advisory, never auto-modifies rules); Success metric "decision consistency ≥ 70%".

**Entry points:** Web `/journal`, `/journal/:tradeId`; CLI `oft log list|show|stats|compare|export`.

### Screens / commands

| ID | Surface | Purpose |
|---|---|---|
| S09.1 | `/journal` | Trade history + stats + filters |
| S09.2 | `/journal/:tradeId` | Trade detail + signal-vs-outcome comparison + LLM review |
| S09.3 | `oft log list\|show\|stats\|compare\|export` | Terminal equivalents |

### Definitions the UI must state

- **PnL:** realized, net of simulated fees and funding; currency USDT.
- **Win rate:** closed trades with net PnL > 0 ÷ closed trades. Open trades are excluded and the count is shown.
- **R multiple:** net PnL ÷ initial risk (entry − stop × size).
- **Signal vs outcome:** proposed zone vs actual entry, trigger→fill slippage, whether target/stop was hit first, and whether the LLM decision matched the eventual rule.

### States

**S09.1 Journal list**

- **Default:** summary strip (net PnL, win rate, expectancy, avg R, sample size, period); table: trade ID, symbol, side, entry, exit, net PnL, R, duration, signal ID, decision, outcome tag; filters (period, symbol, side, outcome).
- **Empty:** no closed trades — "还没有已平仓交易。Paper 交易平仓后会出现在这里。" Links to positions/paper; do not show zeroed stats as if meaningful.
- **Loading:** skeleton; summary keeps last values with `STALE` until refreshed.
- **Error:** storage failure → error panel; export disabled.
- **Partial-data:** open trade appears as `OPEN` and is excluded from stats; missing signal snapshot (e.g. pre-MVP data) → `N/A`; data gap in outcome calculation → `PARTIAL` and excluded from win rate with a count.
- **Success:** filter applied → count announced; export ready.
- **Permission:** read-only.

**S09.2 Trade detail**

- **Default:** trade summary; timeline (signal → trigger → decision → entry → management → exit); signal snapshot; LLM decision; fills; comparison panel; `LLM` review with "仅建议，不自动修改规则" label; linked report/opportunity/position.
- **Empty:** not-found; or open trade → "交易未平仓，复盘将在平仓后生成".
- **Loading:** sections load independently; timeline placeholder without fake events.
- **Error:** LLM review failed → metrics/comparison still render; review band shows error + retry. Missing outcome data → comparison panel explains which side is unavailable.
- **Partial-data:** signal snapshot missing → `N/A`; execution fills incomplete → comparison marked `PARTIAL`; LLM review omitted if no valid decision record, with a reason.
- **Success:** review generated; export; next/previous trade navigation.
- **Permission:** review is advisory; there is no "apply rule change" action by design.

**S09.3 `oft log`**

- **Default:** `list` table; `stats --period 8w` prints definitions; `compare <trade-id>` prints signal vs outcome side by side; `export --format csv|json` for spreadsheet review.
- **Empty:** causes distinguished; stats prints "No closed trades — metrics undefined" rather than zeros.
- **Loading:** none for local reads.
- **Error:** exit 1 with reference.
- **Partial-data:** `compare` marks unavailable fields; `stats` prints excluded counts.
- **Success:** exit 0; export prints path.
- **Permission:** read-only.

---

# F10 — Settings

**Goal:** Make every consequential configuration change explicit, reviewable, and reversible where possible, with the dangerous ones (Live, kill switch, prompt restore, risk loosening) gated by confirmation.

**PRD trace:** MVP 11 (providers), MVP 12 (prompt templates, restore default, versioning), MVP 15 (budget thresholds), MVP 14 (Telegram), MVP 3 (scan period), MVP 8 (risk caps); Milestone 6 / Risks (Live off, kill switch); Decisions (risk caps stricter side).

**Entry points:** Web `/settings` and sub-routes; CLI `oft llm`, `oft prompt`, `oft risk`, `oft config`, `oft budget`, `oft telegram`, `oft live`, `oft kill`.

### Settings shell states (apply to every sub-screen)

- **Default:** section nav + form; each field shows current value, source (`default`/`user`), and last changed.
- **Empty:** a required setting has never been configured → field shows `未配置` with the consequence ("不会推送 Telegram", "无法生成 LLM 分析").
- **Loading:** config read skeleton of fixed height; no form shift when values arrive.
- **Error:** config file unreadable/corrupt → read-only error panel with backup path and repair command; do not silently reset to defaults.
- **Partial-data:** some keys valid, some not (e.g. provider key saved but test failed) → per-field status chips; saving is allowed but the consequence is shown.
- **Success:** scoped save → "已保存 × 3 项" + audit entry + effective next action (e.g. "下一个 4h 周期生效").
- **Permission:** Live settings are gated; mobile is read-only with a desktop/CLI instruction; kill switch is always active.

---

## F10a — LLM providers and models

**Route:** `/settings/llm` · **CLI:** `oft llm providers|models|test|usage`

**Screens:** provider list (OpenAI, Anthropic, DeepSeek), per-provider key (write-only), base URL override, model per role (analysis / entry re-check / management / review), temperature, timeout, concurrency; test panel; usage snapshot.
**CLI:** `oft llm providers` (configured + status), `oft llm models --provider <p>` (models + price per MTok), `oft llm test --provider <p> --role <r>`, `oft llm usage --period day|week|month`.

**States**

- **Default:** each provider row with status `未配置 / 已验证 / 验证失败`, masked key, role mapping, last test time.
- **Empty:** no provider configured → the whole LLM-dependent UI shows a global amber banner; reports/scans remain viewable but no new analysis can run.
- **Loading:** test connection stages (`auth → model → structured output`); per-provider, not global.
- **Error:** 401/403, model unavailable, schema unsupported, timeout. Error names the provider/model and the fallback that will be used.
- **Partial-data:** key valid but strict schema unsupported (DeepSeek) → `JSON mode + 1 repair retry` chip; provider tested for analysis but not for review role → per-role status.
- **Success:** `已验证 · <model> · strict schema · 1.1s`; role mapping saved; next scan uses it.
- **Permission:** keys are write-only and never returned by API/CLI, only masked; no reveal.

---

## F10b — Prompt templates

**Route:** `/settings/prompts`, `/settings/prompts/:promptId`, `/settings/prompts/:promptId/history` · **CLI:** `oft prompt list|show|edit|diff|history|restore|activate`

**Screens:** template list by role; editor with the template body, required variables highlighted (`{{symbol}}`, `{{metrics}}`, `{{schema}}`, `{{timeframe}}`, `{{knowledge}}`); diff vs active/default; version history with author/time/reason; restore default; activate version; "used by N decisions".
**CLI:** `oft prompt list`, `oft prompt show <id> [--version N]`, `oft prompt edit <id>` (opens `$EDITOR`), `oft prompt diff <id> --version N`, `oft prompt history <id>`, `oft prompt restore <id> --default`, `oft prompt activate <id> --version N`.

**States**

- **Default:** active version pinned; editor read-only until "编辑"; required variables panel; preview of the last rendered prompt (redacted).
- **Empty:** custom template never created → "使用内置默认 v1"; restore-to-default is available, not destructive.
- **Loading:** history/usage counts.
- **Error:** save validation — missing required variable, invalid syntax, empty body. Inline field error; nothing saved.
- **Partial-data:** edited but unsaved → sticky "未保存的更改" bar with discard/save; a decision was made with a now-missing version → history shows `missing` and disables replay.
- **Success:** save creates a **new version** (never edits in place); "已保存为 v4，将在下一次分析使用"; audit entry.
- **Permission:** restore default asks `[y/N]` (Web modal) and shows the replaced version; no typed token.

---

## F10c — Risk caps

**Route:** `/settings/risk` · **CLI:** `oft risk show|set|validate`

**Fields:** per-trade risk %, max concurrent positions, daily loss limit, leverage cap, max notional exposure; each with requested vs effective and "LLM proposals are clamped to these"; read-only display of current utilization.
**CLI:** `oft risk show`, `oft risk set --per-trade 0.8 --max-positions 3 ...`, `oft risk validate --scenario <json>`.

**States**

- **Default:** caps with current values, utilization bars, last change; note "取 LLM 提议与上限中更严者".
- **Empty:** not applicable — sane defaults always exist; if config is missing, show defaults with a `default` chip.
- **Loading:** validation of the prospective value; utilization refresh.
- **Error:** invalid value (negative, >100%, leverage beyond exchange bracket, daily loss < per-trade risk); inline error; block save.
- **Partial-data:** leverage brackets unavailable → cap still editable but marked "无法对照交易所分层，使用保守值"; show the affected symbols.
- **Success:** saved; a diff panel shows old → new and which decisions would have been blocked; audit entry.
- **Permission:** **loosening a cap** requires an extra confirmation modal with the diff and `[Confirm]` (not typed). Tightening needs no extra step.

---

## F10d — Scan period and schedule

**Route:** `/settings/scan` · **CLI:** `oft config get|set scan.period`

**Fields:** period (1h/2h/4h/8h/12h/24h; default 4h/8h), analysis lookback (3–7 days for volume profile/structure), timezone display, retry/backoff, concurrency.
**CLI:** `oft config get scan.period`, `oft config set scan.period 8h`, `oft config validate`.

**States**

- **Default:** current period + next run + aligned analysis window; "主窗口与扫描周期对齐" note.
- **Empty:** first run → default 4h shown as `default`.
- **Loading:** schedule recompute.
- **Error:** invalid period, lookback < main window, concurrency above a safe limit; inline error; no save.
- **Partial-data:** scheduler daemon not running → setting saves but shows "调度未生效，需启动 daemon".
- **Success:** saved; shows old/new next-run times; "下一个周期生效".
- **Permission:** no gating.

---

## F10e — Budget thresholds

**Route:** `/settings/budget` · **CLI:** `oft budget show|set|usage`

**Fields:** daily/weekly/monthly USD thresholds (defaults 10/50/150), warn at 80%, alert at threshold; no hard stop; token/cost basis explanation; current spend bars.
**CLI:** `oft budget show`, `oft budget set --daily 10 --weekly 50 --monthly 150`, `oft budget usage --period month`.

**States**

- **Default:** thresholds + current spend + projected month-end; last alert time.
- **Empty:** no usage yet → "本周期尚无 LLM 调用"; thresholds still editable.
- **Loading:** usage aggregation.
- **Error:** cost ledger unavailable → usage `N/A`, thresholds remain editable; alerting marked degraded.
- **Partial-data:** some calls missing price data (unknown model) → `unpriced` count surfaced, never silently zero.
- **Success:** saved; "达到 80% 与 100% 阈值时经 Telegram 告警；不会停止调用" (matches PRD: no hard cap).
- **Permission:** no gating.

---

## F10f — Telegram

**Route:** `/settings/telegram` · **CLI:** `oft telegram status|test|receipts`

**Fields:** bot token (write-only), chat ID, allowlist IDs, push event toggles (scan report, trigger, position, risk, budget), test message, delivery receipts.
**CLI:** `oft telegram status`, `oft telegram test`, `oft telegram receipts [--limit 50]`.

**States**

- **Default:** connected status, allowlist, push toggles, last 10 receipts (`sent/delivered/retrying/failed`).
- **Empty:** not configured → "关键事件将无法推送"; test disabled with reason.
- **Loading:** token validation + test send + receipt polling.
- **Error:** 401, chat blocked, network; precise reason; retries shown as `retrying` not `error` until exhausted.
- **Partial-data:** token valid but one chat unreachable → per-chat status; pushes to other chats continue.
- **Success:** test delivered with message ID; toggles saved.
- **Permission:** read-only bot badge; unknown chat IDs receive a refusal; no approval/write commands exist in MVP.

---

## F10g — Live toggle and kill switch

**Route:** `/settings/live` · **CLI:** `oft live status|enable|disable`, `oft kill on|off|status`

**This is the highest-consequence screen.** It must state, in order: current environment, Live gate status (paper weeks completed/required, keys, TradFi agreement, risk caps validated), kill-switch state, and the exact consequence of enabling.

**UI controls:** `Enable Live` (disabled until gate met), `Disable Live`, `Kill switch` (immediate), open-positions warning, audit log of arm/disarm/kill events.
**CLI:** `oft live status`, `oft live enable`, `oft live disable`, `oft kill` (activate), `oft kill status`, `oft kill off`.

**States**

- **Default:** Paper environment; Live gate checklist with each requirement pass/fail; kill switch `OFF`; last event.
- **Empty:** first run → Live gate shows `0/4 weeks paper`, "不可启用"; explainer link to paper.
- **Loading:** gate re-check; exchange connectivity check; open-position count.
- **Error:** Binance auth failure, TradFi agreement missing, risk caps invalid, clock drift → enabling blocked with the exact failing item; no partial arming.
- **Partial-data:** paper duration met but paper account was reset → gate restarts (count shown); leverage brackets stale → warning and conservative caps.
- **Success:** **Enable** → typed `LIVE` confirmation (Web modal or CLI prompt), then environment turns red persistently; audit entry; banner "订单将发送至 Binance". **Disable** → `[y/N]`, environment returns to Paper, open positions remain and are shown. **Kill switch on** → immediate, no confirmation, persistent red banner "KILL SWITCH ON — 不发送新订单"; does not auto-close positions (design assumption: existing positions remain; management execution is blocked until resumed). **Kill switch off** → typed `RESUME`, shows open positions and current risk.
- **Permission:** Enable requires the 4-week paper gate + armed state; `--yes` is ignored; mobile can activate the kill switch but cannot enable Live. The kill switch is available from every screen and every state.

---

# F11 — Telegram read-only channel

**Goal:** Deliver the closed loop's events to a phone, and answer read-only queries with the same numbers as the Web UI, without ever allowing a write/approval action in MVP.

**PRD trace:** MVP 14 (report push, opportunity/position/risk alerts, read-only query commands, budget alerts); Decisions "Telegram MVP read-only"; Success metric "notification reachability 100% + receipts"; Risk "approval via Telegram deferred to milestone 2".

**Entry points:** Telegram chat (bot); configuration in `/settings/telegram` (F10f); receipts in `/system` and F10f.

### Command surface

| Command | Reply |
|---|---|
| `/status` | Environment, daemon health, next scan, open positions count, budget usage |
| `/positions` | Open positions with size, entry, mark, uPnL, stop |
| `/opportunities` | Open + triggered opportunities, with IDs |
| `/report [symbol]` | Latest report summary: CALC metrics + `LLM` narrative excerpt + link/ID |
| `/budget` | Daily/weekly/monthly spend vs thresholds |
| `/watchlist` | Starred symbols grouped by type |
| `/help` | Command list + "MVP 只读" notice |

### Push events

1. Scan report summary (per completed scan, not per symbol unless configured).
2. Opportunity triggered / invalidated.
3. Entry re-check result awaiting human action (with "请在桌面端处理").
4. Position management decision executed or blocked.
5. Risk alerts (daily loss, leverage, stale data/daemon down).
6. Budget warning at 80% and threshold at 100% (no hard stop).

### States

**S11.1 Push delivery**

- **Default:** message queued with event type, chat ID, attempt count; receipt stored.
- **Empty:** no pushes yet → "首次扫描后将在此推送".
- **Loading:** `queued → sending → sent → delivered`; failures retry with backoff.
- **Error:** token invalid, chat blocked, Telegram 5xx, rate limit; receipt `failed` with reason; Web UI raises a persistent alert if pushes are failing.
- **Partial-data:** report summary carries `PARTIAL`/`N/A` stamps; never presents incomplete metrics as complete.
- **Success:** receipt `delivered` with message ID and timestamp; visible in F10f and `/system`.
- **Permission:** unknown chat/user receives a refusal ("未授权"), no data; all commands are read-only; a write-like command (`/approve`, `/accept`) replies "MVP 只读，请在桌面端或 CLI 操作".

**S11.2 Query reply**

- **Default:** concise, formatted with the same CALC/LLM labels; numbers right-aligned in a monospace code block where Telegram preserves it.
- **Empty:** `/positions` → "没有未平仓持仓。"; `/opportunities` → "当前没有开放或触发的机会。"; `/report` → "还没有报告，先运行一次扫描。"
- **Loading:** typing indicator; if the query exceeds ~5s, send a short "查询中…" then the result (Telegram has no edit guarantee for rapidly edited messages).
- **Error:** query failed → "查询失败：<reason>。请重试或查看桌面端。" with a reference ID.
- **Partial-data:** reply marks stale/N/A inline; `/report` includes data-quality and liquidation-coverage notes.
- **Success:** reply delivered; no confirmation needed.
- **Permission:** allowlist by chat/user ID; unapproved requests refused and logged; no secrets, keys, or raw prompts are ever sent.

---

# F12 — CLI surface

**Goal:** Provide full operation and configuration from the terminal, with stable machine-readable output for scripting, honest exit codes, and confirmations that match the consequence of the action.

**PRD trace:** MVP 10 (CLI full functionality), plus every MVP item as a command entry point; Decisions "Telegram read-only", "Live default off"; Risks "audit trail", "kill switch".

### Command tree

```
oft
├── init                     First-run wizard (resumable, non-interactive flags)
├── serve                    Start local Web UI/API (127.0.0.1 by default)
├── status                   Environment, daemon, next scan, positions, budget
├── doctor                   Preflight: config, keys, DB, clock, Telegram, disk
├── daemon
│   ├── start | stop | restart | status | logs
├── config
│   ├── show | get | set | edit | validate
├── watchlist
│   ├── list | add | remove | star | unstar | search | groups
├── scan
│   ├── run | status | history | show
├── report
│   ├── list | show | export | open
├── opp
│   ├── list | show | timeline | export
├── trigger
│   ├── list | show | recheck | accept | reject
├── pos
│   ├── list | show | manage | history
├── paper
│   ├── balance | orders | fills | equity | reset
├── log
│   ├── list | show | stats | compare | export
├── llm
│   ├── providers | models | test | usage
├── prompt
│   ├── list | show | edit | diff | history | restore | activate
├── risk
│   ├── show | set | validate
├── budget
│   ├── show | set | usage
├── telegram
│   ├── status | test | receipts
├── live
│   ├── status | enable | disable
├── kill
│   ├── on | off | status          (bare `oft kill` == `on`)
├── backfill
│   ├── run | status               (data.binance.vision backfill; size estimate first)
└── system
    ├── logs | info
```

### Output formats

- Default `table` when stdout is a TTY; `json` when piped is **not** automatic (avoid surprising scripts) — scripts pass `--format json`.
- `--format table|json|ndjson|csv`; `--no-color`; `--color=always`; `--ascii`; `--quiet`; `--verbose`; `--local`.
- JSON envelope: `{"ok": true, "data": ..., "error": null, "meta": {"command": "...", "generatedAt": "...", "provenance": "calc|llm|mixed", "dataQuality": "complete|partial|stale"}}`.
- LLM blocks in human output are prefixed `LLM ›`; CALC output is unprefixed. JSON blocks carry `provenance`.
- Timestamps: ISO-8601 UTC with `Z`; `--local` changes display only.

### Confirmation prompts

| Command | Prompt | Token |
|---|---|---|
| `oft live enable` | Gate checklist + warning | Type `LIVE` |
| `oft live disable` | Show open positions | `[y/N]` |
| `oft kill off` | Show risk state + open positions | Type `RESUME` |
| `oft kill on` | None | Immediate |
| `oft paper reset` | Consequence + open positions | Type `RESET` |
| `oft prompt restore` | Show replaced version | `[y/N]` |
| `oft watchlist remove` | Next-scan impact | `[y/N]` |
| `oft daemon stop` | "Open positions remain open" | `[y/N]` |
| `oft backfill run` | Estimated GB + symbol count | `[y/N]` |

`--yes` is accepted only for `[y/N]` prompts, never for typed-token prompts. Non-TTY without a required confirmation exits 2.

### States (global CLI behavior)

- **Default:** TTY human output, semantic colors, aligned tables, ISO-8601 UTC.
- **Empty:** a valid empty result exits 0 and prints the cause; it is not an error. `--quiet` suppresses the cause line but keeps the exit code.
- **Loading:** stage lines on stderr for long jobs, so stdout pipes stay clean. `--format ndjson` emits structured progress events. No decorative spinners in machine modes.
- **Error:** specific message on stderr, reference ID, next command; exit codes: `0` ok, `1` runtime, `2` usage, `3` partial, `4` blocked by risk/guard, `5` auth/config.
- **Partial-data:** exit 3 when some symbols/records succeeded; table adds a `QUALITY` column; JSON adds `dataQuality` and per-record `status`. Never collapse partial into success.
- **Success:** exit 0; prints the resulting object ID and the next command.
- **Permission:** `oft live enable` blocked before the 4-week paper gate (exit 4, explains the remaining weeks); missing keys exit 5; Telegram write commands do not exist; mobile restrictions do not apply to the CLI (the CLI is the full-control surface).

### Per-group notes

- `oft status` and `oft doctor` must work without a running daemon and without network (they report `OFFLINE`/`DEGRADED` instead of failing).
- `oft serve` binds `127.0.0.1` by default; `--host` requires an explicit warning because this is a single-user product.
- `oft backfill` downloads from `data.binance.vision`, verifies checksums, is resumable, and prints a manifest summary; it never silently skips a file.
- `oft system logs` supports `--follow`, `--since`, `--level`; LLM prompt/response bodies are redacted for secrets and marked untrusted.
- `oft kill` (activation) never asks; it prints `KILL SWITCH ON — no new orders will be sent` and the current open positions.

---

## Cross-flow traceability summary

| PRD MVP item | Flows |
|---|---|
| 1 Data ingestion + backfill | F03, F12 (`backfill`), chart quality badges |
| 2 Deterministic metrics | F04, F06, F07, F09 (CALC provenance) |
| 3 Watchlist + scheduling | F01, F02, F03, F10d |
| 4 Knowledge injection | F04 (LLM narrative + prompt version), F10b |
| 5 Periodic analysis | F03, F04 |
| 6 Opportunity ledger + trigger | F05, F06 |
| 7 Entry re-check | F06 |
| 8 Position management | F07, F10c |
| 9 Paper trading | F01, F08, F09 |
| 10 Dual entry | All flows; F12 for full CLI |
| 11 LLM provider abstraction | F01, F10a |
| 12 Prompt templates + versioning | F01, F10b |
| 13 Trade log + review | F09 |
| 14 Telegram read-only | F01, F10f, F11 |
| 15 LLM budget | F01, F10e, F11 |

---

## Design assumptions to confirm before implementation

These are UI/behavior contracts the flows depend on; none blocks Stage 3, but architecture should confirm them:

1. **Kill switch does not auto-close positions.** It blocks new order submission and all LLM-driven execution; existing positions remain and are shown. Emergency close is manual (not in MVP). If architecture chooses auto-close, the confirmation copy and states change.
2. **Live accept has a human confirmation even after arming.** PRD allows code-validated automatic execution; the design keeps a typed confirmation for the first live entry and for any live size change, because the risk is asymmetric. This can be relaxed later.
3. **Rejecting a trigger invalidates the opportunity.** The ledger state becomes `invalidated` with the rejection reason; code still owns trigger state.
4. **Paper reset restarts the Live gate.** PRD requires "at least 4 weeks paper"; resetting the account resets the evidence window.
5. **Narrative failure is not scan failure.** A metrics-only report is valid and must remain usable; the LLM band fails independently.
