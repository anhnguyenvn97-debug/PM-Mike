import json
from datetime import date

import backtest_engine as be
import pandas as pd
import pytest
from common import (
    BookError,
    default_backtest_config,
    load_backtest_config,
    validate_backtest_config,
)
from conftest import (
    ANCHOR,
    SESSIONS,
    UNIVERSE,
    constrain,
    holdings,
    make,
    make_db,
    set_book,
)

import portfolio as pf

FREE = {"cfg": {"brokerage_bps": 0, "sell_tax_bps": 0, "lag_sessions": 1, "risk_free_rate": 0}}


def bt(root, start, config=None, benchmark="VNINDEX", name="p", timeline=None):
    return be.run(name, start, benchmark, config=config or FREE["cfg"],
                  portfolio=root / "portfolio", db=root / "m.db", timeline=timeline)


def profile(home, pid, effective, ratings=None, pp=None, drop=()):
    """A decision profile: the saved book with ratings/pp changed, names dropped."""
    spec = pf.read_book(home) or pf.default_book({g: sorted(m) for g, m in UNIVERSE.items()})
    for g, r in (ratings or {}).items():
        spec["groups"][g] = {**spec["groups"].get(g, {"investable": []}),
                             "rating": r, "pp": (pp or {}).get(g, 0)}
    for s in spec["groups"].values():
        s["investable"] = [t for t in s["investable"] if t not in drop]
    return {"id": pid, "effective": effective, "note": "", **spec}


TILT = {"ratings": {"G1": "OW1", "G3": "UW1"}, "pp": {"G1": 3, "G3": -3}}


def triggers(res):
    return [(str(e.decision), str(e.fill), e.trigger) for e in res["rebalances"].itertuples()]


def test_weights_on_the_last_session_equal_target(root):
    home = make(root)
    set_book(home, ratings={"G1": "OW1", "G3": "UW1"}, pp={"G1": 2, "G3": -2}, drop=["BBB"])
    constrain(home, stock={"max": 0.5})
    make_db(root)
    book = be.load_book("p", root / "portfolio", root / "m.db")
    mk = be.load_market(root / "m.db")
    w, gone, floored = be.targets_at(book, mk["fcap"].loc[pd.Timestamp(ANCHOR)])
    tw = holdings(book["compute"])
    assert dict(zip(book["names"], w)) == pytest.approx({t: tw.get(t, 0.0) for t in book["names"]})
    assert gone == [] and floored == []


def test_inception_cost_and_lag(root):
    make(root, rebalance={"frequency": "1Q", "drift_threshold": None})
    make_db(root)
    cfg = {"brokerage_bps": 10, "sell_tax_bps": 10, "lag_sessions": 2, "risk_free_rate": 0.06}
    res = bt(root, "2026-09-01", cfg)
    assert triggers(res) == [("2026-09-01", "2026-09-03", "inception")]
    assert res["equity"]["portfolio"].iloc[0] == 1.0
    assert res["equity"]["portfolio"].iloc[-1] == pytest.approx(1 - 0.001)  # buys pay brokerage only


def test_returns_drift_the_weights_and_drift_trades_to_target(root):
    make(root, rebalance={"frequency": None, "drift_threshold": 0.01})
    make_db(root, jumps=[("2026-09-07", "AAA", 0.10)])
    res = bt(root, "2026-09-01")
    # AAA holds 40%: +10% -> nav +4%; G1 moves 60% -> 64/104, group drift 0.64/1.04 - 0.6
    assert res["equity"]["portfolio"].iloc[-1] == pytest.approx(1.04)
    assert triggers(res) == [("2026-09-01", "2026-09-02", "inception"),
                             ("2026-09-07", "2026-09-08", "drift")]
    e = res["rebalances"].iloc[1]
    assert e.policy == "full" and str(e.target_as_of) == "2026-09-01"
    assert e.group_drift == pytest.approx(0.64 / 1.04 - 0.6)
    held = pd.Series({"AAA": 0.44, "BBB": 0.2, "CCC": 0.2, "DDD": 0.1, "EEE": 0.1}) / 1.04
    tgt = pd.Series({"AAA": 0.4, "BBB": 0.2, "CCC": 0.2, "DDD": 0.1, "EEE": 0.1})
    assert e.turnover == pytest.approx(0.5 * (held - tgt).abs().sum())
    end = res["holdings_end"].set_index("ticker")
    assert end.loc["AAA", "weight"] == pytest.approx(0.4)  # restored, flat after


