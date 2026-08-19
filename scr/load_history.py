"""History loader: data/fiinpro/*.xlsx|csv -> data/local_history.db

The manual half of the pipeline. FiinPro Data Portal exports are downloaded by
hand, stripped of their banner/footer, and dropped into data/fiinpro/. This
script owns every write to the DuckDB store; it derives nothing that FiinPro
already returned -- it validates, normalises names, and upserts.

Adjusted prices are adjusted AS OF THE EXTRACT DATE: a file pulled next quarter
carries different *_adj values for the same historical sessions, because
intervening dividends and splits have been folded back in. Nothing here may
therefore merge old rows with new ones and call the result a series.

So the database is REBUILT FROM SCRATCH on every run: data/fiinpro/ is the
source of truth and the .db is a derived artifact, exactly as the .gitignore
treats them. That keeps the adjusted series internally consistent, stops a
deleted export from leaving orphan rows behind, and avoids the dead-block
growth that in-place replacement causes. The build lands in a temp file and is
swapped in only if every export validates, so a bad drop cannot destroy a good
database.

Every run rewrites data/local_history.txt with the schema and current state.

Usage:
    .venv\\Scripts\\python.exe scr\\load_history.py            # all drops
    .venv\\Scripts\\python.exe scr\\load_history.py f.xlsx     # one file
"""
import hashlib
import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DROP = DATA / "fiinpro"
DB = DATA / "local_history.db"
TMP = DATA / "local_history.db.building"
DICT = DATA / "local_history.txt"

# FiinPro export header -> schema. Headers carry a "\nUnit: ..." suffix that is
# stripped before lookup. "No" is a row ordinal and is dropped.
COLUMNS = {
    "Ticker": "ticker",
    "Company Name": "company_name",
    "Date": "trade_date",
    "Reference (D)": "reference",
    "Ceiling (D)": "ceiling",
    "Floor (D)": "floor",
    "Open (D)": "open_raw",
    "Close Price (D)": "close_raw",
    "Open Adjusted (D)": "open_adj",
    "Highest Adjusted (D)": "high_adj",
    "Lowest Adjusted (D)": "low_adj",
    "Close Adjusted (D)": "close_adj",
    "Total trading volume (D)": "volume",
    "Current Outstanding Shares": "outstanding_shares",
    "Free Float Shares": "free_float",
    "Market Capitalization": "market_cap",
    "Total trading value (D)": "value",
}

PRICE_COLS = ["reference", "ceiling", "floor", "open_raw", "close_raw",
              "open_adj", "high_adj", "low_adj", "close_adj"]
PRICES_SCHEMA = [
    ("trade_date", "DATE"), ("ticker", "VARCHAR"),
    ("reference", "DOUBLE"), ("ceiling", "DOUBLE"), ("floor", "DOUBLE"),
    ("open_raw", "DOUBLE"), ("close_raw", "DOUBLE"),
    ("open_adj", "DOUBLE"), ("high_adj", "DOUBLE"),
    ("low_adj", "DOUBLE"), ("close_adj", "DOUBLE"),
    ("volume", "BIGINT"), ("value", "DOUBLE"),
    ("outstanding_shares", "BIGINT"), ("free_float", "BIGINT"),
    ("market_cap", "DOUBLE"),
]

# Ceiling band is the only exchange marker FiinPro ships, and unlike a joined
# snapshot it is correct per session: BSR reads UPCOM before its 2025-01-17
# HOSE transfer and HOSE after. Listing days widen to +-20% and resolve to NULL.
EXCHANGE_CASE = """CASE
        WHEN reference IS NULL OR reference = 0 THEN NULL
        WHEN ceiling / reference - 1 < 0.085 THEN 'HOSE'
        WHEN ceiling / reference - 1 < 0.125 THEN 'HNX'
        WHEN ceiling / reference - 1 < 0.175 THEN 'UPCOM'
    END"""


