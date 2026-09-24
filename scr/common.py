"""Shared paths, file grammars and the per-portfolio contracts.

Everything more than one builder reads lives here, so a grammar is defined once:
the market's sessions, statement.json, constraints.json, screens.json,
backtest_config.json, book.json and the decision log.

Sessions (D57): every calculation takes a date. sessions(db) lists the trading
days in market.db; snap(db, d) is the last session on or before d, the session
a date is priced on. A date before the first session FAILs.

statement.json -- one per portfolio, hand-edited (or written by the UI):

    {
      "approach": "free text, discretionary reference only",
      "scope":    "free text, discretionary reference only",
      "holdings": {"min": 20, "max": 30},
      "rebalance": {"frequency": "1Q", "drift_threshold": 0.08,
                    "breach_tolerance": 0.10}
    }

    holdings   integers, 1 <= min <= max. target.py WARNs outside the range.
    rebalance  frequency is "2W" | "1M" | "1Q" | null; drift_threshold is a
               FRACTION (0.08 = 8%) or null; at least one must be set. Drift is
               half the sum of absolute weight gaps at group grain, measured
               against the standing target (derived at inception and on each
               calendar date, held between them).
               breach_tolerance is a FRACTION or null: how far a constraints.json
               cap may be broken before a rebalance is forced (0.10 = 10% of the
               limit, so a 20% stock cap trips at 22%); null switches breach off
               even with caps on. A missing key means 0.10. A breach fill clips
               the broken cap to its limit; it does not reset the book.
               Consumed by backtest_engine.py.
    A "screens" block FAILs: screen thresholds live in screens.json (D62).

constraints.json -- one per portfolio, hand-edited (or written by the UI). A
missing file means every cap is off and the default active budget. Cap values
are FRACTIONS of the book; the active budget is in PERCENTAGE POINTS.

    {
      "active": {"budget_pp": 20},
      "sector": {"on": false, "max": 0.25, "per_group": {"Banks - Private": 0.30}},
      "stock":  {"on": false, "max": 0.10},
      "large":  {"on": false, "threshold": 0.05, "aggregate": 0.40}
    }

    active  always on: half the sum of |active pp| over groups may not exceed
            budget_pp, in (0, 100]. Missing block = 20.
    sector  cap on every live group's weight; per_group overrides max for the
            named groups, tactical groups included. Universal = empty per_group.
    stock   cap on every holding's weight.
    large   UCITS style: holdings above threshold may sum to at most aggregate.
            threshold <= aggregate.
    The tilt and the solver live in target.py; read its docstring.

screens.json -- one per portfolio, written by the desk's Monitor step (or by
hand). Screens are FLAGS, never exclusions (D62): they mark a risk on a name,
and excluding a name is unticking it in the book. A missing file means every
screen off.

    {"turnover":  {"on": false, "min_pct": 0.10},
     "float_cap": {"on": false, "min_bn_vnd": 1000},
     "fol":       {"on": false, "min_limit_pct": 30}}

    turnover   average daily turnover over 21 sessions as % of float cap
               (params.turnover_21_pct); fewer than 21 sessions flags "no data"
    float_cap  params.float_cap in billions of VND
    fol        the foreign ownership limit from index/fol.csv, in percent;
               a name with no row flags "no data" rather than passing
    A flag is {"t", "screen", "value", "threshold", "why": "below" | "no data"}.
    Thresholds are not part of the setup hash: they change no weight.

    Position screens (D70), on the same file, edited on the Replication
    section: they depend on the AUM, so only a replication ticket computes them
    (portfolio.position_flags, why "above" | "no data"). A missing block takes
    its default, on:

    {"ownership": {"on": true, "max_pct_of_shares": 5},
     "float":     {"on": true, "max_pct_of_float": 15},
     "liquidity": {"on": true, "participation_pct": 50, "max_days": 20}}

    ownership  shares held / outstanding_shares, percent (5% is the major
               shareholder disclosure threshold)
    float      shares held / free_float, percent
    liquidity  sessions to trade the position at participation_pct of the
               21-session average daily value (params.adv_21, VND);
               participation_pct in (0, 100]

LOT = 100 shares, the board lot every replication ticket rounds to (D69).

backtest_config.json -- one per portfolio, written by the desk's Backtest tab
(or by hand). Trading assumptions for scr/backtest_engine.py only; the
rebalance mandate stays in statement.json. A missing file means the defaults.

    {"brokerage_bps": 10, "sell_tax_bps": 10, "lag_sessions": 1,
     "risk_free_rate": 0.06}

    brokerage_bps   per side, >= 0          sell_tax_bps  on sells, >= 0
    lag_sessions    integer 0-5, pinned to 1 (D67: every rebalance fills at the
                    next session's OPEN). Another value is a backtest-only
                    what-if (0 = same close) that the engine WARNs about; the
                    desk does not offer it, Monitor and Replication ignore it
    risk_free_rate  FRACTION a year in [0, 1), used for Sharpe only

book.json -- one per portfolio, the working allocation (D58), written by the
desk's Allocation step (portfolio.write_book). The shape is the book spec
(app.py docstring): {"groups": {...}, "tactical": {...}}. It is the draft of
the next decision; a missing file means every group AV with all its names.

decisions/ -- one per portfolio, written by the desk's Record button (or
portfolio.record_decision). A log of allocation profiles, at most one per
effective date; recording on a date that already has one replaces it.

    decisions/log.csv      id, effective, recorded_at, priced_as_of, kind,
                           setup_hash, note
    decisions/<id>.json    {"id": "2026-09-21", "effective": "2026-09-21",
                            "recorded_at": "2026-09-21 14:02",
                            "priced_as_of": "2026-09-19", "kind": "period",
                            "setup_hash": "3f2a...", "note": "...",
                            "groups": {...}, "tactical": {...},
                            "holdings": [{"t", "group", "w"}, ...],
                            "flags": [{"t", "screen", "value", "threshold", "why"}]}
    decisions/archive/<stamp>/   earlier sets, moved there by portfolio.reset

    id         the effective date
    kind       inception | period | active (D60): the first decision is
               inception; a file without kind reads as inception if it is the
               earliest, else period
    priced_as_of  the session the holdings were sized on (snap(effective))
    setup_hash    setup_hash() when recorded (D61)
    groups, tactical   the book spec as saved; holdings and flags as built
    load_decisions returns the profiles sorted by (effective, id), [] without
    the folder.
"""
import csv
import hashlib
import json
import math
import os
from bisect import bisect_right
from datetime import date
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB = DATA / "market.db"
PARAMS = DATA / "params"
INDEX = ROOT / "index"
GROUP_MAP = INDEX / "group_map_live.csv"
PORTFOLIO = ROOT / "portfolio"

