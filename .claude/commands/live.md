---
description: Pull live intraday prices for the universe (price only, no parquet write)
argument-hint: (no args)
---

Live price pull. **This never touches `data/eod.parquet`.**

## 1. Universe

Read `data/universe.yml`; resolve `index` via
`get_index_constituents(index=<index>, include_sector=false)` and append `picks`.

## 2. One MCP call

```
get_stock_prices(tickers=members + picks, latest=true, fields=[close], adjusted=false)
```

`adjusted=false` on purpose: it returns last matched price on every exchange,
which is the same basis the intraday feed uses. With `adjusted=true` the UPCoM
rows come back as session VWAP instead, which is not comparable.

## 3. Write `data/live/<YYYY-MM-DDTHHMM>.csv`

```
ticker,as_of,last
```

Keep the per-ticker `timestamp` from the response as `as_of`. Do **not**
collapse it to a single run time — the rows are last-tick candles and their
stamps genuinely differ, by up to half an hour on thin names. Anything that
needs a synchronised cross-section must account for that.

## 4. Report

Print the requested tickers and flag any whose `as_of` trails the newest stamp
by more than 5 minutes.

Do not compare these values against `eod.parquet` closes without saying which
basis each side uses.
