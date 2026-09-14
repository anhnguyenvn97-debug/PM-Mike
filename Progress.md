# Progress

Log of the ground-up rebuild. Newest entry first. Pair with `Task.md` (what is
left) and `HANDOFF.md` (the pre-rebuild system, now reference only).

---

## 2026-09-14 — Housekeeping and a false delete

- `git rm index/group_map_default.csv` (staged, not committed): nothing read it.
- User reported deleting `hsc_strat_soe_dom` with the folder still present. The
  folder was intact (all files listed) and the app log showed no DELETE and no
  browser requests at all: the click was on the mockup, whose Delete only
  removes the card in the page. No code change.
- The desk app's background copy (started by the Claude session) was killed by
  Windows for low memory. Not restarted; the user runs it in their own
  PowerShell window from now on (Task.md open items).
- Open decision logged: permanent delete vs a `_trash/` move, and committing
  the portfolio files before relying on the desk for deletes.

---

## 2026-09-14 — Step 8 done: desk owns the portfolio files

Every setting that changes a portfolio's outcome was already editable from the
page; this step makes the page the safe way to do it (D23: portfolio files are
desk-owned, hand edits are the fallback). Legacy `sector_cap.json` and
`backtest_rebalance.json` stay outside the desk.

### Changes

| File | What |
|---|---|
| `scr/app.py` | `version(home, key)` fingerprints (mtime_ns, size) the files a save rewrites: statement; constraints; book = book + tactical csv + switch. `/api/p/<name>` serves `versions`; each PUT may carry `version` and FAILs with "changed on disk since the page loaded; reload the portfolio" on a mismatch. No version (a script) skips the check. |
| `scr/static/desk.js` | Saves send the version they loaded. `Reload` button in the flow head re-reads the files and keeps unsaved edits (drafts survive, versions refresh). Target step: `Save and build` when Book or Constraints edits are pending; a failed save stops before the build and leaves the edits in the page. |
| `tests/test_app.py` | Conflict on constraints and book: 422, file untouched, reload then save works, no-version save skips the check. |

### Checks

- pytest 63 passed; ruff clean.
- Not exercised in the browser: Save and build, Reload after a conflict.

---

## 2026-09-14 — Step 7 done: freshness chain for group-map edits

`index/group_map_live.csv` stays hand-edited (D22). Every derived file now
knows when an input it was built from is newer, and the page shows it; nothing
rebuilds a portfolio on its own. Chain: drops + map + fol -> params ->
baseline -> portfolio input/ (the last link compared by content, so a rebuild
that changes nothing never nags).

### Changes

| File | What |
|---|---|
| `scr/common.py` | `newer_than(target, *inputs)`; freshness chain in the docstring. |
| `scr/baseline.py` | `params_stale(pp, db, map, fol)`. `ensure()` rebuilds params when the map or fol is newer too (was db only), so a fork after a map edit never uses the old grouping. `run()` FAILs on the same rule. |
| `scr/app.py` | `/api/state` adds `stale` (built anchor -> inputs newer than its params/baseline), `unmapped` (tickers on the latest session the map lacks), `group_map.edited`. Portfolio summary and detail add `refork` = `{needed, lost, appeared}` from `portfolio.carry` when input/ differs from the anchor's baseline grid. |
| `scr/static/desk.js` | Data tab: fresh / stale / unmapped pill, unmapped list, stale anchors with the reason, "(stale)" in the anchor selects. Cards: "regrouped since fork, re-fork" pill with the ratings a re-fork loses. Fork step: same note, "(stale, rebuilds on fork)". |
| `tests/test_baseline.py` (new), `tests/test_app.py` | `newer_than`; `ensure` rebuilds after a map edit and is a no-op when fresh; state shows stale anchor and refork before/after a re-fork. |

### Checks

- pytest 62 passed; ruff clean.
- Live: `stale {}`, `unmapped []`, both portfolios `refork needed False`;
  `baseline.py` and `target.py` output unchanged.
- `index/group_map_default.csv` was read by nothing; removed afterwards with
  `git rm` (recoverable from HEAD).

---

## 2026-09-14 — Step 6 done: Flask desk app

