import duckdb
import ingest
from conftest import make_rows


def test_builds_from_drop_with_banner_and_footer(drop, tmp_path):
    rows = make_rows(sessions=3)
    rows.append([99, "AAA", "AAA Corp", rows[-1][3].replace(day=20)]
                + [None] * 12 + [1_000_000, 400_000, None, None])  # non-session
    db = tmp_path / "m.db"
    assert ingest.build([drop(rows)], db) == 0

    con = duckdb.connect(db, read_only=True)
    n, t = con.execute("SELECT count(*), count(DISTINCT ticker) FROM prices").fetchone()
    ex = con.execute("SELECT DISTINCT exchange FROM prices_v").fetchall()
    dropped = con.execute("SELECT dropped FROM loads").fetchone()[0]
    con.close()
    assert (n, t, dropped) == (6, 2, 1)
    assert ex == [("HOSE",)]


def test_invalid_drop_leaves_existing_db_untouched(drop, tmp_path):
    db = tmp_path / "m.db"
    assert ingest.build([drop(make_rows(sessions=2), "good.xlsx")], db) == 0
    before = db.read_bytes()

    bad = make_rows(sessions=2)
    bad[0][17] = bad[0][16] + 1  # free_float > outstanding_shares
    assert ingest.build([drop(bad, "bad.xlsx")], db) == 1
    assert db.read_bytes() == before
    assert not (tmp_path / "m.db.building").exists()


def test_overlapping_drops_fail(drop, tmp_path):
    rows = make_rows(sessions=2)
    files = [drop(rows, "a.xlsx"), drop(rows, "b.xlsx")]
    assert ingest.build(files, tmp_path / "m.db") == 1


def test_missing_column_fails(drop, tmp_path):
    path = drop(make_rows(sessions=1))
    import pandas as pd
    raw = pd.read_excel(path, header=None)
    raw.iloc[3, 19] = "Something else"
    raw.to_excel(path, header=False, index=False)
    assert ingest.build([path], tmp_path / "m.db") == 1
