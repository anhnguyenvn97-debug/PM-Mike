"""Portfolio lifecycle: new, the book, screen flags, decisions, reset, delete.

Layout of a portfolio:

    portfolio/<name>/
        statement.json      setup: mandate, holdings range, rebalance rule (common.py)
        constraints.json    setup: active budget and caps (common.py)
        screens.json        loop: screen thresholds, flags only (common.py, D62)
        backtest_config.json   trading assumptions for the backtest (common.py)
        book.json           loop: the working allocation, the next decision's draft (D58)
        decisions/          loop: recorded decisions, one per effective date (common.py)
        target/             derived by scr/target.py (the last build)
        backtest_engine/    derived by scr/backtest_engine.py

The book is a spec, the same shape the desk sends and a decision stores:

    {"groups":   {"<group>": {"rating": "OW2", "pp": 4.5, "investable": ["TCB", ...]}},
     "tactical": {"on": true, "groups": [{"name": "SOE Divestment", "rating": "OW1",
                                          "pp": 2, "members": ["GAS"]}]}}

A ticker listed in its group's investable list is investable; one that is not
listed is not. A group whose list is empty (after tactical claims, when the
overlay is on) is rated NO with 0 pp, and so is a tactical group that claims
nothing: normalize_book applies this auto-NO rule and reports the groups it
flipped. write_book also checks the spec against index/group_map_live.csv: a
ticker the map puts in another group cannot be listed under this one, a group
must be in the map, a tactical name must not be a group name. A ticker the map
does not have (delisted, say) may stay in the spec; evaluate() drops it.

One evaluation rule (D59), evaluate(): a spec evaluated on a session keeps its
ratings and pp, drops investable names not trading on the session (not in that
session's params), drops groups absent from the session's universe with their
pp, and rates groups the universe gained NO. The live build, the preview, a
recorded decision and the backtest replay all go through it. New listings
never enter a book by themselves. A missing book.json is default_book: every
group AV at 0 pp with all its names.

Screen flags (D62), screen_flags(): the name screens in screens.json that are
on, evaluated on a params frame for the names given. A flag never removes a
name. position_flags() (D70) evaluates the position screens on share counts,
for a replication ticket (replicate.py); same row shape.

new <name>
    Create the folder with a default statement.json, constraints.json (all off)
    and screens.json (all off). FAILs if the folder exists, so a typo can never
    overwrite a portfolio. Name: lowercase, digits, _.

screen <name> [--as-of YYYY-MM-DD]
    Print the flags on the book's holdings as built on the session (default the
    latest). Writes nothing.

record_decision (desk: Record) -- the book as saved, built strictly on the
    effective date's session (target.compute), stored in decisions/ with the
    holdings, the flags on them, priced_as_of, kind and setup_hash. Kind rules
    (D60): the first decision is inception whatever was asked; a later one is
    period or active and must be dated after inception; re-recording the
    inception date replaces inception. One per date, a re-record replaces it.

reset <name>
    Move every file in decisions/ to decisions/archive/<YYYY-MM-DD-HHMM>/
    (nothing is deleted). book.json and target/ stay; the next Record is
    inception again.

delete <name> --yes
    Remove portfolio/<name>/ and everything in it. Without --yes it FAILs and
    deletes nothing.

Usage
    .venv\\Scripts\\python.exe scr\\portfolio.py new my_portfolio
    .venv\\Scripts\\python.exe scr\\portfolio.py screen my_portfolio --as-of 2026-09-11
    .venv\\Scripts\\python.exe scr\\portfolio.py reset my_portfolio
    .venv\\Scripts\\python.exe scr\\portfolio.py delete my_portfolio --yes
"""
import argparse
import csv
import json
import math
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import params as params_mod
from common import (
    ARCHIVE,
    BOOK,
    CONSTRAINTS,
    DB,
    DECISION_COLS,
    DECISION_LOG,
    DECISIONS,
    PORTFOLIO,
    RATINGS,
    SCREENS_FILE,
    STATEMENT,
    BookError,
    _read_json,
    as_date,
    atomic_text,
    default_constraints,
    default_screens,
    default_statement,
    dump,
    load_decisions,
    load_screens,
    setup_hash,
    validate_decision,
    validate_statement,
)

NAME = re.compile(r"^[a-z0-9_]+$")


