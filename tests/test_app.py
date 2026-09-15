import json
import os
import time

import app as desk
import pandas as pd
import pytest
from common import BOOK, TAC, TAC_SWITCH, read_grid, read_invalid
from test_portfolio import (  # noqa: F401 - root is a fixture
    ANCHOR,
    build,
    edit_book,
    make,
    publish,
    root,
    weights,
)


@pytest.fixture
def client(root):  # noqa: F811
    desk.app.config.update(PORTFOLIO=root / "portfolio", PARAMS=root / "params",
                           DB=None, TESTING=True)
    return desk.app.test_client()


def spec(**over):
    s = {"groups": {"G1": {"rating": "AV", "mult": None, "investable": ["AAA", "BBB"]},
                    "G2": {"rating": "AV", "mult": None, "investable": ["CCC", "DDD"]},
                    "G3": {"rating": "AV", "mult": None, "investable": ["EEE"]}},
         "tactical": {"on": False, "groups": []}}
    s.update(over)
    return s


def test_pages_load(root, client):  # noqa: F811
    make(root)
    assert client.get("/").status_code == 200
    st = client.get("/api/state").get_json()
    assert st["sticky"] == "2026-09-11" and [p["name"] for p in st["portfolios"]] == ["p"]
    assert st["portfolios"][0]["n"] == 5
    d = client.get("/api/p/p").get_json()
    assert d["anchor"] == "2026-09-11" and d["book"]["G1"]["investable"] == ["AAA", "BBB"]
    assert [g["name"] for g in d["universe"]["groups"]] == ["G1", "G2", "G3"]
    assert client.get("/api/p/nope").status_code == 404
    assert client.get("/api/p/baseline").status_code == 404


def test_save_book_round_trips_and_applies_auto_no(root, client):  # noqa: F811
    home = make(root)
    s = spec(groups={"G1": {"rating": "OW", "mult": 1.5, "investable": ["BBB", "AAA"]},
                     "G2": {"rating": "UW", "mult": None, "investable": ["CCC"]},
                     "G3": {"rating": "AV", "mult": None, "investable": []}},
             tactical={"on": True, "groups": [
                 {"name": "T1", "rating": "OW", "mult": None, "members": ["CCC"]},
                 {"name": "T2", "rating": "OW", "mult": 2, "members": []}]})
    r = client.put("/api/p/p/book", json=s)
    assert r.status_code == 200, r.get_json()["log"]
    assert "G2: no investable name left, rated NO" in r.get_json()["log"]
    g = read_grid(home / BOOK)
    assert g[:3] == [["1.5", "NO", "NO"], ["OW", "NO", "NO"], ["G1", "G2", "G3"]]
    assert g[3:] == [["AAA", "CCC"], ["BBB"]]          # baseline order, G2 keeps CCC
    t = read_grid(home / TAC)
    assert t[:3] == [["OW", "NO"], ["OW", "NO"], ["T1", "T2"]] and t[3] == ["CCC"]
    assert json.loads((home / TAC_SWITCH).read_text()) == {"tactical_group": "yes"}

    d = client.get("/api/p/p").get_json()
    assert d["book"]["G1"] == {"rating": "OW", "mult": 1.5, "investable": ["AAA", "BBB"]}
    assert d["tactical"]["groups"][0] == {"name": "T1", "rating": "OW", "mult": None,
                                          "members": ["CCC"]}
    # G1 .6 x 1.5 = .9, T1 carries CCC's .2 x 1.25 = .25; the rest are NO
    assert weights(build(root)) == pytest.approx(
        {"G1": 0.9 / 1.15, "T1": 0.25 / 1.15, "G2": 0, "G3": 0, "T2": 0})

    # no tactical groups: the csv goes, the switch says no
    assert client.put("/api/p/p/book", json=spec()).status_code == 200
    assert not (home / TAC).exists()
    assert json.loads((home / TAC_SWITCH).read_text()) == {"tactical_group": "no"}


@pytest.mark.parametrize("over, msg", [
    ({"groups": {"G1": {"rating": "AV", "investable": ["CCC"]}}}, "not in the baseline column"),
    ({"groups": {"GX": {"rating": "AV", "investable": []}}}, "unknown group"),
    ({"groups": {"G1": {"rating": "XX", "investable": ["AAA"]}}}, "unknown rating"),
    ({"groups": {"G1": {"rating": "OW", "mult": -1, "investable": ["AAA"]}}}, "non-negative"),
    ({"tactical": {"on": True, "groups": [{"name": "T1", "members": ["AAA"]},
                                          {"name": "T2", "members": ["AAA"]}]}},
     "claimed by both T1 and T2"),
    ({"tactical": {"on": True, "groups": [{"name": "T1", "members": ["ZZZ"]}]}},
     "in no baseline group"),
])
def test_save_book_rejects_bad_input(root, client, over, msg):  # noqa: F811
    home = make(root)
    before = (home / BOOK).read_text()
    r = client.put("/api/p/p/book", json=spec(**over))
    assert r.status_code == 422 and msg in r.get_json()["log"]
    assert (home / BOOK).read_text() == before and not (home / TAC).exists()


