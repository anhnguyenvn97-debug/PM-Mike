"""Desk: a local Flask UI over the scripts. Holds no state of its own.

Every page reads the files the scripts read, and every action calls the script
function (ingest, portfolio new/record/reset/delete, target, backtest_engine)
and returns what it printed. The UI writes only desk-owned files:

    statement.json          Statement and Rebalancing steps, validate_statement first
    constraints.json        Constraints step, validate_constraints first
    book.json               Allocation step, portfolio.write_book (group map checked)
    screens.json            Monitor step (name screens) and Replication (position
                            screens, D70), validate_screens first
    backtest_config.json    Backtest step, validate_backtest_config first
    decisions/              Target step Record (one per date), Decisions step Reset

Every calculation takes a date (D57). GET /api/p/<name>?as_of=YYYY-MM-DD
returns the portfolio with the universe of that date's session (the latest
without it): each group's float-cap weight and members with float cap,
turnover, FOL limit, the screen flags (D62) and "new" (not in the universe the
last decision was priced on). POST /preview {groups, tactical, constraints,
as_of} runs target.compute non-strict on the unsaved book, priced as of the
date, and returns the weights, the active checks, the names the D59 rule drops
("drops") and the same universe block for that session.

Decision log (D53, D56, D60): POST /api/p/<name>/decisions {effective, kind,
note, version, dirty} records the book AS SAVED ON DISK with
portfolio.record_decision (strict build on the date's session; the first
decision is inception whatever kind is sent, later ones need period or active
and a date after inception), then writes target/ for the same date
(target.build). It is version-checked against book.json and refused while the
page has unsaved book edits (dirty). GET /api/p/<name>/decisions, and
"decisions" in /api/p/<name>, list the log newest last, each with its report
against today's universe (portfolio.evaluate), the profile as recorded
("groups", "tactical", "holdings", "flags") and "setup_differs" (its
setup_hash is not today's, D61); "head_matches_book" says whether book.json
equals the latest profile (null without one). GET /api/p/<name>/decisions
also returns "timeline" (backtest_engine.timeline, D68): the recorded
decisions and the calendar rebalances derived from them, derived on read.
POST /reset archives the log (portfolio.reset). GET /monitor is
backtest_engine.monitor (D63).

Writes are atomic (temp file, then rename). Every save may carry the "version"
(mtime and size fingerprint) of the file it rewrites, as /api/p/<name> served
it; a save whose version no longer matches the disk FAILs, so a hand edit or a
second tab is never silently overwritten. Saving the book applies the auto-NO
rule: a group with no investable name left after tactical claims is rated NO,
and so is a tactical group that claims nothing.

The Data tab lists every drop in data/fiinpro/ (stock and index alike, see
ingest.py) and, per benchmark in index_prices, its sessions, range, and the
stock sessions it lacks. Rebuild database runs ingest.main, which loads both.
It also lists the tickers on the latest session the group map lacks (every
calculation on that session FAILs until they are mapped) and that session's
universe by group ("universe" in /api/state). Group map edits need no
rebuild: the next calculation reads the file. POST /api/open/group_map opens
index/group_map_live.csv in the app Windows associates with .csv (a hand
edit; the desk never writes it).

Backtest tab: POST /api/p/<name>/backtest runs backtest_engine.run with the
page's start, benchmark and unsaved trading config (nothing written) and
returns curves, statistics per benchmark, the rebalance log and end holdings.
It replays decisions/ when there are any, unless the body says "mechanical":
true. PUT /backtest_config saves backtest_config.json (version-checked).

Replication (D69, D70), the third section: POST /api/p/<name>/replicate {aum,
cash_pct, exec_date} is replicate.run (nothing written): the ticket that
builds the target in force from cash at the execution session's open, sized
on the close before it, with the name and position screen flags. An
execution date after the last session returns "unattainable" and no lines.
The position thresholds save through PUT /screens with the whole file.

Book spec, as the page sends it:

    {"groups":   {"<group>": {"rating": "OW2", "pp": 4.5,
                              "investable": ["TCB", ...]}},
     "tactical": {"on": true, "groups": [{"name": "SOE Divestment",
                  "rating": "OW1", "pp": 2, "members": ["GAS"]}]}}

pp is the active weight in percentage points (null or missing = 0; NO always
0). The tier range, floor, net and budget are target.py's to check. Tickers
must belong to their group in the group map (tactical: any mapped ticker), and
a stock sits in at most one tactical group.

Bound to 127.0.0.1 only. Mutations need a JSON body and a local Host header,
so a web page elsewhere cannot drive the app. A port something already answers
on is refused at start: Windows lets a second server bind it too, and the two
then split the requests (a stale desk would serve old routes).

Usage
    .venv\\Scripts\\python.exe scr\\app.py            http://127.0.0.1:5000
"""
import contextlib
import io
import os
import re
import sys
import threading
from pathlib import Path

