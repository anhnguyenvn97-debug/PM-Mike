# Progress

Log of the ground-up rebuild. Newest entry first. Pair with `Task.md` (what is
left) and `HANDOFF.md` (the pre-rebuild system, now reference only).

---

## 2026-09-22 — Edit group map button

- Market data, Universe panel: "Edit group map" (POST /api/open/group_map)
  opens `index/group_map_live.csv` in the app Windows uses for .csv. The desk
  still never writes the file; a refresh picks up the saved edit, and a new
  group enters every book rated NO. Test added (145 pass).

## 2026-09-22 — D66: Monitor is the forward test from inception

- `simulate()` takes an optional trace list (one state per session at its
  close); `run(daily=True)` returns it as `daily` (`backtest_engine.day`).
  The backtest does not ask for it.
- `monitor()` returns the whole replay from inception: `series` (portfolio,
  benchmark), every fill, `total` since inception, `daily`, `decisions`.
- Desk Monitor: "Forward test from inception" chart (growth of 100, excess,
  fill markers, pending sessions shaded). Hover updates group drift, breach,
  since inception, the standing target line and the holdings table for that
  session; leaving the chart returns to the latest. The chart code is shared
  with the Backtest (`growthCharts`, `chartHover`).
- finance_advance: 28 sessions from 07-31; since inception +4.62%; on 08-14
  drift 5.87% with the decision fill pending to the 08-17 open, 0.18% after.
- Monitor test extended: fills, series and total equal the replay; the
  pending day, its drift equal to the fill's group drift, the last day equal
  to "now" (144 pass).

## 2026-09-22 — D65: Monitor carries the held book

- `monitor()` ran only the last decision, from cash, so a period or active
  decision showed 100% turnover. It now replays every decision from the
  inception's session and reports from the last decision's session: fills
  since, the return since its close, drift, breach, next calendar date, flags.
- finance_advance (inception 08-01, period 08-15): the 08-15 fill is 6.1%
  turnover (was 100%), since the decision +4.05% (was +4.01%); equal to the
  backtest started on 08-01. Test updated to check both against the replay.
- Labels: `monitor()` passes `also`; the Monitor fill table shows "decision
  + calendar" (and no longer renders a `decision` row as "calendar"), and the
  Backtest log shows "inception + calendar" when the inception covers the
  period's calendar rebalance.

## 2026-09-22 — D64: a decision trades as recorded

- `backtest_engine.place()` puts a decision on the last session on or before
  its effective date (was: the first on or after). A weekend decision is
  decided on Friday's close, the session Record priced it on, and fills at
  Monday's open with the recorded book. Weekday dates are unchanged.
- The first decision after the backtest start opens the window (illustration)
  and also fires as a decision fill on its own session; the timeline reports
  that session as placed and applied, and `n_decision` counts it.
- `simulate()`: a decision filling at the open of a calendar boundary, or in
  flight across it, is marked `also = calendar` and the period's calendar
  rebalance is not traded again.
- `monitor()` starts at the same session; status reads "priced X, filled Y,
  held to Z".
- The backtest start date follows the same rule (was: first session on or
  after), in `run()` and the desk's start hint (`btSnap`), so a start on a
  weekend inception date opens on the Friday it was priced on.
- finance_advance (inception Sat 2026-08-01): Backtest now shows decision
  07-31 -> fill 08-03 (also calendar), n_decision 1; Monitor fills at 08-03's
  open with 100% turnover; a backtest started 08-01 opens 07-31 and matches
  the Monitor (+4.64%). Tests: 4 updated, 3 added (144 pass).

## 2026-09-21 — v2: date-driven portfolio, setup / loop desk (D57-D63; Step 17 A-D)

Spec: `docs/plan_v2.md`. Built in one pass, gated on the Step 16 numbers.

### Decisions landed
- **D57** `params.at(db, session)` on demand; `common.sessions/snap`. Retired
  `baseline.py`, fork, `input/`, `index/anchor_date.json`, `newer_than`,
  `portfolio/baseline/`. A name with no float cap on a session is left out of
  that session's params (`attrs["no_float_cap"]`) instead of failing.
- **D58** `book.json` spec replaces the grid book and `tactical_group.*`.
  `write_book` checks the group map: a ticker the map puts in another group,
  an unknown group, a tactical name clashing with a group. A ticker the map
  lacks may stay (dropped at evaluation).
- **D59** `portfolio.evaluate` (was `reconcile_profile`) for the build, the
  preview and the replay; auto-NO applied after it with a WARN.
- **D60** `target.compute/build(as_of=)`; `priced as of` when the date is not
  a session. Kinds: first decision inception (forced), later period/active,
  none before inception; `portfolio.reset` archives decisions/.
- **D61** decisions store `setup_hash` (statement + constraints, validated);
  Decisions and the backtest timeline show "setup differs". Replay unchanged:
  inception holds before its date, each decision the standing target after.
- **D62** screens are flags: `screens.json` (turnover, float cap, new `fol`
  on `index/fol.csv`, "no data" when a name has no row). `invalid.csv`,
  `exclusions.csv`, `--invalidate`, `restore` and the invalidation rule gone.
- **D63** `backtest_engine.monitor`: last decision held to the latest session;
  drift, breach, next calendar date (weekday estimate), fills, flags.

### Desk
- 8 steps: Setup 1 Statement, 2 Rebalancing, 3 Constraints; Loop 4 Allocation
  and 5 Target (one page, two step-bar entries), 6 Backtest (moved in from its
  own page), 7 Monitor (with the screen rules editor), 8 Decisions (Reset).
- Target: effective date, kind (inception pill or Period/Active), note,
  "Save and record ..." when edits are unsaved; the preview re-prices and the
  Allocation universe switches to the date's session. Chips show screen flags
  (!), "new" (not in the last decision's universe), and struck-through names
  not trading on the date (kept in the book). Lost groups can be removed.
- Data tab: the universe of the latest session replaces the params/baseline
  panel; no build button.

### Migration
- One-shot `scr/migrate_v2.py` (deleted after use): grids -> `book.json`,
  statement screens -> `screens.json`, `2026-09-11-01` -> `2026-09-11.json`
  (inception, priced_as_of = the v1 anchor), retired files -> `_v1/`.
  Dry-run and apply on a scratch copy, then on live `portfolio/`.

### Checks
- Gate, live after migration: `energy_focus` mechanical and replay backtest
  identical to v1 (max |dnav| 0, rebalance log equal; 5.99%, 8 calendar /
  2 breach / 0 drift). Target weights within 2.4e-8 of v1 (v1 read baseline
  weights rounded to 8 decimals). No name dropped by D59. `financial_test`
  fails as before (every group NO in its live book).
- pytest 142 passed (tests rebuilt on a shared `market.db` fixture; new
  params, kind, reset, flags, monitor, next-calendar tests); ruff clean;
  `node --check` clean.
- Desk walk-through on a migrated scratch copy: every step renders without a
  JS error; Reset, a back-dated inception (Saturday -> Friday session), the
  before-inception guard, "Save and record active rebalance", screen rules
  save, Monitor, Backtest replay of both decisions.

---

## 2026-09-21 — Decision review, one profile per date (D56; Step 16 D)

User direction after Step C: the smallest decision frequency is one a day, so a
date holds at most one profile; recording on a date that has one replaces it.
Record belongs with the Target step (you record what you just saw); reviewing
the log is an optional Step 7, read-only. No guided 4 -> 6 loop: the steps stay
a normal flow.

### Changes
- `portfolio.record_decision`: id is the effective date; any row and JSON on
  that date are removed before the new one is written; log.csv kept sorted by
  effective. Prints "replaced" or "recorded". Older `<date>-NN` ids still load
  and are replaced by date.
- `common.py` / `portfolio.py` / `app.py` docstrings: not append-only any more.
  `decisions_json` adds each profile's `groups` and `tactical`.
- Engine unchanged: two dates snapping to one session still resolve last-wins
  with "superseded by" in the timeline status.
- `desk.js`: Book loses the Decision log panel. Target ends with a Record
  decision panel (date and note kept across re-renders; the button reads
  "Replace decision for <date>" when that date exists). New optional step 7
  Decisions: the log (newest first, click to select, latest selected by
  default) and the selected profile as recorded: rated groups with rating,
  active pp and investable names, tactical groups, the NO groups in one line,
  and the replay drops against today's grid. No Next button after Target.
- `desk.css`: `.steps` grid 7 columns; `.dec-list` row hover and selection.
- CLAUDE.md: decisions/ wording.

### Checks
- pytest 125 passed (decision tests updated for date ids and replace); ruff
  clean; `node --check` clean.
- Desk on a scratch copy of `portfolio/`: step 7 lists both decisions, row
  click switches the profile; Target's button turned to "Replace decision for
  2026-09-11" and recording replaced the live-format `2026-09-11-01` entry.

---

## 2026-09-21 — Decision log and replay (D53, D54, D55; Step 16 C)

Spec: `docs/plan_decision_log.md` Step C. A portfolio now keeps an append-only
log of allocation profiles, and the backtest can replay it.

**D53 decision log.** `portfolio/<name>/decisions/log.csv` plus `<id>.json`,
where `id = <effective>-<NN>`. A profile is the desk's book spec verbatim (the
book and tactical overlay only) plus metadata. The Book step's Record decision
button records the book as saved on disk. It is refused while the page has
unsaved book edits, when the page's book version is stale, and when the saved
book fails the strict target build. There is no delete route; remove the log
row and the JSON by hand. The engine replays the profiles as `decision`
triggers, which sit above calendar in the trigger ladder. `timeline=None` is
the mechanical run, exactly as before.

