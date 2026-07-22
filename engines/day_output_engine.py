"""
DayOutputEngine — pure math, zero LLM calls (Proposed Enhancements, Entry 2).

Implements the evening-triggered, pre-ticked checklist described in the
build brief: every active (not yet completed/deferred/accepted-as-lost)
milestone in the committed Blueprint defaults to "done"; the user unticks
whatever was actually missed. This is the real data source that feeds
ReviewEngine.record_week_performance() at week rollover — previously that
method existed but had no genuine trigger anywhere in the system.

Milestones are addressed by their POSITION (index) in blueprint.milestones
for this submission — deliberately avoids adding an `id` field to
Milestone, since Milestone is also BlueprintAgent's output_schema target
and any field the LLM has to additionally populate is a new way for
generation to drift. Index-based addressing needs zero schema risk.

Item 9 (fuller version) additions:
- `today_target_quantity`/`target_display`: when BlueprintAgent set a
  real `quantity`/`unit` on a milestone (e.g. 100 "questions"), compute
  a genuine countable daily slice ("10 of 100 questions today") instead
  of only a time-based one. Falls back to hours when quantity/unit are
  null -- never invents a count DayOutputEngine itself didn't get from
  the agent.
- Trend-aware pacing: uses item 6's real per-task completion history
  (passed in as `task_trends`) to modestly adjust today's target if a
  task has been running behind or consistently ahead -- bounded,
  deterministic, never a guess.
- `boost_tip`: links a real Day Booster suggestion to a milestone when
  the suggestion's own text references that task, so Nudge output and
  Day Output are no longer fully disconnected systems.
"""

from config import LIFE_HAPPENED_MISS_RATIO_THRESHOLD

# Bounded, deterministic pacing adjustment factors -- see build_snapshot.
BEHIND_PACE_THRESHOLD = 0.7
BEHIND_PACE_FACTOR = 1.15   # modest catch-up, never more than +15%
AHEAD_PACE_THRESHOLD = 0.95
AHEAD_PACE_FACTOR = 0.95    # small ease-off, never more than -5%


class DayOutputEngine:
    def build_snapshot(
        self,
        blueprint,
        days_remaining_in_week: int = 7,
        task_trends: dict | None = None,
        day_boosters: list | None = None,
    ) -> list[dict]:
        """
        Returns today's checklist: every milestone still active (not
        already completed, not deferred to next week, not accepted-as-
        lost), pre-ticked as done by default.

        days_remaining_in_week: real days left in the current week
        (including today) -- never less than 1.
        task_trends: {task_id: most_recent_completion_rate}, from item 6's
        get_task_performance_trend() -- optional, adjusts pacing when
        present, has no effect on tasks with no history yet.
        day_boosters: the current week's Day Booster suggestions (from
        NudgeAgent) -- optional, used only to attach a matching tip when
        a suggestion's own text references this milestone's task.
        """
        safe_days = max(int(days_remaining_in_week), 1)
        task_trends = task_trends or {}
        day_boosters = day_boosters or []

        items = []
        for i, m in enumerate(blueprint.milestones):
            if m.completed or m.deferred or m.accepted_as_lost:
                continue

            # Trend-aware pacing -- bounded, deterministic, only applied
            # when real history exists for this specific task.
            trend_factor = 1.0
            recent_rate = task_trends.get(m.task_id)
            if recent_rate is not None:
                if recent_rate < BEHIND_PACE_THRESHOLD:
                    trend_factor = BEHIND_PACE_FACTOR
                elif recent_rate >= AHEAD_PACE_THRESHOLD:
                    trend_factor = AHEAD_PACE_FACTOR

            today_target_hours = round((m.expected_hours * trend_factor) / safe_days, 2)

            today_target_quantity = None
            target_display = None
            if m.quantity is not None and m.unit:
                today_target_quantity = max(1, round((m.quantity * trend_factor) / safe_days))
                target_display = f"{today_target_quantity} of {int(m.quantity)} {m.unit} today"

            # Day Booster linking -- keyword-overlap match, not an exact
            # substring match. Boosters paraphrase a milestone's wording
            # rather than quoting it verbatim (e.g. a milestone titled
            # "Deep dive concept study for 15 challenging questions" gets
            # referenced as "...challenging concepts... from your 'Deep
            # dive concept study' list" -- overlapping significant words,
            # not an exact substring either direction). A small, common
            # stopword list is excluded so short common words don't
            # produce false-positive matches.
            _STOPWORDS = {
                "the", "a", "an", "for", "of", "to", "and", "or", "in",
                "on", "at", "your", "this", "that", "with", "from",
            }

            def _keywords(text: str) -> set:
                words = "".join(c if c.isalnum() else " " for c in text.lower()).split()
                return {w for w in words if len(w) > 2 and w not in _STOPWORDS and not w.isdigit()}

            boost_tip = None
            milestone_keywords = _keywords(m.title)
            for b in day_boosters:
                b_title = getattr(b, "title", None) or (b.get("title") if isinstance(b, dict) else None) or ""
                b_desc = getattr(b, "description", None) or (b.get("description") if isinstance(b, dict) else None) or ""
                booster_keywords = _keywords(f"{b_title} {b_desc}")
                overlap = milestone_keywords & booster_keywords
                # Require at least 2 real shared significant words -- a
                # single shared common-ish word isn't enough evidence of
                # a genuine reference, but 2+ specific words together is
                # a real, honest signal.
                if len(overlap) >= 2:
                    boost_tip = {"title": b_title, "description": b_desc}
                    break

            items.append({
                "index": i,
                "title": m.title,
                "goal_title": m.goal_title,
                "task_title": m.task_title,
                "expected_hours": m.expected_hours,
                "today_target_hours": today_target_hours,
                "quantity": m.quantity,
                "unit": m.unit,
                "today_target_quantity": today_target_quantity,
                "target_display": target_display,
                "pacing_adjusted": trend_factor != 1.0,
                "boost_tip": boost_tip,
                "ticked": True,
            })
        return items

    def apply_submission(self, blueprint, unticked_indices: set[int]) -> dict:
        """
        Applies the user's submission: any active milestone NOT in
        unticked_indices gets marked completed=True (mutates blueprint
        milestones in place — caller is responsible for re-writing
        blueprint to Shared State under the BlueprintAgent ownership tag,
        since Day Output is a deterministic sub-process of Blueprint
        completion tracking, not a competing writer).

        Returns a summary used both for immediate UI feedback and to
        accumulate this week's running totals for PRF calculation.
        """
        total_active = 0
        missed_count = 0
        missed_hours = 0.0

        for i, m in enumerate(blueprint.milestones):
            if m.completed or m.deferred or m.accepted_as_lost:
                continue
            total_active += 1
            if i in unticked_indices:
                missed_count += 1
                missed_hours += m.expected_hours
            else:
                m.completed = True

        miss_ratio = round(missed_count / total_active, 2) if total_active > 0 else 0.0

        return {
            "total_checked": total_active,
            "missed_count": missed_count,
            "missed_hours": round(missed_hours, 2),
            "miss_ratio": miss_ratio,
            "should_offer_life_happened": miss_ratio > LIFE_HAPPENED_MISS_RATIO_THRESHOLD,
        }