def test_fill_at_open_splits_the_session(root):
    make(root, rebalance={"frequency": None, "drift_threshold": 0.01})
    # AAA closes +10% on 09-07 (drift decided), opens 09-08 at 1.05, closes 1.10
    make_db(root, jumps=[("2026-09-07", "AAA", 0.10)], opens={("2026-09-08", "AAA"): 1.05})
    res = bt(root, "2026-09-01")
    assert triggers(res) == [("2026-09-01", "2026-09-02", "inception"),
                             ("2026-09-07", "2026-09-08", "drift")]
    nav = res["equity"].set_index("date")["portfolio"]
    at_open = 1.04 + 0.44 * (1.05 / 1.10 - 1)            # leg 1 on the held 44 of 104
    assert nav[pd.Timestamp("2026-09-08").date()] == pytest.approx(
        at_open * (1 + 0.4 * (1.10 / 1.05 - 1)))           # leg 2 on the target's 40%
    held = pd.Series({"AAA": 0.42, "BBB": 0.2, "CCC": 0.2, "DDD": 0.1, "EEE": 0.1}) / at_open
    tgt = pd.Series({"AAA": 0.4, "BBB": 0.2, "CCC": 0.2, "DDD": 0.1, "EEE": 0.1})
    assert res["rebalances"].iloc[1].turnover == pytest.approx(0.5 * (held - tgt).abs().sum())


def test_open_without_a_fill_compounds_to_the_close(root):
    make(root, rebalance={"frequency": None, "drift_threshold": 0.01})
    make_db(root, jumps=[("2026-09-07", "AAA", 0.10)])
    close_only = bt(root, "2026-09-01")
    make_db(root, jumps=[("2026-09-07", "AAA", 0.10)],
            opens={("2026-09-04", "AAA"): 0.9, ("2026-09-07", "CCC"): 1.2})
    gapped = bt(root, "2026-09-01")
    assert gapped["equity"]["portfolio"].tolist() == pytest.approx(
        close_only["equity"]["portfolio"].tolist())
    assert triggers(gapped) == triggers(close_only)


def test_trigger_is_checked_on_the_session_after_a_fill(root):
    home = make(root, rebalance={"frequency": "1Q", "drift_threshold": None})
    constrain(home, stock={"max": 0.42})
    # inception fills at the 09-02 open; AAA runs +30% over that session, so
    # the new book breaches at the 09-02 close and is decided then, not 09-03
    make_db(root, jumps=[("2026-09-02", "AAA", 0.30)], opens={("2026-09-02", "AAA"): 1.0})
    res = bt(root, "2026-09-01")
    assert triggers(res) == [("2026-09-01", "2026-09-02", "inception"),
                             ("2026-09-02", "2026-09-03", "breach")]


def test_drift_is_measured_against_the_standing_target(root):
    make(root, rebalance={"frequency": None, "drift_threshold": 0.01})
    # AAA +10% with its float cap: a re-derived neutral would follow the market,
    # but the standing target set at inception does not (D50), so drift fires
    make_db(root, jumps=[("2026-09-07", "AAA", 0.10)], fcap={("2026-09-07", "AAA"): 44})
    res = bt(root, "2026-09-01")
    assert triggers(res) == [("2026-09-01", "2026-09-02", "inception"),
                             ("2026-09-07", "2026-09-08", "drift")]
    assert res["holdings_end"].set_index("ticker").loc["AAA", "target"] == pytest.approx(0.4)


