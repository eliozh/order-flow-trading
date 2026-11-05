# ADR-0011: Testing and replay — deterministic fixtures, recorded data, LLM stubs, paper E2E

**Date**: 2026-11-06
**Status**: accepted
**Deciders**: architect (Stage 5); human approves at G3

## Context

The system's value depends on decisions being reproducible and on the code/LLM boundary being trustworthy. Failures are asymmetric: a duplicate order or an oversized position is far worse than a slow scan. The PRD requires replayability ("支持按历史输入重放") and a paper record before Live. Research adds that historical liquidation data does not exist, so tests must not depend on it; TradFi history is partial; provider structured-output behavior differs.

The project also has a design preview and accessibility contract that must be tested (keyboard paths, 375px read-only, dialogs, chart alternative).

## Decision

Adopt a **test pyramid with a deterministic replay core**. "Deterministic" means: fixed clock, fixed seed, recorded inputs, pinned metric/prompt versions, and no network in unit/integration tests.

### Test levels

| Level | Scope | Tools | Rules |
|---|---|---|---|
| Unit | `domain.metrics`, `domain.risk`, OMS state machine, conditions, PnL, cost math | pytest, Hypothesis | Pure, no I/O; property tests for invariants |
| Contract | JSON Schemas, API envelopes, error codes, provider output shapes | pytest + `jsonschema`, generated Pydantic | Every fixture validates; error catalog and enum stay in sync |
| Integration | SQLite/Parquet repositories, backfill conversion, scheduler ledger, gateway with a fake broker | pytest, tmp_path | Real SQLite/Parquet; no network |
| Replay | Historical window → metrics → triggers → paper fills | replay harness | Same domain code as live; fixed clock/seed |
| E2E | CLI + API + SPA flows on a paper run | pytest + HTTPX, Playwright, axe | Recorded data + LLM stubs; no real orders |
| Provider contract | OpenAI/Anthropic/DeepSeek structured output | recorded responses + optional live nightly | Schema-valid rate, refusal/timeout/truncation cases |
| Security | CSRF/origin, secret redaction, path traversal, kill switch, idempotency | pytest, Playwright | Negative cases are mandatory |

Coverage target: 80% line coverage overall, with 100% branch coverage required for `domain.risk`, `domain.trigger`, and the OMS state machine (coverage is a floor, not the goal).

### Fixtures and recorded data

- `tests/fixtures/` contains small, committed samples: a few hundred `aggTrades`, 1m/1h klines, metrics, funding, and one TradFi sample. Each file has a checksum and a README with its source (public `data.binance.vision`) and retrieval date.
- Larger windows are fetched by `scripts/fetch-fixtures` (not committed) and verified against the manifest; tests that need them are marked `@pytest.mark.fixture_large` and run in a separate CI job.
- No copyrighted book text or generated skill is committed. Knowledge tests use a small synthetic playbook with the same structure.
- Golden outputs: CVD, footprint, volume profile, OI delta, funding, and risk verdicts have expected outputs; a change requires an explicit golden update and a metric/prompt version bump where applicable.
- Time is always injected (`ClockPort`); tests use a `FixedClock` or `ReplayClock`, never `datetime.now()`.
- Randomness is seeded; the paper broker's slippage/fill seed is part of the replay run.

### LLM stubs and recorded responses

- `LLMStub` implements `LLMPort` and returns a fixture by `(role, input_hash)`.
- A recorded-response store captures real provider responses (redacted) for schema-valid, refusal, truncation, empty-content, and malformed cases. Provider adapters are tested against these.
- A **stub mode** is explicit in the decision provenance (`provider: "stub"`, `model: "fixture"`) so a paper run cannot be mistaken for a real LLM run in the journal.
- Replay by `input_hash`: if a recorded output exists, use it; otherwise, if `--live-llm` is explicitly set, call the provider and record the result; otherwise fail the replay with a clear error. Replay never silently invents an output.
- Provider nightly (opt-in) runs 100 synthetic decisions per provider and records schema-valid rate, repair rate, latency, and cost (research G7). This is not a CI gate until a baseline exists.

### Replay harness

`oft replay run --scenario <name>` (and the test helper) does:

1. Load `replay_runs` config: symbols, window, `metric_version`, `prompt_version`, LLM mode, seed, starting balance.
2. Replay events in event-time order with no lookahead.
3. Run the same `domain.metrics`, `domain.trigger`, `domain.risk`, and `PaperBroker` code as live.
4. Write to an isolated replay namespace (`replay_*` tables/IDs); never touch live orders/positions/budget.
5. Produce a diff: metrics hash, triggers, decisions, fills, PnL, risk blocks, data-quality outcomes.
6. Exit non-zero if the replay is non-deterministic (same inputs/versions produce different outputs) or if it attempts a live order path.