import backtest_engine
import duckdb
import ingest
import pandas as pd
import params as params_mod
import replicate
import target
from common import (
    BOOK,
    BT_CONFIG,
    CAPS,
    CONSTRAINTS,
    DB,
    DECISION_COLS,
    GROUP_MAP,
    PORTFOLIO,
    SCREENS_FILE,
    STATEMENT,
    BookError,
    atomic_text,
    default_backtest_config,
    default_constraints,
    default_screens,
    dump,
    load_backtest_config,
    load_constraints,
    load_decisions,
    load_screens,
    load_statement,
    sessions,
    setup_hash,
    validate_backtest_config,
    validate_constraints,
    validate_screens,
    validate_statement,
)
from flask import Flask, abort, jsonify, render_template, request

import portfolio as pf

HERE = Path(__file__).resolve().parent
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LOCAL = {"127.0.0.1", "localhost"}

app = Flask(__name__, template_folder=str(HERE / "templates"),
            static_folder=str(HERE / "static"))
app.config.update(PORTFOLIO=PORTFOLIO, DB=DB)
LOCK = threading.RLock()      # one action at a time: stdout capture and file swaps


# ---------- file helpers ----------

VERSIONED = {"statement": (STATEMENT,), "constraints": (CONSTRAINTS,),
             "book": (BOOK,), "screens": (SCREENS_FILE,), "backtest": (BT_CONFIG,)}


def version(home: Path, key: str) -> str:
    """Fingerprint (mtime, size) of the files a save of `key` rewrites."""
    parts = []
    for f in VERSIONED[key]:
        p = home / f
        parts.append(f"{p.stat().st_mtime_ns}:{p.stat().st_size}" if p.exists() else "-")
    return "|".join(parts)


def check_version(home: Path, key: str, sent) -> None:
    """Refuse a save built on a stale copy. sent=None (a client that did not
    load the file first, e.g. a script) skips the check."""
    if sent is not None and sent != version(home, key):
        names = ", ".join(VERSIONED[key])
        raise BookError(f"{names} changed on disk since the page loaded; "
                        "reload the portfolio and redo the edit")


def read_kv(path: Path) -> dict:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        k, _, v = line.partition(":")
        if k.strip():
            out[k.strip()] = v.strip()
    return out


# ---------- views of the files ----------

def db_path() -> Path | None:
    db = app.config["DB"]
    return db if db is not None and Path(db).exists() else None


def session_list() -> list[str]:
    db = db_path()
    return [str(d) for d in sessions(db)] if db else []


def market() -> dict:
    db = app.config["DB"]
    drops = sorted(p for p in ingest.DROP.iterdir() if p.is_file()
                   and p.suffix.lower() in (".xlsx", ".xls", ".csv")
                   and not p.name.startswith("~$")) if ingest.DROP.exists() else []
    have = db_path() is not None
    out = {"db": have, "drops": [
        {"file": p.name, "mb": round(p.stat().st_size / 1e6, 2),
         "newer_than_db": have and p.stat().st_mtime > db.stat().st_mtime}
        for p in drops]}
    if not have:
        return out
    con = duckdb.connect(db, read_only=True)
    try:
        rows, tickers = con.execute(
            "SELECT count(*), count(DISTINCT ticker) FROM prices").fetchone()
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        cols = {r[0] for r in con.execute("DESCRIBE loads").fetchall()} \
            if "loads" in tables else set()
        kind = "kind" if "kind" in cols else "'stock'"  # database built before index drops
        loads = con.execute(
            "SELECT file_name, row_count, dropped, date_min, date_max, ticker_count, "
            f"loaded_at, {kind} FROM loads ORDER BY loaded_at").fetchall() \
            if "loads" in tables else []
        bench = con.execute(
            "SELECT i.code, count(*), min(i.trade_date), max(i.trade_date), "
            "(SELECT count(*) FROM (SELECT DISTINCT trade_date FROM prices) s "
            " WHERE s.trade_date NOT IN (SELECT trade_date FROM index_prices j "
            "                            WHERE j.code = i.code)), "
            "count(*) FILTER (WHERE i.trade_date > (SELECT max(trade_date) FROM prices)) "
            "FROM index_prices i GROUP BY 1 ORDER BY 1").fetchall() \
            if "index_prices" in tables else []
        mcap = ingest.mcap_mismatch(con) if rows and "market_cap" in {
            r[0] for r in con.execute("DESCRIBE prices").fetchall()} else []
    finally:
        con.close()
    out.update(rows=rows, tickers=tickers, loads=[
        {"file": r[0], "rows": r[1], "dropped": r[2], "from": str(r[3]),
         "to": str(r[4]), "tickers": r[5], "loaded_at": str(r[6])[:16], "kind": r[7]}
        for r in loads],
        benchmarks=[{"code": c, "sessions": n, "from": str(a), "to": str(b), "missing": m,
                     "late": lt} for c, n, a, b, m, lt in bench],
        mcap={"rows": sum(r[1] for r in mcap), "names": len(mcap),
              "tickers": [{"ticker": t, "rows": c, "worst": w} for t, c, w in mcap[:5]]},
        rebuilt=stamp(db))
    return out


