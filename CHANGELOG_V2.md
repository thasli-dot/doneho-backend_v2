# DoneHo v2 — Changelog & Design Notes

This document tracks changes made in `doneho-backend_v2` (a separate repo from
the original Kaggle submission, `doneho-backend`, which remains untouched).

Two sections, kept deliberately separate: what's actually **built and
verified**, and what's **designed and discussed but not yet implemented**.
Nothing in the second section should be described as a real feature until it
moves to the first.

**Terminology note:** `Goal` (e.g., "Study and Learning") is the broad
category; a `Goal` holds one or more `Task`s (e.g., "Crack UPSC 2027", "Learn
Python") underneath it. Milestones carry both `task_id` and `goal_id`. Some
of the design discussion below originally blurred these two levels — this
version corrects that: pacing, duration, and coverage tracking all belong at
the **Task** level, since two Tasks under the same Goal can have completely
different durations and deadlines.

---

## Implemented & Verified

### 1. Persistent session storage (Supabase)

**Problem it fixes:** the original design held all session state in an
in-memory Python dictionary (`SESSIONS` in `api.py`). Render's free tier
spins down after ~15 minutes of inactivity, wiping that dictionary --
meaning a returning user's entire history (goals, Blueprint, disruption
log, PRF) could be silently lost between visits.

**What was built:**

