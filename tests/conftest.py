import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scr"))

HEADERS = [
    "No", "Ticker", "Company Name", "Date",
    "Reference (D)\nUnit: VND", "Ceiling (D)\nUnit: VND", "Floor (D)\nUnit: VND",
    "Open (D)\nUnit: VND", "Highest (D)\nUnit: VND", "Lowest (D)\nUnit: VND",
    "Close Price (D)\nUnit: VND", "Open Adjusted (D)\nUnit: VND",
    "Highest Adjusted (D)\nUnit: VND", "Lowest Adjusted (D)\nUnit: VND",
    "Close Adjusted (D)\nUnit: VND", "Total trading volume (D)\nUnit: Shares",
    "Current Outstanding Shares\nUnit: Shares", "Free Float Shares\nUnit: Shares",
    "Market Capitalization\nUnit: VND", "Total trading value (D)\nUnit: VND",
]


def make_rows(tickers=("AAA", "BBB"), sessions=25, start=date(2026, 1, 5)):
    """Synthetic FiinPro rows: flat price 10,000, value 1e9/session, HOSE band."""
    rows, n = [], 0
    for i in range(sessions):
        d = start + timedelta(days=i)
        for t in tickers:
            n += 1
            shares, ff, px = 1_000_000, 400_000, 10_000
            rows.append([n, t, f"{t} Corp", d, px, px * 1.07, px * 0.93,
                         px, px, px, px, px, px, px, px, 100_000,
                         shares, ff, px * shares, 1e9])
    return rows


@pytest.fixture
def drop(tmp_path):
    """Write a drop with a banner and footer, like a raw portal export."""
    def _write(rows, name="drop.xlsx"):
        banner = [[None] * len(HEADERS) for _ in range(3)]
        banner[1][0] = "Data Title"
        footer = [["Website: fiinpro"] + [None] * (len(HEADERS) - 1)]
        pd.DataFrame(banner + [HEADERS] + rows + footer).to_excel(
            tmp_path / name, header=False, index=False)
        return tmp_path / name
    return _write
