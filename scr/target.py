"""Target: the neutral allocation plus a portfolio's hand-edited active weights.

Reads portfolio/<name>/sector_constituents_custom.csv -- the baseline grid
with rows 0-1 edited and cells blanked, nothing else:

    row 0   active pp    percentage points added to the neutral; blank = 0
    row 1   rating       NO | UW3 | UW2 | UW1 | AV | OW1 | OW2 | OW3
    row 2   group names  UNTOUCHED, must match input/sector_constituents.csv
    row 3+  tickers      DELETE by blanking; no additions, no moves

NO means out of scope: the group leaves the neutral. A negative view is UW.
The tier caps how far the active weight may go (D34):

    OW1 0..+3   OW2 0..+6   OW3 0..+9   AV 0   UW1 -3..0   UW2 -6..0   UW3 -9..0

Deletion is stock selection, not allocation: the group keeps its budget and
the surviving names absorb the deleted names' weight by float cap.

Each portfolio sits on its own anchor (input/forked_from.txt) and is priced
from that anchor's baseline, portfolio/baseline/<anchor>/, and params CSV.

Math, priced from data/params/<portfolio anchor>.csv:

    fcap_i = free_float_i * close_raw_i          params.float_cap
    b_g    = baseline allocation weight          claims migrated, see below
    n_g    = b_g / sum_h b_h                     over groups not NO: the neutral
    w_g    = n_g + a_g / 100                     a_g = the group's active pp
    w_i    = w_g * fcap_i / S_g                  S_g = surviving float cap in g

Active checks (D33-D36), all fatal, every fault listed (tilt):

    net     sum of a_g over every group is 0, within 0.001 pp
    range   a_g inside its rating's tier range; NO and AV hold 0
    floor   w_g >= 0, i.e. a_g >= -100 n_g; a group at 0% holds nothing
    budget  1/2 sum |a_g| <= active.budget_pp in constraints.json

compute(strict=False) reports the faults in "active" instead of raising and
weights max(0, w_g) renormalised, so the desk can preview a book mid-edit.
build() and the backtest engine are strict.

Tactical groups, OFF by default (tactical_group.json switch +
tactical_group.csv, same 4-row grid, row 2 invented names, row 3+ claims). A
claimed name leaves its home sector AND takes its float cap along, so the
neutral moves with it; the budget vector still sums to 1:

    blank a cell   name leaves, budget STAYS   survivors absorb it
    claimed        name leaves, budget GOES    b_g shrinks by its float cap

Claims resolve against the BASELINE column, so a name the book deleted is still
claimed. Leave claimed names in place in the book, or switching the overlay off
turns them into deletions. Switch off and the .csv is never opened.

Caps, all OFF by default, constraints.json (schema in common.py). The tilt
gives group weights u_g; the caps then move weight, never the tilt inputs.
With every cap off the tilted weights are the target exactly.

    sector  cap_g = per_group[g] or max, tactical groups included
    stock   m     = max weight of any holding
    large   T, L  = holdings above T sum to at most L (UCITS style)

Solver (apply_constraints). Every holding i gets a ceiling c_i, first m. A
group's capacity is min(cap_g, sum of its members' c_i). Then:

    1. group water-fill: split 100% pro-rata to u_g, clipping each group at
       its capacity and re-spreading the excess pro-rata over the groups
       still below theirs, until none is above
    2. stock water-fill inside each group: split the group's weight pro-rata
       to float cap, clipping at c_i the same way

A group only exceeds its capacity when every member is at its ceiling, so
excess from a stock cap stays inside its group first and spills to other
groups only when the group is full or at its sector cap (D18).

    3. large holdings: G = holdings above T. If sum(G) > L, scale G by
       f = min(1, L / sum(G)); a name that would land below T stops at T and
       leaves G, and f is recomputed on the rest. Each name in G gets
       c_i = f * w_i, each stopped name c_i = T, every other holding
       c_i = min(m, T), and steps 1-2 re-run.

Because no holding outside G may rise above T, the large set can only shrink
and the second pass settles it (D17, stop-at-T). The loop repeats until
sum(G) <= L.

Feasibility is checked before solving and FAILs, never leaving cash:
    sector  sum of caps over live groups >= 100%
    stock   m x holdings >= 100%
    joint   sum of group capacities >= 100%; with large on this is
            L + T x (holdings outside G), reported as such

Portfolio statement (statement.json, see common.py): the surviving holding
count is checked against holdings min/max and WARNs outside it. A missing
statement WARNs; an invalid one FAILs.

Guards, all fatal:
  - input/sector_constituents.csv differs from baseline/<anchor>/ -> re-fork
  - book group row differs from input/, or a ticker the baseline column lacks
  - a live (non-NO) group with every name deleted or claimed away
  - an invalidated name (screen/invalid.csv) in the book or claimed
  - unknown rating (a multiplier-grammar book says so), row 0 not a number
  - any active check above
  - a baseline ticker missing from the params CSV, or params older than baseline
  - tactical switch yes but csv missing; blank/duplicate group name; name reused
    from the sectors; ticker claimed twice or outside the universe
  - constraints.json invalid, sector.per_group naming no known group, or any
    feasibility check above

Outputs, overwritten in portfolio/<name>/target/:
    sector_allocation.csv   every group, NO kept at weight 0; kind, rating,
                            active_pp, migrated_out, baseline_weight,
                            neutral_weight, uncapped_weight (the tilt), weight
                            (after caps), capped (at sector cap), full (every
                            member at its ceiling), active_vs_neutral_pp and
                            active_vs_baseline_pp (both after caps; the
                            baseline is the universe float cap, the closest
                            proxy for VNINDEX weights market.db has)
    holdings.csv            surviving tickers with target_weight; home_sector;
                            pin = stock_max | at_threshold | large | blank
    built_from.txt          provenance and the constraints in force

compute(name, book=, tactical=, constraints=, strict=) runs everything above
without writing or printing; each override stands in for its file, so the UI
previews unsaved edits through the same code. Its "active" holds the net, the
budget used and each group's allowed pp range after the floor. build() is
compute() plus the writes.

Usage
    .venv\\Scripts\\python.exe scr\\target.py hsc_strat_high_growth
"""
import argparse
import math
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from common import (
    ALLOC,
    BOOK,
    CAPS,
    CONSTRAINTS,
    GRID,
    PARAMS,
    PORTFOLIO,
    STATEMENT,
    TAC,
    TAC_SWITCH,
    BookError,
    grid_column,
    load_constraints,
    load_statement,
    portfolio_anchor,
    read_grid,
    read_invalid,
    read_switch,
    validate_constraints,
)

