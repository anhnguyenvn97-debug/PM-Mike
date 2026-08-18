"""Target book: tilt the baseline allocation by a hand-edited rating grid.

Reads portfolio/<name>/sector_constituents_custom.csv -- the baseline grid
with rows 0-1 edited, nothing else:

    row 0   multiplier   the rating repeated (= no override), or a number
                         overriding that rating's default
    row 1   rating       AV | NO | OW | UW
    row 2   sector names UNTOUCHED, must match input/sector_constituents.csv
    row 3+  tickers      DELETE by blanking a cell; no additions, no moves
                         between columns

Defaults: NO=0.0  UW=0.75  AV=1.0  OW=1.25

Deletion is stock selection, not allocation: the sector keeps its full budget
b_g * m_g and the surviving names absorb the deleted names' weight by float
cap. Cutting a sector's budget is what ratings and multipliers are for.

Math, priced on the baseline anchor date's EOD:

    fcap_i = free_float x market_cap / shares    same basis as build_baseline.py
    w_g    = b_g * m_g / sum_h(b_h * m_h)        b_g from baseline allocation
    w_i    = w_g * fcap_i / S_g

market_cap / outstanding_shares is the session's official close -- last matched
on HOSE/HNX, VWAP on UPCoM. See build_baseline.py for why not close_adj. Both
price columns ride along in holdings.csv for the sizing stage (execution price,
rounding, cash) -- which is not this script.

Sector cap, OFF by default, read from portfolio/<name>/sector_cap.json:

    {"sector_cap": "no", "max_weight": 0.25}

It lives above input/, next to the book, because it is hand-edited and
build_portfolio.py --fork writes nothing above input/. A re-fork can never
clobber it.

A missing file and an explicit "no" are the same thing: uncapped, and
max_weight is not read at all. "yes" clips every sector to max_weight -- a
FRACTION, not a percent -- and redistributes the excess pro-rata by weight
across the sectors still under the ceiling. That redistribution can lift a
sector that was under the ceiling over it, so it repeats until nothing is
above; one pass is not enough.

The cap sits on top of the tilt and outranks it: an OW sector clipped to the
ceiling comes out with realised_tilt below 1. The tilt asks, the cap decides.
Weights INSIDE a sector are untouched -- capping moves budget between sectors,
never between names, so holdings.csv keeps its schema and its weight_in_sector.

No pending-action guard here: the anchor date is inherited from a baseline that
already refused to build on a dirty date.

The anchor date is INHERITED from baseline/sector_allocation.csv; there is no
--date here. To move it, edit index/anchor_date.json and re-run
build_baseline.py. This script re-runs that resolver read-only and WARNs if it
would now land on a different date than the baseline on disk was built with.

Guards, all fatal:
  - input/sector_constituents.csv differs from the baseline grid -> re-fork
  - sector row differs from input/, or a ticker appears that the baseline
    column does not have (an addition, or a move between sectors)
  - a surviving (non-NO) sector with every name deleted
  - unknown rating, a row-0 letter contradicting row 1, a multiplier that is
    negative or unparseable, or a nonzero multiplier on a NO sector
  - a book ticker with no parquet row on the anchor date
  - sector_cap.json unparseable, an unknown switch value, or max_weight
    missing, unparseable, <= 0 or > 1 (it is a fraction: 0.25, not 25)
  - a cap too tight to fill the book: max_weight x live sectors < 100%

Outputs, overwritten in portfolio/<name>/target/:

    sector_allocation.csv   every sector, NO ones kept at weight 0; carries
                            uncapped_weight and capped alongside weight, so
                            the redistribution stays auditable against what
                            the tilt alone asked for. Both columns are present
                            whether or not the cap is on -- uncapped
                            weight == weight and capped is False throughout
                            when it is off, so downstream never branches.
    holdings.csv            surviving tickers with target_weight
    built_from.txt          provenance, including the cap in force

Usage
    .venv\\Scripts\\python.exe scr\\build_portfolio_target.py hsc_strat_high_growth
"""
import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO = ROOT / "portfolio"
BASELINE = PORTFOLIO / "baseline"
GRID = "sector_constituents.csv"
BOOK = "sector_constituents_custom.csv"
CAP = "sector_cap.json"

DEFAULT_MULT = {"NO": 0.0, "UW": 0.75, "AV": 1.0, "OW": 1.25}