Local app over the scripts, matching mockup v3. Runs on 127.0.0.1:5000; holds
no state; writes only hand-edit files. Preview uses the Python solver, so the
mockup JS solver discrepancy (step 5) is gone.

### Changes

| File | What |
|---|---|
| `scr/target.py` | `build` split into `compute(name, book=, tactical=, constraints=)` (no writes, no prints; overrides stand in for files) + `build` (writes and report). `parse_tactical(grid)` factored out of `read_tactical` (kept for backtest.py). |
| `scr/app.py` | Flask JSON API + one page. Actions call `ingest.main`, `params.main` + `baseline.main`, `portfolio.new/fork/screen/delete`, `target.build`, and return stdout. Saves: statement, constraints (validated first), book + tactical from a spec (baseline order, auto-NO, barrier rule, no invalidated names). Atomic writes. Global lock. Guards: local Host header, JSON body on mutations. |
| `scr/templates/desk.html`, `scr/static/desk.css`, `scr/static/desk.js` | Mockup v3 CSS verbatim; JS ported to fetch state/portfolio and post edits to `/preview` (debounced). Unsaved Book/Constraints edits stay in the page with a Save/Discard bar; Fork and Build are disabled while dirty. |
| `docs/ui_mockup_v3.html` | Approved mockup, copied out of the session scratchpad. |
| `requirements.txt` | `flask==3.1.3`. |
| `tests/test_app.py`, `tests/test_portfolio.py` | +14 tests: compute == build and writes nothing, overrides, page/API load, book round-trip with auto-NO, 6 rejected inputs, preview == build, validation before write, new/fork/screen/delete lifecycle, guards. |

### Checks

- pytest 59 passed; ruff clean.
- Live targets after the split: holdings and allocation identical to the
  pre-change snapshot, stdout identical; only `built_at` moved.
- Live API: all-three-caps preview on high_growth reproduces step 5 (Banks -
  Private 20, RE Residential 20, Consumer Retail 15; 5 large at 50.00%, 9 at T).
- Saving both live books unchanged through the API is byte-identical.
- Browser: data page, book edit (OW preview 46.0% -> 51.6%), constraints
  preview, discard; no console errors. Drag-and-drop, tactical search, and
  fork/screen/build clicks were not exercised in the browser (covered by API
  tests only).

---

## 2026-09-14 — Step 5 done: scripts for the v3 UI

User approved mockup v3 and this plan, with both recommendations:

- **D20 sticky baseline copy.** Baselines live in `portfolio/baseline/<anchor>/`.
  `baseline.py` also copies the sticky anchor's two files to the root, only
  because `backtest.py` reads the root. The root anchor is also the default for
  `portfolio.py fork`.
- **D21 legacy sector cap.** Portfolios use `constraints.json`.
  `sector_cap.json` stays, read only by `backtest.py`; `target.py` WARNs if it
  says yes without a sector constraint and does not apply it.

### Changed

| Script | Change |
|---|---|
| `common.py` | `constraints.json` schema + validator, `portfolio_anchor()`, `read_invalid()` |
| `params.py` | build factored into `run()` so fork can call it |
| `baseline.py` | dated folders, sticky root copy, `ensure(anchor)` builds on demand |
| `portfolio.py` | `fork --anchor`; `screen --invalidate/--invalidate-all/--restore`; auto-NO on emptied groups; `delete --yes`; `new` writes default `constraints.json` |
| `target.py` | prices from the portfolio's anchor; invalidated-name guard; `waterfill` + `apply_constraints` solver; outputs gain `full` (allocation) and `pin` (holdings); db/resolver dependency dropped |

`apply_caps` left untouched because `backtest.py` imports it; a test pins
`waterfill` to it.

### Live migration

`baseline.py` built `portfolio/baseline/2026-09-11/`; both portfolios got an
all-off `constraints.json`, were re-forked (provenance now points at the dated
folder) and rebuilt. Targets are identical to the pre-change snapshot on every
old column. Backtest `equity.csv`, `rebalances.csv`, `summary.csv` identical
for both portfolios; only `built_at` differs.

### Solver vs mockup (high_growth, live)

