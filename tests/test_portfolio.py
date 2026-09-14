import json

import baseline
import pandas as pd
import pytest
import target
from common import (
    BOOK,
    GRID,
    BookError,
    default_constraints,
    default_statement,
    grid_column,
    read_grid,
    read_invalid,
    validate_constraints,
    validate_statement,
    write_grid,
)

import portfolio

ANCHOR = pd.Timestamp("2026-09-11").date()

# group -> {ticker: float cap}; G1 is 60% of the book, G2 30%, G3 10%.
UNIVERSE = {"G1": {"AAA": 40.0, "BBB": 20.0},
            "G2": {"CCC": 20.0, "DDD": 10.0},
            "G3": {"EEE": 10.0}}


@pytest.fixture
def root(tmp_path):
    rows = [{"ticker": t, "company_name": f"{t} Corp", "group": g,
             "float_cap": f, "close_raw": 1.0,
             "adj_factor": 1.0, "turnover_21_pct": 0.5 if t != "DDD" else 0.01}
            for g, m in UNIVERSE.items() for t, f in m.items()]
    params = pd.DataFrame(rows)
    (tmp_path / "params").mkdir()
    params.to_csv(tmp_path / "params" / f"{ANCHOR}.csv", index=False)
    publish(tmp_path, params, ANCHOR, sticky=True)
    return tmp_path


def publish(root, params, anchor, sticky=False):
    """Write baseline/<anchor>/ and, if sticky, the root copy backtest.py reads."""
    port = root / "portfolio" / "baseline"
    parts = baseline.build(params, anchor)
    baseline.write(port / str(anchor), *parts)
    if sticky:
        baseline.write(port, *parts)


def make(root, name="p", **statement):
    s = default_statement()
    s.update(statement)
    portfolio.new(name, portfolio=root / "portfolio", statement=s)
    portfolio.fork(name, portfolio=root / "portfolio")
    return root / "portfolio" / name


def constrain(home, **blocks):
    c = default_constraints()
    for k, v in blocks.items():
        c[k] = {**c[k], "on": True, **v}
    (home / "constraints.json").write_text(json.dumps(c))


def holdings(res):
    return dict(zip(res["holdings"].ticker, res["holdings"].target_weight))


def edit_book(home, ratings=None, mults=None, blank=()):
    g = read_grid(home / BOOK)
    for grp, r in (ratings or {}).items():
        j = g[2].index(grp)
        g[1][j] = r
        g[0][j] = (mults or {}).get(grp, r)
    cols = [[t for t in grid_column(g, j) if t not in blank] for j in range(len(g[2]))]
    write_grid(home / BOOK, g[:3], cols)


def build(root, name="p"):
    return target.build(name, portfolio=root / "portfolio", params_dir=root / "params")


def weights(res):
    return dict(zip(res["allocation"]["group"], res["allocation"]["weight"]))


def test_baseline_weights(root):
    alloc = pd.read_csv(root / "portfolio" / "baseline" / str(ANCHOR) / "sector_allocation.csv")
    assert dict(zip(alloc.group, alloc.weight)) == pytest.approx(
        {"G1": 0.6, "G2": 0.3, "G3": 0.1})
    assert read_grid(root / "portfolio" / "baseline" / GRID)[2] == ["G1", "G2", "G3"]


def test_untilted_target_equals_baseline(root):
    make(root)
    res = build(root)
    assert weights(res) == pytest.approx({"G1": 0.6, "G2": 0.3, "G3": 0.1})
    h = dict(zip(res["holdings"].ticker, res["holdings"].target_weight))
    assert h["AAA"] == pytest.approx(0.4)