@pytest.mark.parametrize("jump, fired", [(0.10, False), (0.30, True)])
def test_breach_trigger_waits_for_the_tolerance(root, jump, fired):
    home = make(root, rebalance={"frequency": "1Q", "drift_threshold": None})
    constrain(home, stock={"max": 0.42})         # AAA's .40 target does not bind
    make_db(root, jumps=[("2026-09-07", "AAA", jump)])
    res = bt(root, "2026-09-01")
    # +10%: AAA .44/1.04 = 42.3%, over the cap but under 1.1 x 42% = 46.2%
    # +30%: .52/1.12 = 46.4%, a breach; the fill clips AAA to the cap and BBB,
    # the rest of G1, takes the excess; G2 and G3 are untouched (D51)
    want = [("2026-09-01", "2026-09-02", "inception")]
    if fired:
        want.append(("2026-09-07", "2026-09-08", "breach"))
        end = res["holdings_end"].set_index("ticker")["weight"]
        assert end.to_dict() == pytest.approx(
            {"AAA": 0.42, "BBB": 0.72 / 1.12 - 0.42, "CCC": 0.2 / 1.12,
             "DDD": 0.1 / 1.12, "EEE": 0.1 / 1.12})
        assert res["rebalances"].iloc[1].policy == "edge"
        assert res["stats"]["n_breach"] == 1
    assert triggers(res) == want


def test_breach_between_calendars_trades_against_the_standing_target(root):
    home = make(root, rebalance={"frequency": "1M", "drift_threshold": None})
    constrain(home, stock={"max": 0.42})
    # EEE's float cap moves 10 -> 70 mid-month; a re-derived target would give
    # it 70/160, the standing target of 09-01 keeps 10%
    make_db(root, jumps=[("2026-09-07", "AAA", 0.30)], fcap={("2026-09-03", "EEE"): 70})
    res = bt(root, "2026-08-03")
    assert triggers(res)[-1] == ("2026-09-07", "2026-09-08", "breach")
    e = res["rebalances"].iloc[-1]
    assert (e.policy, str(e.target_as_of)) == ("edge", "2026-09-01")
    end = res["holdings_end"].set_index("ticker")
    assert end.loc["EEE", "weight"] == pytest.approx(0.1 / 1.12)
    assert end.loc["EEE", "target"] == pytest.approx(0.1)


def test_edge_fill_falls_back_to_full_when_infeasible(root):
    home = make(root, rebalance={"frequency": "1Q", "drift_threshold": None})
    constrain(home, stock={"max": 0.24})  # 5 names x 24% fills the book; 4 do not
    # EEE has no float cap from 09-04 and trades again on the last session (the
    # books are evaluated there, so it stays in the book)
    make_db(root, jumps=[("2026-09-07", "AAA", 0.30)],
            fcap={("2026-09-04", "EEE"): 0, (str(ANCHOR), "EEE"): 10})
    res = bt(root, "2026-09-01")
    # the fallback target breaks the cap too: one fill, then breach waits
    assert triggers(res) == [("2026-09-01", "2026-09-02", "inception"),
                             ("2026-09-07", "2026-09-08", "breach")]
    e = res["rebalances"].iloc[-1]
    assert (e.policy, e.dropped) == ("full", "EEE")
    assert any("cannot be clipped" in m for m in res["messages"])
    end = res["holdings_end"].set_index("ticker")
    assert "EEE" not in end.index and end["target"].sum() == pytest.approx(1.0)


def test_no_target_call_on_a_breach_or_drift_check(root, monkeypatch):
    make(root, rebalance={"frequency": "1M", "drift_threshold": 0.01})
    make_db(root)
    calls = []
    real = be.targets_at
    monkeypatch.setattr(be, "targets_at", lambda *a, **k: calls.append(1) or real(*a, **k))
    res = bt(root, "2026-07-01")
    assert len(calls) == 3 == len(res["rebalances"])  # inception + August + September


@pytest.mark.parametrize("tol, fired", [(None, False), (0.30, False), (0.005, True)])
def test_breach_tolerance_is_set_in_the_statement(root, tol, fired):
    home = make(root, rebalance={"frequency": "1Q", "drift_threshold": None,
                                 "breach_tolerance": tol})
    constrain(home, stock={"max": 0.42})
    make_db(root, jumps=[("2026-09-07", "AAA", 0.10)])   # AAA .44/1.04 = 42.31%
    res = bt(root, "2026-09-01")
    # over the 42% cap, so the dial decides: null never trips even with the cap
    # on, 30% wants 54.6%, 0.5% trips at 42.21%
    want = [("2026-09-01", "2026-09-02", "inception")]
    if fired:
        want.append(("2026-09-07", "2026-09-08", "breach"))
    assert triggers(res) == want


