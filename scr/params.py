"""Params: data/market.db + index/ -> data/params/<anchor>.csv

Always runs, whether or not any portfolio screens. It is the single place
per-ticker parameters are computed; screens, the baseline, and the UI read the
CSV and never recompute. One row per ticker trading on the anchor date.

    ticker, company_name, exchange, icb_l2, group
    close_raw            official close on the anchor date (VND)
    outstanding_shares, free_float, free_float_ratio
    float_cap            free_float * close_raw            <- the weighting base
    market_cap_vendor    FiinPro's own field, QA only
    mcap_gap             |close_raw*shares - vendor| / vendor
    sessions_21          sessions in the liquidity window (<21 = short history)
    value_21             sum of matched turnover over the window (VND)
    adv_21               value_21 / sessions_21
    turnover_21_pct      adv_21 / float_cap * 100   (average DAILY turnover)
    fol_limit            foreign ownership limit, from index/fol.csv (blank if absent)
    adj_factor           close_adj / close_raw; != 1 means a corporate action
                         between the anchor and the extract date

float_cap is free_float * close_raw, never close_adj and never the vendor
market_cap: the adjusted series prices each name on its own restated scale, and
the vendor cap carries known transcription gaps (see data/market.txt QUALITY).

The liquidity window is the 21 sessions ending ON the anchor date, inclusive.
turnover_21_pct is the average DAILY turnover over the window as a percent of
float cap; 0.5 means half a percent of the float cap changes hands on a typical
session. It is NaN when the ticker has fewer than 21 sessions.

FOL is deferred: fol_limit stays blank until index/fol.csv is filled, and no
screen reads it yet.

Anchor date resolution, in order:
    1. --date            explicit; FAILs if not a session in market.db
    2. anchor_date.json  sticky default (index/anchor_date.json); falls back to
                         (3) if missing, malformed, or not a session
    3. latest session in market.db

A ticker on the anchor date that is missing from index/group_map_live.csv, or
has a blank group, FAILs the build: it would silently vanish from every book.

Usage:
    .venv\\Scripts\\python.exe scr\\params.py
    .venv\\Scripts\\python.exe scr\\params.py --date 2026-09-11
"""
import argparse
import json
import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "market.db"
OUT = ROOT / "data" / "params"
MAP = ROOT / "index" / "group_map_live.csv"
FOL = ROOT / "index" / "fol.csv"
ANCHOR = ROOT / "index" / "anchor_date.json"
WINDOW = 21

COLUMNS = ["ticker", "company_name", "exchange", "icb_l2", "group",
           "close_raw", "outstanding_shares", "free_float", "free_float_ratio",
           "float_cap", "market_cap_vendor", "mcap_gap",
           "sessions_21", "value_21", "adv_21", "turnover_21_pct",
           "fol_limit", "adj_factor"]


def resolve_anchor(sessions: set, date: str | None, cfg: Path) -> tuple:
    """Return (anchor_date, how) or raise ValueError."""
    if date:
        d = pd.to_datetime(date).date()
        if d not in sessions:
            raise ValueError(f"{d} is not a session in market.db")
        return d, "--date"
    if cfg.exists():
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8")).get("anchor_date")
            d = pd.to_datetime(raw).date() if raw else None
        except (ValueError, json.JSONDecodeError):
            d = None
        if d in sessions:
            return d, cfg.name
        if d is not None:
            print(f"WARN  {cfg.name} has {d}, not a session; using latest")
    return max(sessions), "latest"


def load_map(path: Path) -> pd.DataFrame:
    # utf-8-sig: Excel and PowerShell both leave a BOM behind given the chance.
    g = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    g = g.rename(columns={"Ticker": "ticker", "ICB L2 sector": "icb_l2",
                          "Exclusive group": "group"})
    g["ticker"] = g["ticker"].str.strip().str.upper()
    return g[["ticker", "icb_l2", "group"]]


def load_fol(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["ticker", "fol_limit"])
    f = pd.read_csv(path, dtype={"ticker": str}, encoding="utf-8-sig")
    f["ticker"] = f["ticker"].str.strip().str.upper()
    f["fol_limit"] = pd.to_numeric(f["fol_limit"])
    bad = f.loc[(f["fol_limit"] < 0) | (f["fol_limit"] > 1), "ticker"].tolist()
    if bad:
        raise ValueError(f"{path.name}: fol_limit must be a ratio in [0,1]: {bad}")
    return f[["ticker", "fol_limit"]]


