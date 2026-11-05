# ADR-0010: Security and secrets — local trust boundaries, key handling, and threat model

**Date**: 2026-11-06
**Status**: accepted
**Deciders**: architect (Stage 5); human approves at G3

## Context

The product is a single-user, single-machine system that holds Binance trading credentials, three LLM provider keys, and a Telegram bot token, and can move real money when Live is armed. It has no account system and no multi-tenant boundary, but it does have a browser origin, a local HTTP API, a CLI, a filesystem, and three external services. The PRD and design require: keys never in the repo, Live default-off, a kill switch, full audit, Telegram read-only, and every LLM input treated as untrusted.

A local-only product is not automatically safe: any web page the operator visits can attempt requests to `127.0.0.1`, malware can read a world-readable data directory, and a dependency can exfiltrate keys. The threat model must be explicit even at this scale.

## Decision

Adopt a **local zero-trust-at-the-boundary** posture: every external input (market data, LLM output, Telegram commands, browser requests, files) is validated; every secret is isolated; every money-moving action is authorized in code; every consequential event is audited.

### Assets

1. Binance API key/secret (trading permission; ideally no withdrawal).
2. OpenAI/Anthropic/DeepSeek API keys.
3. Telegram bot token and allowlist.
4. Trading capital and open positions.
5. Ledger/audit integrity (orders, fills, positions, PnL, decisions).
6. Private knowledge artifacts derived from copyrighted books.
7. The local machine and its filesystem.

### Trust boundaries

```
[Binance REST/WS]  [LLM providers]  [Telegram]     <- untrusted external networks
        |                |               |
        +----------------+---------------+-------> [oftd daemon]
                                                        |
                          +-----------------------------+-----------------------------+
                          |                             |                             |
                    [SQLite/Parquet]              [local API 127.0.0.1]          [filesystem/secrets]
                                                        |
                                              [browser]        [CLI]
```

- Market data and LLM output cross into the daemon as **data**, never as code or instructions.
- The browser and CLI are clients; neither has exchange credentials.
- The daemon is the only component with secrets and the only component that can submit orders.

### Secret storage

Preferred, in order:

1. **OS keyring** (Secret Service / macOS Keychain / Windows Credential Manager) via `keyring`. Secrets are referenced by name in `config.toml`; values never appear there.
2. **Encrypted file** `$OFT_DATA/secrets.enc` (0600), encrypted with a key stored in the OS keyring. If the keyring is unavailable, the daemon requires `OFT_MASTER_KEY` from the environment or an interactive unlock at startup; it refuses to start unattended with plaintext secrets.
3. **Environment variables** for CI/dev only; documented as less safe; never written to disk by the app.

Rules:

- `config.toml` stores only secret **names**, never values.
- Secrets are never written to SQLite, Parquet, logs, error messages, prompts, reports, or Telegram. `secrets_meta` stores only a fingerprint and last-4.
- The API returns only masked values; there is no reveal endpoint (design F10a permission state).
- Logging uses a redaction filter for key-like strings, `Authorization`, `X-MBX-APIKEY`, cookies, and prompt bodies.
- Key rotation is a first-class action; the old key is revoked at the provider after the new one is verified.
- Binance keys: futures trading enabled, **withdrawal disabled**, IP allowlist where possible, Ed25519/RSA self-generated keys preferred. Testnet keys are separate.
- LLM keys: one per provider; base URL overrides are restricted to `https` and a user-confirmed host; no arbitrary proxy.

### Local API and browser security

- Bind `127.0.0.1` by default. `--host` requires an explicit warning; the supported deployment is loopback only.
- Browser session: random token in an `HttpOnly`, `SameSite=Strict`, `Secure` (when HTTPS) cookie; CSRF token required on all mutations; `Origin`/`Sec-Fetch-Site`/`Host` checks; no CORS headers.
- CLI: bearer token in `$OFT_DATA/run/api-token` (0600) or `OFT_API_TOKEN`; token comparison is constant-time; tokens are rotated on demand.
- SSE: same-origin only; no credentials in query strings; connection limits.
- CSP: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'`.
- No `unsafe-inline`/`unsafe-eval`; no third-party CDN; no remote fonts (system fonts).
- The SPA never receives exchange/LLM/Telegram secrets.
- The kill switch and `/health` remain reachable in every UI state; the kill switch is not gated by a dialog.

### Filesystem and process

