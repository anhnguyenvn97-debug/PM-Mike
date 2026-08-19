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
cap. Cutting a sector's budget is what ratings, multipliers, and -- when it is
switched on -- the tactical overlay are for.

Math, priced on the baseline anchor date's EOD:

    fcap_i = free_float x market_cap / shares    same basis as build_baseline.py
    w_g    = b_g * m_g / sum_h(b_h * m_h)        b_g from baseline allocation
    w_i    = w_g * fcap_i / S_g

market_cap / outstanding_shares is the session's official close -- last matched
on HOSE/HNX, VWAP on UPCoM. See build_baseline.py for why not close_adj. Both
price columns ride along in holdings.csv for the sizing stage (execution price,
rounding, cash) -- which is not this script.

Tactical groups, OFF by default. Two hand-edited files next to the book:

    tactical_group.json   {"tactical_group": "no"}   the switch
    tactical_group.csv    the same 4-row grid as the book, except row 2 names
                          are invented here and row 3+ are claims, not members

A group named there claims its tickers away from their baseline sectors and
holds them as a group of its own, rated and multiplied exactly like a sector.
Names come from anywhere in the universe; a ticker belongs to at most one
tactical group, and a tactical group may not reuse a baseline sector's name --
both share one `group` column in the output.

The claim is resolved against the BASELINE column, not the book column, so a
name the book already deleted is still claimed and its budget still moves. That
is the one place where a name leaving a sector takes the sector's budget along:

    blank a cell   name leaves, budget STAYS   the survivors absorb it
    claimed        name leaves, budget GOES    b_g shrinks by its float cap

Total float cap is unchanged -- names move between groups, they do not leave
the universe -- so the budget vector still sums to 1:

    b_T  = sum_{i in T} fcap_i / F      F = total baseline float cap
    b_g -= sum_{i in T, home(i)=g} fcap_i / F

Switch off and the .csv is never opened: the book alone decides, exactly as it
did before the overlay existed, and a typo in the overlay cannot break a build
that does not use it. Because of that, LEAVE CLAIMED NAMES IN PLACE in the
book. Blanking them there as well makes the switch a one-way door -- off then
means deleted, not back in its sector.

A tactical group rated NO excludes its names outright, like any other NO, and
the freed budget renormalises across what is left. The switch is the dissolve
control; the rating is the allocation control.

Sector cap, OFF by default, read from portfolio/<name>/sector_cap.json:

    {"sector_cap": "no", "max_weight": 0.25}

Both switches live above input/, next to the book, because they are hand-edited
and build_portfolio.py --fork writes nothing above input/. A re-fork can never
clobber them.

A missing file and an explicit "no" are the same thing: uncapped, and
max_weight is not read at all. "yes" clips every group to max_weight -- a
FRACTION, not a percent -- and redistributes the excess pro-rata by weight
across the groups still under the ceiling. That redistribution can lift a group
that was under the ceiling over it, so it repeats until nothing is above; one
pass is not enough. Tactical groups are capped like any other group.

The cap sits on top of the tilt and outranks it: an OW group clipped to the
ceiling comes out with realised_tilt below 1. The tilt asks, the cap decides.
Weights INSIDE a group are untouched -- capping moves budget between groups,
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
  - a surviving (non-NO) sector with every name deleted, or with every
    remaining name claimed by a tactical group
  - unknown rating, a row-0 letter contradicting row 1, a multiplier that is
    negative or unparseable, or a nonzero multiplier on a NO group
  - a book ticker with no parquet row on the anchor date
  - tactical_group.json says yes but tactical_group.csv is missing; a blank or
    duplicate group name; a name reused from the baseline sectors; a ticker
    claimed twice, or claimed while it belongs to no baseline sector
  - the baseline grid and baseline/sector_allocation.csv disagree on float cap,
    checked only when something is actually migrating
  - sector_cap.json unparseable, an unknown switch value, or max_weight
    missing, unparseable, <= 0 or > 1 (it is a fraction: 0.25, not 25)
  - a cap too tight to fill the book: max_weight x live groups < 100%

Outputs, overwritten in portfolio/<name>/target/:

    sector_allocation.csv   every group, NO ones kept at weight 0; `kind` marks
                            sector vs tactical and migrated_out carries the
                            float cap a sector lost to the overlay, so the
                            transfer is auditable. Carries uncapped_weight and
                            capped alongside weight, so the redistribution
                            stays auditable against what the tilt alone asked
                            for. All of these are present whether or not either
                            switch is on -- uncapped weight == weight, capped is
                            False and migrated_out is 0 throughout when they are
                            off, so downstream never branches.
    holdings.csv            surviving tickers with target_weight; home_sector
                            keeps the baseline roll-up available for names
                            sitting in a tactical group
    built_from.txt          provenance, including both switches in force

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
TAC = "tactical_group.csv"
TAC_SWITCH = "tactical_group.json"

