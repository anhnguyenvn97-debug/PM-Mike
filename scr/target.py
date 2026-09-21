"""Target: the neutral allocation plus a portfolio's active weights, on a date.

Reads portfolio/<name>/book.json, the book spec (portfolio.py docstring): per
group a rating, an active pp and the investable names, plus the tactical
overlay. Ratings:

    NO | UW3 | UW2 | UW1 | AV | OW1 | OW2 | OW3

NO means out of scope: the group leaves the neutral. A negative view is UW.
The tier caps how far the active weight may go (D34):

    OW1 0..+3   OW2 0..+6   OW3 0..+9   AV 0   UW1 -3..0   UW2 -6..0   UW3 -9..0

Leaving a name out of a group's investable list is stock selection, not
allocation: the group keeps its budget and the listed names absorb the others'
weight by float cap.

The date (D57, D60). compute(as_of=d) prices everything on snap(d), the last
session on or before d ("priced as of" when they differ); the default is the
latest session. The universe is that session's params (params.at): each
group's column is every ticker trading in it that day, A-Z. The book is
evaluated on that universe by portfolio.evaluate (D59): investable names not
trading are dropped, groups not in the universe are dropped with their pp,
groups the universe gained are rated NO, and each is reported as a WARN in
"messages". Then portfolio.normalize_book applies the auto-NO rule (a group
with no investable name left, a tactical group claiming nothing) and WARNs.

Math, priced from the session's params:

    fcap_i = free_float_i * close_raw_i          params.float_cap
    b_g    = sum of fcap over g's column / total claims migrated, see below
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

Tactical groups, OFF by default (the spec's "tactical": on, and groups with
invented names claiming members). A claimed name leaves its home sector AND
takes its float cap along, so the neutral moves with it; the budget vector
still sums to 1:

    not investable  name leaves, budget STAYS   the listed names absorb it
    claimed         name leaves, budget GOES    b_g shrinks by its float cap

Claims resolve against the session's universe column, so a name left out of
its group's investable list is still claimed. Keep claimed names investable in
their home group, or switching the overlay off turns them into exclusions.
With the overlay off the tactical groups are ignored.

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
  - a date before the first session; a ticker trading on the session that the
    group map lacks (params.py)
  - unknown rating, an active pp that is not a number
  - any active check above
  - tactical overlay on with no groups; blank/duplicate group name; name reused
    from the groups; ticker claimed twice
  - every group NO
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

compute(name, as_of=, spec=, constraints=, strict=) runs everything above
without writing or printing; spec and constraints stand in for book.json and
constraints.json, so the desk previews unsaved edits through the same code.
Its "active" holds the net, the budget used and each group's allowed pp range
after the floor; "params" is the session's params frame, "universe" its
columns, "reconcile" the evaluate() report. build() is compute() plus the
writes; the desk's Record builds and records in one step, so a build without a
record is the CLI below.

Usage
    .venv\\Scripts\\python.exe scr\\target.py energy_focus
    .venv\\Scripts\\python.exe scr\\target.py energy_focus --as-of 2026-09-01
"""
import argparse
import math
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import params as params_mod
from common import (
    ALLOC,
    BOOK,
    CAPS,
    CONSTRAINTS,
    DB,
    PORTFOLIO,
    STATEMENT,
    BookError,
    load_constraints,
    load_statement,
    validate_constraints,
)

import portfolio as pf

TIERS = {"1": 3.0, "2": 6.0, "3": 9.0}      # pp limit per tier (D34)
NET_TOL = 1e-3                              # pp
EPS = 1e-12


def tier_range(rating: str) -> tuple[float, float]:
    """The active pp a rating allows, before the floor."""
    if rating in ("NO", "AV"):
        return 0.0, 0.0
    t = TIERS[rating[2]]
    return (0.0, t) if rating.startswith("OW") else (-t, 0.0)


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