def new(name: str, portfolio: Path = PORTFOLIO,
        statement: dict | None = None) -> Path:
    if not NAME.match(name):
        raise BookError(f"portfolio name {name!r}: use lowercase letters, "
                        "digits and _ only")
    home = portfolio / name
    if home.exists():
        raise BookError(f"{home} already exists")
    s = validate_statement(statement or default_statement())
    home.mkdir(parents=True)
    (home / STATEMENT).write_text(dump(s), encoding="utf-8")
    (home / CONSTRAINTS).write_text(dump(default_constraints()), encoding="utf-8")
    (home / SCREENS_FILE).write_text(dump(default_screens()), encoding="utf-8")
    print(f"OK    created {home}")
    print("      every group starts AV with all its names; edit the book, then record "
          "the inception decision")
    return home


# ---------- the book (D58) ----------

def default_book(cols: dict) -> dict:
    """Every group of a universe AV at 0 pp with all its names; overlay off."""
    return {"groups": {g: {"rating": "AV", "pp": 0, "investable": list(ts)}
                       for g, ts in cols.items()},
            "tactical": {"on": False, "groups": []}}


def read_book(home: Path) -> dict | None:
    """book.json as saved, or None when the portfolio has none yet."""
    p = home / BOOK
    if not p.exists():
        return None
    spec = _read_json(p)
    if not isinstance(spec, dict):
        raise BookError(f"{BOOK}: expected a JSON object")
    return spec


def _pp(rating: str, pp, where: str) -> float:
    """Active pp as stored: 4 decimals at most; NO and a missing value are 0."""
    if pp is not None and (isinstance(pp, bool) or not isinstance(pp, (int, float))
                           or not math.isfinite(pp)):
        raise BookError(f"{where}: active pp {pp!r} must be a number")
    if rating == "NO" or pp is None:
        return 0
    return round(float(pp), 4) + 0.0


def normalize_book(spec: dict, gmap: dict | None = None) -> tuple[dict, list]:
    """Validate a book spec -> (normalized spec, groups the auto-NO rule flipped).

    gmap {ticker: group} (the group map) checks membership: a ticker the map
    puts under another group, a group the map lacks, a tactical name that is a
    group name. Without it only the structure is checked (evaluate() has
    already restricted the spec to a session's universe)."""
    if not isinstance(spec, dict) or not isinstance(spec.get("groups", {}), dict):
        raise BookError("book: groups must map group name -> {rating, pp, investable}")
    groups_in = spec.get("groups", {})
    tac_in = spec.get("tactical") or {"on": False, "groups": []}
    if not isinstance(tac_in, dict) or not isinstance(tac_in.get("groups", []), list):
        raise BookError("book: tactical must be {\"on\": .., \"groups\": [..]}")
    known = set(gmap.values()) if gmap is not None else None
    if known is not None:
        stray = sorted(set(groups_in) - known)
        if stray:
            raise BookError(f"unknown group(s) in the book: {stray}")

    tac_on, claim, tac, flipped = bool(tac_in.get("on")), {}, [], []
    names = []
    for tg in tac_in.get("groups", []):
        if not isinstance(tg, dict):
            raise BookError("tactical group: expected an object")
        name = str(tg.get("name", "")).strip()
        if not name:
            raise BookError("tactical group with a blank name")
        if name in names:
            raise BookError(f"duplicate tactical group {name!r}")
        if name in groups_in or (known is not None and name in known):
            raise BookError(f"tactical group {name!r} is already a group name")
        names.append(name)
        members = list(dict.fromkeys(str(t) for t in tg.get("members", [])))
        for t in members:
            if t in claim:
                raise BookError(f"{t} is claimed by both {claim[t]} and {name}")
            claim[t] = name
        rating = tg.get("rating", "AV")
        if rating not in RATINGS:
            raise BookError(f"{name}: unknown rating {rating!r}")
        if not members and rating != "NO":
            rating = "NO"
            flipped.append(name)
        tac.append({"name": name, "rating": rating,
                    "pp": _pp(rating, tg.get("pp"), name), "members": members})
    live_claim = claim if tac_on else {}

    groups = {}
    for g in sorted(groups_in):
        s = groups_in[g]
        if not isinstance(s, dict):
            raise BookError(f"{g}: expected an object")
        rating = s.get("rating", "AV")
        if rating not in RATINGS:
            raise BookError(f"{g}: unknown rating {rating!r}")
        inv = sorted({str(t) for t in s.get("investable", [])})
        if gmap is not None:
            alien = sorted(t for t in inv if t in gmap and gmap[t] != g)
            if alien:
                raise BookError(f"{g}: {alien} belong to another group in the group map "
                                "(index/group_map_live.csv)")
        if not [t for t in inv if t not in live_claim] and rating != "NO":
            rating = "NO"
            flipped.append(g)
        groups[g] = {"rating": rating, "pp": _pp(rating, s.get("pp"), g), "investable": inv}
    return {"groups": groups, "tactical": {"on": tac_on, "groups": tac}}, flipped