DEFAULT_MULT = {"NO": 0.0, "UW": 0.75, "AV": 1.0, "OW": 1.25}


class BookError(Exception):
    """A fatal, already-worded message for the person editing the book.

    main() prints it as `FAIL  <message>`; embed "\\n      " to add a hint
    line. Everything below raises instead of printing so the validation reads
    as straight-line rules rather than early returns.
    """


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


def read_switch(path: Path, key: str) -> tuple[bool, dict]:
    """Parse a hand-edited on/off JSON. A missing file is "no".

    Two callers -- sector_cap.json and tactical_group.json -- so the yes/no
    grammar, the utf-8-sig read and the wording of the failures live in one
    place. Returns the switch and the whole config, so a caller carrying a
    payload beside the switch reads it without parsing twice.
    """
    if not path.exists():
        return False, {}
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise BookError(f"{path.name} is not readable JSON: {e}")
    if not isinstance(cfg, dict):
        raise BookError(f"{path.name}: expected a JSON object, "
                        f"got {type(cfg).__name__}")
    v = cfg.get(key, "no")
    if isinstance(v, bool):
        return v, cfg
    if isinstance(v, str) and v.strip().lower() in ("yes", "no"):
        return v.strip().lower() == "yes", cfg
    raise BookError(f"{path.name}: {key} is {v!r}, want \"yes\" or \"no\"")


def parse_ratings(grid: list[list[str]], groups: list[str],
                  where: str = "") -> tuple[dict, dict]:
    """Rows 0-1 of a grid: a multiplier over a rating, one pair per column.

    The book and the tactical overlay share this grammar exactly, so they share
    the parser. `where` prefixes the failures with the offending file when it
    is not the book.
    """
    for i in (0, 1):
        if len(grid[i]) != len(groups):
            raise BookError(f"{where}row {i} has {len(grid[i])} cells, "
                            f"expected {len(groups)}")
    mult, rating = {}, {}
    for j, g in enumerate(groups):
        r = grid[1][j]
        if r not in DEFAULT_MULT:
            raise BookError(f"{where}{g}: unknown rating {r!r} "
                            "(want AV | NO | OW | UW)")
        o = grid[0][j]
        if o in DEFAULT_MULT:
            if o != r:
                raise BookError(f"{where}{g}: row-0 letter {o!r} contradicts "
                                f"rating {r!r}; a letter there must repeat row 1")
            m = DEFAULT_MULT[r]
        else:
            try:
                m = float(o)
            except ValueError:
                raise BookError(f"{where}{g}: multiplier {o!r} is neither a "
                                "rating nor a number")
            if m < 0:
                raise BookError(f"{where}{g}: negative multiplier {m}")
        if r == "NO" and m != 0:
            raise BookError(f"{where}{g}: rated NO but multiplier {m}; "
                            "NO means excluded")
        mult[g], rating[g] = m, r
    return mult, rating


