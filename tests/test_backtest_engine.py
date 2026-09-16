import backtest_engine as be
import duckdb
import pandas as pd
import pytest
from common import (
    BookError,
    default_backtest_config,
    load_backtest_config,
    validate_backtest_config,
)
from test_portfolio import (  # noqa: F401
    ANCHOR,
    UNIVERSE,
    constrain,
    edit_book,
    holdings,
    make,
    root,
)

SESSIONS = pd.bdate_range("2026-06-01", ANCHOR)
FREE = {"cfg": {"brokerage_bps": 0, "sell_tax_bps": 0, "lag_sessions": 1, "risk_free_rate": 0}}


def make_db(root, jumps=(), fcap=None, bench_gap=None):  # noqa: F811
    """Flat prices (close_adj 1.0) at the fixture's float caps; jumps = [(date,
    ticker, return)] applied from that session on; fcap = {(from_date, ticker): cap}."""
    rows = []
    level = {t: 1.0 for m in UNIVERSE.values() for t in m}
    for d in SESSIONS:
        for dd, t, r in jumps:
            if d == pd.Timestamp(dd):
                level[t] *= 1 + r
        for m in UNIVERSE.values():
            for t, f in m.items():
                for (fd, ft), v in (fcap or {}).items():
                    if ft == t and d >= pd.Timestamp(fd):
                        f = v
                rows.append((d.date(), t, 1.0, level[t], int(f)))
    px = pd.DataFrame(rows, columns=["trade_date", "ticker", "close_raw", "close_adj", "free_float"])
    ix = pd.DataFrame([(d.date(), c, 1000.0 + i) for i, d in enumerate(SESSIONS)
                       for c in ("VNINDEX", "VN30") if not (c == "VN30" and d == bench_gap)],
                      columns=["trade_date", "code", "close"])
    db = root / "m.db"
    con = duckdb.connect(str(db))
    for table, df in (("prices", px), ("index_prices", ix)):
        con.register("df", df)
        con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM df")
        con.unregister("df")
    con.close()
    return db


def bt(root, start, config=None, benchmark="VNINDEX", name="p"):  # noqa: F811
    return be.run(name, start, benchmark, config=config or FREE["cfg"],
                  portfolio=root / "portfolio", params_dir=root / "params", db=root / "m.db")


def triggers(res):
    return [(str(e.decision), str(e.fill), e.trigger) for e in res["rebalances"].itertuples()]


def test_weights_on_the_anchor_equal_target(root):  # noqa: F811
    home = make(root)
    edit_book(home, ratings={"G1": "OW1", "G3": "UW1"}, pp={"G1": 2, "G3": -2}, blank=["BBB"])
    constrain(home, stock={"max": 0.5})
    make_db(root)
    book = be.load_book("p", root / "portfolio", root / "params")
    mk = be.load_market(root / "m.db")
    w, gone, floored = be.targets_at(book, mk["fcap"].loc[pd.Timestamp(ANCHOR)])
    tw = holdings(book["compute"])
    assert dict(zip(book["names"], w)) == pytest.approx({t: tw.get(t, 0.0) for t in book["names"]})
    assert gone == [] and floored == []


def test_inception_cost_and_lag(root):  # noqa: F811
    make(root, rebalance={"frequency": "1Q", "drift_threshold": None})
    make_db(root)
    cfg = {"brokerage_bps": 10, "sell_tax_bps": 10, "lag_sessions": 2, "risk_free_rate": 0.06}
    res = bt(root, "2026-09-01", cfg)
    assert triggers(res) == [("2026-09-01", "2026-09-03", "inception")]
    assert res["equity"]["portfolio"].iloc[0] == 1.0
    assert res["equity"]["portfolio"].iloc[-1] == pytest.approx(1 - 0.001)  # buys pay brokerage only


