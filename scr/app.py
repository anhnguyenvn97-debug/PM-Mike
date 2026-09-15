"""Desk: a local Flask UI over the scripts. Holds no state of its own.

Every page reads the files the scripts read, and every action calls the script
function (ingest, params, baseline, portfolio new/fork/screen/delete,
target.build) and returns what it printed. The UI writes only hand-edit files:

    statement.json                   statement form, validate_statement first
    backtest_config.json             backtest tab settings, validate_backtest_config first
    constraints.json                 constraints step, validate_constraints first
    sector_constituents_custom.csv   book step: rows 0-1 and which names stay
    tactical_group.csv / .json       book step, tactical overlay

Writes are atomic (temp file, then rename). Every save may carry the "version"
(mtime and size fingerprint) of the files it rewrites, as /api/p/<name> served
it; a save whose version no longer matches the disk FAILs, so a hand edit or a
second tab is never silently overwritten. Saving the book applies the auto-NO
rule: a group with no investable name left after tactical claims is rated NO,
and so is a tactical group that claims nothing.

Live preview posts the unsaved book, overlay and constraints to /preview, which
runs target.compute with them as overrides: the same solver, nothing written.

The Data tab lists every drop in data/fiinpro/ (stock and index alike, see
ingest.py) and, per benchmark in index_prices, its sessions, range, and the
stock sessions it lacks. Rebuild database runs ingest.main, which loads both.

Freshness (common.newer_than) is shown, never acted on by itself: /api/state
lists the built anchors whose params or baseline are older than an input
(market.db, group_map_live.csv, fol.csv), the tickers on the latest session
the group map lacks, and per portfolio whether its input/ grid differs from
its anchor's baseline grid (re-fork needed, with the ratings a re-fork would
lose). index/group_map_live.csv itself stays hand-edited.

Backtest tab: POST /api/p/<name>/backtest runs backtest_engine.run with the
page's start, benchmark and unsaved trading config (nothing written) and
returns curves, statistics per benchmark, the rebalance log and end holdings.
The page switches benchmark and risk-free rate on that result; portfolio,
start, costs and lag need a new run. PUT /backtest_config saves
backtest_config.json (validate_backtest_config, version-checked).

Book spec, as the page sends it:

    {"groups":   {"<group>": {"rating": "OW", "mult": 1.5 | null,
                              "investable": ["TCB", ...]}},
     "tactical": {"on": true, "groups": [{"name": "SOE Divestment",
                  "rating": "OW", "mult": 2 | null, "members": ["GAS"]}]}}

mult null means the rating default. Tickers must be in their baseline column
(tactical: anywhere in the universe), never invalidated, and a stock sits in at
most one tactical group.

Bound to 127.0.0.1 only. Mutations need a JSON body and a local Host header,
so a web page elsewhere cannot drive the app.

Usage
    .venv\\Scripts\\python.exe scr\\app.py            http://127.0.0.1:5000
"""
import contextlib
import csv
import io
import json
import os
import re
import sys
import threading
from datetime import date
from pathlib import Path

import backtest_engine
import baseline
import duckdb
import ingest
import pandas as pd
import params as params_mod
import target
from common import (
    ALLOC,
    BOOK,
    BT_CONFIG,
    CONSTRAINTS,
    DB,
    FORKED,
    GRID,
    GROUP_MAP,
    PARAMS,
    PORTFOLIO,
    STATEMENT,
    TAC,
    TAC_SWITCH,
    BookError,
    default_backtest_config,
    default_constraints,
    grid_column,
    load_backtest_config,
    load_constraints,
    load_statement,
    newer_than,
    portfolio_anchor,
    read_grid,
    read_invalid,
    read_switch,
    validate_backtest_config,
    validate_constraints,
    validate_statement,
    write_grid,
)
from flask import Flask, abort, jsonify, render_template, request

import portfolio as pf

HERE = Path(__file__).resolve().parent
RATINGS = tuple(target.DEFAULT_MULT)
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LOCAL = {"127.0.0.1", "localhost"}

app = Flask(__name__, template_folder=str(HERE / "templates"),
            static_folder=str(HERE / "static"))
app.config.update(PORTFOLIO=PORTFOLIO, PARAMS=PARAMS, DB=DB)
LOCK = threading.RLock()      # one action at a time: stdout capture and file swaps


# ---------- file helpers ----------

