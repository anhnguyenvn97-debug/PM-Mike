import json

import params
import pytest
import target
from common import (
    BOOK,
    DECISION_LOG,
    DECISIONS,
    BookError,
    default_constraints,
    default_screens,
    default_statement,
    load_decisions,
    load_screens,
    setup_hash,
    validate_constraints,
    validate_decision,
    validate_screens,
    validate_statement,
)
from conftest import (
    UNIVERSE,
    build,
    compute,
    constrain,
    holdings,
    make,
    make_db,
    set_book,
    weights,
)

import portfolio


def test_untilted_target_equals_the_float_cap_universe(root):
    home = make(root)
    assert not (home / BOOK).exists() and (home / "screens.json").exists()
    res = build(root)
    assert weights(res) == pytest.approx({"G1": 0.6, "G2": 0.3, "G3": 0.1})
    assert holdings(res)["AAA"] == pytest.approx(0.4)
    assert any("no book.json" in m for m in res["messages"])
    assert "priced_as_of: 2026-09-11" in (home / "target" / "built_from.txt").read_text()


def test_active_pp_and_deletion(root):
    home = make(root)
    set_book(home, ratings={"G1": "OW1", "G2": "UW1"}, pp={"G1": 3, "G2": -3}, drop=["BBB"])
    res = build(root)
    assert weights(res) == pytest.approx({"G1": 0.63, "G2": 0.27, "G3": 0.1})
    h = holdings(res)
    assert "BBB" not in h and h["AAA"] == pytest.approx(0.63)  # budget stays
    a = res["allocation"].set_index("group")
    assert a.at["G1", "active_vs_neutral_pp"] == pytest.approx(3)
    assert a.at["G2", "active_vs_baseline_pp"] == pytest.approx(-3)


def test_no_leaves_the_neutral(root):
    home = make(root)
    set_book(home, ratings={"G1": "OW2", "G2": "UW1", "G3": "NO"}, pp={"G1": 3, "G2": -3})
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
    set_book(home, ratings={"G1": "OW1", "G3": "UW3"}, pp={"G1": 4, "G3": -12})
    with pytest.raises(BookError) as e:
        build(root)
    msg = str(e.value)
    for part in ("G1: OW1 allows +0 to +3 pp, has +4", "G3: UW3 allows -9 to +0 pp",
                 "G3: -12 pp takes it below 0%", "net to -8.000", "use 8.00 of a 5 pp"):
        assert part in msg
    res = compute(root, strict=False)
    assert len(res["active"]["faults"]) == 5
    assert weights(res)["G3"] == 0 and "EEE" not in holdings(res)  # floored, renormalised
    assert weights(res)["G1"] == pytest.approx(0.64 / 0.94)


def test_group_at_zero_holds_nothing_and_range_reports_the_floor(root):
    home = make(root)
    make_db(root, fcap={("2026-09-11", "EEE"): 90 * 0.05 / 0.95})     # G3 = 5% on the 11th
    set_book(home, ratings={"G1": "OW2", "G3": "UW2"}, pp={"G1": 5, "G3": -5})
    res = compute(root)
    assert weights(res)["G3"] == 0 and "EEE" not in holdings(res)
    assert sum(holdings(res).values()) == pytest.approx(1)
    rng = res["active"]["range"]
    assert rng["G3"] == pytest.approx((-5, 0)) and rng["G1"] == (0, 6) and rng["G2"] == (0, 0)
    # the day before, G3 is 10% and the same book holds EEE at 5%
    assert holdings(compute(root, as_of="2026-09-10"))["EEE"] == pytest.approx(0.05)


def test_bad_rating_and_pp_fail(root):
    home = make(root)
    spec = set_book(home)
    for g, s, msg in (("G1", {"rating": "OW", "investable": ["AAA"]}, "unknown rating 'OW'"),
                      ("G1", {"rating": "AV", "pp": "x", "investable": ["AAA"]},
                       "'x' must be a number")):
        bad = json.loads(json.dumps(spec))
        bad["groups"][g] = s
        (home / BOOK).write_text(json.dumps(bad))
        with pytest.raises(BookError, match=msg):
            build(root)
    bad = json.loads(json.dumps(spec))
    bad["groups"]["G1"] = {"rating": "OW1", "pp": 1.25, "investable": ["AAA"]}
    (home / BOOK).write_text(json.dumps(bad))
    with pytest.raises(BookError, match=r"net to \+1\.250"):
        build(root)


