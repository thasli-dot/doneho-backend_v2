"""
DoneHoOrchestrator — wires the Event Bus, Shared Execution State,
Deterministic Engine, and all AI Agents together into the real
end-to-end flow described in Build Brief Section 8 and Section 13,
plus Roadmap Entries 1-3, 5, 7, 8, 9.

Agents never call each other directly; this orchestrator is the only
place that sequences calls, and it does so by publishing/subscribing to
events, not by hardcoding a call chain.
"""

import uuid
import re
from collections import Counter
from datetime import date, datetime
from typing import Optional

from models.schemas import (
    Profile, Goal, Task, Blueprint, ExecutionContract, DisruptionLog,
    DisruptionDirection, GoalCategory, AbsorptionOutcome, RecalibrationProposal,
    RecoveryStep, DisruptionCostEstimate, WeeklyPerformance, AetherTipOutput,
    DailyCheckIn, TaskPerformance, LongTermTaskState, CoverageEntry,
    BehavioralPatternSummary,
)
from core.shared_state import SharedExecutionState
from core.event_bus import (
    EventBus,
    EVENT_GOALS_SUBMITTED, EVENT_TASKS_CLARIFIED, EVENT_BLUEPRINT_REQUESTED,
    EVENT_BLUEPRINT_GENERATED, EVENT_BLUEPRINT_COMMITTED, EVENT_PASS2_SUBMITTED,
    EVENT_DISRUPTION_REPORTED, EVENT_RECALIBRATION_PROPOSED,
    EVENT_RECALIBRATION_APPROVED, EVENT_TASK_OR_GOAL_MODIFIED,
    EVENT_NUDGE_REFRESH_REQUESTED, EVENT_DAY_OUTPUT_SUBMITTED,
    EVENT_LIFE_HAPPENED_REPORTED,
)
from engines.deterministic_engine import DeterministicEngine
from engines.absorption_engine import AbsorptionEngine
from engines.day_output_engine import DayOutputEngine
from engines.recovery_applier import RecoveryApplier
from agents.clarification_agent import run_clarification
from agents.blueprint_agent import run_blueprint
from agents.nudge_agent import run_nudge, run_gain_suggestions
from agents.recalibration_agent import run_cost_estimation, run_hierarchy_decision
from agents.aether_presence_agent import run_aether_tip
from config import DISRUPTION_HISTORY_CONTEXT_LIMIT