ALLOC = "sector_allocation.csv"    # under portfolio/<name>/target/
BOOK = "book.json"
STATEMENT = "statement.json"
CONSTRAINTS = "constraints.json"
SCREENS_FILE = "screens.json"
BT_CONFIG = "backtest_config.json"
DECISIONS = "decisions"            # under portfolio/<name>/
DECISION_LOG = "log.csv"           # under portfolio/<name>/decisions/
ARCHIVE = "archive"                # under portfolio/<name>/decisions/
DECISION_COLS = ["id", "effective", "recorded_at", "priced_as_of", "kind",
                 "setup_hash", "note"]

RATINGS = ("NO", "UW3", "UW2", "UW1", "AV", "OW1", "OW2", "OW3")
KINDS = ("inception", "period", "active")

FREQUENCIES = ("2W", "1M", "1Q")
BREACH_TOL = 0.10                  # statement.json rebalance.breach_tolerance default
SCREENS = {"turnover": "min_pct", "float_cap": "min_bn_vnd", "fol": "min_limit_pct"}
POSITION_SCREENS = {"ownership": ("max_pct_of_shares",), "float": ("max_pct_of_float",),
                    "liquidity": ("participation_pct", "max_days")}     # D70, AUM-dependent
LOT = 100                          # board lot, shares (D69)
CAPS = ("sector", "stock", "large")    # the constraints.json blocks with an on switch