def atomic_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def atomic_grid(path: Path, header: list[list[str]], cols: list[list[str]]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    write_grid(tmp, header, cols)
    os.replace(tmp, path)


def dump(obj) -> str:
    return json.dumps(obj, indent=2) + "\n"


def mult_cell(rating: str, mult) -> str:
    """Row-0 cell: the rating when there is no override, else the number."""
    if rating == "NO" or mult is None:
        return rating
    if isinstance(mult, bool) or not isinstance(mult, (int, float)) or mult < 0:
        raise BookError(f"multiplier {mult!r} must be a non-negative number or blank")
    return f"{float(mult):g}"


def cell_mult(cell: str):
    return None if cell in target.DEFAULT_MULT else float(cell)


def tactical_grid(spec: dict, universe: set, invalid: set) -> tuple[list, list, dict]:
    """Tactical spec -> (header, columns, claim). Empty groups are rated NO."""
    names, row0, row1, cols, claim = [], [], [], [], {}
    for tg in spec.get("groups", []):
        name = str(tg.get("name", "")).strip()
        if not name:
            raise BookError("tactical group with a blank name")
        if name in names:
            raise BookError(f"duplicate tactical group {name!r}")
        members = list(dict.fromkeys(tg.get("members", [])))
        for t in members:
            if t not in universe:
                raise BookError(f"{name}: {t} is in no baseline group")
            if t in invalid:
                raise BookError(f"{name}: {t} is invalidated and cannot be claimed")
            if t in claim:
                raise BookError(f"{t} is claimed by both {claim[t]} and {name}")
            claim[t] = name
        rating = tg.get("rating", "AV") if members else "NO"
        if rating not in RATINGS:
            raise BookError(f"{name}: unknown rating {rating!r}")
        names.append(name)
        row0.append(mult_cell(rating, tg.get("mult")))
        row1.append(rating)
        cols.append(members)
    return [row0, row1, names], cols, claim


def book_grid(base: list[list[str]], spec: dict, invalid: set,
              claim: dict) -> tuple[list, list]:
    """Book spec -> (header, columns) in baseline column order, auto-NO applied."""
    groups, specs = base[2], spec.get("groups", {})
    stray = sorted(set(specs) - set(groups))
    if stray:
        raise BookError(f"unknown group(s) in the book: {stray}")
    row0, row1, cols = [], [], []
    for j, g in enumerate(groups):
        col = grid_column(base, j)
        s = specs.get(g, {"rating": "AV", "mult": None, "investable": col})
        keep = set(s.get("investable", []))
        alien = sorted(keep - set(col))
        if alien:
            raise BookError(f"{g}: {alien} not in the baseline column")
        bad = sorted(keep & invalid)
        if bad:
            raise BookError(f"{g}: {bad} are invalidated and cannot be investable")
        names = [t for t in col if t in keep]
        rating = s.get("rating", "AV")
        if rating not in RATINGS:
            raise BookError(f"{g}: unknown rating {rating!r}")
        if not [t for t in names if t not in claim]:
            rating = "NO"
        row0.append(mult_cell(rating, s.get("mult")))
        row1.append(rating)
        cols.append(names)
    return [row0, row1, list(groups)], cols


def grids_from_spec(home: Path, spec: dict) -> tuple[list, dict]:
    """Validate a book spec against the portfolio -> (book grid, tactical override)."""
    base = read_grid(home / "input" / GRID)
    universe = {t for j in range(len(base[2])) for t in grid_column(base, j)}
    invalid = set(read_invalid(home))
    tac = spec.get("tactical") or {"on": False, "groups": []}
    t_head, t_cols, claim = tactical_grid(tac, universe, invalid)
    on = bool(tac.get("on"))
    header, cols = book_grid(base, spec, invalid, claim if on else {})
    depth = max((len(c) for c in cols), default=0)
    book = header + [[c[i] if i < len(c) else "" for c in cols] for i in range(depth)]
    t_depth = max((len(c) for c in t_cols), default=0)
    t_grid = (t_head + [[c[i] if i < len(c) else "" for c in t_cols]
                        for i in range(t_depth)]) if t_head[2] else None
    return book, {"on": on, "grid": t_grid, "_parts": (header, cols, t_head, t_cols)}


VERSIONED = {"statement": (STATEMENT,), "constraints": (CONSTRAINTS,),
             "book": (BOOK, TAC, TAC_SWITCH), "backtest": (BT_CONFIG,)}


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


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ---------- views of the files ----------

def sessions() -> list[str]:
    db = app.config["DB"]
    if db is None or not db.exists():
        return []
    con = duckdb.connect(db, read_only=True)
    try:
        return [str(r[0]) for r in con.execute(
            "SELECT DISTINCT trade_date FROM prices ORDER BY 1").fetchall()]
    finally:
        con.close()


def market() -> dict:
    db = app.config["DB"]
    drops = sorted(p for p in ingest.DROP.iterdir() if p.is_file()
                   and p.suffix.lower() in (".xlsx", ".xls", ".csv")
                   and not p.name.startswith("~$")) if ingest.DROP.exists() else []
    have = db is not None and db.exists()
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
        cols = {r[0] for r in con.execute("DESCRIBE loads").fetchall()}
        kind = "kind" if "kind" in cols else "'stock'"  # database built before index drops
        loads = con.execute(
            "SELECT file_name, row_count, dropped, date_min, date_max, ticker_count, "
            f"loaded_at, {kind} FROM loads ORDER BY loaded_at").fetchall()
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        bench = con.execute(
            "SELECT i.code, count(*), min(i.trade_date), max(i.trade_date), "
            "(SELECT count(*) FROM (SELECT DISTINCT trade_date FROM prices) s "
            " WHERE s.trade_date NOT IN (SELECT trade_date FROM index_prices j "
            "                            WHERE j.code = i.code)) "
            "FROM index_prices i GROUP BY 1 ORDER BY 1").fetchall() \
            if "index_prices" in tables else []
    finally:
        con.close()
    out.update(rows=rows, tickers=tickers, loads=[
        {"file": r[0], "rows": r[1], "dropped": r[2], "from": str(r[3]),
         "to": str(r[4]), "tickers": r[5], "loaded_at": str(r[6])[:16], "kind": r[7]}
        for r in loads],
        benchmarks=[{"code": c, "sessions": n, "from": str(a), "to": str(b), "missing": m}
                    for c, n, a, b, m in bench],
        rebuilt=stamp(db))
    return out


def built_anchors() -> list[str]:
    root = app.config["PORTFOLIO"] / "baseline"
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and DATE.match(p.name) and (p / ALLOC).exists()) \
        if root.exists() else []


