# Plan v3 — unified rebalance timing, derived period decisions, replication

Written 2026-09-24 after a design session on top of v2 (D57–D66), revised the
same day after an engineering review. This is the spec for the executing
model. Read `CLAUDE.md`, `Task.md`, `Progress.md`, `docs/plan_v2.md` and the
module docstrings of `scr/params.py`, `scr/portfolio.py`, `scr/target.py`,
`scr/backtest_engine.py`, `scr/common.py` and `scr/app.py` first; every
docstring is its script's spec and must be updated with the code.

Rules that hold throughout: PowerShell only; `.venv\Scripts\python.exe`;
`uv pip install`; scripts own every derived write; no commits until asked; no
over-engineering — build what this file says and nothing speculative. Each step
ends with `pytest tests -q`, `ruff check scr tests` and
`node --check scr\static\desk.js` green, a `Progress.md` entry (newest first,
naming the D-numbers landed) and the `Task.md` box ticked. Live
`portfolio/<name>/` data stays unstaged. Before starting each step, state the
plan for that step and wait for the user's go; questions this file does not
answer go to the user, not to a guess.

---

## Why

Two problems, one cause.

**The calendar is out of step with decisions.** An active decision references
its effective date's close and fills at the next open (D64). A calendar
rebalance references the *first* session of the new period and fills the
session after, so a new period opens holding the old period's target for two
sessions. Same event, two timings.

**Periodic rebalances are invisible.** The engine re-derives the standing
target at every calendar boundary from the decision in force, but nothing in
`decisions/` records it. On `finance_advance` the 2026-08-15 decision was
re-derived on 2026-09-03; the Decisions tab shows only the August entry, so
"the last decision" and "the book the portfolio actually holds" stop being the
same thing. Measured: the recorded weights are 0.451 pp away from the standing
target from 4 September on — 428m VND of stock at a 100bn AUM — and 3.62 pp
with the caps switched off.

Replication (the third desk section) needs a single answer to "what does this
portfolio hold as its target on this session". The engine already has that
answer for every session: the forward test's per-session trace (D66). v3 makes
the calendar follow the decision timing, shows the calendar rebalances, and
reads replication from that trace, so there is one source for the target in
force and nothing is hidden behind the Decisions log.

---

## Decisions taken (record these in Progress.md as you land them)