class BookError(Exception):
    """A fatal, already-worded message for the person editing a portfolio.

    Scripts print it as `FAIL  <message>`; embed "\\n      " for a hint line.
    """


# ---------- sessions (D57) ----------

_SESSIONS: dict = {}


def sessions(db: Path = DB) -> list[date]:
    """Sorted trading sessions in market.db (cached by the file's mtime and size)."""
    db = Path(db)
    if not db.exists():
        raise BookError(f"missing {db}\n      run scr/ingest.py (Rebuild database)")
    st = db.stat()
    key = (str(db), st.st_mtime_ns, st.st_size)
    if key not in _SESSIONS:
        con = duckdb.connect(str(db), read_only=True)
        try:
            _SESSIONS.clear()
            _SESSIONS[key] = [r[0] for r in con.execute(
                "SELECT DISTINCT trade_date FROM prices ORDER BY 1").fetchall()]
        finally:
            con.close()
    return _SESSIONS[key]


def as_date(d, what: str = "date") -> date:
    """An ISO date string or date -> date; BookError naming `what` otherwise."""
    if isinstance(d, date):
        return date(d.year, d.month, d.day)
    try:
        return date.fromisoformat(str(d))
    except ValueError:
        raise BookError(f"{what} {d!r} is not a YYYY-MM-DD date")


def snap(db: Path, d=None) -> date:
    """The last session on or before d (default: the latest session)."""
    ss = sessions(db)
    if not ss:
        raise BookError(f"{Path(db).name} has no sessions")
    if d is None:
        return ss[-1]
    want = as_date(d)
    i = bisect_right(ss, want)
    if i == 0:
        raise BookError(f"{want} is before the first session {ss[0]} in {Path(db).name}")
    return ss[i - 1]


# ---------- JSON helpers ----------

def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise BookError(f"{p.name} is not readable JSON: {e}")


def atomic_text(path: Path, text: str) -> None:
    """Write via a temp file and a rename, so a reader never sees half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def dump(obj) -> str:
    return json.dumps(obj, indent=2) + "\n"


# ---------- statement.json ----------

def default_statement() -> dict:
    return {
        "approach": "",
        "scope": "",
        "holdings": {"min": 20, "max": 30},
        "rebalance": {"frequency": "1Q", "drift_threshold": None,
                      "breach_tolerance": BREACH_TOL},
    }


def validate_statement(s: dict) -> dict:
    """Return the statement if valid; raise BookError naming the first fault."""
    where = STATEMENT
    if not isinstance(s, dict):
        raise BookError(f"{where}: expected a JSON object")
    if "screens" in s:
        raise BookError(f"{where}: screens moved to {SCREENS_FILE} (D62); "
                        "delete the block")
    for k in ("approach", "scope"):
        if not isinstance(s.get(k, ""), str):
            raise BookError(f"{where}: {k} must be text")

    h = s.get("holdings")
    if not isinstance(h, dict):
        raise BookError(f"{where}: holdings must be {{\"min\": .., \"max\": ..}}")
    lo, hi = h.get("min"), h.get("max")
    if not (isinstance(lo, int) and isinstance(hi, int)
            and not isinstance(lo, bool) and 1 <= lo <= hi):
        raise BookError(f"{where}: holdings min/max must be integers with "
                        f"1 <= min <= max, got {lo!r}, {hi!r}")

    r = s.get("rebalance")
    if not isinstance(r, dict):
        raise BookError(f"{where}: rebalance must be an object")
    freq, drift = r.get("frequency"), r.get("drift_threshold")
    if freq is not None and freq not in FREQUENCIES:
        raise BookError(f"{where}: rebalance.frequency {freq!r}, want one of "
                        f"{list(FREQUENCIES)} or null")
    if drift is not None and (isinstance(drift, bool)
                              or not isinstance(drift, (int, float))
                              or not 0 < drift < 1):
        raise BookError(f"{where}: rebalance.drift_threshold {drift!r} must "
                        "be a fraction in (0, 1), e.g. 0.08 for 8%")
    if freq is None and drift is None:
        raise BookError(f"{where}: rebalance needs a frequency, a "
                        "drift_threshold, or both")
    tol = r.get("breach_tolerance", BREACH_TOL)
    if tol is not None and (isinstance(tol, bool)
                            or not isinstance(tol, (int, float))
                            or not 0 < tol < 1):
        raise BookError(f"{where}: rebalance.breach_tolerance {tol!r} must be a "
                        "fraction in (0, 1), e.g. 0.10 for 10% of the cap, or "
                        "null to switch breach off")
    return s


def load_statement(home: Path) -> dict | None:
    """The portfolio's statement, validated; None if the file is absent."""
    p = home / STATEMENT
    if not p.exists():
        return None
    return validate_statement(_read_json(p))


