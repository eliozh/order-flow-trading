# Binance exchangeInfo fixtures

Small, committed, deterministic samples shaped like the public
`GET /fapi/v1/exchangeInfo` response. Tests never use the network; the live
smoke is opt-in via `OFT_BINANCE_LIVE_SMOKE=1`.

## `exchange_info_sample.json`

- Source: synthetic sample shaped like the Binance USD-M futures exchangeInfo
  response (public market-data metadata; no credentials required).
- Retrieved/created: 2026-11-06.
- Symbols:
  - `BTCUSDT` — `PERPETUAL`, `TRADING` (tick `0.10`, step `0.001`, min notional `5.00`).
  - `ETHUSDT` — `PERPETUAL`, `TRADING` (tick `0.01`, step `0.001`).
  - `XAUUSDT` — `TRADIFI_PERPETUAL`, `TRADING` (tick `0.01`, step `0.01`, listing `2025-12-11`).
- Non-trading / unsupported entries that the registry must filter out:
  - `FTTUSDT` (`status=SETTLING`).
  - `BTCUSDT_261225` (`contractType=CURRENT_QUARTER`).
- Malformed entries that must be rejected without crashing:
  - `BROKENUSDT` (no filters), `BADTICKUSDT` (non-decimal tick size),
    `NODATEUSDT` (no `onboardDate`), and one non-object list element.

`CHECKSUMS` follows the `data.binance.vision` `.CHECKSUM` shape:
`<sha256>  <filename>`.