def test_preview_writes_nothing_and_matches_build(root, client):  # noqa: F811
    home = make(root)
    s = spec(groups={"G1": {"rating": "OW", "mult": None, "investable": ["AAA"]}})
    cons = {"sector": {"on": False, "max": 0.25, "per_group": {}},
            "stock": {"on": True, "max": 0.5},
            "large": {"on": False, "threshold": 0.05, "aggregate": 0.4}}
    before = (home / BOOK).read_text()
    p = client.post("/api/p/p/preview", json={**s, "constraints": cons}).get_json()
    assert p["ok"] and p["report"]["stock"]["at_max"] == 1
    assert (home / BOOK).read_text() == before and not (home / "target").exists()

    assert client.put("/api/p/p/book", json=s).status_code == 200
    assert client.put("/api/p/p/constraints", json={"constraints": cons}).status_code == 200
    assert client.post("/api/p/p/build", json={}).status_code == 200
    built = dict(zip(*[read_col(home, c) for c in ("ticker", "target_weight")]))
    assert {h["t"]: h["w"] for h in p["holdings"]} == pytest.approx(built)

    bad = client.post("/api/p/p/preview", json={**s, "constraints": {
        **cons, "stock": {"on": True, "max": 0.1}}}).get_json()
    assert not bad["ok"] and "max per stock cannot fill the book" in bad["error"]


def read_col(home, col):
    return pd.read_csv(home / "target" / "holdings.csv")[col].tolist()


def test_constraints_and_statement_validated_before_write(root, client):  # noqa: F811
    home = make(root)
    before = (home / "constraints.json").read_text()
    r = client.put("/api/p/p/constraints", json={"constraints": {
        "large": {"on": True, "threshold": 0.5, "aggregate": 0.4}}})
    assert r.status_code == 422 and "exceeds large.aggregate" in r.get_json()["log"]
    assert (home / "constraints.json").read_text() == before
    st = json.loads((home / "statement.json").read_text())
    st["holdings"] = {"min": 9, "max": 3}
    r = client.put("/api/p/p/statement", json={"statement": st})
    assert r.status_code == 422 and "min <= max" in r.get_json()["log"]
    st["holdings"] = {"min": 3, "max": 9}
    assert client.put("/api/p/p/statement", json={"statement": st}).status_code == 200
    assert json.loads((home / "statement.json").read_text())["holdings"]["max"] == 9


def test_lifecycle_new_fork_screen_delete(root, client):  # noqa: F811
    r = client.post("/api/p", json={"name": "q", "statement": None})
    assert r.status_code == 200
    assert client.post("/api/p", json={"name": "q"}).status_code == 422
    r = client.post("/api/p/q/fork", json={"anchor": "2026-09-11"})
    assert r.status_code == 200 and "forked q" in r.get_json()["log"]
    home = root / "portfolio" / "q"
    r = client.post("/api/p/q/screen", json={"screens": {
        "turnover": {"on": True, "min_pct": 0.1}, "float_cap": {"on": False, "min_bn_vnd": 1000}}})
    assert r.status_code == 200
    assert client.get("/api/p/q").get_json()["exclusions"][0]["ticker"] == "DDD"
    assert client.post("/api/p/q/screen", json={"invalidate": ["DDD"]}).status_code == 200
    assert read_invalid(home) == ["DDD"]
    r = client.post("/api/p/q/screen", json={"invalidate": ["AAA"]})
    assert r.status_code == 422 and "not on the suggestion list" in r.get_json()["log"]
    assert client.delete("/api/p/q", json={"yes": False}).status_code == 422
    assert client.delete("/api/p/q", json={"yes": True}).status_code == 200
    assert not home.exists()


def test_state_shows_stale_anchor_and_refork(root, client):  # noqa: F811
    home = make(root)
    edit_book(home, ratings={"G3": "OW"}, mults={"G3": "2"})
    st = client.get("/api/state").get_json()
    assert st["stale"] == {} and st["unmapped"] == []
    assert st["portfolios"][0]["refork"] == {"needed": False}

    # baseline rebuilt with a regrouping (G3 -> G4): input/ no longer matches it
    pp = root / "params" / f"{ANCHOR}.csv"
    params = pd.read_csv(pp)
    params.loc[params["group"] == "G3", "group"] = "G4"
    params.to_csv(pp, index=False)
    publish(root, params, ANCHOR, sticky=True)
    rf = client.get("/api/state").get_json()["portfolios"][0]["refork"]
    assert rf == {"needed": True, "lost": {"G3": "OW (mult 2)"}, "appeared": ["G4"]}
    assert client.get("/api/p/p").get_json()["refork"] == rf
    assert client.post("/api/p/p/fork", json={"anchor": str(ANCHOR)}).status_code == 200
    assert client.get("/api/p/p").get_json()["refork"] == {"needed": False}

    # params newer than the baseline built from them: the anchor is stale
    t = time.time() + 10
    os.utime(pp, (t, t))
    assert client.get("/api/state").get_json()["stale"] == {str(ANCHOR): [pp.name]}


