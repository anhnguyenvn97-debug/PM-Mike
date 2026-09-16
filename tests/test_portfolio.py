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
    publish(tmp_path, params, ANCHOR)
    return tmp_path


def publish(root, params, anchor):
    """Write baseline/<anchor>/."""
    baseline.write(root / "portfolio" / "baseline" / str(anchor),
                   *baseline.build(params, anchor))


def make(root, name="p", **statement):
    s = default_statement()
    s.update(statement)
    portfolio.new(name, portfolio=root / "portfolio", statement=s)
    portfolio.fork(name, anchor=str(ANCHOR), portfolio=root / "portfolio")
    return root / "portfolio" / name


def constrain(home, **blocks):
    c = default_constraints()
    for k, v in blocks.items():
        c[k] = {**c[k], **({} if k == "active" else {"on": True}), **v}
    (home / "constraints.json").write_text(json.dumps(c))


def holdings(res):
    return dict(zip(res["holdings"].ticker, res["holdings"].target_weight))


def edit_book(home, ratings=None, pp=None, blank=()):
    """ratings {group: rating}, pp {group: active pp}; unnamed pp are 0."""
    g = read_grid(home / BOOK)
    for grp, r in (ratings or {}).items():
        j = g[2].index(grp)
        g[1][j] = r
        g[0][j] = f"{(pp or {}).get(grp, 0):g}"
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
    assert read_grid(root / "portfolio" / "baseline" / str(ANCHOR) / GRID)[2] == \
        ["G1", "G2", "G3"]


def test_untilted_target_equals_baseline(root):
    home = make(root)
    assert read_grid(home / BOOK)[:2] == [["0", "0", "0"], ["AV", "AV", "AV"]]
    res = build(root)
    assert weights(res) == pytest.approx({"G1": 0.6, "G2": 0.3, "G3": 0.1})
    h = dict(zip(res["holdings"].ticker, res["holdings"].target_weight))
    assert h["AAA"] == pytest.approx(0.4)


def test_active_pp_and_deletion(root):
    home = make(root)
    edit_book(home, ratings={"G1": "OW1", "G2": "UW1"}, pp={"G1": 3, "G2": -3},
              blank=["BBB"])
    res = build(root)
    assert weights(res) == pytest.approx({"G1": 0.63, "G2": 0.27, "G3": 0.1})
    h = holdings(res)
    assert "BBB" not in h and h["AAA"] == pytest.approx(0.63)  # budget stays
    a = res["allocation"].set_index("group")
    assert a.at["G1", "active_vs_neutral_pp"] == pytest.approx(3)
    assert a.at["G2", "active_vs_baseline_pp"] == pytest.approx(-3)


def test_no_leaves_the_neutral(root):
    home = make(root)
    edit_book(home, ratings={"G1": "OW2", "G2": "UW1", "G3": "NO"}, pp={"G1": 3, "G2": -3})
    res = build(root)
    # neutral G1 .6/.9, G2 .3/.9; +-3 pp on top
    assert weights(res) == pytest.approx({"G1": 2 / 3 + 0.03, "G2": 1 / 3 - 0.03, "G3": 0})
    a = res["allocation"].set_index("group")
    assert a.at["G1", "neutral_weight"] == pytest.approx(2 / 3)
    assert a.at["G1", "active_vs_baseline_pp"] == pytest.approx(100 * (2 / 3 + 0.03 - 0.6))


def test_active_checks_list_every_fault(root):
    home = make(root)
    constrain(home, active={"budget_pp": 5})
    # G1 OW1 +4 breaks its range, G3 UW3 -12 breaks its range and the floor
    # (neutral 10%), the net is -8, and 1/2 (4 + 12) = 8 > 5
    edit_book(home, ratings={"G1": "OW1", "G3": "UW3"}, pp={"G1": 4, "G3": -12})
    with pytest.raises(BookError) as e:
        build(root)
    msg = str(e.value)
    for part in ("G1: OW1 allows +0 to +3 pp, has +4", "G3: UW3 allows -9 to +0 pp",
                 "G3: -12 pp takes it below 0%", "net to -8.000", "use 8.00 of a 5 pp"):
        assert part in msg
    res = target.compute("p", portfolio=root / "portfolio", params_dir=root / "params",
                         strict=False)
    assert len(res["active"]["faults"]) == 5
    assert weights(res)["G3"] == 0 and "EEE" not in holdings(res)  # floored, renormalised
    assert weights(res)["G1"] == pytest.approx(0.64 / 0.94)


