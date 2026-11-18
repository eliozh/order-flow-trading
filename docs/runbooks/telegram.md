# Telegram channel runbook

The Telegram bot is optional, read-only, and loopback-adjacent: it never places
orders, approves trades, or calls an LLM. Query replies and pushes are bounded
plain text; the bot token is stored through the secret backend and is only ever
rendered as a mask.

The seven executable bot commands are `/status`, `/positions`,
`/opportunities`, `/report [SYMBOL]`, `/budget`, `/watchlist`, and `/help`.
Write-like tokens (`/approve`, `/accept`, `/reject`, `/cancel`, `/buy`, `/sell`,
`/close`) are refused with a fixed read-only message. Only a chat id **and** a
user id that both appear in the allowlists may query; every other sender gets
the fixed `未授权` refusal before any read-model access.

## Configure

```bash
# The token is accepted only on stdin; there is no reveal command.
printf '%s\n' "$TELEGRAM_BOT_TOKEN" | oft telegram token set --stdin
oft telegram allowlist add --chat-id 1001 --user-id 2002
oft telegram toggles show
oft telegram test
oft telegram receipts --limit 20
```

Pushes require exactly one positive (private) chat id in the allowlist. Zero or
several private ids fail closed and push nothing. `oft telegram status` reports
`NOT_CONFIGURED`, `NO_ALLOWLIST`, `READY`, or `DEGRADED` from stored state only;
it never performs a network probe.

## Watchdog: systemd `OnFailure=`

The watchdog is a one-shot command, not a daemon and not started by
`oft daemon start`. It probes the local daemon and sends at most one alert per
outage plus one recovery after a prior outage. The outage epoch is durable in
`system_state`, and the run holds an exclusive lock, so repeated or overlapping
runs are no-ops.

`oft-telegram-watchdog.service`:

```ini
[Unit]
Description=OFT Telegram watchdog (one-shot)
After=oft.service

[Service]
Type=oneshot
User=oft
Group=oft
UMask=0077
WorkingDirectory=/opt/order-flow-trading
Environment=OFT_ENV=paper
Environment=OFT_DATA=/var/lib/oft
# The same $OFT_DATA/oft.env ADR 0012 gives oft.service: on an
# encrypted-file/keyring host it carries OFT_MASTER_KEY and
# PYTHON_KEYRING_BACKEND, without which the watchdog cannot decrypt the token.
EnvironmentFile=-/var/lib/oft/oft.env
ExecStart=/opt/order-flow-trading/.venv/bin/oft telegram watchdog --once
```

Wire it to the daemon unit so a failed daemon triggers one check:

```ini
# in oft.service
[Unit]
OnFailure=oft-telegram-watchdog.service
```

Safe behavior:

- The watchdog never prints, returns, or persists the bot token.
- No single private chat target is a no-op (`NONE`), not a crash. A missing or
  invalid token while a private chat target exists surfaces as `ALERT_FAILED`
  (exit 1) instead of a silent `NONE`.
- A failed alert/recovery or an unreadable daemon state exits non-zero so
  systemd records the failure.
- `--once` is mandatory; omitting it is a validation error (exit 2).

## Watchdog: cron

```cron
# Every five minutes; one probe and at most one alert/recovery per outage.
# /etc/oft/oft.env is a 0600 file with the same OFT_* settings (including
# OFT_MASTER_KEY and PYTHON_KEYRING_BACKEND) the daemon uses.
*/5 * * * * root set -a; . /etc/oft/oft.env; set +a; cd /opt/order-flow-trading && .venv/bin/oft telegram watchdog --once >>/var/log/oft/watchdog.log 2>&1
```

Adapt the working directory and environment file to the installation. Keep the
secret backend configuration identical to the daemon's so the watchdog and the
daemon resolve the same token. A cron entry must not pass the token on the
command line or write it to the log.

## Exit codes

| Exit | Meaning |
|---|---|
| `0` | Probe completed: daemon UP with no prior outage, `ALERT_SENT`, or `RECOVERY_SENT`. |
| `1` | `TELEGRAM_DELIVERY_FAILED` (`ALERT_FAILED`/`RECOVERY_FAILED`) or `DATA_UNAVAILABLE` (daemon state `UNKNOWN`, e.g. the lock/state could not be read). |
| `2` | Validation error, such as `oft telegram watchdog` without `--once`. |
| `5` | Configuration/auth error: invalid settings or an unavailable secret store. |

Exit `1` from cron/systemd means the outage was observed but the alert could not
be confirmed; check `oft telegram status`, the allowlist, and
`oft telegram receipts`, then fix the token/chat. Delivery is at-most-once per
outage episode, so a failed alert is not retried by the same run.
