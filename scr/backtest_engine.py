"""Backtest engine: a portfolio replayed under its own mandate against a benchmark.

Reads data/market.db and the desk's files only:

    statement.json          rebalance mandate + holdings range (required)
    book.json, constraints.json
                            through target.compute, exactly as the target
    decisions/              the recorded decisions (replay)
    backtest_config.json    trading assumptions (common.py; defaults if absent)

Every book is evaluated on the universe of the last session in market.db
(target.compute with as_of = that session, portfolio.evaluate, D59): the
group map and the listed names of today are held backwards, so survivorship
bias flatters every level; the output says so. Each session then prices the
book on its own float cap (below).

Which book is in force (D53, D61). run(timeline=None) holds book.json
throughout: the mechanical backtest. run(timeline=profiles) replays the
decision log (common.load_decisions): each profile is evaluated like the live
book and loaded non-strict, so a lost group's pp become a WARN naming the
decision and tilt renormalises. Before the first decision (inception) the
window holds the inception profile; from each decision on, it is the standing
target that calendar, drift and breach rebalances trade to until the next one.
Statement and constraints are always today's (a decision's setup_hash only
flags a difference). Placing the profiles on sessions: an effective date snaps
forward to a session; every profile at or before the start collapses to the
start, the latest winning; two on one session, the last wins; one after the
history is reported and not applied. If none lands on the start, the first
opens the window and a message says so.

The standing target (D50) is derived on the session that sets it --
inception, each decision, each calendar boundary -- from the book in force,
and held until the next one. With frequency null only decisions re-derive it.
Its weights on that session d are target.py's math on d's float cap:

    fcap_i = free_float_i x close_raw_i       on d, never close_adj or market_cap
    b_g    = sum of fcap over the FULL baseline column / total, claims migrated
    n_g    = b_g / sum b                      over live groups with a priced name
    w_g    = target.tilt(n, active pp)        n_g + a_g / 100, floored at 0
    w_i    = split by fcap, or target.apply_constraints when any cap is on

On the last session these are target.compute's weights (the book passed its
strict active checks there). A live group with no priced name on d drops out, its pp
with it, and the rest renormalise (reported per rebalance). A group whose
neutral on d is smaller than its underweight is held at 0% (counted in the
messages). A cap that cannot be solved on a past date FAILs; the book never
holds cash.

Triggers, checked at each close while no fill is pending, first match wins:

    decision   a profile whose session has come and is not yet applied. One
               that lands while a fill is pending applies at the first close
               with none pending (deferred_from); one on a calendar boundary is
               a single decision fill (also = calendar). The calendar clock is
               never moved by a decision (D55).
    calendar   statement.json frequency 2W | 1M | 1Q | null: the first session
               of each period. 1M / 1Q are calendar periods; 2W counts 14-day
               periods from the Monday of the start session's week. null:
               inception only.
    breach     a cap in constraints.json broken by more than statement.json
               rebalance.breach_tolerance (a missing key means 10%) of its limit
               by the drifted weights (D43, D49): at 10% a name above
               1.1 x stock.max; the names above 1.1 x large.threshold summing
               above 1.1 x large.aggregate; a group above 1.1 x its sector cap.
               Names the solver pins at a cap do not trade on the first uptick.
               A null tolerance switches breach off even with caps on.
    drift      statement.json drift_threshold: 1/2 sum over groups of
               |held - standing target| above the threshold.

Only inception, decision and calendar derive a new standing target; breach
and drift trade against the one held, and drift is measured against it (D50,
superseding D42). Fill policy per trigger (D51):

    full   inception, decision, calendar, drift: the whole book to the
           standing target (names outside the new book are sold).
    edge   breach: target.apply_constraints on the HELD weights (group weights
           = held group sums, pro-rata key = held stock weights), so every cap
           the drifted book breaks is clipped to its limit and the excess
           spills within its scope as in D18; the rest is untouched. When the
           clip is infeasible (dead names shrank capacity) the fill falls back
           to full, a message says so, and breach checks pause until the next
           calendar date (that target breaks the cap too; re-trading it every
           session would change nothing).

A name held or in the standing target whose float cap is missing or 0 on the
decision session is sold: zeroed in the vector traded to and in the standing
target, the rest renormalised. It is listed in the fill's dropped column with
the groups the derivation dropped (a name whose whole group dropped is listed
by its group).

The start session always decides inception. A decision observes session t's
close and fills at the OPEN of t + lag_sessions (D52); lag 0 fills at the
close of t itself. No new decision while a fill is pending: at lag 1 the fill
is done before the next close, so every close is checked; at lag 2 or more the
sessions between the decision and the fill session are not (the trade is in
flight). The portfolio is cash until the first fill.

Returns: total return on close_adj (dividends reinvested on the ex-date, D24);
a name with no print carries its last price. Each session is two legs: the
held weights earn close -> open (open_adj / previous close_adj), a fill trades
at the open, the new weights earn open -> close (close_adj / open_adj). With no
fill the legs compound to the close-to-close return. A name with an invalid
open_adj on a session uses its close_adj for both legs (one INFO message if
that hits a fill session). Between fills weights drift with returns. Costs on
a fill: inception pays brokerage on the buys only; later
fills pay one-way turnover x (2 x brokerage + sell tax).

Benchmark: any code in index_prices (VNINDEX, VN30, VN100), a PRICE index
rebased to the start close. The portfolio leads it by roughly the dividend
yield; labelled, not corrected. A benchmark missing a session in the window
FAILs.

Statistics (252 sessions a year, population std):
    total return, annualised = (1 + total)^(252 / (sessions - 1)) - 1,
    volatility, Sharpe = (annualised - risk_free_rate) / volatility, max
    drawdown; vs benchmark: excess (total, pp), tracking error of daily active
    returns, information ratio = annualised difference / tracking error, beta;
    turnover after inception, costs including inception, rebalance counts.

Start: an ISO date, snapped forward to the first session on or after it; a
date before the history starts at the first session. The window must leave at
least lag + 2 sessions.

run(name, start, benchmark, config=, timeline=) computes without writing (the
desk uses it; config stands in for backtest_config.json). Its "benchmarks"
holds the rebased curve and statistics for EVERY code with a close on each
session of the window, so a page can switch benchmark without re-running; the
requested benchmark must be one of them. Its "timeline" (None without one)
lists each profile: id, effective, kind, note, setup_hash, placed and applied
sessions, status, evaluate report. Its "now" holds the end state: group drift
of the held weights against the standing target and the first cap they break
by more than the tolerance (None when none).

monitor(name) is the desk's Monitor (D63): run() from the last decision's
session to the last session with only that decision in force, reporting the
fills since, "now", the next calendar date and the screen flags on the
standing target's names. A decision after the history, or one too recent to
leave lag + 2 sessions, returns only its status and the flags. It writes
nothing and never trades anything real: the paper portfolio of the standing
target, not broker fills or cash.

build() replays decisions/ when present (--mechanical: book.json) and adds the
files, overwritten in portfolio/<name>/backtest_engine/:

    equity.csv         date, portfolio, benchmark (both 1.0 at the start close)
    rebalances.csv     decision, fill, trigger (inception | decision | calendar |
                       breach | drift), also (calendar when a decision falls on
                       a boundary), profile (decision id or live),
                       deferred_from, policy (full | edge), target_as_of (the
                       session that derived the standing target), group drift
                       vs the standing target, turnover, cost, holdings,
                       in_range, dropped (groups with no priced name, dead names)
    holdings_end.csv   ticker, group, drifted weight at the end, standing target
    summary.csv        one row: window, benchmark, config, statistics
    built_from.txt     provenance, mandate, decisions replayed, caveats

Usage
    .venv\\Scripts\\python.exe scr\\backtest_engine.py energy_focus
    .venv\\Scripts\\python.exe scr\\backtest_engine.py financial_test --start 2026-04-01 --benchmark VN30
    .venv\\Scripts\\python.exe scr\\backtest_engine.py energy_focus --mechanical
"""
import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import params as params_mod
import target
from common import (
    BOOK,
    BREACH_TOL,
    BT_CONFIG,
    DB,
    PORTFOLIO,
    STATEMENT,
    BookError,
    load_backtest_config,
    load_decisions,
    load_screens,
    setup_hash,
    validate_backtest_config,
)

