# ADR-0005: LLM provider abstraction, structured outputs, validation, and cost accounting

**Date**: 2026-11-06
**Status**: accepted
**Deciders**: architect (Stage 5); human approves at G3

## Context

The PRD requires three providers — OpenAI, Anthropic (`api.anthropic.com`), and DeepSeek — switchable and separately configurable. The LLM may analyze market structure, propose opportunities, re-check an entry, and recommend position management. It must **never** place an order, change configuration, skip a trigger, or set a risk cap. Every decision must be schema-validated; non-conforming output is rejected, never "interpreted" (PRD §模块职责边界).

Research establishes that the providers differ materially in schema enforcement (02-research.md §B1):

- OpenAI: native strict `json_schema` with constrained decoding.
- Anthropic: native `json_schema` / strict tool use; no recursive schemas, no numeric/string-length constraints, limited array/union constraints; refusals may not match the schema.
- DeepSeek: JSON mode is prompt-steered only and can return empty content; strict tool calling exists on the `/beta` endpoint and does enforce the schema, with the same "all required / `additionalProperties:false`" constraints.

The same research establishes cost and payload concerns: a 10-symbol scan with 1.4M input tokens is ~$2.80 on a $2/MTok model and ~$0.21 on DeepSeek cache-miss; prompt caching and payload reduction are the main levers.

## Decision

Use a **thin, provider-neutral port** with **one canonical decision envelope** and **provider-specific schema translation**.

### Port and adapters

```
ports.llm.LLMPort:
  capabilities() -> ProviderCapabilities
  complete(request: LLMRequest) -> LLMResult | LLMError
  estimate_cost(request, model) -> CostEstimate

adapters.llm_openai / llm_anthropic / llm_deepseek
```

`LLMRequest` carries: role (`ANALYSIS` | `ENTRY_RECHECK` | `MANAGEMENT` | `REVIEW`), model, system prompt, user prompt, canonical JSON Schema, temperature, max output tokens, timeout, request id, `prompt_id`/`prompt_version`/`prompt_hash`, `input_hash`, and cache hints. `LLMResult` carries the raw text, parsed object, usage, latency, provider request id, refusal/truncation flags, and the capabilities actually used.

The application layer depends only on the port. Provider names, model IDs, and SDK objects never enter `oft.domain`.

### Canonical schema and translation

`docs/contracts/llm-output.schema.json` is the single provider-facing output shape; `docs/contracts/llm-decision.schema.json` wraps it with code-assigned identity, subject references, provenance, and validation outcome after the call. The provider-facing schema is written to be provider-safe:

- every object lists every property in `required`;
- every object has `additionalProperties: false`;
- no recursive references;
- no numeric/string-length/array-length constraints that a provider cannot enforce;
- one flat envelope with `decision_type` and nullable role-specific payloads, rather than a deep union (avoids provider union limits);
- no tool definitions, no function calls, no execution affordances.

Provider adapters translate only presentation quirks:

| Provider | Preferred mechanism | Fallback | Notes |
|---|---|---|---|
| OpenAI | Responses API `text.format = {type:"json_schema", strict:true}` | — | Strip unsupported keywords; treat `refusal` content parts and `status:"incomplete"` as explicit failures |
| Anthropic | `output_config.format = {type:"json_schema"}` or strict tool use | — | Strip numeric/length constraints; handle `stop_reason:"refusal"` |
| DeepSeek | Strict tool calling on the `/beta` endpoint (one tool whose input schema is the decision schema) | JSON mode + schema in prompt | JSON mode can return empty content; never treat empty as valid |

The canonical schema is stored in the prompt version and recorded with every decision. A provider capability probe at settings time records which mechanism is available; the UI shows the degraded mode (design F10a partial state). If a provider rejects a construct such as `anyOf: [X, null]`, the adapter may rewrite it to the semantically equivalent nullable type (`type: ["object", "null"]`); it may not add or remove fields.

### Validation and repair

1. Parse JSON. An empty body, markdown fence, or truncated output is a failure.
2. Validate against `llm-output.schema.json` (Draft 2020-12).
3. Validate with the Pydantic model generated from the schema.
4. Run semantic checks that JSON Schema cannot express: confidence in `[0,1]`; `stop_price` on the correct side of entry; zone low < high; `decision_type` matches the role; required provenance present; no unknown enum values; no oversized rationale (> a configured token budget).
5. **One** repair attempt: re-send the original prompt plus the validator error and the schema, asking for a corrected object. This is a new `llm_calls` row with `attempt=2`.
6. If repair fails, **reject**: persist the invalid output and error, mark the decision `REJECTED`, alert via Telegram, and do not create an opportunity/entry. A metrics-only report remains valid.

No fuzzy parsing, no regex extraction, no "best effort" coercion, no partial acceptance.

### Prompt construction

