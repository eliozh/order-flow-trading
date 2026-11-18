# Telegram channel fixtures

Deterministic, small JSON fixtures for the milestone-5 Telegram hardening
slice. They are the single source for the channel journey
(`tests/e2e/test_telegram_channel.py`) and the Telegram contract checks
(`tests/contract/test_telegram_contract.py`). Nothing here opens a socket, uses
a wall clock, or contains a real credential.

| File | Purpose |
|---|---|
| `updates.json` | Scripted Bot API `getUpdates` items: every F11 command, every write-like refusal, prose/fake-command text, unknown chat/user, chat-only allowlist, and an injection corpus. `expected` names the disposition: `query_reply`, `refusal`, or `ignored`. `allowlists.default`/`allowlists.chat_only` are applied verbatim by the authorization cases; `ignored` holds updates that must be ignored with no receipt and no read-model access. |
| `read_models/position.json` | One OPEN position, valid against `position.schema.json`. |
| `read_models/opportunity.json` | One OPEN opportunity, valid against `opportunity.schema.json`. |
| `read_models/budget.json` | One budget-ledger record, valid against `budget-record.schema.json`. |
| `read_models/report.json` | A stored report payload (snapshot/metrics/LLM) that `/report` renders. |
| `messages/*.json` | Message-shape records validated against `telegram-message.schema.json` (first line, units, ISO-8601 UTC, no controls/emoji/ASCII art). |
| `failures/bot_api.json` | 401/409/429/5xx/timeout Bot API failure shapes and their stable port codes. |

The updates are raw Bot API items so they can be fed to `TelegramFake`
verbatim. `chat_id`/`user_id` are synthetic (`1001`/`2002`); the token in
tests is a well-formed fake.
