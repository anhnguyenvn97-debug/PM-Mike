import json
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scr"))

import common
import params
import target

import portfolio as pf

HEADERS = [
    "No", "Ticker", "Company Name", "Date",
    "Reference (D)\nUnit: VND", "Ceiling (D)\nUnit: VND", "Floor (D)\nUnit: VND",
    "Open (D)\nUnit: VND", "Highest (D)\nUnit: VND", "Lowest (D)\nUnit: VND",
    "Close Price (D)\nUnit: VND", "Open Adjusted (D)\nUnit: VND",
    "Highest Adjusted (D)\nUnit: VND", "Lowest Adjusted (D)\nUnit: VND",
    "Close Adjusted (D)\nUnit: VND", "Total trading volume (D)\nUnit: Shares",
    "Current Outstanding Shares\nUnit: Shares", "Free Float Shares\nUnit: Shares",
    "Market Capitalization\nUnit: VND", "Total trading value (D)\nUnit: VND",
]


def make_rows(tickers=("AAA", "BBB"), sessions=25, start=date(2026, 1, 5)):
    """Synthetic FiinPro rows: flat price 10,000, value 1e9/session, HOSE band."""
    rows, n = [], 0
    for i in range(sessions):
        d = start + timedelta(days=i)
        for t in tickers:
            n += 1
            shares, ff, px = 1_000_000, 400_000, 10_000
            rows.append([n, t, f"{t} Corp", d, px, px * 1.07, px * 0.93,
                         px, px, px, px, px, px, px, px, 100_000,
                         shares, ff, px * shares, 1e9])
    return rows


INDEX_HEADERS = [
    "No", "Index/Sector", "Level", "Date", "Close Index (D)\nUnit: Point",
    "Total Trading Volume (D)\nUnit: Shares", "Total Trading Value (D)\nUnit: VND",
]


def make_index_rows(codes=("VNINDEX", "VN30"), sessions=25, start=date(2026, 1, 5)):
    """Synthetic FiinPro index rows on the same calendar as make_rows."""
    rows, n = [], 0
    for i in range(sessions):
        d = start + timedelta(days=i)
        for c in codes:
            n += 1
            rows.append([n, c, 4, d, 1000 + i, 5_000_000, 1e11])
    return rows


@pytest.fixture
def drop(tmp_path):
    """Write a drop with a banner and footer, like a raw portal export."""
    def _write(rows, name="drop.xlsx", headers=HEADERS):
        banner = [[None] * len(headers) for _ in range(3)]
        banner[1][0] = "Data Title"
        footer = [["Website: fiinpro"] + [None] * (len(headers) - 1)]
        pd.DataFrame(banner + [headers] + rows + footer).to_excel(
            tmp_path / name, header=False, index=False)
        return tmp_path / name
    return _write


# ---------- the shared market.db fixture ----------

ANCHOR = date(2026, 9, 11)          # the last session
SESSIONS = pd.bdate_range("2026-06-01", ANCHOR)

# group -> {ticker: float cap}; G1 is 60% of the book, G2 30%, G3 10%.
UNIVERSE = {"G1": {"AAA": 40.0, "BBB": 20.0},
            "G2": {"CCC": 20.0, "DDD": 10.0},
            "G3": {"EEE": 10.0}}
TURNOVER = {"DDD": 0.01}            # % of float cap a day; every other name 0.5