def compute(name: str, portfolio: Path = PORTFOLIO, db: Path = DB, as_of=None,
            spec: dict | None = None, constraints: dict | None = None,
            strict: bool = True) -> dict:
    """Compute the target on as_of's session without writing or printing.

    spec and constraints replace book.json and constraints.json, for
    previewing unsaved edits. strict=False returns active-check faults in
    "active" instead of raising. Lines build() prints before its report are
    returned in "messages".
    """
    home = portfolio / name
    if not home.is_dir():
        raise BookError(f"no such portfolio: {home}")
    params = params_mod.at(db, as_of)
    session = params.attrs["session"]
    cols = params_mod.universe(params)
    if not cols:
        raise BookError(f"no ticker with a float cap on {session}")

    messages = []
    statement = load_statement(home)
    cons = (load_constraints(home) if constraints is None
            else validate_constraints(constraints))
    if spec is None:
        spec = pf.read_book(home)
        if spec is None:
            spec = pf.default_book(cols)
            messages.append(f"INFO  no {BOOK}; every group AV with all its names")
    evaluated, rep = pf.evaluate(spec, cols)
    messages += pf.report_lines(rep, session)
    book, flipped = pf.normalize_book(evaluated)
    for g in flipped:
        messages.append(f"WARN  {g}: no investable name left on {session}, rated NO")

    sectors = list(cols)
    tac_on = book["tactical"]["on"]
    tac_list = book["tactical"]["groups"] if tac_on else []
    if tac_on and not tac_list:
        raise BookError("tactical overlay is on but has no groups; add one or switch "
                        "the overlay off")
    tac_g = [t["name"] for t in tac_list]
    claim = {m: t["name"] for t in tac_list for m in t["members"]}
    groups = sectors + tac_g
    active = {g: float(book["groups"][g]["pp"]) for g in sectors}
    active.update({t["name"]: float(t["pp"]) for t in tac_list})
    rating = {g: book["groups"][g]["rating"] for g in sectors}
    rating.update({t["name"]: t["rating"] for t in tac_list})

    full = {g: list(cols[g]) for g in sectors}
    home_of = {t: g for g in sectors for t in full[g]}
    members = {}
    for g in sectors:
        inv = set(book["groups"][g]["investable"])
        members[g] = [t for t in full[g] if t in inv and t not in claim]
    for t in tac_list:
        members[t["name"]] = full[t["name"]] = list(t["members"])

    par = params.set_index("ticker")
    fcap = par["float_cap"]
    sector_fcap = {g: float(sum(fcap[t] for t in full[g])) for g in sectors}
    total = sum(sector_fcap.values())
    if claim:
        bweight, migrated = migrate(sector_fcap, total, claim, home_of, fcap)
    else:
        bweight = {g: v / total for g, v in sector_fcap.items()}
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
                                          "while the tactical overlay is on)"))
    any_on = any(cons[k]["on"] for k in CAPS)
    if any_on:
        sol = apply_constraints(w_raw, members, fcap, cons)
        w_cap, bound, full_g = sol["group"], sol["capped"], sol["full"]
        stock_w, pin = sol["stock"], sol["pin"]
    else:
        w_cap, bound, full_g, stock_w, pin = w_raw, set(), set(), None, {}

    sec = pd.DataFrame({
        "as_of": session,
        "group": groups,
        "kind": ["tactical" if g in tac_g else "sector" for g in groups],
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
        {"trade_date": session, "ticker": t, "sector": g, "home_sector": home_of[t],
         "rating": rating[g], "close_raw": par.at[t, "close_raw"],
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
    elif book["tactical"]["groups"]:
        tac_note = f"off -- {len(book['tactical']['groups'])} group(s) defined, not applied"
    else:
        tac_note = "off"

    return {"name": name, "as_of": session, "requested": None if as_of is None else str(as_of),
            "holdings": hold, "allocation": sec, "in_range": in_range,
            "range_note": range_note, "statement": statement, "constraints": cons,
            "any_on": any_on, "active": act, "cap_note": cap_note, "report": report,
            "tac_note": tac_note, "tac_groups": tac_g, "members": members, "full": full,
            "home_of": home_of, "params": params, "universe": cols, "reconcile": rep,
            "spec": book, "messages": messages}


def build(name: str, portfolio: Path = PORTFOLIO, db: Path = DB, as_of=None) -> dict:
    """Compute on as_of's session and write target/. Returns compute()'s result."""
    res = compute(name, portfolio, db, as_of=as_of)
    for line in res["messages"]:
        print(line)
    home, session = portfolio / name, res["as_of"]
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
    book = (f"portfolio/{name}/{BOOK}" if (home / BOOK).exists()
            else f"default (no {BOOK}): every group AV with all its names")
    (out / "built_from.txt").write_text(
        f"book:         {book}\n"
        f"as_of:        {res['requested'] or 'latest session'}\n"
        f"priced_as_of: {session}\n"
        f"params:       data/market.db session {session} (params.at)\n"
        f"active:       {act_note}\n"
        f"constraints:  {cap_note}\n"
        f"tactical:     {tac_note}\n"
        f"holdings:     {range_note}\n"
        f"built_at:     {datetime.now().astimezone():%Y-%m-%d %H:%M}\n",
        encoding="utf-8")

    live = sec[sec["rating"] != "NO"]
    dead = sec[sec["rating"] == "NO"]
    print(f"OK    {name}  priced as of {session}  constraints {cap_note}")
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
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="portfolio folder under portfolio/")
    ap.add_argument("--as-of", default=None,
                    help="YYYY-MM-DD; priced on the last session on or before it "
                         "(default: the latest)")
    a = ap.parse_args(argv)
    try:
        build(a.name, as_of=a.as_of)
    except BookError as e:
        print(f"FAIL  {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
