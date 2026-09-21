import duckdb
import ingest
import pandas as pd
import params
import pytest
from common import BookError, sessions, snap
from conftest import ANCHOR, make_db, make_rows


@pytest.fixture
def con(drop, tmp_path):
    db = tmp_path / "m.db"
    assert ingest.build([drop(make_rows(sessions=25))], db) == 0
    c = duckdb.connect(db, read_only=True)
    yield c
    c.close()


def gmap(tickers=("AAA", "BBB")):
    return pd.DataFrame({"ticker": list(tickers), "icb_l2": "X", "group": "G"})


def latest(con):
    return con.execute("SELECT max(trade_date) FROM prices").fetchone()[0]


def test_values(con):
    fol = pd.DataFrame({"ticker": ["AAA"], "fol_limit": [0.49]})
    df = params.compute(con, latest(con), gmap(), fol)
    a = df.set_index("ticker").loc["AAA"]
    assert a.float_cap == 400_000 * 10_000
    assert a.free_float_ratio == 0.4
    assert a.sessions_21 == 21
    assert a.value_21 == 21e9
    assert a.adv_21 == 1e9
    assert a.turnover_21_pct == pytest.approx(1e9 / 4e9 * 100)
    assert a.fol_limit == 0.49
    assert a.adj_factor == 1
    assert pd.isna(df.set_index("ticker").loc["BBB", "fol_limit"])


def test_short_history_has_no_turnover(con, tmp_path):
    first = con.execute("SELECT min(trade_date) FROM prices").fetchone()[0]
    df = params.compute(con, first, gmap(), params.load_fol(tmp_path / "none.csv"))
    assert (df.sessions_21 == 1).all()
    assert df.turnover_21_pct.isna().all()


def test_ticker_missing_from_map_fails(con):
    with pytest.raises(ValueError, match="not in group map"):
        params.compute(con, latest(con), gmap(["AAA"]), pd.DataFrame(
            columns=["ticker", "fol_limit"]))


def test_at_snaps_caches_and_leaves_out_no_float_cap(root):
    db = root / "m.db"
    assert sessions(db)[-1] == ANCHOR and str(snap(db, "2026-08-01")) == "2026-07-31"
    a = params.at(db, "2026-08-01")
    assert str(a.attrs["session"]) == "2026-07-31" and len(a) == 5
    assert params.at(db, "2026-08-01") is not a and len(params._CACHE) == 1   # a copy, cached
    assert params.universe(a) == {"G1": ["AAA", "BBB"], "G2": ["CCC", "DDD"], "G3": ["EEE"]}
    make_db(root, fcap={("2026-09-10", "EEE"): 0, (str(ANCHOR), "DDD"): None})
    b = params.at(db)
    assert sorted(b["ticker"]) == ["AAA", "BBB", "CCC"] and b.attrs["no_float_cap"] == ["EEE"]
    with pytest.raises(BookError, match="before the first session"):
        params.at(db, "2020-01-01")


def test_group_map_edit_needs_no_rebuild(root):
    db = root / "m.db"
    assert params.universe(params.at(db))["G3"] == ["EEE"]
    gm = pd.read_csv(root / "group_map.csv")
    gm.loc[gm["Ticker"] == "EEE", "Exclusive group"] = "G2"
    gm.to_csv(root / "group_map.csv", index=False)
    assert params.universe(params.at(db))["G2"] == ["CCC", "DDD", "EEE"]


def test_run_writes_the_cache_file(root):
    out = root / "params_out"
    session, path, df = params.run("2026-08-01", db=root / "m.db", out_dir=out)
    assert str(session) == "2026-07-31" and path == out / "2026-07-31.csv"
    assert len(pd.read_csv(path)) == len(df) == 5