def group_map(path: Path | None = None) -> dict:
    """{ticker: group} from index/group_map_live.csv (blank groups left out)."""
    g = params_mod.load_map(path or params_mod.MAP)
    g = g[g["group"] != ""]
    return dict(zip(g["ticker"], g["group"]))


def write_book(home: Path, spec: dict, map_path: Path | None = None) -> list:
    """Validate against the group map and write book.json atomically.
    Returns the groups the auto-NO rule flipped."""
    norm, flipped = normalize_book(spec, group_map(map_path))
    atomic_text(home / BOOK, dump(norm))
    return flipped


# ---------- one evaluation rule (D59) ----------

def evaluate(spec: dict, cols: dict) -> tuple[dict, dict]:
    """A spec on one session's universe {group: [tickers]} -> (spec, report).
    Investable names not trading: dropped. Groups not in the universe: dropped,
    their pp with them. Groups the universe gained: rated NO. Tactical members
    not trading: dropped.
    report = {"dropped_names": {group: [..]}, "lost_groups": {group: "OW1 (3 pp)"},
    "appeared": [..]}."""
    universe = {t for c in cols.values() for t in c}
    rep = {"dropped_names": {}, "lost_groups": {}, "appeared": []}

    def keep(where, names, home):
        out = []
        for t in names:
            if t in home:
                out.append(t)
            else:
                rep["dropped_names"].setdefault(where, []).append(t)
        return out

    groups = {}
    for g, s in (spec.get("groups") or {}).items():
        if g not in cols:
            rep["lost_groups"][g] = f"{s.get('rating', 'AV')} ({s.get('pp') or 0:g} pp)"
            continue
        groups[g] = {"rating": s.get("rating", "AV"), "pp": s.get("pp"),
                     "investable": keep(g, s.get("investable", []), set(cols[g]))}
    for g in cols:
        if g not in groups:
            groups[g] = {"rating": "NO", "pp": 0, "investable": []}
            rep["appeared"].append(g)
    tac = spec.get("tactical") or {"on": False, "groups": []}
    tac = {"on": bool(tac.get("on")),
           "groups": [{**t, "members": keep(str(t.get("name", "")), t.get("members", []),
                                            universe)}
                      for t in tac.get("groups", [])]}
    return {"groups": dict(sorted(groups.items())), "tactical": tac}, rep


def report_lines(rep: dict, session, who: str = "book") -> list[str]:
    """WARN lines for an evaluate() report."""
    out = []
    for g, names in rep["dropped_names"].items():
        out.append(f"WARN  {who}: {g}: {names} not trading on {session}; dropped")
    if rep["lost_groups"]:
        lost = ", ".join(f"{g} {v}" for g, v in rep["lost_groups"].items())
        out.append(f"WARN  {who}: groups not in the universe on {session}, dropped with "
                   f"their pp: {lost}")
    if rep["appeared"]:
        out.append(f"INFO  {who}: groups not in the book, rated NO: {rep['appeared']}")
    return out


# ---------- screen flags (D62) ----------

def screen_flags(frame: pd.DataFrame, tickers, screens: dict) -> list[dict]:
    """Flags on the given tickers from a params frame; tickers not in the
    frame (not trading) are skipped. One row per (ticker, screen) flagged."""
    p = frame.set_index("ticker") if "ticker" in frame.columns else frame
    rows = []
    t_cfg, f_cfg, o_cfg = screens["turnover"], screens["float_cap"], screens["fol"]
    for t in sorted(set(tickers)):
        if t not in p.index:
            continue
        if t_cfg["on"]:
            v = p.at[t, "turnover_21_pct"]
            if pd.isna(v):
                rows.append({"t": t, "screen": "turnover", "value": None,
                             "threshold": t_cfg["min_pct"], "why": "no data"})
            elif v < t_cfg["min_pct"]:
                rows.append({"t": t, "screen": "turnover", "value": float(v),
                             "threshold": t_cfg["min_pct"], "why": "below"})
        if f_cfg["on"]:
            v = float(p.at[t, "float_cap"]) / 1e9
            if v < f_cfg["min_bn_vnd"]:
                rows.append({"t": t, "screen": "float_cap", "value": v,
                             "threshold": f_cfg["min_bn_vnd"], "why": "below"})
        if o_cfg["on"]:
            v = p.at[t, "fol_limit"] if "fol_limit" in p.columns else None
            if v is None or pd.isna(v):
                rows.append({"t": t, "screen": "fol", "value": None,
                             "threshold": o_cfg["min_limit_pct"], "why": "no data"})
            elif 100 * float(v) < o_cfg["min_limit_pct"]:
                rows.append({"t": t, "screen": "fol", "value": 100 * float(v),
                             "threshold": o_cfg["min_limit_pct"], "why": "below"})
    return rows


