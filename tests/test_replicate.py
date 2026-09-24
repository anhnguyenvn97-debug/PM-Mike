import json

import pandas as pd
import pytest
import replicate as rp
from common import BookError, default_screens, validate_screens
from conftest import ANCHOR, make, make_db, set_book

import portfolio as pf

TILT = {"ratings": {"G1": "OW1", "G3": "UW1"}, "pp": {"G1": 3, "G3": -3}}


def ticket(root, aum=10_000, cash=5, exec_date=str(ANCHOR)):
    return rp.run("p", aum, cash, exec_date, portfolio=root / "portfolio", db=root / "m.db")


def home_with_inception(root, freq="1M"):
    home = make(root, rebalance={"frequency": freq, "drift_threshold": None})
    pf.record_decision(home, "2026-07-01", db=root / "m.db")
    return home


# ---------- allocate: the pure core ----------

@pytest.mark.parametrize("equity", [9_500, 12_345.6, 100_000, 777])
def test_allocate_never_overspends_and_stays_within_a_lot(equity):
    w = {"AAA": 0.37, "BBB": 0.23, "CCC": 0.2, "DDD": 0.13, "EEE": 0.07}
    px = {"AAA": 1.3, "BBB": 0.7, "CCC": 2.1, "DDD": 1.0, "EEE": 0.45}
    lots = rp.allocate(w, px, equity)
    spend = sum(lots[t] * 100 * px[t] for t in w)
    assert spend <= equity
    for t, wt in w.items():
        want = equity * wt / (100 * px[t])
        assert want - 1 < lots[t] <= want + 1 and lots[t] >= int(want)
    # nothing left that would still fit a lot of a name below its target
    left = equity - spend
    assert all(100 * px[t] > left for t in w if lots[t] < equity * w[t] / (100 * px[t]))
    assert rp.allocate(w, px, equity) == lots                       # deterministic


def test_allocate_ties_go_to_the_larger_weight_then_a_to_z():
    # 9 400 floors, 100 left: DDD and EEE both .5 lot short with equal weights
    lots = rp.allocate({"AAA": .4, "BBB": .2, "CCC": .2, "DDD": .1, "EEE": .1},
                       dict.fromkeys(["AAA", "BBB", "CCC", "DDD", "EEE"], 1.0), 9_500)
    assert lots == {"AAA": 38, "BBB": 19, "CCC": 19, "DDD": 10, "EEE": 9}
    # A ranks first on its .45 remainder, but a lot no longer fits the 50 left
    lots = rp.allocate({"A": .3, "B": .7}, {"A": 1.0, "B": 1.0}, 150)
    assert lots == {"A": 0, "B": 1}
    lots = rp.allocate({"A": .5, "Z": .5}, {"A": 1.0, "Z": 1.0}, 100)   # .5 each, one lot
    assert lots == {"A": 1, "Z": 0}


# ---------- the ticket ----------

def test_ticket_sizes_the_target_in_force_on_the_previous_close(root):
    home_with_inception(root)
    t = ticket(root)
    assert (t["exec_session"], t["reference"]) == ("2026-09-11", "2026-09-10")
    # the target in force at the 09-10 close is August's calendar rebalance (D67)
    assert (t["target_as_of"], t["trigger"], t["source"], t["profile"]) == (
        "2026-08-31", "calendar", "derived", "2026-07-01")
    got = {x["t"]: (x["lots"], x["shares"]) for x in t["lines"]}
    assert got == {"AAA": (38, 3800), "BBB": (19, 1900), "CCC": (19, 1900),
                   "DDD": (10, 1000), "EEE": (9, 900)}
    tot = t["totals"]
    assert tot["spend"] == 9_500 and tot["cash_pct"] == pytest.approx(5)
    assert all(x["shares"] % 100 == 0 for x in t["lines"])
    assert tot["group_drift"] == pytest.approx(50 / 9_500)   # DDD's extra lot: G2 up, G3 down
    assert {x["t"]: round(x["dev_bp"]) for x in t["lines"]}["EEE"] == -53
    assert any("5 holdings after rounding is outside 20-30" in m for m in t["messages"])