def test_save_refuses_to_overwrite_a_newer_file(root, client):  # noqa: F811
    home = make(root)
    v = client.get("/api/p/p").get_json()["versions"]
    cons = json.loads((home / "constraints.json").read_text())
    cons["stock"] = {"on": True, "max": 0.5}
    (home / "constraints.json").write_text(json.dumps(cons))        # hand edit after load
    r = client.put("/api/p/p/constraints", json={"constraints": cons, "version": v["constraints"]})
    assert r.status_code == 422 and "changed on disk" in r.get_json()["log"]
    assert json.loads((home / "constraints.json").read_text()) == cons

    v = client.get("/api/p/p").get_json()["versions"]              # reload, then save
    assert client.put("/api/p/p/constraints",
                      json={"constraints": cons, "version": v["constraints"]}).status_code == 200
    assert client.put("/api/p/p/book", json={**spec(), "version": v["book"]}).status_code == 200
    r = client.put("/api/p/p/book", json={**spec(), "version": v["book"]})
    assert r.status_code == 422 and "changed on disk" in r.get_json()["log"]
    assert client.put("/api/p/p/book", json=spec()).status_code == 200  # no version: no check


def test_guards(root, client):  # noqa: F811
    make(root)
    assert client.post("/api/p/p/build", data="{}", content_type="text/plain").status_code == 415
    assert client.post("/api/p/p/build", json={},
                       headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/p/p/fork", json={"anchor": "11/09/2026"}).status_code == 422


def test_state_lists_benchmarks(root, client, drop, tmp_path):  # noqa: F811
    import ingest
    from conftest import INDEX_HEADERS, make_index_rows
    from conftest import make_rows as stock_rows
    db = tmp_path / "m.db"
    files = [drop(stock_rows(sessions=4), "stock.xlsx"),
             drop(make_index_rows(sessions=4)[2:], "bench.xlsx", INDEX_HEADERS)]
    assert ingest.build(files, db) == 0
    desk.app.config["DB"] = db
    m = client.get("/api/state").get_json()["market"]
    assert {ld["file"]: ld["kind"] for ld in m["loads"]} == {"stock.xlsx": "stock",
                                                             "bench.xlsx": "index"}
    assert m["benchmarks"] == [
        {"code": "VN30", "sessions": 3, "from": "2026-01-06", "to": "2026-01-08", "missing": 1},
        {"code": "VNINDEX", "sessions": 3, "from": "2026-01-06", "to": "2026-01-08", "missing": 1}]


def test_backtest_run_switches_benchmark_and_writes_nothing(root, client):  # noqa: F811
    from test_backtest_engine import make_db
    home = make(root)
    desk.app.config["DB"] = make_db(root, jumps=[("2026-09-08", "AAA", -0.10)])
    d = client.get("/api/p/p/backtest").get_json()
    assert d["config"]["risk_free_rate"] == 0.06 and d["version"] == "-"
    cfg = {**d["config"], "brokerage_bps": 0, "sell_tax_bps": 0}
    r = client.post("/api/p/p/backtest", json={"start": "2026-09-01", "benchmark": "VN30",
                                               "config": cfg}).get_json()
    assert r["ok"] and r["start"] == "2026-09-01" and r["end"] == "2026-09-11"
    assert set(r["benchmarks"]) == {"VN30", "VNINDEX"}
    assert r["benchmarks"]["VN30"]["stats"]["portfolio"]["total"] == pytest.approx(-0.04)
    assert r["portfolio"][0] == 1.0 and len(r["dates"]) == len(r["portfolio"])
    assert r["rebalances"][0]["trigger"] == "inception" and r["rebalances"][0]["drift"] is None
    assert not (home / "backtest_config.json").exists() and not (home / "backtest_engine").exists()

    bad = client.post("/api/p/p/backtest", json={"start": "2026-09-01", "benchmark": "VN50"}).get_json()
    assert not bad["ok"] and "VN50" in bad["error"]
    assert not client.post("/api/p/p/backtest", json={"start": "01/09/2026"}).get_json()["ok"]


def test_backtest_config_save_validates_and_checks_version(root, client):  # noqa: F811
    home = make(root)
    v = client.get("/api/p/p/backtest").get_json()["version"]
    r = client.put("/api/p/p/backtest_config", json={"config": {"lag_sessions": 9}, "version": v})
    assert r.status_code == 422 and not (home / "backtest_config.json").exists()
    r = client.put("/api/p/p/backtest_config",
                   json={"config": {"risk_free_rate": 0.05}, "version": v})
    assert r.status_code == 200
    assert json.loads((home / "backtest_config.json").read_text())["risk_free_rate"] == 0.05
    assert r.get_json()["version"] == client.get("/api/p/p/backtest").get_json()["version"]
    r = client.put("/api/p/p/backtest_config", json={"config": {}, "version": v})  # stale copy
    assert r.status_code == 422 and "changed on disk" in r.get_json()["log"]
