# Archive fixtures

Small, committed, deterministic fixtures for the archive backfill slice. Tests
never use the network by default; `FixtureArchiveSource` serves these local
files.

## Valid week

`btcusdt_agg-trades_2026-10-01.zip` through `..._2026-10-07.zip` each contain
one `BTCUSDT-aggTrades-YYYY-MM-DD.csv` with three synthetic aggTrades rows.
Each ZIP has a sibling `.CHECKSUM` in the `data.binance.vision` shape
(`<sha256>  <filename>`).

Day `2026-10-03` deliberately repeats one `agg_trade_id` inside the same file,
so the one-week conversion has 22 CSV rows but only **21 unique rows**. Tests
assert the dedupe and idempotent re-runs.

## Hostile / failure fixtures

- `corrupt/btcusdt_agg-trades_2026-10-08.zip` is a valid ZIP, but its
  `.CHECKSUM` deliberately carries an all-zero digest. A download of this file
  must fail closed (`BACKFILL_CHECKSUM_MISMATCH`) with no Parquet part and no
  `COMPLETE` manifest row.
- `zip_slip.zip` contains an entry named `../../zip_slip_escape.txt`.
- `oversize.zip` expands to roughly 2 MiB from a highly compressible payload;
  tests lower the configured limits to exercise the size and expansion-ratio
  guards.

Source shape: synthetic, modeled on the public `data.binance.vision` USD-M
archive layout. Created 2026-11-06.
