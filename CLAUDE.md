# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

One market input, one database. FiinPro Portal exports are downloaded by hand into `data/fiinpro/` (stock exports and benchmark index exports side by side, told apart by header; both land in `data/market.db`) and **scripts own every derived write**. There is no MCP or agent step in the data path. Never hand-edit generated artifacts; fix the input and re-run.

Date-driven (v2, `docs/plan_v2.md`, D57-D63). Every calculation takes a date: `common.snap` prices it on the last session on or before it and `params.at(db, session)` computes that session's per-ticker parameters on demand from `market.db`, `index/group_map_live.csv` and `index/fol.csv` (`data/params/<date>.csv` is only a cache the CLI writes). There is no baseline, fork, anchor or freshness chain: an edit to the group map applies on the next calculation.

Pipeline: `ingest` (→ `data/market.db`) → `portfolio new` → setup (`statement.json`, `constraints.json`) → the loop: edit `book.json` → `target` on an effective date and Record (`decisions/`) → backtest → monitor → edit again.

`book.json` is the working allocation, a spec (ratings, active pp, investable names, tactical overlay) and the draft of the next decision. One evaluation rule (`portfolio.evaluate`, D59) serves the live build, the preview and the replay: investable names not trading on the session are dropped, groups the universe lacks are dropped with their pp, groups it gained are rated NO. New listings never enter a book by themselves.

Decisions (D60, D61): the first is `inception` (forced), later ones `period` or `active` and dated after inception, one per effective date (a re-record replaces it). Each stores the spec, the holdings as built, the screen flags on them, `priced_as_of` and a `setup_hash` of `statement.json` + `constraints.json`. `portfolio.py reset` archives them to `decisions/archive/<stamp>/`; the next Record is a new inception.

Screens are flags, never exclusions (D62): `screens.json` (turnover, float cap, FOL limit) marks risks on names; excluding a name is unticking it in the book.

`backtest_engine.py` is the backtest: it replays a portfolio under its own `statement.json` rebalance mandate and `constraints.json` on `data/market.db`, against a benchmark from `index_prices`, every book evaluated on the last session's universe. Before inception the inception decision holds; from each decision on it is the standing target (derived at the decision and on each calendar date, held between them). Triggers are decision, calendar, breach (a cap broken by more than `statement.json` `rebalance.breach_tolerance` of its limit, a missing key meaning 10%, `null` switching breach off) and drift against the standing target. A breach fill only clips the broken cap to its limit; the others trade the whole book to the standing target. `--mechanical` holds `book.json` throughout. `monitor()` (D63, D65, D66) replays every decision from inception to the latest session: the forward test with each session's state (drift, breach, holdings, pending fill), next calendar date, flags. Trading assumptions live in `backtest_config.json`; `run()` writes nothing, the CLI writes `portfolio/<name>/backtest_engine/`. `data/fiinpro/archive/` stays as untouched history.

Hand-edited files: `index/group_map_live.csv`, `index/fol.csv`. Desk-owned files, one per `portfolio/<name>/`: `statement.json`, `constraints.json`, `screens.json`, `backtest_config.json`, `book.json`, `decisions/`. Edit these through the desk UI; a hand edit still works as the fallback, and a desk save built on an older copy of the file is refused (version check) rather than overwriting it. `portfolio/<name>/_v1/` holds the files the v2 migration retired, kept for the user to delete.

Portfolio rules: customisation is group-grain plus which names are investable. A group's rating (`NO`, `UW1-3`, `AV`, `OW1-3`) and active weight in pp move it from its neutral, its float-cap share of the groups not rated NO: the tier caps the pp at 3/6/9, no group goes below 0%, the pp net to zero, and half their absolute sum stays within `constraints.json` `active.budget_pp` (default 20). NO means out of scope. An empty group is NO. A stock is in at most one tactical group. Constraints never leave cash: infeasible settings FAIL.

Weighting is always `free_float × close_raw` (official close), never `close_adj` and never the vendor `market_cap`.

`Task.md` is the work queue and `Progress.md` the log. Read both before starting work.

## Commands

`.venv\Scripts\python.exe scr\<script>.py`. Test with `.venv\Scripts\python.exe -m pytest tests -q`, lint with `.venv\Scripts\ruff.exe check scr tests`. Every script's module docstring is the spec; read it before editing. Tests share one synthetic `market.db` fixture (`tests/conftest.py`, `make_db`).

Desk UI: `.venv\Scripts\python.exe scr\app.py`, then http://127.0.0.1:5000. Setup steps 1-3 (Statement, Rebalancing, Constraints), then the loop: 4 Allocation and 5 Target on one page (the effective date prices the preview and Record builds and records), 6 Backtest, 7 Monitor (with the screen rules), 8 Decisions (read-only, Reset). It calls the script functions and writes only desk-owned files; its preview runs `target.compute`, so solver changes belong in `target.py`, never in `scr/static/desk.js`. `docs/ui_mockup_v3.html` is the visual reference; the flow is `docs/plan_v2.md`.