class DoneHoOrchestrator:
    def __init__(self, profile: Profile):
        self.state = SharedExecutionState(profile)
        self.bus = EventBus()
        self.deterministic_engine = DeterministicEngine()
        self.absorption_engine = AbsorptionEngine()
        self.day_output_engine = DayOutputEngine()
        self.recovery_applier = RecoveryApplier()
        self._last_nudge_output = None
        self._last_carried_forward: list[str] = []  # item 7 -- set by start_new_week()
        self._register_subscriptions()

    def check_calendar_rollover(self) -> None:
        """
        Real calendar-based week tracking. Called on every request (see
        api.py's get_orchestrator), not just when something happens to
        reload a page. Unlike the old design -- where a new week only
        started if the frontend explicitly called /week/start, which it
        never actually did -- this checks the real current date and
        triggers start_new_week() automatically once 7 real days have
        passed since the week began.
        """
        today = date.today().isoformat()

        if self.state.week_start_date is None:
            # First time this session is seen -- day 1 of week 1 starts now.
            self.state.week_start_date = today
            return

        days_elapsed = (
            date.fromisoformat(today) - date.fromisoformat(self.state.week_start_date)
        ).days

        if days_elapsed >= 7:
            self.start_new_week()
            self.state.week_start_date = today

    def start_new_week(self) -> None:
        """
        Call this at week rollover (before running DeterministicEngine for
        the new week). FIRST records this week's real WeeklyPerformance
        (Entry 2) into ReviewEngine using the Day Output totals
        accumulated all week -- this is the real trigger that was
        previously missing: ReviewEngine.record_week_performance() existed
        but nothing ever called it with real data. THEN resets the
        running Entry-1 counters so the new week starts with a full
        Reserve and no leftover LifeLoad increase. is_first_week is set to
        False here once at least one week's performance has been recorded.
        """
        totals = self.state.day_output_totals
        if totals["total_checked"] > 0:
            completion_rate = round(
                1 - (totals["total_missed"] / totals["total_checked"]), 4
            )
            reserve_usage_ratio = round(
                self.state.reserve_used_this_week / self.state.reserve_hours, 4
            ) if self.state.reserve_hours > 0 else 0.0

            self.deterministic_engine.review_engine.record_week_performance(
                WeeklyPerformance(
                    week_number=self.state.week_number,
                    completion_rate=completion_rate,
                    reserve_usage_ratio=reserve_usage_ratio,
                    caregiving_hours=self.state.last_caregiving_hours,
                )
            )
            self.state.is_first_week = False
            self.state.week_number += 1

            # Item 6 -- per-task completion, distinct from the blended
            # whole-user PRF above. Computed from this week's
            # daily_checkins, mapped back to task_id via the current
            # Blueprint's milestones (title -> task_id lookup).
            if self.state.blueprint is not None:
                title_to_task = {
                    m.title: (m.task_id, m.task_title)
                    for m in self.state.blueprint.milestones
                }
                expected_by_task: dict[str, int] = {}
                missed_by_task: dict[str, int] = {}
                task_title_by_id: dict[str, str] = {}

                for checkin in self.state.daily_checkins:
                    for title in checkin.expected_milestone_titles:
                        match = title_to_task.get(title)
                        if match:
                            task_id, task_title = match
                            expected_by_task[task_id] = expected_by_task.get(task_id, 0) + 1
                            task_title_by_id[task_id] = task_title
                    for title in checkin.missed_milestone_titles:
                        match = title_to_task.get(title)
                        if match:
                            task_id, _ = match
                            missed_by_task[task_id] = missed_by_task.get(task_id, 0) + 1

                for task_id, expected_count in expected_by_task.items():
                    missed_count = missed_by_task.get(task_id, 0)
                    self.state.task_performance_history.append(
                        TaskPerformance(
                            task_id=task_id,
                            task_title=task_title_by_id[task_id],
                            week_number=self.state.week_number,
                            completion_rate=round(1 - (missed_count / expected_count), 4),
                        )
                    )

        # Item 7 -- long-term task coverage ledger + weekly reset.
        # Confirmed design: the weekly goal list resets every week --
        # the user re-enters non-long-term tasks fresh each week -- with
        # ONE exception: fixed-deadline tasks (auto_continue=True)
        # auto-carry forward without re-entry. Runs every rollover,
        # regardless of whether there were any check-ins this week.
        carried_forward_titles: list[str] = []

        for task_id, tracker in list(self.state.long_term_tasks.items()):
            if tracker.is_complete:
                continue

            # Log this week's milestones into the running coverage
            # ledger -- guarded against double-logging (e.g. if
            # /week/start is called more than once for the same
            # week_number, which happened during testing).
            already_logged_this_week = any(
                e.week_number == self.state.week_number
                for e in tracker.coverage_ledger
            )
            if self.state.blueprint is not None and not already_logged_this_week:
                for m in self.state.blueprint.milestones:
                    if m.task_id == task_id:
                        status = (
                            "completed" if m.completed
                            else "deferred" if m.deferred
                            else "accepted_as_lost" if m.accepted_as_lost
                            else "active"
                        )
                        tracker.coverage_ledger.append(
                            CoverageEntry(
                                week_number=self.state.week_number,
                                title=m.title,
                                status=status,
                            )
                        )

            # Past its estimated target week -- stop auto-continuing.
            # (Best-effort estimate from item 3b's duration_hint parsing;
            # not a hard deadline enforcement, just a reasonable cutoff.)
            if tracker.target_week_number is not None and self.state.week_number > tracker.target_week_number:
                tracker.is_complete = True

        # The actual weekly reset -- clears everything, including the
        # now-stale Blueprint (a fresh one gets generated when the user
        # next commits, same "recompute fresh each week" principle the
        # Deterministic Engine already follows).
        self.state.goals = []
        self.state.blueprint = None

        # Re-add only active, auto-continuing long-term tasks -- this is
        # the actual "no re-entry needed" mechanism now that the list
        # above is genuinely empty, not just theoretically.
        for task_id, tracker in self.state.long_term_tasks.items():
            if not tracker.auto_continue or tracker.is_complete:
                continue
            target_goal = next(
                (g for g in self.state.goals if g.id == tracker.goal_id), None
            )
            if target_goal is None:
                target_goal = Goal(
                    id=tracker.goal_id,
                    category=GoalCategory(tracker.goal_category),
                    traffic=0.5,
                    volatility=0.3,
                )
                self.state.goals.append(target_goal)
            target_goal.tasks.append(Task(
                id=task_id,
                title=tracker.task_title,
                clarified=True,  # already clarified in a prior week
                is_flexible=True,
                task_type="fixed_deadline",
            ))
            carried_forward_titles.append(tracker.task_title)

        self._last_carried_forward = carried_forward_titles  # exposed via API, see api.py

        # Item 8, part 2 -- compute this week's behavioral pattern
        # summary from real accumulated history. Runs every rollover,
        # same "always run, not conditional on new check-ins" reasoning
        # as item 7 -- disruption/interaction history can exist even
        # without a Day Output submission that week.
        self._compute_behavioral_patterns()

        self.state.reset_weekly_counters()

    def get_task_performance_trend(self, task_id: str) -> list[TaskPerformance]:
        """
        All recorded weekly completion rates for one task, oldest first.
        Empty list if the task has no recorded weeks yet (e.g. brand new,
        or no week rollover has happened since it was added). Item 7
        (long-term task pacing) will read this to decide whether to ease
        or tighten next week's target for a given task.
        """
        return [
            p for p in self.state.task_performance_history
            if p.task_id == task_id
        ]

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------
    def _register_subscriptions(self) -> None:
        self.bus.subscribe(EVENT_GOALS_SUBMITTED, self._on_goals_submitted)
        self.bus.subscribe(EVENT_TASKS_CLARIFIED, self._on_tasks_clarified)
        self.bus.subscribe(EVENT_BLUEPRINT_REQUESTED, self._on_blueprint_requested)
        self.bus.subscribe(EVENT_BLUEPRINT_GENERATED, self._on_blueprint_generated)
        self.bus.subscribe(EVENT_PASS2_SUBMITTED, self._on_pass2_submitted)
        self.bus.subscribe(EVENT_DISRUPTION_REPORTED, self._on_disruption_reported)
        self.bus.subscribe(EVENT_RECALIBRATION_APPROVED, self._on_recalibration_approved)
        self.bus.subscribe(EVENT_TASK_OR_GOAL_MODIFIED, self._on_task_or_goal_modified)
        self.bus.subscribe(EVENT_NUDGE_REFRESH_REQUESTED, self._on_nudge_refresh_requested)
        self.bus.subscribe(EVENT_LIFE_HAPPENED_REPORTED, self._on_life_happened_reported)

    # ------------------------------------------------------------------
    # Step 1: goals/tasks submitted -> run clarification
    # ------------------------------------------------------------------
    def submit_goals(self, goals: list[Goal]) -> None:
        self.state.goals = goals
        self.bus.publish(EVENT_GOALS_SUBMITTED, {"goals": goals})

    def _on_goals_submitted(self, payload: dict) -> None:
        all_tasks = [t for g in payload["goals"] for t in g.tasks]
        clarification_output = run_clarification(all_tasks)

        flag_by_task_id = {f.task_id: f for f in clarification_output.flags}
        for g in self.state.goals:
            for t in g.tasks:
                flag = flag_by_task_id.get(t.id)
                if flag:
                    t.task_type = flag.task_type
                    t.duration_hint = flag.duration_hint
                    t.options = flag.options
                    # Item 7 -- fixed-deadline tasks are the ones that
                    # genuinely span multiple weeks (e.g. "Crack UPSC
                    # 2027") and should auto-continue without re-entry.
                    if flag.task_type == "fixed_deadline" and t.id not in self.state.long_term_tasks:
                        weeks_remaining = _parse_weeks_remaining(flag.duration_hint)
                        target_week = (
                            self.state.week_number + weeks_remaining
                            if weeks_remaining is not None else None
                        )
                        self.state.long_term_tasks[t.id] = LongTermTaskState(
                            task_id=t.id,
                            task_title=t.title,
                            goal_id=g.id,
                            goal_category=g.category.value,
                            target_week_number=target_week,
                        )
                if flag and flag.is_ambiguous:
                    t.clarified = False
                    t.clarification_note = flag.question  # pending answer
                else:
                    t.clarified = True

        self.bus.publish(EVENT_TASKS_CLARIFIED, {"clarification": clarification_output})

    def answer_clarifications(self, answers: dict[str, str]) -> None:
        """answers: {task_id: user's free-text answer}"""
        for g in self.state.goals:
            for t in g.tasks:
                if t.id in answers:
                    t.clarification_note = answers[t.id]
                    t.clarified = True
        self.bus.publish(EVENT_BLUEPRINT_REQUESTED, {})

    def _on_tasks_clarified(self, payload: dict) -> None:
        # If nothing was flagged as ambiguous, skip straight to Blueprint
        # request with no visible delay (Lovable Section 3b requirement).
        if all(t.clarified for g in self.state.goals for t in g.tasks):
            self.bus.publish(EVENT_BLUEPRINT_REQUESTED, {})

    # ------------------------------------------------------------------
    # Step 2: Blueprint request -> DeterministicEngine first, then BlueprintAgent
    # ------------------------------------------------------------------
    def _on_blueprint_requested(self, payload: dict) -> None:
        det_result = self.deterministic_engine.run(
            profile=self.state.profile,
            goals=self.state.goals,
            is_first_week=self.state.is_first_week,
        )
        self.state.apply_deterministic_result(det_result)

        blueprint_output = run_blueprint(self.state.goals, det_result["hours_by_goal"])

        blueprint = Blueprint(
            weekly_commitment_hours=det_result["recommended_commitment"],
            milestones=blueprint_output.milestones,
            reserve_hours=self.state.reserve_hours,
            lifeload=self.state.lifeload,
            planning_confidence=self.state.planning_confidence,
            status="draft",
        )
        self.state.write("BlueprintAgent", "blueprint", blueprint)
        self.bus.publish(EVENT_BLUEPRINT_GENERATED, {"blueprint": blueprint})

    def _on_blueprint_generated(self, payload: dict) -> None:
        # Hook for UI-layer notification; no further backend action needed
        # until the user commits or submits Pass 2 context.
        pass

    # ------------------------------------------------------------------
    # Step 3: Pass 2 tick-list -> recompute with real context, regenerate
    # ------------------------------------------------------------------
    def submit_pass2(
        self,
        caregiving_hours: float = 0.0,
        planned_event_hours: float = 0.0,
        other_time_constraint_hours: float = 0.0,
        sleep_hours_override: float | None = None,
        commute_hours_override: float | None = None,
        work_hours_override: float | None = None,
    ) -> None:
        self.bus.publish(EVENT_PASS2_SUBMITTED, {
            "caregiving_hours": caregiving_hours,
            "planned_event_hours": planned_event_hours,
            "other_time_constraint_hours": other_time_constraint_hours,
            "sleep_hours_override": sleep_hours_override,
            "commute_hours_override": commute_hours_override,
            "work_hours_override": work_hours_override,
        })

    def _on_pass2_submitted(self, payload: dict) -> None:
        # Entry 7 support: remember caregiving hours for week-end PRF record.
        self.state.last_caregiving_hours = payload.get("caregiving_hours", 0.0)

        det_result = self.deterministic_engine.run(
            profile=self.state.profile,
            goals=self.state.goals,
            is_first_week=self.state.is_first_week,
            sleep_hours_override=payload.get("sleep_hours_override"),
            commute_hours_override=payload.get("commute_hours_override"),
            work_hours_override=payload.get("work_hours_override"),
            caregiving_hours=payload.get("caregiving_hours", 0.0),
            planned_event_hours=payload.get("planned_event_hours", 0.0),
            other_time_constraint_hours=payload.get("other_time_constraint_hours", 0.0),
        )
        self.state.apply_deterministic_result(det_result)

        blueprint_output = run_blueprint(self.state.goals, det_result["hours_by_goal"])
        blueprint = Blueprint(
            weekly_commitment_hours=det_result["recommended_commitment"],
            milestones=blueprint_output.milestones,
            reserve_hours=self.state.reserve_hours,
            lifeload=self.state.lifeload,
            planning_confidence=self.state.planning_confidence,
            status="draft",
        )
        self.state.write("BlueprintAgent", "blueprint", blueprint)
        self.bus.publish(EVENT_BLUEPRINT_GENERATED, {"blueprint": blueprint})

    # ------------------------------------------------------------------
    # Commit + Nudge
    # ------------------------------------------------------------------
    def commit_blueprint(self) -> None:
        if self.state.blueprint is None:
            raise ValueError("No blueprint to commit.")
        self.state.blueprint.status = "committed"
        self.bus.publish(EVENT_BLUEPRINT_COMMITTED, {})
        self.bus.publish(EVENT_NUDGE_REFRESH_REQUESTED, {})

    def _on_nudge_refresh_requested(self, payload: dict) -> None:
        nudge_output = run_nudge(
            self.state.goals, self.state.blueprint, previous_output=None,
            behavioral_context=self._get_behavioral_context_text(),
        )
        self._last_nudge_output = nudge_output
        self.state.write("NudgeAgent", "suggestions", {
            "opportunity_map": nudge_output.opportunity_map,
            "day_boosters": nudge_output.day_boosters,
            "smart_spend": nudge_output.smart_spend,
        })

    def regenerate_suggestions(self) -> None:
        nudge_output = run_nudge(
            self.state.goals, self.state.blueprint, previous_output=self._last_nudge_output,
            behavioral_context=self._get_behavioral_context_text(),
        )
        self._last_nudge_output = nudge_output
        self.state.write("NudgeAgent", "suggestions", {
            "opportunity_map": nudge_output.opportunity_map,
            "day_boosters": nudge_output.day_boosters,
            "smart_spend": nudge_output.smart_spend,
        })

    # ------------------------------------------------------------------
    # Entry 5 — Proactive Aether Presence
    # ------------------------------------------------------------------
    def request_aether_tip(self) -> AetherTipOutput:
        """Callable anytime the frontend wants a fresh, state-aware Aether
        line (Dashboard load, after committing a Blueprint, etc). Uses
        Profile.location only if the user has set one -- never silent,
        disclosed once during onboarding per Section 12.3."""
        disruptions_this_week = len([
            d for d in self.state.disruption_log
            if d.direction == DisruptionDirection.LOSS
        ])
        return run_aether_tip(
            goals=self.state.goals,
            blueprint=self.state.blueprint,
            lifeload=self.state.effective_lifeload,
            disruptions_this_week=disruptions_this_week,
            location=self.state.profile.location,
        )

    # ------------------------------------------------------------------
    # Entry 2 — Day Output
    # ------------------------------------------------------------------
    def get_day_output_checklist(self) -> list[dict]:
        """Today's pre-ticked checklist -- every still-active milestone,
        each with a computed today_target_hours/today_target_quantity
        (item 9), pacing-adjusted using item 6's real per-task trend,
        and linked to a Day Booster suggestion when one genuinely
        references that task."""
        if self.state.blueprint is None:
            return []
        days_remaining = 7
        if self.state.week_start_date is not None:
            elapsed = (date.today() - date.fromisoformat(self.state.week_start_date)).days
            days_remaining = max(7 - elapsed, 1)

        # Real per-task trend -- most recent recorded completion_rate per
        # task_id appearing in this week's milestones. Tasks with no
        # history yet simply get no adjustment (trend_factor stays 1.0).
        task_ids = {m.task_id for m in self.state.blueprint.milestones}
        task_trends = {}
        for task_id in task_ids:
            history = self.get_task_performance_trend(task_id)
            if history:
                task_trends[task_id] = history[-1].completion_rate

        day_boosters = self.state.suggestions.get("day_boosters", [])

        return self.day_output_engine.build_snapshot(
            self.state.blueprint, days_remaining, task_trends, day_boosters
        )

    def submit_day_output(self, day_label: str, unticked_indices: set[int]) -> dict:
        """
        The user's evening submission: everything pre-ticked EXCEPT
        whatever indices they unticked (i.e. actually missed). Marks
        completed milestones on the Blueprint, accumulates this week's
        running totals for PRF (Entry 2), and flags whether Life Happened
        should be proactively offered (Entry 8 hook, >50% missed).

        Real calendar guard: only one submission per real day is allowed
        -- reopening the app later the same day, or refreshing the page,
        no longer lets a second submission through for that same date.
        """
        today = date.today().isoformat()
        if self.state.last_day_submitted_date == today:
            raise ValueError("Day already submitted today. Come back tomorrow.")

        if self.state.blueprint is None:
            raise ValueError("No committed blueprint yet -- nothing to check in against.")

        snapshot = self.day_output_engine.build_snapshot(self.state.blueprint)
        expected_titles = [item["title"] for item in snapshot]
        missed_titles = [item["title"] for item in snapshot if item["index"] in unticked_indices]

        result = self.day_output_engine.apply_submission(self.state.blueprint, unticked_indices)
        # Milestones were mutated in place (completed=True where kept) --
        # write the same object back through the ownership gate so this
        # stays consistent with the one-writer rule, not a silent bypass.
        self.state.write("BlueprintAgent", "blueprint", self.state.blueprint)

        self.state.day_output_totals["total_checked"] += result["total_checked"]
        self.state.day_output_totals["total_missed"] += result["missed_count"]

        checkin = DailyCheckIn(
            day_label=day_label,
            expected_milestone_titles=expected_titles,
            missed_milestone_titles=missed_titles,
            completion_rate=round(1 - result["miss_ratio"], 4),
            life_happened_suggested=result["should_offer_life_happened"],
        )
        self.state.daily_checkins.append(checkin)
        self.state.last_day_submitted_date = today

        self.bus.publish(EVENT_DAY_OUTPUT_SUBMITTED, {"checkin": checkin, "result": result})
        return {
            "should_offer_life_happened": result["should_offer_life_happened"],
            "missed_hours": result["missed_hours"],
            "missed_titles": missed_titles,
        }

    # ------------------------------------------------------------------
    # Entry 8 — "Life Happened" (no-reason-needed trigger)
    # ------------------------------------------------------------------
    def trigger_life_happened(self) -> None:
        """
        A day just didn't work, no specific reason needed. Unlike the
        text-based disruption box, cost is NOT estimated by an LLM
        (Stage 0) -- it's computed directly and deterministically from
        today's actual missed milestones (the most recent Day Output
        submission), then fed into the SAME Stage 1-3 absorption flow
        as any other loss disruption.
        """
        if not self.state.daily_checkins:
            raise ValueError("No Day Output submitted yet today -- nothing to base Life Happened on.")

        today = self.state.daily_checkins[-1]
        if self.state.blueprint is None:
            raise ValueError("No blueprint to reference.")

        missed_hours = sum(
            m.expected_hours for m in self.state.blueprint.milestones
            if m.title in today.missed_milestone_titles
        )

        # Synthetic cost estimate -- no LLM call, deterministic (Section 8
        # Item 2's "reasoned, not assumed" rule applies to TEXT-described
        # disruptions; here the cost is already known exactly).
        cost_estimate = DisruptionCostEstimate(
            estimated_hours_lost=round(missed_hours, 2),
            affected_goal_id=None,
            affected_task_id=None,
            reasoning_note="Computed directly from today's missed Day Output items -- no estimation needed.",
        )
        self.bus.publish(EVENT_LIFE_HAPPENED_REPORTED, {"cost_estimate": cost_estimate})

    def _on_life_happened_reported(self, payload: dict) -> None:
        self._run_absorption_stages_1_to_3("Life happened today.", payload["cost_estimate"])

    # ------------------------------------------------------------------
    # Disruption -> staged LOSS flow (Entry 1, Entry 7) or GAIN flow (Entry 9)
    # ------------------------------------------------------------------
    def report_disruption(self, description: str, direction: DisruptionDirection = DisruptionDirection.LOSS) -> None:
        self.bus.publish(EVENT_DISRUPTION_REPORTED, {"description": description, "direction": direction})

    def _on_disruption_reported(self, payload: dict) -> None:
        if payload["direction"] == DisruptionDirection.GAIN:
            self._handle_gain_disruption(payload["description"])
        else:
            self._handle_loss_disruption(payload["description"])

    def _build_historical_context(self) -> str | None:
        """
        Entry 7 -- per-user pattern learning. Deterministically (no LLM)
        summarizes this user's most recent past LOSS disruptions from
        disruption_log, to hand to Stage 0 as real prior context. The
        LLM does the actual pattern-reasoning (weighing similarity,
        adjusting the estimate) -- this just retrieves and formats.
        """
        loss_entries = [d for d in self.state.disruption_log if d.direction == DisruptionDirection.LOSS]
        if not loss_entries:
            return None
        recent = loss_entries[-DISRUPTION_HISTORY_CONTEXT_LIMIT:]
        lines = ["This user's past disruptions (most recent last):"]
        for d in recent:
            lines.append(f'  - "{d.description}" -> {d.estimated_hours} hours lost')
        return "\n".join(lines)

    # --- LOSS path: Stage 0 (LLM) -> shared Stages 1-3 ---
    def _handle_loss_disruption(self, description: str) -> None:
        # Stage 0 — cost estimation. Always one LLM call, regardless of
        # which stage eventually resolves the disruption. Entry 7: now
        # includes this user's real disruption history as context.
        historical_context = self._build_historical_context()
        cost_estimate = run_cost_estimation(description, self.state.goals, historical_context)
        self._run_absorption_stages_1_to_3(description, cost_estimate)

    def _run_absorption_stages_1_to_3(self, description: str, cost_estimate: DisruptionCostEstimate) -> None:
        """
        Shared by both entry points into the loss-absorption flow: the
        normal text-described disruption (_handle_loss_disruption, after
        its own Stage 0 LLM call) and Life Happened (trigger_life_happened,
        which skips Stage 0 entirely since its cost is already known).
        Keeping this logic in exactly one place is what Roadmap Entry 8
        explicitly calls for: "Both this and the disruption box converge
        into the same Recalibration Agent logic."
        """
        hours_lost = cost_estimate.estimated_hours_lost

        # Stage 1 — silent absorption (pure math, no LLM).
        stage1 = self.absorption_engine.try_silent_absorption(
            estimated_hours_lost=hours_lost,
            remaining_reserve=self.state.remaining_reserve,
        )
        if stage1 is not None:
            self._resolve_stage1(description, cost_estimate, stage1)
            return

        # Stage 2 — LifeLoad renegotiation (pure math, no LLM).
        shortfall = round(hours_lost - self.state.remaining_reserve, 2)
        stage2 = self.absorption_engine.try_lifeload_renegotiation(
            shortfall_hours=shortfall,
            current_effective_lifeload=self.state.effective_lifeload,
            weekly_capacity=self.state.weekly_capacity,
            increase_points_used_this_week=self.state.lifeload_renegotiated_increase,
        )
        if stage2 is not None:
            self._propose_stage2(description, cost_estimate, stage2)
            return

        # Stage 3 — fall back to the ordered Recovery Hierarchy. Branch by
        # task flexibility. Unknown/unmatched task defaults to flexible.
        task = self._find_task(cost_estimate.affected_task_id)
        is_flexible = task.is_flexible if task is not None else True

        if is_flexible:
            self._propose_stage3_hierarchy(description, cost_estimate)
        else:
            self._propose_stage3_accepted_as_lost(description, cost_estimate)

    def _find_task(self, task_id: str | None) -> Task | None:
        if task_id is None:
            return None
        for g in self.state.goals:
            for t in g.tasks:
                if t.id == task_id:
                    return t
        return None

    def _resolve_stage1(self, description: str, cost_estimate, stage1_result: dict) -> None:
        """Stage 1 auto-resolves: no approval needed, applied immediately."""
        self.state.write(
            "RecalibrationAgent", "reserve_used_this_week",
            round(self.state.reserve_used_this_week + stage1_result["absorbed_hours"], 2),
        )

        proposal = RecalibrationProposal(
            outcome=AbsorptionOutcome.SILENT_ABSORPTION,
            estimated_hours_lost=cost_estimate.estimated_hours_lost,
            affected_goal_id=cost_estimate.affected_goal_id,
            affected_task_id=cost_estimate.affected_task_id,
            requires_approval=False,
            message="That's covered — your plan stays on track.",
        )
        self.state.write("RecalibrationAgent", "last_disruption_outcome", proposal)
        self._log_disruption(description, cost_estimate, AbsorptionOutcome.SILENT_ABSORPTION)

        self.bus.publish(EVENT_RECALIBRATION_PROPOSED, {"proposal": proposal, "auto_resolved": True})

    def _propose_stage2(self, description: str, cost_estimate, stage2_result: dict) -> None:
        contract = ExecutionContract(
            original_blueprint_status=self.state.blueprint.status if self.state.blueprint else "unknown",
            outcome=AbsorptionOutcome.LIFELOAD_RENEGOTIATION,
            lifeload_increase_points=stage2_result["lifeload_increase_points"],
            estimated_hours_lost=cost_estimate.estimated_hours_lost,
            affected_goal_id=cost_estimate.affected_goal_id,
            affected_task_id=cost_estimate.affected_task_id,
            approved=False,
            notes="This week's a bit fuller than planned — okay if we stretch capacity slightly to keep everything on track?",
        )
        self._stage_pending_contract(description, cost_estimate, contract, AbsorptionOutcome.LIFELOAD_RENEGOTIATION)

    def _propose_stage3_hierarchy(self, description: str, cost_estimate) -> None:
        hierarchy_result = run_hierarchy_decision(description, cost_estimate.estimated_hours_lost, self.state.goals)
        contract = ExecutionContract(
            original_blueprint_status=self.state.blueprint.status if self.state.blueprint else "unknown",
            outcome=AbsorptionOutcome.HIERARCHY_RECOVERY,
            recovery_step=hierarchy_result.chosen_step,
            estimated_hours_lost=cost_estimate.estimated_hours_lost,
            affected_goal_id=hierarchy_result.affected_goal_id or cost_estimate.affected_goal_id,
            affected_task_id=hierarchy_result.affected_task_id or cost_estimate.affected_task_id,
            approved=False,
            notes=hierarchy_result.proposal_message,
        )
        self._stage_pending_contract(
            description, cost_estimate, contract, AbsorptionOutcome.HIERARCHY_RECOVERY,
            needs_tiebreak_input=hierarchy_result.needs_tiebreak_input,
        )

    def _propose_stage3_accepted_as_lost(self, description: str, cost_estimate) -> None:
        """Rigid, time-locked task — fully deterministic, no LLM call.
        There's no judgment call left once we know it can't be rescheduled."""
        contract = ExecutionContract(
            original_blueprint_status=self.state.blueprint.status if self.state.blueprint else "unknown",
            outcome=AbsorptionOutcome.ACCEPTED_AS_LOST,
            estimated_hours_lost=cost_estimate.estimated_hours_lost,
            affected_goal_id=cost_estimate.affected_goal_id,
            affected_task_id=cost_estimate.affected_task_id,
            approved=False,
            notes="That one's not moveable, so we'll let it go for today — everything else stays on track.",
        )
        self._stage_pending_contract(description, cost_estimate, contract, AbsorptionOutcome.ACCEPTED_AS_LOST)

    def _stage_pending_contract(
        self, description: str, cost_estimate, contract: ExecutionContract,
        outcome: AbsorptionOutcome, needs_tiebreak_input: bool = False,
    ) -> None:
        self.state.write("RecalibrationAgent", "execution_contract", contract)

        proposal = RecalibrationProposal(
            outcome=outcome,
            estimated_hours_lost=cost_estimate.estimated_hours_lost,
            lifeload_increase_points=contract.lifeload_increase_points,
            chosen_step=contract.recovery_step,
            affected_goal_id=contract.affected_goal_id,
            affected_task_id=contract.affected_task_id,
            needs_tiebreak_input=needs_tiebreak_input,
            requires_approval=True,
            message=contract.notes,
        )
        self.state.write("RecalibrationAgent", "last_disruption_outcome", proposal)
        self._log_disruption(description, cost_estimate, outcome)

        self.bus.publish(EVENT_RECALIBRATION_PROPOSED, {"proposal": proposal, "contract": contract, "auto_resolved": False})

    def log_interaction(self, suggestion_type: str, suggestion_title: str, action: str) -> None:
        """
        Item 8 -- records a real user interaction with a Nudge suggestion
        (clicked, dismissed, completed), so future weeks can learn what
        kind of suggestions this user actually engages with. This is raw
        capture -- see _compute_behavioral_patterns() for the
        aggregation step that turns this into NudgeAgent-usable pattern
        data.
        """
        self.state.interaction_signals.append({
            "suggestion_type": suggestion_type,   # "opportunity_map" | "day_boosters" | "smart_spend"
            "suggestion_title": suggestion_title,
            "action": action,                      # "clicked" | "dismissed" | "completed"
            "timestamp": datetime.utcnow().isoformat(),
        })

    def _compute_behavioral_patterns(self) -> None:
        """
        Item 8, part 2 -- deterministic aggregation of real behavioral
        signals into counted patterns. Pure counting, no LLM involved --
        the same Deterministic-Engine-vs-AI split used everywhere else
        in DoneHo. A "pattern" is only reported if seen at least twice;
        a single data point is not a pattern, and this never guesses to
        fill a gap. Called at every week rollover.
        """
        time_slots = [
            f"{d.day_of_week} {d.time_of_day}"
            for d in self.state.disruption_log
            if d.day_of_week and d.day_of_week != "unspecified" and d.time_of_day
        ]
        top_disruption_times = [
            slot for slot, count in Counter(time_slots).most_common(2)
            if count >= 2
        ]

        clicked_types = [
            s["suggestion_type"] for s in self.state.interaction_signals
            if s.get("action") == "clicked"
        ]
        type_counts = Counter(clicked_types).most_common(1)
        most_engaged_type = (
            type_counts[0][0] if type_counts and type_counts[0][1] >= 2 else None
        )

        self.state.behavioral_pattern_history.append(
            BehavioralPatternSummary(
                week_number=self.state.week_number,
                top_disruption_times=top_disruption_times,
                most_engaged_suggestion_type=most_engaged_type,
                total_disruptions_analyzed=len(self.state.disruption_log),
                total_interactions_analyzed=len(self.state.interaction_signals),
            )
        )

    def _get_behavioral_context_text(self) -> Optional[str]:
        """
        Item 8, part 3 -- formats the latest computed pattern summary
        into grounded text for NudgeAgent's prompt, same role as Stage
        0's disruption-history grounding. Returns None if there's no
        summary yet, or nothing meaningful was found this week --
        NudgeAgent should never be told about a "pattern" that's really
        just 1-2 data points.
        """
        if not self.state.behavioral_pattern_history:
            return None
        latest = self.state.behavioral_pattern_history[-1]
        if not latest.top_disruption_times and not latest.most_engaged_suggestion_type:
            return None

        lines = ["REAL BEHAVIORAL PATTERNS FOR THIS USER (from counted history, not a guess):"]
        if latest.top_disruption_times:
            lines.append(f"- Disruptions cluster around: {', '.join(latest.top_disruption_times)}")
        if latest.most_engaged_suggestion_type:
            lines.append(f"- Engages most with: {latest.most_engaged_suggestion_type} suggestions")
        return "\n".join(lines)

    def _log_disruption(self, description: str, cost_estimate, outcome: AbsorptionOutcome) -> None:
        now = datetime.utcnow()
        self.state.disruption_log.append(DisruptionLog(
            description=description,
            direction=DisruptionDirection.LOSS,
            estimated_hours=cost_estimate.estimated_hours_lost,
            day_of_week=now.strftime("%A"),
            time_of_day=now.strftime("%H:00"),
            resolution_stage=outcome,
        ))

    def approve_recalibration(self) -> None:
        """
        All Execution Contract modifications require explicit user
        approval before being committed (Section 6) — this is that gate.
        Only reachable for Stage 2 / Stage 3 outcomes; Stage 1 never
        creates a pending contract in the first place.

        Entry 3 fix: HIERARCHY_RECOVERY and ACCEPTED_AS_LOST now actually
        MUTATE the Blueprint via RecoveryApplier -- previously the
        recovery step was decided but never applied anywhere.
        """
        if self.state.execution_contract is None:
            raise ValueError("No pending recalibration proposal to approve.")

        contract = self.state.execution_contract
        contract.approved = True

        if contract.outcome == AbsorptionOutcome.LIFELOAD_RENEGOTIATION:
            self.state.write(
                "RecalibrationAgent", "reserve_used_this_week", self.state.reserve_hours,
            )
            self.state.write(
                "RecalibrationAgent", "lifeload_renegotiated_increase",
                round(self.state.lifeload_renegotiated_increase + contract.lifeload_increase_points, 2),
            )
        elif contract.outcome in (AbsorptionOutcome.HIERARCHY_RECOVERY, AbsorptionOutcome.ACCEPTED_AS_LOST):
            if self.state.blueprint is not None:
                updated_blueprint = self.recovery_applier.apply(self.state.blueprint, contract, self.state.goals)
                self.state.write("BlueprintAgent", "blueprint", updated_blueprint)

        self.bus.publish(EVENT_RECALIBRATION_APPROVED, {})

    def _on_recalibration_approved(self, payload: dict) -> None:
        # Auto-cascade: Nudge Agent re-runs against the updated Blueprint
        # (Section 7, Item 4 / Roadmap Entry 3).
        self.bus.publish(EVENT_NUDGE_REFRESH_REQUESTED, {})

    # --- GAIN path (Entry 9): single call, routed to Nudge Agent ---
    def _handle_gain_disruption(self, description: str) -> None:
        if self.state.blueprint is None:
            raise ValueError("No committed blueprint yet — cannot generate gain suggestions.")

        gain_output = run_gain_suggestions(description, self.state.goals, self.state.blueprint)
        self.state.write("NudgeAgent", "last_gain_suggestions", gain_output)

        now = datetime.utcnow()
        self.state.disruption_log.append(DisruptionLog(
            description=description,
            direction=DisruptionDirection.GAIN,
            estimated_hours=gain_output.estimated_free_hours,
            day_of_week=now.strftime("%A"),
            time_of_day=now.strftime("%H:00"),
            resolution_stage=None,  # gain path has no absorption stage
        ))

        # No approval gate — these are suggestions, not a plan mutation.
        self.bus.publish(EVENT_RECALIBRATION_PROPOSED, {"gain_output": gain_output, "auto_resolved": True})

    # ------------------------------------------------------------------
    # Modify goals/tasks mid-week
    # ------------------------------------------------------------------
    def modify_task(self, goal_id: str, task: Task, action: str) -> None:
        """action: 'add' | 'edit' | 'remove'. Task-only changes never move
        LifeLoad — only that goal's milestones regenerate (Section 6)."""
        self._apply_task_change(goal_id, task, action)
        self.bus.publish(EVENT_TASK_OR_GOAL_MODIFIED, {"scope": "task", "goal_id": goal_id})

    def modify_goal(self, goal: Goal, action: str) -> None:
        """action: 'add' | 'remove'. Goal changes DO recalculate LifeLoad
        and regenerate the full Blueprint (Section 6)."""
        if action == "add":
            self.state.goals.append(goal)
        elif action == "remove":
            self.state.goals = [g for g in self.state.goals if g.id != goal.id]
        self.bus.publish(EVENT_TASK_OR_GOAL_MODIFIED, {"scope": "goal", "goal_id": goal.id})

    def _apply_task_change(self, goal_id: str, task: Task, action: str) -> None:
        for g in self.state.goals:
            if g.id != goal_id:
                continue
            if action == "add":
                g.tasks.append(task)
            elif action == "edit":
                g.tasks = [task if t.id == task.id else t for t in g.tasks]
            elif action == "remove":
                g.tasks = [t for t in g.tasks if t.id != task.id]

    def _on_task_or_goal_modified(self, payload: dict) -> None:
        if payload["scope"] == "goal":
            # Full recalculation + full Blueprint regeneration.
            self.bus.publish(EVENT_BLUEPRINT_REQUESTED, {})
        else:
            # Task-only: regenerate milestones without touching LifeLoad/
            # capacity/commitment — re-run BlueprintAgent directly, skip
            # DeterministicEngine.
            hours_by_goal = self.deterministic_engine.lifeload_engine.allocate_hours_by_focus(
                self.state.goals, self.state.commitment_contract.get("recommended_commitment", 0)
            )
            blueprint_output = run_blueprint(self.state.goals, hours_by_goal)
            updated = self.state.blueprint
            updated.milestones = blueprint_output.milestones
            self.state.write("BlueprintAgent", "blueprint", updated)
            self.bus.publish(EVENT_BLUEPRINT_GENERATED, {"blueprint": updated})


def new_goal_id() -> str:
    return str(uuid.uuid4())[:8]


def new_task_id() -> str:
    return str(uuid.uuid4())[:8]


def _parse_weeks_remaining(duration_hint: Optional[str]) -> Optional[int]:
    """
    Item 7 -- best-effort extraction of a weeks-remaining estimate from a
    free-text duration_hint (e.g. "~11 months until exam"). Deliberately
    simple: looks for a number next to "week"/"month" and converts.
    Returns None if it can't confidently parse one -- callers must treat
    that as "open-ended," never guess a number instead.
    """
    if not duration_hint:
        return None
    match = re.search(r"(\d+)\s*(week|month)", duration_hint, re.IGNORECASE)
    if not match:
        return None
    count = int(match.group(1))
    unit = match.group(2).lower()
    return round(count * 4.33) if unit.startswith("month") else count