def make_db(root, jumps=(), fcap=None, bench_gap=None, opens=None, universe=None,
            regroup=None, fol=None):
    """market.db, group map and fol.csv under root. Flat prices (close_raw 1.0,
    close_adj 1.0) at UNIVERSE's float caps; open_raw is open_adj on the raw
    scale (1.0 unless opens moves it).
    jumps = [(date, ticker, return)] applied to close_adj from that session on;
    fcap = {(from_date, ticker): cap} from that session on, None = no row (not
    trading); open_adj = close_adj unless opens = {(date, ticker): price};
    universe replaces UNIVERSE; regroup = {ticker: group} in the group map;
    fol = {ticker: limit ratio} for index/fol.csv."""
    universe = universe or UNIVERSE
    level = {t: 1.0 for m in universe.values() for t in m}
    rows = []
    for d in SESSIONS:
        for dd, t, r in jumps:
            if d == pd.Timestamp(dd):
                level[t] *= 1 + r
        for m in universe.values():
            for t, f in m.items():
                for (fd, ft), v in sorted((fcap or {}).items(), key=lambda x: x[0][0]):
                    if ft == t and d >= pd.Timestamp(fd):
                        f = v
                if f is None:
                    continue
                o = (opens or {}).get((str(d.date()), t), level[t])
                value = TURNOVER.get(t, 0.5) / 100 * f
                rows.append((d.date(), t, 1.0, level[t], o, o / level[t], float(f),
                             2.0 * max(f, 1.0), 2.0 * max(f, 1.0), value))
    px = pd.DataFrame(rows, columns=["trade_date", "ticker", "close_raw", "close_adj",
                                     "open_adj", "open_raw", "free_float",
                                     "outstanding_shares", "market_cap", "value"])
    ix = pd.DataFrame([(d.date(), c, 1000.0 + i) for i, d in enumerate(SESSIONS)
                       for c in ("VNINDEX", "VN30") if not (c == "VN30" and d == bench_gap)],
                      columns=["trade_date", "code", "close"])
    names = pd.DataFrame({"ticker": sorted(level), "company_name":
                          [f"{t} Corp" for t in sorted(level)]})
    db = root / "m.db"
    if db.exists():
        db.unlink()
    con = duckdb.connect(str(db))
    for table, df in (("prices", px), ("index_prices", ix), ("tickers", names)):
        con.register("df", df)
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM df")
        con.unregister("df")
    con.execute("CREATE VIEW prices_v AS SELECT *, 'HOSE' AS exchange FROM prices")
    con.close()
    gm = [(t, "X", (regroup or {}).get(t, g)) for g, m in universe.items() for t in m]
    pd.DataFrame(gm, columns=["Ticker", "ICB L2 sector", "Exclusive group"]).to_csv(
        root / "group_map.csv", index=False)
    pd.DataFrame(sorted((fol or {}).items()), columns=["ticker", "fol_limit"]).to_csv(
        root / "fol.csv", index=False)
    params._CACHE.clear()
    common._SESSIONS.clear()
    return db


@pytest.fixture
def root(tmp_path, monkeypatch):
    """tmp_path with market.db (make_db defaults), the group map and fol.csv,
    params pointed at them, and an empty portfolio/."""
    make_db(tmp_path)
    monkeypatch.setattr(params, "MAP", tmp_path / "group_map.csv")
    monkeypatch.setattr(params, "FOL", tmp_path / "fol.csv")
    (tmp_path / "portfolio").mkdir()
    return tmp_path


def make(root, name="p", **statement):
    s = common.default_statement()
    s.update(statement)
    pf.new(name, portfolio=root / "portfolio", statement=s)
    return root / "portfolio" / name


def set_book(home, ratings=None, pp=None, drop=(), tactical=None):
    """Edit and save book.json (the default book if there is none): ratings
    {group: rating}, pp {group: active pp} (unnamed pp are 0), names dropped from
    investable, tactical = the spec's tactical block."""
    spec = pf.read_book(home) or pf.default_book(
        {g: sorted(m) for g, m in UNIVERSE.items()})
    for g, r in (ratings or {}).items():
        spec["groups"][g] = {**spec["groups"].get(g, {"investable": []}),
                             "rating": r, "pp": (pp or {}).get(g, 0)}
    for s in spec["groups"].values():
        s["investable"] = [t for t in s["investable"] if t not in drop]
    if tactical is not None:
        spec["tactical"] = tactical
    pf.write_book(home, spec)
    return spec


def constrain(home, **blocks):
    c = common.default_constraints()
    for k, v in blocks.items():
        c[k] = {**c[k], **({} if k == "active" else {"on": True}), **v}
    (home / "constraints.json").write_text(json.dumps(c))


def compute(root, name="p", **kw):
    return target.compute(name, root / "portfolio", root / "m.db", **kw)


def build(root, name="p", as_of=None):
    return target.build(name, root / "portfolio", root / "m.db", as_of=as_of)


def holdings(res):
    return dict(zip(res["holdings"].ticker, res["holdings"].target_weight))


def weights(res):
    return dict(zip(res["allocation"]["group"], res["allocation"]["weight"]))