def compute(con: duckdb.DuckDBPyConnection, anchor, gmap: pd.DataFrame,
            fol: pd.DataFrame) -> pd.DataFrame:
    """Build the params frame for one anchor date. Raises ValueError on FAIL."""
    window = [r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM prices WHERE trade_date <= ? "
        "ORDER BY 1 DESC LIMIT ?", [anchor, WINDOW]).fetchall()]

    df = con.execute("""
        SELECT p.ticker, t.company_name, p.exchange, p.close_raw, p.close_adj,
               p.outstanding_shares, p.free_float, p.market_cap
        FROM prices_v p JOIN tickers t USING (ticker)
        WHERE p.trade_date = ?""", [anchor]).df()

    liq = con.execute(f"""
        SELECT ticker, count(*) AS sessions_21, sum(value) AS value_21
        FROM prices WHERE trade_date IN ({",".join("?" * len(window))})
        GROUP BY ticker""", window).df()

    missing = sorted(set(df["ticker"]) - set(gmap["ticker"]))
    if missing:
        raise ValueError(f"on {anchor} but not in group map: {missing}")
    blank = sorted(set(df["ticker"]) & set(gmap.loc[gmap["group"] == "", "ticker"]))
    if blank:
        raise ValueError(f"blank Exclusive group in map: {blank}")

    df = df.merge(gmap, on="ticker").merge(liq, on="ticker", how="left") \
           .merge(fol, on="ticker", how="left")

    df["free_float_ratio"] = df["free_float"] / df["outstanding_shares"]
    df["float_cap"] = df["free_float"] * df["close_raw"]
    if (df["float_cap"] <= 0).any():
        raise ValueError("non-positive float cap: "
                         f"{sorted(df.loc[df['float_cap'] <= 0, 'ticker'])}")

    df["market_cap_vendor"] = df["market_cap"]
    df["mcap_gap"] = ((df["close_raw"] * df["outstanding_shares"] - df["market_cap"])
                      .abs() / df["market_cap"])
    df["adv_21"] = df["value_21"] / df["sessions_21"]
    full = df["sessions_21"] == WINDOW
    df["turnover_21_pct"] = (df["adv_21"] / df["float_cap"] * 100).where(full)
    df["adj_factor"] = df["close_adj"] / df["close_raw"]
    return df[COLUMNS].sort_values("ticker").reset_index(drop=True)


def run(date: str | None, db: Path = DB, out_dir: Path = OUT, map_path: Path = MAP,
        fol_path: Path = FOL, anchor_cfg: Path = ANCHOR) -> tuple:
    """Compute and write one params CSV. Returns (anchor, how, path, frame).

    Raises ValueError with a printable message on any FAIL.
    """
    for p in (db, map_path):
        if not p.exists():
            raise ValueError(f"missing input: {p}")
    con = duckdb.connect(db, read_only=True)
    try:
        sessions = {r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM prices").fetchall()}
        anchor, how = resolve_anchor(sessions, date, anchor_cfg)
        df = compute(con, anchor, load_map(map_path), load_fol(fol_path))
    finally:
        con.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{anchor}.csv"
    df.to_csv(out, index=False, lineterminator="\n", float_format="%.10g")
    return anchor, how, out, df


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="anchor session; overrides anchor_date.json")
    a = ap.parse_args(argv)
    try:
        anchor, how, out, df = run(a.date)
    except ValueError as exc:
        print(f"FAIL  {exc}")
        return 1

    print(f"OK    anchor {anchor} ({how}) -> {out}")
    print(f"      {len(df)} tickers, {df['group'].nunique()} groups")
    short = df.loc[df["sessions_21"] < WINDOW, "ticker"].tolist()
    print(f"      short liquidity history (<{WINDOW} sessions): {short or 'none'}")
    gap = df.loc[df["mcap_gap"] > 1e-4, "ticker"].tolist()
    print(f"      vendor market_cap disagrees: {gap or 'none'}")
    adj = df.loc[(df["adj_factor"] - 1).abs() > 1e-9, "ticker"].tolist()
    print(f"      corporate action since anchor (adj_factor != 1): {adj or 'none'}")
    nofol = int(df["fol_limit"].isna().sum())
    print(f"      fol_limit blank for {nofol} ticker(s) (FOL deferred)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