def read_tactical(path: Path, sectors: list[str], home_of: dict):
    """Parse tactical_group.csv into groups, ratings, members and claims.

    Same 4-row grid as the book, but row 2 names are invented here rather than
    inherited from input/, and row 3+ are claims on names that already live in
    some baseline sector. Called only when the switch is on, so nothing in this
    file can break a build that has the overlay turned off.

    Returns (groups in file order, mult, rating, members, claim) where claim
    maps ticker -> group and is the whole contract with the rest of the script.
    """
    if not path.exists():
        raise BookError(f"{TAC_SWITCH} is yes but {path.name} is missing\n"
                        f"      write it beside the book: row 0 multiplier, "
                        f"row 1 rating, row 2 group names, row 3+ tickers")
    grid = read_grid(path)
    if len(grid) < 3:
        raise BookError(f"{path.name}: need at least 3 rows "
                        "(multiplier, rating, group names)")
    groups = grid[2]
    if not groups:
        raise BookError(f"{path.name}: row 2 names no groups")
    seen = set()
    for g in groups:
        if not g:
            raise BookError(f"{path.name}: blank group name in row 2")
        if g in seen:
            raise BookError(f"{path.name}: duplicate group {g!r}")
        if g in sectors:
            raise BookError(f"{path.name}: {g!r} is already a baseline sector\n"
                            "      sectors and tactical groups share one column "
                            "in target/sector_allocation.csv, so pick another name")
        seen.add(g)

    mult, rating = parse_ratings(grid, groups, f"{path.name} ")

    members, claim = {}, {}
    for j, g in enumerate(groups):
        members[g] = [r[j] for r in grid[3:] if j < len(r) and r[j]]
        if mult[g] > 0 and not members[g]:
            raise BookError(f"{path.name} {g}: rated {rating[g]} but claims "
                            "nothing; delete the column or rate it NO")
        for t in members[g]:
            if t not in home_of:
                raise BookError(f"{path.name} {g}: {t} is in no baseline sector\n"
                                "      a tactical group can only claim names the "
                                "universe already has; add it upstream in "
                                "index/group_map_live.csv first")
            if t in claim:
                where = (f"listed twice in {g}" if claim[t] == g
                         else f"claimed by both {claim[t]} and {g}")
                raise BookError(f"{path.name}: {t} is {where}\n"
                                "      a name belongs to at most one tactical group")
            claim[t] = g
    return groups, mult, rating, members, claim


def migrate(sector_fcap: dict, total: float, claim: dict,
            home_of: dict, fcap) -> tuple[dict, dict]:
    """Move each claimed name's float cap from its home sector to its group.

    Works on float cap rather than on the weights, because float cap is what
    the budget is a share of: the claimed name's cap leaves one bucket and
    lands in another, the denominator never moves, and the vector still sums
    to 1 by construction.

    Returns the post-migration budget over sectors AND tactical groups, plus
    the float cap each sector gave up, for the audit column.
    """
    net = {g: float(v) for g, v in sector_fcap.items()}
    out = {g: 0.0 for g in sector_fcap}
    for t, g in claim.items():
        net[home_of[t]] -= fcap[t]
        out[home_of[t]] += fcap[t]
        net[g] = net.get(g, 0.0) + fcap[t]
    return {g: v / total for g, v in net.items()}, out