def test_a_decision_between_the_decision_date_and_the_execution_date(root):
    home = home_with_inception(root)
    set_book(home, **TILT)
    pf.record_decision(home, "2026-08-15", "active", db=root / "m.db")     # a Saturday
    t = ticket(root, exec_date="2026-08-20")
    assert (t["reference"], t["target_as_of"], t["source"], t["trigger"]) == (
        "2026-08-19", "2026-08-14", "recorded", "decision")
    assert {x["t"]: x["target"] for x in t["lines"]} == pytest.approx(
        {"AAA": .42, "BBB": .21, "CCC": .2, "DDD": .1, "EEE": .07})
    before = ticket(root, exec_date="2026-08-14")       # reference 08-13: July's rebalance
    assert (before["target_as_of"], before["source"]) == ("2026-07-31", "derived")


def test_execution_on_a_weekend_snaps_forward(root):
    home_with_inception(root)
    t = ticket(root, exec_date="2026-08-15")
    assert (t["exec_session"], t["reference"]) == ("2026-08-17", "2026-08-14")


def test_cash_is_a_floor_at_the_sizing_close(root):
    home_with_inception(root)
    for aum, cash in ((10_000, 5), (12_345, 2.5), (987_654, 0), (50_000, 33)):
        tot = ticket(root, aum, cash)["totals"]
        assert tot["cash_pct"] >= cash - 1e-9 and tot["spend"] <= tot["equity"]


def test_the_open_is_reported_and_never_changes_the_shares(root):
    home_with_inception(root)
    flat = ticket(root)
    make_db(root, opens={(str(ANCHOR), "AAA"): 1.1})
    t = ticket(root)
    assert [x["shares"] for x in t["lines"]] == [x["shares"] for x in flat["lines"]]
    assert {x["t"]: x["open"] for x in t["lines"]}["AAA"] == pytest.approx(1.1)
    assert t["totals"]["spend_open"] == pytest.approx(9_500 + 380)
    assert t["totals"]["cash_open_pct"] == pytest.approx(1.2)
    assert any("below the 5% floor" in m for m in t["messages"])


def test_a_name_with_no_price_on_the_sizing_session_is_dropped(root):
    home_with_inception(root)
    make_db(root, fcap={("2026-09-10", "EEE"): None, (str(ANCHOR), "EEE"): 10})
    t = ticket(root)
    assert {x["t"] for x in t["lines"]} == {"AAA", "BBB", "CCC", "DDD"}
    assert sum(x["target"] for x in t["lines"]) == pytest.approx(1)
    assert {x["t"]: x["target"] for x in t["lines"]}["AAA"] == pytest.approx(.4 / .9)
    assert any("not priced on 2026-09-10" in m and "EEE" in m for m in t["messages"])


def test_a_name_below_one_lot_stays_at_zero_and_is_listed(root):
    home_with_inception(root)
    t = ticket(root, aum=500, cash=0)       # DDD and EEE want half a lot; DDD gets it
    got = {x["t"]: x["lots"] for x in t["lines"]}
    assert got == {"AAA": 2, "BBB": 1, "CCC": 1, "DDD": 1, "EEE": 0}
    assert any("below one lot" in m and "EEE" in m for m in t["messages"])
    assert t["totals"]["holdings"] == 4


def test_an_execution_date_after_the_data_is_unattainable(root):
    home_with_inception(root)
    t = ticket(root, exec_date="2026-09-14")
    assert t["unattainable"].startswith("execution date 2026-09-14 is after the last session")
    assert t["lines"] == [] and "totals" not in t


def test_an_unattainable_decision_is_named_and_not_used(root):
    home = home_with_inception(root)
    set_book(home, **TILT)
    pf.record_decision(home, str(ANCHOR), "active", db=root / "m.db")
    t = ticket(root)
    assert t["target_as_of"] == "2026-08-31" and t["profile"] == "2026-07-01"
    assert [d["id"] for d in t["decisions_unattainable"]] == [str(ANCHOR)]
    assert any(f"decision {ANCHOR} (effective {ANCHOR}) is recorded but unattainable" in m
               for m in t["messages"])