def test_tactical_claim_moves_budget(root):
    home = make(root)
    set_book(home, tactical={"on": True, "groups": [
        {"name": "T1", "rating": "AV", "pp": 0, "members": ["BBB"]}]})
    assert weights(build(root)) == pytest.approx({"G1": 0.4, "G2": 0.3, "G3": 0.1, "T1": 0.2})
    set_book(home, ratings={"G2": "UW1"}, pp={"G2": -2}, tactical={"on": True, "groups": [
        {"name": "T1", "rating": "OW1", "pp": 2, "members": ["BBB"]}]})
    assert weights(build(root)) == pytest.approx({"G1": 0.4, "G2": 0.28, "G3": 0.1, "T1": 0.22})
    set_book(home, tactical={"on": False, "groups": [
        {"name": "T1", "rating": "OW1", "pp": 2, "members": ["BBB"]}]})
    res = compute(root, strict=False)
    assert "T1" not in weights(res) and "defined, not applied" in res["tac_note"]


def test_compute_matches_build_and_writes_nothing(root):
    home = make(root)
    set_book(home, ratings={"G1": "OW1", "G2": "UW1"}, pp={"G1": 2, "G2": -2}, drop=["BBB"])
    constrain(home, stock={"max": 0.5})
    res = compute(root)
    assert not (home / "target").exists()
    built = build(root)
    assert res["holdings"].equals(built["holdings"])
    assert res["allocation"].equals(built["allocation"])


def test_compute_overrides_replace_files(root):
    make(root)
    spec = portfolio.default_book({g: sorted(m) for g, m in UNIVERSE.items()})
    spec["groups"]["G3"]["rating"] = "NO"
    spec["tactical"] = {"on": True, "groups": [{"name": "T1", "rating": "AV", "pp": 0,
                                                "members": ["BBB"]}]}
    cons = {**default_constraints(), "sector": {"on": True, "max": 0.45, "per_group": {}}}
    res = compute(root, spec=spec, constraints=cons)
    # G3 NO: G1 .4, G2 .3, T1 .2 over .9 -> G1 .444, G2 .333, T1 .222; no cap binds
    assert weights(res) == pytest.approx({"G1": 0.4 / 0.9, "G2": 0.3 / 0.9,
                                          "T1": 0.2 / 0.9, "G3": 0})
    assert weights(compute(root)) == pytest.approx({"G1": 0.6, "G2": 0.3, "G3": 0.1})
    with pytest.raises(BookError, match="tactical overlay is on but has no groups"):
        compute(root, spec={**spec, "tactical": {"on": True, "groups": []}})


def test_as_of_snaps_to_the_last_session_on_or_before(root):
    make(root)
    assert str(compute(root, as_of="2026-08-01")["as_of"]) == "2026-07-31"   # a Saturday
    assert str(compute(root, as_of="2026-12-31")["as_of"]) == "2026-09-11"   # after the history
    assert compute(root, as_of="2026-08-01")["requested"] == "2026-08-01"
    with pytest.raises(BookError, match="before the first session"):
        compute(root, as_of="2026-01-01")
    with pytest.raises(BookError, match="not a YYYY-MM-DD"):
        compute(root, as_of="01/08/2026")


def test_a_name_not_trading_is_dropped_with_a_message(root):
    home = make(root)
    set_book(home)                                               # BBB investable
    make_db(root, fcap={("2026-09-11", "BBB"): None})           # no print on the 11th
    res = compute(root)
    assert "BBB" not in holdings(res)
    assert any("G1: ['BBB'] not trading on 2026-09-11" in m for m in res["messages"])
    assert weights(res)["G1"] == pytest.approx(0.4 / 0.8)
    assert "BBB" in holdings(compute(root, as_of="2026-09-10"))
    assert "BBB" in portfolio.read_book(home)["groups"]["G1"]["investable"]   # spec kept