def apply_caps(w: dict, cap: float) -> tuple[dict, list]:
    """Clip every group to `cap`, spreading the excess pro-rata by weight
    across the groups still under it. Returns the new vector and the groups
    that ended up bound, in the order they hit the ceiling.

    Iterative on purpose. Releasing a capped group's excess pushes weight into
    everything else, which can carry a group that was under the ceiling over
    it; a single pass would leave that group above the cap it was meant to
    obey. Each pass freezes at least one group, so it terminates in at most
    len(w) passes.

    Preconditions, both guarded in build(): sum(w) == 1, and cap * len(w) >= 1.
    Without the second there is no room to put the excess and the loop would
    run out of unfrozen groups with weight left over.
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


def build(a) -> int:
    home = PORTFOLIO / a.name
    if not home.is_dir():
        raise BookError(f"no such portfolio: {home}\n"
                        f"      existing: "
                        f"{sorted(p.name for p in PORTFOLIO.iterdir() if p.is_dir())}")

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
            raise BookError(f"missing input: {p}\n      {hint}")

    # Both switches are optional, so neither is in the existence loop above.
    # Read them here anyway: a typo in a two-key JSON file should fail before
    # the parquet is touched. Cap feasibility needs the live group count and
    # waits until then.
    cap = None
    on, cfg = read_switch(home / CAP, "sector_cap")
    if on:
        raw = cfg.get("max_weight")
        try:
            cap = float(raw)
        except (TypeError, ValueError):
            raise BookError(f"{CAP}: sector_cap is yes but max_weight {raw!r} "
                            "is not a number")
        if not 0 < cap <= 1:
            raise BookError(f"{CAP}: max_weight {cap} out of range\n"
                            "      it is a fraction of the book, so 25% is 0.25")
    tac_on, _ = read_switch(home / TAC_SWITCH, "tactical_group")

    book = read_grid(book_p)
    forked = read_grid(input_p)
    base = read_grid(base_grid_p)

    if forked != base:
        raise BookError(f"input/{GRID} no longer matches the baseline grid\n"
                        f"      the fork predates the current baseline; re-run "
                        f"scr/build_portfolio.py --fork {a.name}, then re-edit rows 0-1")

    # Unchanged by the overlay: the book is still exactly the forked grid, and
    # tactical groups live in their own file precisely so this stays an equality.
    if len(book) < 3 or book[2] != base[2]:
        raise BookError(f"{BOOK} sector row differs from input/{GRID}\n"
                        "      sector names and column order are the binding "
                        "contract; sector edits belong upstream in group_map_live.csv")

    sectors = base[2]
    mult, rating = parse_ratings(book, sectors)

    def column(rows, j):
        return [r[j] for r in rows[3:] if j < len(r) and r[j]]

    full = {g: column(base, j) for j, g in enumerate(sectors)}
    home_of = {t: g for g in sectors for t in full[g]}

    if tac_on:
        tac_g, tac_m, tac_r, tac_members, claim = read_tactical(
            home / TAC, sectors, home_of)
    else:
        tac_g, tac_m, tac_r, tac_members, claim = [], {}, {}, {}, {}

    groups = sectors + tac_g
    mult.update(tac_m)
    rating.update(tac_r)

    members = {}
    for j, g in enumerate(sectors):
        raw = column(book, j)
        alien = sorted(set(raw) - set(full[g]))
        if alien:
            raise BookError(f"{g}: {alien} not in the baseline column\n"
                            "      tickers can only be deleted; additions and "
                            "moves between sectors belong upstream in "
                            "group_map_live.csv")
        taken = [t for t in raw if t in claim]
        members[g] = [t for t in raw if t not in claim]
        if mult[g] > 0 and not members[g]:
            if taken:
                raise BookError(f"{g}: rated {rating[g]} but every name left in "
                                f"it is claimed by a tactical group ({', '.join(taken)}); "
                                "rate it NO to retire the sector")
            raise BookError(f"{g}: rated {rating[g]} but every name is deleted; "
                            "rate it NO to exclude the sector")
    for g in tac_g:
        members[g] = tac_members[g]
        full[g] = tac_members[g]

    alloc = pd.read_csv(base_alloc_p)
    anchor = pd.to_datetime(alloc.anchor_date.iloc[0]).date()
    if set(alloc.group) != set(sectors):
        raise BookError("baseline sector_allocation.csv and its grid disagree "
                        "on the sector set; re-run scr/build_baseline.py")

    eod = pd.read_parquet(a.parquet)
    dates = set(eod.trade_date)
    if anchor not in dates:
        raise BookError(f"baseline anchor_date {anchor} not in {a.parquet.name}")

    would = resolve_anchor(dates, a.anchor_config)
    if would != anchor:
        print(f"WARN  baseline was built on {anchor}, but the resolver "
              f"({a.anchor_config.name} -> latest) now picks {would}")
        print("      re-run scr/build_baseline.py if that is intended; "
              f"this build stays on {anchor}")

    eod = eod[eod.trade_date == anchor].set_index("ticker")
    # The whole baseline population, not just the book: a migrating build
    # reprices every sector's budget, deleted names included.
    priced = set(home_of) | {t for g in groups for t in members[g]}
    absent = sorted(priced - set(eod.index))
    if absent:
        raise BookError(f"in the book but not in the parquet on {anchor}: {absent}")

    fcap = eod.free_float * (eod.market_cap / eod.outstanding_shares)
    bad = sorted(t for t in priced if fcap[t] <= 0)
    if bad:
        raise BookError(f"non-positive float cap: {bad}")

    sector_fcap = dict(zip(alloc.group, alloc.fcap))
    if claim:
        # Migration is arithmetic on baseline/sector_allocation.csv, so that
        # file and the parquet have to still agree about what a sector is
        # worth. Checked only when something migrates: with the overlay off,
        # the weights are read from the file verbatim and never re-derived.
        for g in sectors:
            got = sum(fcap[t] for t in full[g])
            if abs(got / sector_fcap[g] - 1) > 1e-9:
                raise BookError(
                    f"{g}: baseline float cap {sector_fcap[g]:.0f} but the "
                    f"parquet says {got:.0f} on {anchor}\n"
                    "      the baseline is stale; re-run scr/build_baseline.py")
        bweight, migrated = migrate(sector_fcap, alloc.fcap.sum(), claim,
                                    home_of, fcap)
    else:
        bweight = dict(zip(alloc.group, alloc.weight))
        migrated = {g: 0.0 for g in sectors}

    denom = sum(bweight.get(g, 0.0) * mult[g] for g in groups)
    if denom <= 0:
        raise BookError("every group is NO; nothing to weight")

    # NO groups carry weight 0 and take no part in the cap: they neither
    # absorb redistributed excess nor count towards the room available for it.
    live_g = [g for g in groups if mult[g] > 0]
    w_raw = {g: bweight.get(g, 0.0) * mult[g] / denom for g in live_g}

    if cap is not None and cap * len(live_g) < 1 - 1e-12:
        raise BookError(f"sector cap {cap:.2%} cannot fill the book: "
                        f"{len(live_g)} live groups x {cap:.2%} = "
                        f"{cap * len(live_g):.2%}, short of 100%\n"
                        "      raise max_weight, or rate fewer groups NO")

    w_cap, bound = apply_caps(w_raw, cap) if cap is not None else (w_raw, [])

    sec = pd.DataFrame({
        "anchor_date": anchor,
        "group": groups,
        "kind": ["tactical" if g in tac_m else "sector" for g in groups],
        "rating": [rating[g] for g in groups],
        "multiplier": [mult[g] for g in groups],
        "n_members": [len(members[g]) for g in groups],
        "fcap": [int(sum(fcap[t] for t in members[g])) for g in groups],
        "migrated_out": [int(migrated.get(g, 0.0)) for g in groups],
        "baseline_weight": [bweight.get(g, 0.0) for g in groups],
        "uncapped_weight": [w_raw.get(g, 0.0) for g in groups],
        "weight": [w_cap.get(g, 0.0) for g in groups],
        "capped": [g in bound for g in groups],
    })
    sec["realised_tilt"] = sec.weight / sec.baseline_weight
    sec = sec.sort_values(["weight", "fcap"], ascending=False).reset_index(drop=True)

    hold = pd.DataFrame([
        {"trade_date": anchor, "ticker": t, "sector": g,
         "home_sector": home_of[t],
         "rating": rating[g], "multiplier": mult[g],
         "close_adj": eod.close_adj[t], "close_raw": eod.close_raw[t],
         "fcap": int(fcap[t]),
         "weight_in_sector": fcap[t] / sum(fcap[u] for u in members[g])}
        for g in groups if mult[g] > 0 for t in members[g]
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
                f"{cap:.2%}, bound on {len(bound)} of {len(live_g)} live groups")
    if tac_on:
        moved = sum(migrated.values()) / alloc.fcap.sum()
        tac_note = (f"on -- {len(tac_g)} group{'s' * (len(tac_g) != 1)}, "
                    f"{len(claim)} names, {moved:.2%} of the book migrated")
    elif (home / TAC).exists():
        tac_note = f"off -- {TAC} present but not read"
    else:
        tac_note = "off"
    (out / "built_from.txt").write_text(
        f"book:        portfolio/{a.name}/{BOOK}\n"
        f"anchor_date: {anchor}\n"
        f"sector_cap:  {cap_note}\n"
        f"tactical:    {tac_note}\n"
        f"built_at:    {datetime.now():%Y-%m-%d %H:%M}\n",
        encoding="utf-8",
    )

    live = sec[sec.multiplier > 0]
    dead = sec[sec.multiplier == 0]
    print(f"OK    anchor_date {anchor}   sector cap {cap_note}")
    print(f"      tactical {tac_note}")
    print(f"      {out / 'sector_allocation.csv'}  "
          f"{len(sec)} groups, {len(dead)} NO")
    print(f"      {out / 'holdings.csv'}  {len(hold)} tickers")
    if claim:
        # Always printed, never a warning: split across two files, a claim is
        # invisible in the book, and it is the one edit that moves a budget.
        print()
        for g in tac_g:
            named = ", ".join(f"{t} ({home_of[t]})" for t in members[g])
            print(f"      claims  {g}: {named}")
        for g in sectors:
            if migrated.get(g):
                print(f"      budget  {g:<26} "
                      f"{dict(zip(alloc.group, alloc.weight))[g]:>7.4%} -> "
                      f"{bweight[g]:>7.4%}")
        for g in tac_g:
            print(f"      budget  {g:<26} {'--':>7} -> {bweight[g]:>7.4%}")
    print()
    # The uncapped column only earns its width when a cap is in force.
    head = f"{'uncapped':>10}" if cap is not None else ""
    print(f"      {'group':<26} {'rating':>6} {'mult':>5} {'names':>7} "
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="portfolio folder under portfolio/")
    ap.add_argument("--parquet", type=Path, default=ROOT / "data" / "eod.parquet")
    ap.add_argument("--anchor-config", type=Path,
                    default=ROOT / "index" / "anchor_date.json")
    a = ap.parse_args()
    try:
        return build(a)
    except BookError as e:
        print(f"FAIL  {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
