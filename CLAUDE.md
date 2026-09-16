# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

One market input, one database. FiinPro Portal exports are downloaded by hand into `data/fiinpro/` (stock exports and benchmark index exports side by side, told apart by header; both land in `data/market.db`) and **scripts own every derived write**. There is no MCP or agent step in the data path. Never hand-edit generated artifacts; fix the input and re-run.

Pipeline: `ingest` (→ `data/market.db`) → `params` (→ `data/params/<anchor>.csv`) → `baseline` (→ `portfolio/baseline/<anchor>/`) → `portfolio new` → `portfolio fork --anchor` → `portfolio screen` (optional; suggests, `--invalidate` acts; it reads the whole anchor universe, not the curated book, and marks each hit `in_book` yes/no) → edit the book and tactical overlay → `constraints.json` → `target`.

Each portfolio sits on its own anchor (`input/forked_from.txt`). `index/anchor_date.json` is only the default anchor (`baseline.sticky_anchor`).

`backtest_engine.py` is the backtest: it replays a portfolio under its own `statement.json` rebalance mandate, book and `constraints.json` (weights from `target.py`'s functions, re-derived on every session a trigger is checked) on `data/market.db`, against a benchmark from `index_prices`. Triggers are calendar, breach (a cap broken by more than `statement.json` `rebalance.breach_tolerance` of its limit, a missing key meaning 10%, `null` switching breach off) and drift against the re-derived target. Trading assumptions live in `portfolio/<name>/backtest_config.json` (desk-owned, saved from the Backtest tab); `run()` writes nothing and backs the desk's Backtest tab, the CLI writes `portfolio/<name>/backtest_engine/`. The legacy `backtest.py`, `load_history.py` and `local_history.db` are retired (recoverable from git history); `data/fiinpro/archive/` stays as untouched history.

Hand-edited files: `index/group_map_live.csv`, `index/anchor_date.json`, `index/fol.csv`. Desk-owned files, one per `portfolio/<name>/`: `statement.json`, `constraints.json`, the book (`sector_constituents_custom.csv`), `tactical_group.*`, `screen/invalid.csv`. Edit these through the desk UI; a hand edit still works as the fallback, and a desk save built on an older copy of the file is refused (version check) rather than overwriting it.

Portfolio rules: customisation is group-grain plus which names are investable. A group's rating (`NO`, `UW1-3`, `AV`, `OW1-3`) and active weight in pp move it from its neutral, its float-cap share of the groups not rated NO: the tier caps the pp at 3/6/9, no group goes below 0%, the pp net to zero, and half their absolute sum stays within `constraints.json` `active.budget_pp` (default 20). NO means out of scope. An empty group is NO. A stock is in at most one tactical group; invalidated names are never investable or claimable. Constraints never leave cash: infeasible settings FAIL.

Weighting is always `free_float × close_raw` (official close), never `close_adj` and never the vendor `market_cap`.

`Task.md` is the work queue and `Progress.md` the log. Read both before starting work.

## Commands

`.venv\Scripts\python.exe scr\<script>.py`. Test with `.venv\Scripts\python.exe -m pytest tests -q`, lint with `.venv\Scripts\ruff.exe check scr tests`. Every script's module docstring is the spec; read it before editing.

Desk UI: `.venv\Scripts\python.exe scr\app.py`, then http://127.0.0.1:5000. It calls the script functions and writes only hand-edit files; its preview runs `target.compute`, so solver changes belong in `target.py`, never in `scr/static/desk.js`. `docs/ui_mockup_v3.html` is the approved design reference.

Freshness: `common.newer_than` is the one staleness rule (drops, `group_map_live.csv`, `fol.csv` -> params -> baseline -> portfolio `input/`, the last link by content). After editing the group map, press "Build params and baseline" on the Data tab, then re-fork the portfolios the page flags; nothing rebuilds a portfolio on its own.
