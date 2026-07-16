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
    WeeklyPerformance,
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
        "suggestions": s.suggestions,
        "interaction_signals": s.interaction_signals,
        "last_gain_suggestions": _dump(s.last_gain_suggestions),
        "disruption_log": _dump(s.disruption_log),
        "daily_checkins": _dump(s.daily_checkins),
        "day_output_totals": s.day_output_totals,
        "last_caregiving_hours": s.last_caregiving_hours,
        "last_day_submitted_date": s.last_day_submitted_date,
        "week_start_date": s.week_start_date,
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

    s.profile = Profile.model_validate(data["profile"])
    s.goals = [Goal.model_validate(g) for g in data["goals"]]
    s.is_first_week = data["is_first_week"]
    s.week_number = data["week_number"]
    s.weekly_capacity = data["weekly_capacity"]
    s.prf = data["prf"]
    s.lifeload = data["lifeload"]
    s.reserve_hours = data["reserve_hours"]
    s.planning_confidence = data["planning_confidence"]
    s.commitment_contract = data["commitment_contract"]
    s.blueprint = Blueprint.model_validate(data["blueprint"]) if data["blueprint"] else None
    s.execution_contract = (
        ExecutionContract.model_validate(data["execution_contract"])
        if data["execution_contract"] else None
    )
    s.reserve_used_this_week = data["reserve_used_this_week"]
    s.lifeload_renegotiated_increase = data["lifeload_renegotiated_increase"]
    s.last_disruption_outcome = (
        RecalibrationProposal.model_validate(data["last_disruption_outcome"])
        if data["last_disruption_outcome"] else None
    )
    s.suggestions = data["suggestions"]
    s.interaction_signals = data["interaction_signals"]
    s.last_gain_suggestions = (
        FreeTimeSuggestionOutput.model_validate(data["last_gain_suggestions"])
        if data["last_gain_suggestions"] else None
    )
    s.disruption_log = [DisruptionLog.model_validate(d) for d in data["disruption_log"]]
    s.daily_checkins = [DailyCheckIn.model_validate(d) for d in data["daily_checkins"]]
    s.day_output_totals = data["day_output_totals"]
    s.last_caregiving_hours = data["last_caregiving_hours"]
    # .get() with a default, not data[...] -- sessions saved before this
    # change won't have these keys yet, and that must not crash restore.
    s.last_day_submitted_date = data.get("last_day_submitted_date")
    s.week_start_date = data.get("week_start_date")

    review = orchestrator.deterministic_engine.review_engine
    review.history = [WeeklyPerformance.model_validate(w) for w in data["review_history"]]
    if data["review_last_prf"] is not None:
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