def test_returns_drift_the_weights_and_drift_trades_to_target(root):  # noqa: F811
    make(root, rebalance={"frequency": None, "drift_threshold": 0.01})
    make_db(root, jumps=[("2026-09-07", "AAA", 0.10)])
    res = bt(root, "2026-09-01")
    # AAA holds 40%: +10% -> nav +4%; G1 moves 60% -> 64/104, group drift 0.64/1.04 - 0.6
    assert res["equity"]["portfolio"].iloc[-1] == pytest.approx(1.04)
    assert triggers(res) == [("2026-09-01", "2026-09-02", "inception"),
                             ("2026-09-07", "2026-09-08", "drift")]
    e = res["rebalances"].iloc[1]
    assert e.group_drift == pytest.approx(0.64 / 1.04 - 0.6)
    held = pd.Series({"AAA": 0.44, "BBB": 0.2, "CCC": 0.2, "DDD": 0.1, "EEE": 0.1}) / 1.04
    tgt = pd.Series({"AAA": 0.4, "BBB": 0.2, "CCC": 0.2, "DDD": 0.1, "EEE": 0.1})
    assert e.turnover == pytest.approx(0.5 * (held - tgt).abs().sum())
    end = res["holdings_end"].set_index("ticker")
    assert end.loc["AAA", "weight"] == pytest.approx(0.4)  # restored, flat after


def test_drift_is_measured_against_the_rederived_target(root):  # noqa: F811
    make(root, rebalance={"frequency": None, "drift_threshold": 0.01})
    # AAA +10% with its float cap: the neutral moves with the market, so the
    # book has not drifted from its model (the last filled target says 1.5%)
    make_db(root, jumps=[("2026-09-07", "AAA", 0.10)], fcap={("2026-09-07", "AAA"): 44})
    res = bt(root, "2026-09-01")
    assert triggers(res) == [("2026-09-01", "2026-09-02", "inception")]


@pytest.mark.parametrize("jump, fired", [(0.10, False), (0.30, True)])
def test_breach_trigger_waits_for_the_tolerance(root, jump, fired):  # noqa: F811
    home = make(root, rebalance={"frequency": "1Q", "drift_threshold": None})
    constrain(home, stock={"max": 0.42})         # AAA's .40 target does not bind
    make_db(root, jumps=[("2026-09-07", "AAA", jump)])
    res = bt(root, "2026-09-01")
    # +10%: AAA .44/1.04 = 42.3%, over the cap but under 1.1 x 42% = 46.2%
    # +30%: .52/1.12 = 46.4%, a breach; the fill trades back to the target
    want = [("2026-09-01", "2026-09-02", "inception")]
    if fired:
        want.append(("2026-09-07", "2026-09-08", "breach"))
        assert res["holdings_end"].set_index("ticker").loc["AAA", "weight"] == pytest.approx(0.4)
        assert res["stats"]["n_breach"] == 1
    assert triggers(res) == want


@pytest.mark.parametrize("tol, fired", [(None, False), (0.30, False), (0.005, True)])
def test_breach_tolerance_is_set_in_the_statement(root, tol, fired):  # noqa: F811
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


def test_underweight_larger_than_a_past_neutral_holds_zero(root):  # noqa: F811
    home = make(root, rebalance={"frequency": "1M", "drift_threshold": None})
    edit_book(home, ratings={"G1": "OW3", "G3": "UW3"}, pp={"G1": 9, "G3": -9})
    make_db(root, fcap={("2026-06-01", "EEE"): 5, ("2026-08-01", "EEE"): 10})
    res = bt(root, "2026-07-01")
    assert "G3" in res["messages"][-1] and "held at 0%" in res["messages"][-1]
    assert res["rebalances"].iloc[0]["holdings"] == 4               # July: EEE not held
    end = res["holdings_end"].set_index("ticker")
    assert end.loc["EEE", "target"] == pytest.approx(0.01)         # September: 10% - 9 pp


