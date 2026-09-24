"""Replication (D69, D70): a portfolio established from cash at an AUM on a date.

A view, written nowhere: the order ticket that would build the portfolio's
target in force from cash at one open, in board lots, with the risks the size
brings. Reads data/market.db, the group map and fol.csv (params.at) and the
desk's files: decisions/, statement.json, constraints.json, book.json through
the forward test, screens.json for the flags.

Timing (D67). exec_session is the first session ON OR AFTER the execution date
(the one calculation that snaps forward: it fills at an open). The reference
session is exec_session - 1: the ticket trades that close's decision at the
next open, like every rebalance. An execution date after the last session is
"unattainable" (its open is not in the data) and builds no ticket; an
execution session at or before the inception's session has no book in force
and FAILs.

The reference book is the STANDING TARGET at the reference close in the
forward test (backtest_engine.forward, trace D66): recorded decisions and the
calendar rebalances derived from them, names lacking a float cap already
dropped, exactly as the engine trades it. "source" says whether the session
that derived it was a recorded decision (inception or decision) or a derived
calendar rebalance (D68). A decision recorded but unattainable (D67) is named;
the ticket uses the one before it.

Sizing on the reference session's close_raw, the last price known when the
order goes in (the same close the target is read on):

    equity  = AUM x (1 - cash_pct / 100)
    want_i  = equity x w_i / (LOT x close_i)          lots, fractional
    lots_i  = floor(want_i), then one more lot per name in order of the
              fractional remainder (larger target weight, then ticker A-Z, on
              ties) while it still fits in equity

Nobody is more than one lot above target, the spend never exceeds equity, the
result is deterministic. Cash is a hard floor at the sizing close: the residual
falls to cash, so the actual cash there is at or above cash_pct. The
execution session's open_raw is reported beside the close with the spend and
the cash at the open (it never changes the share counts; the open can take
the cash below the floor, and the ticket says so).

A name of the reference book not in the sizing session's params (not trading
or no float cap) is dropped and the survivors renormalised; a name that
rounds to zero lots stays on the ticket at 0. Both are listed in the messages.
The holdings count after rounding is checked against statement.json min/max
(WARN). Deviation per name in bp of the equity book (value / spend); group
drift = 1/2 sum over groups of |actual - target|, comparable to
statement.json drift_threshold.

Flags: the name screens on (screen_flags) and the position screens
(position_flags, D70) on the shares, both from screens.json, both on the
sizing session's params. Flags only; they never trim a position.

run(name, aum, cash_pct, exec_date) returns the ticket:

    name, aum, cash_pct, exec_date (as asked), unattainable (None or why),
    exec_session, reference (the sizing session), target_as_of, profile,
    source (recorded | derived), trigger, lines [{t, group, target, lots,
    shares, close, value, w, w_aum, dev_bp, open, value_open, and every screen
    measure whether or not it is on: own_pct, float_pct, liq_days (at the
    screen's participation), turnover_pct, float_cap_bn, fol_pct}], totals
    {equity, spend, cash, cash_pct, spend_open, cash_open, cash_open_pct,
    holdings, group_drift}, flags, position_flags, screens, holdings_range,
    decisions_unattainable [{id, effective, why}], messages

Usage
    .venv\\Scripts\\python.exe scr\\replicate.py finance_advance --aum 100e9 --cash 5
    .venv\\Scripts\\python.exe scr\\replicate.py finance_advance --aum 100e9 --cash 5 --exec 2026-09-15
"""
import argparse
import math
import sys
from pathlib import Path

import backtest_engine as be
import duckdb
import pandas as pd
import params as params_mod
from common import (
    DB,
    LOT,
    PORTFOLIO,
    BookError,
    as_date,
    load_decisions,
    load_screens,
    load_statement,
)

import portfolio as pf

EPS = 1e-12


def allocate(weights: dict, prices: dict, equity: float, lot: int = LOT) -> dict:
    """{ticker: lots} for weights (summing to 1) at prices within equity. Pure.
    Floor every name, then one more lot per name by largest fractional
    remainder (ties: larger weight, then ticker A-Z) while it fits."""
    want = {t: equity * w / (lot * prices[t]) for t, w in weights.items()}
    lots = {t: math.floor(v) for t, v in want.items()}
    spend = sum(lots[t] * lot * prices[t] for t in lots)
    for t in sorted(want, key=lambda t: (-(want[t] - lots[t]), -weights[t], t)):
        cost = lot * prices[t]
        if want[t] - lots[t] > EPS and spend + cost <= equity:
            lots[t] += 1
            spend += cost
    return lots


