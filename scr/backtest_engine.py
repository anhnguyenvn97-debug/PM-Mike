"""Backtest engine: a portfolio replayed under its own mandate against a benchmark.

Replaces nothing: scr/backtest.py (legacy) keeps running on local_history.db.
This engine reads data/market.db and the desk's files only:

    statement.json          rebalance mandate + holdings range (required)
    book, tactical overlay, constraints.json, screen/invalid.csv
                            through target.compute, exactly as the target
    backtest_config.json    trading assumptions (common.py; defaults if absent)

The book is held as of the portfolio's anchor. Every past date uses today's
group map, ratings, deleted and invalidated names, so survivorship and
look-ahead bias flatter every level; the output says so.

Target weights on session d -- target.py's math on d's float cap:

    fcap_i = free_float_i x close_raw_i       on d, never close_adj or market_cap
    b_g    = sum of fcap over the FULL baseline column / total, claims migrated
    w_g    = b_g x m_g / sum(b x m)           over live groups with a priced name
    w_i    = split by fcap, or target.apply_constraints when any constraint is on

On the anchor these are target.compute's weights. A live group with no priced
name on d drops out and the rest renormalise (reported per rebalance). A
constraint that cannot be solved on a past date FAILs; the book never holds cash.

Mandate (statement.json rebalance):

    frequency        2W | 1M | 1Q | null. A scheduled rebalance on the first
                     session of each period re-derives the target on that
                     session. 1M / 1Q are calendar periods; 2W counts 14-day
                     periods from the Monday of the start session's week.
                     null: inception only.
    drift_threshold  fraction | null. At each close, drift = 1/2 sum over
                     groups of |held - target|; above the threshold a drift
                     rebalance restores the CURRENT target (no re-derivation).

The start session always decides inception. A decision observes session t's
close and fills at the close of t + lag_sessions; no new decision while a
fill is pending. The portfolio is cash until the first fill.

Returns: total return on close_adj (dividends reinvested on the ex-date, D24);
a name with no print carries its last price. Between fills weights drift with
returns. Costs on a fill: inception pays brokerage on the buys only; later
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

run(name, start, benchmark, config=) computes without writing (the desk uses
it; config stands in for backtest_config.json). Its "benchmarks" holds the
rebased curve and statistics for EVERY code with a close on each session of
the window, so a page can switch benchmark without re-running; the requested
benchmark must be one of them. build() adds the files,
overwritten in portfolio/<name>/backtest_engine/:

    equity.csv         date, portfolio, benchmark (both 1.0 at the start close)
    rebalances.csv     decision, fill, trigger, group drift, turnover, cost,
                       holdings, in_range, groups with no priced name
    holdings_end.csv   ticker, group, drifted weight at the end, last target
    summary.csv        one row: window, benchmark, config, statistics
    built_from.txt     provenance, mandate, caveats

Usage
    .venv\\Scripts\\python.exe scr\\backtest_engine.py hsc_strat_high_growth
    .venv\\Scripts\\python.exe scr\\backtest_engine.py financial_test --start 2026-04-01 --benchmark VN30
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import target
from common import (
    BT_CONFIG,
    DB,
    PARAMS,
    PORTFOLIO,
    STATEMENT,
    BookError,
    load_backtest_config,
    validate_backtest_config,
)

YEAR = 252
OUT = "backtest_engine"
EPS = 1e-12


# --------------------------------------------------------------- inputs

def load_market(db: Path) -> dict:
    """Sessions, close_adj and float cap by ticker, benchmark closes by code."""
    if not Path(db).exists():
        raise BookError(f"missing {db}\n      run scr/ingest.py (Rebuild database)")
    con = duckdb.connect(str(db), read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if "index_prices" not in tables:
            raise BookError(f"{Path(db).name} has no benchmarks\n      add an index "
                            "export to data/fiinpro/ and rebuild the database")
        px = con.execute("SELECT trade_date, ticker, close_adj, "
                         "free_float * close_raw AS fcap FROM prices").df()
        ix = con.execute("SELECT trade_date, code, close FROM index_prices").df()
    finally:
        con.close()
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    ix["trade_date"] = pd.to_datetime(ix["trade_date"])
    adj = px.pivot(index="trade_date", columns="ticker", values="close_adj").sort_index()
    return {"sessions": adj.index, "adj": adj,
            "fcap": px.pivot(index="trade_date", columns="ticker", values="fcap").reindex(adj.index),
            "bench": ix.pivot(index="trade_date", columns="code", values="close").reindex(adj.index)}


def load_book(name: str, portfolio: Path = PORTFOLIO, params_dir: Path = PARAMS) -> dict:
    """The portfolio as target.compute sees it, plus the investable names."""
    r = target.compute(name, portfolio, params_dir)
    if r["statement"] is None:
        raise BookError(f"portfolio/{name}/{STATEMENT} is missing; the backtest "
                        "follows its rebalance mandate")
    alloc = r["allocation"]
    mult = dict(zip(alloc["group"], alloc["multiplier"]))
    sectors = [g for g, k in zip(alloc["group"], alloc["kind"]) if k == "sector"]
    tac = list(r["tac_groups"])
    live = [g for g in sectors + tac if mult[g] > 0]
    group_of = {t: g for g in live for t in r["members"][g]}
    return {"name": name, "anchor": r["anchor"], "compute": r, "mult": mult,
            "sectors": sectors, "tac": tac, "live": live,
            "claim": {t: g for g in tac for t in r["members"][g]},
            "names": sorted(group_of), "group_of": group_of,
            "rebalance": r["statement"]["rebalance"],
            "holdings": r["statement"]["holdings"]}


def targets_at(book: dict, fcap: pd.Series, when: str = "") -> tuple[np.ndarray, list]:
    """Target weights over book["names"] on one session's float cap, and the
    live groups dropped for having no priced name."""
    r = book["compute"]
    full, members, mult = r["full"], r["members"], book["mult"]
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
    denom = sum(bweight.get(g, 0.0) * mult[g] for g in live)
    if denom <= 0:
        raise BookError(f"{when}: no live group has a priced name")
    w_raw = {g: bweight.get(g, 0.0) * mult[g] / denom for g in live}
    if r["any_on"]:
        try:
            stock = target.apply_constraints(w_raw, mem, fcap, r["constraints"])["stock"]
        except BookError as e:
            raise BookError(f"{when}: {e}")
    else:
        stock = {t: w_raw[g] * fcap[t] / sum(fcap[u] for u in mem[g])
                 for g in live for t in mem[g]}
    return np.array([stock.get(t, 0.0) for t in book["names"]]), gone


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
             target_fn, groups: list) -> tuple[list, list, np.ndarray, np.ndarray]:
    """NAV per session from start, fill events, final weights, last target.

    R[i] is each name's return into session i; target_fn(i) -> (weights, gone).
    """
    N, n = R.shape
    lag = cfg["lag_sessions"]
    keys = period_keys(sessions, start, freq)
    labels = sorted(set(groups))
    gi = np.array([labels.index(g) for g in groups], dtype=int)

    def drift(w, t):
        return 0.5 * float(np.abs(np.bincount(gi, w - t, len(labels))).sum())

    w, nav, invested, tgt, pending = np.zeros(n), 1.0, False, np.zeros(n), None
    navs, events = [], []

    def fill(i):
        nonlocal w, nav, invested, tgt, pending
        t = pending["t"]
        if invested:
            turn = 0.5 * float(np.abs(t - w).sum())
            cost = turn * (2 * cfg["brokerage_bps"] + cfg["sell_tax_bps"]) / 1e4
        else:
            turn = float(t.sum())
            cost = turn * cfg["brokerage_bps"] / 1e4
        nav *= 1 - cost
        w, tgt, invested = t.copy(), t, True
        events.append({"decision": sessions[pending["decision"]].date(),
                       "fill": sessions[i].date(), "trigger": pending["trigger"],
                       "group_drift": pending["drift"], "turnover": turn, "cost": cost,
                       "holdings": int((t > EPS).sum()), "gone": pending["gone"]})
        pending = None

    for i in range(start, N):
        if i > start:
            pr = float(w @ R[i])
            nav *= 1 + pr
            w = w * (1 + R[i]) / (1 + pr)
        if pending is not None and pending["fill"] == i:
            fill(i)
        if pending is None and i + lag < N:
            if i == start or (keys is not None and keys[i] != keys[i - 1]):
                t, gone = target_fn(i)
                pending = {"fill": i + lag, "decision": i, "t": t, "gone": gone,
                           "trigger": "calendar" if invested else "inception",
                           "drift": drift(w, tgt) if invested else None}
            elif threshold is not None and invested:
                dr = drift(w, tgt)
                if dr > threshold:
                    pending = {"fill": i + lag, "decision": i, "t": tgt, "gone": [],
                               "trigger": "drift", "drift": dr}
            if pending is not None and lag == 0:
                fill(i)
        navs.append(nav)
    return navs, events, w, tgt


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
            "n_drift": sum(e["trigger"] == "drift" for e in events),
            "sessions": n, "years": years}


# --------------------------------------------------------------- run / build

def run(name: str, start: str | None = None, benchmark: str = "VNINDEX",
        config: dict | None = None, portfolio: Path = PORTFOLIO,
        params_dir: Path = PARAMS, db: Path = DB, market: dict | None = None) -> dict:
    """Backtest without writing or printing. See module docstring."""
    book = load_book(name, portfolio, params_dir)
    cfg = (load_backtest_config(portfolio / name) if config is None
           else validate_backtest_config(config))
    mk = market or load_market(db)
    S = mk["sessions"]
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

    missing = sorted(set(book["names"]) - set(mk["adj"].columns))
    if missing:
        raise BookError(f"in the book but not in {Path(db).name}: {missing}")
    R = mk["adj"][book["names"]].ffill().pct_change(fill_method=None).fillna(0.0).to_numpy()
    groups = [book["group_of"][t] for t in book["names"]]
    freq = book["rebalance"]["frequency"]
    threshold = book["rebalance"]["drift_threshold"]

    def target_fn(i):
        return targets_at(book, mk["fcap"].iloc[i], str(S[i].date()))

    navs, events, w, tgt = simulate(R, S, i0, freq, threshold, cfg, target_fn, groups)
    nav = np.array(navs)
    bnav = bench.to_numpy() / bench.iloc[0]
    lo, hi = book["holdings"]["min"], book["holdings"]["max"]
    for e in events:
        e["in_range"] = lo <= e["holdings"] <= hi

    equity = pd.DataFrame({"date": window.date, "portfolio": nav, "benchmark": bnav})
    reb = pd.DataFrame(events, columns=["decision", "fill", "trigger", "group_drift",
                                        "turnover", "cost", "holdings", "in_range", "gone"])
    reb["gone"] = reb["gone"].map(lambda g: "; ".join(g) if g else "")
    end = pd.DataFrame({"ticker": book["names"], "group": groups,
                        "weight": w, "target": tgt})
    end = end[(end["weight"] > EPS) | (end["target"] > EPS)]
    end = end.sort_values("weight", ascending=False).reset_index(drop=True)
    comps = {}
    for code in sorted(mk["bench"].columns):
        col = mk["bench"][code].loc[window]
        if not col.isna().any():
            b = col.to_numpy() / col.iloc[0]
            comps[code] = {"equity": b, "stats": statistics(nav, b, events, cfg["risk_free_rate"])}
    return {"name": name, "anchor": book["anchor"], "benchmark": benchmark,
            "start": window[0].date(), "end": window[-1].date(), "config": cfg,
            "rebalance": book["rebalance"], "holdings_range": book["holdings"],
            "constraints": book["compute"]["cap_note"], "tactical": book["compute"]["tac_note"],
            "equity": equity, "rebalances": reb, "holdings_end": end,
            "stats": comps[benchmark]["stats"], "benchmarks": comps,
            "messages": messages}


def summary_row(res: dict) -> dict:
    s, c = res["stats"], res["config"]
    return {"portfolio": res["name"], "benchmark": res["benchmark"],
            "start": res["start"], "end": res["end"], "sessions": s["sessions"],
            **{f"cfg_{k}": v for k, v in c.items()},
            **{f"p_{k}": v for k, v in s["portfolio"].items()},
            **{f"b_{k}": v for k, v in s["benchmark"].items()},
            **{k: s[k] for k in ("excess", "tracking_error", "information_ratio", "beta",
                                 "turnover", "cost", "n_calendar", "n_drift")}}


def build(name: str, start: str | None = None, benchmark: str = "VNINDEX",
          portfolio: Path = PORTFOLIO, params_dir: Path = PARAMS, db: Path = DB) -> dict:
    """run() plus the files and the printed report."""
    res = run(name, start, benchmark, portfolio=portfolio, params_dir=params_dir, db=db)
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
    cfg_src = (f"portfolio/{name}/{BT_CONFIG}" if (portfolio / name / BT_CONFIG).exists()
               else "defaults (no backtest_config.json)")
    (out / "built_from.txt").write_text(
        f"portfolio:   {name}, book as of anchor {res['anchor']}\n"
        f"window:      {res['start']} -> {res['end']} ({s['sessions']} sessions)\n"
        f"benchmark:   {res['benchmark']}, PRICE index rebased to the start close\n"
        f"mandate:     rebalance {freq}; drift {drift}; holdings "
        f"{res['holdings_range']['min']}-{res['holdings_range']['max']}\n"
        f"constraints: {res['constraints']}\n"
        f"tactical:    {res['tactical']}\n"
        f"config:      {cfg_src}: lag {c['lag_sessions']}, brokerage "
        f"{c['brokerage_bps']:g} bps a side, sell tax {c['sell_tax_bps']:g} bps, "
        f"risk-free {c['risk_free_rate']:.2%}\n"
        f"returns:     total return on close_adj; benchmark is price return, so\n"
        f"             the portfolio leads it by roughly the dividend yield\n"
        f"bias:        today's group map and book held backwards (survivorship,\n"
        f"             look-ahead); read excess and spreads before levels\n"
        f"built_at:    {datetime.now().astimezone():%Y-%m-%d %H:%M}\n",
        encoding="utf-8")

    p, b = s["portfolio"], s["benchmark"]
    fmt = lambda v, f: "n/a" if v is None else format(v, f)
    print(f"OK    {name}  {res['start']} -> {res['end']}  {s['sessions']} sessions  "
          f"vs {res['benchmark']}")
    print(f"      mandate rebalance {freq}, drift {drift}; lag {c['lag_sessions']}, "
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
    print(f"      rebalances {s['n_calendar']} calendar, {s['n_drift']} drift; turnover "
          f"{s['turnover']:.1%}; costs {s['cost'] * 1e4:.1f} bps")
    for e in res["rebalances"].itertuples():
        if not e.in_range:
            print(f"WARN  {e.fill}: {e.holdings} holdings outside "
                  f"{res['holdings_range']['min']}-{res['holdings_range']['max']}")
        if e.gone:
            print(f"WARN  {e.decision}: no priced name in {e.gone}; weight renormalised")
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="portfolio folder under portfolio/")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD, default the first session")
    ap.add_argument("--benchmark", default="VNINDEX", help="index code in index_prices")
    a = ap.parse_args(argv)
    try:
        build(a.name, a.start, a.benchmark)
    except BookError as e:
        print(f"FAIL  {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
