# Plan — standing target, fill at the open, decision log and replay

Written 2026-09-21 after a design session. This is the spec for the executing
model. Read `CLAUDE.md`, `Task.md`, `Progress.md` and the module docstrings of
`scr/backtest_engine.py`, `scr/target.py`, `scr/common.py`, `scr/app.py` first;
every docstring is its script's spec and must be updated with the code.

Rules that hold throughout: PowerShell only; `.venv\Scripts\python.exe`;
`uv pip install`; scripts own every derived write; no commits until asked; each
step ends with `pytest tests -q`, `ruff check scr tests` and
`node --check scr\static\desk.js` green, a `Progress.md` entry (newest first,
same shape as the D49 entry) and the `Task.md` box ticked.

Three steps, in this order. Step A changes every published backtest number, so
it lands alone and is measured alone before B and C start.

---

## Decisions taken (record these in Progress.md as you land them)

- **D50 standing target.** The target is derived at inception, on each calendar
  boundary and on each recorded decision, and *held* between them. Breach and
  drift fills trade against that standing target; drift is measured against it.
  Supersedes D42 (drift vs a re-derived target) and the "re-derived" clause of
  D43. With `frequency: null` the target changes only on a decision.
- **D51 per-trigger policy.** Calendar and decision fills reset the whole book
  to the standing target (`full`). A breach fill only clips the broken cap and
  spills the excess (`edge`) — the D18 waterfill applied to the *held* weights.
  Drift fills are `full`. Edge falls back to `full` when the clip is infeasible
  (dead names shrank capacity); the log says so.
- **D52 fill at the open.** A decision observed at close `t` fills at the open
  of `t + lag_sessions` (lag ≥ 1). The fill session is two legs: held weights
  earn close→open, the trade happens, new weights earn open→close. `lag 0`
  keeps today's meaning (same close). Removes the blind session; triggers are
  still checked at every close.
- **D53 decision log.** `portfolio/<name>/decisions/` holds an append-only log of
  allocation profiles (book + tactical overlay only). Recording is explicit
  (desk button, editable effective date, note). The engine replays them as
  `decision` triggers; `timeline=None` is exactly today's behaviour.
- **D54 screening is universal, and that is a recorded bias.** A past profile
  is filtered through today's `screen/invalid.csv` like the live book. A name
  invalidated after a decision is dropped from that decision at replay, with a
  message; `built_from.txt` and the desk caveats name the look-ahead.
- **D55 calendar clock (user, 2026-09-21).** A decision does not move the
  calendar. Under `1M`, a decision on the 10th sets the target at once and
  the next refresh is the first session of the next month, as before.
  `period_keys` stays a pure function of the date; no change to it.

---

## Step A — standing target and breach-to-edge

### A1. Engine state (`scr/backtest_engine.py`, `simulate`)

Add `standing` (weight vector over `names`), `standing_at` (session index of the
decision that set it). `target_fn(i)` is called **only** when the trigger is
`inception`, `calendar` or (Step C) `decision`. Breach and drift use `standing`:

```
drift(w, standing)                    # not target_fn(i)
breach(w)                             # unchanged check, on held weights
```

Consequences to implement, not just note:

- The per-session `target_fn(i)` call at line ~301 (drift check every session
  when `threshold` is set) goes away; so does the one-entry `cache` in `run()`.
- Before inception `standing is None`; no breach or drift check runs.
- **Dead names.** A name whose `fcap` on the fill session is NaN or 0 is zeroed
  in the vector traded to and the rest renormalised; the names go in the
  existing `gone` column (rename the column `dropped` and put both group and
  ticker drops there, `;`-joined). Prices still forward-fill for returns.

### A2. Edge fill for a breach

New function in `backtest_engine.py`:

```python
def edge_fill(w, names, group_of, fcap_alive, cons) -> tuple[np.ndarray, str]:
    """Clip every cap the held weights break and spill the excess within the
    cap's own scope (D18) -> (weights, "edge" | "full"). Reuses
    target.apply_constraints on the HELD weights: group weights = held group
    sums, the pro-rata key = held stock weights (passed as `fcap`), members =
    names alive on the session. Infeasible -> (standing, "full")."""
```