def test_tilt_and_deletion(root):
    home = make(root)
    edit_book(home, ratings={"G1": "OW", "G3": "NO"}, blank=["BBB"])
    res = build(root)
    # w = b*m / sum(b*m): G1 0.6*1.25=0.75, G2 0.3, G3 0 -> 0.75/1.05, 0.3/1.05
    assert weights(res) == pytest.approx({"G1": 0.75 / 1.05, "G2": 0.3 / 1.05, "G3": 0})
    h = dict(zip(res["holdings"].ticker, res["holdings"].target_weight))
    assert "BBB" not in h and h["AAA"] == pytest.approx(0.75 / 1.05)  # budget stays


def test_tactical_claim_moves_budget(root):
    home = make(root)
    (home / "tactical_group.json").write_text('{"tactical_group": "yes"}')
    write_grid(home / "tactical_group.csv", [["AV"], ["AV"], ["T1"]], [["BBB"]])
    res = build(root)
    assert weights(res) == pytest.approx({"G1": 0.4, "G2": 0.3, "G3": 0.1, "T1": 0.2})


def test_compute_matches_build_and_writes_nothing(root):
    home = make(root)
    edit_book(home, ratings={"G1": "OW"}, blank=["BBB"])
    constrain(home, stock={"max": 0.5})
    res = target.compute("p", portfolio=root / "portfolio", params_dir=root / "params")
    assert not (home / "target").exists()
    built = build(root)
    assert res["holdings"].equals(built["holdings"])
    assert res["allocation"].equals(built["allocation"])


def test_compute_overrides_replace_files(root):
    home = make(root)
    kw = {"portfolio": root / "portfolio", "params_dir": root / "params"}
    book = read_grid(home / BOOK)
    j = book[2].index("G3")
    book[0][j] = book[1][j] = "NO"
    tac = {"on": True, "grid": [["AV"], ["AV"], ["T1"], ["BBB"]]}
    cons = {**default_constraints(), "sector": {"on": True, "max": 0.45, "per_group": {}}}
    res = target.compute("p", book=book, tactical=tac, constraints=cons, **kw)
    # G3 NO: G1 .4, G2 .3, T1 .2 over .9 -> G1 .444, G2 .333, T1 .222; no cap binds
    assert weights(res) == pytest.approx({"G1": 0.4 / 0.9, "G2": 0.3 / 0.9,
                                          "T1": 0.2 / 0.9, "G3": 0})
    assert weights(target.compute("p", **kw)) == pytest.approx(
        {"G1": 0.6, "G2": 0.3, "G3": 0.1})                       # files untouched
    with pytest.raises(BookError, match="tactical overlay is on but has no groups"):
        target.compute("p", tactical={"on": True, "grid": None}, **kw)


def test_sector_cap_iterates(root):
    home = make(root)
    constrain(home, sector={"max": 0.4})
    res = build(root)
    # G1 0.6 -> 0.4; excess 0.2 split 3:1 lifts G2 to 0.45 -> capped 0.4; G3 0.2
    assert weights(res) == pytest.approx({"G1": 0.4, "G2": 0.4, "G3": 0.2})
    assert sorted(res["allocation"].query("capped").group) == ["G1", "G2"]


def test_waterfill_matches_legacy_apply_caps():
    w = {"A": 0.5, "B": 0.3, "C": 0.15, "D": 0.05}
    legacy, _ = target.apply_caps(w, 0.3)
    new, _ = target.waterfill(w, dict.fromkeys(w, 0.3))
    assert new == pytest.approx(legacy)


def test_cap_too_tight_fails(root):
    home = make(root)
    constrain(home, sector={"max": 0.3})
    with pytest.raises(BookError, match="cannot fill"):
        build(root)


def test_per_group_cap_applies_to_tactical(root):
    home = make(root)
    (home / "tactical_group.json").write_text('{"tactical_group": "yes"}')
    write_grid(home / "tactical_group.csv", [["AV"], ["AV"], ["T1"]], [["BBB"]])
    constrain(home, sector={"max": 1.0, "per_group": {"T1": 0.1}})
    # T1 0.2 -> 0.1; 0.1 spread pro-rata over G1 .4, G2 .3, G3 .1
    assert weights(build(root)) == pytest.approx(
        {"G1": 0.45, "G2": 0.3375, "G3": 0.1125, "T1": 0.1})