def test_evaluate_drops_lost_groups_and_rates_gained_ones_no(root):
    home = make(root)
    set_book(home, ratings={"G2": "UW1", "G3": "OW1"}, pp={"G2": -2.5, "G3": 2.5}, drop=["DDD"])
    # new universe: EEE moves to G4, FFF lists in G2
    make_db(root, universe={**UNIVERSE, "G2": {**UNIVERSE["G2"], "FFF": 10.0}},
            regroup={"EEE": "G4"})
    res = compute(root, strict=False)
    rep = res["reconcile"]
    assert rep["lost_groups"] == {"G3": "OW1 (2.5 pp)"} and rep["appeared"] == ["G4"]
    assert res["spec"]["groups"]["G4"]["rating"] == "NO"
    assert "FFF" not in holdings(res) and "DDD" not in holdings(res)   # no auto-add
    assert any("net to -2.500" in f for f in res["active"]["faults"])
    with pytest.raises(BookError, match=r"net to -2\.500"):
        compute(root)


def test_write_book_checks_the_group_map(root):
    home = make(root)
    base = portfolio.default_book({g: sorted(m) for g, m in UNIVERSE.items()})
    for patch, msg in (
            ({"groups": {**base["groups"], "G1": {"rating": "AV", "investable": ["CCC"]}}},
             "belong to another group"),
            ({"groups": {**base["groups"], "GX": {"rating": "AV", "investable": []}}},
             "unknown group"),

            ({"tactical": {"on": True, "groups": [{"name": "T1", "members": ["AAA"]},
                                                  {"name": "T2", "members": ["AAA"]}]}},
             "claimed by both T1 and T2"),
            ({"tactical": {"on": True, "groups": [{"name": "G2", "members": ["AAA"]}]}},
             "already a group name")):
        with pytest.raises(BookError, match=msg):
            portfolio.write_book(home, {**base, **patch})
    assert not (home / BOOK).exists()


def test_write_book_normalizes_and_applies_auto_no(root):
    home = make(root)
    flipped = portfolio.write_book(home, {
        "groups": {"G1": {"rating": "UW2", "pp": -4.123456, "investable": ["BBB", "AAA"]},
                   "G2": {"rating": "UW1", "pp": -1, "investable": ["CCC"]},
                   "G3": {"rating": "AV", "pp": None, "investable": []}},
        "tactical": {"on": True, "groups": [
            {"name": "T1", "rating": "OW2", "pp": 4.1235, "members": ["CCC"]},
            {"name": "T2", "rating": "OW1", "pp": 2, "members": []}]}})
    assert flipped == ["T2", "G2", "G3"]
    b = portfolio.read_book(home)
    assert b["groups"]["G1"] == {"rating": "UW2", "pp": -4.1235, "investable": ["AAA", "BBB"]}
    assert b["groups"]["G2"] == {"rating": "NO", "pp": 0, "investable": ["CCC"]}
    assert b["tactical"]["groups"][1] == {"name": "T2", "rating": "NO", "pp": 0, "members": []}
    # neutral G1 .6 / .8, T1 CCC's .2 / .8; the rest are NO
    assert weights(build(root)) == pytest.approx(
        {"G1": 0.75 - 0.041235, "T1": 0.25 + 0.041235, "G2": 0, "G3": 0, "T2": 0})


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
    set_book(home, tactical={"on": True, "groups": [
        {"name": "T1", "rating": "AV", "pp": 0, "members": ["BBB"]}]})
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


def test_delete(root):
    home = make(root)
    with pytest.raises(BookError, match="--yes"):
        portfolio.delete("p", portfolio=root / "portfolio")
    assert home.exists()
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
    ({"screens": {"turnover": {"on": True}}}, "moved to screens.json"),
])
def test_statement_validation(patch, msg):
    s = json.loads(json.dumps(default_statement()))
    s.update(patch)
    with pytest.raises(BookError, match=msg):
        validate_statement(s)