def read_grid(path: Path) -> list[list[str]]:
    # utf-8-sig + per-row trailing-blank strip: Excel leaves BOMs and pads or
    # trims trailing empty cells unpredictably. Both sides of every comparison
    # go through here, so the normalisation cancels out.
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = [[c.strip() for c in r] for r in csv.reader(f)]
    for r in rows:
        while r and r[-1] == "":
            r.pop()
    while rows and not rows[-1]:
        rows.pop()
    return rows


def apply_caps(w: dict, cap: float) -> tuple[dict, list]:
    """Clip every sector to `cap`, spreading the excess pro-rata by weight
    across the sectors still under it. Returns the new vector and the sectors
    that ended up bound, in the order they hit the ceiling.

    Iterative on purpose. Releasing a capped sector's excess pushes weight into
    everything else, which can carry a sector that was under the ceiling over
    it; a single pass would leave that sector above the cap it was meant to
    obey. Each pass freezes at least one sector, so it terminates in at most
    len(w) passes.

    Preconditions, both guarded in main(): sum(w) == 1, and cap * len(w) >= 1.
    Without the second there is no room to put the excess and the loop would
    run out of unfrozen sectors with weight left over.
    """
    w = dict(w)
    bound: list = []
    while True:
        over = [g for g, v in w.items() if g not in bound and v > cap + 1e-12]
        if not over:
            return w, bound
        bound += over
        free = [g for g in w if g not in bound]
        for g in bound:
            w[g] = cap
        if not free:
            return w, bound
        excess = 1.0 - cap * len(bound)
        s = sum(w[g] for g in free)
        for g in free:
            w[g] = excess * w[g] / s