| Scenario | Python | Mockup JS |
|---|---|---|
| sector 25% | BP 25, RE 25, FS 13.97 | same |
| + stock 10% | BP 25, RE 20, FS 16.24 | same |
| large only (5/50) | VHM 14.57, BP 35.43, RE 19.57; large 5 = 50.00% | same |
| stock 5% | FAIL 85% < 100% | same |
| all three | BP 20, RE 20, CR 15; large 5 = **50.00%** | BP 25, Logistics 16.16, RE 15; large 5 = 44.8% |

The three-constraint case differs. Python is correct to the D17 spec: the five
large names are already at the 10% stock cap and sum to exactly L, so nothing
scales; every other holding stops at T (5%), which caps Banks - Private at
4 × 5% = 20%. The JS scaled the large set before the stock cap had settled and
under-filled L. Python counts every holding exactly at T as `at_threshold`
(9), the JS only counted names it had stopped (4).

### Checks

- `pytest`: 45 passed (24 before). New: sector per-group incl. tactical,
  unknown per-group name, legacy cap not applied, waterfill = apply_caps,
  stock cap within group, stock cap spill, stock infeasible, large stop-at-T,
  large pro-rata scaling, large infeasible, constraints validation (6),
  fork onto a second anchor, portfolios on different anchors, missing anchor,
  invalidate/restore/refork, invalidating the last name -> NO, tactical claim
  of an invalidated name, delete.
- `ruff check scr tests`: clean.

---

## 2026-09-14 — Step 4: UI mockup v1 and v2

Artifact (same URL for both versions):
https://claude.ai/code/artifact/f91fc60e-8d80-4599-a4b3-657c7b00ddc8

v1 reproduced `target.py` exactly on 2026-09-11 (high_growth 17 names, Banks -
Private 46.01%, VHM 18.91%). v2 applied the user's review:

- Portfolios: delete with inline confirm; one `+ New portfolio` card.
- Fork: pick any anchor with 21 sessions behind it. Mockup carries real params
  for 7 anchors (06-30, 07-31, 09-07..09-11), computed by `params.compute`.
- Screen: **Invalidate** replaces blanking. Invalidated names stay in the book,
  struck through, undraggable, and cannot be claimed by a tactical group.
- Book: two boxes per group (all names | investable), drag to include, add
  all / remove all per group and globally. Empty investable box = NO
  automatically; a name arriving in an auto-NO group makes it AV again.
  Tactical overlay lives under the book; claims are retroactive on the boxes.
- Step 5 = constraints, three tick boxes with a live status table.

### Decisions

- **D17 stop-at-T.** UCITS-style large-holding cap: names above threshold T are
  scaled pro-rata to the aggregate cap L; a name that would fall under T stops
  at T and leaves the large set (and no longer counts toward L). Excess pours
  into names below T, each stopping at T, so no name ever crosses T and the
  large set only shrinks. Infeasible when L + T × small names < 100%; the
  solver fails and reports rather than leaving cash.
- **D18 within-group spill.** Excess from a stock-level cap goes to the same
  group's uncapped names first (sector allocation and tilt preserved); it
  spills to other groups pro-rata only when the group is full or at its sector
  cap. Order: sector cap → max per stock → UCITS; loop until stable.

### Solver check (in-page JS, high_growth)

| Scenario | Sum | Result |
|---|---|---|
| none | 100% | matches target.py |
| sector 25% | 100% | Banks-Private and RE-Res both at 25% |
| sector 25% + stock 10% | 100% | VHM, NVL, CTG at 10% |
| all three (T 5%, L 50%) | 100% | 5 large sum 44.8%, 4 held at 5% |
| UCITS only | 100% | VHM 14.57%, 5 large sum exactly 50% |
| stock 5% × 17 names | fail | 85% < 100%, reported before solving |

Two bugs found and fixed in the process: spill gave up after one pass, and
names held at T were wrongly counted against L.

---

## 2026-09-14 — Step 3 done: retire and document

### Removed (`git rm`, recoverable from HEAD `415532e`)