- `persistence.py` (new file) -- serializes the full orchestrator state
(`SharedExecutionState` + `ReviewEngine`'s PRF history) to/from JSON,
using Pydantic's `.model_dump()` / `.model_validate()`.
- Three Supabase (Postgres) tables: `sessions` (full state as JSONB),
`disruptions`, `weekly_performance` (structured, queryable copies for
future SQL analysis).
- `api.py`'s `get_orchestrator()` now checks Supabase before giving up on
a missing in-memory session, and every state-changing endpoint calls
`save_session()` before returning.

**Verified, not just built:**

- Local test: full write -> restart process -> read back, confirmed intact.
- Live Render test: started a session, submitted a goal, manually
restarted the actual deployed service from Render's dashboard, then
fetched `/state` again -- goal and computed values (LifeLoad, PRF,
commitment tier) all came back correctly. Screenshot on file.
- Fixed one real bug found during this work: `SuggestionItem` objects
inside `state.suggestions` weren't being serialized correctly
(`TypeError: Object of type SuggestionItem is not JSON serializable`) --
caught via a live `/commit` call failing with a 500 error, fixed in
`persistence.py`, redeployed, reverified working.

### 2. Real calendar-based day/week tracking

**Problem it fixes:** "day" and "week" previously only advanced because a
page happened to reload -- there was no check against the actual calendar
date. This meant: (a) nothing stopped a duplicate Day Output submission
within the same real day, and (b) `/week/start` (which records real
WeeklyPerformance into PRF) was never called anywhere in the frontend, so
PRF could never move past its first-week default.

**What was built:**

- Two new fields on `SharedExecutionState`: `last_day_submitted_date`,
`week_start_date` (both persisted via the same Supabase layer above).
- `orchestrator.check_calendar_rollover()` (new method) -- called on every
request via `get_orchestrator()`. Checks the real current date; once 7
real days have passed since `week_start_date`, it automatically calls
the existing `start_new_week()` logic (previously correct, but never
triggered) and resets the week marker.
- `submit_day_output()` now checks `last_day_submitted_date` first and
raises a clear `ValueError` ("Day already submitted today. Come back
tomorrow.") if a second submission is attempted the same real day --
surfaced to the frontend as a 400 error via the existing error handling.

**Verified, not just built:**

- Live Render test: submitted Day Output once (succeeded), submitted
again immediately with the same session -- correctly blocked with the
new error message.
- Week-rollover branch (7-day trigger) has not yet been directly observed
firing in real time (would require an actual 7-day wait, or a
deliberately shortened test) -- logic reviewed and consistent with the
existing, already-correct `start_new_week()` implementation, but not
yet watched happen live. Marked here honestly as verified-by-code-review
only, not by live observation.

### 3. NudgeAgent curated seed pattern integration + source tagging

**What was built:**

- `SuggestionItem.source: str = "ai_generated"` (new field,
`models/schemas.py`) -- every suggestion now carries an honest
"curated" vs. "ai_generated" tag. No other code needed changing for
this to flow through -- `api.py` and `persistence.py` both already
serialize `SuggestionItem` generically.
- `agents/seed_patterns.py` (new file) -- a condensed, prompt-ready
subset of `DoneHo_Seed_Reference_v3.md` (9 general rules + 10 of the
strongest illustrative patterns: mirror play, chopping-vegetables
contrast, snabbit-vs-vacuum, career multi-factor matching,
don't-double-count-effort, timing-trigger vs. concurrent-activity,
habit-stacking, immediate-capture). The full 100-task, 36-pattern
document stays in the repo as complete reference; this file is the
condensed version actually sent to the model on every call.
- `agents/nudge_agent.py` -- the seed content wired into the existing
instruction alongside `TECHNIQUES_BLOCK`/`GUARDRAILS_BLOCK`, plus an
explicit directive telling the model when to tag a suggestion
`"curated"` vs. `"ai_generated"`.

**Not yet verified:** written and syntax-checked, but not yet pushed to
the live repo or tested against a real `/commit` call -- unlike items 1-2,
this hasn't been watched working live yet.

**Explicitly NOT included in this pass:** the duration/pacing reasoning
half of the original combined item -- teaching ClarificationAgent to
distinguish habits vs. fixed-deadline goals vs. vague-target goals, and
the proposed search-grounded duration-research step. That's a
ClarificationAgent change, not a NudgeAgent one.

### 3b. Task duration/pacing reasoning (ClarificationAgent)

**What was built:**

- `ClarificationFlag` and `Task` (`models/schemas.py`) — new `task_type`
(`"habit" | "fixed_deadline" | "vague_target" | "self_evident"`) and
`duration_hint` fields, set by the agent for every task, not just
ambiguous ones.
- `agents/clarification_agent.py` — new `DURATION_REASONING_BLOCK`
teaching the 4-way classification, with the correct *kind* of question
per type (pacing question for fixed-deadline goals, scope/level
question with real anchor estimates for vague-target goals, no
question at all for habits).
- `orchestrator.py`'s `_on_goals_submitted` and `api.py`'s
`_serialize_goal` — the storage/response path needed to actually
surface `task_type`/`duration_hint` through the API. (Caught and fixed
a real gap here: the agent originally computed these fields correctly,
but nothing stored or returned them — a live 502 error surfaced this
during testing, same pattern as the earlier `SuggestionItem`
serialization bug.)

**Verified, not just built:** live test on Render — "Learn Python"
correctly classified `vague_target`, flagged ambiguous, with a question
containing real anchor durations ("basics ~4-6 weeks, solid ~3 months,
job-ready ~6+ months"). "Meditate daily" correctly classified `habit`,
NOT flagged ambiguous, no question generated at all — confirming the
"never ask habits a duration question" rule holds in live output, not
just in the prompt text.

**No frontend/UI change required for this value to be real today** —
the smarter question text renders through the existing plain-text
question display. `task_type`/`duration_hint` are exposed via the API
now, ready for the future quick-select UI (item 4) to use, without that
UI needing to exist yet.

**Still not built, remains below:** the proposed search-grounded step to
research realistic durations for novel tasks not covered by curated
examples — cross-reference multiple sources, disregard course-marketing
language, report a range with a stated reason, never a fabricated
precise number.

Source material: `DoneHo_Seed_Reference_v3.md` -- 8 general rules
(including the Combinability Tier terminology, kept explicitly distinct
from DoneHo's existing personalized `FocusLevel`), 100 tasks across all
10 real `GoalCategory` values, and 36 cross-persona patterns (gig
workers, elderly-parent caregivers, students, night-shift/rotating-shift
workers, users with hearing impairment or ADHD, newborn-stage parents,
and more). One entry (#30) surfaced a real technical implication for
item 2: the day-boundary logic may need a user-settable "day start"
rather than an assumed midnight, for rotating-shift users to work
correctly.

---

## Designed & Discussed -- Not Yet Built

The following came out of product design conversations and are **not**
implemented. Listed here so the reasoning isn't not lost, and so none of it
gets accidentally described as a real feature before it exists.

### 4. Clarification UX redesign -- quick-select + escape hatch

- Replace open-text clarifying questions with quick-select options built
from real anchors (e.g., Python skill level with real week estimates
per option), plus an explicit "something else" option opening a small
text field -- so common cases resolve in one tap, without boxing in
users with a genuinely different need.
- For tasks needing more than one clarifying dimension (e.g., meal prep:
cuisine *and* meals/week), batch into one compound card with sensible
pre-filled defaults, not two separate interruptions.
- Principle to preserve: ask only about the single highest-leverage
missing dimension per task category -- not a fixed question applied
everywhere. Most task types (already-scoped, self-evident duration)
should generate zero clarifying questions.

### 5. Task-level refinement UX ("Not quite right? Refine this ->")

- The backend already supports this fully: `/modify-task` regenerates
only that task's milestones (LifeLoad/capacity/other goals untouched),
and this works identically whether the Blueprint is in `"draft"` or
`"committed"` status -- confirmed via code review, no backend change
needed.
- What's missing is discoverability: a small action shown directly on
each milestone, opening the existing edit flow pre-focused on that
task, instead of requiring the user to already know an edit path
exists.
- **Correction needs to be persistent, not one-time.** If a user corrects
an AI-assumed starting point or scope (e.g., "I already know polity,
start with geography instead"), that correction should be stored
against the task (see item 6) so every future week's suggestions for
that same task respect it -- not just the one instance the user
happened to fix. Generalizes beyond any single example: whenever the AI
infers a starting point, level, or scope within a task, the user needs a
fast, specific way to correct it, and that correction must stick going
forward.
- Rejected alternative: regenerating the whole Blueprint repeatedly until
the user is satisfied. Explicitly worse UX -- unpredictable, and the
regenerated version could land on an equally wrong assumption next time.
Direct, targeted correction is the right mechanism, not blind retries.

### 6. Per-task performance & pacing tracking (distinct from whole-user PRF)

- Current PRF is a single, blended trust score across everything a user
is doing -- it cannot distinguish "UPSC prep completion has been
declining 2 weeks running" from "overall the user is doing fine,"
because it averages all tasks together.
- Proposed: a small additional record -- `{task_id, week_number, completion_rate}` -- tracked per task, alongside (not replacing) the
existing whole-user PRF.
- This is also the mechanism that makes multi-week task pacing possible
(see item 7): each week, check whether the previous week's target pace
for that task was actually met, and adjust the next week's target
accordingly (catch up if behind, ease off if consistently ahead) -- the
same "recompute fresh each week from real data" principle the
Deterministic Engine already uses, just pointed at one more number.
- Also stores persistent task-level corrections from item 5 (e.g., a
confirmed starting scope), so they carry forward automatically.

### 7. Long-term task auto-continuation -- rolling plan, not a pre-built schedule

- For tasks spanning multiple weeks (e.g., "Crack UPSC 2027," a ~4-month
task), the task should automatically carry forward into each new week
(with user consent) rather than requiring re-entry -- addressing a real
UX flaw in the current one-task-per-week-only model.
- **Explicit design decision: do NOT pre-compute a full multi-week
schedule up front** (e.g., 16 fixed weekly plans for a 4-month task).
That version is fragile -- it goes stale the moment a disruption or
modification happens partway through, requiring many weeks' worth of
pre-built plan to be reflowed at once.
- **Instead: only ever compute one week's real slice at a time**, using:
(1) total remaining scope within the task's current focus, (2) real
weeks remaining until its deadline, (3) that week's real capacity
(existing Deterministic Engine output), and (4) real pace history from
item 6. This means the existing 3-stage disruption system needs no
changes at all -- it already only ever operates within the current
week, which remains exactly correct under this design.
- **One additional lightweight piece needed:** a simple per-task
"coverage ledger" -- which parts of the task's scope are done, in
progress, or not yet started (e.g., "polity: done, geography: in
progress, economy: not started"). This prevents two different weeks
from accidentally covering the same ground, or the tail end of a
syllabus never being reached. This is not a schedule -- just a checklist
of coverage, close to what the existing `completed` flag on milestones
already does, extended to persist across a task's full multi-week span.
- Depends on: real calendar week tracking (built, above), per-task
performance tracking (item 6), and the coverage ledger described here.

### 8. Behavioral pattern learning loop (disruption timing, suggestion engagement)

- Two schema fields already exist but are never populated or read
anywhere in the codebase, confirmed via direct search:
`DisruptionLog.time_of_day` and `SharedExecutionState.interaction_signals`
(declared as NudgeAgent-owned, zero read/write call sites found).
- Proposed design, consistent with the existing Deterministic-Engine-vs-AI
split: (1) actually populate these fields as real events happen; (2) a
new deterministic aggregation step at week rollover computing real,
counted patterns (e.g., "63% of disruptions cluster Wed-Thu, 4-6pm";
"engages more with fitness Smart Spend links than study ones"); (3) feed
that computed summary into NudgeAgent's prompt as grounded context, same
pattern as Stage 0's existing disruption-history grounding.
- Explicitly distinct from product analytics (PostHog/Mixpanel, already
instrumented) -- that data is for the founder to observe manually; this
section is about DoneHo's own agents automatically adjusting behavior
from it, which is a separate, unbuilt feature.
- Honest scope note: this is the largest of the discussed items, likely
deserving its own dedicated build phase rather than a quick addition.

### 9. Day Output -- show today's slice, not the whole milestone every day

**Current, unaddressed behavior:** `day_output_engine.build_snapshot()`
returns every still-active (not-yet-completed) milestone, in full, every
single day -- regardless of milestone size. A milestone like "Practice 100
mock polity questions" appears identically on Monday and Thursday until
it's ticked complete in one sitting; there's no per-day sub-target.

**Why this is the real, central Day Output problem to fix, and why it
comes last:** genuinely splitting a task's milestone across the days
remaining requires knowing that task's real duration, pacing, and
(for multi-week tasks) its rolling weekly target first -- which is exactly
what items 3, 6, and 7 are meant to produce. Building this before those
would mean guessing at splits with no real basis for the guess.

**Proposed shape once the dependencies above exist:**

- Each milestone gets a computed daily target (e.g., "10 of 100 questions
today"), informed by the task's estimated duration and, for long-running
tasks, that week's rolling slice from item 7 -- not just the weekly
Blueprint cycle in isolation.
- Day Boosters' existing suggestions (e.g., "Beginner-level practice
questions") would feed directly into that day's specific slice, instead
of living as separate, disconnected advisory text the way Nudge output
and Day Output are currently unlinked.
- Depends on: item 3 (duration/pacing reasoning), item 6 (per-task trend,
so pacing can adapt if a task is running behind), and item 7 (rolling
weekly slice for multi-week tasks).

**Scope note:** this is the single biggest remaining piece of the whole
v2 effort -- a real data-model and orchestration change, not a prompt
tweak. Treat it as its own build phase, sequenced after items 3, 6, and 7,
not bundled in casually with smaller fixes.

---

## Business Model Notes (separate from the technical build items above)

Kept distinct from items 1-9 since these are monetization/strategy
decisions, not engineering tasks -- but the stated principle below should
constrain how any future engineering on this is actually built.

### Stated principle, on the record

**User trust comes first. Commission or revenue potential must never
influence which suggestion gets shown or how it's ranked -- only genuine
fit for that user's actual situation decides that.** If a paid option and
an unpaid option are genuinely equally good for the user, which one
earns revenue can break the tie; revenue must never be the reason a worse
option is shown over a better one. This is a direct extension of DoneHo's
existing trust architecture (numbers are never AI-generated, only the
framing is) applied to monetization instead of calculations.

### Revenue direction under consideration

- **Near-term, low-effort:** Smart Spend already generates real,
search-grounded outbound links (Audible, Coursera, Urban Company,
Blinkit, etc.). Appending affiliate/tracking codes to links already
being generated is a near-zero-engineering first step -- an affiliate
commission model (closer to how Amazon Associates works), not a
WhatsApp Business API-style toll, which is a different mechanism and
shouldn't be conflated with it when this gets pitched to anyone.
- **Mid-term:** deeper API integration with partner services (e.g.,
booking directly inside DoneHo rather than linking out) -- a much
larger lift requiring real partnership agreements; realistic only after
meaningful usage scale, not a near-term item.
- **Long-term:** businesses paying for placement/priority in Smart Spend
suggestions -- requires real distribution first; no business pays to
reach an app with a handful of testers. Explicitly a later-stage
direction, not a current plan.
- Honest tension to keep solving deliberately, not gloss over: a
commission-driven Smart Spend creates a real conflict-of-interest risk
against the trust principle above. Any future implementation needs a
concrete mechanism (e.g., ranking logic that is provably blind to
commission except as a tiebreaker) rather than just a stated intention.

---

## Files touched in this v2 work so far

- `persistence.py` -- new file
- `api.py` -- `get_orchestrator()`, all state-changing endpoints
- `core/shared_state.py` -- 2 new fields
- `orchestrator.py` -- `check_calendar_rollover()` (new), `submit_day_output()` (guard added)
- `requirements.txt` -- added `supabase`