def stamp(path: Path) -> str | None:
    if not path.exists():
        return None
    return (pd.Timestamp(path.stat().st_mtime, unit="s", tz="UTC")
            .tz_convert(None).strftime("%Y-%m-%d %H:%M UTC"))


def unmapped() -> list[str]:
    """Tickers on the latest session that group_map_live.csv lacks or leaves blank."""
    db = db_path()
    if db is None or not GROUP_MAP.exists():
        return []
    con = duckdb.connect(db, read_only=True)
    try:
        on = {r[0] for r in con.execute(
            "SELECT DISTINCT ticker FROM prices "
            "WHERE trade_date = (SELECT max(trade_date) FROM prices)").fetchall()}
    finally:
        con.close()
    g = params_mod.load_map(GROUP_MAP)
    return sorted(on - set(g.loc[g["group"] != "", "ticker"]))


def universe_summary() -> dict | None:
    """The latest session's universe for the Data tab: groups by float-cap weight,
    the median 21-day turnover, how many names have a FOL limit."""
    if db_path() is None:
        return None
    try:
        frame = params_mod.at(app.config["DB"])
    except BookError as e:
        return {"error": str(e)}
    total = float(frame["float_cap"].sum())
    g = frame.groupby("group").agg(n=("ticker", "size"), fcap=("float_cap", "sum"))
    g = g.sort_values("fcap", ascending=False)
    to = frame["turnover_21_pct"].dropna()
    return {"as_of": str(frame.attrs["session"]), "tickers": len(frame),
            "groups": [{"name": n, "n": int(r.n), "fcap": float(r.fcap) / 1e9,
                        "weight": float(r.fcap) / total} for n, r in g.iterrows()],
            "median_to": float(to.median()) if len(to) else None,
            "fol": int(frame["fol_limit"].notna().sum()),
            "no_float_cap": frame.attrs.get("no_float_cap", [])}


def priced_universe(profiles: list[dict]) -> set:
    """Tickers of the universe the last decision was priced on; empty without one."""
    last = profiles[-1] if profiles else None
    if not last or not last.get("priced_as_of"):
        return set()
    try:
        return set(params_mod.at(app.config["DB"], last["priced_as_of"])["ticker"])
    except BookError:
        return set()


def universe_json(home: Path, frame: pd.DataFrame, profiles: list[dict]) -> dict:
    """One session's universe: groups with float-cap weight and members with float
    cap (bn VND), turnover, FOL limit, flags and whether they are new."""
    par = frame.set_index("ticker")
    total = float(par["float_cap"].sum())
    before = priced_universe(profiles)
    flags: dict = {}
    for f in pf.screen_flags(frame, frame["ticker"], load_screens(home)):
        flags.setdefault(f["t"], []).append(f)
    groups = []
    for g, ts in params_mod.universe(frame).items():
        members = []
        for t in ts:
            to, fol = par.at[t, "turnover_21_pct"], par.at[t, "fol_limit"]
            members.append({"t": t, "fcap": float(par.at[t, "float_cap"]) / 1e9,
                            "to": None if pd.isna(to) else float(to),
                            "fol": None if pd.isna(fol) else float(fol),
                            "new": bool(before) and t not in before})
        groups.append({"name": g, "weight": float(par.loc[ts, "float_cap"].sum()) / total,
                       "members": members})
    return {"as_of": str(frame.attrs["session"]), "groups": groups, "flags": flags,
            "co": {t: str(par.at[t, "company_name"]) for t in par.index}}


