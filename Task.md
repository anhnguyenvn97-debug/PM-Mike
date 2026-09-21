# Task

Remaining work for the ground-up rebuild. Check items off as they land and log
each finished step in `Progress.md`. Decisions D1–D16 referenced below live
there.

Rules that hold throughout: PowerShell only; `.venv\Scripts\python.exe`; installs
via `uv pip install`; scripts own every derived write; old scripts are reference
only; every script's module docstring is its spec; no commits until asked (D16).

---

## Open items for the user

- [ ] **Holding ranges.** Both portfolios got the default 20–30 and both are
      OUTSIDE (high_growth 17, soe_dom 100). Set real ranges in each
      `statement.json`.
- [ ] **Approach / scope text** is blank in both `statement.json` files.
- [ ] **Review the converted books** (step 11). Views were converted to
      reproduce the old weights, so several AV groups now carry small OW/UW
      pp and RE Residential in high_growth is clipped at UW3 −9. Re-rate them
      in the Book step.
- [ ] **high_growth lost Mining OW ×4.** Decide whether that view moves to
      another group.
- [ ] **Delete is permanent.** Desk Delete runs `shutil.rmtree` on
      `portfolio/<name>/`; no Recycle Bin. Decide: commit the portfolios first,
      and/or add a `portfolio/_trash/<name>-<timestamp>/` move instead.
      Both live portfolios have untracked `statement.json`/`constraints.json`.