RATINGS = ("NO", "UW3", "UW2", "UW1", "AV", "OW1", "OW2", "OW3")
TIERS = {"1": 3.0, "2": 6.0, "3": 9.0}      # pp limit per tier (D34)
NET_TOL = 1e-3                              # pp
EPS = 1e-12


def tier_range(rating: str) -> tuple[float, float]:
    """The active pp a rating allows, before the floor."""
    if rating in ("NO", "AV"):
        return 0.0, 0.0
    t = TIERS[rating[2]]
    return (0.0, t) if rating.startswith("OW") else (-t, 0.0)


def parse_ratings(grid: list[list[str]], groups: list[str],
                  where: str = "") -> tuple[dict, dict]:
    """Rows 0-1 of a grid: active pp over a rating, one pair per column."""
    row0 = grid[0] + [""] * (len(groups) - len(grid[0]))   # read_grid trims blanks
    for i, row in ((0, row0), (1, grid[1])):
        if len(row) != len(groups):
            raise BookError(f"{where}row {i} has {len(row)} cells, "
                            f"expected {len(groups)}")
    active, rating = {}, {}
    for j, g in enumerate(groups):
        r = grid[1][j]
        if r not in RATINGS:
            old = ("; this book still uses multipliers, convert it to active pp"
                   if r in ("OW", "UW") else "")
            raise BookError(f"{where}{g}: unknown rating {r!r} "
                            f"(want {' | '.join(RATINGS)}){old}")
        try:
            a = float(row0[j]) if row0[j] else 0.0
        except ValueError:
            a = math.nan
        if not math.isfinite(a):
            raise BookError(f"{where}{g}: active pp {row0[j]!r} is not a number")
        active[g], rating[g] = a + 0.0, r
    return active, rating


