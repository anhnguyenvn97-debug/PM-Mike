"""Portfolio lifecycle: new, fork, screen, delete.

Layout of a portfolio:

    portfolio/<name>/
        statement.json                  HAND (or UI) -- see common.py
        constraints.json                HAND (or UI) -- see common.py
        sector_constituents_custom.csv  HAND (or UI) -- the book
        tactical_group.json/.csv        HAND (or UI), optional
        input/                         derived by fork, never edit
            sector_constituents.csv     verbatim baseline grid of the anchor
            forked_from.txt             provenance; anchor_date is binding
        screen/
            exclusions.csv              derived by every screen run
            invalid.csv                 CLI/UI -- screen --invalidate/--restore
        target/                         derived by scr/target.py

The book is the investable universe. A ticker present in its group column is
investable; a blank is not. A group whose column is empty must be rated NO:
every command here that can empty a column sets it to NO with 0 active pp and
WARNs (the UI does the same). target.py FAILs on a hand-edited book that breaks
the rule, and on the active pp that no longer net to zero.

new <name>
    Create the folder, a default statement.json and constraints.json (all off).
    FAILs if the folder exists, so a typo can never overwrite a portfolio.
    Name: lowercase, digits, _.

fork <name> [--anchor YYYY-MM-DD]
    Snapshot portfolio/baseline/<anchor>/ into input/ and (re)build the book.
    Default anchor: the sticky anchor (index/anchor_date.json, see
    baseline.sticky_anchor). An anchor whose params or baseline is missing or
    stale is built on demand (baseline.ensure). Any session in market.db is
    accepted; one with fewer than 21 sessions behind it has no turnover, so its
    turnover screen fails every name.
    First fork: the book is a copy of the baseline grid, all AV at 0 pp.
    Re-fork: the old book is carried across BY GROUP NAME, never by position:
        rows 0-1   rating and active pp of every group that still exists
        deletions  names that were in the old input/ column but blank in the
                   old book stay out; names new to the baseline come in live,
                   because nobody has decided on them yet
    Groups that disappeared are reported with the rating they lose; their pp
    leave the net, so target.py FAILs until the book is rebalanced. Groups that
    appeared come in AV. Invalidated names are always kept out. Without an old
    input/ grid, deletions cannot be told apart from names that were never
    there, so only ratings carry and a WARN says so.

screen <name> [--invalidate T [T ...] | --invalidate-all] [--restore T [T ...]]
    Evaluate the statement's screens against data/params/<portfolio anchor>.csv
    for every name in the anchor universe (input/sector_constituents.csv) that
    is not already invalidated, and write screen/exclusions.csv:
        ticker, group, screen, value, threshold, in_book
    in_book is yes for a name the book still holds and no for one deleted in
    the Book step: the second kind is a name to leave out rather than one to
    act on, and it is listed so a curated book cannot hide it.
    Screens only suggest. --invalidate moves named tickers (they must be on the
    current suggestion list) into screen/invalid.csv and out of the book;
    --invalidate-all takes every one. An invalidated name stays in the baseline
    grid but cannot be made investable or claimed until restored. --restore
    drops names from invalid.csv; it does not re-add them to the book -- type
    the ticker back into its group column, or re-fork. A name failing turnover
    because it has under 21 sessions is listed with a blank value.

delete <name> --yes
    Remove portfolio/<name>/ and everything in it. Refuses the baseline folder
    and a missing portfolio. Without --yes it FAILs and deletes nothing.

Usage
    .venv\\Scripts\\python.exe scr\\portfolio.py new my_portfolio
    .venv\\Scripts\\python.exe scr\\portfolio.py fork my_portfolio --anchor 2026-08-29
    .venv\\Scripts\\python.exe scr\\portfolio.py screen my_portfolio --invalidate-all
    .venv\\Scripts\\python.exe scr\\portfolio.py delete my_portfolio --yes
"""
import argparse
import csv
import json
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

import baseline
import pandas as pd
from common import (
    BOOK,
    CONSTRAINTS,
    DB,
    FORKED,
    GRID,
    INVALID,
    PARAMS,
    PORTFOLIO,
    STATEMENT,
    BookError,
    default_constraints,
    default_statement,
    grid_column,
    load_statement,
    portfolio_anchor,
    read_grid,
    read_invalid,
    validate_statement,
    write_grid,
)

