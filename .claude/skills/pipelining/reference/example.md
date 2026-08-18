# Reference example

The visual contract. Rendered 2026-08-13, before custom portfolios were specced.
Match the glyphs, arrows and ordering; the content will have moved on.

```yaml
          ┌────────────────────────┐
          │  data/universe.yml     │  ✎ hand
          │  index: VN100          │
          │  picks: [MSR, F88]     │
          └───────────┬────────────┘
                      │
          ┌───────────┴────────────┐
          │                        │
          ▼                        ▼
   ╔══════════════╗         ╔══════════════╗
   ║  /eod        ║ agent   ║  /live       ║ agent
   ║  5 × MCP     ║         ║  1 × MCP     ║
   ╚══════╤═══════╝         ╚══════╤═══════╝
          │                        │
          ▼                        ▼
  data/raw/<date>.csv      data/live/<ts>.csv
     (verbatim)                 ⊗ dead end
          │
          ▼
   ┌──────────────┐
   │ load_eod.py  │  validate → idempotent write
   └──────┬───────┘
          │
          ▼
  ███ data/eod.parquet ███  ◄── single source of truth
          │
          ├──────────────────────────────────┐
          │                                  │
          ▼                                  │
   ┌───────────────────┐                     │
   │ build_group_map.py│ ◄── group_map_default.csv
   └─────────┬─────────┘        (seed once)  │
             │                               │
             ▼                               │
   index/group_map_live.csv  ✎ hand          │
      append-only, blank = fill me           │
             │                               │
             └──────────────┬────────────────┘
                            │
                            ▼
                  ┌───────────────────┐
                  │ build_baseline.py │  ∩ 107→102
                  └─────────┬─────────┘  fcap = float × close_adj
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
   sector_allocation.csv        sector_constituents.csv
        21 × weight                  21 cols A–Z

              portfolio/hsc_strat_high_growth/  ▢ empty
              portfolio/hsc_strat_soe_dom/      ▢ empty
```

`✎` = you edit · `⊗` = terminal, never feeds parquet · `▢` = spec pending

## Why it reads

- **Sources at the top, one direction.** No back-edges, no side loops.
- **The parquet is the widest node.** Everything downstream converges on it.
- **Dead ends are drawn, not omitted.** `/live` looks like a branch that stops,
  because it is one.
- **Hand-edit points are marked inline.** They're the only places a person can
  change the output.
- **Pending stages appear as empty boxes.** Absence is state worth seeing.