| Path | Why |
|---|---|
| `.claude/commands/eod.md`, `live.md` | MCP path retired (D1) |
| `scr/load_eod.py`, `data/raw/`, `data/live/`, `data/eod.parquet`, `data/universe.yml` | replaced by `ingest.py` + `market.db` |
| `scr/build_baseline.py`, `build_portfolio.py`, `build_portfolio_target.py` | replaced by `baseline.py`, `portfolio.py`, `target.py` after exact parity |
| `scr/build_group_map.py` | needed the parquet's ICB codes; FiinPro drops carry no sector, so it cannot be ported. `params.py` now fails naming any unmapped ticker. |

### Changed

- **`scr/backtest.py`**: imports moved to `common` + `target`; docstring reference
  updated; three lint-only fixes (unused unpack renamed, timezone-aware
  `built_at`, `noqa` on a date-only `strptime`). Before/after runs for both
  portfolios into scratch folders: `equity.csv`, `rebalances.csv`,
  `summary.csv` **identical**. The real `portfolio/*/backtest/` outputs were not
  overwritten.
- **`scr/load_history.py`**: `DROP` repointed to `data/fiinpro/archive/`,
  marked LEGACY. Without this, a rebuild would have read `VN100 data.xlsx` and
  replaced the backtest history. `local_history.db` itself not rebuilt.
- **`data/fiinpro/rejected/`**: holds `VN100 14-9.xlsx` (untracked, git-ignored),
  so neither loader sees it.
- **`requirements.txt`**: `pyarrow` dropped.
- **`.gitignore`**: parquet/MCP comments replaced; ignores `rejected/`.
- **Docs**: `CLAUDE.md`, `HANDOFF.md`, `.claude/skills/pipelining/SKILL.md` and
  `reference/example.md` rewritten for the FiinPro-only pipeline.
- **Claude memory**: `pm-mike-eod-pipeline` and `pm-mike-portfolio-tilt` updated.

### Checks

- `ruff check scr tests`: clean across all of `scr/` (legacy findings gone with
  the deleted files).
- `pytest`: 24/24.

---

## 2026-09-14 — Step 2 done: baseline, portfolio, target

### Decisions from the user (resolving step 1's open items)

| # | Decision |
|---|---|
| D12 | Anchor moved to 2026-09-11 (`index/anchor_date.json`). |
| D13 | Accept 20 groups; Mining is gone (MSR, F88 not in the drop). |
| D14 | FOL skipped for now: `fol_limit` stays blank, no FOL screen accepted. |
| D15 | `turnover_21_pct` = average DAILY turnover over 21 sessions ÷ float cap × 100. |
| D16 | No commits until the user asks. |

### Design choices made in step 2

- **File names kept** (`sector_constituents.csv`, `sector_allocation.csv`,
  `sector_constituents_custom.csv`): `scr/backtest.py` reads them and D9 says do
  not touch it. The planned rename to `book.csv` is dropped.
- **Old scripts left in place** (`build_baseline.py`, `build_portfolio.py`,
  `build_portfolio_target.py`): `backtest.py` imports from the last one.
- **Re-fork carries by group name**, not column position: ratings, multipliers
  and deletions survive a baseline whose group set changed.
- **Screens suggest, the user applies**: `screen` writes `exclusions.csv`;
  `--apply` / `--apply-all` blank tickers in the book.
- **Old "pending corporate action" FAIL replaced** by a WARN on
  `adj_factor != 1` in `baseline.py` (an after-the-fact extract cannot detect
  a pending action).
- **Holding range** is a WARN in `target.py`, recorded in `built_from.txt`.
- **Rebalance mandate** allows frequency, drift threshold, or both.

### Built

| File | What |
|---|---|
| `scr/common.py` | Paths, file-name constants, `BookError`, grid read/write, yes/no switch parser, `statement.json` schema + validation. |
| `scr/baseline.py` | `data/params/<anchor>.csv` → `portfolio/baseline/`. FAILs if params missing or older than `market.db`. WARNs on group-set change and `adj_factor != 1`. |
| `scr/portfolio.py` | `new <name>` (folder + default statement), `fork <name>` (snapshot baseline, carry book by name), `screen <name> [--apply T.. / --apply-all]`. |
| `scr/target.py` | Port of tilt, tactical groups, sector cap from `build_portfolio_target.py`, priced from params. Adds holding-range check. FAILs if params rebuilt after the baseline. |
| `scr/params.py` | Turnover switched to daily average (D15); FOL WARN downgraded to info (D14). |
| `tests/test_portfolio.py` | 16 tests: baseline weights, untilted = baseline, tilt + deletion, tactical budget move, iterative cap, cap too tight, holding range, addition rejected, re-fork carry, screen suggest/apply, `new` guards, statement validation. Suite: 24/24. |
| `portfolio/*/statement.json` | Created for both portfolios: approach/scope blank, holdings 20–30 (default), rebalance 1Q + drift 0.05 (mirrors `backtest_rebalance.json`), screens off. |

