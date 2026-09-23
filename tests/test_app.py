import json

import app as desk
import pandas as pd
import pytest
from common import BOOK
from conftest import ANCHOR, build, make, make_db, set_book, weights

import portfolio as pf


@pytest.fixture
def client(root):
    desk.app.config.update(PORTFOLIO=root / "portfolio", DB=root / "m.db", TESTING=True)
    return desk.app.test_client()


def test_open_group_map(root, client, monkeypatch):
    opened = []
    monkeypatch.setattr(desk.os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(desk, "GROUP_MAP", root / "group_map.csv")
    r = client.post("/api/open/group_map", json={})
    assert r.status_code == 200 and opened == [root / "group_map.csv"]
    assert client.post("/api/open/group_map").status_code == 415      # JSON body only
    monkeypatch.setattr(desk, "GROUP_MAP", root / "missing.csv")
    r = client.post("/api/open/group_map", json={})
    assert r.status_code == 422 and "not found" in r.get_json()["log"] and len(opened) == 1


def spec(**over):
    s = {"groups": {"G1": {"rating": "AV", "pp": 0, "investable": ["AAA", "BBB"]},
                    "G2": {"rating": "AV", "pp": 0, "investable": ["CCC", "DDD"]},
                    "G3": {"rating": "AV", "pp": None, "investable": ["EEE"]}},
         "tactical": {"on": False, "groups": []}}
    s.update(over)
    return s


def test_pages_load(root, client):
    make(root)
    assert client.get("/").status_code == 200
    st = client.get("/api/state").get_json()
    assert [p["name"] for p in st["portfolios"]] == ["p"] and st["sessions"][-1] == str(ANCHOR)
    assert st["portfolios"][0]["n"] == 5 and st["portfolios"][0]["last"] is None
    assert st["universe"]["as_of"] == str(ANCHOR) and st["universe"]["groups"][0] == {
        "name": "G1", "n": 2, "fcap": pytest.approx(60 / 1e9), "weight": pytest.approx(0.6)}
    d = client.get("/api/p/p").get_json()
    assert d["book"] is None and d["universe"]["as_of"] == str(ANCHOR)
    assert [g["name"] for g in d["universe"]["groups"]] == ["G1", "G2", "G3"]
    assert d["universe"]["groups"][0]["weight"] == pytest.approx(0.6)
    assert d["screens"]["fol"] == {"on": False, "min_limit_pct": 30}
    assert client.get("/api/p/p?as_of=2026-08-01").get_json()["universe"]["as_of"] == \
        "2026-07-31"
    assert client.get("/api/p/nope").status_code == 404
    assert client.get("/api/p/p?as_of=01-08-2026").status_code == 400


def test_save_book_round_trips_and_applies_auto_no(root, client):
    home = make(root)
    s = spec(groups={"G1": {"rating": "UW2", "pp": -4.123456, "investable": ["BBB", "AAA"]},
                     "G2": {"rating": "UW1", "pp": -1, "investable": ["CCC"]},
                     "G3": {"rating": "AV", "pp": None, "investable": []}},
             tactical={"on": True, "groups": [
                 {"name": "T1", "rating": "OW2", "pp": 4.1235, "members": ["CCC"]},
                 {"name": "T2", "rating": "OW1", "pp": 2, "members": []}]})
    r = client.put("/api/p/p/book", json=s)
    assert r.status_code == 200, r.get_json()["log"]
    assert "G2: no investable name left, rated NO" in r.get_json()["log"]
    d = client.get("/api/p/p").get_json()
    assert d["book"]["groups"]["G1"] == {"rating": "UW2", "pp": -4.1235,
                                         "investable": ["AAA", "BBB"]}
    assert d["book"]["tactical"]["groups"][0] == {"name": "T1", "rating": "OW2", "pp": 4.1235,
                                                  "members": ["CCC"]}
    assert r.get_json()["version"] == d["versions"]["book"]
    assert weights(build(root)) == pytest.approx(
        {"G1": 0.75 - 0.041235, "T1": 0.25 + 0.041235, "G2": 0, "G3": 0, "T2": 0})
    assert json.loads((home / BOOK).read_text())["tactical"]["on"] is True


@pytest.mark.parametrize("over, msg", [
    ({"groups": {"G1": {"rating": "AV", "investable": ["CCC"]}}}, "belong to another group"),
    ({"groups": {"GX": {"rating": "AV", "investable": []}}}, "unknown group"),
    ({"groups": {"G1": {"rating": "OW", "investable": ["AAA"]}}}, "unknown rating"),
    ({"groups": {"G1": {"rating": "OW1", "pp": "2", "investable": ["AAA"]}}}, "must be a number"),
    ({"tactical": {"on": True, "groups": [{"name": "T1", "members": ["AAA"]},
                                          {"name": "T2", "members": ["AAA"]}]}},
     "claimed by both T1 and T2"),

])
def test_save_book_rejects_bad_input(root, client, over, msg):
    home = make(root)
    r = client.put("/api/p/p/book", json=spec(**over))
    assert r.status_code == 422 and msg in r.get_json()["log"]
    assert not (home / BOOK).exists()


def test_preview_is_priced_as_of_the_date_and_writes_nothing(root, client):
    home = make(root)
    make_db(root, fcap={("2026-09-01", "EEE"): 30.0})        # G3 grows in September
    s = spec(groups={"G1": {"rating": "OW1", "pp": 3, "investable": ["AAA"]},
                     "G2": {"rating": "UW1", "pp": -3, "investable": ["CCC", "DDD"]},
                     "G3": {"rating": "AV", "pp": 0, "investable": ["EEE"]}})
    cons = {"sector": {"on": False, "max": 0.25, "per_group": {}},
            "stock": {"on": True, "max": 0.5},
            "large": {"on": False, "threshold": 0.05, "aggregate": 0.4}}
    p = client.post("/api/p/p/preview", json={**s, "constraints": cons,
                                              "as_of": "2026-08-15"}).get_json()
    assert p["ok"] and p["as_of"] == "2026-08-14" and p["universe"]["as_of"] == "2026-08-14"
    assert {r["group"]: r["neutral"] for r in p["rows"]}["G3"] == pytest.approx(0.1)
    later = client.post("/api/p/p/preview", json={**s, "constraints": cons}).get_json()
    assert {r["group"]: r["neutral"] for r in later["rows"]}["G3"] == pytest.approx(30 / 120)
    assert not (home / BOOK).exists() and not (home / "target").exists()
    bad = client.post("/api/p/p/preview", json={**s, "constraints": {
        **cons, "stock": {"on": True, "max": 0.1}}}).get_json()
    assert not bad["ok"] and "max per stock cannot fill the book" in bad["error"]


def test_preview_reports_faults_and_drops(root, client):
    make(root)
    make_db(root, fcap={(str(ANCHOR), "BBB"): None})
    s = spec(groups={"G1": {"rating": "OW1", "pp": 2, "investable": ["AAA", "BBB"]},
                     "G3": {"rating": "UW3", "pp": -9, "investable": ["EEE"]}})
    p = client.post("/api/p/p/preview", json=s).get_json()
    assert p["ok"] and p["active"]["net_pp"] == pytest.approx(-7)
    assert len(p["active"]["faults"]) == 1 and "net to -7.000" in p["active"]["faults"][0]
    assert p["drops"]["dropped_names"] == {"G1": ["BBB"]} and p["drops"]["appeared"] == ["G2"]
    rows = {x["group"]: x for x in p["rows"]}
    assert rows["G2"]["rating"] == "NO"


def test_universe_marks_flags_and_new_names(root, client):
    home = make(root)
    client.put("/api/p/p/screens", json={"screens": {"turnover": {"on": True, "min_pct": 0.1}}})
    pf.record_decision(home, "2026-07-01", db=root / "m.db")
    make_db(root, universe={"G1": {"AAA": 40.0, "BBB": 20.0}, "G2": {"CCC": 20.0, "DDD": 10.0},
                            "G3": {"EEE": 10.0}},
            fcap={("2026-06-01", "FFF"): None})
    u = client.get("/api/p/p").get_json()["universe"]
    assert u["flags"]["DDD"][0]["screen"] == "turnover"
    assert not any(m["new"] for g in u["groups"] for m in g["members"])


def test_statement_constraints_screens_validated_before_write(root, client):
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
    r = client.put("/api/p/p/screens", json={"screens": {"fol": {"on": True, "min_limit_pct": -1}}})
    assert r.status_code == 422
    r = client.put("/api/p/p/screens", json={"screens": {"fol": {"on": True}}})
    assert r.status_code == 200
    assert json.loads((home / "screens.json").read_text())["fol"] == {"on": True,
                                                                      "min_limit_pct": 30}


def test_lifecycle_new_and_delete(root, client):
    r = client.post("/api/p", json={"name": "q", "statement": None})
    assert r.status_code == 200
    assert client.post("/api/p", json={"name": "q"}).status_code == 422
    home = root / "portfolio" / "q"
    assert (home / "screens.json").exists()
    assert client.delete("/api/p/q", json={"yes": False}).status_code == 422
    assert client.delete("/api/p/q", json={"yes": True}).status_code == 200
    assert not home.exists()


def test_save_refuses_to_overwrite_a_newer_file(root, client):
    home = make(root)
    v = client.get("/api/p/p").get_json()["versions"]
    cons = json.loads((home / "constraints.json").read_text())
    cons["stock"] = {"on": True, "max": 0.5}
    (home / "constraints.json").write_text(json.dumps(cons))        # hand edit after load
    r = client.put("/api/p/p/constraints", json={"constraints": cons, "version": v["constraints"]})
    assert r.status_code == 422 and "changed on disk" in r.get_json()["log"]
    v = client.get("/api/p/p").get_json()["versions"]              # reload, then save
    assert client.put("/api/p/p/constraints",
                      json={"constraints": cons, "version": v["constraints"]}).status_code == 200
    assert client.put("/api/p/p/book", json={**spec(), "version": v["book"]}).status_code == 200
    r = client.put("/api/p/p/book", json={**spec(), "version": v["book"]})
    assert r.status_code == 422 and "changed on disk" in r.get_json()["log"]
    assert client.put("/api/p/p/book", json=spec()).status_code == 200  # no version: no check


def test_guards(root, client):
    make(root)
    assert client.post("/api/p/p/preview", data="{}", content_type="text/plain").status_code == 415
    assert client.post("/api/p/p/preview", json={},
                       headers={"Host": "evil.example"}).status_code == 403
    r = client.post("/api/p/p/decisions", json={"effective": "11/09/2026"})
    assert r.status_code == 422 and "not YYYY-MM-DD" in r.get_json()["log"]


def test_state_lists_benchmarks(root, client, drop, tmp_path):
    import ingest
    from conftest import INDEX_HEADERS, make_index_rows
    from conftest import make_rows as stock_rows
    db = tmp_path / "ingested.db"
    files = [drop(stock_rows(sessions=4), "stock.xlsx"),
             drop(make_index_rows(sessions=4)[2:], "bench.xlsx", INDEX_HEADERS)]
    assert ingest.build(files, db) == 0
    desk.app.config["DB"] = db
    m = client.get("/api/state").get_json()["market"]
    assert {ld["file"]: ld["kind"] for ld in m["loads"]} == {"stock.xlsx": "stock",
                                                             "bench.xlsx": "index"}
    assert m["benchmarks"] == [
        {"code": "VN30", "sessions": 3, "from": "2026-01-06", "to": "2026-01-08", "missing": 1,
         "late": 0},
        {"code": "VNINDEX", "sessions": 3, "from": "2026-01-06", "to": "2026-01-08", "missing": 1,
         "late": 0}]
    assert m["mcap"] == {"rows": 0, "names": 0, "tickers": []}


def test_backtest_run_switches_benchmark_and_writes_nothing(root, client):
    home = make(root)
    make_db(root, jumps=[("2026-09-08", "AAA", -0.10)])
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


def test_record_decision_kinds_and_guards(root, client):
    home = make(root)
    d = client.get("/api/p/p").get_json()
    assert d["decisions"] == [] and d["head_matches_book"] is None
    v = d["versions"]["book"]
    r = client.post("/api/p/p/decisions", json={"effective": "2026-09-01", "version": v,
                                                "dirty": True})
    assert r.status_code == 422 and "unsaved edits" in r.get_json()["log"]
    r = client.post("/api/p/p/decisions", json={"effective": "2026-08-01", "note": "start",
                                                "kind": "active", "version": v})
    assert r.status_code == 200, r.get_json()["log"]
    assert "recorded inception decision 2026-08-01, priced as of 2026-07-31" in r.get_json()["log"]
    assert "priced_as_of: 2026-07-31" in (home / "target" / "built_from.txt").read_text()
    r = client.post("/api/p/p/decisions", json={"effective": "2026-07-01", "kind": "period",
                                                "version": v})
    assert r.status_code == 422 and "before inception" in r.get_json()["log"]
    r = client.post("/api/p/p/decisions", json={"effective": "2026-09-01", "version": v})
    assert r.status_code == 422 and "period or active" in r.get_json()["log"]
    assert client.post("/api/p/p/decisions", json={"effective": "2026-09-01", "kind": "period",
                                                   "note": "sept", "version": v}).status_code == 200
    log = client.get("/api/p/p/decisions").get_json()
    assert [(x["id"], x["kind"], x["note"]) for x in log["decisions"]] == [
        ("2026-08-01", "inception", "start"), ("2026-09-01", "period", "sept")]
    x = log["decisions"][1]
    assert x["priced_as_of"] == "2026-09-01" and x["setup_differs"] is False
    assert len(x["holdings"]) == 5 and x["report"]["dropped_names"] == {}
    assert log["head_matches_book"] is None                          # no book.json yet

    assert client.put("/api/p/p/book", json=spec()).status_code == 200
    assert client.get("/api/p/p").get_json()["head_matches_book"] is True
    assert client.put("/api/p/p/book", json=spec(groups={
        **spec()["groups"], "G1": {"rating": "AV", "pp": 0, "investable": ["AAA"]}})
    ).status_code == 200
    assert client.get("/api/p/p").get_json()["head_matches_book"] is False
    r = client.post("/api/p/p/decisions", json={"effective": "2026-09-02", "kind": "active",
                                                "version": v})
    assert r.status_code == 422 and "changed on disk" in r.get_json()["log"]   # stale page
    client.put("/api/p/p/constraints", json={"constraints": {"stock": {"on": True, "max": 0.5}}})
    assert client.get("/api/p/p").get_json()["decisions"][0]["setup_differs"] is True


def test_reset_then_monitor(root, client):
    home = make(root, rebalance={"frequency": "1M", "drift_threshold": None})
    m = client.get("/api/p/p/monitor").get_json()
    assert m["ok"] and m["decision"] is None
    set_book(home)
    v = client.get("/api/p/p").get_json()["versions"]["book"]
    client.post("/api/p/p/decisions", json={"effective": "2026-07-01", "version": v})
    m = client.get("/api/p/p/monitor").get_json()
    assert m["ok"] and m["decision"]["kind"] == "inception"
    assert m["next_calendar"] == "2026-10-01" and m["now"]["drift"] == pytest.approx(0)
    assert [r["trigger"] for r in m["rebalances"]] == ["inception", "calendar", "calendar"]
    assert client.post("/api/p/p/reset", json={}).status_code == 200
    assert client.get("/api/p/p").get_json()["decisions"] == []
    assert client.post("/api/p/p/reset", json={}).status_code == 422


def test_backtest_replays_decisions_unless_mechanical(root, client):
    make(root)
    v = client.get("/api/p/p").get_json()["versions"]["book"]
    client.post("/api/p/p/decisions", json={"effective": "2026-06-01", "version": v})
    r = client.post("/api/p/p/backtest", json={"start": "2026-09-01"}).get_json()
    assert r["ok"] and r["rebalances"][0]["profile"] == "2026-06-01"
    assert r["timeline"][0]["applied"] == "2026-09-01" and r["timeline"][0]["kind"] == "inception"
    m = client.post("/api/p/p/backtest", json={"start": "2026-09-01", "mechanical": True}).get_json()
    assert m["rebalances"][0]["profile"] == "live" and m["timeline"] is None


def test_backtest_config_save_validates_and_checks_version(root, client):
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


def test_state_lists_unmapped_tickers(root, client):
    make(root)
    gm = pd.read_csv(root / "group_map.csv")
    gm[gm["Ticker"] != "EEE"].to_csv(root / "group_map.csv", index=False)
    desk.GROUP_MAP = root / "group_map.csv"
    try:
        st = client.get("/api/state").get_json()
    finally:
        import common
        desk.GROUP_MAP = common.GROUP_MAP
    assert st["unmapped"] == ["EEE"]
    assert "not in group map" in st["portfolios"][0]["error"]