**D54.** A profile is carried onto today's baseline grid by name
(`portfolio.reconcile_profile`), filtered through today's
`screen/invalid.csv`, and loaded non-strict. Invalidated-since names, names gone
from their column and lost groups are each a WARN naming the decision, and
they appear in `built_from.txt` and in the desk's Decision timeline. A lost
group's pp break the net; `tilt` floors and renormalises.

**D55.** A decision never moves the calendar: `period_keys` is unchanged.

Placing profiles: an effective date snaps forward to a session. Every profile
at or before the start collapses to the start, and the latest wins; two on one
session, the last wins; one after the history is reported and not applied. If
none lands on the start, the first one opens the window, with an INFO message.
A decision that lands while a fill is pending is applied at the first free
close (`deferred_from`). A decision on a calendar boundary is one `decision`
fill with `also = calendar`.

`RATINGS` moved from `target.py` to `common.py`, where `validate_decision`
needs it; `target.RATINGS` still resolves. That is the only change to
`target.py`.

### Changes

| File | What |
|---|---|
| `scr/common.py` | `DECISIONS`, `DECISION_LOG`, `DECISION_COLS`, `RATINGS`; `validate_decision`, `load_decisions`; decisions grammar in the docstring. |
| `scr/portfolio.py` | Moved from `app.py`: `pp_cell`, `cell_pp`, `tactical_grid`, `book_grid`, `grids_from_spec`. New: `book_spec` (the saved files as a spec), `reconcile_profile`, `record_decision`; docstring. |
| `scr/backtest_engine.py` | `load_book(book=, tactical=, strict=)` (+ `faults`); `load_profile`; `place`; `run(timeline=)` over the union of every book's names; `simulate` takes regimes (per-book target, groups, breach, edge) with the decision trigger, `also`, `deferred_from`; `breach_check(names=)` and `edge_fill` handle names outside the book. Outputs: `rebalances.csv` + `also`, `profile`, `deferred_from`; `summary.csv` + `n_decision`; `built_from.txt` `decisions:` block; `run()` returns `timeline`. CLI `--mechanical`; `build` replays the log by default. Docstring. |
| `scr/app.py` | `GET/POST /api/p/<name>/decisions`; `detail()` + `decisions`, `head_matches_book` (via `pf.book_spec`); `summary()` + `decisions` count; backtest body `mechanical`; `backtest_json` + profile/also/deferred_from/timeline; docstring. |
| `scr/static/desk.js`, `desk.css` | Book step Decision log panel: date, note, Record decision (disabled while dirty), match badge, log with each profile's report against today's grid. Backtest: Replay toggle (on by default, shown when the log has entries), `mk-decision` marker and legend, `decision` pill, Profile column, Decision timeline table, D54 caveat. |
| `tests/` | Engine: no timeline equals the live book; a decision is a trigger and sets the standing target; decisions before the window collapse; the first decision opens the window; a decision on a calendar boundary is one fill; a decision during a pending fill is deferred; one after the history is reported; invalidated-since names are dropped (D54); a lost group warns and runs. Portfolio: `reconcile_profile`, `load_decisions` ordering and validation. App: record/list round trip, dirty and stale refusals, `head_matches_book` flips, backtest replay vs `mechanical`. |

### Checks

- pytest 125 passed; ruff and `node --check` clean.
- `energy_focus` mechanical: unchanged from Step B (5.99%). On a scratch copy of
  `portfolio/`, recording the live book as a decision and replaying it
  reproduces the mechanical equity curve exactly (max abs diff 0). The live
  `portfolio/energy_focus/` has no `decisions/` folder.
- Desk on 5057 over the scratch copy: the Book step shows the Decision log with
  the "book matches the last decision" badge; the Backtest runs with Replay on
  (Decision timeline, Profile column, +5.99%) and off (no timeline).

---

## 2026-09-21 — Fill at the open (D52; Step 16 B)

Spec: `docs/plan_decision_log.md` Step B. Until now a decision at close t filled
at the close of t + lag, so a lag 1 fill reset the weights at t+1's close and
erased whatever t+1's own return had done to them.

**D52.** A decision at close t fills at the OPEN of t + lag_sessions; lag 0
keeps the same-close fill. Each session is two legs: the held weights earn
close -> open (`open_adj` / previous `close_adj`), the fill trades at the open,
and the new weights earn open -> close. With no fill the legs compound to the
close-to-close return. At lag 1 the fill is done before the next close, so every
close is checked. At lag 2 or more the sessions in between stay unchecked while
the trade is in flight; the docstring and the desk caveat say so. A name with no
valid `open_adj` on a session uses its `close_adj` for both legs, and one INFO
message is written if that hits a fill session. `market.db` has no null or
non-positive `open_adj` in 16,900 rows today.

### Changes

| File | What |
|---|---|
| `scr/backtest_engine.py` | `load_market` returns `open`; `run()` builds `R_pre` / `R_post` (an invalid open falls back to the close) plus the INFO message; `simulate(..., R_post=)` runs leg 1, the fill, then leg 2; docstring (decision/fill, returns); `built_from.txt` `config:` line. |
| `scr/common.py` | `lag_sessions` grammar: fill at the OPEN, 0 = same close. |
| `scr/static/desk.js` | Rebalance log header "fill at the open N session(s) later" / "fill at the same close"; lag field hint; caveat 4 rewritten. |
| `tests/test_backtest_engine.py` | `make_db(opens=)` writes `open_adj` (default = close). New: fill at the open splits the session (hand-computed nav and turnover), an open gap without a fill compounds to the close-only curve, and a breach born on the fill session is decided at that close. |

### Checks

- pytest 112 passed; ruff and `node --check` clean. Every Step A number held
  unchanged with open = close.
- `energy_focus` (in memory, nothing written), after A -> after B: triggers
  8 / 2 / 0 both; turnover 33.37% -> 33.59%; costs 20.01 -> 20.08 bps; total
  return 3.61% -> 5.99%; excess +3.23% -> +5.61%. The inception day explains
  almost all of the jump in total return: buying at the 01-06 open rather than
  its close captures that session's +2.1% open-to-close. Measured from the 01-06
  close the return is 3.80%, so fills at the open add about 0.2 pp over the
  window.

---

## 2026-09-21 — Standing target and breach-to-edge (D50, D51; Step 16 A)

Spec: `docs/plan_decision_log.md` Step A. Until now the engine re-derived the
target from that session's float cap on every trigger check, and measured drift
against the re-derived target (D42). The target moved with the market between
calendar dates, so a breach fill traded the whole book to a fresh target.

**D50 standing target.** The target is derived at inception and on each calendar
boundary and held between them. Drift is measured against it. With
`frequency: null` it is derived once. This supersedes D42 and the "re-derived"
clause of D43.

**D51 per-trigger policy.** Inception, calendar and drift fills are `full`: they
trade the whole book to the standing target. A breach fill is `edge`:
`target.apply_constraints` runs on the held weights (group weights = held group
sums, pro-rata key = held stock weights). That clips every broken cap to its
limit and spills the excess within the cap's scope (D18), and leaves the rest of
the book alone. If the clip is infeasible it falls back to `full` with a WARN,
and breach checks pause until the next calendar date. Without the pause, the
fallback target, which breaks the cap too, was re-filled every session at zero
turnover. The first test run caught this.