def position_flags(frame: pd.DataFrame, shares: dict, screens: dict) -> list[dict]:
    """Position screens (D70) on {ticker: shares held} from the sizing
    session's params frame, one row per (ticker, screen) flagged, the shape of
    screen_flags with why "above" | "no data":

        ownership  100 x shares / outstanding_shares  > max_pct_of_shares
        float      100 x shares / free_float          > max_pct_of_float
        liquidity  shares x close_raw / (participation_pct / 100 x adv_21)
                   sessions                           > max_days

    Tickers not in the frame and zero positions are skipped."""
    p = frame.set_index("ticker") if "ticker" in frame.columns else frame
    rows = []
    own, flt, liq = screens["ownership"], screens["float"], screens["liquidity"]

    def check(t, name, v, limit):
        if v is None or pd.isna(v):
            rows.append({"t": t, "screen": name, "value": None, "threshold": limit,
                         "why": "no data"})
        elif v > limit:
            rows.append({"t": t, "screen": name, "value": float(v), "threshold": limit,
                         "why": "above"})

    for t in sorted(shares):
        n = shares[t]
        if n <= 0 or t not in p.index:
            continue
        if own["on"]:
            out = p.at[t, "outstanding_shares"]
            check(t, "ownership", 100 * n / out if out and out > 0 else None,
                  own["max_pct_of_shares"])
        if flt["on"]:
            ff = p.at[t, "free_float"]
            check(t, "float", 100 * n / ff if ff and ff > 0 else None, flt["max_pct_of_float"])
        if liq["on"]:
            adv = p.at[t, "adv_21"]
            ok = adv is not None and not pd.isna(adv) and adv > 0
            check(t, "liquidity",
                  n * float(p.at[t, "close_raw"]) / (liq["participation_pct"] / 100 * adv)
                  if ok else None, liq["max_days"])
    return rows


def flag_text(f: dict) -> str:
    unit = {"turnover": "%", "float_cap": " bn", "fol": "%", "ownership": "% of shares",
            "float": "% of float", "liquidity": " sessions"}[f["screen"]]
    if f["why"] == "no data":
        return f"{f['screen']} no data"
    sign = ">" if f["why"] == "above" else "<"
    return f"{f['screen']} {f['value']:.4g}{unit} {sign} {f['threshold']:g}{unit}"


def screen(name: str, as_of=None, portfolio: Path = PORTFOLIO, db: Path = DB) -> list[dict]:
    """Print the flags on the book's holdings as built on as_of's session."""
    import target
    home = portfolio / name
    res = target.compute(name, portfolio, db, as_of=as_of, strict=False)
    sc = load_screens(home)
    flags = screen_flags(res["params"], res["holdings"]["ticker"], sc)
    on = [k for k in sc if sc[k]["on"]]
    print(f"OK    {name}  priced as of {res['as_of']}  screens on: {on or 'none'}")
    print(f"      {len(res['holdings'])} holdings, {len({f['t'] for f in flags})} flagged "
          "(flags never remove a name; untick it in the book to exclude it)")
    for f in flags:
        print(f"      {f['t']:<6} {flag_text(f)}")
    return flags


# ---------- decisions (D56, D60, D61) ----------

