# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

One market input, one database. FiinPro Portal exports are downloaded by hand into `data/fiinpro/` and **scripts own every derived write**. There is no MCP or agent step in the data path. Never hand-edit generated artifacts; fix the input and re-run.

Pipeline: `ingest` (→ `data/market.db`) → `params` (→ `data/params/<anchor>.csv`) → `baseline` (→ `portfolio/baseline/<anchor>/`) → `portfolio new` → `portfolio fork --anchor` → `portfolio screen` (optional; suggests, `--invalidate` acts) → edit the book and tactical overlay → `constraints.json` → `target`.

Each portfolio sits on its own anchor (`input/forked_from.txt`). `portfolio/baseline/` root holds a sticky copy of the `index/anchor_date.json` anchor, kept only because `backtest.py` reads it.

`backtest.py` is legacy and frozen: it still replays `data/local_history.db`, built by `load_history.py` from `data/fiinpro/archive/`, and reads the root baseline and `sector_cap.json`. Do not change its behaviour. `target.py` never reads `sector_cap.json`.

Hand-edited files: `index/group_map_live.csv`, `index/anchor_date.json`, `index/fol.csv`, plus legacy `sector_cap.json` and `backtest_rebalance.json`. Desk-owned files, one per `portfolio/<name>/`: `statement.json`, `constraints.json`, the book (`sector_constituents_custom.csv`), `tactical_group.*`, `screen/invalid.csv`. Edit these through the desk UI; a hand edit still works as the fallback, and a desk save built on an older copy of the file is refused (version check) rather than overwriting it.

Portfolio rules: customisation is group-grain (ratings, multipliers) plus which names are investable. An empty group is NO. A stock is in at most one tactical group; invalidated names are never investable or claimable. Constraints never leave cash: infeasible settings FAIL.

Weighting is always `free_float × close_raw` (official close), never `close_adj` and never the vendor `market_cap`.

`Task.md` is the work queue and `Progress.md` the log. Read both before starting work.

## Commands

`.venv\Scripts\python.exe scr\<script>.py`. Test with `.venv\Scripts\python.exe -m pytest tests -q`, lint with `.venv\Scripts\ruff.exe check scr tests`. Every script's module docstring is the spec; read it before editing.

Desk UI: `.venv\Scripts\python.exe scr\app.py`, then http://127.0.0.1:5000. It calls the script functions and writes only hand-edit files; its preview runs `target.compute`, so solver changes belong in `target.py`, never in `scr/static/desk.js`. `docs/ui_mockup_v3.html` is the approved design reference.

Freshness: `common.newer_than` is the one staleness rule (drops, `group_map_live.csv`, `fol.csv` -> params -> baseline -> portfolio `input/`, the last link by content). After editing the group map, press "Build params and baseline" on the Data tab, then re-fork the portfolios the page flags; nothing rebuilds a portfolio on its own.