Dead names: a name held or in the standing target with no float cap on the
decision session is zeroed in the traded-to vector and in the standing target,
and the rest renormalise. The `gone` column is renamed `dropped` and lists
dropped groups and dead names; a name whose group dropped is listed by its
group. This is judged on the decision session, not the fill session as the spec
said: every other input to a decision is read at the decision close.

### Changes

| File | What |
|---|---|
| `scr/backtest_engine.py` | `simulate` holds `standing` / `standing_at`, calls `target_fn` on inception and calendar only, drift vs standing, `alive` matrix for dead names, `stuck` flag after an infeasible edge; returns messages. New `edge_fill`. `run()` drops the one-entry cache. `rebalances.csv` + `policy`, `target_as_of`, `gone` -> `dropped`; `holdings_end.csv` `target` = standing target; `built_from.txt` mandate line; docstring. |
| `scr/common.py` | Rebalance grammar: drift vs the standing target; a breach fill clips to the cap. |
| `scr/app.py` | `backtest_json` passes `policy`, `target_as_of`, `dropped`. |
| `scr/static/desk.js` | Rebalance log gets Policy and Target as of columns; breach pill title reads the tolerance; Mandate panel drift/breach text; caveat 3 rewritten; Holdings-at-end label names the standing target. |
| `tests/test_backtest_engine.py` | Drift test inverted (`..._against_the_standing_target`, now fires); breach +30% asserts the edge weights (AAA 42%, BBB takes the excess, G2/G3 untouched); drift test asserts `full`; new: breach between calendars keeps the old standing target, edge falls back to full when infeasible (one fill, no churn), exactly three `targets_at` calls over three months. |

### Checks

- pytest 109 passed; ruff and `node --check` clean.
- `energy_focus` before and after, computed with `run()` (nothing written; the
  live `backtest_engine/` outputs are untouched):

  | | before | after |
  |---|---|---|
  | calendar / breach / drift | 8 / 2 / 1 | 8 / 2 / 0 |
  | turnover after inception | 36.34% | 33.37% |
  | costs incl. inception | 20.90 bps | 20.01 bps |
  | total return | 4.00% | 3.61% |
  | excess vs VNINDEX | +3.62% | +3.23% |

  Turnover fell, not rose. The two breaches now fill `edge` (4.1% and 3.8% of
  turnover, against 5.1% and 3.7% before). The drift fill of 2026-07-27 is gone:
  against the standing target of 07-01 the book had drifted less than 6%, but
  against that day's re-derived target it had drifted 6.4%. Most of the drift
  showed up at the 08-03 calendar fill instead (4.9% turnover against 2.2%).

---

## 2026-09-16 — Breach tolerance is a statement dial (D49)

Review of the three rebalance triggers. Calendar and drift are both set in the
Statement step; breach was not set anywhere. Which caps trip it came from
`constraints.json` (Constraints step, already adjustable), but how far a cap may
be broken before a trade is forced was `BREACH_TOL = 0.10`, a constant in
`backtest_engine.py` — invisible from the desk and the same for every portfolio.

**D49.** `statement.json` `rebalance.breach_tolerance`: a fraction of the cap, or
`null` to switch breach off even with caps on. A missing key means 0.10, so every
existing portfolio keeps the behaviour it has today without a file edit. Caps
themselves stay owned by the Constraints step; the Statement step only gets the
slack dial, beside drift.

### Changes

| File | What |
|---|---|
| `scr/common.py` | `BREACH_TOL` moved here; `breach_tolerance` in `default_statement()` and validated in `validate_statement()` (fraction in (0, 1) or null); statement docstring. |
| `scr/backtest_engine.py` | `breach_check(book, groups, tol)` returns `None` when `tol` is `None`; `run()` reads the key with a 0.10 default; `built_from.txt` and the console print the breach setting; docstring. |
| `scr/static/desk.js` | Breach field in `statementForm` / `readStatementForm` / `bindStatementForm` / `DEFAULT_STATEMENT`; the Backtest tab's Mandate panel reads the tolerance instead of naming 10%. |
| `tests/` | `test_breach_tolerance_is_set_in_the_statement` (null, 30%, 0.5%); a validation case. |

### Checks

- pytest 106 passed; ruff and `node --check` clean.
- `energy_focus`, no key on disk: 8 calendar, 6 breach, 0 drift — unchanged, as
  the user observed. On a scratch copy of the folder: `null` -> 0 breach,
  turnover 28.30%; `0.25` -> 0 breach; `0.10` -> 6 breach, 38.37%; `0.02` -> 19
  breach, 52.33%, cost 25.7 bps.
- Desk on 5055: the field renders checked at 10 for a portfolio with no key, the
  toggle disables the input, the Mandate panel reads "broken by more than 10% of
  its limit". Nothing saved.

---

## 2026-09-16 — Group coverage in the Book step (D48)

Review question: PVT is the largest holding in energy_focus although it is the
smallest live float cap. Cause: the neutral is the group's float-cap weight in
the benchmark, and PVT was the only Logistics name kept, so it carried the
whole 30.95% (Logistics is 45.01 tn, the largest of the five live groups; PVT
is 12.7% of it). Working as designed — deleting a name is stock selection and
the group keeps its budget.

**D48.** Keep the benchmark neutral; a neutral computed from the names kept
would be self-referential, since deleting a name would rewrite the anchor it
is measured against and fuse allocation with selection. Instead the Book step
shows, per group, `cover <kept float cap / group float cap> · <kept> of <n>
names · <largest survivor> <its weight> of the book`, amber below one third.
Display only: no weight, no constraint, no Python change.

### Changes

| File | What |
|---|---|
| `scr/static/desk.js` | `coverage()` + `COVER_THIN`; the cover line under each non-NO group. A name claimed by a tactical group leaves both sides of the ratio. |
| `scr/static/desk.css` | `.gmeta.cover`, `.gmeta.cover.thin`. |

### Checks

- `node --check` ok; pytest 102 passed; browser on port 5055, energy_focus:
  Logistics `cover 23% · 2 of 4 names · PVT 16.0%` (amber), Construction
  Contractors `cover 28% · 2 of 6` (amber), Industrial `cover 80% · 1 of 2`.
  Nothing saved.

---

## 2026-09-16 — Rating control: NO button + UW3-OW3 spectrum (D47)

**D47, the user's design.** The rating stops being four side buttons plus a
tier row. It is now a red on/off **NO** button (`#E53935`, the only red on the
page) and, while NO is off, a seven-cell spectrum `UW3 UW2 UW1 AV OW1 OW2 OW3`.
Clicking a cell sets the rating and lights every cell between AV and it in the
user's palette; the rest stay grey. AV is lit whenever NO is off. While NO is
on, the spectrum and the pp input are disabled and the group is out of scope.

Palette (`SPEC` in `desk.js`): `#003366 #0055CC #66B2FF #FFF176 #C8E6C9
#66BB6A #2E7D32`. UW is the blue half, OW the green half, AV yellow.