def tilt(neutral: dict, active: dict, rating: dict,
         budget: float) -> tuple[dict, list, list]:
    """Neutral plus active pp -> (group weights, faults, groups below 0%).

    neutral covers the groups in scope and sums to 1; active and rating cover
    every group. Weights are max(0, n + a/100) renormalised, which is exactly
    n + a/100 when there is no fault.
    """
    faults = []
    for g, r in rating.items():
        lo, hi = tier_range(r)
        if not lo - 1e-9 <= active[g] <= hi + 1e-9:
            allowed = "0" if lo == hi else f"{lo:+g} to {hi:+g}"
            faults.append(f"{g}: {r} allows {allowed} pp, has {active[g]:+g}")
    floored = [g for g, n in neutral.items() if 100 * n + active[g] < -1e-6]
    for g in floored:
        faults.append(f"{g}: {active[g]:+g} pp takes it below 0% "
                      f"(neutral {100 * neutral[g]:.2f}%)")
    net = sum(active.values())
    if abs(net) > NET_TOL:
        faults.append(f"active pp net to {net:+.3f}, must be 0: overweights are "
                      "funded by underweights")
    used = 0.5 * sum(abs(a) for a in active.values())
    if used > budget + NET_TOL:
        faults.append(f"active pp use {used:.2f} of a {budget:g} pp budget")
    w = {g: max(0.0, n + active[g] / 100) for g, n in neutral.items()}
    s = sum(w.values())
    if s <= 0:
        raise BookError("active weights leave nothing to hold")
    return {g: v / s for g, v in w.items()}, faults, floored


def parse_tactical(grid: list[list[str]], sectors: list[str], home_of: dict,
                   where: str = TAC):
    """Parse a tactical grid -> (groups, active, rating, members, claim)."""
    if len(grid) < 3 or not grid[2]:
        raise BookError(f"{where}: need rows multiplier, rating, group names")
    groups = grid[2]
    seen = set()
    for g in groups:
        if not g:
            raise BookError(f"{where}: blank group name in row 2")
        if g in seen:
            raise BookError(f"{where}: duplicate group {g!r}")
        if g in sectors:
            raise BookError(f"{where}: {g!r} is already a baseline group")
        seen.add(g)

    active, rating = parse_ratings(grid, groups, f"{where} ")
    members, claim = {}, {}
    for j, g in enumerate(groups):
        members[g] = grid_column(grid, j)
        if rating[g] != "NO" and not members[g]:
            raise BookError(f"{where} {g}: rated {rating[g]} but claims "
                            "nothing; delete the column or rate it NO")
        for t in members[g]:
            if t not in home_of:
                raise BookError(f"{where} {g}: {t} is in no baseline group")
            if t in claim:
                why = (f"listed twice in {g}" if claim[t] == g
                       else f"claimed by both {claim[t]} and {g}")
                raise BookError(f"{where}: {t} is {why}")
            claim[t] = g
    return groups, active, rating, members, claim


def migrate(sector_fcap: dict, total: float, claim: dict,
            home_of: dict, fcap) -> tuple[dict, dict]:
    """Move each claimed name's float cap from its home sector to its group."""
    net = {g: float(v) for g, v in sector_fcap.items()}
    out = {g: 0.0 for g in sector_fcap}
    for t, g in claim.items():
        net[home_of[t]] -= fcap[t]
        out[home_of[t]] += fcap[t]
        net[g] = net.get(g, 0.0) + fcap[t]
    return {g: v / total for g, v in net.items()}, out


def waterfill(w: dict, cap: dict, total: float = 1.0) -> tuple[dict, set]:
    """Split total pro-rata to w, clip each key at cap[key], re-spread the excess
    over keys still below their cap until none is above. Returns (out, bound).

    Pro-rata water-filling has one fixed point, so the order clips happen in
    does not matter. Precondition, checked by callers: sum(cap) >= total.
    """
    bound: set = set()
    while True:
        free = [k for k in w if k not in bound]
        rest = total - sum(cap[k] for k in bound)
        s = sum(w[k] for k in free)
        out = {k: cap[k] for k in bound}
        if free and s > 0:
            out.update({k: rest * w[k] / s for k in free})
        else:
            out.update({k: 0.0 for k in free})
        over = {k for k in free if out[k] > cap[k] + EPS}
        if not over:
            return out, bound
        bound |= over


