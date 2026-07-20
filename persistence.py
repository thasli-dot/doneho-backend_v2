""" 
persistence.py — Save and restore DoneHo session state to/from Supabase.

This is intentionally a separate module, not a change to orchestrator.py
or shared_state.py. It knows how to turn a live DoneHoOrchestrator's state
into plain JSON (for saving) and rebuild an equivalent orchestrator from
that JSON (for restoring after a server restart).

Only two things actually need persisting across a restart:
  1. SharedExecutionState (all the fields listed in shared_state.py)
  2. DeterministicEngine's ReviewEngine history + last PRF (so PRF genuinely
     carries forward across weeks, not just within one server lifetime)

AbsorptionEngine, DayOutputEngine, and RecoveryApplier hold no meaningful
instance state (no __init__ overrides beyond the default) — they're
recreated fresh every time, same as before.
"""
import os
from supabase import create_client
from models.schemas import (
    Profile, Goal, Blueprint, ExecutionContract, DisruptionLog,
    RecalibrationProposal, FreeTimeSuggestionOutput, DailyCheckIn,
    WeeklyPerformance, SuggestionItem, TaskPerformance, LongTermTaskState,
)
from orchestrator import DoneHoOrchestrator

_supabase = None


def get_supabase():
    global _supabase
    if _supabase is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SECRET_KEY"]
        _supabase = create_client(url, key)
    return _supabase


def _dump(model_or_list):
    """Turn a Pydantic model, a list of them, or None into JSON-safe data."""
    if model_or_list is None:
        return None
    if isinstance(model_or_list, list):
        return [m.model_dump(mode="json") for m in model_or_list]
    return model_or_list.model_dump(mode="json")


def serialize_state(orchestrator: DoneHoOrchestrator) -> dict:
    """Turn a live orchestrator's state into a plain JSON-safe dict."""
    s = orchestrator.state
    review = orchestrator.deterministic_engine.review_engine

    return {
        "profile": _dump(s.profile),
        "goals": _dump(s.goals),
        "is_first_week": s.is_first_week,
        "week_number": s.week_number,
        "weekly_capacity": s.weekly_capacity,
        "prf": s.prf,
        "lifeload": s.lifeload,
        "reserve_hours": s.reserve_hours,
        "planning_confidence": s.planning_confidence,
        "commitment_contract": s.commitment_contract,
        "blueprint": _dump(s.blueprint),
        "execution_contract": _dump(s.execution_contract),
        "reserve_used_this_week": s.reserve_used_this_week,
        "lifeload_renegotiated_increase": s.lifeload_renegotiated_increase,
        "last_disruption_outcome": _dump(s.last_disruption_outcome),
        "suggestions": {
            "opportunity_map": _dump(s.suggestions["opportunity_map"]),
            "day_boosters": _dump(s.suggestions["day_boosters"]),
            "smart_spend": _dump(s.suggestions["smart_spend"]),
        },
        "interaction_signals": s.interaction_signals,
        "last_gain_suggestions": _dump(s.last_gain_suggestions),
        "disruption_log": _dump(s.disruption_log),
        "daily_checkins": _dump(s.daily_checkins),
        "day_output_totals": s.day_output_totals,
        "last_caregiving_hours": s.last_caregiving_hours,
        "last_day_submitted_date": s.last_day_submitted_date,
        "week_start_date": s.week_start_date,
        "task_performance_history": _dump(s.task_performance_history),
        "long_term_tasks": {
            task_id: tracker.model_dump(mode="json")
            for task_id, tracker in s.long_term_tasks.items()
        },
        # ReviewEngine — needed so PRF genuinely carries across weeks/restarts
        "review_history": _dump(review.history),
        "review_last_prf": getattr(review, "_last_prf", None),
    }


