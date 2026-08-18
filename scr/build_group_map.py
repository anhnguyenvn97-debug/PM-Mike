"""Keep index/group_map_live.csv in sync with the EOD universe — by appending
only. Existing rows are never rewritten, so hand-entered "Exclusive group"
values survive every run.

Schema is identical to group_map_default.csv:
    Ticker,Company_name,ICB L2 sector,Exclusive group

Behaviour
    live file missing -> seeded byte-for-byte from group_map_default.csv
    live file present -> used as-is; only tickers it does not already contain
                         are appended, with a blank "Exclusive group"

Nothing else is touched. If there is nothing new, the file is not opened for
writing at all. Tickers you delete from the live file will be re-appended on
the next run if they are still in the universe; tickers that leave the universe
are left alone.

Sources
    index/group_map_default.csv   seed only, read once when live is missing
    data/eod.parquet              live universe + current ICB L2 (latest date)

Usage
    .venv\\Scripts\\python.exe scr\\build_group_map.py
    .venv\\Scripts\\python.exe scr\\build_group_map.py --out somewhere.csv
"""
import argparse
import csv
import re
import shutil
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
HEADER = ["Ticker", "Company_name", "ICB L2 sector", "Exclusive group"]


def prettify_icb(code: str) -> str:
    """BANKS_L2 -> 'Banks'. Fallback only; the learned map wins where it can."""
    body = re.sub(r"_L\d$", "", code)
    words = [w.capitalize() for w in body.split("_")]
    return " ".join("&" if w == "And" else w for w in words)


def read_map(path: Path) -> pd.DataFrame:
    # utf-8-sig: Excel and PowerShell both like to leave a BOM behind, which
    # would otherwise turn the first column into "﻿Ticker".
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    df.columns = HEADER
    return df


def ensure_trailing_newline(path: Path) -> None:
    """Append-mode writes must not land on the tail of an unterminated row."""
    with open(path, "rb") as f:
        if f.seek(0, 2) == 0:
            return
        f.seek(-1, 2)
        if f.read(1) not in (b"\n", b"\r"):
            with open(path, "a", encoding="utf-8", newline="") as g:
                g.write("\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--default", type=Path, default=ROOT / "index" / "group_map_default.csv")
    ap.add_argument("--parquet", type=Path, default=ROOT / "data" / "eod.parquet")
    ap.add_argument("--out", type=Path, default=ROOT / "index" / "group_map_live.csv")
    a = ap.parse_args()

    if not a.parquet.exists():
        print(f"FAIL  missing input: {a.parquet}")
        return 1

    eod = pd.read_parquet(a.parquet, columns=["trade_date", "ticker", "icb_l2"])
    eod = eod[eod.trade_date == eod.trade_date.max()]
    live_icb = dict(zip(eod.ticker, eod.icb_l2))

    seeded = False
    if not a.out.exists():
        if not a.default.exists():
            print(f"FAIL  no live file and no seed: {a.default}")
            return 1
        a.out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(a.default, a.out)
        seeded = True

    base = read_map(a.out)
    known = set(base.Ticker)

    # Match the wording already in the file rather than imposing my own.
    learned = {
        live_icb[r.Ticker]: r._3
        for r in base.itertuples()
        if r.Ticker in live_icb and r._3
    }

    new = sorted(set(live_icb) - known)
    if new:
        ensure_trailing_newline(a.out)
        with open(a.out, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            for t in new:
                code = live_icb[t]
                w.writerow([t, "", learned.get(code) or prettify_icb(code), ""])

    blank = int((base["Exclusive group"] == "").sum()) + len(new)
    print(f"OK    {a.out}")
    print(f"      {'seeded from ' + a.default.name + ', ' if seeded else ''}"
          f"{len(known)} existing row(s) untouched")
    print(f"      universe {len(live_icb)} tickers as of {eod.trade_date.max()}")
    print(f"      appended: {new or 'none'}")
    print(f"      in file but not in universe: {sorted(known - set(live_icb)) or 'none'}")
    print(f"      rows still needing an Exclusive group: {blank}")

    drift = [
        f"{r.Ticker}: {r._3} -> {prettify_icb(live_icb[r.Ticker])}"
        for r in base.itertuples()
        if r.Ticker in live_icb and r._3
        and r._3 != (learned.get(live_icb[r.Ticker]) or prettify_icb(live_icb[r.Ticker]))
    ]
    if drift:
        print(f"      WARN ICB L2 changed upstream (not edited here): {drift}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
