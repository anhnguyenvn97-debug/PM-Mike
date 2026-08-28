# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

Split by design: **the agent fetches** (FiinQuant MCP → `data/raw/<date>.csv` via `/eod`, `/live`) and **scripts own every write** to `data/eod.parquet` and `data/local_history.db`. Never hand-edit generated artifacts; fix the input and re-run.

Pipeline: `/eod` → `build_group_map` → `build_baseline` → `build_portfolio --fork <name>` → hand-edit `sector_constituents_custom.csv` → `build_portfolio_target` → `backtest` (standalone, replays `local_history.db`).

Hand-edited files only: `data/universe.yml`, `index/group_map_live.csv`, and each `portfolio/<name>/` book plus its `sector_cap.json` / `tactical_group.*` / `backtest_rebalance.json`.

Weighting is always `free_float × market_cap / outstanding_shares` (official close), never `close_adj`.

## Commands

`.venv\Scripts\python.exe scr\<script>.py` — run `ruff check scr` to lint. Every script's module docstring is the spec; read it before editing.