def test_per_group_unknown_name_fails(root):
    home = make(root)
    constrain(home, sector={"max": 1.0, "per_group": {"Nope": 0.1}})
    with pytest.raises(BookError, match="names no known group"):
        build(root)


def test_legacy_sector_cap_is_not_applied(root, capsys):
    home = make(root)
    (home / "sector_cap.json").write_text('{"sector_cap": "yes", "max_weight": 0.4}')
    assert weights(build(root)) == pytest.approx({"G1": 0.6, "G2": 0.3, "G3": 0.1})
    assert "read only by backtest.py" in capsys.readouterr().out


def test_stock_cap_stays_inside_group(root):
    home = make(root)
    constrain(home, stock={"max": 0.3})
    res = build(root)
    # AAA .4 -> .3, its .1 goes to BBB (.2 -> .3); G1 keeps .6
    assert holdings(res) == pytest.approx(
        {"AAA": 0.3, "BBB": 0.3, "CCC": 0.2, "DDD": 0.1, "EEE": 0.1})
    assert weights(res)["G1"] == pytest.approx(0.6)


def test_stock_cap_spills_when_group_full(root):
    home = make(root)
    constrain(home, stock={"max": 0.25})
    res = build(root)
    # G1 holds at most .5; .1 spills to G2:G3 at 3:1
    assert weights(res) == pytest.approx({"G1": 0.5, "G2": 0.375, "G3": 0.125})
    assert holdings(res)["CCC"] == pytest.approx(0.25)
    assert res["allocation"].set_index("group").at["G1", "full"]


def test_stock_cap_infeasible(root):
    home = make(root)
    constrain(home, stock={"max": 0.15})
    with pytest.raises(BookError, match=r"15\.00% x 5 holdings"):
        build(root)


def test_large_holdings_stop_at_threshold(root):
    home = make(root)
    constrain(home, large={"threshold": 0.15, "aggregate": 0.5})
    res = build(root)
    # large {AAA .4, BBB .2, CCC .2} = .8 > .5; f=.625 puts BBB, CCC under .15 so
    # they stop at .15; AAA alone (.4) is within .5 and keeps f=1.
    # G1 holds .4+.15, G2 .15+.15; the rest goes to EEE, which stops at .15.
    h = holdings(res)
    assert h == pytest.approx({"AAA": 0.4, "BBB": 0.15, "CCC": 0.15, "DDD": 0.15,
                               "EEE": 0.15})
    assert sum(v for v in h.values() if v > 0.15 + 1e-9) <= 0.5
    assert res["holdings"].set_index("ticker").at["AAA", "pin"] == "large"


def test_large_holdings_scales_pro_rata():
    # A {a1 .3, a2 .3}, B eight names at .05. T .1, L .5: a1+a2 = .6 scale by
    # 5/6 to .25 each; A can then hold only .5, so .1 spills into B's eight
    # names (each capped at T), lifting them to .0625.
    members = {"A": ["a1", "a2"], "B": [f"b{i}" for i in range(8)]}
    fcap = {"a1": 3, "a2": 3, **{f"b{i}": 0.5 for i in range(8)}}
    cons = default_constraints()
    cons["large"] = {"on": True, "threshold": 0.1, "aggregate": 0.5}
    sol = target.apply_constraints({"A": 0.6, "B": 0.4}, members, fcap, cons)
    assert sol["stock"]["a1"] == pytest.approx(0.25)
    assert sol["stock"]["b0"] == pytest.approx(0.0625)
    assert sol["group"] == pytest.approx({"A": 0.5, "B": 0.5})
    assert sum(sol["stock"].values()) == pytest.approx(1.0)