def test_underweight_larger_than_a_past_neutral_holds_zero(root):
    home = make(root, rebalance={"frequency": "1M", "drift_threshold": None})
    set_book(home, ratings={"G1": "OW3", "G3": "UW3"}, pp={"G1": 9, "G3": -9})
    make_db(root, fcap={("2026-06-01", "EEE"): 5, ("2026-08-01", "EEE"): 10})
    res = bt(root, "2026-07-01")
    assert "G3" in res["messages"][-1] and "held at 0%" in res["messages"][-1]
    assert res["rebalances"].iloc[0]["holdings"] == 4               # July: EEE not held
    end = res["holdings_end"].set_index("ticker")
    assert end.loc["EEE", "target"] == pytest.approx(0.01)         # September: 10% - 9 pp


def test_monthly_calendar_rederives_budgets(root):
    make(root, rebalance={"frequency": "1M", "drift_threshold": None})
    make_db(root, fcap={("2026-08-15", "EEE"): 70})  # G3 10 -> 70 of 160
    res = bt(root, "2026-07-15")
    assert triggers(res) == [("2026-07-15", "2026-07-16", "inception"),
                             ("2026-08-03", "2026-08-04", "calendar"),
                             ("2026-09-01", "2026-09-02", "calendar")]
    end = res["holdings_end"].set_index("ticker")
    assert end.loc["EEE", "target"] == pytest.approx(70 / 160)


def test_two_week_periods_count_from_the_start_week(root):
    make(root, rebalance={"frequency": "2W", "drift_threshold": None})
    make_db(root)
    res = bt(root, "2026-08-05")  # a Wednesday; periods start Mondays 08-03, 08-17, 08-31
    assert [d for d, _, _ in triggers(res)] == ["2026-08-05", "2026-08-17", "2026-08-31"]


def test_statistics_against_benchmark(root):
    make(root)
    make_db(root, jumps=[("2026-09-08", "AAA", -0.10)])
    res = bt(root, "2026-09-01", benchmark="VN30")
    s = res["stats"]
    b = res["equity"]["benchmark"]
    i0 = SESSIONS.get_loc(pd.Timestamp("2026-09-01"))
    assert b.iloc[0] == 1.0
    assert b.iloc[-1] == pytest.approx((1000 + len(SESSIONS) - 1) / (1000 + i0))
    assert s["portfolio"]["total"] == pytest.approx(-0.04)
    assert s["portfolio"]["max_drawdown"] == pytest.approx(-0.04)
    assert s["excess"] == pytest.approx(-0.04 - s["benchmark"]["total"])
    assert s["benchmark"]["sharpe"] == pytest.approx(
        (s["benchmark"]["annualised"] - 0) / s["benchmark"]["vol"])
    assert s["n_calendar"] == 0 and s["turnover"] == 0


def test_start_snaps_back_and_window_guards(root):
    make(root)
    make_db(root, bench_gap=pd.Timestamp("2026-09-09"))
    assert str(bt(root, "2026-08-01")["start"]) == "2026-07-31"  # Saturday -> Friday (D64)
    assert str(bt(root, "2026-08-03")["start"]) == "2026-08-03"  # a session stays
    res = bt(root, "2020-01-01")
    assert str(res["start"]) == "2026-06-01" and "before the history" in res["messages"][0]
    with pytest.raises(BookError, match="leaves 2 session"):
        bt(root, "2026-09-10", {**FREE["cfg"], "lag_sessions": 1})
    with pytest.raises(BookError, match="not in index_prices"):
        bt(root, None, benchmark="VN100")
    with pytest.raises(BookError, match="VN30 has no close on 2026-09-09"):
        bt(root, "2026-09-01", benchmark="VN30")


def test_group_with_no_priced_name_drops_out(root):
    make(root, rebalance={"frequency": "1M", "drift_threshold": None})
    # G3's only name has no float cap in August and trades again on the last session
    make_db(root, fcap={("2026-08-01", "EEE"): 0, (str(ANCHOR), "EEE"): 10})
    res = bt(root, "2026-07-01")
    aug = res["rebalances"].iloc[1]
    assert (str(aug.decision), aug.dropped) == ("2026-08-03", "G3")
    assert res["holdings_end"].set_index("ticker")["target"].to_dict() == pytest.approx(
        {"AAA": 40 / 90, "BBB": 20 / 90, "CCC": 20 / 90, "DDD": 10 / 90})