@pytest.mark.parametrize("patch, msg", [
    ({"momentum": {"on": True}}, "unknown screen"),
    ({"fol": {"on": "yes"}}, "true or false"),
    ({"turnover": {"on": True, "min_pct": -1}}, "non-negative"),
    ({"float_cap": {"on": True, "min": 3}}, "unknown key"),
])
def test_screens_validation(patch, msg):
    with pytest.raises(BookError, match=msg):
        validate_screens({**default_screens(), **patch})


# ---------- screens are flags (D62) ----------

def test_screen_flags_never_remove_a_name(root):
    home = make(root)
    make_db(root, fol={"AAA": 0.49, "CCC": 0.2})
    (home / "screens.json").write_text(json.dumps({
        "turnover": {"on": True, "min_pct": 0.1}, "float_cap": {"on": True, "min_bn_vnd": 15 / 1e9},
        "fol": {"on": True, "min_limit_pct": 30}}))
    flags = portfolio.screen("p", portfolio=root / "portfolio", db=root / "m.db")
    got = {(f["t"], f["screen"], f["why"]) for f in flags}
    assert got == {("DDD", "turnover", "below"), ("DDD", "float_cap", "below"),
                   ("EEE", "float_cap", "below"), ("CCC", "fol", "below"),
                   ("BBB", "fol", "no data"), ("DDD", "fol", "no data"),
                   ("EEE", "fol", "no data")}
    assert len(build(root)["holdings"]) == 5
    assert load_screens(home)["fol"]["min_limit_pct"] == 30


def test_turnover_with_short_history_flags_no_data(root):
    make(root)
    frame = params.at(root / "m.db", "2026-06-03")     # 3 sessions in
    flags = portfolio.screen_flags(frame, ["AAA"], validate_screens(
        {"turnover": {"on": True, "min_pct": 0.1}}))
    assert flags == [{"t": "AAA", "screen": "turnover", "value": None, "threshold": 0.1,
                      "why": "no data"}]


# ---------- decisions (D56, D60, D61) ----------

def record(root, eff, kind=None, note=""):
    return portfolio.record_decision(root / "portfolio" / "p", eff, kind, note,
                                     db=root / "m.db")


def test_first_decision_is_inception_and_later_ones_need_a_kind(root):
    home = make(root)
    set_book(home, ratings={"G1": "OW1", "G3": "UW1"}, pp={"G1": 3, "G3": -3})
    d = record(root, "2026-07-04", "period", "t0")           # a Saturday; kind forced
    assert (d["kind"], d["priced_as_of"]) == ("inception", "2026-07-03")
    assert d["setup_hash"] == setup_hash(home) and len(d["holdings"]) == 5
    assert sum(h["w"] for h in d["holdings"]) == pytest.approx(1)
    with pytest.raises(BookError, match="before inception 2026-07-04"):
        record(root, "2026-07-01", "active")
    with pytest.raises(BookError, match="period or active"):
        record(root, "2026-08-03")
    record(root, "2026-08-03", "active", "tilt")
    record(root, "2026-07-04", "active", "redo")              # re-record: still inception
    assert [(x["id"], x["kind"], x["note"]) for x in load_decisions(home)] == [
        ("2026-07-04", "inception", "redo"), ("2026-08-03", "active", "tilt")]
    assert sorted(p.name for p in (home / DECISIONS).iterdir()) == [
        "2026-07-04.json", "2026-08-03.json", DECISION_LOG]
    assert (home / DECISIONS / DECISION_LOG).read_text().splitlines()[0] == \
        "id,effective,recorded_at,priced_as_of,kind,setup_hash,note"


def test_record_refuses_a_book_that_fails_the_strict_build(root):
    home = make(root)
    set_book(home, ratings={"G1": "OW1"}, pp={"G1": 3})
    with pytest.raises(BookError, match="net to"):
        record(root, "2026-09-01")
    assert not (home / DECISIONS).exists()


