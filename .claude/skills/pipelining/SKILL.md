---
name: pipelining
description: Draw the PM-Mike data pipeline as an ASCII flow diagram — FiinPro drops, scripts, hand-edited files, and the outputs each stage writes. Use when asked to visualize, diagram, map, or show the pipeline, the data flow, or how the project fits together.
---

# Pipelining

One ASCII diagram, drawn from the project as it actually is right now.

The diagram is the whole deliverable. Don't narrate it afterwards — if a fact
matters, it belongs in a box.

## 1. Rediscover the base

Skip whatever this conversation already established. Fill only the gaps.

| Look at | For |
| --- | --- |
| `data/fiinpro/` | the drops feeding ingest (top level only; `archive/` feeds the legacy store) |
| `data/market.txt` | database state: rows, tickers, sessions, quality |
| `scr/*.py` | script stages — read the module docstring, not the code |
| `index/*` | hand-edited layers: group map, anchor date, FOL |
| `portfolio/*/` | statements, books, overlays, outputs, and which folders are empty |

Every script declares its inputs and outputs in its docstring. Two things the
directory listing won't tell you:

- **Who edits what.** `index/*`, `statement.json`, the book and the overlay
  files are hand-edited. `market.db`, `params/`, `baseline/`, `input/`,
  `screen/` and `target/` never are.
- **What is legacy.** `load_history.py` → `local_history.db` → `backtest.py`
  is frozen and runs beside the main pipeline, not inside it.

## 2. Draw it

`reference/example.md` is the visual contract. Match it — same glyphs, same
arrows, same top-to-bottom order. Don't invent a layout.

```
┌─ ─┐   plain box     file or artifact
╔═ ═╗   double box    script stage
███     solid bar     single source of truth
✎       hand edit
⊗       terminal branch, feeds nothing / not built
▢       exists but empty, spec pending
◄──     annotation
```

- One fenced block, sources at the top, in a ```yaml fence — it colourises the
  labels legibly in the terminal.
- Label any junction that filters or joins: `∩ 100 tickers, 20 groups`.
- Legend at the bottom, only the glyphs you used.
- Empty folders still appear. A pending stage is pipeline state.

## 3. Close with gaps

Only what is genuinely unresolved: an unset decision, an unbuilt stage, a
deferred input. Nothing else.