`target.apply_constraints(w_group, members, fcap, cons)` only uses `fcap[t]` as
the within-group pro-rata key, so passing the held weights as that key makes it
"clip the broken cap and spread the excess over the group by current weight",
including the UCITS stop-at-T loop. No new maths. Capacity is unchanged from the
standing solve unless names died, so infeasibility only arises then → fall back.

`breach_check` keeps returning the reason string; `simulate` on trigger
`breach` calls `edge_fill` and records `policy`.

### A3. Fill bookkeeping

`fill()` takes the vector and policy from `pending`. Events gain:

```
policy        full | edge
target_as_of  ISO date of the session whose derivation set the standing target
```

`holdings_end.csv` `target` column = `standing` at the end (rename its header
from "last target" to "standing target" in the docstring).

### A4. Files and docs

- `rebalances.csv`: `+ policy, target_as_of`; `gone` → `dropped`.
- `summary.csv`: unchanged keys.
- `built_from.txt` `mandate:` line: add `target held between calendar dates;
  breach clips to the cap (D50, D51)`.
- Module docstring: rewrite lines 14–47 (target math "on session d" → "on the
  session that sets the standing target"; triggers section; the D42 sentence).
- `scr/common.py` docstring lines 22–29 (rebalance grammar): drift is measured
  against the standing target.
- `scr/static/desk.js`: Mandate panel (line ~1139) "vs the target re-derived
  that session" → "vs the standing target"; caveat bullet 3 (line ~1162)
  rewritten for D50/D51; breach pill title; Rebalance log gets a `Policy`
  column and `Target as of`; `backtest_json` in `app.py` passes both.
- `Task.md` "Later": strike "trade to band edge".

### A5. Tests (`tests/test_backtest_engine.py`)

Change:
- `test_drift_is_measured_against_the_rederived_target` → rename
  `..._against_the_standing_target`, invert: with AAA +10% *and* its fcap moved,
  drift **does** fire (the standing target did not follow).
- `test_breach_trigger_waits_for_the_tolerance`: on the +30% case assert AAA
  ends at `0.42` (the cap), `policy == "edge"`, the other G1 names absorbed the
  spill pro-rata to held weight, G2/G3 weights untouched, `n_breach == 1`.
- `test_returns_drift_the_weights_and_drift_trades_to_target`: passes as is
  (frequency null → standing = inception target); keep and assert
  `policy == "full"`.

Keep unchanged (calendar still re-derives): `test_monthly_calendar_rederives_budgets`,
`test_infeasible_constraint_on_a_past_date_fails`,
`test_group_with_no_priced_name_drops_out` (column rename only),
`test_underweight_larger_than_a_past_neutral_holds_zero`.

Add:
- `test_breach_between_calendars_trades_to_the_standing_target`: `1M`, fcap of
  a name changes mid-month, a breach fires; the fill's `target_as_of` is the
  month's first session and the weights match the *old* fcap solve.
- `test_edge_fill_falls_back_to_full_when_infeasible`: a name dies between the
  standing solve and the breach; `policy == "full"`, message present.
- `test_no_target_call_on_a_breach_or_drift_check`: monkeypatch `targets_at`
  to count calls; with `1M` over three months and a drift threshold, exactly
  three derivations (inception + two calendars).

### A6. Measure before moving on

Run `build` on `energy_focus` (the user's choice; it carries the Step 15
breach numbers) before and after, on a scratch copy of the folder so the live
`backtest_engine/` outputs are not overwritten until the user asks. Record in
Progress.md: rebalance counts by trigger, turnover, cost, total return. The
user expects turnover to rise and wants the number.

---

## Step B — fill at the open

### B1. Market (`load_market`)

Select `open_adj` as well; return `mk["open"]` pivoted like `adj`. Test helper
`make_db` (`tests/test_backtest_engine.py`) must write an `open_adj` column
(default `= close_adj`; add an `opens={(date, ticker): price}` override).

### B2. Two-leg session (`simulate`)

Precompute over `names`:

```
R_pre[i]  = open[i]  / adj[i-1] - 1        (ffill both, fillna 0)
R_post[i] = adj[i]   / open[i]  - 1
```

Every session `i > start`: leg 1 on held weights (`nav`, `w` drift by `R_pre`);
if `pending["fill"] == i` and `lag >= 1` → `fill(i)`; leg 2 by `R_post`. With
no fill the two legs compound to exactly today's `R[i]`, so every existing
number is unchanged when `open == close` (the fixtures) — assert that in a test.
`lag == 0` keeps filling at the close of the decision session, after leg 2.

Guard: `open_adj` NaN or ≤ 0 for a name on a fill session → use that session's
`close_adj` for both legs of that name and add one INFO message per run.

### B3. Blind session

With `lag 1` the fill is done before the close of `i+1`, so `pending is None`
at that close and triggers are checked: the blind session is gone. For `lag ≥ 2`
the intermediate sessions stay blind by construction (the trade is in flight);
say so in the docstring and the desk caveat.

### B4. Docs

- Engine docstring: decision/fill paragraph (line ~48), returns paragraph
  (line ~52), `built_from.txt` `config:` line ("fills at the open").
- `common.py` line 63: `lag_sessions  integer 0-5: fill at the OPEN this many
  sessions after the decision; 0 = same close`.
- `desk.js` Rebalance log header (line ~1102) "fill N sessions later" → "at the
  open N session(s) later"; caveat bullet 4.

### B5. Tests

- `test_fill_at_open_splits_the_session`: `opens` puts AAA's open on the fill
  day at 1.05 with close 1.10; nav = (leg 1 at old weights) × (leg 2 at new).
  Hand-compute like the existing drift test.
- `test_open_equal_close_reproduces_close_fills`: run the same scenario with
  `open == close` and compare equity to a saved expectation from Step A.
- `test_trigger_is_checked_on_the_session_after_a_fill`: breach condition
  appears on `t+1`; with `lag 1` the breach decision is dated `t+1`, not `t+2`.

---

## Step C — decision log and replay

### C1. Storage

```
portfolio/<name>/decisions/log.csv        id, effective, recorded_at, anchor, note
portfolio/<name>/decisions/<id>.json      one profile
```

`id = <effective>-<NN>` (`2026-09-21-01`, `-02` on the same day). Append-only;
no delete route in v1 (hand-delete the row and file; document it). The profile
is the desk's book spec **verbatim** (app.py docstring lines 44–54) plus
metadata — no new grammar:

```json
{"id": "2026-09-21-01", "effective": "2026-09-21", "recorded_at": "2026-09-21 14:02",
 "anchor": "2026-09-10", "note": "cut Steel to UW2 after the export data",
 "groups": {"Banks - Private": {"rating": "OW2", "pp": 4, "investable": ["TCB", "..."]}},
 "tactical": {"on": true, "groups": [{"name": "SOE Divestment", "rating": "OW1",
                                      "pp": 2, "members": ["GAS"]}]}}
```

### C2. Grammar and I/O (`scr/common.py`)

`DECISIONS = "decisions"`, `DECISION_LOG = "log.csv"`; `validate_decision(d)`
(ISO `effective`, non-empty `id`, `groups` a dict, ratings in `RATINGS`, pp
numeric); `load_decisions(home) -> list[dict]` sorted by `(effective, id)`,
`[]` if the folder is absent; the grammar block in the module docstring next to
`backtest_config.json`.

### C3. Move the spec→grid code out of the desk

`tactical_grid`, `book_grid`, `grids_from_spec` (`app.py:159–231`) are pure
grid functions; move them to `scr/portfolio.py` (which owns `carry`), import
them back into `app.py`. Add:

```python
def reconcile_profile(profile, base, invalid) -> tuple[dict, dict]:
    """A recorded profile onto the CURRENT baseline grid -> (spec, report).
    Names no longer in their column or invalidated since: dropped. Groups no
    longer in the grid: dropped (their pp with them). Groups the grid gained:
    rated NO. report = {"dropped_names", "lost_groups", "appeared", "invalidated"}."""
```

Then `grids_from_spec(home, spec)` strict, as today. This is the fork's
carry-by-name rule applied to a profile.

### C4. Engine (`scr/backtest_engine.py`)

- `load_book(name, ..., book=None, tactical=None, strict=True)` passes the
  overrides to `target.compute`. Replayed profiles load with `strict=False`;
  active-rule faults (a lost group breaking net-zero) become WARN messages
  naming the decision id — the run continues, `tilt` floors and renormalises.
- `run(..., timeline=None)`. `None` → `[(i0, live book)]`, today's path.
  Otherwise for each decision: `j = S.searchsorted(effective)` (snap forward);
  everything with `j <= i0` collapses to one entry at `i0` keeping the latest;
  equal `j` → last wins; `j >= N` → dropped with an INFO message. If nothing
  lands at `i0` the live book is *not* inserted — the first decision is the
  window's opening book (the "T0 back-dated" row); tell the user in a message.
- `names` = sorted union over every loaded book; each book's vector is padded
  with zeros to the union; `groups`, `breach_check` and the drift `gi` index are
  per book. `simulate` receives `books: list[(index, book)]` and a
  `derive(book, i)` callable; the standing state holds the active book.
- Trigger ladder, first match wins: `decision` (a book with index ≤ i not yet
  applied) → `calendar` → `breach` → `drift`. A decision landing while a fill
  is pending is applied at the first session with no fill pending, logged
  `deferred_from`. A decision on a calendar boundary is one `decision` fill
  with `also = "calendar"`.
- Outputs: `rebalances.csv` `+ profile` (decision id or `live`), `+ also`,
  `+ deferred_from`; `summary.csv` `+ n_decision`; `built_from.txt` a
  `decisions:` block (id, effective, session applied, dropped names/groups);
  `run()` returns `timeline` for the desk.
- CLI: `--mechanical` ignores the log; default replays it when present.

### C5. Desk (`scr/app.py`, `scr/static/desk.js`)

- `GET  /api/p/<name>/decisions` → the log with each profile's report against
  the current grid.
- `POST /api/p/<name>/decisions` `{effective, note, version}` records the book
  **as saved on disk** (read `BOOK`, `TAC`, `TAC_SWITCH`, build the spec the
  same way `detail()` does). Version-checked against `"book"`. Refuse when the
  page says the book is dirty — the button is disabled until saved, and the
  server rejects a stale version anyway.
- `detail()` gains `decisions` and `head_matches_book` (current book spec ==
  latest profile's spec after reconcile). Book step: a "Record decision" button
  (disabled while dirty) with date + note fields, a badge "book differs from the
  last recorded decision" when `head_matches_book` is false, and the log list.
- Backtest tab: "Replay decisions" toggle (on by default when the log is
  non-empty; passes `mechanical: true` otherwise), `mk-decision` chart marker
  and legend entry, `decision` pill in the Rebalance log, a Timeline table
  under it (id, effective, applied session, note, drops). Caveat bullet for D54.

### C6. Tests

`tests/test_portfolio.py`: `reconcile_profile` — alien name, lost group, gained
group, invalidated-since; `load_decisions` ordering and validation.

`tests/test_backtest_engine.py`:
- `test_no_timeline_equals_the_live_book`: equity and rebalances identical with
  `timeline=None` and with a one-entry timeline of the live book at `-inf`.
- `test_decision_is_a_trigger_and_sets_the_standing_target`.
- `test_decisions_before_the_window_collapse_to_the_start`.
- `test_decision_on_a_calendar_boundary_is_one_fill` (`also == "calendar"`).
- `test_decision_during_a_pending_fill_is_deferred`.
- `test_decision_after_the_history_is_reported_not_applied`.
- `test_replayed_profile_drops_names_invalidated_since` (D54 message).
- `test_lost_group_in_a_profile_warns_and_runs`.

`tests/test_app.py`: record → list round-trip; stale version → 422; dirty
refusal; `head_matches_book` flips after an edit; backtest endpoint with
`mechanical`.

---

## Order of work inside each step

1. Read the docstrings you will change; write the new docstring text first —
   it is the spec.
2. Engine/common change with tests red → green.
3. `app.py` + `desk.js`; `node --check`.
4. Live run on the real portfolios; numbers into Progress.md.
5. Progress.md entry (decisions above), Task.md ticks.

Do not touch `scr/static/desk.js` for solver logic; the desk previews through
`target.compute` and the engine derives through `targets_at`. Nothing in this
plan changes `target.py` except the move of pure grid helpers *into*
`portfolio.py` (Step C3), which does not alter `target.py` at all.