def test_reset_archives_and_the_next_record_is_inception(root):
    home = make(root)
    record(root, "2026-07-01")
    record(root, "2026-08-03", "period")
    dest = portfolio.reset(home)
    assert load_decisions(home) == [] and (dest / DECISION_LOG).exists()
    assert sorted(p.name for p in dest.iterdir()) == ["2026-07-01.json", "2026-08-03.json",
                                                      DECISION_LOG]
    assert record(root, "2026-06-15", "period")["kind"] == "inception"
    with pytest.raises(BookError, match="no decisions"):
        portfolio.reset(make(root, "q"))


def test_setup_hash_follows_statement_and_constraints_not_screens(root):
    home = make(root)
    h = setup_hash(home)
    (home / "screens.json").write_text(json.dumps({"turnover": {"on": True, "min_pct": 1}}))
    assert setup_hash(home) == h
    constrain(home, stock={"max": 0.5})
    assert setup_hash(home) != h


def test_evaluate_carries_by_name():
    cols = {"G1": ["AAA"], "G2": ["CCC", "DDD"], "G4": ["FFF"]}
    prof = {"groups": {"G1": {"rating": "OW1", "pp": 3, "investable": ["AAA", "BBB"]},
                       "G2": {"rating": "AV", "pp": 0, "investable": ["CCC", "DDD"]},
                       "G3": {"rating": "UW1", "pp": -3, "investable": ["EEE"]}},
            "tactical": {"on": True, "groups": [{"name": "T", "rating": "AV", "pp": 0,
                                                 "members": ["DDD", "ZZZ"]}]}}
    spec, rep = portfolio.evaluate(prof, cols)
    assert spec["groups"]["G1"] == {"rating": "OW1", "pp": 3, "investable": ["AAA"]}
    assert spec["groups"]["G2"]["investable"] == ["CCC", "DDD"]
    assert spec["groups"]["G4"] == {"rating": "NO", "pp": 0, "investable": []}
    assert "G3" not in spec["groups"]
    assert spec["tactical"]["groups"][0]["members"] == ["DDD"]
    assert rep == {"dropped_names": {"G1": ["BBB"], "T": ["ZZZ"]},
                   "lost_groups": {"G3": "UW1 (-3 pp)"}, "appeared": ["G4"]}


def test_load_decisions_orders_validates_and_fills_kind(tmp_path):
    folder = tmp_path / DECISIONS
    folder.mkdir()
    rows = ["2026-09-02", "2026-08-15", "2026-08-01"]
    (folder / DECISION_LOG).write_text(
        "id,effective,recorded_at,anchor,note\n" + "".join(f"{i},{i},,,\n" for i in rows))
    for i in rows:
        (folder / f"{i}.json").write_text(json.dumps(
            {"id": i, "effective": i, "groups": {}, "tactical": {"on": False, "groups": []}}))
    got = load_decisions(tmp_path)
    assert [(d["id"], d["kind"]) for d in got] == [
        ("2026-08-01", "inception"), ("2026-08-15", "period"), ("2026-09-02", "period")]
    assert load_decisions(tmp_path / "none") == []
    with pytest.raises(BookError, match="unknown rating 'XX'"):
        validate_decision({"id": "a", "effective": "2026-01-01",
                           "groups": {"G1": {"rating": "XX"}}})
    with pytest.raises(BookError, match="not YYYY-MM-DD"):
        validate_decision({"id": "a", "effective": "01/01/2026", "groups": {}})
    with pytest.raises(BookError, match="kind 'rebalance'"):
        validate_decision({"id": "a", "effective": "2026-01-01", "groups": {},
                           "kind": "rebalance"})
    with pytest.raises(BookError, match="holdings must be a list"):
        validate_decision({"id": "a", "effective": "2026-01-01", "groups": {},
                           "holdings": [{"t": "AAA"}]})
    (folder / "2026-09-02.json").unlink()
    with pytest.raises(BookError, match="is missing"):
        load_decisions(tmp_path)