- [ ] **Run the desk yourself** in a PowerShell window
      (`.venv\Scripts\python.exe scr\app.py`, then http://127.0.0.1:5000). A
      copy started by a Claude session dies with the session or under low
      memory. The mockup (`docs/ui_mockup_v3.html`, artifact URL) looks the
      same but writes nothing.

---

## Current layout

```
data/fiinpro/*.xlsx              hand     FiinPro drops, only market input
index/group_map_live.csv         hand     sector grouping, read on every calculation
index/fol.csv                    hand     FOL limits (header only; the fol screen flags "no data")
portfolio/<name>/statement.json  desk     approach, scope, holdings, rebalance rule
portfolio/<name>/constraints.json  desk   active budget pp; sector / stock / large caps
portfolio/<name>/screens.json    desk     screen rules, flags only
portfolio/<name>/book.json       desk     the working allocation (spec)
portfolio/<name>/decisions/      desk     recorded decisions, one per date; archive/ after reset
portfolio/<name>/backtest_config.json  desk  backtest costs, lag, risk-free rate

data/market.db, data/market.txt  derived  scr/ingest.py
data/params/<date>.csv           cache    scr/params.py --as-of (nothing reads it)
portfolio/<name>/target/         derived  scr/target.py (desk Record)
portfolio/<name>/backtest_engine/  derived  scr/backtest_engine.py
portfolio/<name>/_v1/            kept     files the v2 migration retired; delete when satisfied
data/fiinpro/archive/            kept     old drops, read by nothing
```

---

## Step 1 — Ingest and params  [DONE 2026-09-14]

- [x] `scr/ingest.py`, `scr/params.py`, tests, `requirements.txt`, archive drops

## Step 2 — Baseline, portfolio, target  [DONE 2026-09-14]

- [x] `scr/common.py` (statement schema, grid I/O, switches)
- [x] `scr/baseline.py` from params
- [x] `scr/portfolio.py` new / fork (carry by name) / screen (suggest, apply)
- [x] `scr/target.py` port + holding-range WARN
- [x] Parity vs old scripts: exact on 5 variants
- [x] Tests: 24/24
- [x] Live rebuild of baseline and both portfolios on 2026-09-11
- ~~Rename book to `book.csv`~~ dropped: `backtest.py` reads the old name (D9)

## Step 3 — Retire and document  [DONE 2026-09-14]

- [x] `git rm` `/eod`, `/live` commands, `load_eod.py`, `data/raw/`, `data/live/`,
      `data/eod.parquet`, `data/universe.yml` (recoverable from HEAD).
- [x] Repointed `backtest.py` imports to `common` + `target`; outputs identical.
- [x] `git rm` `build_baseline.py`, `build_portfolio.py`,
      `build_portfolio_target.py`, `build_group_map.py` (no sector source in
      FiinPro drops, so the helper cannot be ported).
- [x] `load_history.py` + `local_history.db` kept as legacy for the backtester;
      loader repointed to `data/fiinpro/archive/`.
- [x] Dropped `pyarrow`; cleaned `.gitignore`.
- [x] Rewrote `CLAUDE.md`, `HANDOFF.md`, pipelining skill and its example.
- [x] `ruff check scr tests` clean; `pytest` 24/24.
- [x] `index/group_map_default.csv` removed 2026-09-14 (unused seed for the
      deleted helper; recoverable from HEAD).
- [ ] Leftover, low priority: `backtest_rebalance.txt` files mention deleted
      scripts. Left for the backtester revision.

## Step 4 — UI

- [x] Artifact mockup (D10), v1 published 2026-09-14:
      https://claude.ai/code/artifact/f91fc60e-8d80-4599-a4b3-657c7b00ddc8
      Math runs in the page on real 2026-09-11 data; matches target.py output. Pages:
      1. Data status: drops, `market.txt` state, run ingest, run params.
      2. Portfolio list: existing portfolios + **New portfolio**.
      3. Per-portfolio flow: Statement → Fork → Screen (accept exclusions) →
         Book (ratings, multipliers, deletions) → Overlays (tactical, sector
         cap) → Target (result, holding-range flag).
- [x] v2 after user review (same URL, 2026-09-14): delete portfolio; Fork picks
      an anchor (baseline per anchor); Screen invalidates instead of blanking;
      Book = drag-to-include boxes + tactical overlay, auto-NO on empty; step 5
      = constraints (sector cap universal/individual, max per stock, UCITS
      large-holding cap). Decisions D17 stop-at-T, D18 within-group spill.
- [x] User review: v3 approved 2026-09-14.

## Step 5 — Scripts for the v3 UI  [DONE 2026-09-14]

- [x] Baseline per anchor `portfolio/baseline/<anchor>/`; sticky root copy for
      backtest.py (D20). `baseline.ensure()` builds params + baseline on demand.
- [x] `portfolio fork --anchor`; target and screen price from the portfolio's
      own anchor.
- [x] `screen --invalidate / --invalidate-all / --restore` -> `screen/invalid.csv`;
      target FAILs on an invalidated name in the book or a claim.
- [x] Auto-NO: fork and invalidate rate an emptied group NO and WARN.
- [x] `constraints.json` (sector universal/per-group incl. tactical, stock max,
      large T/L) + solver `target.apply_constraints` (D17, D18). Legacy
      `sector_cap.json` read only by backtest.py (D21).
- [x] `portfolio delete --yes`.
- [x] Tests 24 -> 45; ruff clean; live targets identical on all old columns;
      backtest outputs identical.
- [ ] Mockup JS solver differs from Python in the three-constraint case (see
      Progress). Irrelevant once Flask calls Python; noted, not fixed.

## Step 6 — Flask app  [DONE 2026-09-14]

- [x] `flask==3.1.3` installed and in `requirements.txt`.
- [x] `scr/app.py` + one template, mockup v3 CSS and ported JS. Every action
      calls the script functions and shows stdout. No state of its own.
- [x] Book / tactical / constraints / statement editors write only hand-edit
      files, validated, atomic. Live preview via `target.compute` overrides.
- [x] Tests 45 -> 59; ruff clean; live parity and byte-identical round-trip.
- [ ] User review of the app in the browser.
- [ ] Not browser-tested by me: drag-and-drop, tactical search, fork/screen/build
      buttons (API-tested).

## Step 7 — Group-map edits through the desk  [DONE 2026-09-14]

- [x] `common.newer_than`; `baseline.ensure` rebuilds on a newer map or fol.
- [x] Data tab: stale anchors with the reason, unmapped tickers, map edit time.
- [x] Cards and Fork step: re-fork needed when input/ differs from the anchor's
      baseline, with the ratings a re-fork loses.
- [x] Tests 59 -> 62; ruff clean; live state fresh.
- [ ] `group_map_live.csv` stays hand-edited (Excel is the better editor); the
      page only reports on it. Revisit if that gets tedious.

## Step 8 — Desk owns the portfolio files  [DONE 2026-09-14]

- [x] Version check on every save (statement, constraints, book+tactical):
      422 "changed on disk" instead of overwriting a hand edit or another tab.
- [x] Reload button keeps unsaved edits, refreshes versions.
- [x] Target step: "Save and build" when edits are pending.
- [x] Tests 62 -> 63; ruff clean. `CLAUDE.md`: portfolio files are desk-owned.
- [ ] Not browser-tested: Save and build, Reload after a conflict (API-tested).

## Step 9 — Benchmarks in market.db  [DONE 2026-09-15]

- [x] `ingest.py` loads index drops (`Index/Sector` header) into `index_prices`
      beside stock drops; calendar check vs stock sessions (D24, D25).
- [x] Data tab: kind per load, Benchmarks table, price-return note.
- [x] Tests 63 -> 71; ruff clean; live rebuild, stock rows unchanged.
- [ ] Not browser-tested: the Data tab render (API-tested).

## Step 10 — Backtest engine, mockup first

- [x] Engine spec: total return on `close_adj` (D24), benchmark chosen from
      `index_prices` (VNINDEX / VN30 / VN100), price-return gap labelled.
      Follows the mandate (D26), fixed invalid.csv (D27), per-portfolio
      backtest JSON for costs / lag / fill / risk-free rate (D28), chart =
      portfolio vs benchmark (D29).
- [x] Mockup `docs/backtest_mockup.html` (real data, engine in-page; JS =
      Python prototype to 2e-16). Artifact publish was blocked by permission.
- [x] User approval of the mockup and engine logic (2026-09-15).
- [x] Risk-free rate default 6%.
- [x] `scr/backtest_engine.py` + `backtest_config.json` grammar in `common.py`;
      tests 71 -> 89; live parity with the prototype 1e-8.
- [x] Backtest tab in the desk app: `/api/p/<name>/backtest` (run, no writes),
      `/backtest_config` (save, version-checked); tests 89 -> 91;
      browser-checked on a test desk (run, stale bar, benchmark and rf switch).
- [ ] Not browser-tested: Save settings, Revert, a FAIL result.
- [ ] Stock history in `market.db` starts 2026-01-05; a 2025 window needs a
      2025 stock drop (and index history to match).

## Step 11 — Active-weight tilt and triggers (D31-D43)  [DONE 2026-09-16]

- [x] `target.py`: neutral over non-NO groups + active pp; tiers 3/6/9; net,
      range, floor, budget checks (strict build, non-strict preview).
- [x] `constraints.json` `active.budget_pp` (default 20).
- [x] Book grammar row 0 = pp, row 1 = `NO|UW3..UW1|AV|OW1..OW3`; fork,
      auto-NO and carry follow.
- [x] Backtest: drift vs the re-derived target (D42); breach trigger at 10%
      of a cap's limit (D43); floored past groups reported.
- [x] Desk: side + tier + bounded pp input, net and budget meters, Balance
      to zero with undo, Save blocked until balanced; budget in Constraints;
      Target shows neutral, vs neutral, vs base; breach in the Backtest tab.
- [x] Three live books converted (weights reproduced); backups in the
      session scratchpad.
- [x] Retired `backtest.py`, `load_history.py`, `local_history.*`, the
      sticky root baseline, `sector_cap.json`, `backtest_rebalance.*`.
- [x] Tests 91 -> 101; ruff clean; desk browser-checked on port 5055.
- [ ] `docs/ui_mockup_v3.html` still shows multipliers (reference only).

## Step 12 — Universe-wide screen (D46)  [DONE 2026-09-16]

- [x] `screen` evaluates the anchor universe minus invalidated names;
      `exclusions.csv` gains `in_book`.
- [x] Desk step 3: In-book column, dimmed rows for names already out of the
      book, Select-all limited to in-book hits.
- [x] Tests 101 -> 102; ruff clean; live runs on both screened portfolios.
- [ ] A group rated NO still contributes its names to the screen. Small wart:
      it can suggest invalidating a name in a group you already cut.

## Step 13 — Rating spectrum control (D47)  [DONE 2026-09-16]

- [x] NO on/off button in `#E53935`; spectrum UW3-OW3 with the band lit from
      AV to the selected cell; pp input bounded to the tier, hidden under NO.
- [x] Browser-checked on port 5055; nothing saved.
- [ ] `docs/ui_mockup_v3.html` still shows the old side + tier buttons
      (reference only).

## Step 14 — Group coverage line (D48)  [DONE 2026-09-16]

- [x] Book step shows cover, names kept of total, and the largest survivor's
      portfolio weight per group; amber under one third. Display only.
- [ ] `financial_test` book was saved empty (all groups NO, no names) at
      09:55 on 2026-09-16 — its target is blocked. Restorable from
      `input/sector_constituents.csv` plus the ratings in
      `target/sector_allocation.csv` (built 02:46). Say the word.

## Step 15 — Breach tolerance in the Statement step (D49)  [DONE 2026-09-16]

- [x] `statement.json` `rebalance.breach_tolerance` (fraction, or null to switch
      breach off); the Statement step gets the toggle + % field beside drift; the
      engine and the Backtest tab's Mandate panel read it. A missing key means
      10%, so the live portfolios are unchanged.
- [ ] Decide the tolerance per portfolio. `energy_focus` at the 10% default ran
      6 breach rebalances in eight months; `null` or `0.25` gives 0 (turnover
      28.3% vs 38.4%), `0.02` gives 19 (52.3%, 25.7 bps).

## Step 16 — Standing target, fill at the open, decision log (D50-D54)

Spec: `docs/plan_decision_log.md`. Three steps, in order, each green on
pytest / ruff / `node --check` and logged in `Progress.md` before the next.

- [x] **A. Standing target + breach-to-edge.** Target derived at inception,
      calendar and decision only; breach/drift trade against it (D50).
      Breach clips to the cap and spills within scope (D51). Re-baseline the
      tests; record before/after turnover and trigger counts on
      `energy_focus`.
- [x] **B. Fill at the open** (D52). `open_adj` from `market.db`; two-leg fill
      session; no blind session at lag 1.
- [x] **C. Decision log + replay** (D53, D54). `portfolio/<name>/decisions/`,
      `run(..., timeline=)`, `decision` trigger, desk Record button and
      Backtest replay toggle.
- [x] Calendar clock settled (D55): a decision does not move it; the next
      refresh is the first session of the next period.
- [x] **D. Decision review** (D56). One profile per effective date (a
      re-record replaces it); Record moves to Target; optional Step 7
      Decisions lists the log and shows a profile read-only.

## Step 17 — v2: date-driven portfolio, setup / loop desk (D57-D63)  [DONE 2026-09-21]

Spec: `docs/plan_v2.md`. Built 2026-09-21; see Progress.md.

- [x] **A.** `params.at(session)`, `as_of=`/`spec=` through `target.compute`
      and the engine loaders; energy_focus numbers unchanged.
- [x] **B.** `book.json`, `screens.json`, screens as flags (invalidation
      retired), decision `kind` rules and reset, `priced_as_of`/`setup_hash`/
      holdings/flags, `migrate_v2.py`.
- [x] **C.** Desk: 8-step setup/loop bar, Allocation + Target page, Backtest
      in the flow, Monitor with screen thresholds, Decisions pills and Reset.
- [x] **D.** Retire baseline.py, fork/carry, newer_than, anchor_date.json,
      the migration script.

## Later (not scheduled)

- Minimum trade size; calendar as a review. ~~Trade to band edge~~ → Step 16 A.

- FOL: fill `index/fol.csv` (the `fol` screen itself moves into Step 17 B).
- Rebalance stage consuming `statement.json` mandate; drift = ½ Σ |gap| at
  group grain (D7), against today's re-derived target (D42).
- Survivorship bias, sizing/execution (see `HANDOFF.md` §8).
- Backtest tab: selector to overlay other portfolios' backtests for
  cross-comparison (D29).