def restore_state(orchestrator: DoneHoOrchestrator, data: dict) -> None:
    """Rebuild a fresh orchestrator's state from previously saved JSON.
    Bypasses state.write()'s ownership gate on purpose — this is a
    lifecycle/infra operation (loading from storage), not a live agent
    write, same category as reset_weekly_counters()."""
    s = orchestrator.state

    # Every field below uses .get() with a sensible default, not direct
    # data["field"] access. This is a deliberate defensive fix: a
    # KeyError was observed in production (item 7 testing) on
    # 'commitment_contract' specifically, cause not fully confirmed
    # (possibly multiple server workers/instances not sharing the
    # in-memory SESSIONS cache, forcing frequent restores; possibly a
    # save/restore race). Rather than leave the whole function fragile
    # to any similarly missing key, every field restores safely now --
    # a genuinely missing key falls back to a sensible default instead
    # of crashing the whole session.
    s.profile = Profile.model_validate(data["profile"])
    s.goals = [Goal.model_validate(g) for g in data.get("goals", [])]
    s.is_first_week = data.get("is_first_week", True)
    s.week_number = data.get("week_number", 1)
    s.weekly_capacity = data.get("weekly_capacity", 0.0)
    s.prf = data.get("prf", 0.5)
    s.lifeload = data.get("lifeload", 0.0)
    s.reserve_hours = data.get("reserve_hours", 0.0)
    s.planning_confidence = data.get("planning_confidence", 0.0)
    s.commitment_contract = data.get("commitment_contract") or {}
    blueprint_data = data.get("blueprint")
    s.blueprint = Blueprint.model_validate(blueprint_data) if blueprint_data else None
    execution_contract_data = data.get("execution_contract")
    s.execution_contract = (
        ExecutionContract.model_validate(execution_contract_data)
        if execution_contract_data else None
    )
    s.reserve_used_this_week = data.get("reserve_used_this_week", 0.0)
    s.lifeload_renegotiated_increase = data.get("lifeload_renegotiated_increase", 0.0)
    last_disruption_data = data.get("last_disruption_outcome")
    s.last_disruption_outcome = (
        RecalibrationProposal.model_validate(last_disruption_data)
        if last_disruption_data else None
    )
    suggestions_data = data.get("suggestions") or {}
    s.suggestions = {
        "opportunity_map": [SuggestionItem.model_validate(x) for x in suggestions_data.get("opportunity_map", [])],
        "day_boosters": [SuggestionItem.model_validate(x) for x in suggestions_data.get("day_boosters", [])],
        "smart_spend": [SuggestionItem.model_validate(x) for x in suggestions_data.get("smart_spend", [])],
    }
    s.interaction_signals = data.get("interaction_signals", [])
    last_gain_data = data.get("last_gain_suggestions")
    s.last_gain_suggestions = (
        FreeTimeSuggestionOutput.model_validate(last_gain_data)
        if last_gain_data else None
    )
    s.disruption_log = [DisruptionLog.model_validate(d) for d in data.get("disruption_log", [])]
    s.daily_checkins = [DailyCheckIn.model_validate(d) for d in data.get("daily_checkins", [])]
    s.day_output_totals = data.get("day_output_totals") or {"total_checked": 0, "total_missed": 0}
    s.last_caregiving_hours = data.get("last_caregiving_hours", 0.0)
    s.last_day_submitted_date = data.get("last_day_submitted_date")
    s.week_start_date = data.get("week_start_date")
    s.task_performance_history = [
        TaskPerformance.model_validate(t)
        for t in data.get("task_performance_history", [])
    ]
    s.long_term_tasks = {
        task_id: LongTermTaskState.model_validate(tracker_data)
        for task_id, tracker_data in data.get("long_term_tasks", {}).items()
    }

    review = orchestrator.deterministic_engine.review_engine
    review.history = [WeeklyPerformance.model_validate(w) for w in data.get("review_history", [])]
    if data.get("review_last_prf") is not None:
        review._last_prf = data["review_last_prf"]


def save_session(session_id: str, orchestrator: DoneHoOrchestrator) -> None:
    """Write the current state to Supabase. Call this after any endpoint
    that changes state — safe to call often, it's just an upsert."""
    data = serialize_state(orchestrator)
    get_supabase().table("sessions").upsert({
        "session_id": session_id,
        "state_json": data,
    }).execute()


def load_session(session_id: str) -> dict | None:
    """Fetch previously saved state for a session, or None if it doesn't exist."""
    result = (
        get_supabase()
        .table("sessions")
        .select("state_json")
        .eq("session_id", session_id)
        .execute()
    )
    if result.data:
        return result.data[0]["state_json"]
    return None