def test_group_at_zero_holds_nothing_and_range_reports_the_floor(root):
    home = make(root)
    later = pd.Timestamp("2026-09-12").date()
    params = pd.read_csv(root / "params" / f"{ANCHOR}.csv")
    params.loc[params.ticker == "EEE", "float_cap"] = 90 * 0.05 / 0.95    # G3 = 5%
    params.to_csv(root / "params" / f"{later}.csv", index=False)
    publish(root, params, later)
    portfolio.fork("p", anchor=str(later), portfolio=root / "portfolio")
    edit_book(home, ratings={"G1": "OW2", "G3": "UW2"}, pp={"G1": 5, "G3": -5})
    res = target.compute("p", portfolio=root / "portfolio", params_dir=root / "params")
    assert weights(res)["G3"] == 0 and "EEE" not in holdings(res)
    assert sum(holdings(res).values()) == pytest.approx(1)
    rng = res["active"]["range"]
    assert rng["G3"] == pytest.approx((-5, 0)) and rng["G1"] == (0, 6) and rng["G2"] == (0, 0)


def test_multiplier_book_fails_with_a_hint(root):
    home = make(root)
    g = read_grid(home / BOOK)
    write_grid(home / BOOK, [["OW", "AV", "AV"], ["OW", "AV", "AV"], g[2]],
               [grid_column(g, j) for j in range(3)])
    with pytest.raises(BookError, match="still uses multipliers"):
        build(root)
    write_grid(home / BOOK, [["1.25", "0", "0"], ["OW1", "AV", "AV"], g[2]],
               [grid_column(g, j) for j in range(3)])
    with pytest.raises(BookError, match=r"net to \+1\.250"):  # an old multiplier as pp
        build(root)
    write_grid(home / BOOK, [["x", "0", "0"], ["AV", "AV", "AV"], g[2]],
               [grid_column(g, j) for j in range(3)])
    with pytest.raises(BookError, match="'x' is not a number"):
        build(root)


def test_tactical_claim_moves_budget(root):
    home = make(root)
    (home / "tactical_group.json").write_text('{"tactical_group": "yes"}')
    write_grid(home / "tactical_group.csv", [["0"], ["AV"], ["T1"]], [["BBB"]])
    res = build(root)
    assert weights(res) == pytest.approx({"G1": 0.4, "G2": 0.3, "G3": 0.1, "T1": 0.2})
    write_grid(home / "tactical_group.csv", [["2"], ["OW1"], ["T1"]], [["BBB"]])
    edit_book(home, ratings={"G2": "UW1"}, pp={"G2": -2})
    assert weights(build(root)) == pytest.approx({"G1": 0.4, "G2": 0.28, "G3": 0.1, "T1": 0.22})


def test_compute_matches_build_and_writes_nothing(root):
    home = make(root)
    edit_book(home, ratings={"G1": "OW1", "G2": "UW1"}, pp={"G1": 2, "G2": -2},
              blank=["BBB"])
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
    book[1][j] = "NO"
    tac = {"on": True, "grid": [["0"], ["AV"], ["T1"], ["BBB"]]}
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


def test_waterfill_respreads_until_clean():
    w = {"A": 0.5, "B": 0.3, "C": 0.15, "D": 0.05}
    out, bound = target.waterfill(w, dict.fromkeys(w, 0.3))
    # A clips; .7 over B:C:D lifts B past .3; .4 over C:D gives C .3, D .1
    assert out == pytest.approx({"A": 0.3, "B": 0.3, "C": 0.3, "D": 0.1})
    assert bound == {"A", "B"}


def test_cap_too_tight_fails(root):
    home = make(root)
    constrain(home, sector={"max": 0.3})
    with pytest.raises(BookError, match="cannot fill"):
        build(root)


def test_per_group_cap_applies_to_tactical(root):
    home = make(root)
    (home / "tactical_group.json").write_text('{"tactical_group": "yes"}')
    write_grid(home / "tactical_group.csv", [["0"], ["AV"], ["T1"]], [["BBB"]])
    constrain(home, sector={"max": 1.0, "per_group": {"T1": 0.1}})
    # T1 0.2 -> 0.1; 0.1 spread pro-rata over G1 .4, G2 .3, G3 .1
    assert weights(build(root)) == pytest.approx(
        {"G1": 0.45, "G2": 0.3375, "G3": 0.1125, "T1": 0.1})


