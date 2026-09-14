import json

import duckdb
import ingest
import pandas as pd
import params
import pytest
from conftest import make_rows


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


def test_anchor_resolution(tmp_path):
    s = {pd.Timestamp("2026-01-05").date(), pd.Timestamp("2026-01-06").date()}
    cfg = tmp_path / "anchor.json"
    assert params.resolve_anchor(s, None, cfg)[1] == "latest"
    cfg.write_text(json.dumps({"anchor_date": "2026-01-05"}))
    assert str(params.resolve_anchor(s, None, cfg)[0]) == "2026-01-05"
    cfg.write_text(json.dumps({"anchor_date": "2025-12-31"}))
    assert params.resolve_anchor(s, None, cfg)[1] == "latest"
    with pytest.raises(ValueError):
        params.resolve_anchor(s, "2026-02-01", cfg)
