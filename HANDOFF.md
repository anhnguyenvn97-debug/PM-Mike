# PM-Mike — Handoff

Vietnam equity portfolio construction: a FiinPro drop becomes a float-cap
baseline, a PM tilts it per portfolio, and the result is a target allocation.
State as of 2026-09-14, after the ground-up rebuild (see `Progress.md` for the
decision log, `Task.md` for what is left).

---

## 1. Architecture decision

**One input, one database, scripts own every write.** FiinQuant MCP stalled, so
the agent is no longer in the data path. FiinPro Portal exports are downloaded by
hand and dropped into `data/fiinpro/`. Every derived file is rebuilt by a script;
fix the input and re-run, never edit an output.

| Hand-edited file | Decides |
|---|---|
| `data/fiinpro/*.xlsx` | the market data and therefore the universe |
| `index/group_map_live.csv` | ticker → ICB L2 sector → Exclusive group (default grouping) |
| `index/anchor_date.json` | the session every weight is priced on (now 2026-09-11) |
| `index/fol.csv` | foreign ownership limits (deferred, header only) |
| `portfolio/<name>/statement.json` | approach, scope, holding range, rebalance mandate, screens |
| `portfolio/<name>/sector_constituents_custom.csv` | the book: ratings, multipliers, deletions |
| `portfolio/<name>/sector_cap.json` | sector cap switch and max weight |
| `portfolio/<name>/tactical_group.json` + `.csv` | tactical overlay switch and claims |
| `portfolio/<name>/backtest_rebalance.json` | legacy backtester discipline |

---

## 2. Pipeline

```
data/fiinpro/*.xlsx  ✎
        │  scr/ingest.py        validate, rebuild from scratch, all-or-nothing
        ▼
data/market.db  + market.txt    prices, tickers, loads, prices_v (exchange)
        │  scr/params.py        ◄── index/group_map_live.csv ✎, index/fol.csv ✎,
        ▼                           index/anchor_date.json ✎
data/params/<anchor>.csv        float cap, 21d turnover, sector, QA columns
        │  scr/baseline.py
        ▼
portfolio/baseline/             sector_allocation.csv + sector_constituents.csv grid
        │  scr/portfolio.py new <name>     → statement.json ✎
        │  scr/portfolio.py fork <name>    → input/ + book (carried by group name)
        │  scr/portfolio.py screen <name>  → screen/exclusions.csv (suggest, --apply)
        ▼
portfolio/<name>/sector_constituents_custom.csv  ✎ the book
        │  scr/target.py <name>  ◄── sector_cap.json ✎, tactical_group.* ✎
        ▼
portfolio/<name>/target/        sector_allocation.csv, holdings.csv, built_from.txt
        │
        ▼
sizing & execution              ⊗ not built

legacy, frozen:
data/fiinpro/archive/*.xlsx → scr/load_history.py → data/local_history.db
        → scr/backtest.py <name> → portfolio/<name>/backtest/
```

---

## 3. Weighting math

```
fcap_i = free_float_i × close_raw_i          params.float_cap
b_g    = Σ fcap in group g / Σ fcap          baseline weight
w_g    = b_g · m_g / Σ_h (b_h · m_h)         tilted group weight
w_i    = w_g · fcap_i / S_g                  S_g = surviving float cap in g
```

`close_raw` is the official close. `close_adj` is restated per ticker as of the
extract date, so it is only used for returns. The vendor `market_cap` is kept as
a QA column because it has transcription gaps (20 rows in the current drop).

Ratings: `NO` 0.0, `UW` 0.75, `AV` 1.0, `OW` 1.25. A number in row 0 overrides
the default multiplier.

---

## 4. The book and overlays

The book is the baseline grid with rows 0–1 edited and cells blanked:

| Row | Content |
|---|---|
| 0 | multiplier (the rating repeated, or a number) |
| 1 | rating `AV` / `NO` / `OW` / `UW` |
| 2 | group names, immutable |
| 3+ | tickers; blank a cell to delete, never add or move |

- **Blank a cell:** the name leaves and the group keeps its budget.
- **Tactical claim:** the name leaves and takes its float cap into the tactical
  group. Claims resolve against the baseline column.
- **Sector cap:** clips every live group to `max_weight` and redistributes the
  excess pro-rata, iterating until nothing is above. The cap outranks the tilt.

**Re-fork carries by group name.** When the baseline changes, `fork` keeps each
surviving group's rating and multiplier and keeps deleted names deleted. New
names come in live, new groups come in `AV`, and vanished groups are reported
with the rating they lose.

---

## 5. Statement and screens

```json
{
  "approach": "", "scope": "",
  "holdings": {"min": 20, "max": 30},
  "rebalance": {"frequency": "1Q", "drift_threshold": 0.05},
  "screens": {
    "turnover":  {"on": false, "min_pct": 0.1},
    "float_cap": {"on": false, "min_bn_vnd": 1000}
  }
}
```

- `holdings`: `target.py` WARNs when the surviving count is outside the range.
- `rebalance`: frequency `2W` / `1M` / `1Q`, a drift threshold as a fraction, or
  both. Drift is half the sum of absolute group weight gaps. No stage consumes it
  yet.
