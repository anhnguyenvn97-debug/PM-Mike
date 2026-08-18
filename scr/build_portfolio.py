"""Custom portfolio builder.

Layout of a custom portfolio:

    portfolio/<name>/
        sector_constituents_custom.csv   HAND-EDITED book (that exact name --
                                         build_portfolio_target.py reads it)
        input/
            sector_constituents.csv   verbatim baseline copy, refreshed
            forked_from.txt           which baseline vintage it came from

    --fork <name>    refresh portfolio/<name>/input/ from portfolio/baseline/

input/ is machine territory and is overwritten without asking, because nothing
you type lives there. --fork reads nothing above input/ and writes nothing
above it either, so whatever you keep up there -- whatever it is called -- is
irrelevant to this command and safe from it.

<name> must already be a folder under portfolio/, so a mistyped name fails
instead of quietly creating a portfolio nobody meant.

The weighting half lives in scr/build_portfolio_target.py: it reads your book,
checks it against the baseline sector set, resolves ratings and multipliers,
and writes portfolio/<name>/target/.

Usage
    .venv\\Scripts\\python.exe scr\\build_portfolio.py --fork hsc_strat_soe_dom
"""
import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO = ROOT / "portfolio"
BASELINE = PORTFOLIO / "baseline"
GRID = "sector_constituents.csv"
PROVENANCE = "forked_from.txt"


def fork(name: str) -> int:
    src = BASELINE / GRID
    alloc = BASELINE / "sector_allocation.csv"
    home = PORTFOLIO / name

    for p in (src, alloc):
        if not p.exists():
            print(f"FAIL  no baseline to fork from: {p}")
            print("      run scr/build_baseline.py first")
            return 1

    if not home.is_dir():
        print(f"FAIL  no such portfolio: {home}")
        print(f"      existing: {sorted(p.name for p in PORTFOLIO.iterdir() if p.is_dir())}")
        return 1

    anchor = pd.read_csv(alloc, usecols=["anchor_date"]).anchor_date.iloc[0]
    dst = home / "input" / GRID
    replaced = dst.exists()

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)

    # The grid carries no date of its own, so a fork taken today and one taken
    # after the next index review are indistinguishable on disk.
    (dst.parent / PROVENANCE).write_text(
        f"forked_from: portfolio/baseline/{GRID}\n"
        f"anchor_date: {anchor}\n"
        f"forked_at:   {datetime.now():%Y-%m-%d %H:%M}\n",
        encoding="utf-8",
    )

    rows = src.read_text(encoding="utf-8").rstrip("\n").split("\n")
    print(f"OK    {'refreshed' if replaced else 'forked'} {name} from baseline "
          f"anchor_date {anchor}")
    print(f"      {dst}")
    print(f"      {len(rows)} rows, {len(rows[0].split(','))} sectors")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fork", metavar="NAME",
                    help="refresh portfolio/NAME/input/ from the baseline grid")
    a = ap.parse_args()

    if a.fork:
        return fork(a.fork)

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