def stamp(path: Path) -> str | None:
    if not path.exists():
        return None
    return (pd.Timestamp(path.stat().st_mtime, unit="s", tz="UTC")
            .tz_convert(None).strftime("%Y-%m-%d %H:%M UTC"))


def freshness() -> dict:
    """Built anchor -> names of the inputs newer than its params or baseline."""
    db, pdir = app.config["DB"], app.config["PARAMS"]
    root = app.config["PORTFOLIO"] / "baseline"
    out = {}
    for a in built_anchors():
        pp = pdir / f"{a}.csv"
        why = [p.name for p in newer_than(pp, db, GROUP_MAP, params_mod.FOL)]
        if newer_than(root / a / ALLOC, pp):
            why.append(pp.name)
        if why:
            out[a] = why
    return out


def unmapped() -> list[str]:
    """Tickers on the latest session that group_map_live.csv lacks or leaves blank."""
    db = app.config["DB"]
    if db is None or not db.exists() or not GROUP_MAP.exists():
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


def refork(home: Path, anchor: str) -> dict | None:
    """Does the anchor's baseline grid differ from input/? If so, what a re-fork
    carries and loses (portfolio.carry's report). None when either grid is missing."""
    src = app.config["PORTFOLIO"] / "baseline" / anchor / GRID
    inp = home / "input" / GRID
    if not src.exists() or not inp.exists():
        return None
    base, old_input = read_grid(src), read_grid(inp)
    if base == old_input:
        return {"needed": False}
    old_book = read_grid(home / BOOK) if (home / BOOK).exists() else None
    _, _, rep = pf.carry(base, old_book, old_input)
    return {"needed": True, "lost": rep["lost"], "appeared": rep["appeared"]}