def _fill(w_group: dict, members: dict, fcap, ceil: dict, gcap: dict) -> tuple:
    """Steps 1-2 of the solver for one set of ceilings -> (W, w, capacity, bound)."""
    capacity = {g: min(gcap[g], sum(ceil[t] for t in members[g])) for g in w_group}
    total = sum(capacity.values())
    if total < 1 - 1e-9:
        return None, None, capacity, total
    W, bound = waterfill(w_group, capacity)
    w = {}
    for g in w_group:
        part, _ = waterfill({t: float(fcap[t]) for t in members[g]},
                            {t: ceil[t] for t in members[g]}, W[g])
        w.update(part)
    return W, w, capacity, bound


def apply_constraints(w_group: dict, members: dict, fcap, cons: dict) -> dict:
    """Solve the constraints on tilted live-group weights. See module docstring.

    w_group sums to 1 over live groups; members holds each group's surviving
    tickers. Returns {"group": W, "stock": w, "capped": set, "full": set,
    "pin": {ticker: str}, "large": (T, L) or None}. Raises BookError if infeasible.
    """
    live = [g for g in w_group if w_group[g] > 0]
    wg = {g: w_group[g] for g in live}
    n = sum(len(members[g]) for g in live)
    sec, stk, lg = cons["sector"], cons["stock"], cons["large"]

    gcap = {g: math.inf for g in live}
    if sec["on"]:
        gcap = {g: sec["per_group"].get(g, sec["max"]) for g in live}
        s = sum(gcap.values())
        if s < 1 - 1e-12:
            raise BookError(f"sector cap cannot fill the book: {len(live)} live "
                            f"groups, caps sum to {s:.2%} < 100%\n"
                            "      raise sector.max or per_group, or rate fewer "
                            "groups NO")
    m = stk["max"] if stk["on"] else 1.0
    if stk["on"] and m * n < 1 - 1e-12:
        raise BookError(f"max per stock cannot fill the book: {m:.2%} x {n} "
                        f"holdings = {m * n:.2%} < 100%\n"
                        "      raise stock.max, or add names")

    ceil = {t: m for g in live for t in members[g]}
    T, L = (lg["threshold"], lg["aggregate"]) if lg["on"] else (None, None)
    stopped: set = set()
    kept: set = set()
    for _ in range(n + 2):
        W, w, capacity, bound = _fill(wg, members, fcap, ceil, gcap)
        if W is None:
            if lg["on"]:
                small = n - len(kept)
                raise BookError(
                    f"constraints cannot fill the book: capacity {bound:.2%} < 100%\n"
                    f"      large holdings: aggregate {L:.2%} + threshold {T:.2%} x "
                    f"{small} holdings outside the large set = "
                    f"{min(1.0, L) + T * small:.2%} at most\n"
                    "      raise large.aggregate or large.threshold, loosen the "
                    "other caps, or add names")
            raise BookError(f"constraints cannot fill the book together: "
                            f"capacity {bound:.2%} < 100%\n"
                            "      loosen the sector or stock cap, or add names")
        if not lg["on"]:
            break
        big = [t for t in w if w[t] > T + EPS]
        if sum(w[t] for t in big) <= L + 1e-9:
            break
        cur = list(big)
        while cur:
            f = min(1.0, L / sum(w[t] for t in cur))
            drop = [t for t in cur if w[t] * f < T]
            if not drop:
                break
            stopped |= set(drop)
            cur = [t for t in cur if t not in drop]
        kept = set(cur)
        f = min(1.0, L / sum(w[t] for t in cur)) if cur else 1.0
        ceil = {t: (w[t] * f if t in kept else min(m, T)) for t in ceil}
    else:
        raise BookError("constraint solver did not settle; loosen a constraint")

    pin = {}
    for t, v in w.items():
        if stk["on"] and v >= m - 1e-9:
            pin[t] = "stock_max"
        elif lg["on"] and abs(v - T) <= 1e-9:
            pin[t] = "at_threshold"
        elif lg["on"] and v > T + EPS:
            pin[t] = "large"
        else:
            pin[t] = ""
    capped = {g for g in bound if sec["on"] and capacity[g] >= gcap[g] - EPS}
    full = {g for g in live if W[g] >= sum(ceil[t] for t in members[g]) - 1e-9
            and (stk["on"] or lg["on"])}
    return {"group": W, "stock": w, "capped": capped, "full": full, "pin": pin,
            "large": (T, L) if lg["on"] else None}


