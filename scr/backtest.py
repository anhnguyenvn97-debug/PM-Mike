"""Price-return backtester: a book's variants replayed on local_history.db.

STANDALONE from the forward-tracking pipeline. It reads the same hand-edited
configs the live builder reads, imports the weighting math from
build_portfolio_target.py READ-ONLY (one implementation of the tilt, zero
edits to that script), and writes only under portfolio/<name>/backtest/ --
never target/, never data/, never anything the live pipeline consumes.

Variants -- the full set the book's files allow, always. The on/off switches
in sector_cap.json and tactical_group.json are IGNORED here: a backtest prices
every scenario, not the one currently switched on. Only file PRESENCE gates a
variant:

    default              book constituents, every multiplier forced to 1.0
                         (a group whose book column is empty drops out and the
                         budget renormalises)
    tilt                 grid ratings and multipliers, exactly the live math
    sector_cap           tilt + max_weight from sector_cap.json
    tactical             tilt + tactical_group.csv overlay
    sector_cap_tactical  tilt + overlay + cap, live composition order
                         (tilt -> migrate -> cap)

    no sector_cap.json      -> cap variants skipped, noted in built_from.txt
    no tactical_group.csv   -> tactical variants skipped, noted likewise

Discipline comes from backtest_rebalance.json -- the portfolio's own copy,
else baseline's, else buy-and-hold (missing file = "rebalance: no"). See
backtest_rebalance.txt for the full data dictionary. In brief: scheduled
re-anchor per `calendar` on the period's first session, budgets re-derived
from that session's float cap (`anchor_on_rebalance`); an off-schedule drift
band that RESTORES the current target without re-anchoring; decisions observe
session t's close and fill `lag_sessions` later at `execution_price`.

Between events the book holds: weights drift with close_adj returns, a name
with no print carries its last price (return 0) and corrects at the next
tradeable session, and a late listing enters at the first scheduled rebalance
after it has a price. Baseline budgets are always free_float x market_cap /
outstanding_shares over the FULL baseline column -- deleted names keep their
budget in the sector (deletion is selection, not allocation), claimed names
take it with them (the tactical claim is allocation).

Costs: brokerage both sides plus sell tax on the one-way turnover; inception
pays the buy side only. Fill at close_adj applies the trade at that session's
close; open_adj splits the day (overnight on old weights, intraday on new).

Survivorship: local_history.db holds ONE frozen universe -- today's
constituents held backwards, no membership history. Stamped in built_from.txt.

Outputs, overwritten in portfolio/<name>/backtest/:
    equity.csv       date x variant NAV, base 1.0
    rebalances.csv   one row per executed event: trigger, group drift at the
                     decision, name-level turnover transacted, cost, and the
                     count of market_cap-identity violations on decision day
    summary.csv      per variant: CAGR, vol, max drawdown, turnover/yr,
                     cost drag, event counts
    built_from.txt   provenance: db sha256s, configs in force, variants
                     skipped, survivorship note

Usage
    .venv\\Scripts\\python.exe scr\\backtest.py hsc_strat_soe_dom
    .venv\\Scripts\\python.exe scr\\backtest.py hsc_strat_high_growth --variants default,tilt
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from build_portfolio_target import (
    BASELINE, BOOK, CAP, GRID, PORTFOLIO, ROOT, TAC,
    BookError, apply_caps, migrate, parse_ratings, read_grid, read_tactical,
)

REB = "backtest_rebalance.json"
ALL_VARIANTS = ["default", "tilt", "sector_cap", "tactical", "sector_cap_tactical"]
IDENTITY_TOL = 1e-4  # relative gap market_cap vs close_raw * shares


# --------------------------------------------------------------- config

def yes_no(cfg: dict, key: str, path: Path) -> bool:
    v = str(cfg.get(key, "no")).strip().lower()
    if v not in ("yes", "no"):
        raise BookError(f"{path.name}: {key} is {cfg.get(key)!r}, want yes|no")
    return v == "yes"


def read_rebalance(home: Path) -> tuple[dict, Path | None]:
    """Portfolio's own copy wins whole-file; else baseline's; else buy-and-hold."""
    for p in (home / REB, BASELINE / REB):
        if p.exists():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise BookError(f"{p} is not readable JSON: {e}")
            return cfg, p
    return {"rebalance": "no"}, None


def parse_edge(cfg: dict, key: str, src: Path | None):
    v = str(cfg.get(key, "default")).strip()
    if v.lower() == "default":
        return None
    try:
        return pd.Timestamp(datetime.strptime(v, "%d-%m-%Y").date())
    except ValueError:
        raise BookError(f"{src.name if src else REB}: {key} {v!r} is neither "
                        "\"default\" nor dd-mm-yyyy")


def read_cap_value(home: Path):
    """max_weight from sector_cap.json, IGNORING the on/off switch.
    Missing file -> None (cap variants unavailable)."""
    p = home / CAP
    if not p.exists():
        return None
    try:
        cfg = json.loads(p.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise BookError(f"{p.name} is not readable JSON: {e}")
    raw = cfg.get("max_weight")
    try:
        cap = float(raw)
    except (TypeError, ValueError):
        raise BookError(f"{CAP}: max_weight {raw!r} is not a number")
    if not 0 < cap <= 1:
        raise BookError(f"{CAP}: max_weight {cap} out of range (fraction: 0.25)")
    return cap


# --------------------------------------------------------------- engine

class Backtest:
    def __init__(self, a):
        home = PORTFOLIO / a.name
        if not home.is_dir():
            raise BookError(f"no such portfolio: {home}")
        self.name, self.home = a.name, home

        # --- discipline
        cfg, self.reb_src = read_rebalance(home)
        self.rebalance = yes_no(cfg, "rebalance", self.reb_src or home / REB)
        self.calendar = str(cfg.get("calendar", "quarterly")).strip().lower()
        if self.calendar not in ("monthly", "quarterly"):
            raise BookError(f"{REB}: calendar {cfg.get('calendar')!r}, "
                            "want monthly|quarterly")
        if str(cfg.get("on", "first_session")).strip() != "first_session":
            raise BookError(f"{REB}: on {cfg.get('on')!r}; only first_session "
                            "is implemented")
        self.start = parse_edge(cfg, "start", self.reb_src)
        self.end = parse_edge(cfg, "end", self.reb_src)
        self.anchor_on = yes_no(cfg, "anchor_on_rebalance", self.reb_src or home)
        self.band = yes_no(cfg, "drift_band", self.reb_src or home) and self.rebalance
        self.grain = str(cfg.get("drift_grain", "group")).strip().lower()
        if self.grain not in ("group", "name"):
            raise BookError(f"{REB}: drift_grain {cfg.get('drift_grain')!r}, "
                            "want group|name")
        self.threshold = float(cfg.get("drift_threshold", 0.05))
        self.lag = int(cfg.get("lag_sessions", 1))
        if self.lag < 0:
            raise BookError(f"{REB}: lag_sessions {self.lag} is negative")
        self.exec_price = str(cfg.get("execution_price", "close_adj")).strip()
        if self.exec_price not in ("close_adj", "open_adj"):
            raise BookError(f"{REB}: execution_price {self.exec_price!r}, "
                            "want close_adj|open_adj")
        self.brokerage = float(cfg.get("brokerage_bps", 0.0))
        self.sell_tax = float(cfg.get("sell_tax_bps", 0.0))

        # --- book, baseline membership, overlays (same grammar as the live builder)
        base = read_grid(BASELINE / GRID)
        book = read_grid(home / BOOK)
        if len(book) < 3 or book[2] != base[2]:
            raise BookError(f"{BOOK} sector row differs from the baseline grid")
        self.sectors = base[2]
        self.book_mult, self.book_rating = parse_ratings(book, self.sectors)

        def column(rows, j):
            return [r[j] for r in rows[3:] if j < len(r) and r[j]]

        self.full = {g: column(base, j) for j, g in enumerate(self.sectors)}
        self.home_of = {t: g for g in self.sectors for t in self.full[g]}
        self.book_members = {}
        for j, g in enumerate(self.sectors):
            raw = column(book, j)
            alien = sorted(set(raw) - set(self.full[g]))
            if alien:
                raise BookError(f"{g}: {alien} not in the baseline column")
            self.book_members[g] = raw

        self.cap_value = read_cap_value(home)
        if (home / TAC).exists():
            (self.tac_g, self.tac_m, self.tac_r,
             self.tac_members, self.claim) = read_tactical(
                home / TAC, self.sectors, self.home_of)
        else:
            self.tac_g, self.tac_m, self.tac_members, self.claim = [], {}, {}, {}

        # --- prices
        self.db = a.db
        con = duckdb.connect(str(a.db), read_only=True)
        tickers = sorted(self.home_of)
        ph = ",".join("?" * len(tickers))
        px = con.execute(
            f"select trade_date, ticker, open_adj, close_adj, close_raw, "
            f"market_cap, outstanding_shares, free_float "
            f"from prices where ticker in ({ph}) order by trade_date",
            tickers).df()
        self.loads = con.execute(
            "select file_name, sha256 from loads order by file_name").df()
        con.close()
        missing = sorted(set(tickers) - set(px.ticker))
        if missing:
            raise BookError(f"in the book but not in {a.db.name}: {missing}")

        px["trade_date"] = pd.to_datetime(px.trade_date)
        close = px.pivot(index="trade_date", columns="ticker", values="close_adj")
        opn = px.pivot(index="trade_date", columns="ticker", values="open_adj")
        px["fcap"] = px.free_float * px.market_cap / px.outstanding_shares
        fcap = px.pivot(index="trade_date", columns="ticker", values="fcap")
        px["bad"] = (px.market_cap - px.close_raw * px.outstanding_shares).abs() \
            > IDENTITY_TOL * px.market_cap
        self.bad_by_date = px.groupby("trade_date").bad.sum()

        idx = close.index
        lo = idx[0] if self.start is None else self.start
        hi = idx[-1] if self.end is None else self.end
        sessions = idx[(idx >= lo) & (idx <= hi)]
        if len(sessions) < 2 + self.lag:
            raise BookError(f"window {lo.date()} -> {hi.date()} holds "
                            f"{len(sessions)} sessions; nothing to backtest")
        self.sessions = sessions

        closef = close.ffill()
        opnf = opn.where(opn.notna(), closef)  # no open print: fill at carry
        self.ret = closef.pct_change(fill_method=None).loc[sessions]
        self.co_ret = (opnf / closef.shift(1) - 1).loc[sessions]
        self.oc_ret = (closef / opnf - 1).loc[sessions]
        self.fcap_ff = fcap.ffill()
        self.tickers = list(close.columns)

        per = sessions.to_period("Q" if self.calendar == "quarterly" else "M")
        if self.rebalance:
            self.sched = {i for i in range(len(sessions))
                          if i == 0 or per[i] != per[i - 1]}
        else:
            self.sched = {0}

    # ---------------------------------------------------- target weights

    def targets_at(self, d, use_tilt: bool, use_tac: bool, cap):
        """The live builder's math on session d's float cap: budgets over the
        FULL baseline column, deletions keep budget, claims move it, tilt
        multiplies, cap clips. Returns (name weights, group weights, group_of)."""
        f = self.fcap_ff.loc[d]
        alive = f.notna() & (f > 0)
        sector_fcap = {g: float(f[[t for t in self.full[g] if alive[t]]].sum())
                       for g in self.sectors}
        total = sum(sector_fcap.values())
        if total <= 0:
            raise BookError(f"no float cap anywhere on {d.date()}")

        if use_tac:
            claim = {t: g for t, g in self.claim.items() if alive[t]}
            bweight, _ = migrate(sector_fcap, total, claim, self.home_of, f)
            groups = self.sectors + self.tac_g
        else:
            claim = {}
            bweight = {g: v / total for g, v in sector_fcap.items()}
            groups = list(self.sectors)

        members = {g: [t for t in self.book_members[g] if alive[t] and t not in claim]
                   for g in self.sectors}
        if use_tac:
            for g in self.tac_g:
                members[g] = [t for t in self.tac_members[g] if alive[t]]

        if use_tilt:
            mult = dict(self.book_mult)
            if use_tac:
                mult.update(self.tac_m)
        else:
            mult = {g: 1.0 for g in groups}
        # a group with nothing alive to hold cannot carry weight; unlike the
        # live builder this is not an editing error here, just renormalise
        for g in groups:
            if not members[g]:
                mult[g] = 0.0

        denom = sum(bweight.get(g, 0.0) * mult[g] for g in groups)
        if denom <= 0:
            raise BookError(f"every group is empty or NO on {d.date()}")
        live_g = [g for g in groups if mult[g] > 0]
        w_raw = {g: bweight.get(g, 0.0) * mult[g] / denom for g in live_g}
        if cap is not None:
            if cap * len(live_g) < 1 - 1e-12:
                raise BookError(f"cap {cap:.2%} x {len(live_g)} live groups "
                                f"cannot fill the book on {d.date()}")
            w_grp, _ = apply_caps(w_raw, cap)
        else:
            w_grp = w_raw

        w = pd.Series(0.0, index=self.tickers)
        group_of = {}
        for g in live_g:
            sub = f[members[g]]
            w[members[g]] = w_grp[g] * sub / sub.sum()
            for t in members[g]:
                group_of[t] = g
        return w, w_grp, group_of

    def drift(self, w, target, group_of):
        if self.grain == "name":
            return 0.5 * float((w - target).abs().sum())
        gmap = pd.Series(group_of)
        gw = w.reindex(gmap.index).groupby(gmap).sum()
        tw = target.reindex(gmap.index).groupby(gmap).sum()
        return 0.5 * float((gw - tw).abs().sum())

    # ---------------------------------------------------- simulation

    def run(self, vname: str):
        use_tilt = vname != "default"
        use_tac = vname in ("tactical", "sector_cap_tactical")
        cap = self.cap_value if vname in ("sector_cap", "sector_cap_tactical") else None

        s = self.sessions
        n = len(s)
        w = pd.Series(0.0, index=self.tickers)
        nav, invested = 1.0, False
        target, group_of = None, None
        frozen = None  # inception target triple when anchor_on_rebalance is no
        pending = None
        navs = np.empty(n)
        events = []

        def do_fill(i, d):
            nonlocal w, nav, invested, target, group_of, pending
            tgt, w_grp, go = pending["triple"]
            if invested:
                turn = 0.5 * float((tgt - w).abs().sum())
                cost = turn * (2 * self.brokerage + self.sell_tax) / 1e4
            else:
                turn = float(tgt.sum())
                cost = turn * self.brokerage / 1e4  # buys only, nothing to sell
            nav *= 1 - cost
            w, target, group_of = tgt.copy(), tgt, go
            invested = True
            events.append({
                "variant": vname,
                "decision_date": pending["decision"].date(),
                "fill_date": d.date(),
                "trigger": pending["trigger"],
                "group_drift_at_decision": round(pending["drift"], 6)
                if pending["drift"] is not None else "",
                "turnover_1way": round(turn, 6),
                "cost": round(cost, 8),
                "bad_identity_rows": int(self.bad_by_date.get(pending["decision"], 0)),
            })
            pending = None

        for i, d in enumerate(s):
            fill_today = pending is not None and pending["fill"] == i
            if i > 0:
                if fill_today and self.exec_price == "open_adj":
                    for leg in (self.co_ret, self.oc_ret):
                        r = leg.loc[d].fillna(0.0)
                        pr = float((w * r).sum())
                        nav *= 1 + pr
                        w = w * (1 + r) / (1 + pr)
                        if fill_today:  # trade at the open, between the legs
                            do_fill(i, d)
                            fill_today = False
                else:
                    r = self.ret.loc[d].fillna(0.0)
                    pr = float((w * r).sum())
                    nav *= 1 + pr
                    w = w * (1 + r) / (1 + pr)
            if fill_today:  # close_adj fill, after the day's move
                do_fill(i, d)

            if pending is None and i + self.lag < n:
                if i in self.sched:
                    if self.anchor_on or target is None:
                        triple = self.targets_at(d, use_tilt, use_tac, cap)
                        if frozen is None:
                            frozen = triple
                    else:
                        triple = frozen
                    pending = {
                        "fill": i + self.lag, "decision": d,
                        "trigger": "inception" if not invested else "calendar",
                        "drift": self.drift(w, target, group_of) if invested else None,
                        "triple": triple,
                    }
                elif self.band and invested:
                    dr = self.drift(w, target, group_of)
                    if dr > self.threshold:
                        pending = {
                            "fill": i + self.lag, "decision": d,
                            "trigger": "band", "drift": dr,
                            "triple": (target, None, group_of),
                        }
                if pending is not None and self.lag == 0:
                    do_fill(i, d)
            navs[i] = nav

        return pd.Series(navs, index=s, name=vname), events


# --------------------------------------------------------------- report

def summarise(nav: pd.Series, events: list) -> dict:
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    r = nav.pct_change().dropna()
    dd = nav / nav.cummax() - 1
    turn = sum(e["turnover_1way"] for e in events if e["trigger"] != "inception")
    cost = sum(e["cost"] for e in events)
    return {
        "start": nav.index[0].date(), "end": nav.index[-1].date(),
        "sessions": len(nav),
        "total_return": round(nav.iloc[-1] - 1, 6),
        "cagr": round(nav.iloc[-1] ** (1 / years) - 1, 6),
        "vol_ann": round(float(r.std(ddof=0)) * 252 ** 0.5, 6),
        "max_drawdown": round(float(dd.min()), 6),
        "n_calendar": sum(e["trigger"] == "calendar" for e in events),
        "n_band": sum(e["trigger"] == "band" for e in events),
        "turnover_1way_per_yr": round(turn / years, 6),
        "cost_bps_per_yr": round(cost / years * 1e4, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="portfolio folder under portfolio/")
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "local_history.db")
    ap.add_argument("--variants", default=None,
                    help=f"comma list from: {','.join(ALL_VARIANTS)}")
    ap.add_argument("--out", type=Path, default=None,
                    help="output folder (default portfolio/<name>/backtest)")
    a = ap.parse_args()
    if not a.db.exists():
        print(f"FAIL  missing {a.db}; run scr/load_history.py first")
        return 1

    try:
        bt = Backtest(a)

        available, skipped = [], []
        for v in ALL_VARIANTS:
            if v in ("sector_cap", "sector_cap_tactical") and bt.cap_value is None:
                skipped.append((v, f"{CAP} absent"))
            elif v in ("tactical", "sector_cap_tactical") and not bt.tac_g:
                skipped.append((v, f"{TAC} absent"))
            else:
                available.append(v)
        if a.variants:
            asked = [v.strip() for v in a.variants.split(",") if v.strip()]
            unknown = sorted(set(asked) - set(ALL_VARIANTS))
            if unknown:
                raise BookError(f"unknown variants {unknown}; "
                                f"pick from {ALL_VARIANTS}")
            gone = [v for v in asked if v not in available]
            if gone:
                raise BookError(f"variants {gone} unavailable for {bt.name}: "
                                + "; ".join(f"{v}: {why}" for v, why in skipped
                                            if v in gone))
            run_list = [v for v in ALL_VARIANTS if v in asked]
        else:
            run_list = available

        curves, all_events, rows = [], [], []
        for v in run_list:
            nav, events = bt.run(v)
            curves.append(nav)
            all_events += events
            rows.append({"variant": v, **summarise(nav, events)})

        out = a.out or bt.home / "backtest"
        out.mkdir(exist_ok=True)
        eq = pd.concat(curves, axis=1)
        eq.index = eq.index.date
        eq.index.name = "date"
        eq.to_csv(out / "equity.csv", lineterminator="\n", float_format="%.8f")
        pd.DataFrame(all_events).to_csv(out / "rebalances.csv", index=False,
                                        lineterminator="\n")
        pd.DataFrame(rows).to_csv(out / "summary.csv", index=False,
                                  lineterminator="\n")

        band_note = (f"on, {bt.grain} grain, threshold {bt.threshold}"
                     if bt.band else "off")
        reb_note = ("buy-and-hold (no config found)" if bt.reb_src is None else
                    f"{bt.reb_src.relative_to(ROOT)}"
                    + (" (inherited from baseline)"
                       if bt.reb_src.parent == BASELINE else " (own copy)"))
        cap_note = ("absent -- cap variants skipped" if bt.cap_value is None else
                    f"max_weight {bt.cap_value} from {CAP}; on/off switch IGNORED")
        tac_note = ("absent -- tactical variants skipped" if not bt.tac_g else
                    f"{TAC}: {len(bt.tac_g)} group(s), {len(bt.claim)} claims; "
                    f"{'tactical_group.json'} switch IGNORED")
        loads_note = "\n".join(f"    {r.file_name}  {r.sha256}"
                               for r in bt.loads.itertuples())
        skip_note = ("\n".join(f"    {v}: {why}" for v, why in skipped)
                     or "    none")
        (out / "built_from.txt").write_text(
            f"db:          {a.db}\n{loads_note}\n"
            f"book:        portfolio/{bt.name}/{BOOK}\n"
            f"rebalance:   {reb_note}\n"
            f"sector_cap:  {cap_note}\n"
            f"tactical:    {tac_note}\n"
            f"window:      {bt.sessions[0].date()} -> {bt.sessions[-1].date()} "
            f"({len(bt.sessions)} sessions), start/end from config\n"
            f"discipline:  {'off -- buy-and-hold' if not bt.rebalance else bt.calendar}"
            f", anchor_on_rebalance {'yes' if bt.anchor_on else 'no'}, "
            f"band {band_note}, lag {bt.lag}, fill {bt.exec_price}, "
            f"costs {bt.brokerage:g}+{bt.brokerage:g}+{bt.sell_tax:g} bps\n"
            f"variants:    run: {', '.join(run_list)}\n"
            f"  skipped:\n{skip_note}\n"
            f"survivorship: local_history.db holds one frozen universe --\n"
            f"    today's constituents held backwards, no membership history.\n"
            f"    Every variant shares the bias; levels are optimistic,\n"
            f"    variant SPREADS are the reliable read.\n"
            f"built_at:    {datetime.now():%Y-%m-%d %H:%M}\n",
            encoding="utf-8")

        print(f"OK    {bt.name}  {bt.sessions[0].date()} -> "
              f"{bt.sessions[-1].date()}  {len(bt.sessions)} sessions")
        print(f"      rebalance {reb_note}")
        print(f"      variants run: {', '.join(run_list)}")
        for v, why in skipped:
            print(f"      skipped {v}: {why}")
        print(f"      -> {out}\\equity.csv, rebalances.csv, summary.csv, "
              f"built_from.txt")
        print()
        cols = ["variant", "total_return", "cagr", "vol_ann", "max_drawdown",
                "n_calendar", "n_band", "turnover_1way_per_yr", "cost_bps_per_yr"]
        summ = pd.DataFrame(rows)[cols]
        print(summ.to_string(index=False))
        return 0
    except BookError as e:
        print(f"FAIL  {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
