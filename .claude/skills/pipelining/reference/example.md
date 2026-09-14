# Reference example

The visual contract. Rendered 2026-09-14, after the FiinPro-only rebuild.
Match the glyphs, arrows and ordering; the content will have moved on.

```yaml
   ┌──────────────────────────────┐
   │ data/fiinpro/VN100 data.xlsx │  ✎ hand drop
   └──────────────┬───────────────┘
                  │
                  ▼
          ╔══════════════╗
          ║  ingest.py   ║  validate → rebuild from scratch
          ╚══════╤═══════╝
                 │
                 ▼
  ███ data/market.db ███  ◄── single source of truth
       169 sessions × 100 tickers
                 │
                 │      index/group_map_live.csv  ✎
                 │      index/anchor_date.json    ✎  2026-09-11
                 │      index/fol.csv             ▢ deferred
                 ▼             │
          ╔══════════════╗     │
          ║  params.py   ║ ◄───┘
          ╚══════╤═══════╝
                 │
                 ▼
   data/params/2026-09-11.csv   float cap, 21d turnover
                 │
                 ▼
          ╔══════════════╗
          ║ baseline.py  ║  ∩ 100 tickers, 20 groups
          ╚══════╤═══════╝
                 │
                 ▼
   portfolio/baseline/<anchor>/  one per anchor (+ sticky root copy)
                 │
                 ▼
          ╔══════════════════╗
          ║ portfolio.py     ║  new → statement.json ✎
          ║ new/fork/screen  ║  fork --anchor → book ✎
          ╚══════╤═══════════╝  screen → exclusions.csv, --invalidate → invalid.csv
                 │
                 ▼
          ╔══════════════╗
          ║  target.py   ║ ◄── constraints.json ✎, tactical_group.* ✎
          ╚══════╤═══════╝
                 │
                 ▼
   portfolio/<name>/target/  holdings.csv
                 │
                 ▼
          sizing & execution  ⊗ not built
```

`✎` = you edit · `⊗` = not built · `▢` = spec pending

## Why it reads

- **Sources at the top, one direction.** No back-edges, no side loops.
- **The database is the widest node.** Everything downstream converges on it.
- **Unbuilt stages are drawn, not omitted.** Sizing looks like a branch that
  stops, because it is one.
- **Hand-edit points are marked inline.** They're the only places a person can
  change the output.
- **Pending inputs appear as empty boxes.** Absence is state worth seeing.