def compute(name: str, portfolio: Path = PORTFOLIO, params_dir: Path = PARAMS,
            book: list[list[str]] | None = None, tactical: dict | None = None,
            constraints: dict | None = None, strict: bool = True) -> dict:
    """Compute the target without writing or printing anything.

    Each override replaces the file on disk, for previewing unsaved edits:
        book         the book grid (rows 0-2 + ticker rows), as read_grid returns
        tactical     {"on": bool, "grid": tactical grid or None}
        constraints  a constraints dict, validated like constraints.json
    strict=False returns active-check faults in "active" instead of raising.
    Lines build() would print before its report are returned in "messages".
    """
    home = portfolio / name
    if not home.is_dir():
        raise BookError(f"no such portfolio: {home}")
    anchor_s = portfolio_anchor(home)
    baseline = portfolio / "baseline" / anchor_s

    book_p, input_p = home / BOOK, home / "input" / GRID
    base_grid_p, base_alloc_p = baseline / GRID, baseline / ALLOC
    needed = [(base_grid_p, f"run scr/portfolio.py fork {name} --anchor {anchor_s}"),
              (base_alloc_p, f"run scr/portfolio.py fork {name} --anchor {anchor_s}"),
              (input_p, f"run scr/portfolio.py fork {name}")]
    if book is None:
        needed.append((book_p, f"run scr/portfolio.py fork {name}"))
    for p, hint in needed:
        if not p.exists():
            raise BookError(f"missing input: {p}\n      {hint}")

    messages = []
    statement = load_statement(home)
    cons = (load_constraints(home) if constraints is None
            else validate_constraints(constraints))
    if tactical is None:
        tac_on, _ = read_switch(home / TAC_SWITCH, "tactical_group")
        tac_grid = read_grid(home / TAC) if tac_on and (home / TAC).exists() else None
        if tac_on and tac_grid is None:
            raise BookError(f"{TAC_SWITCH} is yes but {TAC} is missing")
    else:
        tac_on, tac_grid = bool(tactical.get("on")), tactical.get("grid")
        if tac_on and not tac_grid:
            raise BookError("tactical overlay is on but has no groups; add one "
                            "or switch the overlay off")
    invalid = set(read_invalid(home))

    book = read_grid(book_p) if book is None else book
    forked, base = read_grid(input_p), read_grid(base_grid_p)
    if forked != base:
        raise BookError(f"input/{GRID} no longer matches the baseline grid\n"
                        f"      re-run scr/portfolio.py fork {name}")
    if len(book) < 3 or book[2] != base[2]:
        raise BookError(f"{BOOK} group row differs from input/{GRID}\n"
                        "      group names and order are the binding contract; "
                        f"re-run scr/portfolio.py fork {name}")

    sectors = base[2]
    active, rating = parse_ratings(book, sectors)
    full = {g: grid_column(base, j) for j, g in enumerate(sectors)}
    home_of = {t: g for g in sectors for t in full[g]}

    if tac_on:
        tac_g, tac_a, tac_r, tac_members, claim = parse_tactical(
            tac_grid, sectors, home_of)
    else:
        tac_g, tac_a, tac_r, tac_members, claim = [], {}, {}, {}, {}
    groups = sectors + tac_g
    active.update(tac_a)
    rating.update(tac_r)

    members = {}
    for j, g in enumerate(sectors):
        raw = grid_column(book, j)
        alien = sorted(set(raw) - set(full[g]))
        if alien:
            raise BookError(f"{g}: {alien} not in the baseline column\n"
                            "      tickers can only be deleted; additions and "
                            "moves belong in index/group_map_live.csv")
        taken = [t for t in raw if t in claim]
        members[g] = [t for t in raw if t not in claim]
        if rating[g] != "NO" and not members[g]:
            if taken:
                raise BookError(f"{g}: rated {rating[g]} but every name left is "
                                f"claimed ({', '.join(taken)}); rate it NO")
            raise BookError(f"{g}: rated {rating[g]} but every name is deleted; "
                            "rate it NO to exclude the group")
    for g in tac_g:
        members[g] = full[g] = tac_members[g]

    bad = sorted(invalid & {t for g in groups for t in members[g]})
    if bad:
        raise BookError(f"invalidated names are investable or claimed: {bad}\n"
                        f"      remove them from {BOOK} / {TAC}, or restore them "
                        f"(scr/portfolio.py screen {name} --restore ...)")

    alloc = pd.read_csv(base_alloc_p)
    anchor = pd.to_datetime(alloc["anchor_date"].iloc[0]).date()
    if str(anchor) != anchor_s:
        raise BookError(f"baseline/{anchor_s}/{ALLOC} says anchor {anchor}; "
                        f"re-run scr/baseline.py --date {anchor_s}")
    if set(alloc["group"]) != set(sectors):
        raise BookError("baseline sector_allocation.csv and its grid disagree; "
                        f"re-run scr/baseline.py --date {anchor_s}")

    pp = params_dir / f"{anchor}.csv"
    if not pp.exists():
        raise BookError(f"missing {pp}\n      run scr/params.py --date {anchor}")
    if pp.stat().st_mtime > base_alloc_p.stat().st_mtime + 1:
        raise BookError(f"{pp.name} was rebuilt after the baseline\n"
                        f"      re-run scr/portfolio.py fork {name} --anchor {anchor}")
    params = pd.read_csv(pp).set_index("ticker")

    absent = sorted(set(home_of) - set(params.index))
    if absent:
        raise BookError(f"baseline tickers missing from {pp.name}: {absent}")
    fcap = params["float_cap"]

    sector_fcap = dict(zip(alloc["group"], alloc["fcap"]))
    total = alloc["fcap"].sum()
    if claim:
        for g in sectors:
            got = sum(fcap[t] for t in full[g])
            if abs(got / sector_fcap[g] - 1) > 1e-9:
                raise BookError(f"{g}: baseline float cap {sector_fcap[g]:.0f} "
                                f"but params say {got:.0f}; re-run baseline.py")
        bweight, migrated = migrate(sector_fcap, total, claim, home_of, fcap)
    else:
        bweight = dict(zip(alloc["group"], alloc["weight"]))
        migrated = {g: 0.0 for g in sectors}

    live_g = [g for g in groups if rating[g] != "NO"]
    scope = sum(bweight.get(g, 0.0) for g in live_g)
    if scope <= 0:
        raise BookError("every group is NO; nothing to weight")
    neutral = {g: bweight.get(g, 0.0) / scope for g in live_g}
    budget = cons["active"]["budget_pp"]
    w_raw, faults, _ = tilt(neutral, active, rating, budget)
    if faults and strict:
        raise BookError("active weights break the book rules:\n      "
                        + "\n      ".join(faults))
    held_g = [g for g in live_g if w_raw[g] > EPS]
    w_raw = {g: w_raw[g] for g in held_g}
    rng = {}
    for g in groups:
        lo, hi = tier_range(rating[g])
        rng[g] = (max(lo, -100 * neutral[g]) if g in neutral else lo, hi)
    act = {"net_pp": sum(active.values()),
           "used_pp": 0.5 * sum(abs(a) for a in active.values()),
           "budget_pp": budget, "faults": faults, "range": rng}

    if cons["sector"]["on"]:
        stray = sorted(set(cons["sector"]["per_group"]) - set(groups))
        if stray:
            raise BookError(f"{CONSTRAINTS}: sector.per_group names no known group: "
                            f"{stray}" + ("" if tac_on else
                                          "\n      (tactical groups count only "
                                          "while the tactical switch is on)"))
    any_on = any(cons[k]["on"] for k in CAPS)
    if any_on:
        sol = apply_constraints(w_raw, members, fcap, cons)
        w_cap, bound, full_g = sol["group"], sol["capped"], sol["full"]
        stock_w, pin = sol["stock"], sol["pin"]
    else:
        w_cap, bound, full_g, stock_w, pin = w_raw, set(), set(), None, {}

    sec = pd.DataFrame({
        "anchor_date": anchor,
        "group": groups,
        "kind": ["tactical" if g in tac_a else "sector" for g in groups],
        "rating": [rating[g] for g in groups],
        "active_pp": [active[g] for g in groups],
        "n_members": [len(members[g]) for g in groups],
        "fcap": [float(sum(fcap[t] for t in members[g])) for g in groups],
        "migrated_out": [float(migrated.get(g, 0.0)) for g in groups],
        "baseline_weight": [bweight.get(g, 0.0) for g in groups],
        "neutral_weight": [neutral.get(g, 0.0) for g in groups],
        "uncapped_weight": [w_raw.get(g, 0.0) for g in groups],
        "weight": [w_cap.get(g, 0.0) for g in groups],
        "capped": [g in bound for g in groups],
        "full": [g in full_g for g in groups],
    })
    sec["active_vs_neutral_pp"] = 100 * (sec["weight"] - sec["neutral_weight"])
    sec["active_vs_baseline_pp"] = 100 * (sec["weight"] - sec["baseline_weight"])
    sec = sec.sort_values(["weight", "fcap"], ascending=False).reset_index(drop=True)

    hold = pd.DataFrame([
        {"trade_date": anchor, "ticker": t, "sector": g, "home_sector": home_of[t],
         "rating": rating[g], "close_raw": params.at[t, "close_raw"],
         "fcap": float(fcap[t]),
         "weight_in_sector": fcap[t] / sum(fcap[u] for u in members[g])}
        for g in held_g for t in members[g]
    ])
    if stock_w is None:
        hold["target_weight"] = hold["weight_in_sector"] * hold["sector"].map(
            dict(zip(sec["group"], sec["weight"])))
    else:
        hold["target_weight"] = hold["ticker"].map(stock_w)
        hold["weight_in_sector"] = hold["target_weight"] / hold["sector"].map(w_cap)
    hold["pin"] = hold["ticker"].map(pin).fillna("")
    hold = hold.sort_values("target_weight", ascending=False).reset_index(drop=True)

    n = len(hold)
    if statement is None:
        range_note = f"no {STATEMENT}"
        in_range = None
    else:
        lo, hi = statement["holdings"]["min"], statement["holdings"]["max"]
        in_range = lo <= n <= hi
        range_note = f"{n} holdings vs range {lo}-{hi}: " + \
            ("within" if in_range else "OUTSIDE")

    notes, report = [], {}
    s_c, k_c, l_c = cons["sector"], cons["stock"], cons["large"]
    if s_c["on"]:
        per = f", {len(s_c['per_group'])} per-group" if s_c["per_group"] else ""
        report["sector"] = {"capped": sorted(bound), "live": len(held_g)}
        notes.append(f"sector {s_c['max']:.2%}{per} (capped {len(bound)} of "
                     f"{len(held_g)} live groups)")
    if k_c["on"]:
        at_max = int((hold["pin"] == "stock_max").sum())
        report["stock"] = {"at_max": at_max}
        notes.append(f"stock {k_c['max']:.2%} ({at_max} at max)")
    if l_c["on"]:
        big = hold.loc[hold["target_weight"] > l_c["threshold"] + EPS, "target_weight"]
        at_t = int((hold["pin"] == "at_threshold").sum())
        report["large"] = {"n": len(big), "sum": float(big.sum()), "at_threshold": at_t}
        notes.append(f"large >{l_c['threshold']:.2%} sum <= {l_c['aggregate']:.2%} "
                     f"({len(big)} large, {big.sum():.2%}; {at_t} at threshold)")
    cap_note = "; ".join(notes) if notes else "off"
    if tac_on:
        tac_note = (f"on -- {len(tac_g)} group(s), {len(claim)} names, "
                    f"{sum(migrated.values()) / total:.2%} of the book migrated")
    elif (home / TAC).exists():
        tac_note = f"off -- {TAC} present but not read"
    else:
        tac_note = "off"

    return {"name": name, "anchor": anchor, "anchor_s": anchor_s, "holdings": hold,
            "allocation": sec, "in_range": in_range, "range_note": range_note,
            "statement": statement, "constraints": cons, "any_on": any_on,
            "active": act,
            "cap_note": cap_note, "report": report, "tac_note": tac_note,
            "tac_groups": tac_g, "members": members, "full": full,
            "home_of": home_of, "params_file": pp.name, "messages": messages}