def test_infeasible_constraint_on_a_past_date_fails(root):
    home = make(root, rebalance={"frequency": "1M", "drift_threshold": None})
    constrain(home, stock={"max": 0.24})  # 5 names x 24% fills the book; 4 do not
    make_db(root, fcap={("2026-08-01", "EEE"): 0, (str(ANCHOR), "EEE"): 10})
    with pytest.raises(BookError, match="2026-08-03: max per stock"):
        bt(root, "2026-07-01")


def test_build_writes_outputs(root, capsys):
    make(root)
    make_db(root)
    (root / "portfolio" / "p" / "backtest_config.json").write_text('{"risk_free_rate": 0.05}')
    res = be.build("p", "2026-09-01", "VNINDEX", portfolio=root / "portfolio",
                   db=root / "m.db")
    out = root / "portfolio" / "p" / be.OUT
    assert {p.name for p in out.iterdir()} == {"equity.csv", "rebalances.csv", "holdings_end.csv",
                                               "summary.csv", "built_from.txt"}
    assert res["config"]["risk_free_rate"] == 0.05
    assert pd.read_csv(out / "summary.csv")["cfg_risk_free_rate"][0] == 0.05
    assert "OK    p" in capsys.readouterr().out


def test_run_writes_nothing(root):
    make(root)
    make_db(root)
    bt(root, "2026-09-01")
    assert not (root / "portfolio" / "p" / be.OUT).exists()


def test_no_timeline_equals_the_live_book(root):
    home = make(root, rebalance={"frequency": "1M", "drift_threshold": 0.01})
    make_db(root, jumps=[("2026-08-12", "AAA", 0.10)])
    mech = bt(root, "2026-07-01")
    replay = bt(root, "2026-07-01", timeline=[profile(home, "live-1", "1900-01-01")])
    assert replay["equity"]["portfolio"].tolist() == pytest.approx(
        mech["equity"]["portfolio"].tolist())
    assert triggers(replay) == triggers(mech)
    assert set(replay["rebalances"]["profile"]) == {"live-1"}
    assert set(mech["rebalances"]["profile"]) == {"live"} and mech["timeline"] is None


def test_decision_is_a_trigger_and_sets_the_standing_target(root):
    home = make(root, rebalance={"frequency": "1M", "drift_threshold": None})
    make_db(root)
    res = bt(root, "2026-07-01", timeline=[profile(home, "a", "1900-01-01"),
                                           profile(home, "b", "2026-07-15", **TILT)])
    assert triggers(res) == [("2026-07-01", "2026-07-02", "inception"),
                             ("2026-07-15", "2026-07-16", "decision"),
                             ("2026-08-03", "2026-08-04", "calendar"),
                             ("2026-09-01", "2026-09-02", "calendar")]
    reb = res["rebalances"]
    assert list(reb["profile"]) == ["a", "b", "b", "b"]
    assert str(reb.iloc[1].target_as_of) == "2026-07-15"
    assert res["stats"]["n_decision"] == 1
    end = res["holdings_end"].set_index("ticker")["target"]
    assert end.to_dict() == pytest.approx({"AAA": 0.42, "BBB": 0.21, "CCC": 0.2,
                                           "DDD": 0.1, "EEE": 0.07})
    assert [(d["id"], str(d["applied"]), d["status"]) for d in res["timeline"]] == [
        ("a", "2026-07-01", "applied"), ("b", "2026-07-15", "applied")]


def test_decisions_before_the_window_collapse_to_the_start(root):
    home = make(root, rebalance={"frequency": "1Q", "drift_threshold": None})
    make_db(root)
    res = bt(root, "2026-07-01", timeline=[profile(home, "a", "2026-06-01"),
                                           profile(home, "b", "2026-06-15", **TILT)])
    assert triggers(res) == [("2026-07-01", "2026-07-02", "inception")]
    assert res["rebalances"].iloc[0].profile == "b"
    assert {d["id"]: d["status"] for d in res["timeline"]} == {"a": "superseded by b",
                                                               "b": "applied"}