import portfolio as pf

YEAR = 252
OUT = "backtest_engine"
EPS = 1e-12


# --------------------------------------------------------------- inputs

def load_market(db: Path) -> dict:
    """Sessions, close_adj, open_adj and float cap by ticker, benchmark closes by code."""
    if not Path(db).exists():
        raise BookError(f"missing {db}\n      run scr/ingest.py (Rebuild database)")
    con = duckdb.connect(str(db), read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if "index_prices" not in tables:
            raise BookError(f"{Path(db).name} has no benchmarks\n      add an index "
                            "export to data/fiinpro/ and rebuild the database")
        px = con.execute("SELECT trade_date, ticker, close_adj, open_adj, "
                         "free_float * close_raw AS fcap FROM prices").df()
        ix = con.execute("SELECT trade_date, code, close FROM index_prices").df()
    finally:
        con.close()
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    ix["trade_date"] = pd.to_datetime(ix["trade_date"])
    adj = px.pivot(index="trade_date", columns="ticker", values="close_adj").sort_index()
    return {"sessions": adj.index, "adj": adj,
            "open": px.pivot(index="trade_date", columns="ticker", values="open_adj").reindex(adj.index),
            "fcap": px.pivot(index="trade_date", columns="ticker", values="fcap").reindex(adj.index),
            "bench": ix.pivot(index="trade_date", columns="code", values="close").reindex(adj.index)}


def load_book(name: str, portfolio: Path = PORTFOLIO, db: Path = DB,
              spec: dict | None = None, as_of=None, strict: bool = True) -> dict:
    """The portfolio as target.compute sees it on as_of's session, plus the
    investable names. spec overrides book.json (a replayed profile);
    strict=False returns the active-rule faults in "faults" instead of raising."""
    r = target.compute(name, portfolio, db, as_of=as_of, spec=spec, strict=strict)
    if r["statement"] is None:
        raise BookError(f"portfolio/{name}/{STATEMENT} is missing; the backtest "
                        "follows its rebalance mandate")
    alloc = r["allocation"]
    active = dict(zip(alloc["group"], alloc["active_pp"]))
    rating = dict(zip(alloc["group"], alloc["rating"]))
    sectors = [g for g, k in zip(alloc["group"], alloc["kind"]) if k == "sector"]
    tac = list(r["tac_groups"])
    live = [g for g in sectors + tac if rating[g] != "NO"]
    group_of = {t: g for g in live for t in r["members"][g]}
    return {"name": name, "as_of": r["as_of"], "compute": r, "active": active,
            "rating": rating, "sectors": sectors, "tac": tac, "live": live,
            "claim": {t: g for g in tac for t in r["members"][g]},
            "names": sorted(group_of), "group_of": group_of,
            "rebalance": r["statement"]["rebalance"],
            "holdings": r["statement"]["holdings"], "faults": r["active"]["faults"]}


def load_profile(name: str, profile: dict, portfolio: Path = PORTFOLIO, db: Path = DB,
                 as_of=None) -> tuple[dict, list, dict]:
    """A recorded decision as a book on as_of's universe -> (book, messages,
    evaluate report). Loaded non-strict: an active-rule fault (a lost group
    leaving the pp off net zero) is a WARN naming the decision, and tilt floors
    and renormalises."""
    spec = {"groups": profile.get("groups", {}),
            "tactical": profile.get("tactical") or {"on": False, "groups": []}}
    book = load_book(name, portfolio, db, spec=spec, as_of=as_of, strict=False)
    r = book["compute"]
    did = profile["id"]
    msgs = [f"WARN  decision {did}: {m[6:].removeprefix('book: ')}" for m in r["messages"]
            if m.startswith("WARN")]
    for f in book["faults"]:
        msgs.append(f"WARN  decision {did}: {f}; weights floored and renormalised")
    return book, msgs, r["reconcile"]


def targets_at(book: dict, fcap: pd.Series, when: str = "") -> tuple[np.ndarray, list, list]:
    """Target weights over book["names"] on one session's float cap, the live
    groups dropped for having no priced name, and the groups held at 0%."""
    r = book["compute"]
    full, members = r["full"], r["members"]
    alive = fcap.notna() & (fcap > 0)
    ok = lambda t: t in alive.index and bool(alive[t])
    sector_fcap = {g: float(sum(fcap[t] for t in full[g] if ok(t))) for g in book["sectors"]}
    total = sum(sector_fcap.values())
    if total <= 0:
        raise BookError(f"{when}: no float cap in any baseline group")
    claim = {t: g for t, g in book["claim"].items() if ok(t)}
    if claim:
        bweight, _ = target.migrate(sector_fcap, total, claim, r["home_of"], fcap)
    else:
        bweight = {g: v / total for g, v in sector_fcap.items()}
    mem = {g: [t for t in members[g] if ok(t)] for g in book["live"]}
    live = [g for g in book["live"] if mem[g]]
    gone = [g for g in book["live"] if not mem[g]]
    scope = sum(bweight.get(g, 0.0) for g in live)
    if scope <= 0:
        raise BookError(f"{when}: no live group has a priced name")
    neutral = {g: bweight.get(g, 0.0) / scope for g in live}
    try:
        w_raw, _, floored = target.tilt(neutral, {g: book["active"][g] for g in live},
                                        {g: book["rating"][g] for g in live},
                                        r["constraints"]["active"]["budget_pp"])
    except BookError as e:
        raise BookError(f"{when}: {e}")
    held = [g for g in live if w_raw[g] > EPS]
    if r["any_on"]:
        try:
            stock = target.apply_constraints({g: w_raw[g] for g in held}, mem, fcap,
                                             r["constraints"])["stock"]
        except BookError as e:
            raise BookError(f"{when}: {e}")
    else:
        stock = {t: w_raw[g] * fcap[t] / sum(fcap[u] for u in mem[g])
                 for g in held for t in mem[g]}
    return np.array([stock.get(t, 0.0) for t in book["names"]]), gone, floored


def breach_check(book: dict, groups: list, tol: float | None = BREACH_TOL,
                 names: list | None = None):
    """-> fn(w) naming the first cap the drifted weights break by more than
    tol of its limit, or None when no cap is on or tol is None. w runs over
    names (default book["names"]); groups[k] is names[k]'s group, "" for a
    name outside the book (no sector cap)."""
    cons = book["compute"]["constraints"]
    sec, stk, lg = cons["sector"], cons["stock"], cons["large"]
    if tol is None or not (sec["on"] or stk["on"] or lg["on"]):
        return None
    k = 1 + tol
    labels = sorted(set(groups))
    gi = np.array([labels.index(g) for g in groups], dtype=int)
    names = names or book["names"]
    caps = np.array([sec["per_group"].get(g, sec["max"]) if g else np.inf
                     for g in labels]) if sec["on"] else None

    def fn(w):
        if stk["on"] and w.max() > k * stk["max"]:
            return f"{names[int(w.argmax())]} {w.max():.2%} > stock max {stk['max']:.2%}"
        if lg["on"]:
            big = w[w > k * lg["threshold"]].sum()
            if big > k * lg["aggregate"]:
                return f"large holdings {big:.2%} > aggregate {lg['aggregate']:.2%}"
        if sec["on"]:
            gw = np.bincount(gi, w, len(labels))
            over = gw > k * caps
            if over.any():
                j = int(np.argmax(np.where(over, gw - caps, -np.inf)))
                return f"{labels[j]} {gw[j]:.2%} > sector cap {caps[j]:.2%}"
        return None
    return fn


def edge_fill(w: np.ndarray, names: list, group_of: dict, alive: np.ndarray,
              cons: dict) -> np.ndarray | None:
    """Clip every cap the held weights break and spill the excess within the
    cap's own scope (D18, D51) -> weights, or None when infeasible. Reuses
    target.apply_constraints on the HELD weights: group weights = held group
    sums, the pro-rata key = held stock weights, members = names alive on the
    session. A group with no live member drops and the rest renormalise."""
    wg, members = {}, {}
    for t, x, a in zip(names, w, alive):
        g = group_of.get(t)
        if g is None:                   # not in this book: sold, its weight renormalised
            continue
        wg[g] = wg.get(g, 0.0) + float(x)
        if a:
            members.setdefault(g, []).append(t)
    wg = {g: v for g, v in wg.items() if g in members and v > EPS}
    total = sum(wg.values())
    if total <= EPS:
        return None
    try:
        stock = target.apply_constraints({g: v / total for g, v in wg.items()}, members,
                                         pd.Series(w, index=names), cons)["stock"]
    except BookError:
        return None
    out = np.array([stock.get(t, 0.0) for t in names])
    return out if abs(out.sum() - 1) < 1e-9 else None


# --------------------------------------------------------------- simulation

def period_keys(sessions: pd.DatetimeIndex, start: int, freq: str | None):
    if freq is None:
        return None
    if freq == "1M":
        return [d.year * 12 + d.month for d in sessions]
    if freq == "1Q":
        return [d.year * 4 + (d.month - 1) // 3 for d in sessions]
    if freq == "2W":
        monday = sessions[start] - pd.Timedelta(days=sessions[start].weekday())
        return [(d - monday).days // 14 for d in sessions]
    raise BookError(f"unknown rebalance frequency {freq!r}")


def simulate(R: np.ndarray, sessions, start: int, freq, threshold, cfg: dict,
             regimes: list, alive: np.ndarray | None = None, names: list | None = None,
             R_post: np.ndarray | None = None) -> tuple:
    """NAV per session from start, fill events, final weights, standing target,
    messages, index of the last regime applied.

    R[i] is each name's return from the close of session i-1 to the open of
    session i, R_post[i] from that open to the close (zeros when None, so R is
    close to close and a fill at lag >= 1 trades at the previous close's
    prices). regimes, sorted by "at" with regimes[0]["at"] == start, are the
    books in force: {"at": session index, "id", "target": fn(i) -> (weights,
    gone) deriving the standing target (called on inception, decision and
    calendar sessions only), "groups": each name's group, "" outside the book,
    "breach": fn(w) -> reason or None, or None (breach_check), "edge": fn(w,
    alive_i) -> edge-fill weights or None (edge_fill)}. alive[i] marks names
    with a float cap on session i (all alive when None).
    """
    N, n = R.shape
    R_post = np.zeros_like(R) if R_post is None else R_post
    lag = cfg["lag_sessions"]
    keys = period_keys(sessions, start, freq)
    names = names or [str(k) for k in range(n)]
    for reg in regimes:
        reg["labels"] = sorted(set(reg["groups"]))
        reg["gi"] = np.array([reg["labels"].index(g) for g in reg["groups"]], dtype=int)

    def drift(w, t):
        return 0.5 * float(np.abs(np.bincount(reg["gi"], w - t, len(reg["labels"]))).sum())

    w, nav, invested, pending = np.zeros(n), 1.0, False, None
    standing, standing_at, stuck, k = None, None, False, -1
    reg = regimes[0]
    navs, events, messages = [], [], []

    def fill(i):
        nonlocal w, nav, invested, pending
        t = pending["t"]
        if invested:
            turn = 0.5 * float(np.abs(t - w).sum())
            cost = turn * (2 * cfg["brokerage_bps"] + cfg["sell_tax_bps"]) / 1e4
        else:
            turn = float(t.sum())
            cost = turn * cfg["brokerage_bps"] / 1e4
        nav *= 1 - cost
        w, invested = t.copy(), True
        events.append({"decision": sessions[pending["decision"]].date(),
                       "fill": sessions[i].date(), "trigger": pending["trigger"],
                       "also": pending["also"], "profile": pending["profile"],
                       "deferred_from": pending["deferred_from"],
                       "policy": pending["policy"], "target_as_of": pending["as_of"],
                       "group_drift": pending["drift"], "turnover": turn, "cost": cost,
                       "holdings": int((t > EPS).sum()), "dropped": pending["dropped"]})
        pending = None

    def leg(r):
        nonlocal w, nav
        pr = float(w @ r)
        nav *= 1 + pr
        w = w * (1 + r) / (1 + pr)

    for i in range(start, N):
        if i > start:
            leg(R[i])                                  # close -> open, held weights
        if pending is not None and pending["fill"] == i:
            fill(i)                                    # at the open (lag >= 1)
        if i > start:
            leg(R_post[i])                             # open -> close, new weights
        if pending is None and i + lag < N:
            trigger = also = deferred = None
            cal = keys is not None and i > start and keys[i] != keys[i - 1]
            due = k + 1 < len(regimes) and regimes[k + 1]["at"] <= i
            if i == start or due:
                while k + 1 < len(regimes) and regimes[k + 1]["at"] <= i:
                    k += 1                             # the latest due book wins
                reg = regimes[k]
                trigger = "inception" if i == start else "decision"
                also = "calendar" if cal else None
                if reg["at"] < i:
                    deferred = sessions[reg["at"]].date()
            elif cal:
                trigger = "calendar"
            elif reg["breach"] is not None and not stuck and reg["breach"](w):
                trigger = "breach"
            elif threshold is not None and drift(w, standing) > threshold:
                trigger = "drift"
            if trigger:
                gone = []
                if trigger in ("inception", "decision", "calendar"):
                    standing, gone = reg["target"](i)
                    standing_at, stuck = i, False
                live = np.ones(n, bool) if alive is None else alive[i]
                dead = ((w > EPS) | (standing > EPS)) & ~live
                if dead.any():
                    standing = np.where(live, standing, 0.0)
                    if standing.sum() <= EPS:
                        raise BookError(f"{sessions[i].date()}: no name of the standing "
                                        "target has a float cap")
                    standing = standing / standing.sum()
                dr = drift(w, standing) if invested else None
                t, policy = standing, "full"
                if trigger == "breach":
                    t = reg["edge"](w, live) if reg["edge"] is not None else None
                    if t is None:
                        t, stuck = standing, True
                        messages.append(f"WARN  {sessions[i].date()}: the breach cannot be "
                                        "clipped to the cap (names without a float cap shrank "
                                        "capacity); traded to the full standing target, "
                                        "breach checks off until the next calendar date")
                    else:
                        policy = "edge"
                dropped = list(gone) + [names[j] for j in np.flatnonzero(dead)
                                        if reg["groups"][j] not in gone]
                pending = {"fill": i + lag, "decision": i, "t": t, "dropped": dropped,
                           "trigger": trigger, "also": also, "profile": reg["id"],
                           "deferred_from": deferred, "drift": dr, "policy": policy,
                           "as_of": sessions[standing_at].date()}
            if pending is not None and lag == 0:
                fill(i)
        navs.append(nav)
    return navs, events, w, standing, messages, k


def statistics(nav: np.ndarray, bench: np.ndarray, events: list, rf: float) -> dict:
    n = len(nav)
    years = (n - 1) / YEAR

    def one(lv):
        r = lv[1:] / lv[:-1] - 1
        tr = lv[-1] / lv[0] - 1
        ann = (1 + tr) ** (1 / years) - 1
        vol = float(r.std()) * YEAR ** 0.5
        dd = float((lv / np.maximum.accumulate(lv) - 1).min())
        return {"total": tr, "annualised": ann, "vol": vol,
                "sharpe": (ann - rf) / vol if vol > 0 else None, "max_drawdown": dd}, r

    p, rp = one(nav)
    b, rb = one(bench)
    te = float((rp - rb).std()) * YEAR ** 0.5
    vb = float(rb.var())
    later = [e for e in events if e["trigger"] != "inception"]
    return {"portfolio": p, "benchmark": b,
            "excess": p["total"] - b["total"], "tracking_error": te,
            "information_ratio": (p["annualised"] - b["annualised"]) / te if te > 0 else None,
            "beta": float(((rp - rp.mean()) * (rb - rb.mean())).mean()) / vb if vb > 0 else None,
            "turnover": sum(e["turnover"] for e in later),
            "cost": sum(e["cost"] for e in events),
            "n_calendar": sum(e["trigger"] == "calendar" for e in events),
            "n_breach": sum(e["trigger"] == "breach" for e in events),
            "n_drift": sum(e["trigger"] == "drift" for e in events),
            "n_decision": sum(e["trigger"] == "decision" for e in events),
            "sessions": n, "years": years}


# --------------------------------------------------------------- run / build

def place(timeline: list, S: pd.DatetimeIndex, i0: int) -> tuple[list, list, dict]:
    """Decisions onto sessions -> ([(index, profile)], messages, status by id).
    Effective dates snap forward; everything at or before the start collapses
    to the start, the latest winning; the same session: the last wins; after
    the history: reported, not applied. If none lands on the start, the first
    one opens the window."""
    msgs, status, at = [], {}, {}
    for d in sorted(timeline, key=lambda d: (str(d["effective"]), d["id"])):
        j = int(S.searchsorted(pd.Timestamp(d["effective"])))
        if j >= len(S):
            status[d["id"]] = "after the history"
            msgs.append(f"INFO  decision {d['id']} effective {d['effective']} is after the "
                        f"last session {S[-1].date()}; not applied")
            continue
        j = max(j, i0)
        if j in at:
            status[at[j]["id"]] = f"superseded by {d['id']}"
        at[j] = d
    if not at:
        raise BookError("no decision lands in the window\n      run without replaying "
                        "decisions (mechanical)")
    first = min(at)
    if first > i0:
        d = at.pop(first)
        at[i0] = d
        msgs.append(f"INFO  no decision is in force at the start {S[i0].date()}; decision "
                    f"{d['id']} (effective {d['effective']}) opens the window")
    return sorted(at.items(), key=lambda x: x[0]), msgs, status


def run(name: str, start: str | None = None, benchmark: str = "VNINDEX",
        config: dict | None = None, portfolio: Path = PORTFOLIO,
        db: Path = DB, market: dict | None = None,
        timeline: list | None = None) -> dict:
    """Backtest without writing or printing. See module docstring."""
    mk = market or load_market(db)
    S = mk["sessions"]
    as_of = S[-1].date()
    live = load_book(name, portfolio, db, as_of=as_of, strict=timeline is None)
    cfg = (load_backtest_config(portfolio / name) if config is None
           else validate_backtest_config(config))
    messages = []

    if benchmark not in mk["bench"].columns:
        raise BookError(f"benchmark {benchmark!r} is not in index_prices; available "
                        f"{sorted(mk['bench'].columns)}")
    if start is None:
        i0 = 0
    else:
        try:
            want = pd.Timestamp(start)
        except ValueError:
            raise BookError(f"start {start!r} is not a date (YYYY-MM-DD)")
        i0 = int(S.searchsorted(want))
        if i0 == 0 and want < S[0]:
            messages.append(f"INFO  start {want.date()} is before the history; "
                            f"starts at {S[0].date()}")
    lag = cfg["lag_sessions"]
    if len(S) - i0 < lag + 2:
        last = S[max(0, len(S) - lag - 2)].date()
        raise BookError(f"start {start} leaves {max(0, len(S) - i0)} session(s); a fill "
                        f"lag of {lag} needs {lag + 2}\n      start on or before {last}")
    window = S[i0:]
    bench = mk["bench"][benchmark].loc[window]
    if bench.isna().any():
        raise BookError(f"{benchmark} has no close on {bench[bench.isna()].index[0].date()}"
                        "\n      extend the index export and rebuild the database")

    # the books in force: the live book, or the recorded decisions (D53)
    if timeline is None:
        placed, status, reports = [(i0, {"id": "live"})], {}, {}
        books = {"live": live}
    else:
        placed, msgs, status = place(timeline, S, i0)
        messages += msgs
        books, reports = {}, {}
        for _, d in placed:
            books[d["id"]], msgs, reports[d["id"]] = load_profile(name, d, portfolio, db,
                                                                  as_of)
            messages += msgs
    names = sorted(set().union(*(b["names"] for b in books.values())))
    missing = sorted(set(names) - set(mk["adj"].columns))
    if missing:
        raise BookError(f"in the book but not in {Path(db).name}: {missing}")
    pos = {t: j for j, t in enumerate(names)}
    adj = mk["adj"][names].ffill()
    raw_open = mk["open"].reindex(columns=names)
    bad_open = ~(raw_open > 0) & mk["adj"][names].notna()
    opn = raw_open.where(raw_open > 0, adj)
    R_pre = (opn / adj.shift(1) - 1).fillna(0.0).to_numpy()
    R_post = (adj / opn - 1).fillna(0.0).to_numpy()
    fc = mk["fcap"].reindex(columns=names)
    alive = (fc.notna() & (fc > 0)).to_numpy()
    freq = live["rebalance"]["frequency"]
    threshold = live["rebalance"]["drift_threshold"]
    tol = live["rebalance"].get("breach_tolerance", BREACH_TOL)
    cons = live["compute"]["constraints"]
    floored = set()

    def regime(at, pid, book):
        idx = [pos[t] for t in book["names"]]
        groups = [book["group_of"].get(t, "") for t in names]

        def derive(i):
            w_b, gone, fl = targets_at(book, mk["fcap"].iloc[i], str(S[i].date()))
            floored.update(fl)
            out = np.zeros(len(names))
            out[idx] = w_b
            return out, gone
        return {"at": at, "id": pid, "book": book, "target": derive, "groups": groups,
                "breach": breach_check(book, groups, tol, names),
                "edge": lambda w, live_i: edge_fill(w, names, book["group_of"], live_i, cons)}

    regimes = [regime(at, d["id"], books[d["id"]]) for at, d in placed]
    navs, events, w, tgt, notes, k = simulate(R_pre, S, i0, freq, threshold, cfg, regimes,
                                              alive, names, R_post)
    messages += notes
    fills = pd.DatetimeIndex([pd.Timestamp(e["fill"]) for e in events])
    hit = bad_open.loc[bad_open.index.isin(fills)]
    hit = sorted(hit.columns[hit.any()])
    if cfg["lag_sessions"] and hit:
        messages.append(f"INFO  no valid open_adj on a fill session for {hit}; their close "
                        "is used for both legs of that session")
    if floored:
        messages.append(f"INFO  held at 0% on some sessions, the neutral smaller than "
                        f"the underweight: {sorted(floored)}")
    nav = np.array(navs)
    bnav = bench.to_numpy() / bench.iloc[0]
    lo, hi = live["holdings"]["min"], live["holdings"]["max"]
    for e in events:
        e["in_range"] = lo <= e["holdings"] <= hi

    applied = {e["profile"]: e["decision"] for e in events
               if e["trigger"] in ("inception", "decision")}
    tl = []
    if timeline is not None:
        today = setup_hash(portfolio / name)
        for d in sorted(timeline, key=lambda d: (str(d["effective"]), d["id"])):
            at = next((a for a, p in placed if p["id"] == d["id"]), None)
            tl.append({"id": d["id"], "effective": str(d["effective"]),
                       "kind": d.get("kind"), "note": d.get("note", ""),
                       "setup_hash": d.get("setup_hash"),
                       "setup_differs": bool(d.get("setup_hash")) and d["setup_hash"] != today,
                       "placed": None if at is None else S[at].date(),
                       "applied": applied.get(d["id"]),
                       "status": status.get(d["id"]) or ("applied" if d["id"] in applied
                                                         else "not reached"),
                       "report": reports.get(d["id"])})

    equity = pd.DataFrame({"date": window.date, "portfolio": nav, "benchmark": bnav})
    reb = pd.DataFrame(events, columns=["decision", "fill", "trigger", "also", "profile",
                                        "deferred_from", "policy", "target_as_of",
                                        "group_drift", "turnover", "cost", "holdings",
                                        "in_range", "dropped"])
    reb["dropped"] = reb["dropped"].map(lambda g: "; ".join(g) if g else "")
    for c in ("also", "deferred_from"):
        reb[c] = reb[c].map(lambda v: "" if v is None or pd.isna(v) else str(v))
    last = regimes[k]
    end = pd.DataFrame({"ticker": names, "group": last["groups"], "weight": w, "target": tgt})
    end = end[(end["weight"] > EPS) | (end["target"] > EPS)]
    end = end.sort_values("weight", ascending=False).reset_index(drop=True)
    gi = np.array([last["labels"].index(g) for g in last["groups"]], dtype=int)
    now = {"drift": 0.5 * float(np.abs(np.bincount(gi, w - tgt, len(last["labels"]))).sum()),
           "threshold": threshold, "tolerance": tol,
           "breach": last["breach"](w) if last["breach"] is not None else None,
           "profile": last["id"]}
    comps = {}
    for code in sorted(mk["bench"].columns):
        col = mk["bench"][code].loc[window]
        if not col.isna().any():
            b = col.to_numpy() / col.iloc[0]
            comps[code] = {"equity": b, "stats": statistics(nav, b, events, cfg["risk_free_rate"])}
    return {"name": name, "as_of": live["as_of"], "benchmark": benchmark, "now": now,
            "start": window[0].date(), "end": window[-1].date(), "config": cfg,
            "rebalance": live["rebalance"], "holdings_range": live["holdings"],
            "constraints": live["compute"]["cap_note"], "tactical": live["compute"]["tac_note"],
            "equity": equity, "rebalances": reb, "holdings_end": end,
            "stats": comps[benchmark]["stats"], "benchmarks": comps,
            "timeline": tl if timeline is not None else None, "messages": messages}


def next_calendar(freq: str | None, start: date, last: date) -> date | None:
    """First weekday of the calendar period after `last` (period_keys' periods;
    2W counts from the Monday of `start`'s week). Holidays are not known, so
    the fill lands on the first session on or after it. None without a frequency."""
    if freq is None:
        return None
    if freq == "1M":
        d = date(last.year + last.month // 12, last.month % 12 + 1, 1)
    elif freq == "1Q":
        q = (last.month - 1) // 3 + 1
        d = date(last.year + q // 4, (q % 4) * 3 + 1, 1)
    else:
        monday = start - timedelta(days=start.weekday())
        d = monday + timedelta(days=14 * ((last - monday).days // 14 + 1))
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def monitor(name: str, portfolio: Path = PORTFOLIO, db: Path = DB,
            market: dict | None = None) -> dict:
    """The desk's Monitor (D63): the last decision held to the last session.
    See module docstring. Writes nothing."""
    home = portfolio / name
    profiles = load_decisions(home)
    screens = load_screens(home)
    out = {"decision": None, "latest": None, "status": "", "now": None, "rebalances": [],
           "holdings": [], "next_calendar": None, "messages": [], "flags": [],
           "screens_on": [k for k, v in screens.items() if v["on"]]}
    if not profiles:
        out["status"] = ("no decision recorded yet; record the inception decision on the "
                         "Target step first")
        return out
    last = profiles[-1]
    mk = market or load_market(db)
    S = mk["sessions"]
    latest = S[-1].date()
    frame = params_mod.at(db, latest)
    out.update(decision={k: last.get(k) for k in ("id", "effective", "kind", "priced_as_of",
                                                  "note")},
               latest=str(latest))

    def flag_names(names):
        out["flags"] = pf.screen_flags(frame, names, screens)

    recorded = [h["t"] for h in last.get("holdings") or []] or \
        [t for s in last["groups"].values() for t in s.get("investable", [])]
    j = int(S.searchsorted(pd.Timestamp(last["effective"])))
    lag = load_backtest_config(home)["lag_sessions"]
    if j >= len(S):
        out["status"] = (f"decision {last['id']} is effective after the last session "
                         f"{latest}; not in force yet")
        flag_names(recorded)
        return out
    if len(S) - j < lag + 2:
        out["status"] = (f"decision {last['id']} is too recent to monitor: "
                         f"{len(S) - j} session(s) from {S[j].date()} to {latest}, a lag of "
                         f"{lag} needs {lag + 2}")
        flag_names(recorded)
        return out
    bench = next((c for c in ("VNINDEX", *sorted(mk["bench"].columns))
                  if c in mk["bench"].columns and not mk["bench"][c].loc[S[j:]].isna().any()),
                 None)
    if bench is None:
        raise BookError(f"no benchmark has a close on every session since {S[j].date()}")
    res = run(name, str(S[j].date()), bench, portfolio=portfolio, db=db, market=mk,
              timeline=[last])
    end = res["holdings_end"]
    freq = res["rebalance"]["frequency"]
    out.update(
        status=f"decision {last['id']} held from {res['start']} to {res['end']}",
        now=res["now"], messages=res["messages"],
        next_calendar=None if (d := next_calendar(freq, res["start"], latest)) is None
        else str(d),
        frequency=freq,
        total=res["stats"]["portfolio"]["total"],
        rebalances=[{"decision": str(r.decision), "fill": str(r.fill), "trigger": r.trigger,
                     "policy": r.policy, "turnover": float(r.turnover)}
                    for r in res["rebalances"].itertuples()],
        holdings=[{"t": r.ticker, "group": r.group, "w": float(r.weight),
                   "target": float(r.target)} for r in end.itertuples()])
    flag_names(end.loc[end["target"] > EPS, "ticker"])
    return out


def summary_row(res: dict) -> dict:
    s, c = res["stats"], res["config"]
    return {"portfolio": res["name"], "benchmark": res["benchmark"],
            "start": res["start"], "end": res["end"], "sessions": s["sessions"],
            **{f"cfg_{k}": v for k, v in c.items()},
            **{f"p_{k}": v for k, v in s["portfolio"].items()},
            **{f"b_{k}": v for k, v in s["benchmark"].items()},
            **{k: s[k] for k in ("excess", "tracking_error", "information_ratio", "beta",
                                 "turnover", "cost", "n_decision", "n_calendar", "n_breach",
                                 "n_drift")}}


def build(name: str, start: str | None = None, benchmark: str = "VNINDEX",
          portfolio: Path = PORTFOLIO, db: Path = DB, mechanical: bool = False) -> dict:
    """run() plus the files and the printed report. Replays the decision log
    when there is one, unless mechanical."""
    timeline = None if mechanical else load_decisions(portfolio / name) or None
    res = run(name, start, benchmark, portfolio=portfolio, db=db, timeline=timeline)
    for line in res["messages"]:
        print(line)
    out = portfolio / name / OUT
    out.mkdir(exist_ok=True)
    kw = {"index": False, "lineterminator": "\n", "float_format": "%.8f"}
    res["equity"].to_csv(out / "equity.csv", **kw)
    res["rebalances"].to_csv(out / "rebalances.csv", **kw)
    res["holdings_end"].to_csv(out / "holdings_end.csv", **kw)
    pd.DataFrame([summary_row(res)]).to_csv(out / "summary.csv", **kw)

    s, c, rb = res["stats"], res["config"], res["rebalance"]
    freq = rb["frequency"] or "none (inception only)"
    drift = f"{rb['drift_threshold']:.2%} group grain" if rb["drift_threshold"] else "off"
    tol = rb.get("breach_tolerance", BREACH_TOL)
    breach = f"{tol:.0%} of the cap" if tol is not None else "off"
    cfg_src = (f"portfolio/{name}/{BT_CONFIG}" if (portfolio / name / BT_CONFIG).exists()
               else "defaults (no backtest_config.json)")
    if res["timeline"] is None:
        why = "--mechanical" if mechanical else "no decision log"
        dec = f"decisions:   none replayed ({why}); {BOOK} throughout\n"
    else:
        dec = "decisions:   replayed from decisions/ (D53, D61), each on the last\n" \
              "             session's universe (D59)\n"
        for d in res["timeline"]:
            r = d["report"] or {}
            drops = "; ".join(filter(None, [
                f"not trading {r['dropped_names']}" if r.get("dropped_names") else "",
                f"groups lost {sorted(r['lost_groups'])}" if r.get("lost_groups") else "",
                "setup differs from today's" if d.get("setup_differs") else ""]))
            dec += (f"             {d['id']} {d.get('kind') or ''} effective {d['effective']}: "
                    f"{d['status']}{' ' + str(d['applied']) if d['applied'] else ''}"
                    f"{'; ' + drops if drops else ''}\n")
    (out / "built_from.txt").write_text(
        f"portfolio:   {name}, books evaluated on {res['as_of']}\n"
        f"window:      {res['start']} -> {res['end']} ({s['sessions']} sessions)\n"
        f"benchmark:   {res['benchmark']}, PRICE index rebased to the start close\n"
        f"mandate:     rebalance {freq}; drift {drift}; breach {breach}; holdings "
        f"{res['holdings_range']['min']}-{res['holdings_range']['max']}\n"
        f"             target held between calendar dates; breach clips to the cap\n"
        f"             (D50, D51)\n"
        f"constraints: {res['constraints']}\n"
        f"tactical:    {res['tactical']}\n"
        f"{dec}"
        f"config:    {cfg_src}: fills at the open {c['lag_sessions']} session(s) after "
        f"the decision (0 = same close), brokerage "
        f"{c['brokerage_bps']:g} bps a side, sell tax {c['sell_tax_bps']:g} bps, "
        f"risk-free {c['risk_free_rate']:.2%}\n"
        f"returns:     total return on close_adj; benchmark is price return, so\n"
        f"             the portfolio leads it by roughly the dividend yield\n"
        f"bias:        today's group map and listed names held backwards\n"
        f"             (survivorship); read excess and spreads before levels\n"
        f"built_at:    {datetime.now().astimezone():%Y-%m-%d %H:%M}\n",
        encoding="utf-8")

    p, b = s["portfolio"], s["benchmark"]
    fmt = lambda v, f: "n/a" if v is None else format(v, f)
    print(f"OK    {name}  {res['start']} -> {res['end']}  {s['sessions']} sessions  "
          f"vs {res['benchmark']}")
    print(f"      mandate rebalance {freq}, drift {drift}, breach {breach}; lag {c['lag_sessions']}, "
          f"{c['brokerage_bps']:g}+{c['sell_tax_bps']:g} bps")
    print(f"      -> {out}\\equity.csv, rebalances.csv, holdings_end.csv, summary.csv")
    print()
    print(f"      {'':<22} {'portfolio':>10} {res['benchmark']:>10}")
    print(f"      {'total return':<22} {p['total']:>10.2%} {b['total']:>10.2%}")
    print(f"      {'annualised':<22} {p['annualised']:>10.2%} {b['annualised']:>10.2%}")
    print(f"      {'volatility':<22} {p['vol']:>10.2%} {b['vol']:>10.2%}")
    print(f"      {'sharpe':<22} {fmt(p['sharpe'], '>10.2f')} {fmt(b['sharpe'], '>10.2f')}")
    print(f"      {'max drawdown':<22} {p['max_drawdown']:>10.2%} {b['max_drawdown']:>10.2%}")
    print(f"      excess {s['excess']:+.2%}  TE {s['tracking_error']:.2%}  "
          f"IR {fmt(s['information_ratio'], '.2f')}  beta {fmt(s['beta'], '.2f')}")
    print(f"      rebalances {s['n_decision']} decision, {s['n_calendar']} calendar, "
          f"{s['n_breach']} breach, "
          f"{s['n_drift']} drift; turnover "
          f"{s['turnover']:.1%}; costs {s['cost'] * 1e4:.1f} bps")
    for e in res["rebalances"].itertuples():
        if not e.in_range:
            print(f"WARN  {e.fill}: {e.holdings} holdings outside "
                  f"{res['holdings_range']['min']}-{res['holdings_range']['max']}")
        if e.dropped:
            print(f"WARN  {e.decision}: no float cap for {e.dropped}; dropped, weight "
                  "renormalised")
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="portfolio folder under portfolio/")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD, default the first session")
    ap.add_argument("--benchmark", default="VNINDEX", help="index code in index_prices")
    ap.add_argument("--mechanical", action="store_true",
                    help="ignore decisions/ and hold the live book throughout")
    a = ap.parse_args(argv)
    try:
        build(a.name, a.start, a.benchmark, mechanical=a.mechanical)
    except BookError as e:
        print(f"FAIL  {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
