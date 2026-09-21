# Plan v2 — date-driven portfolio, setup / loop structure

Written 2026-09-21 after a design session on top of Step 16 (D50–D56), revised
twice the same day with the user's answers. This is the spec for the executing
model. Read `CLAUDE.md`, `Task.md`, `Progress.md`, `docs/plan_decision_log.md`
(the Step 16 plan, same format as this file) and the module docstrings of
`scr/params.py`, `scr/baseline.py`, `scr/portfolio.py`, `scr/target.py`,
`scr/backtest_engine.py`, `scr/common.py` and `scr/app.py` first; every
docstring is its script's spec and must be updated with the code.

Rules that hold throughout: PowerShell only; `.venv\Scripts\python.exe`;
`uv pip install`; scripts own every derived write; no commits until asked;
no over-engineering — build what this file says and nothing speculative. Each
step ends with `pytest tests -q`, `ruff check scr tests` and
`node --check scr\static\desk.js` green, a `Progress.md` entry (newest first,
naming the D-numbers landed) and the `Task.md` box ticked. Live
`portfolio/<name>/` data is migrated by a script, never by hand, and stays
unstaged. Before starting each step, state the plan for that step and wait for
the user's go; questions this file does not answer go to the user, not to a
guess.

---

## Why

The anchor exists only because the baseline is a **precomputed file**. Every
portfolio step reads a copy of that file (`input/`), so a monthly Fork is needed
to move the copy, a freshness chain (`common.newer_than`) is needed to police
it, and the book is merged onto the new copy by a rule (`portfolio.carry`) that
differs from the rule the backtest uses to replay old decisions.

In v2 the numbers for any session are computed on demand from `data/market.db`.
The date becomes an argument to every calculation. Fork, `baseline/`, `input/`,
the sticky anchor and the freshness chain go away; one evaluation rule serves
the live build and the backtest replay. The desk splits into a **setup** block
(the mandate, edited rarely) and a **loop** (allocate → build target and record
→ test → monitor → allocate again).

Weighting stays `free_float × close_raw` at the chosen session. Nothing about
the tilt maths (D31–D43), the caps or the standing target (D50–D52) changes.

---

## Decisions taken (record these in Progress.md as you land them)

- **D57 date-driven params.** `params.at(db, session)` returns the params frame
  for any session in `market.db` (group, close_raw, free_float, float_cap,
  21-session liquidity). `data/params/<date>.csv` is written as a cache and for
  inspection; nothing reads it as an input of record. `baseline.py`, Fork,
  `input/`, `index/anchor_date.json` and `common.newer_than` are retired.
  Editing `index/group_map_live.csv` or `index/fol.csv` needs no rebuild step;
  the next calculation reads them.
- **D58 the book is a spec.** `portfolio/<name>/book.json` holds the working
  allocation in the decision-JSON shape (`groups`, `tactical`). It replaces
  `sector_constituents_custom.csv`, `tactical_group.csv` and
  `tactical_group.json`. The live book **is** the draft of the next decision;
  Record freezes it.
