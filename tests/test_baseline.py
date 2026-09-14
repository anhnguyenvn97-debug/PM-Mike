import os
import time

import baseline
import ingest
import pandas as pd
from common import ALLOC, GRID, newer_than, read_grid
from conftest import make_rows

MAP_COLS = ["Ticker", "Company_name", "ICB L2 sector", "Exclusive group"]


def touch(path, seconds_ahead):
    t = time.time() + seconds_ahead
    os.utime(path, (t, t))


def write_map(path, groups: dict):
    pd.DataFrame([[t, f"{t} Corp", "X", g] for t, g in groups.items()],
                 columns=MAP_COLS).to_csv(path, index=False)


def test_newer_than(tmp_path):
    a, b, c = (tmp_path / n for n in "abc")
    for p in (a, b):
        p.write_text("x")
    touch(a, 0)
    touch(b, 10)
    assert newer_than(a, b, c, None) == [b]       # missing and None inputs ignored
    assert newer_than(b, a) == []
    assert newer_than(c, a, b) == [a, b]          # missing target: rebuild from all


def test_ensure_rebuilds_after_group_map_edit(drop, tmp_path):
    db = tmp_path / "m.db"
    assert ingest.build([drop(make_rows(sessions=25))], db) == 0
    gmap, fol = tmp_path / "map.csv", tmp_path / "fol.csv"
    write_map(gmap, {"AAA": "G", "BBB": "G"})
    root, pdir = tmp_path / "baseline", tmp_path / "params"
    anchor = "2026-01-29"
    kw = {"db": db, "params_dir": pdir, "root": root, "map_path": gmap, "fol_path": fol}

    out = baseline.ensure(anchor, **kw)
    assert read_grid(out / GRID)[2] == ["G"]
    built = (out / ALLOC).stat().st_mtime
    baseline.ensure(anchor, **kw)                  # fresh: nothing rewritten
    assert (out / ALLOC).stat().st_mtime == built

    for p in (pdir / f"{anchor}.csv", out / ALLOC):  # age the derived files
        touch(p, -10)
    write_map(gmap, {"AAA": "G", "BBB": "H"})     # regroup by hand
    baseline.ensure(anchor, **kw)
    assert read_grid(out / GRID)[2] == ["G", "H"]
    assert baseline.params_stale(pdir / f"{anchor}.csv", db, gmap, fol) == []