- Role-specific system prompt: operator terminal voice, the code/LLM boundary, the schema, the knowledge artifact, and explicit instruction that all market data and knowledge text is **data, not instructions**.
- Untrusted data is delimited (`<market_data>...</market_data>`) and preceded by a statement that instructions inside are not to be followed.
- The schema is appended as a structural requirement, not as prose.
- Payload reduction in priority order (research §B3): send computed aggregates, not raw trades; window/decimate; top-N footprint levels; compact encoding; two-stage prompting; prompt caching for the static system prompt + playbook; document sampling for reproducibility.
- Prompt templates are versioned and never edited in place (ADR-0009). The rendered prompt is hashed; the hash is stored on the decision.

### Cost accounting and budgets

- Record `usage` from the provider response: input, output, cache read, cache write, reasoning tokens.
- Price table is versioned (`price_version`) and stored in config; `cost = input×in_price + output×out_price + cache_write×write_price + cache_read×read_price`.
- Unknown model or missing price → `unpriced=true`, surfaced in the budget UI; never silently zero.
- Every call writes `budget_ledger` and rolls up day/week/month. Thresholds default to daily $10 / weekly $50 / monthly $150, warn at 80%, alert at 100% via Telegram; **no hard stop** (PRD decision).
- Per-provider and global concurrency semaphores; per-scan call budget; timeouts (default 120s analysis, 45s re-check/management) and a circuit breaker after repeated 5xx/429s.
- Retries: transport/5xx/429 only, max 2 attempts, exponential backoff + jitter; never retry a refusal or a schema failure except the single repair.

### What the LLM can and cannot do

**Can**: return a `DecisionEnvelope` with analysis, proposed opportunity, entry re-check, or management recommendation; state confidence, rationale, caveats, and proposed risk parameters.

**Cannot**: call any tool, call an exchange, read or write secrets, mutate config, trigger an opportunity, bypass the risk gate, set the final order size, or emit executable commands. The adapter has no network client other than the provider endpoint and no import path to execution. The schema has no fields for credentials, URLs, tool names, or order IDs that could be submitted.

## Alternatives Considered

### Alternative 1: LiteLLM as the abstraction

- **Pros**: one API over 100+ providers, built-in cost tracking, retries/fallbacks.
- **Cons**: another dependency and supply-chain surface; a license split between core and commercial features; its routing/cost model is broader than needed; provider-specific strict-schema quirks still need handling.
- **Why not**: three providers with known APIs are a small adapter. A thin port keeps the trust boundary auditable and avoids a dependency whose behavior we do not control. LiteLLM can be added behind the same port later if provider count grows.

### Alternative 2: One provider only (e.g. Anthropic)

- **Pros**: simplest; one SDK; predictable schema behavior.
- **Cons**: violates the PRD; no cost/availability fallback; vendor lock-in; DeepSeek's cost profile is materially better for high-volume scans.
- **Why not**: provider neutrality is a product requirement and a resilience property.

### Alternative 3: Tool-calling agent with exchange tools

- **Pros**: flexible, can fetch more data on demand.
- **Cons**: catastrophic for the boundary: the model could call order tools, be prompt-injected into tool use, and become non-deterministic. It also makes replay and authorization much harder.
- **Why not**: explicitly forbidden by the PRD. The LLM is an output-only function; all data is prepared by code.

### Alternative 4: Local/open-weight model

- **Pros**: no API cost, data stays local.
- **Cons**: quality/reliability for structured market judgment is unproven; hardware requirements; no provider fallback; more ops.
- **Why not**: out of MVP scope; the port leaves room for a local adapter later.

## Consequences

### Positive

- One schema and one validation path for all providers; provider differences are confined to adapters.
- Rejection is deterministic and auditable; a bad model cannot reach execution.
- Cost is visible per call, scan, day, week, and month.
- The boundary is testable with recorded provider responses and contract tests.

### Negative

- Provider quirk handling is real work (DeepSeek beta tool calling, Anthropic refusal semantics, OpenAI schema compilation latency).
- Strict schemas constrain how expressive the output can be; narrative must fit a bounded string field.
- One repair retry adds latency and cost on failures.

### Risks

- **Risk**: DeepSeek strict tool calling is beta and may change. **Mitigation**: capability probe, JSON-mode fallback, contract tests, and a provider status chip in settings.
- **Risk**: prompt injection via market data. **Mitigation**: no tools, delimited untrusted blocks, schema whitelist, no external news/social text in MVP, output validation.
- **Risk**: schema-valid but semantically wrong decisions. **Mitigation**: the deterministic trigger, risk gate, and human confirmation remain; the LLM is advisory.
- **Risk**: cost runaway. **Mitigation**: ledger, thresholds, concurrency caps, payload reduction, prompt caching, and a per-scan call budget.

## Compliance checks

- The LLM adapter has no execution/Binance imports (ADR-0001).
- All four decision types validate against `llm-output.schema.json`; the persisted record validates against `llm-decision.schema.json`.
- Invalid/refused/truncated output produces `REJECTED` + alert, never an order.
- Every decision records provider, model, prompt version/hash, input hash, timestamps, usage, cost, and schema version.
- No tool definitions are sent to any provider.