### Parity check (passed)

Scratch script `parity.py` (session scratchpad, not in repo) fed the new code
the OLD 2026-08-12 parquet data, float cap on the old basis, and ran old and new
target builders side by side on a copy of `portfolio/`:

- New baseline grid byte-identical; allocation equal to 1e-9.
- 5 target variants (high_growth as-is and cap 0.25; soe_dom as-is, tactical,
  tactical + cap 0.2): max absolute weight difference **0.0**; `capped`, `kind`,
  sector assignment identical; re-fork preserved both books.

### Live run on 2026-09-11

| Portfolio | Groups | NO | Holdings | Range 20–30 |
|---|---|---|---|---|
| hsc_strat_high_growth | 20 | 12 | 17 | OUTSIDE |
| hsc_strat_soe_dom | 20 | 0 | 100 | OUTSIDE |

- high_growth lost **Mining OW ×4** (MSR gone). Largest target groups: Banks –
  Private 46.01%, Real Estate – Residential 20.63% (UW 0.5), Financial Services
  9.32%.
- soe_dom tactical overlay verified on live data in a scratch copy (switch left
  off in the repo): SOE Divestment claims GAS, BSR, PLX, 0.85% of the book.
- Baseline top groups: Banks – Private 31.13%, Real Estate – Residential
  27.92%, Banks – State 6.78%.

### Not verified

`scr/backtest.py` was not run (D9). Both books and the baseline now have 20
groups, so its sector-row check should pass, but it still reads
`data/local_history.db`.

### Commands

```powershell
.venv\Scripts\python.exe scr\ingest.py
.venv\Scripts\python.exe scr\params.py
.venv\Scripts\python.exe scr\baseline.py
.venv\Scripts\python.exe scr\portfolio.py new <name>
.venv\Scripts\python.exe scr\portfolio.py fork <name>
.venv\Scripts\python.exe scr\portfolio.py screen <name> [--apply-all]
.venv\Scripts\python.exe scr\target.py <name>
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\ruff.exe check scr\ingest.py scr\params.py scr\common.py scr\baseline.py scr\portfolio.py scr\target.py tests
```

---

## 2026-09-14 — Step 1 done: ingest + params

### Why the rebuild

FiinQuant MCP stalled. The old design had the agent fetch EOD data via MCP
(`/eod`, `/live`) into `data/eod.parquet`, with FiinPro history in a separate
`data/local_history.db`. Two stores, two schemas, one vendor. Decision: rebuild
from the ground up around ONE database fed only by hand-downloaded FiinPro
Portal drops, with old scripts as reference only.

### Decisions locked with the user

| # | Decision |
|---|---|
| D1 | FiinPro Portal drops in `data/fiinpro/` are the only market input. `/eod`, `/live`, MCP, `eod.parquet`, `data/raw/`, `data/live/`, `universe.yml` are to be retired. |
| D2 | Float cap = `free_float × close_raw`. Vendor `market_cap` is a QA column only (it has transcription gaps). Never `close_adj`. |
| D3 | Parameters are always computed from the db into a ready-to-use CSV. Screens read it, never compute. |
| D4 | Screening filters (21d turnover / float cap, FOL, free-float cap) are optional and per portfolio. They produce suggested exclusions the user accepts into the book. They never cut the baseline universe. |
| D5 | `index/group_map_live.csv` is the default sector grouping. |
| D6 | Portfolio statement per portfolio: approach and scope (plain text), holding range min–max (warn if outside), rebalance mandate (frequency 2W / 1M / 1Q, or aggregated drift threshold). |
| D7 | Drift metric = half the sum of absolute weight gaps at group grain. |
| D8 | Tactical groups, tilt, sector cap: same semantics as today. |
| D9 | Backtester: do nothing for now. |
| D10 | UI: a local Flask app from the venv (a published artifact cannot run scripts or write files). Artifact mockup first, app after approval. |
| D11 | Raw input for the new db is `data/fiinpro/VN100 data.xlsx` only. Earlier drops are archived. |