def test_first_decision_after_the_start_opens_the_window_and_trades_on_its_date(root):
    home = make(root, rebalance={"frequency": "1Q", "drift_threshold": None})
    make_db(root)
    res = bt(root, "2026-07-01", timeline=[profile(home, "t0", "2026-07-15", **TILT)])
    assert triggers(res) == [("2026-07-01", "2026-07-02", "inception"),
                             ("2026-07-15", "2026-07-16", "decision")]
    assert list(res["rebalances"]["profile"]) == ["t0", "t0"]
    assert any("opens the window as an illustration and trades as recorded from 2026-07-15"
               in m for m in res["messages"])
    assert [(d["id"], str(d["placed"]), str(d["applied"])) for d in res["timeline"]] == [
        ("t0", "2026-07-15", "2026-07-15")]
    assert res["stats"]["n_decision"] == 1


def test_weekend_decision_is_decided_on_friday_and_covers_the_calendar(root):
    home = make(root, rebalance={"frequency": "1M", "drift_threshold": None})
    make_db(root)
    res = bt(root, "2026-07-01", timeline=[profile(home, "a", "1900-01-01"),
                                           profile(home, "b", "2026-08-01", **TILT)])
    # Saturday 08-01: decided on Friday's close, filled at Monday's open, the
    # first session of August, so August trades no calendar rebalance again
    assert triggers(res) == [("2026-07-01", "2026-07-02", "inception"),
                             ("2026-07-31", "2026-08-03", "decision"),
                             ("2026-09-01", "2026-09-02", "calendar")]
    reb = res["rebalances"]
    assert reb.iloc[1].also == "calendar" and str(reb.iloc[1].target_as_of) == "2026-07-31"


def test_decision_in_flight_on_a_calendar_boundary_covers_it(root):
    home = make(root, rebalance={"frequency": "1M", "drift_threshold": None})
    make_db(root)
    cfg = {**FREE["cfg"], "lag_sessions": 2}
    res = bt(root, "2026-07-01", cfg, timeline=[profile(home, "a", "1900-01-01"),
                                                profile(home, "b", "2026-07-31", **TILT)])
    assert triggers(res) == [("2026-07-01", "2026-07-03", "inception"),
                             ("2026-07-31", "2026-08-04", "decision"),
                             ("2026-09-01", "2026-09-03", "calendar")]
    assert res["rebalances"].iloc[1].also == "calendar"


def test_replay_trades_the_book_as_recorded_on_a_weekend_date(root):
    home = make(root, rebalance={"frequency": "1Q", "drift_threshold": None})  # next: 10-01
    make_db(root, fcap={("2026-08-03", "BBB"): 40.0})     # Monday's float cap differs
    pf.record_decision(home, "2026-07-01", db=root / "m.db")
    set_book(home, **TILT)
    d = pf.record_decision(home, "2026-08-01", "active", db=root / "m.db")  # a Saturday
    assert d["priced_as_of"] == "2026-07-31"
    res = bt(root, "2026-07-01", timeline=pf.load_decisions(home))
    assert triggers(res)[1] == ("2026-07-31", "2026-08-03", "decision")
    end = res["holdings_end"].set_index("ticker")["target"].to_dict()
    assert end == pytest.approx({h["t"]: h["w"] for h in d["holdings"]})


def test_decision_during_a_pending_fill_is_deferred(root):
    home = make(root, rebalance={"frequency": "1Q", "drift_threshold": None})
    make_db(root)
    cfg = {**FREE["cfg"], "lag_sessions": 2}
    res = bt(root, "2026-07-01", cfg, timeline=[profile(home, "a", "1900-01-01"),
                                                profile(home, "b", "2026-07-02", **TILT)])
    # inception decided 07-01 fills 07-03; b lands 07-02 and is decided at 07-03
    assert triggers(res) == [("2026-07-01", "2026-07-03", "inception"),
                             ("2026-07-03", "2026-07-07", "decision")]
    assert res["rebalances"].iloc[1].deferred_from == "2026-07-02"


def test_decision_after_the_history_is_reported_not_applied(root):
    home = make(root, rebalance={"frequency": "1Q", "drift_threshold": None})
    make_db(root)
    res = bt(root, "2026-09-01", timeline=[profile(home, "a", "1900-01-01"),
                                           profile(home, "z", "2026-12-01", **TILT)])
    assert triggers(res) == [("2026-09-01", "2026-09-02", "inception")]
    assert {d["id"]: d["status"] for d in res["timeline"]}["z"] == "after the history"
    assert any("z effective 2026-12-01 is after the last session" in m for m in res["messages"])