def universe(anchor: str) -> dict:
    """Groups of one anchor's baseline with float cap (bn VND), turnover, names."""
    root = app.config["PORTFOLIO"] / "baseline" / anchor
    pp = app.config["PARAMS"] / f"{anchor}.csv"
    if not (root / GRID).exists() or not pp.exists():
        raise BookError(f"no baseline for {anchor}; fork onto it to build one")
    grid = read_grid(root / GRID)
    alloc = pd.read_csv(root / ALLOC).set_index("group")
    par = pd.read_csv(pp).set_index("ticker")
    groups, names = [], {}
    for j, g in enumerate(grid[2]):
        members = []
        for t in grid_column(grid, j):
            to = par.at[t, "turnover_21_pct"]
            members.append({"t": t, "fcap": float(par.at[t, "float_cap"]) / 1e9,
                            "to": None if pd.isna(to) else float(to)})
            names[t] = str(par.at[t, "company_name"])
        groups.append({"name": g, "weight": float(alloc.at[g, "weight"]),
                       "members": members})
    return {"anchor": anchor, "groups": groups, "co": names}


def preview_json(res: dict) -> dict:
    sec, hold = res["allocation"], res["holdings"]
    rows = [{"group": r.group, "kind": r.kind, "rating": r.rating,
             "m": float(r.multiplier), "base": float(r.baseline_weight),
             "unc": float(r.uncapped_weight), "w": float(r.weight),
             "migrated": float(r.migrated_out), "capped": bool(r.capped),
             "full": bool(r.full), "n": int(r.n_members),
             "n_all": len(res["full"][r.group])} for r in sec.itertuples()]
    holdings = [{"t": r.ticker, "group": r.sector, "home": r.home_sector,
                 "w": float(r.target_weight), "fcap": float(r.fcap) / 1e9,
                 "pin": r.pin} for r in hold.itertuples()]
    return {"ok": True, "rows": rows, "holdings": holdings, "n": len(holdings),
            "in_range": res["in_range"], "range_note": res["range_note"],
            "cap_note": res["cap_note"], "tac_note": res["tac_note"],
            "report": res["report"], "messages": res["messages"]}


def num(x):
    """JSON-safe float: NaN and None become null."""
    return None if x is None or pd.isna(x) else float(x)


def stats_json(s: dict) -> dict:
    return {k: ({kk: num(vv) for kk, vv in v.items()} if isinstance(v, dict) else num(v))
            for k, v in s.items()}


def backtest_json(res: dict) -> dict:
    eq = res["equity"]
    return {
        "ok": True, "name": res["name"], "anchor": str(res["anchor"]),
        "start": str(res["start"]), "end": str(res["end"]), "benchmark": res["benchmark"],
        "config": res["config"], "rebalance": res["rebalance"],
        "holdings_range": res["holdings_range"], "constraints": res["constraints"],
        "tactical": res["tactical"], "messages": res["messages"],
        "dates": [str(d) for d in eq["date"]],
        "portfolio": [float(v) for v in eq["portfolio"]],
        "benchmarks": {c: {"equity": [float(v) for v in b["equity"]], "stats": stats_json(b["stats"])}
                       for c, b in res["benchmarks"].items()},
        "rebalances": [{"decision": str(r.decision), "fill": str(r.fill), "trigger": r.trigger,
                        "drift": num(r.group_drift), "turnover": float(r.turnover),
                        "cost": float(r.cost), "holdings": int(r.holdings),
                        "in_range": bool(r.in_range), "gone": r.gone}
                       for r in res["rebalances"].itertuples()],
        "holdings_end": [{"t": r.ticker, "group": r.group, "w": float(r.weight),
                          "target": float(r.target)} for r in res["holdings_end"].itertuples()],
    }


def summary(name: str) -> dict:
    home = app.config["PORTFOLIO"] / name
    out = {"name": name, "statement": None, "anchor": None, "error": None,
           "constraints_on": [], "refork": None,
           "built_at": read_kv(home / "target" / "built_from.txt").get("built_at")}
    try:
        out["statement"] = load_statement(home)
        out["constraints_on"] = [k for k, v in load_constraints(home).items() if v["on"]]
        out["anchor"] = portfolio_anchor(home)
        out["refork"] = refork(home, out["anchor"])
        p = preview_json(target.compute(name, app.config["PORTFOLIO"],
                                        app.config["PARAMS"]))
        out.update(n=p["n"], top=p["rows"][0] if p["rows"] else None)
    except (BookError, ValueError, OSError) as e:
        out["error"] = str(e)
    return out


def portfolio_names() -> list[str]:
    root = app.config["PORTFOLIO"]
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and p.name != "baseline" and pf.NAME.match(p.name))