def preview_json(res: dict) -> dict:
    sec, hold, act = res["allocation"], res["holdings"], res["active"]
    rows = [{"group": r.group, "kind": r.kind, "rating": r.rating,
             "pp": float(r.active_pp), "base": float(r.baseline_weight),
             "neutral": float(r.neutral_weight), "unc": float(r.uncapped_weight),
             "w": float(r.weight), "vs_neutral": float(r.active_vs_neutral_pp),
             "lo": act["range"][r.group][0], "hi": act["range"][r.group][1],
             "migrated": float(r.migrated_out), "capped": bool(r.capped),
             "full": bool(r.full), "n": int(r.n_members),
             "n_all": len(res["full"][r.group])} for r in sec.itertuples()]
    holdings = [{"t": r.ticker, "group": r.sector, "home": r.home_sector,
                 "w": float(r.target_weight), "fcap": float(r.fcap) / 1e9,
                 "pin": r.pin} for r in hold.itertuples()]
    return {"ok": True, "rows": rows, "holdings": holdings, "n": len(holdings),
            "as_of": str(res["as_of"]), "requested": res["requested"],
            "in_range": res["in_range"], "range_note": res["range_note"],
            "cap_note": res["cap_note"], "tac_note": res["tac_note"],
            "report": res["report"], "messages": res["messages"], "drops": res["reconcile"],
            "active": {k: act[k] for k in ("net_pp", "used_pp", "budget_pp", "faults")}}


def num(x):
    """JSON-safe float: NaN and None become null."""
    return None if x is None or pd.isna(x) else float(x)


def stats_json(s: dict) -> dict:
    return {k: ({kk: num(vv) for kk, vv in v.items()} if isinstance(v, dict) else num(v))
            for k, v in s.items()}


def backtest_json(res: dict) -> dict:
    eq = res["equity"]
    return {
        "ok": True, "name": res["name"], "as_of": str(res["as_of"]),
        "start": str(res["start"]), "end": str(res["end"]), "benchmark": res["benchmark"],
        "config": res["config"], "rebalance": res["rebalance"],
        "holdings_range": res["holdings_range"], "constraints": res["constraints"],
        "tactical": res["tactical"], "messages": res["messages"],
        "dates": [str(d) for d in eq["date"]],
        "portfolio": [float(v) for v in eq["portfolio"]],
        "benchmarks": {c: {"equity": [float(v) for v in b["equity"]], "stats": stats_json(b["stats"])}
                       for c, b in res["benchmarks"].items()},
        "rebalances": [{"decision": str(r.decision), "fill": str(r.fill), "trigger": r.trigger,
                        "also": r.also, "profile": r.profile,
                        "deferred_from": r.deferred_from,
                        "policy": r.policy, "target_as_of": str(r.target_as_of),
                        "drift": num(r.group_drift), "turnover": float(r.turnover),
                        "cost": float(r.cost), "holdings": int(r.holdings),
                        "in_range": bool(r.in_range), "dropped": r.dropped}
                       for r in res["rebalances"].itertuples()],
        "holdings_end": [{"t": r.ticker, "group": r.group, "w": float(r.weight),
                          "target": float(r.target)} for r in res["holdings_end"].itertuples()],
        "timeline": None if res["timeline"] is None else [
            {**d, "placed": d["placed"] and str(d["placed"]),
             "applied": d["applied"] and str(d["applied"])} for d in res["timeline"]],
    }


def summary(name: str) -> dict:
    home = app.config["PORTFOLIO"] / name
    out = {"name": name, "statement": None, "error": None, "constraints_on": [],
           "decisions": 0, "last": None,
           "built_at": read_kv(home / "target" / "built_from.txt").get("built_at")}
    try:
        out["statement"] = load_statement(home)
        out["constraints_on"] = [k for k in CAPS if load_constraints(home)[k]["on"]]
        profiles = load_decisions(home)
        out["decisions"] = len(profiles)
        if profiles:
            out["last"] = {k: profiles[-1].get(k) for k in ("id", "kind", "effective")}
        if db_path() is not None:
            p = preview_json(target.compute(name, app.config["PORTFOLIO"], app.config["DB"],
                                            strict=False))
            out.update(n=p["n"], top=p["rows"][0] if p["rows"] else None)
    except (BookError, ValueError, OSError) as e:
        out["error"] = str(e)
    return out


