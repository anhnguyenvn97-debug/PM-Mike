---
description: Fetch T-1 EOD data for the universe and load it into data/eod.parquet
argument-hint: <YYYY-MM-DD>
---

Load EOD data for **$1** into the parquet. Follow this recipe exactly — do not
improvise extra calls, and do not compute derived values yourself.

## 1. Read the universe

Read `data/universe.yml`. It gives `index` and `picks`.

## 2. Five MCP calls, in this order

1. `get_index_constituents(index=<index>, include_sector=false)`
   → the member list. Sector enrichment is off on purpose; call 2 covers it.
2. `get_basic_info(tickers=members + picks)`
   → `exchangeCode`, `icbNameL2`. Picks are usually **not** index members, so
     this is the only call that classifies them.
3. `get_stock_prices(tickers=members + picks, from_date=$1, to_date=$1,`
   `fields=[open,high,low,close], adjusted=true, include_unclosed=false)`
4. Same as 3 but `adjusted=false` and `fields=[...,volume,value]`.
5. `get_equity_snapshot(tickers=members + picks, as_of_date=$1,`
   `metrics=[market_cap, free_float, free_float_ratio, outstanding_shares])`

Check `coverage_ratio` and `missing_tickers` on call 5, and confirm
`snapshot_date_fallback_applied` is false. If the provider fell back to a
different date, stop and report it — do not write a row stamped $1 with data
from another day.

## 3. Write `data/raw/$1.csv`

One row per ticker, joined **on ticker** — responses come back unordered, so
never align by position. Header, exactly:

```
ticker,exchange,icb_l2,in_index,is_pick,open_adj,high_adj,low_adj,close_adj,open_raw,high_raw,low_raw,close_raw,volume,value,market_cap,free_float,free_float_ratio,outstanding_shares
```

- `exchange` upper-cased (`UPCoM` → `UPCOM`).
- `in_index` / `is_pick` are 1/0.
- Transcribe verbatim. Do not round, reformat, or recompute anything.

Reject the day if any `timestamp` in calls 3/4 is not `00:00` — a non-midnight
stamp means the session was still open and the row is provisional, not a close.

## 4. Load

```
.venv\Scripts\python.exe data\load_eod.py $1
```

The loader validates and owns the parquet write. It is idempotent: re-running a
date replaces that date's rows. If it prints FAIL, fix `data/raw/$1.csv` and run
again — never hand-edit the parquet.

Report the loader's output verbatim, plus anything odd from step 2.