def resolve_anchor(dates: set, cfg: Path):
    """build_baseline.py's resolver, minus --date, read-only."""
    if cfg.exists():
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8")).get("anchor_date")
            d = pd.to_datetime(raw).date() if raw else None
        except Exception:
            d = None
        if d in dates:
            return d
    return max(dates)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="portfolio folder under portfolio/")
    ap.add_argument("--parquet", type=Path, default=ROOT / "data" / "eod.parquet")
    ap.add_argument("--anchor-config", type=Path,
                    default=ROOT / "index" / "anchor_date.json")
    a = ap.parse_args()

    home = PORTFOLIO / a.name
    if not home.is_dir():
        print(f"FAIL  no such portfolio: {home}")
        print(f"      existing: {sorted(p.name for p in PORTFOLIO.iterdir() if p.is_dir())}")
        return 1

    book_p = home / BOOK
    input_p = home / "input" / GRID
    base_grid_p = BASELINE / GRID
    base_alloc_p = BASELINE / "sector_allocation.csv"

    for p, hint in ((base_grid_p, "run scr/build_baseline.py first"),
                    (base_alloc_p, "run scr/build_baseline.py first"),
                    (input_p, f"run scr/build_portfolio.py --fork {a.name}"),
                    (book_p, f"copy input/{GRID} up one level as {BOOK}, edit rows 0-1"),
                    (a.parquet, "run the /eod recipe first")):
        if not p.exists():
            print(f"FAIL  missing input: {p}")
            print(f"      {hint}")
            return 1

    # Cap config is optional, so it is not in the existence loop above. Read it
    # here anyway: a typo in a two-key JSON file should fail before the parquet
    # is touched. Feasibility needs the live sector count and waits until then.
    cap = None
    cap_p = home / CAP
    if cap_p.exists():
        try:
            cfg = json.loads(cap_p.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"FAIL  {CAP} is not readable JSON: {e}")
            return 1
        if not isinstance(cfg, dict):
            print(f"FAIL  {CAP}: expected a JSON object, got {type(cfg).__name__}")
            return 1
        switch = cfg.get("sector_cap", "no")
        if isinstance(switch, bool):
            on = switch
        elif isinstance(switch, str) and switch.strip().lower() in ("yes", "no"):
            on = switch.strip().lower() == "yes"
        else:
            print(f"FAIL  {CAP}: sector_cap is {switch!r}, want \"yes\" or \"no\"")
            return 1
        if on:
            raw = cfg.get("max_weight")
            try:
                cap = float(raw)
            except (TypeError, ValueError):
                print(f"FAIL  {CAP}: sector_cap is yes but max_weight {raw!r} "
                      "is not a number")
                return 1
            if not 0 < cap <= 1:
                print(f"FAIL  {CAP}: max_weight {cap} out of range")
                print("      it is a fraction of the book, so 25% is 0.25")
                return 1

    book = read_grid(book_p)
    forked = read_grid(input_p)
    base = read_grid(base_grid_p)

    if forked != base:
        print(f"FAIL  input/{GRID} no longer matches the baseline grid")
        print(f"      the fork predates the current baseline; re-run "
              f"scr/build_portfolio.py --fork {a.name}, then re-edit rows 0-1")
        return 1

    if len(book) < 3 or book[2] != base[2]:
        print(f"FAIL  {BOOK} sector row differs from input/{GRID}")
        print("      sector names and column order are the binding contract; "
              "sector edits belong upstream in group_map_live.csv")
        return 1

    sectors = base[2]
    for i in (0, 1):
        if len(book[i]) != len(sectors):
            print(f"FAIL  row {i} has {len(book[i])} cells, expected {len(sectors)}")
            return 1

    mult, rating = {}, {}
    for j, g in enumerate(sectors):
        r = book[1][j]
        if r not in DEFAULT_MULT:
            print(f"FAIL  {g}: unknown rating {r!r} (want AV | NO | OW | UW)")
            return 1
        o = book[0][j]
        if o in DEFAULT_MULT:
            if o != r:
                print(f"FAIL  {g}: row-0 letter {o!r} contradicts rating {r!r}; "
                      "a letter there must repeat row 1")
                return 1
            m = DEFAULT_MULT[r]
        else:
            try:
                m = float(o)
            except ValueError:
                print(f"FAIL  {g}: multiplier {o!r} is neither a rating nor a number")
                return 1
            if m < 0:
                print(f"FAIL  {g}: negative multiplier {m}")
                return 1
        if r == "NO" and m != 0:
            print(f"FAIL  {g}: rated NO but multiplier {m}; NO means excluded")
            return 1
        mult[g], rating[g] = m, r

    def column(rows, j):
        return [r[j] for r in rows[3:] if j < len(r) and r[j]]

    members, full = {}, {}
    for j, g in enumerate(sectors):
        members[g], full[g] = column(book, j), column(base, j)
        alien = sorted(set(members[g]) - set(full[g]))
        if alien:
            print(f"FAIL  {g}: {alien} not in the baseline column")
            print("      tickers can only be deleted; additions and moves "
                  "between sectors belong upstream in group_map_live.csv")
            return 1
        if mult[g] > 0 and not members[g]:
            print(f"FAIL  {g}: rated {rating[g]} but every name is deleted; "
                  "rate it NO to exclude the sector")
            return 1

    alloc = pd.read_csv(base_alloc_p)
    anchor = pd.to_datetime(alloc.anchor_date.iloc[0]).date()
    bweight = dict(zip(alloc.group, alloc.weight))
    if set(bweight) != set(sectors):
        print("FAIL  baseline sector_allocation.csv and its grid disagree on "
              "the sector set; re-run scr/build_baseline.py")
        return 1

    eod = pd.read_parquet(a.parquet)
    dates = set(eod.trade_date)
    if anchor not in dates:
        print(f"FAIL  baseline anchor_date {anchor} not in {a.parquet.name}")
        return 1

    would = resolve_anchor(dates, a.anchor_config)
    if would != anchor:
        print(f"WARN  baseline was built on {anchor}, but the resolver "
              f"({a.anchor_config.name} -> latest) now picks {would}")
        print("      re-run scr/build_baseline.py if that is intended; "
              f"this build stays on {anchor}")

    eod = eod[eod.trade_date == anchor].set_index("ticker")
    tickers = [t for g in sectors for t in members[g]]
    absent = sorted(set(tickers) - set(eod.index))
    if absent:
        print(f"FAIL  in the book but not in the parquet on {anchor}: {absent}")
        return 1

    fcap = eod.free_float * (eod.market_cap / eod.outstanding_shares)
    bad = sorted(t for t in tickers if fcap[t] <= 0)
    if bad:
        print(f"FAIL  non-positive float cap: {bad}")
        return 1

    denom = sum(bweight[g] * mult[g] for g in sectors)
    if denom <= 0:
        print("FAIL  every sector is NO; nothing to weight")
        return 1

    # NO sectors carry weight 0 and take no part in the cap: they neither
    # absorb redistributed excess nor count towards the room available for it.
    live_g = [g for g in sectors if mult[g] > 0]
    w_raw = {g: bweight[g] * mult[g] / denom for g in live_g}

    if cap is not None and cap * len(live_g) < 1 - 1e-12:
        print(f"FAIL  sector cap {cap:.2%} cannot fill the book: "
              f"{len(live_g)} live sectors x {cap:.2%} = "
              f"{cap * len(live_g):.2%}, short of 100%")
        print("      raise max_weight, or rate fewer sectors NO")
        return 1

    w_cap, bound = apply_caps(w_raw, cap) if cap is not None else (w_raw, [])

    sec = pd.DataFrame({
        "anchor_date": anchor,
        "group": sectors,
        "rating": [rating[g] for g in sectors],
        "multiplier": [mult[g] for g in sectors],
        "n_members": [len(members[g]) for g in sectors],
        "fcap": [int(sum(fcap[t] for t in members[g])) for g in sectors],
        "baseline_weight": [bweight[g] for g in sectors],
        "uncapped_weight": [w_raw.get(g, 0.0) for g in sectors],
        "weight": [w_cap.get(g, 0.0) for g in sectors],
        "capped": [g in bound for g in sectors],
    })
    sec["realised_tilt"] = sec.weight / sec.baseline_weight
    sec = sec.sort_values(["weight", "fcap"], ascending=False).reset_index(drop=True)

    hold = pd.DataFrame([
        {"trade_date": anchor, "ticker": t, "sector": g,
         "rating": rating[g], "multiplier": mult[g],
         "close_adj": eod.close_adj[t], "close_raw": eod.close_raw[t],
         "fcap": int(fcap[t]),
         "weight_in_sector": fcap[t] / sum(fcap[u] for u in members[g])}
        for g in sectors if mult[g] > 0 for t in members[g]
    ])
    wsec = dict(zip(sec.group, sec.weight))
    hold["target_weight"] = hold.weight_in_sector * hold.sector.map(wsec)
    hold = hold.sort_values("target_weight", ascending=False).reset_index(drop=True)

    out = home / "target"
    out.mkdir(exist_ok=True)
    sec.to_csv(out / "sector_allocation.csv", index=False,
               lineterminator="\n", float_format="%.8f")
    hold.to_csv(out / "holdings.csv", index=False,
                lineterminator="\n", float_format="%.8f")
    cap_note = ("off" if cap is None else
                f"{cap:.2%}, bound on {len(bound)} of {len(live_g)} live sectors")
    (out / "built_from.txt").write_text(
        f"book:        portfolio/{a.name}/{BOOK}\n"
        f"anchor_date: {anchor}\n"
        f"sector_cap:  {cap_note}\n"
        f"built_at:    {datetime.now():%Y-%m-%d %H:%M}\n",
        encoding="utf-8",
    )

    live = sec[sec.multiplier > 0]
    dead = sec[sec.multiplier == 0]
    print(f"OK    anchor_date {anchor}   sector cap {cap_note}")
    print(f"      {out / 'sector_allocation.csv'}  "
          f"{len(sec)} sectors, {len(dead)} NO")
    print(f"      {out / 'holdings.csv'}  {len(hold)} tickers")
    print()
    # The uncapped column only earns its width when a cap is in force.
    head = f"{'uncapped':>10}" if cap is not None else ""
    print(f"      {'sector':<26} {'rating':>6} {'mult':>5} {'names':>7} "
          f"{'baseline':>9}{head} {'target':>8} {'realised':>9}")
    for r in live.itertuples():
        names = f"{len(members[r.group])}/{len(full[r.group])}"
        col = f"{r.uncapped_weight:>10.2%}" if cap is not None else ""
        flag = " CAP" if r.capped else ""
        print(f"      {r.group:<26} {r.rating:>6} {r.multiplier:>5.2f} "
              f"{names:>7} {r.baseline_weight:>8.2%}{col} {r.weight:>7.2%} "
              f"{r.realised_tilt:>8.2f}x{flag}")
    if len(dead):
        print(f"      NO, weight 0: {sorted(dead.group)}")
    print(f"      target weights sum to {hold.target_weight.sum():.10f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