def portfolio_names() -> list[str]:
    root = app.config["PORTFOLIO"]
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and pf.NAME.match(p.name) and (p / STATEMENT).exists())


def decisions_json(home: Path, profiles: list[dict], cols: dict | None) -> list[dict]:
    """The decision log, each profile with its report against today's universe."""
    try:
        today = setup_hash(home)
    except BookError:
        today = None
    return [{k: d.get(k) for k in DECISION_COLS}
            | {"groups": d["groups"], "tactical": d.get("tactical") or {"on": False, "groups": []},
               "holdings": d.get("holdings"), "flags": d.get("flags"),
               "setup_differs": bool(d.get("setup_hash")) and today is not None
               and d["setup_hash"] != today,
               "report": pf.evaluate(d, cols)[1] if cols is not None else None}
            for d in profiles]


def head_matches(spec: dict | None, profiles: list[dict]) -> bool | None:
    """Does book.json equal the latest profile, both normalized? None when there
    is no decision, no book.json, or either side does not validate."""
    if not profiles or spec is None:
        return None
    last = {"groups": profiles[-1]["groups"], "tactical": profiles[-1].get("tactical")}
    try:
        return pf.normalize_book(spec)[0] == pf.normalize_book(last)[0]
    except BookError:
        return None


def detail(name: str, as_of: str | None = None) -> dict:
    home = app.config["PORTFOLIO"] / name
    out = {"name": name, "errors": [], "statement": None,
           "constraints": default_constraints(), "screens": default_screens(),
           "book": None, "universe": None, "latest": None,
           "versions": {k: version(home, k) for k in VERSIONED},
           "decisions": [], "head_matches_book": None, "setup_hash": None}
    for key, fn in (("statement", load_statement), ("constraints", load_constraints),
                    ("screens", load_screens)):
        try:
            v = fn(home)
            if v is not None:
                out[key] = v
        except BookError as e:
            out["errors"].append(str(e))
    try:
        out["setup_hash"] = setup_hash(home)
    except BookError:
        pass
    spec = None
    try:
        spec = pf.read_book(home)
        out["book"] = pf.normalize_book(spec)[0] if spec is not None else None
    except BookError as e:
        out["errors"].append(f"{BOOK}: {e}")
    try:
        profiles = load_decisions(home)
    except (BookError, OSError) as e:
        out["errors"].append(str(e))
        profiles = []
    cols = None
    if db_path() is not None:
        try:
            out["latest"] = session_list()[-1]
            frame = params_mod.at(app.config["DB"], as_of)
            out["universe"] = universe_json(home, frame, profiles)
            cols = params_mod.universe(params_mod.at(app.config["DB"]))
        except BookError as e:
            out["errors"].append(str(e))
    out["decisions"] = decisions_json(home, profiles, cols)
    out["head_matches_book"] = head_matches(out["book"] if spec is not None else None,
                                            profiles)
    return out


# ---------- request plumbing ----------

@app.before_request
def guard():
    host = request.host.rsplit(":", 1)[0]
    if host not in LOCAL:
        abort(403)
    if request.method in ("POST", "PUT", "DELETE") and not request.is_json:
        abort(415)


def body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400)
    return data


def home_of(name: str) -> Path:
    home = app.config["PORTFOLIO"] / name
    if not pf.NAME.match(name) or not home.is_dir():
        abort(404)
    return home


def run(fn, *args, **kwargs):
    """Call a script function under the lock -> JSON with ok, log, result."""
    buf = io.StringIO()
    with LOCK, contextlib.redirect_stdout(buf):
        try:
            result = fn(*args, **kwargs)
            ok = not (isinstance(result, int) and not isinstance(result, bool) and result)
        except (BookError, ValueError) as e:
            print(f"FAIL  {e}")
            result, ok = None, False
    return ok, buf.getvalue(), result


def reply(ok: bool, log: str, **extra):
    return jsonify(ok=ok, log=log, **extra), (200 if ok else 422)


def date_arg(v, what: str = "date"):
    if v is None or v == "":
        return None
    if not (isinstance(v, str) and DATE.match(v)):
        raise BookError(f"{what} {v!r} is not YYYY-MM-DD")
    return v