Replay is also the regression tool for metric changes: a new metric version is compared against the old one on a fixed window; differences are reviewed, not auto-accepted.

### Paper E2E

A full loop with recorded data and a fake clock:

- onboarding → watchlist → scheduled scan → report → opportunity → code trigger → LLM stub entry re-check → risk gate → paper accept → fill → position → management recommendation → partial/full close → journal.
- Compress 4 weeks of paper time via the fake clock; assert the Live gate opens only after the required weeks and a reset closes it.
- Failure paths: schema rejection, provider timeout, data gap, stale mark, risk block, kill switch, duplicate accept, daemon restart mid-scan, order timeout/UNKNOWN, reconciliation.
- Assertions: no duplicate `client_order_id`; no order without a risk verdict; no live order when Live is off; kill switch blocks submits; audit chain intact.

### Frontend and accessibility

- Vitest + React Testing Library for components and hooks.
- Playwright for every F01–F12 flow at 1440/768/375, keyboard-only walks, dialog focus trap/escape, status-bar skip links, chart data-table fallback, and the 375px read-only boundary (except the kill switch).
- axe-core automated checks plus one manual screen-reader pass before G6 (design A12).
- Visual regression for the chart overlays at multiple zoom levels (footprint/VP synchronization).

### CI gates

- `ruff` lint/format, `mypy` (or pyright) type check, `import-linter` boundary check.
- `pytest` unit/integration/contract/replay; coverage threshold.
- JSON Schema validation of all contract fixtures and API responses; error-catalog enum sync check.
- `npm ci`, frontend lint/type/test/build; Playwright on the built bundle.
- `pip-audit` and `npm audit`; secret scanning; license check.
- No network in unit/integration; provider tests are marked and opt-in.

## Alternatives Considered

### Alternative 1: Only unit tests and manual paper trading

- **Pros**: fast to start.
- **Cons**: cannot prove idempotency, replay, kill switch, or the LLM boundary; manual paper trading is slow and non-reproducible.
- **Why not**: the risk profile demands automated negative-path tests.

### Alternative 2: Record/replay HTTP (VCR) only

- **Pros**: realistic provider/exchange bytes.
- **Cons**: masks state-machine and timing bugs; cassettes drift; does not exercise the risk gate or OMS.
- **Why not**: VCR is useful for adapters, not as the primary strategy.

### Alternative 3: Adopt `backtesting.py` / `vectorbt` for replay

- **Pros**: mature backtest loops, metrics.
- **Cons**: bar-close models do not fit footprint/order-flow logic; `backtesting.py` is AGPL-3.0; separate execution code would diverge from live.
- **Why not**: the same domain code over event-time Parquet is more faithful and avoids a second engine.

### Alternative 4: Live testnet E2E only

- **Pros**: real exchange behavior.
- **Cons**: non-deterministic, rate-limited, limited TradFi coverage, needs keys/network; cannot run in CI.
- **Why not**: testnet is a milestone-6 validation, not the regression suite.

## Consequences

### Positive

- Replay and live share the same metric/trigger/risk code, so tests protect production behavior.
- Negative paths (duplicate orders, stale data, kill switch, schema rejection) are first-class.
- Fixtures are small, checksummed, and legally safe.
- LLM provider differences are tested without network flakiness.

### Negative

- Fixture maintenance and golden updates are real work.
- A fake clock/replay harness must be designed into the code from the start (hence ADR-0001's `ClockPort`).
- Provider nightly runs cost money and need redaction.

### Risks

- **Risk**: fixtures become stale or unrepresentative. **Mitigation**: source/date metadata, periodic refresh, and a large-window CI job.
- **Risk**: tests pass but behavior differs on live Binance. **Mitigation**: testnet validation, reconciliation tests, and a pre-Live checklist.
- **Risk**: golden updates hide regressions. **Mitigation**: review diffs, require a metric-version bump, and keep old versions for comparison.

## Compliance checks

- Every metric, trigger, risk, and OMS change has a test that fails before the change.
- Replay uses pinned `metric_version`/`prompt_version` and fails loudly on non-determinism.
- No test requires historical USDⓈ-M liquidations (they do not exist).
- The E2E paper run covers schema rejection, data gap, kill switch, duplicate accept, and restart.
- 80% coverage overall; 100% branch coverage on risk/trigger/OMS.
