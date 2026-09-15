import duckdb
import ingest
import pandas as pd
import pytest
from conftest import INDEX_HEADERS, make_index_rows, make_rows


def build_with_index(drop, tmp_path, idx_rows, stock_sessions=5):
    files = [drop(make_rows(sessions=stock_sessions), "stock.xlsx"),
             drop(idx_rows, "bench.xlsx", INDEX_HEADERS)]
    return ingest.build(files, tmp_path / "m.db", tmp_path / "m.txt")


def test_index_drop_loads_beside_stock_drop(drop, tmp_path, capsys):
    rows = make_index_rows(sessions=5)
    rows.append([99, "VN30", 4, rows[-1][3].replace(day=25), None, None, None])  # non-session
    assert build_with_index(drop, tmp_path, rows) == 0

    con = duckdb.connect(tmp_path / "m.db", read_only=True)
    idx = con.execute("SELECT code, count(*) FROM index_prices GROUP BY 1 ORDER BY 1").fetchall()
    loads = con.execute("SELECT file_name, kind, ticker_count, dropped FROM loads "
                        "ORDER BY 1").fetchall()
    stock = con.execute("SELECT count(*) FROM prices").fetchone()[0]
    con.close()
    assert idx == [("VN30", 5), ("VNINDEX", 5)]
    assert loads == [("bench.xlsx", "index", 2, 1), ("stock.xlsx", "stock", 2, 0)]
    assert stock == 10
    assert "WARN" not in capsys.readouterr().out
    assert "VNINDEX" in (tmp_path / "m.txt").read_text(encoding="utf-8")


def test_index_short_or_late_coverage_warns(drop, tmp_path, capsys):
    rows = make_index_rows(codes=("VN100",), sessions=7)[2:]  # misses 2, runs 2 past
    assert build_with_index(drop, tmp_path, rows) == 0
    out = capsys.readouterr().out
    assert "VN100: 2 session(s) after the last stock session" in out
    assert "VN100: missing 2 stock session(s)" in out


def test_index_date_off_the_stock_calendar_fails(drop, tmp_path):
    stock = make_rows(sessions=5)
    stock = [r for r in stock if r[3] != stock[4][3]]  # remove the 3rd session
    idx = drop(make_index_rows(sessions=5), "bench.xlsx", INDEX_HEADERS)
    db = tmp_path / "m.db"
    assert ingest.build([drop(stock, "stock.xlsx"), idx], db) == 1
    assert not db.exists()


@pytest.mark.parametrize("bad", ["dup", "zero"])
def test_invalid_index_drop_fails(drop, tmp_path, bad):
    rows = make_index_rows(sessions=3)
    if bad == "dup":
        rows.append(list(rows[0]))
    else:
        rows[0][4] = 0
    assert build_with_index(drop, tmp_path, rows, stock_sessions=3) == 1


def test_overlapping_index_drops_fail(drop, tmp_path):
    rows = make_index_rows(sessions=2)
    files = [drop(rows, "a.xlsx", INDEX_HEADERS), drop(rows, "b.xlsx", INDEX_HEADERS)]
    assert ingest.build(files, tmp_path / "m.db") == 1


def test_unknown_export_fails(drop, tmp_path):
    path = drop(make_rows(sessions=1))
    raw = pd.read_excel(path, header=None)
    raw.iloc[3, 1] = "Symbol"
    raw.to_excel(path, header=False, index=False)
    assert ingest.build([path], tmp_path / "m.db") == 1


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