NAME = re.compile(r"^[a-z0-9_]+$")
EXCL = ["ticker", "group", "screen", "value", "threshold", "in_book"]
INVALID_COLS = ["ticker", "group", "screen", "value", "threshold", "invalidated_at"]


def new(name: str, portfolio: Path = PORTFOLIO,
        statement: dict | None = None) -> Path:
    if not NAME.match(name):
        raise BookError(f"portfolio name {name!r}: use lowercase letters, "
                        "digits and _ only")
    home = portfolio / name
    if home.exists():
        raise BookError(f"{home} already exists")
    s = validate_statement(statement or default_statement())
    home.mkdir(parents=True)
    (home / STATEMENT).write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    (home / CONSTRAINTS).write_text(json.dumps(default_constraints(), indent=2) + "\n",
                                    encoding="utf-8")
    print(f"OK    created {home}")
    print(f"      edit {STATEMENT}, then run scr/portfolio.py fork {name}")
    return home


def auto_no(header: list[list[str]], cols: list[list[str]]) -> list[str]:
    """Rate every empty, non-NO column NO in place; return the groups flipped."""
    flipped = []
    for j, g in enumerate(header[2]):
        if not cols[j] and header[1][j] != "NO":
            header[0][j], header[1][j] = "0", "NO"
            flipped.append(g)
    return flipped


def carry(base: list[list[str]], old_book: list[list[str]] | None,
          old_input: list[list[str]] | None) -> tuple[list, list, dict]:
    """Rebuild a book on a new baseline grid. Returns (header, columns, report)."""
    groups = base[2]
    report = {"carried": [], "appeared": [], "lost": {}, "deleted": {},
              "deletions_known": old_input is not None}
    if old_book is None:
        return ([["0"] * len(groups), ["AV"] * len(groups), list(groups)],
                [grid_column(base, j) for j in range(len(groups))], report)

    if len(old_book) < 3:
        raise BookError(f"existing {BOOK} has fewer than 3 rows")
    old_groups = old_book[2]
    old_at = {g: j for j, g in enumerate(old_groups)}
    in_at = {g: j for j, g in enumerate(old_input[2])} if old_input else {}

    def cell(row, j):
        return old_book[row][j] if j < len(old_book[row]) else ""

    row0, row1, cols = [], [], []
    for j, g in enumerate(groups):
        fresh = grid_column(base, j)
        if g in old_at:
            k = old_at[g]
            row0.append(cell(0, k) or "0")
            row1.append(cell(1, k) or "AV")
            report["carried"].append(g)
            if g in in_at:
                gone = set(grid_column(old_input, in_at[g])) - \
                    set(grid_column(old_book, k))
                kept_out = sorted(gone & set(fresh))
                if kept_out:
                    report["deleted"][g] = kept_out
                fresh = [t for t in fresh if t not in gone]
        else:
            row0.append("0")
            row1.append("AV")
            report["appeared"].append(g)
        cols.append(fresh)
    for g in old_groups:
        if g not in groups:
            report["lost"][g] = f"{cell(1, old_at[g])} ({cell(0, old_at[g]) or '0'} pp)"
    return [row0, row1, list(groups)], cols, report