def test_no_book_before_the_inception_fill(root):
    home_with_inception(root)
    with pytest.raises(BookError, match="no book is in force before the 2026-07-01 open"):
        ticket(root, exec_date="2026-07-01")
    assert ticket(root, exec_date="2026-07-02")["target_as_of"] == "2026-07-01"


def test_no_decision_or_none_attainable(root):
    home = make(root)
    with pytest.raises(BookError, match="no decision recorded yet"):
        ticket(root)
    pf.record_decision(home, str(ANCHOR), db=root / "m.db")
    with pytest.raises(BookError, match="no decision is attainable yet"):
        ticket(root)


def test_the_ticket_writes_nothing(root):
    home = home_with_inception(root)
    before = sorted((p.relative_to(home), p.stat().st_mtime_ns) for p in home.rglob("*"))
    ticket(root)
    assert sorted((p.relative_to(home), p.stat().st_mtime_ns) for p in home.rglob("*")) == before


# ---------- position screens (D70) ----------

FRAME = pd.DataFrame([{"ticker": "X", "outstanding_shares": 1000.0, "free_float": 400.0,
                       "close_raw": 10.0, "adv_21": 1000.0}])


def only(name, **cfg):
    sc = validate_screens({k: {**v, "on": k == name} for k, v in default_screens().items()})
    sc[name].update(cfg)
    return sc


@pytest.mark.parametrize("name, at, over", [
    ("ownership", 50, 51),        # 5% of 1 000 outstanding
    ("float", 60, 61),            # 15% of 400 free float
    ("liquidity", 1000, 1001),    # 1 000 x 10 VND / (50% x 1 000 a session) = 20 sessions
])
def test_each_position_screen_fires_above_its_threshold(name, at, over):
    assert pf.position_flags(FRAME, {"X": at}, only(name)) == []
    f = pf.position_flags(FRAME, {"X": over}, only(name))
    assert [(x["screen"], x["why"]) for x in f] == [(name, "above")]
    assert ">" in pf.flag_text(f[0])


def test_liquidity_without_volume_flags_no_data_and_zero_positions_skip():
    frame = FRAME.assign(adv_21=float("nan"))
    f = pf.position_flags(frame, {"X": 100, "Y": 100}, only("liquidity"))
    assert f == [{"t": "X", "screen": "liquidity", "value": None, "threshold": 20,
                  "why": "no data"}]
    assert pf.position_flags(FRAME, {"X": 0}, default_screens()) == []


def test_ticket_flags_positions_from_screens_json(root):
    home = home_with_inception(root)
    t = ticket(root)
    # 3 800 AAA shares against 80 outstanding: every position screen fires
    assert {(f["t"], f["screen"]) for f in t["position_flags"]} >= {
        ("AAA", "ownership"), ("AAA", "float"), ("AAA", "liquidity")}
    (home / "screens.json").write_text(json.dumps(
        {"ownership": {"on": False}, "float": {"on": False}, "liquidity": {"on": False},
         "turnover": {"on": True, "min_pct": 0.1}}))
    t = ticket(root)
    assert t["position_flags"] == [] and [(f["t"], f["screen"]) for f in t["flags"]] == [
        ("DDD", "turnover")]


@pytest.mark.parametrize("patch, msg", [
    ({"liquidity": {"on": True, "participation_pct": 0}}, r"\(0, 100\]"),
    ({"liquidity": {"on": True, "participation_pct": 120}}, r"\(0, 100\]"),
    ({"ownership": {"on": True, "max_pct_of_shares": -1}}, "non-negative"),
    ({"float": {"on": True, "cap": 1}}, "unknown key"),
])
def test_position_screen_validation(patch, msg):
    with pytest.raises(BookError, match=msg):
        validate_screens({**default_screens(), **patch})


def test_a_screens_file_without_position_screens_reads_the_defaults(tmp_path):
    sc = validate_screens({"turnover": {"on": True, "min_pct": 0.2}})
    assert sc["ownership"] == {"on": True, "max_pct_of_shares": 5}
    assert sc["liquidity"] == {"on": True, "participation_pct": 50, "max_days": 20}
