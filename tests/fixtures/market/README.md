# Market fixtures

Small, committed, deterministic samples. No network is used by tests.

## `btcusdt_agg-trades_sample.csv`

- Source: synthetic sample shaped like a `data.binance.vision` USD-M
  `aggTrades` daily file (public Binance market-data archive schema).
- Retrieved/created: 2026-11-06.
- Window: 2025-11-05T00:00:00Z to 2025-11-05T04:00:00Z (one 4h bar).
- Symbol: BTCUSDT.
- Columns: `agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker`.

### Golden delta

Taker buys are `is_buyer_maker=false`; taker sells are `is_buyer_maker=true`.

| Side | Quantity |
|---|---|
| buy  | 1.5 + 2.0 + 0.75 = **4.25** |
| sell | 0.5 + 1.25 + 1.0 = **2.75** |
| delta | 4.25 - 2.75 = **1.50** |
| cumulative CVD (one bar) | **1.50** |
| trade count | **6** |

Tests assert these exact decimal strings through the metric, the API, and the
CLI. Decimal arithmetic is used end to end; values are never floats.

`CHECKSUMS` follows the `data.binance.vision` `.CHECKSUM` shape:
`<sha256>  <filename>`.

## `btcusdt_klines_4h.csv`

- Source: derived **independently from the raw aggTrades rows** in
  `tests/fixtures/archive/btcusdt_agg-trades_2026-10-01..07.zip` (public
  `data.binance.vision` kline schema), **not** from any CVD output. Each 4h bar
  aggregates price OHLC and taker buy volume (`is_buyer_maker=false`) directly
  from the trades; it is not back-computed from the metric result.
- Created: 2026-11-06.
- Window: 2026-10-01T00:00:00Z to 2026-10-08T00:00:00Z (7 daily 4h bars; the
  fixture trades all fall in the first bar of each day).
- Symbol: BTCUSDT. Period: 4h.
- Columns: `symbol,bar_start,open,high,low,close,volume,taker_buy_volume,close_time`.
- Cross-check identity: `delta = 2 * taker_buy_volume - volume` per bar
  (0.20 = 2*0.40 - 0.60). Tampering one row must fail the metric cross-check.

## `xauusdt_metrics_sample.csv`

- Source: synthetic sample shaped like the public Binance USD-M `metrics`
  (`openInterest`, `sumOpenInterestValue`, long/short and taker ratios) and
  funding/mark-price endpoints. **Not** an official archive file.
- Created: 2026-11-06.
- Symbol: XAUUSDT (TradFi perpetual). Window: 2026-10-01T00:00:00Z to
  2026-10-01T04:00:00Z.
- Columns: `symbol,record_type,ts,open_interest,open_interest_value,`
  `long_short_ratio,taker_ratio,funding_rate,mark_price,index_price,`
  `funding_interval_hours`.
- `record_type=OPEN_INTEREST` timestamps are deliberately irregular
  (00:00, 00:05, 00:11, 00:15, 00:22), matching the documented TradFi irregular
  sampling. The metric core resamples them to 5m, stamps the section
  `IRREGULAR_RESAMPLED` and the document `ESTIMATED`, and marks synthesized
  points as estimates. Interpolation is never presented as measured.
- `record_type=FUNDING` rows use an 8h interval. A missing funding period is
  reported `MISSING`; it is never inserted as a zero rate.

## `liquidations_btcusdt.jsonl`

- Source: **recorded/live-shaped public `forceOrder` events** (normalized to
  the ADR-0002 liquidation columns), not an archive download. Historical
  USDⓈ-M liquidation data does not exist; no test depends on it.
- Created: 2026-11-06.
- Symbol: BTCUSDT. Event window: 2026-10-01T00:01:05Z to 00:02:45Z.
- Side mapping: `side=SELL` means a long position was liquidated;
  `side=BUY` means a short position was liquidated.
- Coverage is labelled **since deployment** (`coverage_start` is the recorder's
  injected deployment time). `limitations` always states that the WS feed
  pushes only the largest liquidation per symbol per second and that no
  historical backfill exists.
- Decimal fields are JSON strings so binary floats never enter the metric path.