def fork(name: str, anchor: str | None = None, portfolio: Path = PORTFOLIO,
         db: Path | None = None, params_dir: Path = PARAMS) -> dict:
    """Fork or re-fork onto an anchor. db=None never builds; the baseline must exist."""
    home, root = portfolio / name, portfolio / "baseline"
    if not home.is_dir():
        raise BookError(f"no such portfolio: {home}\n"
                        f"      run scr/portfolio.py new {name}")
    if anchor is None:
        if db is None:
            raise BookError("no anchor given and no database to resolve the sticky one")
        anchor = baseline.sticky_anchor(db)
    try:
        anchor = date.fromisoformat(str(anchor)).isoformat()
    except ValueError:
        raise BookError(f"anchor {anchor!r} is not a YYYY-MM-DD date")
    if db is not None:
        if pd.Timestamp(anchor).date() not in baseline.sessions_of(db):
            raise BookError(f"{anchor} is not a session in {db.name}")
        src = baseline.ensure(anchor, db=db, params_dir=params_dir, root=root)
    else:
        src = root / anchor
    if not (src / GRID).exists():
        raise BookError(f"no baseline to fork from: {src / GRID}\n"
                        f"      run scr/baseline.py --date {anchor}")

    base = read_grid(src / GRID)
    book_p, input_p = home / BOOK, home / "input" / GRID
    old_book = read_grid(book_p) if book_p.exists() else None
    old_input = read_grid(input_p) if input_p.exists() else None
    try:
        old_anchor = portfolio_anchor(home) if old_input else None
    except BookError:
        old_anchor = None

    header, cols, rep = carry(base, old_book, old_input)
    invalid = set(read_invalid(home))
    rep["invalid_kept_out"] = sorted(invalid & {t for c in cols for t in c})
    cols = [[t for t in c if t not in invalid] for c in cols]
    rep["auto_no"] = auto_no(header, cols)

    input_p.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src / GRID, input_p)
    (input_p.parent / FORKED).write_text(
        f"forked_from: portfolio/baseline/{anchor}/{GRID}\n"
        f"anchor_date: {anchor}\n"
        f"forked_at:   {datetime.now().astimezone():%Y-%m-%d %H:%M}\n", encoding="utf-8")
    write_grid(book_p, header, cols)

    if old_book is None:
        print(f"OK    forked {name} from baseline {anchor}; {BOOK} seeded all AV")
    else:
        moved = f" (was {old_anchor})" if old_anchor and old_anchor != anchor else ""
        print(f"OK    re-forked {name} onto baseline {anchor}{moved}; {BOOK} rebuilt")
        print(f"      ratings and active pp carried for {len(rep['carried'])} group(s)")
        for g, ts in rep["deleted"].items():
            print(f"      kept deleted  {g}: {ts}")
        if rep["appeared"]:
            print(f"      appeared, AV: {rep['appeared']}")
        for g, r in rep["lost"].items():
            print(f"WARN  group gone, rating lost: {g} = {r}")
        if not rep["deletions_known"]:
            print("WARN  no previous input/ grid; deletions could not be carried")
    if rep["invalid_kept_out"]:
        print(f"      invalidated, kept out: {rep['invalid_kept_out']}")
    for g in rep["auto_no"]:
        print(f"WARN  {g}: no investable name left, rated NO")
    if load_statement(home) is None:
        print(f"WARN  no {STATEMENT}; create one before building the target")
    return rep


