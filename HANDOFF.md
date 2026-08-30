# PM-Mike — Handoff

Vietnam equity portfolio construction. Takes VN100 + deliberate picks, builds a
float-cap **baseline** allocation, lets a PM express sector views by hand-editing
a grid, and replays the result against local price history.

State as of 2026-08-30 · 6 commits · 7 scripts · 2 live portfolios · no tests.

---

## 1. The one architectural decision

**The agent fetches; scripts own every write.**

FiinQuant is reachable only through an MCP server, so there is no Python client.
Claude runs the `/eod` recipe (5 MCP calls), transcribes the response verbatim
into `data/raw/<date>.csv`, and stops. `scr/load_eod.py` validates that CSV and is
the only thing that writes `data/eod.parquet`. Same split on the history side:
FiinPro exports are downloaded by hand into `data/fiinpro/`, and
`scr/load_history.py` owns every write to `data/local_history.db`.

Consequence: generated artifacts are never hand-edited. Fix the input, re-run.

**Hand-edited files — the complete list:**

| File | Owner | What it decides |
|---|---|---|
| `data/universe.yml` | PM | index + picks |
| `index/group_map_live.csv` | PM | ticker → *Exclusive group* (the 21 sectors) |
| `index/anchor_date.json` | PM | sticky pricing date |
| `portfolio/<n>/sector_constituents_custom.csv` | PM | **the book** — ratings, multipliers, deletions |
| `portfolio/<n>/sector_cap.json` | PM | max group weight, on/off |
| `portfolio/<n>/tactical_group.csv` + `.json` | PM | cross-sector overlay, on/off |
| `portfolio/<n>/backtest_rebalance.json` | PM | rebalance discipline (inherits baseline's) |

Everything else on disk is derived and overwritten without asking.

---

## 2. Pipeline

```
data/universe.yml ──✎
       │
       ▼  /eod <date>   (agent: 5 MCP calls, verbatim transcription)
data/raw/<date>.csv
       │
       ▼  scr/load_eod.py <date>            idempotent per date
data/eod.parquet  ███ only record of past index membership
       │
       ├──▶ scr/build_group_map.py  ──▶ index/group_map_live.csv ──✎ (append-only)
       │                                          │
       ▼                                          ▼
    scr/build_baseline.py  ◄─── index/anchor_date.json ──✎
       │
       ├──▶ portfolio/baseline/sector_allocation.csv    the anchor vector
       └──▶ portfolio/baseline/sector_constituents.csv  the 4-row grid
                   │
                   ▼  scr/build_portfolio.py --fork <name>
       portfolio/<name>/input/sector_constituents.csv   (machine territory)
                   │
                   ▼  copy up one level, edit rows 0–1 ──✎
       portfolio/<name>/sector_constituents_custom.csv  THE BOOK
                   │
                   ▼  scr/build_portfolio_target.py <name>
       portfolio/<name>/target/{sector_allocation,holdings,built_from}
                   │
                   └──▶ ⊗ sizing / execution stage NOT BUILT

── separate track, does not feed the above ──
data/fiinpro/*.xlsx ──✎  ──▶ scr/load_history.py ──▶ data/local_history.db
                                                            │
       portfolio/<name>/backtest_rebalance.json ──✎         ▼
                                    scr/backtest.py <name>
                        portfolio/<name>/backtest/{equity,rebalances,summary}

⊗  data/live/*.csv  — /live writes it, nothing reads it. By design.
```

---

## 3. The weighting math

Float cap, always:

```
fcap_i = free_float_i × (market_cap_i / outstanding_shares_i)
```

`market_cap / outstanding_shares` is the session's **official close** — last matched
price on HOSE/HNX, session VWAP on UPCoM — without an exchange conditional, and it
reconciles with the provider's own `market_cap` by construction.

**Never `close_adj`.** The adjusted series folds each ticker's pending corporate-action
factor into its own price, so an adjusted cross-section prices 102 names on 102 private
scales. `close_adj` is comparable across *time* for one ticker; official close is
comparable across *tickers* on one date. Weighting is a cross-section. `close_adj` is
also restated backwards when an action is announced, which would silently rewrite
historical weights.

Tilt, priced on the anchor date:

```
b_g               baseline group budget (float-cap share)
w_g = b_g·m_g / Σ_h b_h·m_h        group weight after multipliers
w_i = w_g · fcap_i / S_g           name weight inside the group
```

Group layer is pure attribution — float-cap-within-group × float-cap-across-groups is
algebraically flat float cap. It exists because it's the grain views are expressed at.

---

## 4. The book (the PM-facing artifact)

`sector_constituents_custom.csv` — 21 columns, one per sector, A–Z:

```
row 0   multiplier   the rating repeated (= use default), or a number override
row 1   rating       AV | NO | OW | UW
row 2   sector name  UNTOUCHED — the binding contract with input/
row 3+  tickers      DELETE by blanking. No additions, no moves.
```

Defaults: `NO=0.0  UW=0.75  AV=1.0  OW=1.25`.

**The rule that matters:** deleting a ticker is *stock selection*, not allocation.
The sector keeps its full budget `b_g·m_g` and the survivors absorb the deleted name's
weight by float cap. Cutting a sector's budget is what ratings and multipliers are for.

Live example — `hsc_strat_high_growth`, Mining rated `OW` with a `4` override:
baseline 0.14% → target 0.83%, `realised_tilt 5.81x`. Real Estate – Residential
`UW` overridden to `0.5`: 25.98% → 18.87%.

### Overlays, both OFF by default

**Tactical groups** (`tactical_group.csv` + `.json`). A named group claims tickers
away from their home sectors and is rated like a sector. This is the *one* place a
name leaving a sector takes budget with it:

```
blank a cell  → name leaves, budget STAYS  (survivors absorb it)
claimed       → name leaves, budget GOES   (b_g shrinks by its float cap)
```

Claims resolve against the **baseline** column, not the book column, so a name the book
already deleted is still claimed. Total float cap is unchanged, so budgets still sum to 1.
Leave claimed names in the book — blanking them there too makes the switch a one-way door.

**Sector cap** (`sector_cap.json`, `max_weight` is a *fraction*). Clips every group and
redistributes the excess pro-rata, **iteratively** — one pass can lift an under-cap group
over the ceiling. Sits on top of the tilt and outranks it: a capped OW group comes out
with `realised_tilt < 1`. Weights *inside* a group are untouched.

---

## 5. Backtester

`scr/backtest.py` is standalone. It imports the weighting math from
`build_portfolio_target.py` **read-only** (one implementation of the tilt, zero edits)
and writes only under `portfolio/<name>/backtest/`.

It prices **every scenario the book's files allow** and ignores the on/off switches —
only file *presence* gates a variant:

| Variant | What it is |
|---|---|
| `default` | book constituents, all multipliers forced to 1.0 |
| `tilt` | the live math exactly |
| `sector_cap` | tilt + `max_weight` |
| `tactical` | tilt + overlay |
| `sector_cap_tactical` | tilt → migrate → cap, live composition order |

Discipline comes from `backtest_rebalance.json`; `backtest_rebalance.txt` is a
hand-maintained data dictionary that also records **why each default was chosen**
(drift distributions, threshold sweeps, cost tables). Read it before touching the JSON.

Current defaults: quarterly re-anchor on the period's first session, drift band on at
group grain, threshold 0.05, T+1 fill at `close_adj`, 10 bps brokerage/side + 10 bps sell tax.

Notable calls already argued out in that file:
- **group** grain, not name — within-group weights are float-cap arithmetic, not decisions.
- Band **restores** the current target; it does not re-anchor. Restore is to full target,
  not trimmed to the band edge (that design would need a cooldown; this one doesn't).
- `anchor_on_rebalance: no` is a **diagnostic only** — by 2026-07-01 a frozen target sits
  25.1% of NAV from the live one.
- `execution_price` dominates the cost model: median |close_adj − open_adj| is 1.08% of
  close, larger than any plausible brokerage assumption.

---

## 6. What's actually on disk today

**Data**

| Store | Coverage | Source |
|---|---|---|
| `data/eod.parquet` | **3 sessions**, 2026-08-12 → 08-14, 102 tickers × 21 cols | agent via MCP |
| `data/local_history.db` | 42,201 rows, 107 tickers, **403 sessions**, 2025-01-02 → 2026-08-18 | 3 FiinPro exports |

Two disjoint stores with different adjustment semantics. Nothing reconciles them.
`local_history.txt` is the regenerated data dictionary and the diffable record of DB state.

Anchor date pinned to **2026-08-12**. Baseline: 21 groups, 102 names. Top budgets —
Banks-Private 31.66%, Real Estate-Residential 25.98%, Banks-State 7.06%.

**Portfolios**

| | `hsc_strat_high_growth` | `hsc_strat_soe_dom` |
|---|---|---|
| Book | tilted — 9 live groups, 12 rated NO, 18 names | **untilted** — byte-identical to baseline |
| `sector_cap.json` | off, 0.25 | off, 0.20 |
| `tactical_group` | file absent | off — 1 group *SOE Divestment* (GAS, BSR, PLX) at OW×2 |
| `target/` | 18 holdings | 102 holdings = baseline |

**Backtest, 403 sessions, CAGR:**

```
hsc_strat_high_growth   default 23.6%   tilt 25.5%   sector_cap 27.6%
                        (tactical variants skipped — no tactical_group.csv)
hsc_strat_soe_dom       default 26.2%   tilt 26.2%   sector_cap 25.4%
                        tactical 26.2%  sector_cap_tactical 25.5%
```

`soe_dom` default == tilt because its book carries no view.

---

## 7. Why it fails loudly (the guard rails)

These are the design's real content — worth reading before changing anything.

- **`load_eod.py`** reconstructs the provider's `market_cap` from official close ×
  shares to 1e-5. This ties the price pull and the snapshot pull together: a
  transcription typo in either breaks it. Also checks dupes, nulls, exchange codes,
  close-inside-range on both price bases, float ≤ shares.
- **`build_baseline.py`** *refuses to build* on a date with a pending corporate action
  (`close_adj != close_raw` off UPCoM). On a stock dividend the price drops on the
  ex-date but `outstanding_shares` rises only on the credit date — in that window
  `market_cap` uses a stale share count and the name is understated by the whole bonus
  ratio. No arithmetic fix exists, only a clean date.
- **`build_portfolio_target.py`** has ~15 fatal guards: stale fork, sector row drift,
  tickers added or moved between columns, a surviving sector with everything deleted,
  a NO group with a nonzero multiplier, a book ticker with no parquet row, duplicate
  tactical claims, an infeasible cap (`max_weight × live_groups < 100%`).
- **`load_history.py`** rebuilds the DB from scratch every run into a temp file and
  swaps only if every export validates — a bad drop can't destroy a good database.
  Adjusted prices are adjusted *as of extract date*, so old and new rows can never be
  merged and called a series.
- **Exchange is derived, never joined**: `prices_v` reads it off the ceiling band width
  (<8.5% HOSE, <12.5% HNX, <17.5% UPCOM). Correct *per session* — BSR reads UPCOM before
  its 2025-01-17 HOSE transfer and HOSE after, which a joined snapshot would get wrong.

---

## 8. Review points / open gaps

**Blocking a clean checkout**
1. **`requirements.txt` is incomplete.** `duckdb` (1.5.5), `openpyxl` (3.1.5) and
   `pyarrow` (25.0.1) are installed and imported but unpinned. `backtest.py`,
   `load_history.py` and every parquet read fail on a fresh venv.
2. **`ruff check scr`** — the lint gate named in `CLAUDE.md` — reports **8 findings**
   today (import sort, 3× naive-datetime, 2× blind except, 1 unused unpack). Not clean.
3. **Zero tests.** `pytest` is pinned; no test files exist. All correctness lives in
   runtime guards, which only fire when someone runs the script.

**Product / methodology**
4. **No benchmark curve.** `default` is *book constituents at flat multipliers* — not the
   untilted 102-name baseline and not VN100. Nothing in `summary.csv` answers "did the
   tilt beat the market". Variant spreads are all the backtest can currently support.
5. **Survivorship bias is structural.** `get_index_constituents` has no as-of parameter,
   so `local_history.db` holds today's universe held backwards. Stamped into
   `built_from.txt`; levels are optimistic. The forward `eod.parquet` snapshot is the
   *only* accumulating membership record — and it is 3 days old.
6. **Sizing/execution unbuilt.** `holdings.csv` stops at `target_weight` and carries both
   price bases "for the sizing stage". No share counts, lot rounding, cash, or trade list.
7. **`hsc_strat_soe_dom` currently expresses no view** — untilted book, both overlays off,
   so `target/` is baseline. Its only articulated idea (SOE Divestment at OW×2) exists in
   backtest only. Intentional, or a book someone forgot to finish?

**Latent fragility**
8. **Column-position coupling.** Books bind ratings by column *index*. Renaming a sector in
   `group_map_live.csv` reorders the A–Z columns and silently misaligns every forked book.
   Guarded by a grid-equality check (forces a re-fork) and a WARN in `build_baseline.py` —
   but the re-fork discards the PM's rows 0–1, which must then be re-typed by hand.
9. **`/eod` is LLM transcription.** Five MCP calls joined on ticker into a CSV. The
   `market_cap` identity check is a strong backstop, but the step is manual and has no
   retry semantics beyond re-running the recipe.
10. `backtest.py:200` — `self.tac_r` is not assigned when `tactical_group.csv` is absent
    (the 5-tuple branch sets it, the 4-tuple fallback doesn't). Unused today; a latent
    `AttributeError` for anyone who reaches for tactical ratings.

---

## 9. Runbook

```powershell
# daily forward pull
/eod 2026-08-31                                        # agent + MCP, writes raw CSV, runs loader
.venv\Scripts\python.exe scr\build_group_map.py        # append new tickers, then fill Exclusive group by hand
.venv\Scripts\python.exe scr\build_baseline.py         # anchors on index/anchor_date.json

# a new portfolio
mkdir portfolio\<name>
.venv\Scripts\python.exe scr\build_portfolio.py --fork <name>
copy portfolio\<name>\input\sector_constituents.csv portfolio\<name>\sector_constituents_custom.csv
#   ...edit rows 0-1, blank names to delete...
.venv\Scripts\python.exe scr\build_portfolio_target.py <name>

# history + backtest
#   drop stripped FiinPro exports into data\fiinpro\ first
.venv\Scripts\python.exe scr\load_history.py
.venv\Scripts\python.exe scr\backtest.py <name>
.venv\Scripts\python.exe scr\backtest.py <name> --variants default,tilt

.venv\Scripts\python.exe -m ruff check scr
```

Every script's **module docstring is the spec** — it is longer and more precise than the
code below it. Read it before editing. `backtest_rebalance.txt` plays the same role for
the rebalance config.