### Rejected input

`VN100 14-9.xlsx` was NOT the VN100: 100 HOSE tickers alphabetically AAA–DRH,
only 21 overlapped the sector map, and it had no trading value column. Archived.

### Built

| File | What |
|---|---|
| `scr/ingest.py` | `data/fiinpro/*` (top level) → `data/market.db`. Rebuilt from scratch each run, temp file swapped in only if every drop validates. Finds the header row ("No"), strips banner/footer, drops non-session rows (no close). Validates duplicates, nulls, OHLC coherence (raw and adj), negatives, free float ≤ shares; vendor cap mismatch is a warning. Refuses two drops overlapping on (date, ticker). Writes `data/market.txt`. Sector and FOL are NOT stored. |
| `scr/params.py` | `market.db` + `index/group_map_live.csv` + `index/fol.csv` → `data/params/<anchor>.csv`. Anchor: `--date` > `index/anchor_date.json` > latest. Ticker missing from map or blank group = FAIL. |
| `tests/` | `conftest.py` (synthetic drop with banner/footer), `test_ingest.py` (4), `test_params.py` (4). 8/8 pass. |
| `index/fol.csv` | Header-only template `ticker,fol_limit` (ratio 0–1). |
| `requirements.txt` | Now matches imports: pandas, numpy, duckdb, openpyxl, pyarrow (legacy), ruff, pytest. Dropped anthropic, mcp, python-dotenv (unused). |
| `.gitignore` | Ignores `data/market.db*`. |
| `data/fiinpro/archive/` | Old drops moved here (`git mv` for tracked ones). Ingest ignores it. |

### params CSV columns

`ticker, company_name, exchange, icb_l2, group, close_raw, outstanding_shares,
free_float, free_float_ratio, float_cap, market_cap_vendor, mcap_gap,
sessions_21, value_21, adv_21, turnover_21_pct, fol_limit, adj_factor`

- Liquidity window = 21 sessions ending on the anchor, inclusive.
- `turnover_21_pct` = cumulative `value_21 / float_cap × 100`; NaN if < 21 sessions.
- `adj_factor` = `close_adj / close_raw`; ≠ 1 means a corporate action between anchor and extract date.

### State on disk after step 1

| Item | Value |
|---|---|
| `market.db` | 16,900 rows, 100 tickers, 169 sessions, 2026-01-05 → 2026-09-11, all HOSE, 3.2 MB |
| Non-session rows dropped | 100 (all 2026-09-14, not yet traded at extract) |
| Vendor cap mismatches | 20 rows; worst NVL 9.0%, SHB 4.0% |
| Params written | `data/params/2026-08-12.csv` (sticky anchor), `data/params/2026-09-11.csv` |
| Groups | 20 (Mining lost: MSR, F88 not in drop) |
| Map rows not in drop | DXS, F88, HDC, IMP, MSR, SCS, SZC |
| turnover_21_pct (09-11) | median 13.2%, min BWE 1.4%, max 78.0% |
| Corporate action since 08-12 anchor | BID, DIG, MSB, PHR, SSI, TCH, VIB, VIX, VPI |

### Untouched on purpose

Legacy scripts (`load_eod.py`, `load_history.py`, `build_*.py`, `backtest.py`),
`data/eod.parquet`, `data/local_history.db`, both portfolios. The backtester
still runs on the old store.

### Not committed

Nothing from step 1 is committed yet. Branch `docs/handoff`.

### Commands

```powershell
.venv\Scripts\python.exe scr\ingest.py
.venv\Scripts\python.exe scr\params.py                 # sticky anchor
.venv\Scripts\python.exe scr\params.py --date 2026-09-11
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\ruff.exe check scr tests
```
