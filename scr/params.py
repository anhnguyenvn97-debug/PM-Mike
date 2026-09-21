"""Params: data/market.db + index/ -> per-ticker parameters for any session (D57)

The single place per-ticker parameters are computed. at(db, session) returns
the frame for one session, computed on demand from market.db, the group map
and index/fol.csv; target.py, the screens, the backtest and the desk all call
it. data/params/<date>.csv is only a cache for inspection (the CLI writes it);
nothing reads it as an input. One row per ticker trading on the session with a
positive float cap:

    ticker, company_name, exchange, icb_l2, group
    close_raw            official close on the session (VND)
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
                         between the session and the extract date

float_cap is free_float * close_raw, never close_adj and never the vendor
market_cap: the adjusted series prices each name on its own restated scale, and
the vendor cap carries known transcription gaps (see data/market.txt QUALITY).
A name whose float cap is missing or not positive on the session is left out
(it cannot be weighted); frame.attrs["no_float_cap"] lists them.

The liquidity window is the 21 sessions ending ON the session, inclusive.
turnover_21_pct is the average DAILY turnover over the window as a percent of
float cap; 0.5 means half a percent of the float cap changes hands on a typical
session. It is NaN when the ticker has fewer than 21 sessions.

A ticker on the session that is missing from index/group_map_live.csv, or has
a blank group, FAILs: it would silently vanish from every book. Edits to the
group map or fol.csv need no rebuild; the next call reads them (the cache is
keyed by the files' modification times).

Usage:
    .venv\\Scripts\\python.exe scr\\params.py                 latest session
    .venv\\Scripts\\python.exe scr\\params.py --as-of 2026-09-11
"""
import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd
from common import DB, BookError, snap

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "params"
MAP = ROOT / "index" / "group_map_live.csv"
FOL = ROOT / "index" / "fol.csv"
WINDOW = 21

COLUMNS = ["ticker", "company_name", "exchange", "icb_l2", "group",
           "close_raw", "outstanding_shares", "free_float", "free_float_ratio",
           "float_cap", "market_cap_vendor", "mcap_gap",
           "sessions_21", "value_21", "adv_21", "turnover_21_pct",
           "fol_limit", "adj_factor"]


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


def compute(con: duckdb.DuckDBPyConnection, session, gmap: pd.DataFrame,
            fol: pd.DataFrame) -> pd.DataFrame:
    """Build the params frame for one session. Raises ValueError on FAIL."""
    window = [r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM prices WHERE trade_date <= ? "
        "ORDER BY 1 DESC LIMIT ?", [session, WINDOW]).fetchall()]

    df = con.execute("""
        SELECT p.ticker, t.company_name, p.exchange, p.close_raw, p.close_adj,
               p.outstanding_shares, p.free_float, p.market_cap
        FROM prices_v p JOIN tickers t USING (ticker)
        WHERE p.trade_date = ?""", [session]).df()

    liq = con.execute(f"""
        SELECT ticker, count(*) AS sessions_21, sum(value) AS value_21
        FROM prices WHERE trade_date IN ({",".join("?" * len(window))})
        GROUP BY ticker""", window).df()

    missing = sorted(set(df["ticker"]) - set(gmap["ticker"]))
    if missing:
        raise ValueError(f"on {session} but not in group map: {missing}")
    blank = sorted(set(df["ticker"]) & set(gmap.loc[gmap["group"] == "", "ticker"]))
    if blank:
        raise ValueError(f"blank Exclusive group in map: {blank}")

    df = df.merge(gmap, on="ticker").merge(liq, on="ticker", how="left") \
           .merge(fol, on="ticker", how="left")

    df["free_float_ratio"] = df["free_float"] / df["outstanding_shares"]
    df["float_cap"] = df["free_float"] * df["close_raw"]
    dead = df["float_cap"].isna() | (df["float_cap"] <= 0)
    no_fcap = sorted(df.loc[dead, "ticker"])
    df = df[~dead].copy()

    df["market_cap_vendor"] = df["market_cap"]
    df["mcap_gap"] = ((df["close_raw"] * df["outstanding_shares"] - df["market_cap"])
                      .abs() / df["market_cap"])
    df["adv_21"] = df["value_21"] / df["sessions_21"]
    full = df["sessions_21"] == WINDOW
    df["turnover_21_pct"] = (df["adv_21"] / df["float_cap"] * 100).where(full)
    df["adj_factor"] = df["close_adj"] / df["close_raw"]
    out = df[COLUMNS].sort_values("ticker").reset_index(drop=True)
    out.attrs["no_float_cap"] = no_fcap
    return out


_CACHE: dict = {}


def _stamp(p: Path):
    return (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None


def at(db: Path = DB, session=None, map_path: Path | None = None,
       fol_path: Path | None = None) -> pd.DataFrame:
    """The params frame for one session of market.db (default the latest).
    session is snapped to the last session on or before it. Raises BookError."""
    db = Path(db)
    map_path, fol_path = map_path or MAP, fol_path or FOL
    session = snap(db, session)
    if not map_path.exists():
        raise BookError(f"missing {map_path}")
    key = (str(db), _stamp(db), str(session), str(map_path), _stamp(map_path),
           str(fol_path), _stamp(fol_path))
    if key not in _CACHE:
        con = duckdb.connect(str(db), read_only=True)
        try:
            frame = compute(con, session, load_map(map_path), load_fol(fol_path))
        except ValueError as e:
            raise BookError(str(e))
        finally:
            con.close()
        if len(_CACHE) > 64:
            _CACHE.clear()
        _CACHE[key] = frame
    out = _CACHE[key].copy()
    out.attrs = dict(_CACHE[key].attrs, session=session)
    return out


def universe(frame: pd.DataFrame) -> dict:
    """{group: [tickers A-Z]} over a params frame, groups A-Z."""
    return {g: sorted(s) for g, s in sorted(frame.groupby("group")["ticker"])}


def run(as_of: str | None, db: Path = DB, out_dir: Path = OUT) -> tuple:
    """Compute and write one params CSV. Returns (session, path, frame)."""
    df = at(db, as_of)
    session = df.attrs["session"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{session}.csv"
    df.to_csv(out, index=False, lineterminator="\n", float_format="%.10g")
    return session, out, df


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", help="YYYY-MM-DD; priced on the last session on or "
                                    "before it (default: the latest)")
    a = ap.parse_args(argv)
    try:
        session, out, df = run(a.as_of)
    except BookError as exc:
        print(f"FAIL  {exc}")
        return 1

    print(f"OK    session {session} -> {out} (cache; nothing reads it)")
    print(f"      {len(df)} tickers, {df['group'].nunique()} groups")
    short = df.loc[df["sessions_21"] < WINDOW, "ticker"].tolist()
    print(f"      short liquidity history (<{WINDOW} sessions): {short or 'none'}")
    gap = df.loc[df["mcap_gap"] > 1e-4, "ticker"].tolist()
    print(f"      vendor market_cap disagrees: {gap or 'none'}")
    adj = df.loc[(df["adj_factor"] - 1).abs() > 1e-9, "ticker"].tolist()
    print(f"      corporate action since the session (adj_factor != 1): {adj or 'none'}")
    print(f"      no float cap, left out: {df.attrs['no_float_cap'] or 'none'}")
    nofol = int(df["fol_limit"].isna().sum())
    print(f"      fol_limit blank for {nofol} ticker(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