- `screens`: `turnover` is average daily traded value over 21 sessions as a
  percent of float cap. `float_cap` is in billions of VND. FOL is deferred.
  Screens never edit the book on their own; `--apply` does.

---

## 6. State on disk (2026-09-14)

| Store | Content |
|---|---|
| `data/market.db` | 16,900 rows, 100 tickers, 169 sessions, 2026-01-05 → 2026-09-11, all HOSE |
| `data/params/2026-09-11.csv` | 100 tickers, 20 groups; turnover median about 0.63%/day |
| `portfolio/baseline/` | 20 groups; Banks – Private 31.13%, Real Estate – Residential 27.92%, Banks – State 6.78% |
| `data/local_history.db` (legacy) | 42,201 rows, 107 tickers, 403 sessions, 2025-01-02 → 2026-08-18 |

Map rows with no data in the drop: DXS, F88, HDC, IMP, MSR, SCS, SZC. Mining has
no members, so the baseline has 20 groups, not 21.

| Portfolio | Book | Cap | Tactical | Holdings | Range |
|---|---|---|---|---|---|
| hsc_strat_high_growth | 12 groups NO, UW Banks – State, OW Consumer Retail, UW×0.5 RE – Residential | off (0.25) | none | 17 | 20–30, outside |
| hsc_strat_soe_dom | all AV | off (0.20) | SOE Divestment OW×2 (GAS, BSR, PLX), off | 100 | 20–30, outside |

---

## 7. Guard rails

- **`ingest.py`** refuses duplicates, nulls, open or close outside the day's
  range on both price bases, negative volume or value, and free float above
  shares. It refuses two drops covering the same session, since they may carry
  different adjustment states. A failed build leaves the old database untouched.
- **`params.py`** fails on a ticker missing from the group map or with a blank
  group, and on non-positive float cap.
- **`baseline.py`** fails if the params file is missing or older than the
  database. It warns on a changed group set and on `adj_factor != 1`, since share
  counts may lag a stock dividend.
- **`target.py`** fails on a stale fork, group row drift, added or moved tickers,
  a live group with every name gone, bad ratings or multipliers, invalid overlay
  files, an infeasible cap, and params rebuilt after the baseline.
- **Exchange is derived per session** from the ceiling band width: under 8.5%
  HOSE, under 12.5% HNX, under 17.5% UPCoM.

Parity was proven before the old scripts were deleted: on the old 2026-08-12 data
the new baseline was byte-identical and five target variants matched to 0.0.
The backtester gave identical outputs after its imports moved to the new modules.

---

## 8. Open gaps

**Needs a user decision**
1. Both portfolios sit outside the default 20–30 holding range; the ranges and
   the approach/scope text are placeholders.
2. `hsc_strat_high_growth` lost its Mining OW×4 view when MSR left the drop.

**Product / methodology**
3. **No UI yet.** Step 4 in `Task.md`: artifact mockup, then a local Flask app.
4. **FOL deferred.** `index/fol.csv` is empty and no screen reads it.
5. **Rebalance mandate is recorded but unused.** Nothing consumes
   `statement.json` rebalance yet; the backtester still reads its own JSON.
6. **Backtester is on the legacy store.** It replays `local_history.db`, has no
   benchmark curve, and its universe is today's names held backwards
   (survivorship bias).
7. **Sizing and execution unbuilt.** `holdings.csv` stops at `target_weight`.

**Latent fragility**
8. **Corporate action window.** An extract taken after the anchor cannot detect a
   pending stock dividend; only a WARN on `adj_factor` remains.
9. **Group map has no automatic source.** FiinPro drops carry no sector, so a new
   ticker fails `params.py` until someone adds it to `group_map_live.csv`.
10. `backtest.py`: `self.tac_r` is unassigned when `tactical_group.csv` is absent.
    Unused today. Left alone because the backtester is frozen.
11. `backtest_rebalance.txt` files still mention the deleted `build_*.py` scripts.

---

## 9. Runbook

```powershell
# new data: drop the FiinPro export into data\fiinpro\ (archive the old one)
.venv\Scripts\python.exe scr\ingest.py
.venv\Scripts\python.exe scr\params.py                  # anchors on index\anchor_date.json
.venv\Scripts\python.exe scr\baseline.py

# a new portfolio
.venv\Scripts\python.exe scr\portfolio.py new <name>    # then edit statement.json
.venv\Scripts\python.exe scr\portfolio.py fork <name>
.venv\Scripts\python.exe scr\portfolio.py screen <name> # optional; --apply-all or --apply T ..
#   ...edit rows 0-1 of sector_constituents_custom.csv, blank names to delete...
.venv\Scripts\python.exe scr\target.py <name>

# after a baseline change: re-fork every portfolio, then rebuild its target
.venv\Scripts\python.exe scr\portfolio.py fork <name>
.venv\Scripts\python.exe scr\target.py <name>

# legacy backtest
.venv\Scripts\python.exe scr\backtest.py <name>

# checks
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\ruff.exe check scr tests
```

Every script's **module docstring is the spec**. Read it before editing.