def test_per_group_unknown_name_fails(root):
    home = make(root)
    constrain(home, sector={"max": 1.0, "per_group": {"Nope": 0.1}})
    with pytest.raises(BookError, match="names no known group"):
        build(root)


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
    ({"active": {"budget_pp": 0}}, "percentage points"),
    ({"active": {"budget_pp": 0.2, "on": True}}, "unknown key"),
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
    edit_book(home, ratings={"G2": "UW1", "G3": "OW1"}, pp={"G2": -2.5, "G3": 2.5},
              blank=["DDD"])
    # New baseline: G3 disappears, G4 appears, FFF joins G2.
    params = pd.DataFrame([
        {"ticker": t, "group": g, "float_cap": 1.0, "close_raw": 1.0, "adj_factor": 1.0}
        for g, ts in {"G1": ["AAA", "BBB"], "G2": ["CCC", "DDD", "FFF"],
                      "G4": ["EEE"]}.items() for t in ts])
    new_anchor = pd.Timestamp("2026-09-12").date()
    params.to_csv(root / "params" / f"{new_anchor}.csv", index=False)
    publish(root, params, new_anchor)
    rep = portfolio.fork("p", anchor=str(new_anchor), portfolio=root / "portfolio")
    g = read_grid(home / BOOK)
    j = g[2].index("G2")
    assert (g[0][j], g[1][j]) == ("-2.5", "UW1")
    assert grid_column(g, j) == ["CCC", "FFF"]  # DDD stays deleted, FFF comes in
    assert rep["appeared"] == ["G4"] and rep["lost"] == {"G3": "OW1 (2.5 pp)"}
    with pytest.raises(BookError, match=r"net to -2\.500"):  # G3's +2.5 left with it
        build(root)
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
    assert excl.in_book.tolist() == ["yes"]
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
    rep = portfolio.fork("p", anchor=str(ANCHOR), portfolio=root / "portfolio")
    assert "DDD" not in live() and rep["invalid_kept_out"] == []


def test_screen_covers_the_universe_not_just_the_book(root):
    s = default_statement()
    s["screens"]["turnover"] = {"on": True, "min_pct": 0.1}
    home = make(root, screens=s["screens"])
    kw = {"portfolio": root / "portfolio", "params_dir": root / "params"}
    edit_book(home, blank=("DDD",))          # deleted in the Book step, never screened

    excl = portfolio.screen("p", **kw)
    assert excl.ticker.tolist() == ["DDD"]   # a curated book cannot hide it
    assert excl.in_book.tolist() == ["no"]

    # invalidating a name already out of the book bars it from coming back
    portfolio.screen("p", invalidate=["DDD"], **kw)
    assert read_invalid(home) == ["DDD"]
    assert portfolio.screen("p", **kw).empty     # and it is not suggested twice


def test_invalidating_last_name_rates_group_no(root):
    home = make(root)
    s = json.loads((home / "statement.json").read_text())
    s["screens"]["float_cap"] = {"on": True, "min_bn_vnd": 15 / 1e9}
    (home / "statement.json").write_text(json.dumps(s))
    portfolio.screen("p", invalidate=["EEE"], portfolio=root / "portfolio",
                     params_dir=root / "params")
    g = read_grid(home / BOOK)
    j = g[2].index("G3")
    assert (g[0][j], g[1][j]) == ("0", "NO")
    assert weights(build(root))["G3"] == 0


def test_tactical_cannot_claim_invalidated(root):
    home = make(root)
    (home / "screen").mkdir()
    (home / "screen" / "invalid.csv").write_text(
        "ticker,group,screen,value,threshold,invalidated_at\nBBB,G1,turnover,0,0.1,x\n")
    edit_book(home, blank=["BBB"])
    (home / "tactical_group.json").write_text('{"tactical_group": "yes"}')
    write_grid(home / "tactical_group.csv", [["0"], ["AV"], ["T1"]], [["BBB"]])
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
    ({"rebalance": {"frequency": "1Q", "drift_threshold": None,
                    "breach_tolerance": 10}}, "breach_tolerance"),
    ({"screens": {"fol": {"on": True}}}, "unknown screen"),
])
def test_statement_validation(patch, msg):
    s = json.loads(json.dumps(default_statement()))
    s.update(patch)
    with pytest.raises(BookError, match=msg):
        validate_statement(s)
