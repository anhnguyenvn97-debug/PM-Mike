---
name: pipelining
description: Draw the PM-Mike data pipeline as an ASCII flow diagram — inputs, agent MCP calls, scripts, and the files each stage writes. Use when asked to visualize, diagram, map, or show the pipeline, the data flow, or how the project fits together.
---

# Pipelining

One ASCII diagram, drawn from the project as it actually is right now.

The diagram is the whole deliverable. Don't narrate it afterwards — if a fact
matters, it belongs in a box.

## 1. Rediscover the base

Skip whatever this conversation already established. Fill only the gaps.

| Look at | For |
| --- | --- |
| `data/universe.yml` | index + deliberate picks |
| `.claude/commands/*.md` | agent stages, MCP calls per stage |
| `scr/*.py`, `data/*.py` | script stages — read the module docstring, not the code |
| `index/*.csv` | hand-edited layers |
| `portfolio/*/` | outputs, and which folders are still empty |

Every builder here declares its inputs and outputs in its first paragraph. Two
things the directory listing won't tell you:

- **Who edits what.** `data/universe.yml` and `index/group_map_live.csv` are
  hand-edited. `data/eod.parquet` never is.
- **Where a branch dies.** `data/live/*.csv` feeds nothing, by design.

## 2. Draw it

`reference/example.md` is the visual contract. Match it — same glyphs, same
arrows, same top-to-bottom order. Don't invent a layout.

```
┌─ ─┐   plain box     file or artifact
╔═ ═╗   double box    agent stage (MCP calls)
███     solid bar     single source of truth
✎       hand edit
⊗       terminal branch, feeds nothing
▢       exists but empty, spec pending
◄──     annotation
```

- One fenced block, sources at the top, in a ```yaml fence — it colourises the
  labels legibly in the terminal.
- Label any junction that filters or joins: `∩ 107→102`.
- Legend at the bottom, only the glyphs you used.
- Empty folders still appear. A pending stage is pipeline state.

## 3. Close with gaps

Only what is genuinely unresolved: an untested trigger, an undecided rule, an
unspecified stage. Nothing else.
