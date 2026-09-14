"""Ingest: data/fiinpro/*.xlsx|csv -> data/market.db

The only market input. FiinPro Data Portal exports are downloaded by hand and
dropped into data/fiinpro/ (top level only; archive/ is ignored). This script
owns every write to market.db. It derives nothing FiinPro already returned --
it locates the header, validates, normalises names, and loads.

Adjusted prices are adjusted AS OF THE EXTRACT DATE, so rows from different
extracts must never be merged into one series. The database is therefore
REBUILT FROM SCRATCH on every run from whatever sits in data/fiinpro/. The
build lands in a temp file and is swapped in only if every drop validates, so
a bad drop cannot destroy a good database.

Banner and footer rows are tolerated: the header is the first row whose first
cell is "No", and rows without a ticker or a parseable date are dropped.
Rows with no close price are non-sessions (pre-listing padding, suspension, or
a date that has not traded yet) and are dropped and counted.

Sector and FOL are NOT stored. They are hand-edited files (index/) and are
joined by scr/params.py at build time, so editing them never needs a re-ingest.

Every run rewrites data/market.txt with the schema and current state.

Usage:
    .venv\\Scripts\\python.exe scr\\ingest.py            # every drop
    .venv\\Scripts\\python.exe scr\\ingest.py f.xlsx     # named drop(s) only
"""
import hashlib
import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DROP = DATA / "fiinpro"
DB = DATA / "market.db"
DICT = DATA / "market.txt"

# FiinPro header -> schema. Headers carry a "\nUnit: ..." suffix that is
# stripped before lookup. "No" is a row ordinal and is dropped.
COLUMNS = {
    "Ticker": "ticker",
    "Company Name": "company_name",
    "Date": "trade_date",
    "Reference (D)": "reference",
    "Ceiling (D)": "ceiling",
    "Floor (D)": "floor",
    "Open (D)": "open_raw",
    "Highest (D)": "high_raw",
    "Lowest (D)": "low_raw",
    "Close Price (D)": "close_raw",
    "Open Adjusted (D)": "open_adj",
    "Highest Adjusted (D)": "high_adj",
    "Lowest Adjusted (D)": "low_adj",
    "Close Adjusted (D)": "close_adj",
    "Total trading volume (D)": "volume",
    "Total trading value (D)": "value",
    "Current Outstanding Shares": "outstanding_shares",
    "Free Float Shares": "free_float",
    "Market Capitalization": "market_cap",
}

PRICE_COLS = ["reference", "ceiling", "floor",
              "open_raw", "high_raw", "low_raw", "close_raw",
              "open_adj", "high_adj", "low_adj", "close_adj"]
SCHEMA = [("trade_date", "DATE"), ("ticker", "VARCHAR")] + \
    [(c, "DOUBLE") for c in PRICE_COLS] + [
    ("volume", "BIGINT"), ("value", "DOUBLE"),
    ("outstanding_shares", "BIGINT"), ("free_float", "BIGINT"),
    ("market_cap", "DOUBLE"),
]

# Ceiling band is the only exchange marker FiinPro ships, and it is correct per
# session (a transfer between exchanges shows up on the day it happens).
# Listing days widen to +-20% and resolve to NULL.
EXCHANGE_CASE = """CASE
        WHEN reference IS NULL OR reference = 0 THEN NULL
        WHEN ceiling / reference - 1 < 0.085 THEN 'HOSE'
        WHEN ceiling / reference - 1 < 0.125 THEN 'HNX'
        WHEN ceiling / reference - 1 < 0.175 THEN 'UPCOM'
    END"""


def read_drop(path: Path) -> tuple[pd.DataFrame, int]:
    """Load one FiinPro export, normalised to SCHEMA. Returns (rows, dropped)."""
    if path.suffix.lower() in (".xlsx", ".xls"):
        raw = pd.read_excel(path, header=None)
    else:
        raw = pd.read_csv(path, header=None)

    first = raw.iloc[:, 0].astype(str).str.strip()
    hits = first.index[first.eq("No")]
    if not len(hits):
        raise ValueError("no header row (first cell 'No') found")
    h = hits[0]

    df = raw.iloc[h + 1:].copy()
    df.columns = [str(c).split("\n")[0].strip() for c in raw.iloc[h]]
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        shown = ", ".join(missing[:3])
        more = f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""
        raise ValueError(f"not a FiinPro price export -- missing {shown}{more}")

    df = df[list(COLUMNS)].rename(columns=COLUMNS)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df = df[df["ticker"].notna() & df["trade_date"].notna()].copy()  # footer
    df["trade_date"] = df["trade_date"].dt.date
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()

    for c, typ in SCHEMA:
        if typ in ("DOUBLE", "BIGINT"):
            df[c] = pd.to_numeric(df[c])

    blank = df["close_raw"].isna()
    return df[~blank].reset_index(drop=True), int(blank.sum())