def test_replayed_profile_drops_names_not_trading_on_the_last_session(root):
    home = make(root, rebalance={"frequency": "1Q", "drift_threshold": None})
    old = profile(home, "a", "1900-01-01")          # recorded with BBB investable
    make_db(root, fcap={(str(ANCHOR), "BBB"): None})
    res = bt(root, "2026-09-01", timeline=[old])
    assert "BBB" not in set(res["holdings_end"]["ticker"])
    assert any("decision a: G1: ['BBB'] not trading on 2026-09-11" in m
               for m in res["messages"])
    assert res["timeline"][0]["report"]["dropped_names"] == {"G1": ["BBB"]}


def test_lost_group_in_a_profile_warns_and_runs(root):
    home = make(root, rebalance={"frequency": "1Q", "drift_threshold": None})
    make_db(root)
    gone = profile(home, "a", "1900-01-01", ratings={"G1": "OW1", "G9": "UW1"},
                   pp={"G1": 3, "G9": -3})
    res = bt(root, "2026-09-01", timeline=[gone])
    assert triggers(res) == [("2026-09-01", "2026-09-02", "inception")]
    assert any("groups not in the universe" in m and "G9" in m for m in res["messages"])
    assert any("decision a: active pp net to +3.000" in m for m in res["messages"])
    assert res["holdings_end"]["target"].sum() == pytest.approx(1.0)


def test_timeline_flags_a_decision_recorded_under_another_setup(root):
    home = make(root, rebalance={"frequency": "1Q", "drift_threshold": None})
    d = pf.record_decision(home, "2026-07-01", db=root / "m.db")
    res = bt(root, "2026-09-01", timeline=[d])
    assert res["timeline"][0]["kind"] == "inception"
    assert res["timeline"][0]["setup_differs"] is False
    constrain(home, stock={"max": 0.5})
    assert bt(root, "2026-09-01", timeline=[d])["timeline"][0]["setup_differs"] is True


@pytest.mark.parametrize("freq, start, last, want", [
    ("1M", "2026-07-01", "2026-09-11", "2026-10-01"),
    ("1M", "2026-07-01", "2026-12-15", "2027-01-01"),
    ("1Q", "2026-07-01", "2026-09-11", "2026-10-01"),
    ("1Q", "2026-07-01", "2026-11-11", "2027-01-01"),
    ("2W", "2026-08-05", "2026-09-11", "2026-09-14"),    # Mondays 08-03, 08-17, 08-31, 09-14
    ("1M", "2026-07-01", "2026-10-20", "2026-11-02"),    # Nov 1 is a Sunday
    (None, "2026-07-01", "2026-09-11", None),
])
def test_next_calendar(freq, start, last, want):
    got = be.next_calendar(freq, date.fromisoformat(start), date.fromisoformat(last))
    assert (str(got) if got else None) == want