# ---------- routes ----------

@app.get("/")
def index():
    return render_template("desk.html")


@app.get("/api/state")
def state():
    with LOCK:
        return jsonify(market=market(), sessions=session_list(), unmapped=unmapped(),
                       universe=universe_summary(), group_map={"edited": stamp(GROUP_MAP)},
                       portfolios=[summary(n) for n in portfolio_names()])


@app.post("/api/open/group_map")
def open_group_map():
    """Open index/group_map_live.csv in the app Windows associates with .csv
    (the desk never writes it; the next calculation reads the saved file)."""
    if not GROUP_MAP.exists():
        return reply(False, f"FAIL  {GROUP_MAP} not found\n")
    try:
        os.startfile(GROUP_MAP)                        # Windows only
    except (AttributeError, OSError) as e:
        return reply(False, f"FAIL  cannot open {GROUP_MAP}: {e}\n")
    return reply(True, f"opened {GROUP_MAP}\n")


@app.post("/api/run/ingest")
def run_ingest():
    ok, log, _ = run(ingest.main, [])
    return reply(ok, log)


@app.get("/api/p/<name>")
def get_portfolio(name):
    home_of(name)
    as_of = request.args.get("as_of") or None
    if as_of is not None and not DATE.match(as_of):
        abort(400)
    with LOCK:
        return jsonify(detail(name, as_of))


@app.post("/api/p")
def new_portfolio():
    b = body()
    name = str(b.get("name", ""))
    ok, log, _ = run(pf.new, name, app.config["PORTFOLIO"], b.get("statement"))
    return reply(ok, log)


@app.delete("/api/p/<name>")
def delete_portfolio(name):
    home_of(name)
    ok, log, _ = run(pf.delete, name, yes=bool(body().get("yes")),
                     portfolio=app.config["PORTFOLIO"])
    return reply(ok, log)


def save_json(name: str, key: str, fname: str, validate):
    home = home_of(name)
    b = body()

    def save(obj):
        valid = validate(obj)
        if key == "screens":             # every screen present; the others save as sent
            obj = valid
        check_version(home, key, b.get("version"))
        atomic_text(home / fname, dump(obj))
        print(f"OK    saved portfolio/{name}/{fname}")
    ok, log, _ = run(save, b.get(key))
    return reply(ok, log, version=version(home, key))


@app.put("/api/p/<name>/statement")
def save_statement(name):
    return save_json(name, "statement", STATEMENT, validate_statement)


@app.put("/api/p/<name>/constraints")
def save_constraints(name):
    return save_json(name, "constraints", CONSTRAINTS, validate_constraints)


@app.put("/api/p/<name>/screens")
def save_screens(name):
    return save_json(name, "screens", SCREENS_FILE, validate_screens)


@app.put("/api/p/<name>/book")
def save_book(name):
    home = home_of(name)
    b = body()

    def save():
        spec = {"groups": b.get("groups", {}), "tactical": b.get("tactical")}
        pf.normalize_book(spec, pf.group_map())            # validate before the version
        check_version(home, "book", b.get("version"))
        flipped = pf.write_book(home, spec)
        print(f"OK    saved portfolio/{name}/{BOOK}")
        for g in flipped:
            print(f"WARN  {g}: no investable name left, rated NO")
        on = bool((spec.get("tactical") or {}).get("on"))
        print(f"      tactical overlay {'on' if on else 'off'}")
    ok, log, _ = run(save)
    return reply(ok, log, version=version(home, "book"))


@app.get("/api/p/<name>/decisions")
def get_decisions(name):
    home = home_of(name)
    with LOCK:
        try:
            profiles = load_decisions(home)
            cols = (params_mod.universe(params_mod.at(app.config["DB"]))
                    if db_path() is not None else None)
            spec = pf.read_book(home)
            tl = (backtest_engine.timeline(name, app.config["PORTFOLIO"], app.config["DB"])
                  if db_path() is not None else [])
            return jsonify(ok=True, decisions=decisions_json(home, profiles, cols),
                           head_matches_book=head_matches(spec, profiles), timeline=tl)
        except (BookError, ValueError, OSError) as e:
            return jsonify(ok=False, error=str(e))


