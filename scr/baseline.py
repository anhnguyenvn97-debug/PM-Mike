"""Baseline: data/params/<anchor>.csv -> portfolio/baseline/<anchor>/

The untilted float-cap allocation a portfolio forks from. One folder per anchor
date, so portfolios on different anchors never invalidate each other. Reads only
the params CSV for the anchor date -- float cap, group and membership are
already there -- so it never touches market.db prices or the group map directly.

    portfolio/baseline/<anchor>/     one per anchor, what portfolios fork from

    sector_allocation.csv    anchor_date, group, n_members, fcap, weight
    sector_constituents.csv  the A-Z grid, one column per group:
        rows 0-1  AV placeholders, never parsed; a fork writes the book's own
        row 2     group name
        row 3+    tickers, A-Z, blank-padded

The grid keeps the book's shape, so a book is this grid with rows 0-1
rewritten and cells blanked. Rows 0-1 stay AV on purpose: portfolios compare
input/ to this file byte for byte, and a new placeholder would flag every
portfolio for a re-fork.

Weighting is float cap within group and float cap across groups, which is flat
float cap across all names. float_cap = free_float * close_raw (see params.py).

Anchor date: --date > index/anchor_date.json > latest session in market.db,
the same resolver params.py uses. The params CSV for that date must exist and
must be newer than market.db, index/group_map_live.csv and index/fol.csv, or
the build FAILs: a stale params file would weight on data the database no
longer holds, or on a grouping the map no longer has (common.newer_than).

WARNs, never fails, on adj_factor != 1: a corporate action between the anchor
and the extract date. The price is still the official close, but a stock
dividend credits shares days after the ex-date, so free_float may be stale for
those names. Pick a cleaner anchor if the listed weights matter.

sticky_anchor() is that resolver with no --date: the default anchor for
`portfolio.py fork` and the desk. A rebuilt baseline whose groups changed is
flagged per portfolio by the desk (re-fork needed); nothing is copied.

ensure(anchor) is the on-demand path `portfolio.py fork --anchor` uses: it runs
params.py and builds the dated folder only when either is missing or stale, so
a fork after a group-map edit never uses the old grouping.

Usage
    .venv\\Scripts\\python.exe scr\\baseline.py
    .venv\\Scripts\\python.exe scr\\baseline.py --date 2026-09-11
"""
import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd
import params as params_mod
from common import (
    ALLOC,
    ANCHOR_CFG,
    DB,
    GRID,
    PARAMS,
    PORTFOLIO,
    BookError,
    newer_than,
    write_grid,
)
from params import resolve_anchor

BASELINE = PORTFOLIO / "baseline"


def params_stale(pp: Path, db: Path = DB, map_path: Path = params_mod.MAP,
                 fol_path: Path = params_mod.FOL) -> list[str]:
    """Names of the inputs newer than the params CSV pp (empty = fresh)."""
    return [p.name for p in newer_than(pp, db, map_path, fol_path)]


def build(params: pd.DataFrame, anchor) -> tuple[pd.DataFrame, list[str], dict]:
    """Return (allocation frame, sorted groups, members by group)."""
    if params["group"].isna().any() or (params["group"] == "").any():
        raise BookError("params rows with no group; re-run params.py")
    if (params["float_cap"] <= 0).any():
        bad = sorted(params.loc[params["float_cap"] <= 0, "ticker"])
        raise BookError(f"non-positive float cap: {bad}")

    total = params["float_cap"].sum()
    alloc = (params.groupby("group")
             .agg(n_members=("ticker", "size"), fcap=("float_cap", "sum"))
             .assign(weight=lambda s: s["fcap"] / total)
             .sort_values("weight", ascending=False)
             .reset_index())
    alloc.insert(0, "anchor_date", anchor)
    members = {g: sorted(s) for g, s in params.groupby("group")["ticker"]}
    return alloc, sorted(members), members


def write(outdir: Path, alloc: pd.DataFrame, groups: list[str],
          members: dict) -> None:
    """Write sector_allocation.csv and the A-Z grid into outdir."""
    outdir.mkdir(parents=True, exist_ok=True)
    alloc.to_csv(outdir / ALLOC, index=False, lineterminator="\n", float_format="%.8f")
    write_grid(outdir / GRID,
               [["AV"] * len(groups), ["AV"] * len(groups), groups],
               [members[g] for g in groups])


def sessions_of(db: Path) -> set:
    if not db.exists():
        raise BookError(f"missing {db}; run scr/ingest.py first")
    con = duckdb.connect(db, read_only=True)
    try:
        return {r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM prices").fetchall()}
    finally:
        con.close()


def sticky_anchor(db: Path = DB, anchor_cfg: Path = ANCHOR_CFG) -> str:
    """index/anchor_date.json if it names a session, else the latest session."""
    try:
        anchor, _ = resolve_anchor(sessions_of(db), None, anchor_cfg)
    except ValueError as e:
        raise BookError(str(e))
    return str(anchor)


def ensure(anchor: str, db: Path = DB, params_dir: Path = PARAMS,
           root: Path = BASELINE, **params_paths) -> Path:
    """Return portfolio/baseline/<anchor>/, building params and baseline if stale."""
    pp = params_dir / f"{anchor}.csv"
    why = params_stale(pp, db, params_paths.get("map_path", params_mod.MAP),
                       params_paths.get("fol_path", params_mod.FOL))
    if why:
        try:
            params_mod.run(anchor, db=db, out_dir=params_dir, **params_paths)
        except ValueError as e:
            raise BookError(str(e))
        print(f"OK    built {pp} (newer: {', '.join(why)})")
    outdir = root / anchor
    ap = outdir / ALLOC
    if newer_than(ap, pp):
        alloc, groups, members = build(pd.read_csv(pp), anchor)
        write(outdir, alloc, groups, members)
        print(f"OK    built {outdir}  {len(groups)} groups")
    return outdir


def run(date: str | None, db: Path = DB, params_dir: Path = PARAMS,
        anchor_cfg: Path = ANCHOR_CFG, root: Path = BASELINE) -> int:
    sessions = sessions_of(db)
    try:
        anchor, how = resolve_anchor(sessions, date, anchor_cfg)
    except ValueError as e:
        raise BookError(str(e))

    pp = params_dir / f"{anchor}.csv"
    if not pp.exists():
        raise BookError(f"missing {pp}\n      run scr/params.py --date {anchor}")
    why = params_stale(pp, db)
    if why:
        raise BookError(f"{pp.name} is older than {', '.join(why)}\n"
                        f"      run scr/params.py --date {anchor}")

    params = pd.read_csv(pp)
    alloc, groups, members = build(params, anchor)
    outdir = root / str(anchor)
    write(outdir, alloc, groups, members)

    depth = max(len(m) for m in members.values())
    print(f"OK    anchor_date {anchor} ({how})")
    print(f"      {outdir / ALLOC}  {len(groups)} groups")
    print(f"      {outdir / GRID}  {len(groups)} x {depth} deep, "
          f"{len(params)} tickers")
    adj = params.loc[(params["adj_factor"] - 1).abs() > 1e-9, "ticker"].tolist()
    if adj:
        print(f"WARN  corporate action after the anchor, free_float may be "
              f"stale: {adj}")
    print(f"      weights sum to {alloc['weight'].sum():.10f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="anchor session; overrides anchor_date.json")
    a = ap.parse_args(argv)
    try:
        return run(a.date)
    except BookError as e:
        print(f"FAIL  {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