def test_large_holdings_infeasible(root):
    home = make(root)
    constrain(home, large={"threshold": 0.1, "aggregate": 0.3})
    with pytest.raises(BookError, match=r"aggregate 30.00% \+ threshold 10.00% x 4"):
        build(root)


@pytest.mark.parametrize("patch, msg", [
    ({"sector": {"on": True, "max": 25}}, "fraction"),
    ({"stock": {"on": True, "max": 0}}, "fraction"),
    ({"large": {"on": True, "threshold": 0.5, "aggregate": 0.4}}, "exceeds"),
    ({"large": {"on": True, "threshold": 1, "aggregate": 1}}, "fraction"),
    ({"fol": {"on": True}}, "unknown block"),
    ({"stock": {"on": True, "max": 0.1, "min": 0.01}}, "unknown key"),
])
def test_constraints_validation(patch, msg):
    c = default_constraints()
    c.update(patch)
    with pytest.raises(BookError, match=msg):
        validate_constraints(c)


def test_holding_range_flag(root):
    make(root, holdings={"min": 1, "max": 5})
    assert build(root)["in_range"] is True
    make(root, "q", holdings={"min": 10, "max": 20})
    assert build(root, "q")["in_range"] is False


def test_addition_fails(root):
    home = make(root)
    g = read_grid(home / BOOK)
    write_grid(home / BOOK, g[:3], [["AAA", "BBB", "EEE"], ["CCC", "DDD"], ["EEE"]])
    with pytest.raises(BookError, match="not in the baseline column"):
        build(root)


def test_refork_carries_by_name(root):
    home = make(root)
    edit_book(home, ratings={"G2": "UW"}, mults={"G2": "0.5"}, blank=["DDD"])
    # New baseline: G3 disappears, G4 appears, FFF joins G2.
    params = pd.DataFrame([
        {"ticker": t, "group": g, "float_cap": 1.0, "close_raw": 1.0, "adj_factor": 1.0}
        for g, ts in {"G1": ["AAA", "BBB"], "G2": ["CCC", "DDD", "FFF"],
                      "G4": ["EEE"]}.items() for t in ts])
    new_anchor = pd.Timestamp("2026-09-12").date()
    publish(root, params, new_anchor)
    rep = portfolio.fork("p", anchor=str(new_anchor), portfolio=root / "portfolio")
    g = read_grid(home / BOOK)
    j = g[2].index("G2")
    assert (g[0][j], g[1][j]) == ("0.5", "UW")
    assert grid_column(g, j) == ["CCC", "FFF"]  # DDD stays deleted, FFF comes in
    assert rep["appeared"] == ["G4"] and "G3" in rep["lost"]
    assert "anchor_date: 2026-09-12" in (home / "input" / "forked_from.txt").read_text()


def test_portfolios_on_different_anchors(root):
    make(root)
    later = pd.Timestamp("2026-09-12").date()
    params = pd.read_csv(root / "params" / f"{ANCHOR}.csv")
    params.loc[params.ticker == "EEE", "float_cap"] = 30.0      # G3 grows
    params.to_csv(root / "params" / f"{later}.csv", index=False)
    publish(root, params, later)
    portfolio.new("q", portfolio=root / "portfolio")
    portfolio.fork("q", anchor=str(later), portfolio=root / "portfolio")
    assert weights(build(root, "q"))["G3"] == pytest.approx(0.25)
    assert weights(build(root, "p"))["G3"] == pytest.approx(0.1)  # p unaffected


def test_fork_missing_anchor_fails(root):
    portfolio.new("p", portfolio=root / "portfolio")
    with pytest.raises(BookError, match="no baseline to fork from"):
        portfolio.fork("p", anchor="2026-01-02", portfolio=root / "portfolio")


