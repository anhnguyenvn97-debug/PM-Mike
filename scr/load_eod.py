"""EOD loader: data/raw/<date>.csv -> data/eod.parquet

Deterministic half of the pipeline. The agent fetches via MCP and writes the
raw CSV; this script owns every write to the parquet. It derives nothing that
the provider already returned -- it only validates and appends.

Usage:
    .venv\\Scripts\\python.exe scr\\load_eod.py 2026-08-12
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PARQUET = DATA / "eod.parquet"

VALID_EXCHANGES = {"HOSE", "HNX", "UPCOM"}
NUMERIC = [
    "open_adj", "high_adj", "low_adj", "close_adj",
    "open_raw", "high_raw", "low_raw", "close_raw",
    "volume", "value",
    "market_cap", "free_float", "free_float_ratio", "outstanding_shares",
]


def validate(df: pd.DataFrame, trade_date: str) -> list[str]:
    """Return a list of failures. Empty list == clean."""
    errs = []

    if df["ticker"].duplicated().any():
        dupes = sorted(df.loc[df["ticker"].duplicated(), "ticker"])
        errs.append(f"duplicate tickers: {dupes}")

    nulls = df.columns[df.isna().any()].tolist()
    if nulls:
        errs.append(f"null values in columns: {nulls}")

    bad_exch = sorted(set(df["exchange"]) - VALID_EXCHANGES)
    if bad_exch:
        errs.append(f"unknown exchange codes: {bad_exch}")

    if not df["icb_l2"].str.endswith("_L2").all():
        bad = sorted(df.loc[~df["icb_l2"].str.endswith("_L2"), "ticker"])
        errs.append(f"icb_l2 not an L2 code: {bad}")

    # close must sit inside its own session range, both price bases
    for tag in ("adj", "raw"):
        lo, hi, cl = df[f"low_{tag}"], df[f"high_{tag}"], df[f"close_{tag}"]
        out = df.loc[(cl < lo) | (cl > hi), "ticker"].tolist()
        if out:
            errs.append(f"close_{tag} outside [low,high]: {out}")

    # float can never exceed shares outstanding
    over = df.loc[df["free_float"] > df["outstanding_shares"], "ticker"].tolist()
    if over:
        errs.append(f"free_float > outstanding_shares: {over}")

    # Independent transcription check: the provider's own market_cap must
    # reconstruct from the session's OFFICIAL close * outstanding_shares. This
    # ties the price pull and the snapshot pull together -- a typo in either
    # one breaks it.
    #
    # Official close is exchange-dependent. On UPCoM it is the session VWAP,
    # which is what adjusted=true returns; everywhere else it is the last
    # matched price, which is what adjusted=false returns. Comparing against
    # close_adj on every exchange used to work only because no HOSE name had
    # an outstanding corporate action: adjusted=true also folds in pending
    # action factors, so on 2026-08-14 BID came back 6.4% under its traded
    # price and SSI 20.1% under, and neither is a transcription error.
    official = df["close_adj"].where(df["exchange"] == "UPCOM", df["close_raw"])
    implied = official * df["outstanding_shares"]
    rel = ((implied - df["market_cap"]).abs() / df["market_cap"])
    bad_cap = df.loc[rel > 1e-5, ["ticker"]].assign(rel=rel[rel > 1e-5])
    if not bad_cap.empty:
        rows = [f"{r.ticker}({r.rel:.2e})" for r in bad_cap.itertuples()]
        errs.append(f"market_cap != official_close*shares: {rows}")

    if (df["volume"] < 0).any() or (df["value"] < 0).any():
        errs.append("negative volume or value")

    return errs


def main(trade_date: str) -> int:
    src = DATA / "raw" / f"{trade_date}.csv"
    if not src.exists():
        print(f"FAIL  no raw file: {src}")
        return 1

    df = pd.read_csv(src)
    df["exchange"] = df["exchange"].str.upper()
    for c in NUMERIC:
        df[c] = pd.to_numeric(df[c])
    df["in_index"] = df["in_index"].astype(bool)
    df["is_pick"] = df["is_pick"].astype(bool)

    errs = validate(df, trade_date)
    if errs:
        print(f"FAIL  {trade_date}: {len(errs)} check(s) failed")
        for e in errs:
            print(f"  - {e}")
        return 1

    df.insert(0, "trade_date", pd.to_datetime(trade_date).date())
    df["ingested_at"] = pd.Timestamp.now().floor("s")

    # idempotent: this date's rows are replaced wholesale, never appended twice
    if PARQUET.exists():
        prev = pd.read_parquet(PARQUET)
        kept = prev[prev["trade_date"] != df["trade_date"].iloc[0]]
        replaced = len(prev) - len(kept)
        out = pd.concat([kept, df], ignore_index=True)
    else:
        replaced = 0
        out = df

    out = out.sort_values(["trade_date", "ticker"]).reset_index(drop=True)
    out.to_parquet(PARQUET, index=False)

    idx = int(df["in_index"].sum())
    print(f"OK    {trade_date}: {len(df)} rows written "
          f"({idx} index + {len(df) - idx} picks), {replaced} replaced")
    print(f"      parquet now {len(out)} rows across "
          f"{out['trade_date'].nunique()} date(s) -> {PARQUET}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