def validate(df: pd.DataFrame) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). A non-empty error list blocks the build."""
    errs, warns = [], []

    dup = df.duplicated(["trade_date", "ticker"], keep=False)
    if dup.any():
        pairs = df.loc[dup, ["ticker", "trade_date"]].head(5).values.tolist()
        errs.append(f"{dup.sum()} duplicate (date,ticker) rows, e.g. {pairs}")

    nulls = [c for c in PRICE_COLS + ["volume", "value", "outstanding_shares",
                                      "free_float", "market_cap"]
             if df[c].isna().any()]
    if nulls:
        errs.append(f"null values in {nulls}")

    for tag in ("raw", "adj"):
        lo, hi = df[f"low_{tag}"], df[f"high_{tag}"]
        for c in (f"open_{tag}", f"close_{tag}"):
            out = df.loc[(df[c] < lo) | (df[c] > hi), "ticker"]
            if len(out):
                errs.append(f"{c} outside [low_{tag}, high_{tag}]: "
                            f"{sorted(set(out))[:8]}")

    if (df["volume"] < 0).any() or (df["value"] < 0).any():
        errs.append("negative volume or value")

    over = df.loc[df["free_float"] > df["outstanding_shares"], "ticker"]
    if len(over):
        errs.append(f"free_float > outstanding_shares: {sorted(set(over))[:8]}")

    # Vendor cap is a QA column only -- float cap is computed from close_raw.
    # A disagreement is recorded, never blocking: it cannot be fixed at source.
    rel = ((df["close_raw"] * df["outstanding_shares"] - df["market_cap"]).abs()
           / df["market_cap"])
    bad = rel > 1e-4
    if bad.any():
        sample = df.loc[bad, "ticker"].value_counts().head(5).to_dict()
        warns.append(f"market_cap != close_raw*shares on {bad.sum()} row(s); "
                     f"top tickers {sample}")
    return errs, warns


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    cols = ", ".join(f"{n} {t}" for n, t in SCHEMA)
    con.execute(f"CREATE TABLE prices ({cols}, PRIMARY KEY (trade_date, ticker))")
    con.execute("CREATE TABLE tickers "
                "(ticker VARCHAR PRIMARY KEY, company_name VARCHAR)")
    con.execute("CREATE TABLE loads (file_name VARCHAR, sha256 VARCHAR, "
                "row_count BIGINT, dropped BIGINT, date_min DATE, date_max DATE, "
                "ticker_count INTEGER, loaded_at TIMESTAMP)")
    con.execute(f"CREATE VIEW prices_v AS "
                f"SELECT *, {EXCHANGE_CASE} AS exchange FROM prices")


def load_drop(con: duckdb.DuckDBPyConnection, path: Path) -> int:
    df, dropped = read_drop(path)
    errs, warns = validate(df)
    for w in warns:
        print(f"WARN  {path.name}: {w}")
    if errs:
        print(f"FAIL  {path.name}: {len(errs)} check(s) failed")
        for e in errs:
            print(f"  - {e}")
        return 1

    prices = df[[n for n, _ in SCHEMA]]
    con.register("new_rows", prices)
    clash = con.execute("SELECT count(*) FROM prices JOIN new_rows "
                        "USING (trade_date, ticker)").fetchone()[0]
    if clash:
        # Two drops covering the same session can carry different adjustment
        # states. Refuse rather than pick one silently.
        con.unregister("new_rows")
        print(f"FAIL  {path.name}: {clash:,} (date,ticker) rows already loaded "
              f"from another drop -- remove or archive the overlap")
        return 1

    con.execute("INSERT INTO prices SELECT * FROM new_rows")
    con.register("new_names", df[["ticker", "company_name"]]
                 .drop_duplicates("ticker"))
    con.execute("INSERT OR REPLACE INTO tickers SELECT * FROM new_names")
    con.execute(
        "INSERT INTO loads SELECT ?, ?, ?, ?, min(trade_date), max(trade_date), "
        "count(DISTINCT ticker), now()::TIMESTAMP FROM new_rows",
        [path.name, hashlib.sha256(path.read_bytes()).hexdigest(),
         len(prices), dropped])
    con.unregister("new_rows")
    con.unregister("new_names")

    print(f"OK    {path.name}: {len(prices):,} rows "
          f"({dropped:,} non-session dropped), "
          f"{prices['ticker'].nunique()} tickers, "
          f"{prices['trade_date'].min()} -> {prices['trade_date'].max()}")
    return 0


def write_dictionary(con: duckdb.DuckDBPyConnection, db: Path, out: Path) -> None:
    now = pd.Timestamp.now().floor("s")
    n, dmin, dmax, nt, nd = con.execute(
        "SELECT count(*), min(trade_date), max(trade_date), "
        "count(DISTINCT ticker), count(DISTINCT trade_date) FROM prices"
    ).fetchone()
    L = [f"{db.name} - data dictionary",
         f"generated {now} by scr/ingest.py (do not edit)",
         f"size {db.stat().st_size / 1e6:.1f} MB", "",
         "STATE",
         f"  prices   {n:,} rows | {nt} tickers | {nd} sessions | {dmin} -> {dmax}",
         "",
         "SCHEMA  prices (PK trade_date, ticker) - VND, share counts unadjusted"]
    notes = {
        "reference": "prior session official close",
        "ceiling": "price band upper; band width encodes exchange",
        "close_raw": "official close; float cap uses this",
        "open_adj": "back-adjusted, as of extract date",
        "close_adj": "back-adjusted, as of extract date; returns only",
        "value": "matched turnover; liquidity screens use this",
        "free_float": "share count, time-varying",
        "market_cap": "vendor field, QA only",
    }
    for name, typ in SCHEMA:
        L.append(f"  {name:<19}{typ:<9}{notes.get(name, '')}")
    L += ["", "SCHEMA  tickers (ticker PK, company_name)",
          "SCHEMA  loads   (file_name, sha256, row_count, dropped, date_min,",
          "                 date_max, ticker_count, loaded_at)", ""]

    ex = con.execute("SELECT exchange, count(DISTINCT ticker) FROM prices_v "
                     f"WHERE trade_date = '{dmax}' GROUP BY 1 ORDER BY 2 DESC"
                     ).fetchall()
    L += ["VIEW  prices_v = prices + exchange from ceiling band",
          "  <8.5% HOSE | <12.5% HNX | <17.5% UPCOM | else NULL (listing day)",
          f"  on {dmax}: " + " ".join(f"{e or 'NULL'}={c}" for e, c in ex), ""]

    L += ["COVERAGE  shortest histories"]
    for t, c, a, b in con.execute(
            "SELECT ticker, count(*) n, min(trade_date), max(trade_date) "
            "FROM prices GROUP BY 1 ORDER BY n, ticker LIMIT 5").fetchall():
        L.append(f"  {t:<6}{c:>5} sessions  {a} -> {b}")
    L.append("")

    L += ["BUILD  drops in this rebuild"]
    for f, r, dr, a, b, tc in con.execute(
            "SELECT file_name, row_count, dropped, date_min, date_max, "
            "ticker_count FROM loads ORDER BY file_name").fetchall():
        L += [f"  {f}",
              f"      {r:,} rows | {dr:,} dropped | {tc} tickers | {a} -> {b}"]
    L.append("")

    bad = con.execute(
        "SELECT ticker, count(*), max(abs(close_raw * outstanding_shares "
        "- market_cap) / market_cap) FROM prices WHERE abs(close_raw * "
        "outstanding_shares - market_cap) / market_cap > 1e-4 "
        "GROUP BY 1 ORDER BY 2 DESC, 1").fetchall()
    L += ["QUALITY  vendor market_cap vs close_raw * outstanding_shares",
          f"  {sum(r[1] for r in bad):,} of {n:,} rows off by >1e-4"]
    L += [f"  {t:<6}{c:>5} rows  worst {w:>7.2%}" for t, c, w in bad[:5]]
    L.append("")
    out.write_text("\n".join(L), encoding="utf-8")


def build(files: list[Path], db: Path, dictionary: Path | None = None) -> int:
    """Rebuild db from files, all-or-nothing. Returns 0 on success."""
    tmp = db.with_name(db.name + ".building")
    for leftover in (tmp, tmp.with_name(tmp.name + ".wal")):
        leftover.unlink(missing_ok=True)

    rc = 0
    con = duckdb.connect(tmp)
    try:
        create_schema(con)
        for path in files:
            if not path.exists():
                print(f"FAIL  no such file: {path}")
                rc = 1
                continue
            try:
                rc |= load_drop(con, path)
            except Exception as exc:  # noqa: BLE001 - any drop can be malformed
                print(f"FAIL  {path.name}: {exc}")
                rc = 1
        con.execute("CHECKPOINT")
    finally:
        con.close()

    if rc:
        for leftover in (tmp, tmp.with_name(tmp.name + ".wal")):
            leftover.unlink(missing_ok=True)
        print("      build aborted, existing database left untouched")
        return rc

    tmp.replace(db)
    if dictionary is not None:
        con = duckdb.connect(db, read_only=True)
        try:
            write_dictionary(con, db, dictionary)
        finally:
            con.close()
    print(f"      rebuilt {db.name} ({db.stat().st_size / 1e6:.1f} MB) "
          f"from {len(files)} drop(s)")
    return 0


def main(argv: list[str]) -> int:
    if argv:
        files = [Path(a) if Path(a).is_absolute() else DROP / a for a in argv]
    else:
        files = sorted(p for p in DROP.iterdir()
                       if p.is_file()
                       and p.suffix.lower() in (".xlsx", ".xls", ".csv")
                       and not p.name.startswith("~$"))
    if not files:
        print(f"FAIL  no drops in {DROP}")
        return 1
    return build(files, DB, DICT)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