def reference(name: str, exec_date=None, portfolio: Path = PORTFOLIO, db: Path = DB,
              market: dict | None = None) -> dict:
    """The target in force for an execution date: exec_session, the reference
    session before it and the standing target at its close (module docstring).
    {"unattainable": why} alone when the date is after the last session."""
    profiles = load_decisions(portfolio / name)
    if not profiles:
        raise BookError("no decision recorded yet; record the inception decision on the "
                        "Target step first")
    mk = market or be.load_market(db)
    S = mk["sessions"]
    want = as_date(exec_date, "execution date") if exec_date else S[-1].date()
    k = int(S.searchsorted(pd.Timestamp(want), side="left"))       # on or after
    later = [{"id": p["id"], "effective": str(p["effective"]),
              "why": be.unattainable(S, p["effective"])} for p in profiles]
    later = [d for d in later if d["why"]]
    if k == len(S):
        return {"unattainable": f"execution date {want} is after the last session "
                                f"{S[-1].date()}; its open is not in the data",
                "decisions_unattainable": later}
    if len(later) == len(profiles):
        raise BookError(f"no decision is attainable yet: {later[0]['id']} is unattainable, "
                        f"{later[0]['why']}")
    i0 = max(int(S.searchsorted(pd.Timestamp(profiles[0]["effective"]), side="right")) - 1, 0)
    if k - 1 < i0:
        raise BookError(f"no book is in force before the {S[k].date()} open: the inception "
                        f"is priced on {S[i0].date()}\n      execute on or after "
                        f"{S[i0 + 1].date()}")
    res = be.forward(name, profiles, portfolio, db, mk)
    day = res["daily"][k - 1 - i0]
    asof = day["target_as_of"]
    reb = res["rebalances"]
    row = reb[reb["decision"].astype(str) == asof]
    trigger = str(row["trigger"].iloc[0]) if len(row) else None
    return {"unattainable": None, "exec_session": str(S[k].date()),
            "reference": str(S[k - 1].date()), "target_as_of": asof,
            "profile": day["profile"], "trigger": trigger,
            "source": "derived" if trigger == "calendar" else "recorded",
            "weights": {h["t"]: h["target"] for h in day["holdings"] if h["target"] > EPS},
            "groups": {h["t"]: h["group"] for h in day["holdings"]},
            "decisions_unattainable": later}


def open_raw(db: Path, session: str) -> dict:
    """{ticker: open_raw} on one session (names with no valid open left out)."""
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = con.execute("SELECT ticker, open_raw FROM prices WHERE trade_date = ? "
                           "AND open_raw > 0", [session]).fetchall()
    finally:
        con.close()
    return {t: float(v) for t, v in rows}