# ---------- screens.json (D62) ----------

def default_screens() -> dict:
    return {"turnover": {"on": False, "min_pct": 0.10},
            "float_cap": {"on": False, "min_bn_vnd": 1000},
            "fol": {"on": False, "min_limit_pct": 30},
            "ownership": {"on": True, "max_pct_of_shares": 5},
            "float": {"on": True, "max_pct_of_float": 15},
            "liquidity": {"on": True, "participation_pct": 50, "max_days": 20}}


def validate_screens(sc: dict) -> dict:
    """Return screens with every screen present (a missing one takes its
    default); raise BookError on a fault."""
    where = SCREENS_FILE
    if not isinstance(sc, dict):
        raise BookError(f"{where}: expected a JSON object")
    keys = {**{n: (k,) for n, k in SCREENS.items()}, **POSITION_SCREENS}
    unknown = sorted(set(sc) - set(keys))
    if unknown:
        raise BookError(f"{where}: unknown screen(s) {unknown}; available {sorted(keys)}")
    base, out = default_screens(), {}
    for name, fields in keys.items():
        cfg = sc.get(name, base[name])
        if not isinstance(cfg, dict):
            raise BookError(f"{where}: {name} must be an object")
        extra = sorted(set(cfg) - {"on", *fields})
        if extra:
            raise BookError(f"{where}: {name} has unknown key(s) {extra}")
        if not isinstance(cfg.get("on", False), bool):
            raise BookError(f"{where}: {name}.on must be true or false")
        out[name] = {"on": bool(cfg.get("on", False))}
        for key in fields:
            v = cfg.get(key, base[name][key])
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not math.isfinite(v) or v < 0:
                raise BookError(f"{where}: {name}.{key} must be a non-negative number, "
                                f"got {v!r}")
            out[name][key] = v
    p = out["liquidity"]["participation_pct"]
    if not 0 < p <= 100:
        raise BookError(f"{where}: liquidity.participation_pct {p!r} must be in (0, 100]")
    return out


def load_screens(home: Path) -> dict:
    """The portfolio's screens, validated; all off if the file is absent."""
    p = home / SCREENS_FILE
    return validate_screens(_read_json(p) if p.exists() else default_screens())


# ---------- constraints.json ----------

def default_constraints() -> dict:
    return {
        "active": {"budget_pp": 20.0},
        "sector": {"on": False, "max": 0.25, "per_group": {}},
        "stock": {"on": False, "max": 0.10},
        "large": {"on": False, "threshold": 0.05, "aggregate": 0.40},
    }


def _fraction(v, where: str, upper_open: bool = False) -> float:
    ok = (not isinstance(v, bool) and isinstance(v, (int, float))
          and math.isfinite(v) and 0 < v and (v < 1 if upper_open else v <= 1))
    if not ok:
        rng = "(0, 1)" if upper_open else "(0, 1]"
        raise BookError(f"{CONSTRAINTS}: {where} {v!r} must be a fraction in "
                        f"{rng}, e.g. 0.25 for 25%")
    return float(v)