No UW/OW end labels under the bar (dropped at the user's request): the caption
beside the pp box carries the state instead, e.g. `OW2 · range 0.00 to +6.00 pp`.

### Changes

| File | What |
|---|---|
| `scr/static/desk.js` | `SPEC`, `inBand`; `rateControl` rewritten (NO toggle, spectrum, pp input below); `[data-side]`/`[data-tier]` handlers replaced by `[data-no]`/`[data-rate]`; Book legend shows the strip. Tactical groups get the spectrum without a NO button. |
| `scr/static/desk.css` | `.nobtn`, `.spec`, `.specend`, `.specleg`; `.rate` rules removed; group side column 200 -> 236px. |

No Python changed: the grammar on disk is still `NO|UW3..OW3` plus pp, and
`target.py` checks range, floor, net and budget exactly as before.

### Checks

- `node --check` ok; pytest 102 passed.
- Browser, port 5055, high_growth: OW2 lights AV/OW1/OW2 with the selected
  cell ringed; clicking UW3 clamped +5.71 pp to 0 and turned the net red;
  NO greyed the bar, hid the pp box and showed "out of scope"; NO off
  returned AV with only AV lit; 20 bars, 20 NO buttons, no console errors;
  Discard restored the book. Nothing saved.

---

## 2026-09-16 — Screens read the universe, not the book (D46)

The user found that step 3 suggests nothing once a book is curated: `screen`
walked the book grid, so a name deleted in the Book step could never be
flagged again. **D46:** screens evaluate every name of the anchor universe
(`input/sector_constituents.csv`) except those already invalidated, and each
row of `exclusions.csv` carries `in_book` (yes/no). Invalidating a name that
is already out of the book is legal and means "never let it back in".

Nothing else changed: exclusions still only suggest, `invalid.csv` keeps its
old columns, and the list stays private to the portfolio that produced it.

### Changes

| File | What |
|---|---|
| `scr/portfolio.py` | `screen()` builds candidates from `input/` + the book, skips invalidated names, adds `in_book`; counts and per-row "(not in the book)" in the log; `EXCL` gains a column, `INVALID_COLS` unchanged. |
| `scr/static/desk.js`, `desk.css` | Step 3: In-book column (`yes` / `deleted`), dimmed rows for names already out, header count "N in the book, M already out", Select-all restricted to in-book hits, new empty-state wording. |
| `tests/test_portfolio.py` | `test_screen_covers_the_universe_not_just_the_book`; `in_book` asserted in the existing screen test. 101 -> 102. |
| `CLAUDE.md` | Pipeline line names the new scope. |

### Checks

- pytest 102 passed; ruff clean; `node --check desk.js` ok.
- Live: `energy_focus` 100 screened / 92 investable, 3 suggested (8 earlier
  invalidations no longer re-suggested); `financial_test` 100 / 100, 68.
- Scratch copy of high_growth at 0.5%/day: 36 suggested, 19 in the book,
  17 already out — the 17 the old code could not see.

---

## 2026-09-16 — Step 11 done: active-weight tilt, triggers, legacy retired

Built D31-D43. Two further decisions with the user:

- **D44 breach tolerance.** A cap triggers a rebalance only when broken by
  more than 10% of its limit (stock max 10% -> above 11%; the large set uses
  1.1 x threshold and 1.1 x aggregate). Names pinned at a cap do not trade on
  the first uptick.
- **D45 book conversion.** Existing books converted to reproduce the old
  weights: pp = 100 (old weight - in-scope neutral), clipped to +-9, tier =
  smallest covering. AV groups picked up small pp; re-rate by hand.

Design choices made in the build:

- Past dates in the backtest: a group whose neutral is below its underweight
  is held at 0% and listed in the messages (not a FAIL; the anchor book
  already passed the strict checks).
- Baseline grid rows 0-1 stay `AV` placeholders: input/ is compared byte for
  byte, so a new placeholder would flag every portfolio for a re-fork.
- The sticky anchor is now resolved from `index/anchor_date.json` against
  market.db (`baseline.sticky_anchor(db)`); no root copy.

### Changes

| File | What |
|---|---|
| `scr/target.py` | `RATINGS`, `TIERS`, `tier_range`, `tilt`; `parse_ratings` reads pp (multiplier books FAIL with a hint); `compute(strict=)` with `active` (net, used, budget, faults, range); outputs `active_pp`, `neutral_weight`, `active_vs_neutral_pp`, `active_vs_baseline_pp`; legacy `apply_caps`, `read_tactical`, sector_cap WARN and sticky INFO removed. |
| `scr/common.py` | `constraints.json` `active.budget_pp`; `CAPS`; `CAP` removed. |
| `scr/portfolio.py`, `scr/baseline.py` | pp in carry / auto-NO; fork needs an anchor or a db; sticky root copy removed. |
| `scr/backtest_engine.py` | `targets_at` via `target.tilt`; `breach_check`; every trigger re-derives the target; `n_breach`. |
| `scr/app.py`, `scr/static/desk.*` | pp book spec, non-strict preview, rating side + tier + bounded pp, meters, Balance to zero, budget field, Target columns, breach in Backtest. |
| Removed | `scr/backtest.py`, `scr/load_history.py`, `data/local_history.txt` (+ local .db), `portfolio/baseline/*.csv` and `backtest_rebalance.*`, `hsc_strat_high_growth/backtest/`, `backtest_rebalance.*`, `sector_cap.json` (git rm, staged). |
| Docs | `CLAUDE.md`, `Task.md`, `.gitignore`, pipelining skill. |

### Checks

- pytest 101 passed; ruff clean; `node --check desk.js` ok.
- Live targets: all three build; active 9.72 / 1.79 / 9.45 of 20 pp.
- Live backtests vs VNINDEX from 2026-01-05: high_growth -2.93% (2 calendar,
  0 breach, 0 drift; was -1.89% with 1 drift trade on the old book and
  engine), financial_test -2.75% (8 calendar, 1 breach), energy_focus
  +26.39% (8 calendar, 1 breach, 2 drift).
- Desk on port 5055: pp clamps to the tier, net blocks Save, Balance to zero
  and Undo, side switch clamps pp, Target blocked on faults and clear after
  discard, breach pill and key in Backtest, no console errors. Nothing saved.

---

## 2026-09-16 — Review: tilt, ratings and rebalancing redesign

Decisions from the user's review of the tilt, rating and rebalancing
mechanism. Nothing built yet; no code changed.

### Findings that led here

- AV is not neutral against the full baseline: cutting NO groups and
  renormalising inflates every rated group (Banks - Private AV 31.13% ->
  46.01%; Banks - State UW still ×1.11). Multipliers also size a bet by group
  size.
- The drift trigger measured against the last rebalance's target, not today's
  (`backtest_engine.simulate`, drift branch). hsc_strat_high_growth
  2026-02-04: 5.51% stale vs 1.52% re-derived; it traded 4.9% turnover it
  should not have.
- No check on hard limits between rebalances; calendar resets turn over far
  more than group drift (Apr 6.8% turnover at ~1-2% re-derived drift).

### Decisions

- **D31 neutral.** Cut NO groups, rescale `free_float × close_raw` over the
  rest. NO means out of scope only; a negative view is UW.
- **D32 active weight in pp.** Views are active weight in pp per group,
  entered by hand. Replaces the multipliers.
- **D33 net zero.** Σ active = 0, funded by hand. FAIL otherwise.
- **D34 rating range.** Three strength levels per side, each a pp limit:
  OW 0..+3 / +6 / +9 pp, AV = 0, UW −3 / −6 / −9..0 pp. FAIL otherwise.
  Rating labels in the book are settled at build time.
- **D35 active budget.** ½Σ|active| ≤ B pp for the whole portfolio; B
  defaults to 20 pp, set per portfolio in `constraints.json`. FAIL otherwise.
- **D36 floor.** active ≥ −neutral per group (a group may go to 0%, never
  below). FAIL, no auto-fix.
- **D37 where the rules live.** Checks in `target.py`. The desk bounds each
  input to its range, shows net and budget meters, blocks Save on a breach,
  and offers "Balance to zero" (scales the larger side down; accept or undo).
- **D38 reporting.** Active pp vs the neutral and vs VNINDEX, after
  constraints.
- **D39 within a group.** Float-cap pro-rata stays; stock max and UCITS run
  after the tilt.
- **D40 tail positions.** Not fixed. Re-check once the pp target is built
  (a binding stock max already lifts tails in concentrated groups).
- **D41 tactical trades.** Out of scope. The existing tactical group is
  unchanged.
- **D42 drift trigger re-derives the target.** Each drift check calls
  `target_fn(i)`, measures drift against it and trades to it; also carries
  `gone` on drift trades. Supersedes "restores the current target" in D26.
  Applies to the later rebalance stage too.
- **D43 breach trigger.** Rebalance to the re-derived target when a name
  breaks the stock max or the large-holding limits between rebalances.

### Deferred

- Minimum trade size (skip per-name trades under ~0.25 pp).
- Calendar date as a review: trade only if drift exceeds a smaller band.
- Trade to the band edge instead of exact target.

### Open inputs for the user

- Converting existing books (`hsc_strat_high_growth`, `financial_test`,
  `energy_focus`) from multipliers to pp: computed equivalents for review, or
  re-entered by hand.

---

## 2026-09-15 — Commit: benchmarks, backtest engine, Backtest tab

Branch `docs/handoff`, on top of `ae463f7`. User chose ONE commit with
everything below, including the portfolio files and the `backtest_engine/`
outputs.

**Benchmark index drops into market.db** (Step 9, D24-D25)

- `scr/ingest.py`, `tests/conftest.py`, `tests/test_ingest.py`
- `scr/app.py`, `scr/static/desk.js` Data tab parts (Kind column, Benchmarks
  table)
- `data/fiinpro/Benchmarks.xlsx`, `data/fiinpro/README.txt` (new)
- `data/market.txt` (regenerated dictionary)

**Backtest engine and the desk Backtest tab** (Step 10, D26-D30)

- `scr/backtest_engine.py`, `tests/test_backtest_engine.py` (new)
- `scr/common.py` (`backtest_config.json` grammar)
- `scr/app.py` (backtest routes), `scr/static/desk.js`, `scr/static/desk.css`
- `tests/test_app.py` (benchmarks state + backtest routes)
- `docs/backtest_mockup.html` (approved mockup, reference)
- `CLAUDE.md`, `Task.md`, `Progress.md`

**Portfolio files and outputs (included):**

| Path | What it is |
|---|---|
| `portfolio/financial_test/statement.json`, `sector_constituents_custom.csv`, `screen/exclusions.csv` | Your portfolio edits (1M + 8% drift mandate, book, screen run) |
| `portfolio/financial_test/backtest_config.json` | Saved from the Backtest tab (not written by me) |
| `portfolio/*/backtest_engine/` | CLI outputs from the live runs; derived, regenerable. Commit like `backtest/`, or add to `.gitignore` |

Checks at this point: pytest 91 passed, ruff clean. `tests/test_app.py` has
CRLF line endings in the appended tests; git normalises them on add.

---

## 2026-09-15 — Step 10 done: Backtest tab in the desk

| File | What |
|---|---|
| `scr/backtest_engine.py` | `run()` also returns `benchmarks`: curve and statistics for every code covering the window, so the page switches benchmark without a re-run. |
| `scr/app.py` | `GET /api/p/<name>/backtest` (config, version, defaults), `POST /api/p/<name>/backtest` (engine run with the page's unsaved config, nothing written), `PUT /api/p/<name>/backtest_config` (validated, version-checked). `backtest_config.json` joins the desk-owned files. |
| `scr/static/desk.js`, `desk.css` | Backtest page from the approved mockup (D30): nav entry and a Backtest button on each portfolio; auto-runs on first open; Run / stale bar / run stamp; benchmark and risk-free rate apply to the last result (Sharpe rescaled by rf); Save / Revert settings. |
| `tests/test_app.py` | 2 tests: run returns all benchmarks and writes nothing, bad benchmark and date fail; config save validates and refuses a stale version. |

Checks: pytest 91 passed, ruff clean. Browser, test desk on port 5055 with
live data: high_growth −1.89% vs VNINDEX +0.38%, excess −2.27 pp (= CLI and
mockup); lag 3 dims results with the stale bar until Run; VN30 and rf 3%
switch instantly. Restart your desk and Ctrl+F5 to pick up the new code.

---

## 2026-09-15 — Step 10: backtest engine built

User approved the mockup and engine logic; risk-free rate default 6%.

### Changes

| File | What |
|---|---|
| `scr/backtest_engine.py` | New. Docstring is the spec (D26-D30). `load_market`, `load_book` (via `target.compute`), `targets_at` (target.py's `migrate` / `apply_constraints` on a session's `free_float × close_raw`), `simulate`, `statistics`, `run` (no writes), `build` + CLI (`--start`, `--benchmark`) -> `portfolio/<name>/backtest_engine/`. |
| `scr/common.py` | `backtest_config.json` grammar: `BT_CONFIG`, defaults (10/10 bps, lag 1, rf 0.06), `validate_backtest_config`, `load_backtest_config`. |
| `tests/test_backtest_engine.py` | 18 tests: anchor weights = target, inception cost and lag, drift trigger and restore, monthly re-derivation, 2W periods, statistics, start snapping and window guards, group with no priced name, infeasible past date, build outputs, run writes nothing, config validation and defaults. |
| `CLAUDE.md`, `Task.md` | Engine and config documented. |

### Checks

- pytest 89 passed; ruff clean. `backtest.py` untouched.
- Live: engine vs prototype NAV within 1e-8 (prototype rounded its data),
  identical triggers, 8 runs; mockup headline figures reproduced exactly.
- CLI: high_growth vs VNINDEX from 2026-01-05 -1.89% vs +0.38%, Sharpe -0.38
  at rf 6%; holdings 17 outside 20-30 WARNed per rebalance. financial_test vs
  VN30 from 2026-04-01 +0.46% vs +4.02%. Unknown benchmark FAILs.

### Next

Backtest tab in the desk app.

---

## 2026-09-15 — Step 10 started: backtest engine spec

### Decisions

- **D26 backtest follows the mandate.** Engine reads the desk's files, not
  the legacy `backtest_rebalance.json` / `sector_cap.json`:
  `statement.json` rebalance (frequency `2W`/`1M`/`1Q` on the first session
  of the period, `2W` counted from the start date; drift threshold at group
  grain, ½Σ|gap|, restores the current target), the book held as of today,
  the tactical overlay when its switch is on, `constraints.json` re-solved
  with `target.apply_constraints` at every scheduled rebalance (infeasible
  on a past date = FAIL), holdings range flagged per rebalance. Budgets
  re-derived from that session's `free_float × close_raw`. Start date picked
  by the user; end = last stock session.
- **D27 screens.** Fixed `screen/invalid.csv` only; no point-in-time re-screen.
- **D28 backtest config.** Costs, lag, fill price and risk-free rate live in
  a separate per-portfolio backtest JSON, edited from the backtest tab; not
  in `statement.json`.
- **D29 chart.** Portfolio vs the chosen benchmark only. Later: a selector to
  overlay other portfolios' backtests for cross-comparison.
- **D30 run button.** Portfolio, start date, costs and lag take effect on
  "Run backtest"; until then a stale bar says results show the last run and a
  stamp names its settings. Benchmark and risk-free rate apply at once (they
  rebase the comparison or change Sharpe, no re-simulation).

### Mockup

`docs/backtest_mockup.html`, built from a prototype in the job tmp folder:
target weights per session from `target.py` functions (anchor parity 1e-8),
simulation in page JS = Python prototype to 2e-16, identical triggers.
Artifact publish blocked by permission; open the file locally.

---

## 2026-09-15 — Step 9 done: benchmarks in market.db

User supplied `data/fiinpro/Benchmarks.xlsx` (FiinPro "Index & Sector"
trading data, banner and footer removed by hand): VNINDEX, VN30, VN100, 170
sessions each, 2026-01-05 -> 2026-09-14.

### Decisions

- **D24 return basis.** The backtest will be total return on `close_adj`
  (dividend credited on the ex-date, reinvested in the same name). FiinPro
  adjusts by ratio (verified: `close_adj / close_raw` is piecewise constant,
  74 of 100 tickers adjusted). Benchmarks are price indexes; the gap (about
  the dividend yield) is labelled, not corrected. Weighting stays
  `free_float × close_raw`.
- **D25 index drops.** Same folder as stock drops, told apart by header;
  one rebuild loads both, all or nothing. Index date inside the stock range
  but not a stock session = FAIL; past the last stock session or a stock
  session a benchmark lacks = WARN. The index export carries 2026-09-14,
  which the stock export had as a blank non-session.

### Changes

| File | What |
|---|---|
| `scr/ingest.py` | `read_drop` returns the kind; `index_prices (trade_date, code, close, volume, value)`; `validate_index` (dup, close <= 0, negatives); overlap check per table; `check_calendar`; `loads.kind`; `market.txt` benchmark schema and coverage. |
| `scr/app.py`, `scr/static/desk.js` | `market()` adds `kind` per load and `benchmarks` (code, sessions, range, stock sessions missing); tolerates a pre-index database. Data tab: Kind column, Benchmarks table, price-return note. Rebuild database button unchanged (calls `ingest.main`). |
| `tests/conftest.py`, `tests/test_ingest.py`, `tests/test_app.py` | Index drop fixture; 8 tests: load beside stock, late/short coverage warns, off-calendar fails, dup and zero close fail, overlap fails, unknown export fails, state lists benchmarks. |
| `CLAUDE.md` | One market input now covers index exports. |

### Checks

- pytest 71 passed; ruff clean.
- Live rebuild: `prices` and `tickers` identical to the previous database
  (EXCEPT both ways, 0 rows). Three WARNs: one session past 2026-09-11 per
  index. `/api/state` shows three benchmarks, 0 missing.
- Side effect: the rebuilt `market.db` is newer than the params, so the Data
  tab reports the built anchors stale until "Build params and baseline" runs
  (content unchanged, no re-fork flagged).
- A desk started before this change still has the old `ingest` module loaded
  and would FAIL on `Benchmarks.xlsx` (database untouched). Restart the desk,
  then Ctrl+F5 for the new `desk.js`.

### Next

Step 10: backtest engine spec and artifact mockup. Open question for the
user: backtest window 2026-01-05 -> 2026-09-11 on current data, or supply a
2025 stock drop plus 2025 index history first.

---

## 2026-09-14 — Housekeeping and a false delete

- `git rm index/group_map_default.csv` (staged, not committed): nothing read it.
- User reported deleting `hsc_strat_soe_dom` with the folder still present. The
  folder was intact (all files listed) and the app log showed no DELETE and no
  browser requests at all: the click was on the mockup, whose Delete only
  removes the card in the page. No code change.
- The desk app's background copy (started by the Claude session) was killed by
  Windows for low memory. Not restarted; the user runs it in their own
  PowerShell window from now on (Task.md open items).
- Open decision logged: permanent delete vs a `_trash/` move, and committing
  the portfolio files before relying on the desk for deletes.

---

## 2026-09-14 — Step 8 done: desk owns the portfolio files

Every setting that changes a portfolio's outcome was already editable from the
page; this step makes the page the safe way to do it (D23: portfolio files are
desk-owned, hand edits are the fallback). Legacy `sector_cap.json` and
`backtest_rebalance.json` stay outside the desk.

### Changes

| File | What |
|---|---|
| `scr/app.py` | `version(home, key)` fingerprints (mtime_ns, size) the files a save rewrites: statement; constraints; book = book + tactical csv + switch. `/api/p/<name>` serves `versions`; each PUT may carry `version` and FAILs with "changed on disk since the page loaded; reload the portfolio" on a mismatch. No version (a script) skips the check. |
| `scr/static/desk.js` | Saves send the version they loaded. `Reload` button in the flow head re-reads the files and keeps unsaved edits (drafts survive, versions refresh). Target step: `Save and build` when Book or Constraints edits are pending; a failed save stops before the build and leaves the edits in the page. |
| `tests/test_app.py` | Conflict on constraints and book: 422, file untouched, reload then save works, no-version save skips the check. |

### Checks

- pytest 63 passed; ruff clean.
- Not exercised in the browser: Save and build, Reload after a conflict.

---

## 2026-09-14 — Step 7 done: freshness chain for group-map edits

`index/group_map_live.csv` stays hand-edited (D22). Every derived file now
knows when an input it was built from is newer, and the page shows it; nothing
rebuilds a portfolio on its own. Chain: drops + map + fol -> params ->
baseline -> portfolio input/ (the last link compared by content, so a rebuild
that changes nothing never nags).

### Changes

| File | What |
|---|---|
| `scr/common.py` | `newer_than(target, *inputs)`; freshness chain in the docstring. |
| `scr/baseline.py` | `params_stale(pp, db, map, fol)`. `ensure()` rebuilds params when the map or fol is newer too (was db only), so a fork after a map edit never uses the old grouping. `run()` FAILs on the same rule. |
| `scr/app.py` | `/api/state` adds `stale` (built anchor -> inputs newer than its params/baseline), `unmapped` (tickers on the latest session the map lacks), `group_map.edited`. Portfolio summary and detail add `refork` = `{needed, lost, appeared}` from `portfolio.carry` when input/ differs from the anchor's baseline grid. |
| `scr/static/desk.js` | Data tab: fresh / stale / unmapped pill, unmapped list, stale anchors with the reason, "(stale)" in the anchor selects. Cards: "regrouped since fork, re-fork" pill with the ratings a re-fork loses. Fork step: same note, "(stale, rebuilds on fork)". |
| `tests/test_baseline.py` (new), `tests/test_app.py` | `newer_than`; `ensure` rebuilds after a map edit and is a no-op when fresh; state shows stale anchor and refork before/after a re-fork. |

### Checks

- pytest 62 passed; ruff clean.
- Live: `stale {}`, `unmapped []`, both portfolios `refork needed False`;
  `baseline.py` and `target.py` output unchanged.
- `index/group_map_default.csv` was read by nothing; removed afterwards with
  `git rm` (recoverable from HEAD).

---

## 2026-09-14 — Step 6 done: Flask desk app

Local app over the scripts, matching mockup v3. Runs on 127.0.0.1:5000; holds
no state; writes only hand-edit files. Preview uses the Python solver, so the
mockup JS solver discrepancy (step 5) is gone.

### Changes

| File | What |
|---|---|
| `scr/target.py` | `build` split into `compute(name, book=, tactical=, constraints=)` (no writes, no prints; overrides stand in for files) + `build` (writes and report). `parse_tactical(grid)` factored out of `read_tactical` (kept for backtest.py). |
| `scr/app.py` | Flask JSON API + one page. Actions call `ingest.main`, `params.main` + `baseline.main`, `portfolio.new/fork/screen/delete`, `target.build`, and return stdout. Saves: statement, constraints (validated first), book + tactical from a spec (baseline order, auto-NO, barrier rule, no invalidated names). Atomic writes. Global lock. Guards: local Host header, JSON body on mutations. |
| `scr/templates/desk.html`, `scr/static/desk.css`, `scr/static/desk.js` | Mockup v3 CSS verbatim; JS ported to fetch state/portfolio and post edits to `/preview` (debounced). Unsaved Book/Constraints edits stay in the page with a Save/Discard bar; Fork and Build are disabled while dirty. |
| `docs/ui_mockup_v3.html` | Approved mockup, copied out of the session scratchpad. |
| `requirements.txt` | `flask==3.1.3`. |
| `tests/test_app.py`, `tests/test_portfolio.py` | +14 tests: compute == build and writes nothing, overrides, page/API load, book round-trip with auto-NO, 6 rejected inputs, preview == build, validation before write, new/fork/screen/delete lifecycle, guards. |

### Checks

- pytest 59 passed; ruff clean.
- Live targets after the split: holdings and allocation identical to the
  pre-change snapshot, stdout identical; only `built_at` moved.
- Live API: all-three-caps preview on high_growth reproduces step 5 (Banks -
  Private 20, RE Residential 20, Consumer Retail 15; 5 large at 50.00%, 9 at T).
- Saving both live books unchanged through the API is byte-identical.
- Browser: data page, book edit (OW preview 46.0% -> 51.6%), constraints
  preview, discard; no console errors. Drag-and-drop, tactical search, and
  fork/screen/build clicks were not exercised in the browser (covered by API
  tests only).

---

## 2026-09-14 — Step 5 done: scripts for the v3 UI

User approved mockup v3 and this plan, with both recommendations:

- **D20 sticky baseline copy.** Baselines live in `portfolio/baseline/<anchor>/`.
  `baseline.py` also copies the sticky anchor's two files to the root, only
  because `backtest.py` reads the root. The root anchor is also the default for
  `portfolio.py fork`.
- **D21 legacy sector cap.** Portfolios use `constraints.json`.
  `sector_cap.json` stays, read only by `backtest.py`; `target.py` WARNs if it
  says yes without a sector constraint and does not apply it.

### Changed

| Script | Change |
|---|---|
| `common.py` | `constraints.json` schema + validator, `portfolio_anchor()`, `read_invalid()` |
| `params.py` | build factored into `run()` so fork can call it |
| `baseline.py` | dated folders, sticky root copy, `ensure(anchor)` builds on demand |
| `portfolio.py` | `fork --anchor`; `screen --invalidate/--invalidate-all/--restore`; auto-NO on emptied groups; `delete --yes`; `new` writes default `constraints.json` |
| `target.py` | prices from the portfolio's anchor; invalidated-name guard; `waterfill` + `apply_constraints` solver; outputs gain `full` (allocation) and `pin` (holdings); db/resolver dependency dropped |

`apply_caps` left untouched because `backtest.py` imports it; a test pins
`waterfill` to it.

### Live migration

`baseline.py` built `portfolio/baseline/2026-09-11/`; both portfolios got an
all-off `constraints.json`, were re-forked (provenance now points at the dated
folder) and rebuilt. Targets are identical to the pre-change snapshot on every
old column. Backtest `equity.csv`, `rebalances.csv`, `summary.csv` identical
for both portfolios; only `built_at` differs.

### Solver vs mockup (high_growth, live)

| Scenario | Python | Mockup JS |
|---|---|---|
| sector 25% | BP 25, RE 25, FS 13.97 | same |
| + stock 10% | BP 25, RE 20, FS 16.24 | same |
| large only (5/50) | VHM 14.57, BP 35.43, RE 19.57; large 5 = 50.00% | same |
| stock 5% | FAIL 85% < 100% | same |
| all three | BP 20, RE 20, CR 15; large 5 = **50.00%** | BP 25, Logistics 16.16, RE 15; large 5 = 44.8% |

The three-constraint case differs. Python is correct to the D17 spec: the five
large names are already at the 10% stock cap and sum to exactly L, so nothing
scales; every other holding stops at T (5%), which caps Banks - Private at
4 × 5% = 20%. The JS scaled the large set before the stock cap had settled and
under-filled L. Python counts every holding exactly at T as `at_threshold`
(9), the JS only counted names it had stopped (4).

### Checks

- `pytest`: 45 passed (24 before). New: sector per-group incl. tactical,
  unknown per-group name, legacy cap not applied, waterfill = apply_caps,
  stock cap within group, stock cap spill, stock infeasible, large stop-at-T,
  large pro-rata scaling, large infeasible, constraints validation (6),
  fork onto a second anchor, portfolios on different anchors, missing anchor,
  invalidate/restore/refork, invalidating the last name -> NO, tactical claim
  of an invalidated name, delete.
- `ruff check scr tests`: clean.

---

## 2026-09-14 — Step 4: UI mockup v1 and v2

Artifact (same URL for both versions):
https://claude.ai/code/artifact/f91fc60e-8d80-4599-a4b3-657c7b00ddc8

v1 reproduced `target.py` exactly on 2026-09-11 (high_growth 17 names, Banks -
Private 46.01%, VHM 18.91%). v2 applied the user's review:

- Portfolios: delete with inline confirm; one `+ New portfolio` card.
- Fork: pick any anchor with 21 sessions behind it. Mockup carries real params
  for 7 anchors (06-30, 07-31, 09-07..09-11), computed by `params.compute`.
- Screen: **Invalidate** replaces blanking. Invalidated names stay in the book,
  struck through, undraggable, and cannot be claimed by a tactical group.
- Book: two boxes per group (all names | investable), drag to include, add
  all / remove all per group and globally. Empty investable box = NO
  automatically; a name arriving in an auto-NO group makes it AV again.
  Tactical overlay lives under the book; claims are retroactive on the boxes.
- Step 5 = constraints, three tick boxes with a live status table.

### Decisions

- **D17 stop-at-T.** UCITS-style large-holding cap: names above threshold T are
  scaled pro-rata to the aggregate cap L; a name that would fall under T stops
  at T and leaves the large set (and no longer counts toward L). Excess pours
  into names below T, each stopping at T, so no name ever crosses T and the
  large set only shrinks. Infeasible when L + T × small names < 100%; the
  solver fails and reports rather than leaving cash.
- **D18 within-group spill.** Excess from a stock-level cap goes to the same
  group's uncapped names first (sector allocation and tilt preserved); it
  spills to other groups pro-rata only when the group is full or at its sector
  cap. Order: sector cap → max per stock → UCITS; loop until stable.

### Solver check (in-page JS, high_growth)

| Scenario | Sum | Result |
|---|---|---|
| none | 100% | matches target.py |
| sector 25% | 100% | Banks-Private and RE-Res both at 25% |
| sector 25% + stock 10% | 100% | VHM, NVL, CTG at 10% |
| all three (T 5%, L 50%) | 100% | 5 large sum 44.8%, 4 held at 5% |
| UCITS only | 100% | VHM 14.57%, 5 large sum exactly 50% |
| stock 5% × 17 names | fail | 85% < 100%, reported before solving |

Two bugs found and fixed in the process: spill gave up after one pass, and
names held at T were wrongly counted against L.

---

## 2026-09-14 — Step 3 done: retire and document

### Removed (`git rm`, recoverable from HEAD `415532e`)

| Path | Why |
|---|---|
| `.claude/commands/eod.md`, `live.md` | MCP path retired (D1) |
| `scr/load_eod.py`, `data/raw/`, `data/live/`, `data/eod.parquet`, `data/universe.yml` | replaced by `ingest.py` + `market.db` |
| `scr/build_baseline.py`, `build_portfolio.py`, `build_portfolio_target.py` | replaced by `baseline.py`, `portfolio.py`, `target.py` after exact parity |
| `scr/build_group_map.py` | needed the parquet's ICB codes; FiinPro drops carry no sector, so it cannot be ported. `params.py` now fails naming any unmapped ticker. |

### Changed

- **`scr/backtest.py`**: imports moved to `common` + `target`; docstring reference
  updated; three lint-only fixes (unused unpack renamed, timezone-aware
  `built_at`, `noqa` on a date-only `strptime`). Before/after runs for both
  portfolios into scratch folders: `equity.csv`, `rebalances.csv`,
  `summary.csv` **identical**. The real `portfolio/*/backtest/` outputs were not
  overwritten.
- **`scr/load_history.py`**: `DROP` repointed to `data/fiinpro/archive/`,
  marked LEGACY. Without this, a rebuild would have read `VN100 data.xlsx` and
  replaced the backtest history. `local_history.db` itself not rebuilt.
- **`data/fiinpro/rejected/`**: holds `VN100 14-9.xlsx` (untracked, git-ignored),
  so neither loader sees it.
- **`requirements.txt`**: `pyarrow` dropped.
- **`.gitignore`**: parquet/MCP comments replaced; ignores `rejected/`.
- **Docs**: `CLAUDE.md`, `HANDOFF.md`, `.claude/skills/pipelining/SKILL.md` and
  `reference/example.md` rewritten for the FiinPro-only pipeline.
- **Claude memory**: `pm-mike-eod-pipeline` and `pm-mike-portfolio-tilt` updated.

### Checks

- `ruff check scr tests`: clean across all of `scr/` (legacy findings gone with
  the deleted files).
- `pytest`: 24/24.

---

## 2026-09-14 — Step 2 done: baseline, portfolio, target

### Decisions from the user (resolving step 1's open items)

| # | Decision |
|---|---|
| D12 | Anchor moved to 2026-09-11 (`index/anchor_date.json`). |
| D13 | Accept 20 groups; Mining is gone (MSR, F88 not in the drop). |
| D14 | FOL skipped for now: `fol_limit` stays blank, no FOL screen accepted. |
| D15 | `turnover_21_pct` = average DAILY turnover over 21 sessions ÷ float cap × 100. |
| D16 | No commits until the user asks. |

### Design choices made in step 2

- **File names kept** (`sector_constituents.csv`, `sector_allocation.csv`,
  `sector_constituents_custom.csv`): `scr/backtest.py` reads them and D9 says do
  not touch it. The planned rename to `book.csv` is dropped.
- **Old scripts left in place** (`build_baseline.py`, `build_portfolio.py`,
  `build_portfolio_target.py`): `backtest.py` imports from the last one.
- **Re-fork carries by group name**, not column position: ratings, multipliers
  and deletions survive a baseline whose group set changed.
- **Screens suggest, the user applies**: `screen` writes `exclusions.csv`;
  `--apply` / `--apply-all` blank tickers in the book.
- **Old "pending corporate action" FAIL replaced** by a WARN on
  `adj_factor != 1` in `baseline.py` (an after-the-fact extract cannot detect
  a pending action).
- **Holding range** is a WARN in `target.py`, recorded in `built_from.txt`.
- **Rebalance mandate** allows frequency, drift threshold, or both.

### Built

| File | What |
|---|---|
| `scr/common.py` | Paths, file-name constants, `BookError`, grid read/write, yes/no switch parser, `statement.json` schema + validation. |
| `scr/baseline.py` | `data/params/<anchor>.csv` → `portfolio/baseline/`. FAILs if params missing or older than `market.db`. WARNs on group-set change and `adj_factor != 1`. |
| `scr/portfolio.py` | `new <name>` (folder + default statement), `fork <name>` (snapshot baseline, carry book by name), `screen <name> [--apply T.. / --apply-all]`. |
| `scr/target.py` | Port of tilt, tactical groups, sector cap from `build_portfolio_target.py`, priced from params. Adds holding-range check. FAILs if params rebuilt after the baseline. |
| `scr/params.py` | Turnover switched to daily average (D15); FOL WARN downgraded to info (D14). |
| `tests/test_portfolio.py` | 16 tests: baseline weights, untilted = baseline, tilt + deletion, tactical budget move, iterative cap, cap too tight, holding range, addition rejected, re-fork carry, screen suggest/apply, `new` guards, statement validation. Suite: 24/24. |
| `portfolio/*/statement.json` | Created for both portfolios: approach/scope blank, holdings 20–30 (default), rebalance 1Q + drift 0.05 (mirrors `backtest_rebalance.json`), screens off. |

### Parity check (passed)

Scratch script `parity.py` (session scratchpad, not in repo) fed the new code
the OLD 2026-08-12 parquet data, float cap on the old basis, and ran old and new
target builders side by side on a copy of `portfolio/`:

- New baseline grid byte-identical; allocation equal to 1e-9.
- 5 target variants (high_growth as-is and cap 0.25; soe_dom as-is, tactical,
  tactical + cap 0.2): max absolute weight difference **0.0**; `capped`, `kind`,
  sector assignment identical; re-fork preserved both books.

### Live run on 2026-09-11

| Portfolio | Groups | NO | Holdings | Range 20–30 |
|---|---|---|---|---|
| hsc_strat_high_growth | 20 | 12 | 17 | OUTSIDE |
| hsc_strat_soe_dom | 20 | 0 | 100 | OUTSIDE |

- high_growth lost **Mining OW ×4** (MSR gone). Largest target groups: Banks –
  Private 46.01%, Real Estate – Residential 20.63% (UW 0.5), Financial Services
  9.32%.
- soe_dom tactical overlay verified on live data in a scratch copy (switch left
  off in the repo): SOE Divestment claims GAS, BSR, PLX, 0.85% of the book.
- Baseline top groups: Banks – Private 31.13%, Real Estate – Residential
  27.92%, Banks – State 6.78%.

### Not verified

`scr/backtest.py` was not run (D9). Both books and the baseline now have 20
groups, so its sector-row check should pass, but it still reads
`data/local_history.db`.

### Commands

```powershell
.venv\Scripts\python.exe scr\ingest.py
.venv\Scripts\python.exe scr\params.py
.venv\Scripts\python.exe scr\baseline.py
.venv\Scripts\python.exe scr\portfolio.py new <name>
.venv\Scripts\python.exe scr\portfolio.py fork <name>
.venv\Scripts\python.exe scr\portfolio.py screen <name> [--apply-all]
.venv\Scripts\python.exe scr\target.py <name>
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\ruff.exe check scr\ingest.py scr\params.py scr\common.py scr\baseline.py scr\portfolio.py scr\target.py tests
```

---

## 2026-09-14 — Step 1 done: ingest + params

### Why the rebuild

FiinQuant MCP stalled. The old design had the agent fetch EOD data via MCP
(`/eod`, `/live`) into `data/eod.parquet`, with FiinPro history in a separate
`data/local_history.db`. Two stores, two schemas, one vendor. Decision: rebuild
from the ground up around ONE database fed only by hand-downloaded FiinPro
Portal drops, with old scripts as reference only.

### Decisions locked with the user

| # | Decision |
|---|---|
| D1 | FiinPro Portal drops in `data/fiinpro/` are the only market input. `/eod`, `/live`, MCP, `eod.parquet`, `data/raw/`, `data/live/`, `universe.yml` are to be retired. |
| D2 | Float cap = `free_float × close_raw`. Vendor `market_cap` is a QA column only (it has transcription gaps). Never `close_adj`. |
| D3 | Parameters are always computed from the db into a ready-to-use CSV. Screens read it, never compute. |
| D4 | Screening filters (21d turnover / float cap, FOL, free-float cap) are optional and per portfolio. They produce suggested exclusions the user accepts into the book. They never cut the baseline universe. |
| D5 | `index/group_map_live.csv` is the default sector grouping. |
| D6 | Portfolio statement per portfolio: approach and scope (plain text), holding range min–max (warn if outside), rebalance mandate (frequency 2W / 1M / 1Q, or aggregated drift threshold). |
| D7 | Drift metric = half the sum of absolute weight gaps at group grain. |
| D8 | Tactical groups, tilt, sector cap: same semantics as today. |
| D9 | Backtester: do nothing for now. |
| D10 | UI: a local Flask app from the venv (a published artifact cannot run scripts or write files). Artifact mockup first, app after approval. |
| D11 | Raw input for the new db is `data/fiinpro/VN100 data.xlsx` only. Earlier drops are archived. |

### Rejected input

`VN100 14-9.xlsx` was NOT the VN100: 100 HOSE tickers alphabetically AAA–DRH,
only 21 overlapped the sector map, and it had no trading value column. Archived.

### Built

| File | What |
|---|---|
| `scr/ingest.py` | `data/fiinpro/*` (top level) → `data/market.db`. Rebuilt from scratch each run, temp file swapped in only if every drop validates. Finds the header row ("No"), strips banner/footer, drops non-session rows (no close). Validates duplicates, nulls, OHLC coherence (raw and adj), negatives, free float ≤ shares; vendor cap mismatch is a warning. Refuses two drops overlapping on (date, ticker). Writes `data/market.txt`. Sector and FOL are NOT stored. |
| `scr/params.py` | `market.db` + `index/group_map_live.csv` + `index/fol.csv` → `data/params/<anchor>.csv`. Anchor: `--date` > `index/anchor_date.json` > latest. Ticker missing from map or blank group = FAIL. |
| `tests/` | `conftest.py` (synthetic drop with banner/footer), `test_ingest.py` (4), `test_params.py` (4). 8/8 pass. |
| `index/fol.csv` | Header-only template `ticker,fol_limit` (ratio 0–1). |
| `requirements.txt` | Now matches imports: pandas, numpy, duckdb, openpyxl, pyarrow (legacy), ruff, pytest. Dropped anthropic, mcp, python-dotenv (unused). |
| `.gitignore` | Ignores `data/market.db*`. |
| `data/fiinpro/archive/` | Old drops moved here (`git mv` for tracked ones). Ingest ignores it. |

### params CSV columns

`ticker, company_name, exchange, icb_l2, group, close_raw, outstanding_shares,
free_float, free_float_ratio, float_cap, market_cap_vendor, mcap_gap,
sessions_21, value_21, adv_21, turnover_21_pct, fol_limit, adj_factor`

- Liquidity window = 21 sessions ending on the anchor, inclusive.
- `turnover_21_pct` = cumulative `value_21 / float_cap × 100`; NaN if < 21 sessions.
- `adj_factor` = `close_adj / close_raw`; ≠ 1 means a corporate action between anchor and extract date.

### State on disk after step 1

| Item | Value |
|---|---|
| `market.db` | 16,900 rows, 100 tickers, 169 sessions, 2026-01-05 → 2026-09-11, all HOSE, 3.2 MB |
| Non-session rows dropped | 100 (all 2026-09-14, not yet traded at extract) |
| Vendor cap mismatches | 20 rows; worst NVL 9.0%, SHB 4.0% |
| Params written | `data/params/2026-08-12.csv` (sticky anchor), `data/params/2026-09-11.csv` |
| Groups | 20 (Mining lost: MSR, F88 not in drop) |
| Map rows not in drop | DXS, F88, HDC, IMP, MSR, SCS, SZC |
| turnover_21_pct (09-11) | median 13.2%, min BWE 1.4%, max 78.0% |
| Corporate action since 08-12 anchor | BID, DIG, MSB, PHR, SSI, TCH, VIB, VIX, VPI |

### Untouched on purpose

Legacy scripts (`load_eod.py`, `load_history.py`, `build_*.py`, `backtest.py`),
`data/eod.parquet`, `data/local_history.db`, both portfolios. The backtester
still runs on the old store.

### Not committed

Nothing from step 1 is committed yet. Branch `docs/handoff`.

### Commands

```powershell
.venv\Scripts\python.exe scr\ingest.py
.venv\Scripts\python.exe scr\params.py                 # sticky anchor
.venv\Scripts\python.exe scr\params.py --date 2026-09-11
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\ruff.exe check scr tests
```