def test_monthly_calendar_rederives_budgets(root):  # noqa: F811
    make(root, rebalance={"frequency": "1M", "drift_threshold": None})
    make_db(root, fcap={("2026-08-15", "EEE"): 70})  # G3 10 -> 70 of 160
    res = bt(root, "2026-07-15")
    assert triggers(res) == [("2026-07-15", "2026-07-16", "inception"),
                             ("2026-08-03", "2026-08-04", "calendar"),
                             ("2026-09-01", "2026-09-02", "calendar")]
    end = res["holdings_end"].set_index("ticker")
    assert end.loc["EEE", "target"] == pytest.approx(70 / 160)


def test_two_week_periods_count_from_the_start_week(root):  # noqa: F811
    make(root, rebalance={"frequency": "2W", "drift_threshold": None})
    make_db(root)
    res = bt(root, "2026-08-05")  # a Wednesday; periods start Mondays 08-03, 08-17, 08-31
    assert [d for d, _, _ in triggers(res)] == ["2026-08-05", "2026-08-17", "2026-08-31"]


def test_statistics_against_benchmark(root):  # noqa: F811
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


def test_start_snaps_forward_and_window_guards(root):  # noqa: F811
    make(root)
    make_db(root, bench_gap=pd.Timestamp("2026-09-09"))
    assert str(bt(root, "2026-08-01")["start"]) == "2026-08-03"  # Saturday -> Monday
    res = bt(root, "2020-01-01")
    assert str(res["start"]) == "2026-06-01" and "before the history" in res["messages"][0]
    with pytest.raises(BookError, match="leaves 2 session"):
        bt(root, "2026-09-10", {**FREE["cfg"], "lag_sessions": 1})
    with pytest.raises(BookError, match="not in index_prices"):
        bt(root, None, benchmark="VN100")
    with pytest.raises(BookError, match="VN30 has no close on 2026-09-09"):
        bt(root, "2026-09-01", benchmark="VN30")


def test_group_with_no_priced_name_drops_out(root):  # noqa: F811
    make(root, rebalance={"frequency": "1M", "drift_threshold": None})
    make_db(root, fcap={("2026-08-01", "EEE"): 0})  # G3's only name
    res = bt(root, "2026-07-01")
    aug = res["rebalances"].iloc[1]
    assert (str(aug.decision), aug.gone) == ("2026-08-03", "G3")
    assert res["holdings_end"].set_index("ticker")["target"].to_dict() == pytest.approx(
        {"AAA": 40 / 90, "BBB": 20 / 90, "CCC": 20 / 90, "DDD": 10 / 90})


def test_infeasible_constraint_on_a_past_date_fails(root):  # noqa: F811
    home = make(root, rebalance={"frequency": "1M", "drift_threshold": None})
    constrain(home, stock={"max": 0.24})  # 5 names x 24% fills the book; 4 do not
    make_db(root, fcap={("2026-08-01", "EEE"): 0})
    with pytest.raises(BookError, match="2026-08-03: max per stock"):
        bt(root, "2026-07-01")


def test_build_writes_outputs(root, capsys):  # noqa: F811
    make(root)
    make_db(root)
    (root / "portfolio" / "p" / "backtest_config.json").write_text('{"risk_free_rate": 0.05}')
    res = be.build("p", "2026-09-01", "VNINDEX", portfolio=root / "portfolio",
                   params_dir=root / "params", db=root / "m.db")
    out = root / "portfolio" / "p" / be.OUT
    assert {p.name for p in out.iterdir()} == {"equity.csv", "rebalances.csv", "holdings_end.csv",
                                               "summary.csv", "built_from.txt"}
    assert res["config"]["risk_free_rate"] == 0.05
    assert pd.read_csv(out / "summary.csv")["cfg_risk_free_rate"][0] == 0.05
    assert "OK    p" in capsys.readouterr().out


def test_run_writes_nothing(root):  # noqa: F811
    make(root)
    make_db(root)
    bt(root, "2026-09-01")
    assert not (root / "portfolio" / "p" / be.OUT).exists()


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