def validate_constraints(c: dict) -> dict:
    """Return constraints with every block present; raise BookError on a fault."""
    if not isinstance(c, dict):
        raise BookError(f"{CONSTRAINTS}: expected a JSON object")
    base = default_constraints()
    unknown = sorted(set(c) - set(base))
    if unknown:
        raise BookError(f"{CONSTRAINTS}: unknown block(s) {unknown}; "
                        f"available {sorted(base)}")
    act = c.get("active", base["active"])
    if not isinstance(act, dict):
        raise BookError(f"{CONSTRAINTS}: active must be an object")
    extra = sorted(set(act) - set(base["active"]))
    if extra:
        raise BookError(f"{CONSTRAINTS}: active has unknown key(s) {extra}")
    b = act.get("budget_pp", base["active"]["budget_pp"])
    if isinstance(b, bool) or not isinstance(b, (int, float)) or not (
            math.isfinite(b) and 0 < b <= 100):
        raise BookError(f"{CONSTRAINTS}: active.budget_pp {b!r} must be percentage "
                        "points in (0, 100], e.g. 20")
    out = {"active": {"budget_pp": float(b)}}
    for block in CAPS:
        dflt = base[block]
        cfg = c.get(block, {"on": False})
        if not isinstance(cfg, dict):
            raise BookError(f"{CONSTRAINTS}: {block} must be an object")
        extra = sorted(set(cfg) - set(dflt))
        if extra:
            raise BookError(f"{CONSTRAINTS}: {block} has unknown key(s) {extra}")
        on = cfg.get("on", False)
        if not isinstance(on, bool):
            raise BookError(f"{CONSTRAINTS}: {block}.on must be true or false")
        out[block] = {**dflt, **cfg, "on": on}
    s, st, lg = out["sector"], out["stock"], out["large"]
    if s["on"]:
        s["max"] = _fraction(s["max"], "sector.max")
        if not isinstance(s["per_group"], dict):
            raise BookError(f"{CONSTRAINTS}: sector.per_group must map group "
                            "name -> fraction")
        s["per_group"] = {g: _fraction(v, f"sector.per_group[{g!r}]")
                          for g, v in s["per_group"].items()}
    if st["on"]:
        st["max"] = _fraction(st["max"], "stock.max")
    if lg["on"]:
        lg["threshold"] = _fraction(lg["threshold"], "large.threshold", upper_open=True)
        lg["aggregate"] = _fraction(lg["aggregate"], "large.aggregate")
        if lg["threshold"] > lg["aggregate"]:
            raise BookError(f"{CONSTRAINTS}: large.threshold {lg['threshold']} "
                            f"exceeds large.aggregate {lg['aggregate']}")
    return out


def load_constraints(home: Path) -> dict:
    """The portfolio's constraints, validated; all off if the file is absent."""
    p = home / CONSTRAINTS
    return validate_constraints(_read_json(p) if p.exists() else default_constraints())


def setup_hash(home: Path) -> str:
    """12-hex fingerprint of the setup a decision is recorded under (D61): the
    statement and the constraints, both validated so a missing key and its
    default hash alike. Screens are not part of it."""
    st = load_statement(home)
    blob = json.dumps({"statement": st, "constraints": load_constraints(home)},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# ---------- backtest_config.json ----------

def default_backtest_config() -> dict:
    return {"brokerage_bps": 10.0, "sell_tax_bps": 10.0, "lag_sessions": 1,
            "risk_free_rate": 0.06}


def validate_backtest_config(c: dict) -> dict:
    """Return the config with every key present; raise BookError on a fault."""
    if not isinstance(c, dict):
        raise BookError(f"{BT_CONFIG}: expected a JSON object")
    base = default_backtest_config()
    unknown = sorted(set(c) - set(base))
    if unknown:
        raise BookError(f"{BT_CONFIG}: unknown key(s) {unknown}; available {sorted(base)}")
    out = {**base, **c}

    def number(v):
        return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)

    for k in ("brokerage_bps", "sell_tax_bps"):
        if not number(out[k]) or out[k] < 0:
            raise BookError(f"{BT_CONFIG}: {k} {out[k]!r} must be a number >= 0 (bps)")
        out[k] = float(out[k])
    lag = out["lag_sessions"]
    if isinstance(lag, bool) or not isinstance(lag, int) or not 0 <= lag <= 5:
        raise BookError(f"{BT_CONFIG}: lag_sessions {lag!r} must be an integer 0-5")
    rf = out["risk_free_rate"]
    if not number(rf) or not 0 <= rf < 1:
        raise BookError(f"{BT_CONFIG}: risk_free_rate {rf!r} must be a fraction in "
                        "[0, 1), e.g. 0.06 for 6%")
    out["risk_free_rate"] = float(rf)
    return out


