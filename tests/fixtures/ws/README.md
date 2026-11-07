# WebSocket recorded fixtures

Recorded-shaped public USD-M stream messages (`wss://fstream.binance.com`),
retrieved/derived 2026-11-06. They are **not** a live recording: each file is a
small, deterministic sample shaped exactly like the documented combined-stream
envelope (`{"stream": "...", "data": {...}}`) and deliberately contains the
hostile cases this slice must survive:

- valid events for each stream;
- duplicate `agg_trade_id` / duplicate event times;
- an out-of-order `agg_trade_id`;
- malformed JSON, missing fields, invalid enums, and invalid ranges;
- control characters in text fields (stripped, never logged raw);
- an oversized line (`aggtrade.jsonl`, padding > 64 KiB is not committed; the
  line is intentionally larger than the small test receive cap).

Fields follow the documented public schemas: `aggTrade` (`e,E,s,a,p,q,f,l,T,m`),
`kline` (`e,E,s,k`), `markPriceUpdate` (`e,E,s,p,i,P,r,T`), and `forceOrder`
(`e,E,o`). No credentials or private data are present.