@app.post("/api/p/<name>/decisions")
def record_decision(name):
    home = home_of(name)
    b = body()

    def save():
        if b.get("dirty"):
            raise BookError("the book has unsaved edits; save it first, a decision "
                            "records the book as saved")
        check_version(home, "book", b.get("version"))
        eff = date_arg(b.get("effective"), "effective date")
        d = pf.record_decision(home, eff, b.get("kind"), str(b.get("note") or ""),
                               db=app.config["DB"])
        target.build(name, app.config["PORTFOLIO"], app.config["DB"], as_of=d["effective"])
        return d["id"]
    ok, log, did = run(save)
    return reply(ok, log, id=did)


@app.post("/api/p/<name>/reset")
def reset(name):
    home = home_of(name)
    ok, log, _ = run(pf.reset, home)
    return reply(ok, log)


@app.post("/api/p/<name>/preview")
def preview(name):
    home = home_of(name)
    b = body()

    def calc():
        as_of = date_arg(b.get("as_of"), "effective date")
        spec = ({"groups": b.get("groups", {}), "tactical": b.get("tactical")}
                if "groups" in b else None)
        res = target.compute(name, app.config["PORTFOLIO"], app.config["DB"], as_of=as_of,
                             spec=spec, constraints=b.get("constraints"), strict=False)
        out = preview_json(res)
        out["universe"] = universe_json(home, res["params"], load_decisions(home))
        return out
    ok, log, res = run(calc)
    if not ok:
        return jsonify(ok=False, error=log.removeprefix("FAIL  ").strip())
    return jsonify(res)


@app.get("/api/p/<name>/monitor")
def monitor(name):
    home_of(name)

    def calc():
        m = backtest_engine.monitor(name, app.config["PORTFOLIO"], app.config["DB"])
        m["now"] = m["now"] and {k: (num(v) if isinstance(v, float) else v)
                                 for k, v in m["now"].items()}
        return m
    ok, log, res = run(calc)
    if not ok:
        return jsonify(ok=False, error=log.removeprefix("FAIL  ").strip())
    return jsonify(ok=True, **res)


@app.post("/api/p/<name>/replicate")
def replicate_ticket(name):
    home_of(name)
    b = body()

    def calc():
        return replicate.run(name, b.get("aum"), b.get("cash_pct", 0),
                             date_arg(b.get("exec_date"), "execution date"),
                             app.config["PORTFOLIO"], app.config["DB"])
    ok, log, res = run(calc)
    if not ok:
        return jsonify(ok=False, error=log.removeprefix("FAIL  ").strip())
    return jsonify(ok=True, **res)


@app.get("/api/p/<name>/backtest")
def get_backtest(name):
    home = home_of(name)
    with LOCK:
        out = {"name": name, "version": version(home, "backtest"),
               "defaults": default_backtest_config(), "config": default_backtest_config(),
               "error": None}
        try:
            out["config"] = load_backtest_config(home)
        except BookError as e:
            out["error"] = str(e)
        return jsonify(out)


@app.post("/api/p/<name>/backtest")
def run_backtest(name):
    home = home_of(name)
    b = body()
    start, bench = b.get("start"), b.get("benchmark", "VNINDEX")
    if start is not None and not (isinstance(start, str) and DATE.match(start)):
        return jsonify(ok=False, error=f"start {start!r} is not YYYY-MM-DD")
    if not isinstance(bench, str):
        return jsonify(ok=False, error="benchmark must be an index code")

    def calc():
        timeline = None if b.get("mechanical") else load_decisions(home) or None
        return backtest_json(backtest_engine.run(
            name, start, bench, config=b.get("config"), portfolio=app.config["PORTFOLIO"],
            db=app.config["DB"] or DB, timeline=timeline))
    ok, log, res = run(calc)
    if not ok:
        return jsonify(ok=False, error=log.removeprefix("FAIL  ").strip())
    return jsonify(res)


@app.put("/api/p/<name>/backtest_config")
def save_backtest_config(name):
    home = home_of(name)
    b = body()

    def save(c):
        c = validate_backtest_config(c)
        check_version(home, "backtest", b.get("version"))
        atomic_text(home / BT_CONFIG, dump(c))
        print(f"OK    saved portfolio/{name}/{BT_CONFIG}")
    ok, log, _ = run(save, b.get("config"))
    return reply(ok, log, version=version(home, "backtest"))


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5000)
    a = ap.parse_args(argv)
    import socket
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", a.port)) == 0:
            print(f"FAIL  port {a.port} is already in use (another desk running?); "
                  f"stop it or pass --port", file=sys.stderr)
            return 1
    app.run(host="127.0.0.1", port=a.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