def _write_invalid(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=INVALID_COLS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def _read_invalid_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def screen(name: str, invalidate: list[str] | None = None,
           invalidate_all: bool = False, restore: list[str] | None = None,
           portfolio: Path = PORTFOLIO, params_dir: Path = PARAMS) -> pd.DataFrame:
    home = portfolio / name
    statement = load_statement(home)
    if statement is None:
        raise BookError(f"no {STATEMENT} in {home}")
    book_p = home / BOOK
    if not book_p.exists():
        raise BookError(f"missing {book_p}\n      run scr/portfolio.py fork {name}")
    anchor = portfolio_anchor(home)
    pp = params_dir / f"{anchor}.csv"
    if not pp.exists():
        raise BookError(f"missing {pp}\n      run scr/params.py --date {anchor}")
    params = pd.read_csv(pp).set_index("ticker")
    inv_p = home / "screen" / INVALID
    inv_rows = _read_invalid_rows(inv_p)

    if restore:
        have = {r["ticker"] for r in inv_rows}
        stray = sorted(set(restore) - have)
        if stray:
            raise BookError(f"not invalidated, nothing to restore: {stray}")
        inv_rows = [r for r in inv_rows if r["ticker"] not in set(restore)]
        _write_invalid(inv_p, inv_rows)
        print(f"OK    restored {sorted(set(restore))}; not re-added to {BOOK}, "
              "type them back into their group column or re-fork")

    book = read_grid(book_p)
    live = {t: g for j, g in enumerate(book[2]) for t in grid_column(book, j)}
    base_p = home / "input" / GRID
    universe = dict(live)
    if base_p.exists():
        base = read_grid(base_p)
        universe = {t: g for j, g in enumerate(base[2])
                    for t in grid_column(base, j)} | live
    out_already = {r["ticker"] for r in inv_rows}

    sc = statement.get("screens", {})
    rows = []
    t_cfg, f_cfg = sc.get("turnover", {}), sc.get("float_cap", {})
    for t, g in sorted(universe.items()):
        if t in out_already or t not in params.index:
            continue
        held = "yes" if t in live else "no"
        if t_cfg.get("on"):
            v = params.at[t, "turnover_21_pct"]
            if pd.isna(v) or v < t_cfg["min_pct"]:
                rows.append([t, g, "turnover", None if pd.isna(v) else v,
                             t_cfg["min_pct"], held])
        if f_cfg.get("on"):
            v = params.at[t, "float_cap"] / 1e9
            if v < f_cfg["min_bn_vnd"]:
                rows.append([t, g, "float_cap", v, f_cfg["min_bn_vnd"], held])
    excl = pd.DataFrame(rows, columns=EXCL)

    out = home / "screen"
    out.mkdir(exist_ok=True)
    excl.to_csv(out / "exclusions.csv", index=False, lineterminator="\n",
                float_format="%.6g")
    on = [k for k in ("turnover", "float_cap") if sc.get(k, {}).get("on")]
    in_book = excl[excl["in_book"] == "yes"]["ticker"].nunique()
    print(f"OK    {name}  anchor {anchor}  screens on: {on or 'none'}")
    print(f"      {len(universe)} names screened, {len(live)} of them investable; "
          f"{excl['ticker'].nunique()} suggested, {in_book} in the book "
          f"-> {out / 'exclusions.csv'}")
    for r in excl.itertuples():
        val = "no 21-session history" if pd.isna(r.value) else f"{r.value:.4g}"
        tail = "" if r.in_book == "yes" else "  (not in the book)"
        print(f"      {r.ticker:<6} {r.group:<26} {r.screen:<10} {val} "
              f"< {r.threshold}{tail}")

    targets = sorted(set(excl["ticker"])) if invalidate_all else sorted(set(invalidate or []))
    if targets:
        stray = sorted(set(targets) - set(excl["ticker"]))
        if stray:
            raise BookError(f"not on the suggestion list, nothing invalidated: {stray}")
        stamp = f"{datetime.now().astimezone():%Y-%m-%d %H:%M}"
        for r in excl[excl["ticker"].isin(targets)].itertuples():
            inv_rows.append({"ticker": r.ticker, "group": r.group, "screen": r.screen,
                             "value": "" if pd.isna(r.value) else f"{r.value:.6g}",
                             "threshold": r.threshold, "invalidated_at": stamp})
        _write_invalid(inv_p, inv_rows)
        cols = [[t for t in grid_column(book, j) if t not in targets]
                for j in range(len(book[2]))]
        header = [list(book[0]), list(book[1]), list(book[2])]
        flipped = auto_no(header, cols)
        write_grid(book_p, header, cols)
        outside = sorted(set(targets) - set(live))
        print(f"OK    invalidated {targets} -> {inv_p}; removed from {BOOK}")
        if outside:
            print(f"      already out of the book, now barred from returning: {outside}")
        for g in flipped:
            print(f"WARN  {g}: no investable name left, rated NO")
    return excl


def delete(name: str, yes: bool = False, portfolio: Path = PORTFOLIO) -> Path:
    home = portfolio / name
    if name == "baseline" or not NAME.match(name):
        raise BookError(f"refusing to delete {home}")
    if not home.is_dir():
        raise BookError(f"no such portfolio: {home}")
    if not yes:
        raise BookError(f"this deletes {home} and everything in it; pass --yes")
    shutil.rmtree(home)
    print(f"OK    deleted {home}")
    return home


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("new").add_argument("name")
    f = sub.add_parser("fork")
    f.add_argument("name")
    f.add_argument("--anchor", help="YYYY-MM-DD; default: sticky anchor")
    s = sub.add_parser("screen")
    s.add_argument("name")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--invalidate", nargs="+", metavar="TICKER")
    g.add_argument("--invalidate-all", action="store_true")
    s.add_argument("--restore", nargs="+", metavar="TICKER")
    d = sub.add_parser("delete")
    d.add_argument("name")
    d.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "new":
            new(a.name)
        elif a.cmd == "fork":
            fork(a.name, a.anchor, db=DB)
        elif a.cmd == "screen":
            screen(a.name, a.invalidate, a.invalidate_all, a.restore)
        else:
            delete(a.name, a.yes)
    except BookError as e:
        print(f"FAIL  {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