def build(name: str, portfolio: Path = PORTFOLIO, params_dir: Path = PARAMS) -> dict:
    """Compute and write the target. Returns a summary dict for callers/tests."""
    res = compute(name, portfolio, params_dir)
    for line in res["messages"]:
        print(line)
    home, anchor = portfolio / name, res["anchor"]
    sec, hold, members, full = (res["allocation"], res["holdings"], res["members"],
                                res["full"])
    cap_note, tac_note, range_note = res["cap_note"], res["tac_note"], res["range_note"]
    in_range, any_on, n = res["in_range"], res["any_on"], len(res["holdings"])

    out = home / "target"
    out.mkdir(exist_ok=True)
    sec.to_csv(out / ALLOC, index=False, lineterminator="\n", float_format="%.8f")
    hold.to_csv(out / "holdings.csv", index=False, lineterminator="\n",
                float_format="%.8f")
    act = res["active"]
    act_note = (f"net {act['net_pp']:+.3f} pp, {act['used_pp']:.2f} of a "
                f"{act['budget_pp']:g} pp budget")
    (out / "built_from.txt").write_text(
        f"book:        portfolio/{name}/{BOOK}\n"
        f"anchor_date: {anchor}\n"
        f"baseline:    portfolio/baseline/{res['anchor_s']}/\n"
        f"params:      data/params/{res['params_file']}\n"
        f"active:      {act_note}\n"
        f"constraints: {cap_note}\n"
        f"tactical:    {tac_note}\n"
        f"holdings:    {range_note}\n"
        f"built_at:    {datetime.now().astimezone():%Y-%m-%d %H:%M}\n",
        encoding="utf-8")

    live = sec[sec["rating"] != "NO"]
    dead = sec[sec["rating"] == "NO"]
    print(f"OK    {name}  anchor_date {anchor}  constraints {cap_note}")
    print(f"      active {act_note}")
    print(f"      tactical {tac_note}")
    print(f"      {out / ALLOC}  {len(sec)} groups, {len(dead)} NO")
    print(f"      {out / 'holdings.csv'}  {n} tickers")
    for g in res["tac_groups"]:
        print(f"      claims  {g}: "
              + ", ".join(f"{t} ({res['home_of'][t]})" for t in members[g]))
    print()
    head = f" {'tilted':>8}" if any_on else ""
    print(f"      {'group':<26} {'rating':>6} {'pp':>6} {'names':>7} {'baseline':>8} "
          f"{'neutral':>8}{head} {'target':>8} {'vs neut':>8}")
    for r in live.itertuples():
        names = f"{len(members[r.group])}/{len(full[r.group])}"
        col = f" {r.uncapped_weight:>8.2%}" if any_on else ""
        flag = (" CAP" if r.capped else "") + (" FULL" if r.full else "")
        print(f"      {r.group:<26} {r.rating:>6} {r.active_pp:>+6.2f} {names:>7} "
              f"{r.baseline_weight:>8.2%} {r.neutral_weight:>8.2%}{col} "
              f"{r.weight:>8.2%} {r.active_vs_neutral_pp:>+8.2f}{flag}")
    if len(dead):
        print(f"      NO, weight 0: {sorted(dead['group'])}")
    print(f"      target weights sum to {hold['target_weight'].sum():.10f}")
    if in_range is False:
        print(f"WARN  {range_note}")
    elif res["statement"] is None:
        print(f"WARN  no {STATEMENT}; holding range not checked "
              f"(scr/portfolio.py new creates one)")
    else:
        print(f"      {range_note}")
    return {"anchor": anchor, "holdings": hold, "allocation": sec,
            "in_range": in_range}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="portfolio folder under portfolio/")
    a = ap.parse_args(argv)
    try:
        build(a.name)
    except BookError as e:
        print(f"FAIL  {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
