"""Build the baseline float-cap allocation from one EOD date.

Two outputs, both in portfolio/baseline:

    sector_allocation.csv    the anchor vector, one row per Exclusive group
    sector_constituents.csv  one column per sector A-Z:

        row 0   multiplier   AV | NO | OW | UW, or a number overriding the
                             default for that rating
        row 1   rating       AV | NO | OW | UW
        row 2   sector name
        row 3+  tickers, A-Z, blank-padded

Baseline is untilted by definition, so both rows come out all AV -- AV is the
float-cap weight, i.e. a multiplier of 1.0 on itself. That keeps baseline and
custom books structurally identical: forking one is a plain file copy, and a
single reader parses both.

Constituents are group_map_live.csv INTERSECT the parquet universe. Map rows for
tickers outside the universe are dropped; a universe ticker missing from the map
is an error, because it would silently vanish from the book.

Float cap is free_float (a share count) times the session's OFFICIAL close,
obtained as market_cap / outstanding_shares. That resolves to last matched
price on HOSE/HNX and session VWAP on UPCoM without an exchange conditional,
and it reconciles with the provider's market_cap by construction.

Not close_adj. The adjusted series folds in pending corporate-action factors,
each ticker carrying its own, so an adjusted cross-section prices 102 names on
102 private scales. close_adj is comparable across time for one ticker; the
official close is comparable across tickers on one date. Weighting is a
cross-section, so it takes the latter. close_adj is also restated backwards
when an action is announced, which would silently change historical weights.

Weighting is float cap within group and float cap across groups, which is
algebraically flat float cap across all names. The group layer is attribution:
it is the grain that tilts will be expressed in.

Anchor date resolution, in order:
    1. --date            explicit override; FAILs if not in the parquet
    2. anchor_date.json  hand-edited sticky default (index/anchor_date.json);
                         falls back to (3) if missing, malformed, or its date
                         is not an exact trade_date in the parquet
    3. trade_date.max()  latest date present in the parquet

Usage
    .venv\\Scripts\\python.exe scr\\build_baseline.py
    .venv\\Scripts\\python.exe scr\\build_baseline.py --date 2026-08-12
"""
import argparse
import csv
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
GROUP = "Exclusive group"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", type=Path, default=ROOT / "index" / "group_map_live.csv")
    ap.add_argument("--parquet", type=Path, default=ROOT / "data" / "eod.parquet")
    ap.add_argument("--outdir", type=Path, default=ROOT / "portfolio" / "baseline")
    ap.add_argument("--date", default=None,
                     help="trade_date to anchor on; overrides anchor_date.json")
    ap.add_argument("--anchor-config", type=Path,
                     default=ROOT / "index" / "anchor_date.json",
                     help="hand-edited sticky default; falls back to latest if "
                          "missing or its date is not in the parquet")
    a = ap.parse_args()

    for p in (a.map, a.parquet):
        if not p.exists():
            print(f"FAIL  missing input: {p}")
            return 1

    eod = pd.read_parquet(a.parquet)
    available = set(eod.trade_date)

    if a.date:
        date = pd.to_datetime(a.date).date()
        if date not in available:
            print(f"FAIL  {date} not in {a.parquet.name}; available: {sorted(available)}")
            return 1
    else:
        date = None
        if a.anchor_config.exists():
            try:
                cfg_raw = json.loads(a.anchor_config.read_text(encoding="utf-8")).get("anchor_date")
                cfg_date = pd.to_datetime(cfg_raw).date() if cfg_raw else None
            except Exception:
                cfg_date = None
            if cfg_date in available:
                date = cfg_date
            elif cfg_date is not None:
                print(f"WARN  {a.anchor_config.name} has {cfg_date}, not in "
                      f"{a.parquet.name}; falling back to latest")
        if date is None:
            date = eod.trade_date.max()

    eod = eod[eod.trade_date == date]

    # utf-8-sig: Excel and PowerShell both leave a BOM behind given the chance.
    gmap = pd.read_csv(a.map, dtype=str, encoding="utf-8-sig").fillna("")

    blank = sorted(gmap.loc[gmap[GROUP] == "", "Ticker"])
    if blank:
        print(f"FAIL  {len(blank)} map row(s) have no {GROUP}: {blank}")
        return 1

    missing = sorted(set(eod.ticker) - set(gmap.Ticker))
    if missing:
        print(f"FAIL  in universe but not in {a.map.name}: {missing}")
        print("      run scr/build_group_map.py, then fill in the group")
        return 1

    # A pending corporate action shows up as close_adj != close_raw on an
    # exchange whose official close is the last matched price. On UPCoM the two
    # differ by design (VWAP vs last matched), so it is not a signal there.
    #
    # This has to stop the build, not warn. On a stock dividend the price drops
    # on the ex-date but outstanding_shares only rises on the credit date, days
    # or weeks later; in that window market_cap is derived from a stale share
    # count and the name is understated by the whole bonus ratio. Every price
    # basis inherits that -- there is no arithmetic fix, only a clean date.
    pend = eod[(eod.exchange != "UPCOM") & (eod.close_adj != eod.close_raw)]
    if not pend.empty:
        print(f"FAIL  pending corporate action on {date}:")
        for r in pend.sort_values("ticker").itertuples():
            f = r.close_adj / r.close_raw
            print(f"        {r.ticker:<5} factor {f:.4f}  "
                  f"({(1 / f - 1) * 100:.2f}% if a stock dividend)")
        print("      share counts may be stale, so float cap is unreliable for")
        print("      these names; pin index/anchor_date.json to a clean date")
        return 1

    df = eod.merge(gmap, left_on="ticker", right_on="Ticker")
    df["fcap"] = df.free_float * (df.market_cap / df.outstanding_shares)

    if (df.fcap <= 0).any():
        print(f"FAIL  non-positive float cap: {sorted(df.loc[df.fcap <= 0, 'ticker'])}")
        return 1

    total = df.fcap.sum()

    sector = (
        df.groupby(GROUP)
        .agg(n_members=("ticker", "size"), fcap=("fcap", "sum"))
        .assign(weight=lambda s: s.fcap / total)
        .sort_values("weight", ascending=False)
        .reset_index()
    )

    a.outdir.mkdir(parents=True, exist_ok=True)
    alloc = a.outdir / "sector_allocation.csv"
    const = a.outdir / "sector_constituents.csv"

    # Read the outgoing sector set before overwriting it. A sector appearing or
    # disappearing reorders the columns of the constituents grid, and every
    # custom book forked from the old layout binds its ratings by column
    # position -- so the change has to be shouted about, not inferred later.
    prev = set(pd.read_csv(alloc, usecols=["group"]).group) if alloc.exists() else set()

    sector.insert(0, "anchor_date", date)
    sector.rename(columns={GROUP: "group"}).to_csv(
        alloc, index=False, lineterminator="\n", float_format="%.8f"
    )

    # Wide membership sheet: one column per sector A-Z, tickers A-Z down each.
    # Columns are ragged, so short sectors are blank-padded to a common depth.
    members = {g: sorted(s) for g, s in df.groupby(GROUP).ticker}
    sectors = sorted(members)
    depth = max(len(v) for v in members.values())

    with open(const, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["AV"] * len(sectors))   # multiplier override
        w.writerow(["AV"] * len(sectors))   # rating
        w.writerow(sectors)
        for i in range(depth):
            w.writerow([m[i] if i < len(m) else "" for m in
                        (members[g] for g in sectors)])

    print(f"OK    anchor_date {date}")
    print(f"      {alloc}  {len(sector)} groups")
    print(f"      {const}  {len(sector)} sectors x {depth} deep, {len(df)} tickers")
    print(f"      dropped, in map but not in universe: "
          f"{sorted(set(gmap.Ticker) - set(eod.ticker)) or 'none'}")

    now = set(sector[GROUP])
    dark = sorted((set(gmap[GROUP]) - {""}) - now)
    print(f"      groups defined but with no surviving member: {dark or 'none'}")

    added, gone = sorted(now - prev), sorted(prev - now)
    if prev and (added or gone):
        print("      WARN sector set changed since the last run. Custom grids "
              "bind ratings by column")
        print("           position, so any book forked from the old layout is "
              "now misaligned.")
        if added:
            print(f"           appeared:   {added}")
        if gone:
            print(f"           disappeared: {gone}")
    print(f"      sector weights sum to {sector.weight.sum():.10f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
