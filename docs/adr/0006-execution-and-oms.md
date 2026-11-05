# ADR-0006: Execution and OMS — paper simulator, live adapter, risk gate, idempotency, kill switch

**Date**: 2026-11-06
**Status**: accepted
**Deciders**: architect (Stage 5); human approves at G3

## Context

This is the highest-consequence decision. The PRD requires paper trading with full simulated fills and accounting (MVP 9), then a Binance live adapter default-off with code-enforced risk caps and a one-click kill switch (milestone 6). Risk must take the **stricter** of the LLM proposal and the user-configured caps. The LLM has no right to place, cancel, or resize an order. The design (F06/F07/F10g) adds: a decision card with trigger → LLM → risk gate → human action; live accept requires a typed confirmation; the kill switch blocks new orders and LLM-driven execution but does not auto-close positions; paper reset restarts the 4-week Live gate.

Research adds exchange realities: Binance has request-weight limits (2400/min), order limits (1200/min, 300/10s), `429`/`418` bans up to 3 days, symbol filters (`tickSize`, `stepSize`, `minNotional`), one-way vs hedge position mode, leverage brackets that are signed-only, and a TradFi agreement that must be signed before TradFi orders.

## Decision

Implement a **single execution gateway** in front of two broker adapters with a shared state machine.

```
LLM decision ──► RiskGate (code) ──► RiskVerdict (persisted) ──► Human approval (when required)
                                                                    │
                                                                    ▼
                                                        ExecutionGateway.submit(OrderIntent, AuthContext)
                                                          ├── kill switch check (file + DB)
                                                          ├── environment/live gate check
                                                          ├── idempotency check
                                                          ├── risk verdict freshness/consistency check
                                                          ├── exchange filter normalization
                                                          ├── BrokerPort.submit
                                                          └── persist order + audit + outbox
                                                                    │
                                              ┌─────────────────────┴─────────────────────┐
                                              ▼                                           ▼
                                       PaperBroker                                 BinanceBroker
                                   (deterministic sim)                     (live / testnet, user stream)
```

### Domain objects

- `OrderIntent`: symbol, side, type, quantity or quote amount, limit/stop price, reduce-only, time-in-force, `decision_id`, `risk_verdict_id`, `approval_id`, `intent_hash`.
- `RiskVerdict`: `ALLOW` | `REDUCE` | `BLOCK`; requested vs allowed size/leverage/notional; binding cap; input snapshot hash; computed_at; TTL (30 s default); stale inputs list. Persisted before any submit.
- `Order`: the internal state machine: `CREATED → SUBMITTED → PARTIALLY_FILLED → FILLED`, with `CANCELED`, `REJECTED`, `EXPIRED`, `UNKNOWN` terminal/exceptional states.
- `Fill`, `Position`, `TradeLog`: as defined in `docs/contracts/`.

### Risk gate

The risk gate is deterministic code in `oft.domain.risk`. It recomputes from the current account state and market snapshot; it does not trust the LLM's numbers.

Caps (all user-configurable, with conservative defaults):

| Cap | Default | Enforcement |
|---|---|---|
| Per-trade risk | 0.8% of equity | `risk_amount = |entry − stop| × size`; size reduced to fit |
| Max concurrent positions | 3 | New entry blocked when at limit |
| Daily loss limit | 3% of equity | New entries blocked after breach; management closes allowed |
| Leverage cap | 5× | Clamped to user cap and exchange bracket (whichever is lower) |
| Max notional exposure | 20% of equity | Size reduced; block if below min notional |
| Stop required | Yes | No entry without a stop |
| Max slippage / spread | 25 bps / 15 bps | Block if the current book exceeds the threshold |
| Stale mark | 60 s | Block if the mark is older than the budget |
| Min notional / step / tick | exchange filters | Normalize; reject if below min |

`effective_size = min(LLM_proposal, per_trade_cap, notional_cap, leverage_cap, exchange_max)`; the verdict records which cap bound it. A zero/negative size is `BLOCK`. The gate also rejects intents whose `decision_id` is not `VALIDATED`, whose `risk_verdict_id` is missing/expired, or whose price moved beyond a configured tolerance since the verdict.

### Paper broker

- Starts from a configured balance (`paper_accounts.initial_balance`, default 10,000 USDT).
- Market orders fill at the last trade/mark plus a configurable slippage (default 2 bps) and taker fee (default 5 bps); limit orders fill when a trade prints through the limit; maker fee (default 2 bps) when the limit is not immediately crossed.
- No queue-position modeling in MVP; partial fills are optional and off by default. Funding is accrued at settlement times from the recorded funding rate.
- Deterministic with a fixed seed and a `ReplayClock` for tests. The same `BrokerPort` and the same state machine as live.
- Every order/fill/position/PnL mutation is written in one SQLite transaction with an audit event.

### Live broker

- `binance-connector-python` for REST; a thin own WS client for the user-data stream (routing paths per research §A1). No ccxt.
- One-way position mode and isolated margin for MVP; document the limitation. Leverage is set per symbol, clamped to the signed leverage bracket; fetch brackets once per symbol with the user key and cache.
- Normalize every order to `tickSize`/`stepSize`/`minNotional`; set `reduceOnly` for exits; use `newClientOrderId` as the idempotency key.
- Reconcile on startup, after reconnect, and periodically (60 s): open orders, positions, balances, recent fills. Unknown exchange state blocks new orders until reconciliation succeeds (`RECONCILIATION_REQUIRED`).
- Testnet is the first live-like target (`demo-fapi.binance.com`); TradFi test coverage is limited (46 symbols) and must be validated before live TradFi.
- TradFi: require the signed stock-contract agreement; treat `NO_TRADING` as a liquidity/regime flag, not a halt (research §A2); surface it in the UI.