def detail(name: str) -> dict:
    home = app.config["PORTFOLIO"] / name
    out = {"name": name, "errors": [], "statement": None,
           "constraints": default_constraints(), "anchor": None, "forked": {},
           "universe": None, "book": None, "tactical": {"on": False, "groups": []},
           "refork": None, "versions": {k: version(home, k) for k in VERSIONED},
           "invalid": read_rows(home / "screen" / pf.INVALID),
           "exclusions": read_rows(home / "screen" / "exclusions.csv")}
    for key, fn in (("statement", load_statement), ("constraints", load_constraints)):
        try:
            v = fn(home)
            if v is not None:
                out[key] = v
        except BookError as e:
            out["errors"].append(str(e))
    try:
        out["anchor"] = portfolio_anchor(home)
    except BookError:
        return out
    out["forked"] = read_kv(home / "input" / FORKED)
    try:
        out["universe"] = universe(out["anchor"])
        out["refork"] = refork(home, out["anchor"])
    except BookError as e:
        out["errors"].append(str(e))
    if (home / BOOK).exists():
        book = read_grid(home / BOOK)
        out["book"] = {}
        try:
            for j, g in enumerate(book[2]):
                out["book"][g] = {"rating": book[1][j], "mult": cell_mult(book[0][j]),
                                  "investable": grid_column(book, j)}
        except (IndexError, ValueError) as e:
            out["errors"].append(f"{BOOK} is malformed: {e}")
    try:
        on, _ = read_switch(home / TAC_SWITCH, "tactical_group")
        out["tactical"]["on"] = on
        if (home / TAC).exists():
            g = read_grid(home / TAC)
            out["tactical"]["groups"] = [
                {"name": n, "rating": g[1][j], "mult": cell_mult(g[0][j]),
                 "members": grid_column(g, j)} for j, n in enumerate(g[2])] \
                if len(g) >= 3 else []
    except (BookError, IndexError, ValueError) as e:
        out["errors"].append(f"{TAC}: {e}")
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
    if name == "baseline" or not pf.NAME.match(name):
        abort(404)
    home = app.config["PORTFOLIO"] / name
    if not home.is_dir():
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


# ---------- routes ----------

@app.get("/")
def index():
    return render_template("desk.html")


@app.get("/api/state")
def state():
    with LOCK:
        ss = sessions()
        root = app.config["PORTFOLIO"] / "baseline"
        try:
            sticky = baseline.sticky_anchor(root)
        except BookError:
            sticky = None
        base = None
        if sticky:
            try:
                base = universe(sticky)
            except BookError:
                pass
        return jsonify(market=market(), sessions=ss, eligible=ss[params_mod.WINDOW - 1:],
                       sticky=sticky, anchors=built_anchors(), baseline=base,
                       stale=freshness(), unmapped=unmapped(),
                       group_map={"edited": stamp(GROUP_MAP)},
                       portfolios=[summary(n) for n in portfolio_names()])


@app.post("/api/run/ingest")
def run_ingest():
    ok, log, _ = run(ingest.main, [])
    return reply(ok, log)


@app.post("/api/run/baseline")
def run_baseline():
    d = body().get("date")
    if d is not None and not (isinstance(d, str) and DATE.match(d)):
        return reply(False, f"FAIL  date {d!r} is not YYYY-MM-DD\n")
    argv = ["--date", d] if d else []
    ok, log, _ = run(params_mod.main, argv)
    if ok:
        ok, more, _ = run(baseline.main, argv)
        log += more
    return reply(ok, log)


@app.get("/api/p/<name>")
def get_portfolio(name):
    home_of(name)
    with LOCK:
        return jsonify(detail(name))


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


@app.put("/api/p/<name>/statement")
def save_statement(name):
    home = home_of(name)

    b = body()

    def save(s):
        validate_statement(s)
        check_version(home, "statement", b.get("version"))
        atomic_text(home / STATEMENT, dump(s))
        print(f"OK    saved portfolio/{name}/{STATEMENT}")
    ok, log, _ = run(save, b.get("statement"))
    return reply(ok, log)


@app.put("/api/p/<name>/constraints")
def save_constraints(name):
    home = home_of(name)

    b = body()

    def save(c):
        validate_constraints(c)
        check_version(home, "constraints", b.get("version"))
        atomic_text(home / CONSTRAINTS, dump(c))
        print(f"OK    saved portfolio/{name}/{CONSTRAINTS}")
    ok, log, _ = run(save, b.get("constraints"))
    return reply(ok, log)