def load_backtest_config(home: Path) -> dict:
    """The portfolio's backtest config, validated; defaults if the file is absent."""
    p = home / BT_CONFIG
    return validate_backtest_config(_read_json(p) if p.exists() else {})


# ---------- decisions/ ----------

def _iso(v, where: str, what: str) -> None:
    try:
        date.fromisoformat(str(v))
    except ValueError:
        raise BookError(f"{where}: {what} {v!r} is not YYYY-MM-DD")


def validate_decision(d: dict) -> dict:
    """Return a decision profile if valid; raise BookError naming the first fault."""
    if not isinstance(d, dict):
        raise BookError("decision: expected a JSON object")
    did = d.get("id")
    where = f"decision {did!r}"
    if not isinstance(did, str) or not did:
        raise BookError("decision: id must be non-empty text")
    _iso(d.get("effective"), where, "effective")
    if d.get("priced_as_of") is not None:
        _iso(d["priced_as_of"], where, "priced_as_of")
    if d.get("kind") is not None and d["kind"] not in KINDS:
        raise BookError(f"{where}: kind {d['kind']!r}, want one of {list(KINDS)}")
    if d.get("setup_hash") is not None and not isinstance(d["setup_hash"], str):
        raise BookError(f"{where}: setup_hash must be text")
    groups = d.get("groups")
    if not isinstance(groups, dict):
        raise BookError(f"{where}: groups must map group name -> spec")
    tac = d.get("tactical") or {"on": False, "groups": []}
    if not isinstance(tac, dict) or not isinstance(tac.get("groups", []), list):
        raise BookError(f"{where}: tactical must be {{\"on\": .., \"groups\": [..]}}")
    for g, s in list(groups.items()) + [(t.get("name"), t) for t in tac.get("groups", [])]:
        if not isinstance(s, dict):
            raise BookError(f"{where}: {g}: expected an object")
        if s.get("rating", "AV") not in RATINGS:
            raise BookError(f"{where}: {g}: unknown rating {s.get('rating')!r}")
        pp = s.get("pp")
        if pp is not None and (isinstance(pp, bool) or not isinstance(pp, (int, float))
                               or not math.isfinite(pp)):
            raise BookError(f"{where}: {g}: active pp {pp!r} must be a number")
    for key, need in (("holdings", ("t", "group", "w")), ("flags", ("t", "screen"))):
        rows = d.get(key)
        if rows is None:
            continue
        if not isinstance(rows, list) or not all(
                isinstance(r, dict) and all(k in r for k in need) for r in rows):
            raise BookError(f"{where}: {key} must be a list of {{{', '.join(need)}, ..}}")
    return d


def load_decisions(home: Path) -> list[dict]:
    """The portfolio's decision profiles sorted by (effective, id); [] if none.
    A profile without a kind reads as inception when it is the earliest, else
    period (files recorded before D60)."""
    log = home / DECISIONS / DECISION_LOG
    if not log.exists():
        return []
    with open(log, encoding="utf-8-sig", newline="") as f:
        ids = [r["id"].strip() for r in csv.DictReader(f) if r.get("id", "").strip()]
    out = []
    for did in ids:
        p = home / DECISIONS / f"{did}.json"
        if not p.exists():
            raise BookError(f"{DECISIONS}/{DECISION_LOG} lists {did} but {p.name} is missing")
        out.append(validate_decision(_read_json(p)))
    out.sort(key=lambda d: (d["effective"], d["id"]))
    for i, d in enumerate(out):
        if not d.get("kind"):
            d["kind"] = "inception" if i == 0 else "period"
    return out