- **D67 one rebalance timing rule.** Every rebalance references a **session
  close** and fills at the **next session's open**. Which close is the
  reference is the only difference between kinds:
  - an active decision (`inception`, `period`, `active`) references the last
    session on or before its effective date — unchanged from D64;
  - a **calendar** rebalance references the **last session of the period
    ending**, and therefore fills at the open of the first session of the new
    period.

  In `simulate()` the calendar trigger moves from "session `i` starts a new
  period" (`keys[i] != keys[i-1]`) to "session `i` ends one"
  (`keys[i+1] != keys[i]`).

  **Attainability.** A rebalance exists only when both its reference close and
  its fill open are in `market.db`: the reference is at most the
  **second-to-last session** and the fill at most the **last session**, whose
  open is the fill price. Nothing is derived on the last session and nothing
  looks past the data:
  - a calendar boundary is known only once the first session of the new
    period is in the data; until then the period is not over;
  - a recorded decision whose reference session is the last session, or whose
    effective date is after it, is **unattainable**. It stays recorded, is
    flagged `unattainable` everywhere it is shown (Target, Decisions, Monitor,
    Replication), and the decision before it stays in force. It becomes
    attainable by itself when a drop adds the next session; the status is
    computed on read, never stored. This replaces the engine's "after the
    history" and "not reached" statuses and Monitor's "too recent to monitor".
  - the effective-date picker on the Target step and the execution-date
    picker on Replication stop at the last session. Choosing the last session
    as an effective date is allowed and shows `unattainable` beside Record.

  `lag_sessions` is pinned to 1 and stops being a desk dial. All three live
  portfolios already run 1, so nothing migrates. It stays in
  `backtest_config.json` as a validated backtest-only key (three engine tests
  use 2), a value other than 1 WARNs, and Monitor and Replication always use 1.

  This revises D52 and D64 (timing), D50 (which sessions derive) and D63/D66
  (Monitor's handling of the last decision).

  Consequence, and the reason the change is small: the D64 collision block
  (`backtest_engine.py:472-478`) collapses. A decision that is also a calendar
  rebalance is now exactly one test — its reference session is the period's
  last session — instead of three cases about fills landing on or crossing a
  boundary. The `i > start` guard means an inception on a period's last
  session simply *is* that boundary's rebalance, correctly and without a tag.
  The existing `i + lag < N` guard already refuses a trigger on the last
  session; D67 names that refusal and reports it.

- **D68 periodic rebalances surface as derived decisions, never written.**
  Every calendar rebalance since inception appears in the Decisions log and
  anywhere "the last decision" is read, marked `source: "derived"`; recorded
  decisions are `source: "recorded"`. `kind` is not overloaded — a derived
  entry carries `kind: "period"` and its `source` tells it from a period
  decision the user recorded.
  - They are **derived on read and never written to `portfolio/<name>/`**. A
    materialised `decisions/<date>.json` for a calendar rebalance would be a
    precomputed file whose inputs keep moving (the group map is hand-edited and
    `params.at` reads it live), which is the baseline problem v2 deleted, and
    it would break `reset` and "one per effective date, a re-record replaces
    it".
  - **One source.** `backtest_engine.timeline(name)` replays every recorded
    decision from inception (`run(timeline=load_decisions(home), daily=True)`,
    the same run `monitor()` makes, 0.1–0.2 s today) and returns the recorded
    decisions plus the `calendar` rows of that run's `rebalances`, in session
    order. It lives in the engine beside `monitor()`, not in `common.py`
    (`backtest_engine` imports `common`). `common.load_decisions` is unchanged
    and stays the record of what the user chose; the engine keeps reading it.
  - Derived entries are **not** fed back into `run(timeline=...)`. As regimes
    they would fire a `decision` trigger instead of `calendar` — same weights,
    wrong label.
  - A derived entry is recomputed on **today's** group map and today's
    recorded decisions, so the same entry can read differently next month.
    The Decisions tab says so on those rows.
  - Recording an active decision on a date that already has a derived entry
    replaces it — the existing `due` branch (`backtest_engine.py:482`) already
    wins and tags `also = "calendar"`. No new machinery.
  - The next due calendar date stays `next_calendar()`, which reports the
    **trade** date; Monitor also shows the **reference** date (the last
    weekday of the current period; holidays are not known ahead).

- **D69 replication.** A third desk section after Market data and Portfolios:
  what a portfolio would look like if it were established **from cash** at a
  given AUM on a given date.
  - Inputs: portfolio, AUM in VND, cash percentage, execution date. Defaults:
    the last session as the execution date.
  - `exec_session` is the first session **on or after** the execution date —
    replication is the one calculation that snaps forward, because it fills at
    an open. The picker stops at the last session; a date past it (API or
    hand input) is `unattainable` and builds no ticket.
  - **The reference session is `exec_session − 1`**, and the reference book is
    the **standing target at its close in the forward-test trace** (`run(...,
    daily=True)`, D66: per-name `target` and `target_as_of`). That is the
    target in force — recorded or derived, whichever came last, with names
    lacking a float cap already dropped and renormalised, exactly as the
    engine would trade it. "Decide 15 August, execute 20 August" needs no
    special case: the close of 19 August still holds the target derived on 14
    August.
  - An execution session at or before inception's fill has no book in force
    and says so. An unattainable decision recorded after the reference session
    is named on the ticket: "decision X is recorded but unattainable; this
    ticket uses Y (`target_as_of`)".
  - **Sizing price is the reference session's close** (`close_raw`), the last
    price known when the order is submitted — the same close the target is
    read on. The execution session's `open_raw`, which always exists under
    D67, is reported beside it with the spend at that open; it never changes
    the share counts.
  - Conversion: `equity = AUM × (1 − cash)`; `lots_i = floor(equity × w_i /
    (LOT × px_i))`; then largest-remainder apportionment of the residual —
    rank by fractional lot remainder, add one lot each while it still fits in
    `equity`, tie-break on larger target weight then ticker A-Z. Nobody exceeds
    target by more than one lot, spend never exceeds the budget, the result is
    deterministic. `LOT = 100` is a constant in `common.py`, not a setting.
  - Cash is a **hard floor at the sizing price**: the residual falls to cash,
    so actual cash at the sizing close is always at or above the requested
    percentage. Requested, actual at the close and actual at the open are all
    reported.
  - A name in the reference book with no price on the sizing session is
    dropped and the survivors renormalised; a name whose target is under one
    lot gets zero lots. Both are listed, never silently vanished. Holdings
    count after rounding is checked against `statement.json` min/max and WARNs
    outside it, as the target does.
  - Deviation is reported per name in bp and once as ½ Σ|actual − target| in
    pp, so it is comparable to `rebalance.drift_threshold`.
  - The ticket is a **view**: computed on demand, written nowhere. No order
    file, no broker format, no per-replica state.

- **D70 position screens.** Replication flags three risks that depend on AUM
  and so are never computed on the Monitor. Thresholds are per portfolio and
  live in **`screens.json`**, beside the name screens, so the existing
  default, validation, route and version-checked save carry them:

  ```json
  {"turnover": {...}, "float_cap": {...}, "fol": {...},
   "ownership": {"on": true, "max_pct_of_shares": 5},
   "float":     {"on": true, "max_pct_of_float": 15},
   "liquidity": {"on": true, "participation_pct": 50, "max_days": 20}}
  ```

  - `ownership`  `shares_i / outstanding_shares_i` above 5% — the major
    shareholder disclosure threshold
  - `float`      `shares_i / free_float_i` above 15%
  - `liquidity`  `value_i / (participation × adv_21_i)` above 20 sessions
    (`adv_21` is VND a session)

  `common.SCREENS` keeps the three name screens; a sibling `POSITION_SCREENS`
  lists the three new ones (liquidity has two thresholds), and
  `validate_screens` accepts both. The Monitor's screen rules edit only the
  name screens; the Replication section edits only the position screens.
  `portfolio.position_flags(frame, positions, screens)` sits beside
  `screen_flags` and returns the same row shape `{t, screen, value,
  threshold, why}`, so the desk's flag rendering and `flag_text` serve both.
  All three read `params.at(db, sizing_session)`; no new data. Like D62 they
  are **flags only** — they never trim a position, and excluding a name is
  unticking it in the book. `index/fol.csv` is header-only today, so the `fol`
  column reads "no data" throughout until the user populates it.

---

## Target layout

Unchanged. No new file under `portfolio/<name>/`; replication writes nothing.

---

## Desk structure

```
Market data                     unchanged
Portfolios   Setup 1-3, Loop 4-8   Target, Decisions and Monitor gain D67/D68
Replication  portfolio, AUM, cash %, execution date, position thresholds; the ticket
```

The Decisions tab gains derived rows, marked and visually quieter than
recorded ones, with a note that they are recomputed on today's group map, and
an `unattainable` pill on any recorded decision the data cannot fill yet.
Replication needs a portfolio with an inception decision and says so plainly
when there is none, as Monitor does.

---

## Step A — D67, timing and attainability

### A1. `scr/backtest_engine.py`
Move the calendar trigger to the period's last session; rewrite the D64
collision block as the single test above. `place()` and the `run()` timeline
report `unattainable` for a decision whose session is the last one or whose
effective date is after it. `monitor()` reports the **last attainable**
decision and the book in force, and lists any unattainable ones as flagged
rows, instead of stopping at "too recent" / "not in force yet". Pin
`lag_sessions` to 1 for `monitor()` with a WARN otherwise; `next_calendar()`
keeps returning the trade date and gains the reference date beside it. Update
the module docstring, which is the spec.

### A2. `scr/common.py`
`validate_backtest_config` WARNs on a `lag_sessions` other than 1.

### A3. `scr/app.py`, `scr/static/desk.js`
Remove the "Fill lag, sessions" field from the Backtest settings
(`desk.js:1382`) and its RUNKEYS entry; the rebalance-log label reads "fill at
the next open". Target step: the effective-date picker stops at the last
session, and the last session shows `unattainable` beside Record. Monitor:
the unattainable rows and the reference date beside the next calendar date.

### A4. Baseline first, then tests
Record today's numbers **before** the change; every backtest number moves and
the `plan_v2.md` gate ("`energy_focus` mechanical: 5.99%, 8/2/0 triggers") is
no longer the invariant. Measured on the current data with the start at the
inception decision (by shifting `period_keys`, which approximates the change):

```
                                 current      after D67
finance_advance  replay     +5.8538%      +5.7186%     turnover 0.0835 -> 0.0752
finance_advance  mechanical +6.1988%      +6.1581%     turnover 0.0149 -> 0.0136
energy_focus     both       +4.0422%      +4.0422%     (no boundary in its window)
```

The calendar fill moves from "derived 2026-09-03, traded 2026-09-04" to
"derived 2026-08-28, traded 2026-09-03", and its turnover falls because the
book is measured before it drifts across the break. Re-measure the gate with
the start the gate uses and write the new numbers into `Task.md`.

New tests: the calendar fires on a period's last session and not on the first;
inception on a period's last session is that boundary's rebalance and the
period is not traded again; the last session in the data fires nothing; a
decision effective on the last session, or after it, is `unattainable` and its
predecessor stays in force, in `run()` and in `monitor()`.

---

## Step C — D69, D70, replication (before Step B: it needs only the trace)

### C1. `scr/replicate.py` (new)
```
reference(name, exec_date, db)      -> exec_session, reference session, weights,
                                       target_as_of, unattainable decisions
allocate(weights, prices, equity)   -> lots; pure, no I/O, the testable core
run(name, aum, cash_pct, exec_date) -> the ticket
```
`reference` reads the forward-test trace (`run(..., daily=True)`) at
`exec_session − 1`. Module docstring is the spec, in the shape of
`portfolio.py`'s. `allocate` stays free of I/O so the apportionment invariants
can be tested directly: never overspend, at most one lot above target,
deterministic under ties.

### C2. `scr/common.py`, `scr/portfolio.py`
`snap_fwd(db, d)`; `LOT = 100`; `POSITION_SCREENS` and their defaults in
`default_screens` / `validate_screens` (a `screens.json` without them reads
the defaults, so nothing migrates). `portfolio.position_flags` beside
`screen_flags`; `flag_text` learns the three units.

### C3. `scr/app.py`, desk
`POST /api/p/<name>/replicate {aum, cash_pct, exec_date}`; the position
thresholds save through the existing `screens` route. A third top-level
section with the inputs, the thresholds, the ticket table (target weight,
lots, shares, sizing close, value, actual weight, deviation bp, execution
open) and both flag blocks, red on breach and amber on "no data".

### C4. Tests
`tests/test_replicate.py` on the shared `make_db` fixture: apportionment
invariants; cash never dips below the requested floor at the sizing close; a
name with no price is dropped and the survivors renormalise; a sub-lot name
gets zero lots and is listed; each position screen fires at its threshold and
not below it; an execution date past the last session is `unattainable` and
builds no ticket; the reference is the trace's standing target at
`exec_session − 1`, including a decision effective between the decision date
and the execution date; an unattainable decision is named and not used.

---

## Step B — D68, derived decisions

### B1. `scr/backtest_engine.py`
`timeline(name)` returning recorded plus derived entries with `source`, built
from one `run(timeline=load_decisions(home), daily=True)`. A derived entry
carries `id` (its reference session), `effective`, `kind: "period"`, `source:
"derived"`, `priced_as_of`, the weights and the `setup_hash` of the recorded
decision it derives from. Recorded entries carry their attainability status.

### B2. `scr/app.py`, `scr/static/desk.js`
`GET /api/p/<name>/decisions` returns the timeline; the tab marks derived rows
and carries the today's-map note. A recorded entry also carries `also`
(`"calendar"` when it took a period boundary's place, from the run's
`rebalances`). The pill is built from `source` and `also`, never from `kind`
alone, since a derived row and a recorded period decision share `kind:
"period"`:

```
derived                         calendar · auto       quiet row, today's-map note
recorded, also = calendar       <kind> + calendar     e.g. "active + calendar"
recorded                        <kind>                inception | period | active
```

### B3. Tests
A derived entry appears between two recorded ones in session order; recording
on its date replaces it and the recorded entry carries `also = "calendar"`; `reset` leaves derived entries regenerating from the
inception that remains; nothing new is written under `portfolio/<name>/`.

---

## Checks that gate each step

- Step A: the numbers above reproduce (or the difference from the
  `period_keys` shift is explained), and the `finance_advance` calendar fill
  is derived 2026-08-28 / traded 2026-09-03. A scratch decision effective on
  the last session shows `unattainable` on Target, Decisions and Monitor.
- Step C: a 100bn / 5% cash ticket on `finance_advance` executed on the last
  session spends at most 95bn at the sizing close, leaves cash at or above 5%
  there, reports the spend at the open, and every holding is a multiple of
  100 shares.
- Step B: `portfolio/` is byte-identical before and after a Decisions tab load
  (nothing written); the timeline on `finance_advance` is inception
  2026-08-01, decision 2026-08-15, derived 2026-08-28.
- Desk walk-through on a scratch copy, as in v2.

## Not in scope

Replicating onto an existing book (delta tickets, sell tax, two-sided orders):
the replica starts from cash. Order slicing, ATO mechanics, price bands, tick
sizes, max order size, broker upload formats. Tracking the replica after the
execution date — no positions file, no mark-to-market, no rebalance tickets.
Anything past the last session: no provisional derivations, no assumed next
session. Any foreign-room check beyond the existing "no data" flag until
`index/fol.csv` has rows.