def read_export(path: Path) -> pd.DataFrame:
    """Load one stripped FiinPro export and normalise it to the schema."""
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)

    df.columns = [str(c).split("\n")[0].strip() for c in df.columns]
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        shown = ", ".join(missing[:3])
        more = f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""
        raise ValueError(f"not a FiinPro price export -- missing {shown}{more}")

    df = df[list(COLUMNS)].rename(columns=COLUMNS)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()

    # Pre-listing padding: FiinPro emits calendar rows before a ticker listed,
    # carrying share counts but no prices. They are not sessions.
    pre = df["close_adj"].isna()
    df = df[~pre].copy()

    for c, typ in PRICES_SCHEMA:
        if typ in ("DOUBLE", "BIGINT"):
            df[c] = pd.to_numeric(df[c])
    return df, int(pre.sum())


def validate(df: pd.DataFrame) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). A non-empty error list blocks the load."""
    errs, warns = [], []

    dup = df.duplicated(["trade_date", "ticker"], keep=False)
    if dup.any():
        pairs = df.loc[dup, ["ticker", "trade_date"]].head(5).values.tolist()
        errs.append(f"{dup.sum()} duplicate (date,ticker) rows, e.g. {pairs}")

    nulls = [c for c in PRICE_COLS + ["volume", "value"] if df[c].isna().any()]
    if nulls:
        errs.append(f"null values in {nulls}")

    # Adjusted OHLC is the Yang-Zhang input; it must be internally coherent.
    for c in ("open_adj", "close_adj"):
        out = df.loc[(df[c] < df["low_adj"]) | (df[c] > df["high_adj"]), "ticker"]
        if len(out):
            errs.append(f"{c} outside [low_adj, high_adj]: {sorted(set(out))[:8]}")

    if (df["volume"] < 0).any() or (df["value"] < 0).any():
        errs.append("negative volume or value")

    over = df.loc[df["free_float"] > df["outstanding_shares"], "ticker"]
    if len(over):
        errs.append(f"free_float > outstanding_shares: {sorted(set(over))[:8]}")

    # Transcription check. Official close is exchange-dependent: UPCoM reports
    # the session VWAP (what *_adj carries), everywhere else the last matched
    # price. Reported as a warning -- a stale provider row must not block a
    # bulk history load the user cannot repair at source.
    band = df["ceiling"] / df["reference"] - 1
    official = df["close_adj"].where(band >= 0.125, df["close_raw"])
    rel = ((official * df["outstanding_shares"] - df["market_cap"]).abs()
           / df["market_cap"])
    bad = rel > 1e-4
    if bad.any():
        sample = df.loc[bad, "ticker"].value_counts().head(5).to_dict()
        warns.append(f"market_cap != official_close*shares on {bad.sum()} row(s); "
                     f"top tickers {sample}")
    return errs, warns


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    cols = ", ".join(f"{n} {t}" for n, t in PRICES_SCHEMA)
    con.execute(f"CREATE TABLE IF NOT EXISTS prices ({cols}, "
                f"PRIMARY KEY (trade_date, ticker))")
    con.execute("CREATE TABLE IF NOT EXISTS tickers "
                "(ticker VARCHAR PRIMARY KEY, company_name VARCHAR)")
    con.execute("CREATE TABLE IF NOT EXISTS loads ("
                "file_name VARCHAR, sha256 VARCHAR, row_count BIGINT, "
                "replaced BIGINT, date_min DATE, date_max DATE, "
                "ticker_count INTEGER, loaded_at TIMESTAMP)")
    con.execute(f"CREATE OR REPLACE VIEW prices_v AS "
                f"SELECT *, {EXCHANGE_CASE} AS exchange FROM prices")


def write_dictionary(con: duckdb.DuckDBPyConnection) -> None:
    """Rewrite local_history.txt from the live database state."""
    now = pd.Timestamp.now().floor("s")
    L = ["local_history.db - data dictionary",
         f"generated {now} by scr/load_history.py (do not edit)",
         f"size {DB.stat().st_size / 1e6:.1f} MB",
         ""]

    n, dmin, dmax, nt, nd = con.execute(
        "SELECT count(*), min(trade_date), max(trade_date), "
        "count(DISTINCT ticker), count(DISTINCT trade_date) FROM prices"
    ).fetchone()
    L += ["STATE",
          (f"  prices   {n:,} rows | {nt} tickers | {nd} sessions "
           f"| {dmin} -> {dmax}"),
          f"  tickers  {con.execute('SELECT count(*) FROM tickers').fetchone()[0]:,} rows",
          f"  loads    {con.execute('SELECT count(*) FROM loads').fetchone()[0]:,} rows",
          ""]

    L += ["SCHEMA  prices (PK trade_date, ticker) - VND, unadjusted share counts"]
    notes = {
        "reference": "prior session official close",
        "ceiling": "price band upper limit; band width encodes exchange",
        "floor": "price band lower limit",
        "open_raw": "opening price, unadjusted",
        "close_raw": "last matched price, unadjusted",
        "open_adj": "back-adjusted, as of extract date",
        "high_adj": "back-adjusted, as of extract date",
        "low_adj": "back-adjusted, as of extract date",
        "close_adj": "back-adjusted; UPCoM reports session VWAP",
        "volume": "matched shares",
        "value": "matched turnover",
        "outstanding_shares": "time-varying, on the row",
        "free_float": "time-varying, on the row",
        "market_cap": "official close * outstanding_shares",
    }
    for name, typ in PRICES_SCHEMA:
        L.append(f"  {name:<19}{typ:<9}{notes.get(name, '')}")
    L += ["",
          "SCHEMA  tickers", "  ticker             VARCHAR  PK",
          "  company_name       VARCHAR",
          "",
          "SCHEMA  loads  (build manifest: one row per export in this rebuild)",
          "  file_name, sha256, row_count, replaced, date_min, date_max,",
          "  ticker_count, loaded_at",
          ""]

    ex = con.execute("SELECT exchange, count(*) FROM prices_v "
                     "GROUP BY 1 ORDER BY 2 DESC").fetchall()
    L += ["VIEW  prices_v = prices + exchange derived from ceiling band",
          "  <8.5% HOSE | <12.5% HNX | <17.5% UPCOM | else NULL (listing day)",
          "  " + " ".join(f"{e or 'NULL'}={c:,}" for e, c in ex),
          ""]

    L += ["COVERAGE  sessions per ticker"]
    rows = con.execute(
        "SELECT ticker, count(*) n, min(trade_date) a, max(trade_date) b "
        "FROM prices GROUP BY 1 ORDER BY n ASC LIMIT 5").fetchall()
    for t, c, a, b in rows:
        L.append(f"  {t:<6}{c:>6} sessions  {a} -> {b}")
    L += [f"  ... {nt} tickers total, {nd} distinct sessions", ""]

    L += ["BUILD  every export in data/fiinpro/ at the last rebuild"]
    for f, r, rp, a, b, tc, ts in con.execute(
            "SELECT file_name, row_count, replaced, date_min, date_max, "
            "ticker_count, loaded_at FROM loads ORDER BY file_name").fetchall():
        L.append(f"  {f}")
        over = f", {rp:,} overlapped an earlier file" if rp else ""
        L.append(f"      {r:,} rows | {tc} tickers | {a} -> {b}{over}")

    # Rows where FiinPro's own cap column disagrees with its price and share
    # count. Not repairable at source, so it is recorded rather than blocked --
    # anything reading market_cap or float cap should know where it is soft.
    bad_n, = con.execute(
        "SELECT count(*) FROM prices_v WHERE market_cap > 0 AND abs("
        "CASE WHEN exchange = 'UPCOM' THEN close_adj ELSE close_raw END "
        "* outstanding_shares - market_cap) / market_cap > 1e-4").fetchone()
    L += ["QUALITY  market_cap vs official_close * outstanding_shares",
          f"  {bad_n:,} of {n:,} rows off by >1e-4"]
    for t, c, worst in con.execute(
            "SELECT ticker, count(*), max(abs("
            "CASE WHEN exchange = 'UPCOM' THEN close_adj ELSE close_raw END "
            "* outstanding_shares - market_cap) / market_cap) FROM prices_v "
            "WHERE market_cap > 0 AND abs("
            "CASE WHEN exchange = 'UPCOM' THEN close_adj ELSE close_raw END "
            "* outstanding_shares - market_cap) / market_cap > 1e-4 "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 5").fetchall():
        L.append(f"  {t:<6}{c:>6} rows  worst {worst:>8.1%}")

    L += ["",
          "NOT STORED",
          "  exchange  derived in prices_v; never joined from a snapshot",
          "  sector    absent from the export; join at query time when needed",
          "  high_raw / low_raw  FiinPro ships High/Low adjusted only",
          ""]
    DICT.write_text("\n".join(L), encoding="utf-8")


def load_file(con: duckdb.DuckDBPyConnection, path: Path) -> int:
    df, dropped = read_export(path)
    errs, warns = validate(df)
    for w in warns:
        print(f"WARN  {path.name}: {w}")
    if errs:
        print(f"FAIL  {path.name}: {len(errs)} check(s) failed")
        for e in errs:
            print(f"  - {e}")
        return 1

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    prices = df[[n for n, _ in PRICES_SCHEMA]]
    con.register("new_rows", prices)
    con.register("new_names", df[["ticker", "company_name"]].drop_duplicates())

    replaced = con.execute(
        "SELECT count(*) FROM prices p JOIN new_rows n USING (trade_date, ticker)"
    ).fetchone()[0]
    con.execute("DELETE FROM prices WHERE EXISTS (SELECT 1 FROM new_rows n "
                "WHERE n.trade_date = prices.trade_date "
                "AND n.ticker = prices.ticker)")
    con.execute("INSERT INTO prices SELECT * FROM new_rows")
    con.execute("DELETE FROM tickers WHERE ticker IN (SELECT ticker FROM new_names)")
    con.execute("INSERT INTO tickers SELECT ticker, company_name FROM new_names")
    con.execute(
        "INSERT INTO loads SELECT ?, ?, ?, ?, min(trade_date), max(trade_date), "
        "count(DISTINCT ticker), now()::TIMESTAMP FROM new_rows",
        [path.name, digest, len(prices), replaced])
    con.unregister("new_rows")
    con.unregister("new_names")

    over = f", {replaced:,} overlapped an earlier file" if replaced else ""
    print(f"OK    {path.name}: {len(prices):,} rows "
          f"({dropped:,} pre-listing dropped{over}), "
          f"{prices['ticker'].nunique()} tickers, "
          f"{prices['trade_date'].min()} -> {prices['trade_date'].max()}")
    return 0


def main(argv: list[str]) -> int:
    if argv:
        files = [Path(a) if Path(a).is_absolute() else DROP / a for a in argv]
    else:
        files = sorted(p for p in DROP.iterdir()
                       if p.suffix.lower() in (".xlsx", ".xls", ".csv")
                       and not p.name.startswith("~$"))
    if not files:
        print(f"FAIL  no export files in {DROP}")
        return 1

    for leftover in (TMP, TMP.with_suffix(TMP.suffix + ".wal")):
        leftover.unlink(missing_ok=True)

    con = duckdb.connect(TMP)
    try:
        ensure_schema(con)
        rc = 0
        for path in files:
            if not path.exists():
                print(f"FAIL  no such file: {path}")
                rc = 1
                continue
            # An unreadable drop is a failed build, not a crash -- the abort
            # path below still has to run so the temp file is cleaned up.
            try:
                rc |= load_file(con, path)
            except Exception as exc:  # noqa: BLE001 - any drop can be malformed
                print(f"FAIL  {path.name}: {exc}")
                rc = 1
        con.execute("CHECKPOINT")  # fold the WAL in before the swap
    finally:
        con.close()

    # All-or-nothing: a half-built database is worse than a stale one.
    if rc:
        for leftover in (TMP, TMP.with_suffix(TMP.suffix + ".wal")):
            leftover.unlink(missing_ok=True)
        print("      build aborted, existing database left untouched")
        return rc

    TMP.replace(DB)

    con = duckdb.connect(DB, read_only=True)
    try:
        write_dictionary(con)
    finally:
        con.close()

    print(f"      rebuilt {DB.name} ({DB.stat().st_size / 1e6:.1f} MB) "
          f"from {len(files)} export(s)")
    print(f"      dictionary -> {DICT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