- **D59 one evaluation rule.** A spec evaluated on session `d` keeps its ratings
  and pp, drops investable names not trading on `d`, drops groups absent from
  the group map, and rates gained groups NO. This is
  `portfolio.reconcile_profile` today; it now serves the live build too.
  New listings never enter a book by themselves (changes Fork's auto-add); the
  Allocation page marks names that are in the universe but not in the last
  decision as "new" so the user sees them.
- **D60 effective date sizes the target; kinds are enforced.** Target build
  takes an effective date. It snaps to the last session at or before it
  (`priced as of` in the output when they differ), sizes on that session and
  records the spec, the built holdings and the screen flags (D62) in
  `decisions/<date>.json`. Kind rules:
  - The first decision of a portfolio is **inception**, forced; the desk shows
    no kind choice for it.
  - Every later decision is **period** or **active**, chosen by the user. Both
    become the standing target from their session on; the label is for the
    record, it does not change the replay.
  - A later decision's effective date must be after inception. Re-recording
    the inception date replaces inception (still one per date, D56).
  - **Reset** is the only way to a new inception: it moves `decisions/` to
    `decisions/archive/<YYYY-MM-DD-HHMM>/` (nothing deleted). `book.json` and
    `target/` stay as they are; the next Record is inception again and
    rebuilds `target/`. Desk button on the Decisions step with a confirm; CLI
    `portfolio.py reset <name>`.
- **D61 backtest anchors on decisions.** Unchanged from D53–D55, restated:
  before inception the backtest holds the inception profile (today's "first
  decision opens the window"); from inception on, the most recent decision in
  force is the standing target that calendar, drift and breach rebalances trade
  to, until the next decision replaces it. `--mechanical` holds `book.json`
  throughout, as it holds the grids today. Settings (statement, constraints)
  are today's. Each decision stores `setup_hash` over `statement.json` and
  `constraints.json`; Decisions and the backtest show a "setup differs" pill on
  entries recorded under other settings. No back-dating guard beyond D60.
- **D62 screening is a flag, not an exclusion.** Screens (turnover, float cap,
  foreign-ownership room) mark risks; they never remove a name. Excluding a
  name is unticking it in Allocation, which the decision records. So:
  - Flags are computed for the whole universe shown in Allocation (so the user
    sees them while ticking names), measured at the effective date's session.
    The decision stores the flags on its holdings only. Monitor re-measures
    them on the latest session for the standing target.
  - `screen/invalid.csv`, `screen/exclusions.csv`, `--invalidate` and
    `restore` are retired, and with them the "invalidated names are never
    investable or claimable" rule. This also removes the D54 look-ahead: every
    exclusion is dated by the decision that made it.
  - Thresholds live in `screens.json`, edited on the Monitor step. They are
    not part of `setup_hash` (they change no weight).
  - The foreign-ownership screen (`fol`) reads `index/fol.csv`; a name with no
    row shows "no data" rather than passing. The "FOL screen" item under Later
    in `Task.md` moves here.
- **D63 Monitor.** A live page fed by the engine: run from the last decision's
  session to the latest session, hold the standing target, report drift,
  breaches, the next calendar session and the screen flags on current
  holdings. With no decision recorded it says so and shows nothing else. It
  writes nothing but `screens.json`. It tracks the paper portfolio from the
  standing target, not broker fills or cash.

---

## Target layout

```
data/market.db                        unchanged
data/params/<date>.csv                cache only (D57)
index/group_map_live.csv, fol.csv     hand-edited, unchanged
portfolio/<name>/
  statement.json                      setup 1–2: mandate, holdings range, rebalance rule
  constraints.json                    setup 3
  screens.json                        loop 7: screen thresholds (moved out of statement.json)
  backtest_config.json
  book.json                           loop 4: the working spec (D58)
  decisions/log.csv                   id, effective, recorded_at, priced_as_of, kind, setup_hash, note
  decisions/<date>.json               spec + holdings as built + screen flags + setup_hash
  decisions/archive/<stamp>/          earlier decision sets, moved there by Reset (D60)
  target/                             last build: holdings.csv, sector_allocation.csv, built_from.txt
  backtest_engine/                    CLI output, unchanged
  _v1/                                files the migration retired, kept for the user to delete
```

Removed: `portfolio/baseline/`, `portfolio/<name>/input/`,
`sector_constituents_custom.csv`, `tactical_group.*`, `screen/`,
`index/anchor_date.json`, `scr/baseline.py`, `portfolio.fork`, `portfolio.carry`,
`common.newer_than` and the desk's re-fork flags.

---

## Desk structure

```
Setup   1 Statement      mandate, holdings range          statement.json
        2 Rebalancing    frequency, drift, breach tol      statement.json (rebalance block)
        3 Constraints    budget and caps                   constraints.json
Loop    4 Allocation     ratings, pp, investable names,    book.json          } one page,
                         new-name and screen-flag marks                       } two steps
        5 Target         effective date, kind, note,       target/, decisions/ }
                         weights, flags, Record
        6 Backtest       replay of decisions/ (existing tab, moved into the flow)
        7 Monitor        drift, breach, calendar due, screen flags, thresholds  (D63)
        8 Decisions      read-only log and profiles, Reset (optional)
```

Setup steps are always available. Loop steps need a valid statement. After
Monitor the user returns to 4; step 8 is a review tab, not part of the loop.

Steps 4 and 5 share one page with two sections: Allocation on top (group table
with rating, pp and investable names), Target below (effective date, kind,
note, the weights priced as of the effective date, the flags on those weights,
and Record). Both steps sit in the step bar with their own status; clicking
either opens the page scrolled to its section. There is no separate Build
button: Record builds `target/` and writes the decision. "Build target"
survives as a CLI verb (`target.py build <name> --as-of <date>`) for a build
without a record.

The universe shown in Allocation is `params.at(db, snap(effective))`, so a
back-dated decision is built on the universe of its own date. A name in
`book.json` that is not trading on that session is shown greyed with "not
trading"; it stays in the spec (it may trade again) but is dropped at
evaluation (D59), and Target lists the drops.

The Data tab keeps ingest and the drop profile; the "Build params and
baseline" button and the re-fork flags go.

---

## Step A — `params.at` and a date argument through the solver

Grids and `input/` still exist during this step; the aim is that every number
can be produced from a date, with the file path kept working until B.

### A1. `scr/params.py`, `scr/common.py`
Add `params.at(con_or_db, session, gmap, fol) -> DataFrame` around the
existing `compute`; `session` must be a session in `market.db` (raise
otherwise). Keep the CLI writing `data/params/<date>.csv`; add `--as-of`. Add
`common.sessions(db) -> DatetimeIndex` and `common.snap(db, date) -> session`
(last session ≤ date; raise if before the first session).

### A2. `scr/target.py`
`compute(name, ..., as_of=None, spec=None)`: `as_of` snaps per A1 and replaces
`params_dir`+anchor; `spec` (D58 shape) replaces the `book`/`tactical` grids.
The universe grid is built from the params frame (group → tickers by float
cap), not from `input/`. Evaluation goes through `reconcile_profile` (D59)
before the tilt. `build(name, as_of, ...)` writes `target/` with
`priced_as_of` in `built_from.txt`. Remove the `forked_from` reads and the
re-fork hint. The existing `portfolio.book_spec(home)` turns the grids into a
spec while they remain.

### A3. `scr/backtest_engine.py`
`load_book`/`load_profile` call the new `compute(spec=..., as_of=session)`.
The engine already prices per session; only the loading path changes.
`params.at` is called only where a target is derived (inception, decisions,
calendar dates), never per session. Numbers on `energy_focus` must be
unchanged to 1e-12 when the spec equals the current book.

### A4. Tests
Move `tests/test_backtest_engine.make_db` into `tests/conftest.py` as the
shared `market.db` fixture; new tests build from it, not from CSV grids.
`tests/test_params.py`: `at` equals the CSV `compute` for the anchors on disk.
`tests/test_target.py`: `as_of` snapping, a name not trading is dropped with a
message, `spec=` equals the grid path on the same data.

---

## Step B — book.json, screen flags, kinds, migration

### B1. `scr/portfolio.py`
- `read_book(home) -> spec`, `write_book(home, spec)` on `book.json`; drop the
  grid readers/writers once nothing calls them.
- `screen(home, as_of, names) -> flags` computes the D62 flags from
  `params.at` and `index/fol.csv` for the given names; the CLI prints them for
  the standing target. `--invalidate`, `restore` and `exclusions.csv` go.
- `record_decision(home, effective, kind, note)`: enforces D60 (first is
  inception, later ones period/active and after inception), stores the spec,
  the holdings from `target.compute(as_of=effective)`, the flags on those
  holdings, `priced_as_of`, `kind` and `setup_hash`. Keeps "one per date,
  replace" (D56). `log.csv` gains the new columns.
- `reset(home)`: moves `decisions/` to `decisions/archive/<stamp>/`.

### B2. `scr/common.py`
- `validate_decision` gains `kind ∈ {inception, period, active}`,
  `priced_as_of`, `setup_hash`, `holdings` (list of `{t, group, w}`) and
  `flags`, all optional for old files (an old file with no `kind` reads as
  inception if it is the earliest, else period).
- `screens.json` schema and `validate_screens`; `SCREENS` gains `fol`.
- `setup_hash(home)` over `statement.json` and `constraints.json`.
- `read_invalid` and the invalid-file columns go.

### B3. `scr/migrate_v2.py` (one-shot, then deleted in Step D)
For each `portfolio/<name>/` that has a `statement.json` (skips `baseline/`
and anything else):
- grids → `book.json`; names listed in `screen/invalid.csv` become
  non-investable in the book and are dropped from tactical groups (the same
  effect they have today);
- the screens block moves out of `statement.json` into `screens.json`;
- `decisions/<date>-NN.json` → `<date>.json`, keeping the latest per date;
  the others go to `_v1/decisions/`; `kind` is filled per B2; `log.csv` is
  rewritten with the new columns;
- `input/`, `screen/` and the grid files move to `portfolio/<name>/_v1/`.
  Nothing is deleted; the user deletes `_v1/` once satisfied.

Dry-run by default, `--apply` to write, prints every change. Run it on a
scratch copy first and diff the `target.compute` result before and after.

### B4. Tests
Round trip of `book.json`; kind rules (first forced inception, later before
inception refused, re-record of inception date stays inception, reset
archives and the next record is inception); flags stored in the decision and
never removing a name; `fol` "no data"; migration on a fixture equals the
hand-written expected files.

---

## Step C — desk: setup / loop, Allocation + Target page, Monitor

### C1. `scr/app.py`
Routes: `GET/PUT /api/p/<name>/screens`; `PUT /api/p/<name>/book` takes the
spec and writes `book.json`; `POST /api/p/<name>/preview {spec, as_of}`
returns the universe, weights, drops and flags priced as of the date;
`POST /api/p/<name>/decisions {effective, kind, note, version}` records and
builds; `POST /api/p/<name>/reset`; `GET /api/p/<name>/monitor` runs the
engine from the last decision to the latest session and returns drift,
breaches, next calendar session and flags. Remove `/fork`, `/screen`
invalidate and restore, the params/baseline build, the re-fork flags and the
sticky anchor from `/api/state`.

### C2. `scr/static/desk.js`, `desk.css`
- `STEPS` becomes the 8-step setup/loop list; the step bar shows the two
  blocks.
- Steps 4 and 5 render one page (today's Book and Target panels) scrolled to
  the clicked section. The Target section has the effective date, kind (hidden
  and fixed to inception on the first record), note, drops, flags and Record;
  the preview re-prices when the date changes. "New" and flag marks in
  Allocation.
- Backtest moves from its own page into step 6 (same code, same tab state).
- Monitor per D63, with the screen thresholds editor.
- Decisions gains `kind`, `priced_as_of`, the flags, the "setup differs" pill
  (D61) and Reset with a confirm.
- Remove the Fork step, the anchor picker, the invalidate/restore controls,
  the Data-tab build button and the "re-fork needed" card text.

### C3. Docs
Docstrings of every touched module; `CLAUDE.md` Architecture, pipeline line,
portfolio rules (the invalidation rule goes, D62), freshness paragraph (goes)
and the hand-edited/desk-owned file lists; `docs/ui_mockup_v3.html` stays as
the visual reference but the flow text in it is superseded by this file.

---

## Step D — retire

Delete `scr/baseline.py`, `portfolio.fork/carry`, `common.newer_than`,
`index/anchor_date.json`, `portfolio/baseline/`, `scr/migrate_v2.py` and
their tests. Grep for `anchor`, `forked_from`, `input/`, `newer_than`,
`sticky`, `invalid`, `exclusions` and remove the last references. `Task.md`:
close Step 17.

---

## Checks that gate each step

- `energy_focus` backtest, mechanical: identical to the Step 16 numbers after A
  and B (5.99% total return, 8/2/0 triggers at the time of writing).
- Replay with the recorded 2026-09-11 decision (inception after migration):
  identical to mechanical.
- Migration dry-run on a scratch copy of `portfolio/` shows no unexpected
  changes; `target.compute` before and after differs only by names the
  D59 rule drops (list them in `Progress.md`).
- Desk walk-through on a scratch copy for every step, as in Step 16 (a copy of
  `portfolio/` under the scratchpad directory, `app.py` pointed at it on a
  spare port, checked in the browser), including a Reset and a fresh
  inception.

## Not in scope

Per-decision replay on each decision's own setup (D61 flags the difference
instead). Actual traded positions and cash: Monitor tracks the paper portfolio
from the standing target, not fills from a broker.