def test_monitor_holds_the_last_decision_to_the_last_session(root):
    home = make(root, rebalance={"frequency": "1M", "drift_threshold": 0.5})
    (home / "screens.json").write_text(json.dumps({"turnover": {"on": True, "min_pct": 0.1}}))
    assert be.monitor("p", root / "portfolio", root / "m.db")["decision"] is None
    make_db(root, jumps=[("2026-09-07", "AAA", 0.10)])
    pf.record_decision(home, "2026-06-01", db=root / "m.db")
    set_book(home, ratings={"G1": "OW1", "G3": "UW1"}, pp={"G1": 3, "G3": -3})
    pf.record_decision(home, "2026-08-15", "active", db=root / "m.db")   # a Saturday
    m = be.monitor("p", root / "portfolio", root / "m.db")
    assert m["decision"]["id"] == "2026-08-15" and m["decision"]["kind"] == "active"
    # decided on Friday 08-14's close, as recorded; filled at Monday's open,
    # trading from the inception book actually held, not from cash (D65)
    replay = be.run("p", "2026-06-01", config=load_backtest_config(home),
                    portfolio=root / "portfolio", db=root / "m.db",
                    timeline=pf.load_decisions(home))
    # the forward test from inception (D66): every fill of the replay
    reb = replay["rebalances"]
    assert [(r["decision"], r["fill"], r["trigger"]) for r in m["rebalances"]] == [
        (str(r.decision), str(r.fill), r.trigger) for r in reb.itertuples()]
    assert [(r["decision"], r["fill"], r["trigger"]) for r in m["rebalances"][-2:]] == [
        ("2026-08-14", "2026-08-17", "decision"), ("2026-09-01", "2026-09-02", "calendar")]
    assert m["rebalances"][0]["trigger"] == "inception" and m["start"] == "2026-06-01"
    switch = reb.set_index("decision").loc[date(2026, 8, 14)]
    assert [r["also"] for r in m["rebalances"][-2:]] == ["", ""]
    turn = m["rebalances"][-2]["turnover"]
    assert turn == pytest.approx(switch.turnover) and 0 < turn < 0.2
    nav = replay["equity"]["portfolio"]
    assert m["series"]["portfolio"] == pytest.approx(list(nav))
    assert m["total"] == pytest.approx(nav.iloc[-1] - 1)
    # one state per session at its close
    day = dict(zip(m["series"]["dates"], m["daily"]))
    assert len(day) == len(m["daily"]) == len(replay["equity"])
    first = m["daily"][0]
    assert first["drift"] is None and first["pending"]["trigger"] == "inception"
    assert day["2026-08-13"]["profile"] == "2026-06-01" and day["2026-08-13"]["pending"] is None
    pend = day["2026-08-14"]                                   # decided, not yet traded
    assert pend["profile"] == "2026-08-15" and pend["target_as_of"] == "2026-08-14"
    assert pend["pending"] == {"trigger": "decision", "decision": "2026-08-14",
                               "fill": "2026-08-17"}
    assert pend["drift"] == pytest.approx(switch.group_drift)
    filled = day["2026-08-17"]
    assert filled["pending"] is None and filled["drift"] < pend["drift"]
    assert sum(h["w"] for h in filled["holdings"]) == pytest.approx(1)
    assert day["2026-09-01"]["target_as_of"] == "2026-09-01"
    assert m["daily"][-1]["drift"] == pytest.approx(m["now"]["drift"])
    assert {h["t"]: (h["w"], h["target"]) for h in m["daily"][-1]["holdings"]} == pytest.approx(
        {h["t"]: (h["w"], h["target"]) for h in m["holdings"]})
    assert m["status"] == ("decision 2026-08-15 priced 2026-08-14, filled 2026-08-17, "
                           "held to 2026-09-11")
    assert m["next_calendar"] == "2026-10-01" and m["now"]["breach"] is None
    # AAA (.42) +10% on 09-07: G1 .63 -> (.462 + .21) / 1.042
    assert m["now"]["drift"] == pytest.approx((0.462 + 0.21) / 1.042 - 0.63)
    assert {h["t"]: h["target"] for h in m["holdings"]}["AAA"] == pytest.approx(0.42)
    assert [(f["t"], f["screen"]) for f in m["flags"]] == [("DDD", "turnover")]


def test_monitor_reports_a_decision_too_recent_or_after_the_history(root):
    home = make(root)
    pf.record_decision(home, str(ANCHOR), db=root / "m.db")
    m = be.monitor("p", root / "portfolio", root / "m.db")
    assert "too recent" in m["status"] and m["now"] is None
    assert m["rebalances"] == [] and m["flags"] == []
    pf.record_decision(home, "2026-12-01", "period", db=root / "m.db")
    assert "after the last session" in be.monitor("p", root / "portfolio",
                                                   root / "m.db")["status"]


@pytest.mark.parametrize("patch, msg", [
    ({"lag_sessions": 1.5}, "integer 0-5"), ({"lag_sessions": 6}, "integer 0-5"),
    ({"brokerage_bps": -1}, ">= 0"), ({"sell_tax_bps": True}, ">= 0"),
    ({"risk_free_rate": 6}, "fraction"), ({"fee": 1}, "unknown key"),
])
def test_backtest_config_validation(patch, msg):
    with pytest.raises(BookError, match=msg):
        validate_backtest_config(patch)


def test_backtest_config_defaults(tmp_path):
    assert load_backtest_config(tmp_path) == default_backtest_config()
    assert default_backtest_config()["risk_free_rate"] == 0.06