def test_screen_invalidates_restores(root):
    s = default_statement()
    s["screens"]["turnover"] = {"on": True, "min_pct": 0.1}
    home = make(root, screens=s["screens"])
    kw = {"portfolio": root / "portfolio", "params_dir": root / "params"}

    def live():
        g = read_grid(home / BOOK)
        return {t for j in range(len(g[2])) for t in grid_column(g, j)}

    excl = portfolio.screen("p", **kw)
    assert excl.ticker.tolist() == ["DDD"]
    assert "DDD" in live()
    portfolio.screen("p", invalidate_all=True, **kw)
    assert "DDD" not in live() and read_invalid(home) == ["DDD"]
    assert "DDD" in grid_column(read_grid(home / "input" / GRID), 1)  # still in universe
    build(root)                                                        # valid book builds

    # typing it back into the book while invalid is refused
    edit = read_grid(home / BOOK)
    write_grid(home / BOOK, edit[:3], [["AAA", "BBB"], ["CCC", "DDD"], ["EEE"]])
    with pytest.raises(BookError, match="invalidated names"):
        build(root)
    portfolio.screen("p", restore=["DDD"], **kw)
    assert read_invalid(home) == []
    build(root)

    # re-fork keeps an invalidated name out
    portfolio.screen("p", invalidate=["DDD"], **kw)
    rep = portfolio.fork("p", portfolio=root / "portfolio")
    assert "DDD" not in live() and rep["invalid_kept_out"] == []


def test_invalidating_last_name_rates_group_no(root):
    home = make(root)
    s = json.loads((home / "statement.json").read_text())
    s["screens"]["float_cap"] = {"on": True, "min_bn_vnd": 15 / 1e9}
    (home / "statement.json").write_text(json.dumps(s))
    portfolio.screen("p", invalidate=["EEE"], portfolio=root / "portfolio",
                     params_dir=root / "params")
    g = read_grid(home / BOOK)
    j = g[2].index("G3")
    assert (g[0][j], g[1][j]) == ("NO", "NO")
    assert weights(build(root))["G3"] == 0


def test_tactical_cannot_claim_invalidated(root):
    home = make(root)
    (home / "screen").mkdir()
    (home / "screen" / "invalid.csv").write_text(
        "ticker,group,screen,value,threshold,invalidated_at\nBBB,G1,turnover,0,0.1,x\n")
    edit_book(home, blank=["BBB"])
    (home / "tactical_group.json").write_text('{"tactical_group": "yes"}')
    write_grid(home / "tactical_group.csv", [["AV"], ["AV"], ["T1"]], [["BBB"]])
    with pytest.raises(BookError, match="invalidated names"):
        build(root)


def test_delete(root):
    home = make(root)
    with pytest.raises(BookError, match="--yes"):
        portfolio.delete("p", portfolio=root / "portfolio")
    assert home.exists()
    with pytest.raises(BookError, match="refusing"):
        portfolio.delete("baseline", yes=True, portfolio=root / "portfolio")
    portfolio.delete("p", yes=True, portfolio=root / "portfolio")
    assert not home.exists()
    with pytest.raises(BookError, match="no such portfolio"):
        portfolio.delete("p", yes=True, portfolio=root / "portfolio")


def test_new_refuses_existing_and_bad_names(root):
    make(root)
    with pytest.raises(BookError, match="already exists"):
        portfolio.new("p", portfolio=root / "portfolio")
    with pytest.raises(BookError, match="lowercase"):
        portfolio.new("Bad Name", portfolio=root / "portfolio")


@pytest.mark.parametrize("patch, msg", [
    ({"holdings": {"min": 30, "max": 20}}, "min <= max"),
    ({"rebalance": {"frequency": "1W", "drift_threshold": None}}, "frequency"),
    ({"rebalance": {"frequency": None, "drift_threshold": 8}}, "fraction"),
    ({"rebalance": {"frequency": None, "drift_threshold": None}}, "needs"),
    ({"screens": {"fol": {"on": True}}}, "unknown screen"),
])
def test_statement_validation(patch, msg):
    s = json.loads(json.dumps(default_statement()))
    s.update(patch)
    with pytest.raises(BookError, match=msg):
        validate_statement(s)