@app.put("/api/p/<name>/book")
def save_book(name):
    home = home_of(name)

    def save(spec):
        if not (home / "input" / GRID).exists():
            raise BookError(f"{name} is not forked yet")
        _, tac = grids_from_spec(home, spec)
        header, cols, t_head, t_cols = tac["_parts"]
        check_version(home, "book", spec.get("version"))
        atomic_grid(home / BOOK, header, cols)
        print(f"OK    saved portfolio/{name}/{BOOK}")
        flipped = [g for g, r in zip(header[2], header[1])
                   if r == "NO" and spec.get("groups", {}).get(g, {}).get("rating") != "NO"]
        for g in flipped:
            print(f"WARN  {g}: no investable name left, rated NO")
        if t_head[2]:
            atomic_grid(home / TAC, t_head, t_cols)
            print(f"OK    saved portfolio/{name}/{TAC}  {len(t_head[2])} group(s)")
        elif (home / TAC).exists():
            (home / TAC).unlink()
            print(f"OK    removed portfolio/{name}/{TAC}  (no tactical groups)")
        if tac["on"] or (home / TAC_SWITCH).exists():    # absent already means off
            atomic_text(home / TAC_SWITCH,
                        json.dumps({"tactical_group": "yes" if tac["on"] else "no"}) + "\n")
        print(f"      tactical overlay {'on' if tac['on'] else 'off'}")
    ok, log, _ = run(save, body())
    return reply(ok, log)


@app.post("/api/p/<name>/preview")
def preview(name):
    home = home_of(name)
    b = body()

    def calc():
        book = tac = None
        if "groups" in b:
            book, tac = grids_from_spec(home, b)
            tac = {"on": tac["on"], "grid": tac["grid"]}
        return preview_json(target.compute(name, app.config["PORTFOLIO"],
                                           app.config["PARAMS"], book=book,
                                           tactical=tac,
                                           constraints=b.get("constraints")))
    ok, log, res = run(calc)
    if not ok:
        return jsonify(ok=False, error=log.removeprefix("FAIL  ").strip())
    return jsonify(res)


@app.post("/api/p/<name>/fork")
def fork(name):
    home_of(name)
    anchor = body().get("anchor")
    if anchor is not None:
        try:
            anchor = date.fromisoformat(str(anchor)).isoformat()
        except ValueError:
            return reply(False, f"FAIL  anchor {anchor!r} is not a YYYY-MM-DD date\n")
    ok, log, rep = run(pf.fork, name, anchor, app.config["PORTFOLIO"],
                       db=app.config["DB"], params_dir=app.config["PARAMS"])
    return reply(ok, log, report=rep)


@app.post("/api/p/<name>/screen")
def screen(name):
    home = home_of(name)
    b = body()

    def go():
        if "screens" in b:
            s = load_statement(home)
            if s is None:
                raise BookError(f"no {STATEMENT} in {home}")
            s["screens"] = b["screens"]
            validate_statement(s)
            atomic_text(home / STATEMENT, dump(s))
            print(f"OK    saved screens in portfolio/{name}/{STATEMENT}")
        pf.screen(name, invalidate=b.get("invalidate") or None,
                  invalidate_all=bool(b.get("invalidate_all")),
                  restore=b.get("restore") or None,
                  portfolio=app.config["PORTFOLIO"], params_dir=app.config["PARAMS"])
    ok, log, _ = run(go)
    return reply(ok, log)


@app.post("/api/p/<name>/build")
def build(name):
    home_of(name)
    ok, log, _ = run(target.build, name, app.config["PORTFOLIO"], app.config["PARAMS"])
    return reply(ok, log)


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
    home_of(name)
    b = body()
    start, bench = b.get("start"), b.get("benchmark", "VNINDEX")
    if start is not None and not (isinstance(start, str) and DATE.match(start)):
        return jsonify(ok=False, error=f"start {start!r} is not YYYY-MM-DD")
    if not isinstance(bench, str):
        return jsonify(ok=False, error="benchmark must be an index code")

    def calc():
        return backtest_json(backtest_engine.run(
            name, start, bench, config=b.get("config"), portfolio=app.config["PORTFOLIO"],
            params_dir=app.config["PARAMS"], db=app.config["DB"] or DB))
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
    app.run(host="127.0.0.1", port=a.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