def run(name: str, aum: float, cash_pct: float, exec_date=None,
        portfolio: Path = PORTFOLIO, db: Path = DB, market: dict | None = None) -> dict:
    """The replication ticket. See module docstring. Writes nothing."""
    if isinstance(aum, bool) or not isinstance(aum, (int, float)) or not math.isfinite(aum) \
            or aum <= 0:
        raise BookError(f"AUM {aum!r} must be a positive number of VND")
    if isinstance(cash_pct, bool) or not isinstance(cash_pct, (int, float)) \
            or not 0 <= cash_pct < 100:
        raise BookError(f"cash {cash_pct!r} must be a percentage in [0, 100)")
    home = portfolio / name
    screens = load_screens(home)
    out = {"name": name, "aum": float(aum), "cash_pct": float(cash_pct),
           "exec_date": None if exec_date is None else str(exec_date), "screens": screens,
           "lines": [], "flags": [], "position_flags": [], "messages": []}
    ref = reference(name, exec_date, portfolio, db, market)
    out.update({k: v for k, v in ref.items() if k not in ("weights", "groups")})
    if ref["unattainable"]:
        return out
    msgs = out["messages"]
    for d in ref["decisions_unattainable"]:
        msgs.append(f"INFO  decision {d['id']} (effective {d['effective']}) is recorded but "
                    f"unattainable: {d['why']}; this ticket uses the target derived "
                    f"{ref['target_as_of']}")

    frame = params_mod.at(db, ref["reference"]).set_index("ticker")
    w = {t: v for t, v in ref["weights"].items() if t in frame.index}
    gone = sorted(set(ref["weights"]) - set(w))
    if not w:
        raise BookError(f"no name of the target derived {ref['target_as_of']} is priced on "
                        f"{ref['reference']}")
    if gone:
        msgs.append(f"INFO  not priced on {ref['reference']}, dropped and the rest "
                    f"renormalised: {gone}")
    total = sum(w.values())
    w = {t: v / total for t, v in w.items()}
    close = {t: float(frame.at[t, "close_raw"]) for t in w}
    equity = aum * (1 - cash_pct / 100)
    lots = allocate(w, close, equity)
    opn = open_raw(db, ref["exec_session"])

    shares = {t: lots[t] * LOT for t in w}
    spend = sum(shares[t] * close[t] for t in w)
    has_open = all(t in opn for t in w if shares[t])
    spend_open = sum(shares[t] * opn[t] for t in w if shares[t]) if has_open else None
    zero = sorted(t for t in w if not lots[t])
    if zero:
        msgs.append(f"INFO  below one lot of {LOT} shares at this AUM, 0 shares: {zero}")
    if not has_open:
        msgs.append(f"INFO  no open on {ref['exec_session']} for "
                    f"{sorted(t for t in w if shares[t] and t not in opn)}; "
                    "the spend at the open is not computed")
    held = sum(1 for t in w if shares[t])
    rng = (load_statement(home) or {}).get("holdings") or {}
    out["holdings_range"] = rng
    if rng and not rng["min"] <= held <= rng["max"]:
        msgs.append(f"WARN  {held} holdings after rounding is outside {rng['min']}-"
                    f"{rng['max']} (statement.json)")

    liq = screens["liquidity"]["participation_pct"] / 100

    def ratio(a, b):
        return None if pd.isna(a) or pd.isna(b) or not b > 0 else float(a) / float(b)

    lines = []
    for t in sorted(w, key=lambda t: (-w[t], t)):
        value = shares[t] * close[t]
        act = value / spend if spend > 0 else 0.0
        p = frame.loc[t]
        days = ratio(value, liq * p["adv_21"])
        lines.append({"t": t, "group": ref["groups"].get(t, ""), "target": w[t],
                      "lots": lots[t], "shares": shares[t], "close": close[t],
                      "value": value, "w": act, "w_aum": value / aum,
                      "dev_bp": (act - w[t]) * 1e4, "open": opn.get(t),
                      "value_open": shares[t] * opn[t] if t in opn else None,
                      "own_pct": None if (r := ratio(shares[t], p["outstanding_shares"])) is None
                      else 100 * r,
                      "float_pct": None if (r := ratio(shares[t], p["free_float"])) is None
                      else 100 * r,
                      "liq_days": days,
                      "turnover_pct": None if pd.isna(p["turnover_21_pct"])
                      else float(p["turnover_21_pct"]),
                      "float_cap_bn": float(p["float_cap"]) / 1e9,
                      "fol_pct": None if pd.isna(p["fol_limit"]) else 100 * float(p["fol_limit"])})
    gsum: dict = {}
    for x in lines:
        a = gsum.setdefault(x["group"], [0.0, 0.0])
        a[0] += x["w"]
        a[1] += x["target"]
    out["lines"] = lines
    out["totals"] = {
        "equity": equity, "spend": spend, "cash": aum - spend, "cash_pct": 100 * (aum - spend) / aum,
        "spend_open": spend_open,
        "cash_open": None if spend_open is None else aum - spend_open,
        "cash_open_pct": None if spend_open is None else 100 * (aum - spend_open) / aum,
        "holdings": held,
        "group_drift": 0.5 * sum(abs(a - b) for a, b in gsum.values())}
    if spend_open is not None and out["totals"]["cash_open_pct"] < cash_pct:
        msgs.append(f"INFO  at the {ref['exec_session']} open the same shares cost "
                    f"{spend_open:,.0f} VND: cash {out['totals']['cash_open_pct']:.2f}% is "
                    f"below the {cash_pct:g}% floor (sized on the {ref['reference']} close)")
    held_t = [t for t in w if shares[t]]
    out["flags"] = pf.screen_flags(frame.reset_index(), held_t, screens)
    out["position_flags"] = pf.position_flags(frame.reset_index(), shares, screens)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--aum", type=float, required=True, help="VND, e.g. 100e9")
    ap.add_argument("--cash", type=float, default=0.0, help="percent kept in cash")
    ap.add_argument("--exec", dest="exec_date", help="YYYY-MM-DD, default the last session")
    a = ap.parse_args(argv)
    try:
        r = run(a.name, a.aum, a.cash, a.exec_date)
    except BookError as e:
        print(f"FAIL  {e}")
        return 1
    if r["unattainable"]:
        print(f"FAIL  unattainable: {r['unattainable']}")
        return 1
    tot = r["totals"]
    print(f"OK    {a.name}: {r['aum']:,.0f} VND, cash {r['cash_pct']:g}%, fill at the "
          f"{r['exec_session']} open, sized on the {r['reference']} close")
    print(f"      target derived {r['target_as_of']} ({r['source']}, {r['trigger']}, "
          f"decision {r['profile']})")
    print(f"      {'ticker':<7}{'target':>8}{'shares':>14}{'close':>10}{'value bn':>11}"
          f"{'weight':>8}{'dev bp':>8}")
    for x in r["lines"]:
        print(f"      {x['t']:<7}{x['target']:>8.2%}{x['shares']:>14,}{x['close']:>10,.0f}"
              f"{x['value'] / 1e9:>11.3f}{x['w']:>8.2%}{x['dev_bp']:>8.1f}")
    print(f"      spend {tot['spend']:,.0f}  cash {tot['cash_pct']:.3f}%  holdings "
          f"{tot['holdings']}  group drift {tot['group_drift'] * 100:.3f} pp")
    if tot["spend_open"] is not None:
        print(f"      at the open: spend {tot['spend_open']:,.0f}  cash "
              f"{tot['cash_open_pct']:.3f}%")
    for f in r["flags"] + r["position_flags"]:
        print(f"FLAG  {f['t']:<6} {pf.flag_text(f)}")
    for m in r["messages"]:
        print(m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