### Idempotency and duplicate-order prevention

Three independent layers:

1. **API idempotency key** for the human/CLI action (`accept`, `manage`, `reset`), stored in `idempotency_keys`.
2. **Deterministic `client_order_id`**: `oft-{env}-{symbol}-{purpose}-{hash(decision_id|position_id|intent_hash)[:16]}`. The same logical intent always produces the same ID. `orders.client_order_id` is `UNIQUE`; the exchange also rejects a duplicate active `newClientOrderId`.
3. **Reconcile-before-retry**: on timeout/unknown, query the exchange by `client_order_id` and/or user stream before resubmitting. Never blind-retry a submit. Persist `UNKNOWN`, block new orders for that position, and alert.

The gateway is the only writer of `orders`; a SQLite lease (`job_locks` or `system_state`) ensures only one daemon instance may submit.

### Live arming gate

`live_armed` is false by default. Enabling requires all of:

- at least 4 consecutive weeks of paper records (reset restarts the counter);
- valid Binance keys with futures trading enabled and withdrawal disabled;
- TradFi agreement signed if any TradFi symbol is starred;
- risk caps validated against exchange brackets;
- clock offset within tolerance;
- typed `LIVE` confirmation in the UI/CLI (`--yes` is ignored);
- an audit event.

Disabling requires `[y/N]`; open positions remain and are shown. The kill switch is independent of arming.

### Kill switch

- State is written to both `system_state.kill_switch` and a sentinel file `$OFT_DATA/run/KILL_SWITCH` (0600). The gateway checks the file before every submit and before every retry; if the daemon restarts, the file is still there. Activation is immediate, prompts for nothing, and is available from every screen/state.
- It blocks new orders and all LLM-driven management execution. Risk-reducing actions (canceling a resting order, a reduce-only exit) remain allowed and audited; the gateway classifies each intent as risk-increasing or risk-reducing before applying the kill switch. It does **not** auto-close positions (design assumption 1). Closing a position while the kill switch is on requires a future explicit manual-close feature (not MVP); the operator can act directly in Binance if needed. This is stated in the UI.
- Deactivation requires typed `RESUME`, shows open positions/risk, and writes an audit event.

## Alternatives Considered

### Alternative 1: Adopt Freqtrade or Hummingbot for execution

- **Pros**: mature order management, exchange adapters, dry-run/live modes.
- **Cons**: Freqtrade is GPL-3.0 and strategy-shaped; Hummingbot is market-making/arbitrage-shaped; neither understands order-flow semantics or the PRD's risk gate; integrating the LLM boundary would fight their architecture.
- **Why not**: the OMS is small and must be exactly the documented state machine. Build it thin on the official connector.

### Alternative 2: Direct exchange calls from the API/CLI/LLM adapter

- **Pros**: fewer layers.
- **Cons**: multiple order paths, no single risk gate, no single idempotency point, no audit guarantee. It is the exact failure mode the PRD forbids.
- **Why not**: structurally unsafe. The gateway is mandatory.

### Alternative 3: Use ccxt for the exchange adapter

- **Pros**: broad exchange coverage, familiar API.
- **Cons**: WS streaming is ccxt.pro (commercial); extra abstraction for a single venue; weaker control over Binance-specific fields (TradFi, user stream, filters).
- **Why not**: official connector + thin WS gives more control and fewer dependencies.

### Alternative 4: Auto-close positions on kill switch

- **Pros**: immediate risk reduction.
- **Cons**: can realize losses at the worst moment; can fail on a disconnected exchange; the design explicitly assumes positions remain; a partial failure would leave a more confusing state.
- **Why not**: not in MVP. The kill switch's contract is "no new orders", and the UI must say so. Manual flatten is a future, explicit feature.

## Consequences

### Positive

- One choke point for authorization, risk, idempotency, kill switch, and audit.
- Paper and live share the state machine and contracts; paper evidence is meaningful.
- The risk gate's "stricter of" rule is explicit and testable.
- Unknown states are handled conservatively; no blind retries.

### Negative

- The gateway is a single point of failure; if it is down, no orders execute (which is the safe direction).
- One-way/isolated MVP limits some advanced position management.
- Live reconciliation and exchange quirks are real operational work.

### Risks

- **Risk**: double order on timeout. **Mitigation**: deterministic client order ID, unique constraint, reconcile-before-retry, UNKNOWN block, single daemon lease.
- **Risk**: stale risk inputs allow an oversized order. **Mitigation**: 30 s verdict TTL, re-check at submit, price-move tolerance, stale-mark block.
- **Risk**: kill switch not honored after a crash. **Mitigation**: file sentinel + DB state, checked before every submit/retry, tested in E2E.
- **Risk**: exchange rate-limit ban. **Mitigation**: weight budget, exponential backoff, no retry storms, order-rate limiter, `429`/`418` handling with alert.
- **Risk**: paper fills unrealistically optimistic. **Mitigation**: configurable slippage/fees, conservative defaults, and a published fill-model description in the journal.

## Compliance checks

- The LLM adapter cannot import the gateway/broker (ADR-0001).
- Every order has a `risk_verdict_id`, `approval_id` (when required), and `client_order_id`.
- The risk gate recomputes and records requested vs allowed; the stricter rule is tested.
- The kill switch is checked in `submit` and in every retry; its file survives restart.
- Live is off by default; arming requires the 4-week gate and typed `LIVE`.