def _write_log(folder: Path, rows: list[dict]) -> None:
    with open(folder / DECISION_LOG, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, DECISION_COLS, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def decision_kind(profiles: list[dict], effective: str, kind: str | None) -> str:
    """The kind a decision on `effective` must have (D60); BookError when the
    date or the kind is not allowed."""
    if not profiles or profiles[0]["effective"] == effective:
        return "inception"
    inc = profiles[0]["effective"]
    if effective < inc:
        raise BookError(f"{effective} is before inception {inc}; a later decision must "
                        "be dated after it\n      reset the portfolio to start a new inception")
    if kind not in ("period", "active"):
        raise BookError(f"choose the kind of rebalance: period or active (got {kind!r}); "
                        "only the first decision is inception")
    return kind


def record_decision(home: Path, effective=None, kind: str | None = None, note: str = "",
                    db: Path = DB) -> dict:
    """Record the book as saved (book.json, or the default book) as the
    decision for its effective date (default today): built strictly on the
    date's session, replacing any decision already on that date."""
    import target
    now = datetime.now().astimezone()
    eff = as_date(effective or now.date(), "effective date").isoformat()
    profiles = load_decisions(home)
    kind = decision_kind(profiles, eff, kind)
    res = target.compute(home.name, home.parent, db, as_of=eff, strict=True)
    spec = read_book(home)
    spec = normalize_book(spec)[0] if spec is not None else default_book(res["universe"])
    hold = res["holdings"]
    sc = load_screens(home)
    d = validate_decision({
        "id": eff, "effective": eff, "recorded_at": f"{now:%Y-%m-%d %H:%M}",
        "priced_as_of": str(res["as_of"]), "kind": kind, "setup_hash": setup_hash(home),
        "note": str(note), **spec,
        "holdings": [{"t": r.ticker, "group": r.sector, "w": float(r.target_weight)}
                     for r in hold.itertuples()],
        "flags": screen_flags(res["params"], hold["ticker"], sc)})
    folder = home / DECISIONS
    folder.mkdir(exist_ok=True)
    old = [p for p in profiles if p["effective"] == eff]
    for p in old:
        (folder / f"{p['id']}.json").unlink(missing_ok=True)
    (folder / f"{d['id']}.json").write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
    rows = [{k: p.get(k, "") for k in DECISION_COLS} for p in profiles if p not in old]
    rows = sorted(rows + [{k: d[k] for k in DECISION_COLS}], key=lambda r: r["effective"])
    _write_log(folder, rows)
    verb = "replaced" if old else "recorded"
    priced = "" if str(res["as_of"]) == eff else f", priced as of {res['as_of']}"
    print(f"OK    {verb} {kind} decision {d['id']}{priced} in "
          f"portfolio/{home.name}/{DECISIONS}/")
    print(f"      {len(hold)} holdings, {len({f['t'] for f in d['flags']})} flagged")
    return d


def reset(home: Path) -> Path:
    """Move decisions/ into decisions/archive/<stamp>/; the next Record is inception."""
    folder = home / DECISIONS
    items = [p for p in folder.iterdir() if p.name != ARCHIVE] if folder.exists() else []
    if not items:
        raise BookError(f"{home.name} has no decisions to reset")
    stamp = f"{datetime.now().astimezone():%Y-%m-%d-%H%M}"
    dest, n = folder / ARCHIVE / stamp, 1
    while dest.exists():
        n += 1
        dest = folder / ARCHIVE / f"{stamp}-{n}"
    dest.mkdir(parents=True)
    for p in items:
        shutil.move(str(p), str(dest / p.name))
    print(f"OK    reset {home.name}: {len(items)} file(s) moved to "
          f"{DECISIONS}/{ARCHIVE}/{dest.name}/; the next decision is inception")
    return dest


def delete(name: str, yes: bool = False, portfolio: Path = PORTFOLIO) -> Path:
    home = portfolio / name
    if not NAME.match(name):
        raise BookError(f"refusing to delete {home}")
    if not home.is_dir():
        raise BookError(f"no such portfolio: {home}")
    if not yes:
        raise BookError(f"this deletes {home} and everything in it; pass --yes")
    shutil.rmtree(home)
    print(f"OK    deleted {home}")
    return home


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("new").add_argument("name")
    s = sub.add_parser("screen")
    s.add_argument("name")
    s.add_argument("--as-of", default=None, help="YYYY-MM-DD, default the latest session")
    sub.add_parser("reset").add_argument("name")
    d = sub.add_parser("delete")
    d.add_argument("name")
    d.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "new":
            new(a.name)
        elif a.cmd == "screen":
            screen(a.name, a.as_of)
        elif a.cmd == "reset":
            home = PORTFOLIO / a.name
            if not home.is_dir():
                raise BookError(f"no such portfolio: {home}")
            reset(home)
        else:
            delete(a.name, a.yes)
    except BookError as e:
        print(f"FAIL  {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