- `$OFT_DATA` is `0700`; secrets/API token are `0600`; logs `0600`.
- systemd hardening: dedicated user, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only` with only `$OFT_DATA` writable, `PrivateTmp`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `LockPersonality`, `MemoryDenyWriteExecute` (drop the last one if a pinned dependency requires executable memory; verify during packaging).
- Backups are encrypted and stored outside the repo; restore is tested.
- Exports (report/journal) write only to a user-specified path; no path traversal; filenames sanitized.

### Audit trail

`audit_events` is append-only with a hash chain (`prev_hash` + `hash`). Recorded events include: startup/shutdown, config changes, key add/rotate/delete, live arm/disarm, kill switch on/off, every order intent/verdict/submit/fill/cancel, risk blocks, prompt version activation/restore, paper reset, backfill start/finish, Telegram allowlist changes, and schema rejections. The chain is verified at startup and by `oft doctor`; a broken chain is a critical alert.

### Threat model

| # | Threat / vector | Impact | Control(s) in this architecture | Residual | Verification |
|---|---|---|---|---|---|
| T1 | **Prompt injection via market data**: a symbol name, trade, or metadata field contains instructions | Model produces malicious proposal or leaks data | Market data is delimited as data; system prompt states precedence; no tools; output schema whitelist; no external news/social text in MVP; control-character stripping and length caps; reject + alert on anomalous output | A schema-valid but misleading proposal can still occur | Injection corpus tests; schema tests; manual red-team fixtures |
| T2 | **Prompt injection via knowledge/book text** | Same as T1 | Knowledge treated as data; delimited; compiled/reviewed artifact; no tools; section IDs; human approval | A malicious book can bias judgment | Review + injection tests |
| T3 | **LLM hallucination / invalid output** | Wrong trade or bad narrative | Strict schema + Pydantic + semantic checks; one repair; reject + alert; metrics-only report; risk gate and human confirmation remain | Semantically wrong but valid output | Contract tests per provider; 100-decision schema-valid rate spike (research G7) |
| T4 | **LLM reaches the order path** | Unauthorized order | LLM adapter has no execution/Binance imports; no tools; schema has no order/credential fields; `ExecutionGateway` requires a persisted `risk_verdict_id` + approval; import-linter + runtime capability check | A future code change could add an import | CI import-linter; architecture test; E2E |
| T5 | **Double order / retry storm** | Double fill, excess exposure | Deterministic `client_order_id`; unique DB constraint; API idempotency key; reconcile-before-retry; `UNKNOWN` blocks; one daemon lease; exchange-side duplicate ID rejection | Exchange may accept two distinct intents if IDs differ | Fault-injection tests; reconciliation tests |
| T6 | **Risk cap bypass** | Oversized loss | Caps in code, stricter-of rule, re-check at submit, verdict TTL, exchange brackets, min/max filters, kill switch | A bug in the risk gate | Property tests: never exceed caps; golden scenarios |
| T7 | **Stale data drives a decision** | Bad entry/exit | Freshness budgets; `STALE` blocks risk; clock check; snapshot hash; no trigger on stale snapshot; UI stamps | Short window of undetected staleness | Staleness tests; clock-drift tests |
| T8 | **Local web page CSRF / DNS rebinding** | Unauthorized config/order action | Loopback bind; Host/Origin/Sec-Fetch checks; SameSite=Strict HttpOnly session; CSRF token; no CORS; CSP | Malware/browser extension with local access | CSRF tests; origin tests; Blackhole/DNS-rebinding test |
| T9 | **Secret leakage** | Key theft, fund loss | Keyring/encrypted file; 0600; no secrets in logs/prompts/DB/API; redaction; masked display; no reveal | Memory dump / compromised OS | Secret-scan in CI; log-redaction tests; filesystem audit |
| T10 | **Dependency / supply-chain compromise** | Code execution, key theft | Lockfiles; pinned versions; minimal deps; review new deps; `pip-audit`/`npm audit`; hashes; no postinstall from untrusted packages; official Binance connector; license review (MIT/Apache; avoid GPL for distributed code) | A compromised transitive dep | CI audit; SBOM; dependency review |
| T11 | **Kill switch fails or is bypassed** | Continued orders during an incident | File sentinel + DB state; checked in gateway before every submit/retry; survives restart; no prompt; tested from every UI state; audit event | A direct exchange action outside the app | E2E kill-switch tests; restart test |
| T12 | **Stale/duplicate market event** | Wrong metrics/trigger | Event ID/sequence checks; dedupe; gap detection; quality stamps; recompute last N bars | Archive corrections after the fact | Golden data tests; gap detection tests |
| T13 | **Telegram write abuse** | Unauthorized action | MVP read-only; allowlist by chat/user; unknown chats refused and logged; no `/approve`/`/accept`; no secrets in messages | Bot token theft | Command tests; allowlist tests |
| T14 | **Log injection / sensitive logging** | Forged log lines, secret exposure | Structured JSON logs; control-character escaping; redaction; no raw prompt bodies by default | Operator copies a log elsewhere | Log tests; redaction tests |
| T15 | **Backup/export exposure** | Data/secret leak | Encrypted backups; 0600; export path validation; no secrets in exports | User shares an export | Restore/export tests |
| T16 | **Clock manipulation / drift** | Wrong window, duplicate fills | Binance server time check; NTP; drift > 5 s warn, > 60 s block live; stale stamps | OS clock compromise | Clock tests |
| T17 | **Audit tampering** | Lost accountability | Hash chain; file permissions; startup verification; backups | Operator with root can rewrite | Chain-verification tests |
| T18 | **Malicious archive file** | Path traversal, zip bomb, code execution | Download to a new empty dir; verify checksum; size limits; stream extraction; no execution of archive contents; parse with fixed schemas; run converters with least privilege | A parser vulnerability | Fuzz/extraction tests |
| T19 | **Race between scan and execution** | Order based on a stale opportunity | Trigger snapshot hash; verdict TTL; re-check at submit; state machine transitions | Timing edge cases | Concurrency tests |
| T20 | **Live arming by mistake** | Real loss | Default off; 4-week paper gate; typed `LIVE`; audit; red persistent UI; `--yes` ignored | Operator intentionally arms | Gate tests; CLI tests |

### Incident response

- `oft kill` is the first action for any order-path uncertainty; it never prompts.
- `oft doctor` reports kill-switch state, clock, disk, DB integrity, audit chain, WS/Telegram health, and stale runs.
- A reconciliation failure blocks new orders; the runbook says to reconcile or act directly in Binance.
- Key compromise: revoke at the provider, rotate in settings, audit the window, review orders.
- A broken audit chain or corrupt DB triggers a restore from the latest verified backup; trading stays disabled until verified.

## Alternatives Considered

### Alternative 1: No local auth (loopback only)

- **Pros**: simplest.
- **Cons**: any web page or local process can POST to `127.0.0.1`; DNS rebinding can bypass naive origin assumptions.
- **Why not**: CSRF/origin controls and a session token are cheap and materially reduce the local attack surface.

### Alternative 2: Full account system / multi-user auth

- **Pros**: future-proof for hosted use.
- **Cons**: explicitly out of scope; adds password/session/reset complexity and a false sense of multi-tenant security.
- **Why not**: single-user local is the product. The boundary is the machine, not an account.

### Alternative 3: Plaintext `.env` only

- **Pros**: familiar, simple.
- **Cons**: secrets on disk in clear text; easy to leak via backups, screen shares, or a repo mistake.
- **Why not**: keyring/encrypted storage is a small amount of code for a large risk reduction. `.env` remains a documented dev fallback only.

### Alternative 4: Cloud secret manager

- **Pros**: strong access control, audit.
- **Cons**: network dependency at startup; sends a signal to a cloud; contradicts local-first; another account.
- **Why not**: the OS keyring is the right local primitive. A cloud manager can be an optional adapter later.

## Consequences

### Positive

- One place to authorize orders; one place to store secrets; one audit chain.
- The threat model is explicit and testable, not implicit.
- The local browser surface is hardened against the realistic local attacks.
- Key rotation and restore are documented operations.

### Negative

- The OS keyring may be unavailable on headless Linux; the encrypted-file fallback adds an unlock step.
- Session/CSRF machinery is more code than a naive localhost app.
- Hash-chained audit adds a write per event; acceptable at this scale.

### Risks

- **Risk**: keyring locked at boot prevents unattended restart. **Mitigation**: document the encrypted-file + `OFT_MASTER_KEY` deployment, or unlock interactively; Live refuses to arm without a successful secret load.
- **Risk**: a compromised OS defeats all controls. **Mitigation**: out of scope; document the trust assumption (the machine is trusted).
- **Risk**: dependency compromise. **Mitigation**: lockfiles, audit, minimal deps, and the import boundary.

## Compliance checks

- No secret value in `config.toml`, SQLite, Parquet, logs, prompts, reports, or API responses.
- Mutations require CSRF + origin checks; API requires a bearer token or session.
- Every order has a persisted risk verdict and audit event; the kill switch is checked before submit.
- Live is off by default and requires the 4-week gate + typed `LIVE`.
- `books/`, `skills/`, and `playbook/` are gitignored; exports warn before sharing.
- CI runs secret scanning, dependency audit, import-linter, and contract tests.
