import sys, os, json


from engines.deterministic_engine import DeterministicEngine
from engines.absorption_engine import AbsorptionEngine
from engines.review_engine import ReviewEngine
from models.schemas import Profile, Goal, WeeklyPerformance, GoalCategory
import config as cfg

results = []

def log(component, case, expected, actual, passed, note=""):
    results.append(dict(component=component, case=case, expected=expected, actual=actual,
                         passed=passed, note=note))

# ---------------------------------------------------------------
# 1. DETERMINISTIC ENGINE — run across varied user profiles
# ---------------------------------------------------------------
scenarios = [
    ("Student, first week, light load", Profile(name="A", profession="student"), True, dict(caregiving_hours=0)),
    ("Nurse, caregiving 15h, first week", Profile(name="B", profession="nurse"), True, dict(caregiving_hours=15)),
    ("Freelancer/gig-style, ongoing week", Profile(name="C", profession="freelancer"), False, dict(caregiving_hours=0)),
    ("Unemployed/job-seeking, heavy caregiving 25h (above 20h cap)", Profile(name="D", profession="unemployed"), True, dict(caregiving_hours=25)),
    ("Business owner, planned event 10h", Profile(name="E", profession="business owner"), False, dict(planned_event_hours=10)),
]

goals_sample = [
    Goal(id="g1", category=GoalCategory.CAREER_AND_WORK, traffic=0.7, volatility=0.3, tasks=[]),
    Goal(id="g2", category=GoalCategory.HEALTH_AND_WELLNESS, traffic=0.3, volatility=0.2, tasks=[]),
]

de = DeterministicEngine()
for label, profile, first_week, kwargs in scenarios:
    try:
        out = de.run(profile=profile, goals=goals_sample, is_first_week=first_week, **kwargs)
        checks = []
        checks.append(("weekly_capacity > 0", out["weekly_capacity"] > 0))
        checks.append(("PRF within [0.10, 1.0] clamp", cfg.PRF_MIN <= out["prf"] <= cfg.PRF_MAX))
        checks.append(("reserve_hours >= 0", out["reserve_hours"] >= 0))
        checks.append(("lifeload is numeric and finite", isinstance(out["lifeload"], (int, float))))
        allpass = all(c[1] for c in checks)
        log("Deterministic Engine", label,
            "capacity>0, PRF in [0.10,1.0], reserve>=0, lifeload numeric",
            f"capacity={out['weekly_capacity']:.1f}h, PRF={out['prf']:.3f}, reserve={out['reserve_hours']:.1f}h, lifeload={out['lifeload']:.1f}",
            allpass, "; ".join(n for n,p in checks if not p) or "all checks passed")
    except Exception as e:
        log("Deterministic Engine", label, "runs without error", f"EXCEPTION: {e}", False, "crashed")

# caregiving 25h case: verify penalty capped at PRF_CAREGIVING_MAX_PENALTY specifically
out_high_caregiving = de.run(profile=Profile(name="D2", profession="unemployed"), goals=goals_sample,
                              is_first_week=True, caregiving_hours=40)  # above the 20h saturation point
out_baseline = ReviewEngine()
prf_direct = out_baseline.calculate_prf(current_caregiving_hours=40)
prf_uncapped_diff = round(cfg.PRF_FIRST_WEEK_DEFAULT - prf_direct, 4)
capped_ok = prf_uncapped_diff <= cfg.PRF_CAREGIVING_MAX_PENALTY + 1e-6
log("ReviewEngine (PRF)", "Caregiving penalty caps at 40h (>>20h saturation point)",
    f"penalty <= {cfg.PRF_CAREGIVING_MAX_PENALTY}",
    f"actual penalty = {prf_uncapped_diff}", capped_ok,
    "confirms cap holds even far past the 20h saturation point")

# ---------------------------------------------------------------
# 2. PRF FORMULA — exact threshold behavior, per documented rules
# ---------------------------------------------------------------
prf_cases = [
    ("Good week: completion=0.85, reserve_usage=0.20 -> should get +0.05 bonus",
     WeeklyPerformance(week_number=1, completion_rate=0.85, reserve_usage_ratio=0.20), "bonus"),
    ("Bad week: completion=0.40 -> should get -0.07 penalty",
     WeeklyPerformance(week_number=1, completion_rate=0.40, reserve_usage_ratio=0.10), "penalty"),
    ("Bad week: reserve_usage=0.85 (over floor) even with ok completion -> penalty",
     WeeklyPerformance(week_number=1, completion_rate=0.75, reserve_usage_ratio=0.85), "penalty"),
    ("Neutral week: completion=0.65, reserve_usage=0.50 -> nudge toward 0.5",
     WeeklyPerformance(week_number=1, completion_rate=0.65, reserve_usage_ratio=0.50), "neutral"),
    ("Boundary: completion exactly 0.80, reserve exactly 0.30 -> bonus (>=, <=)",
     WeeklyPerformance(week_number=1, completion_rate=0.80, reserve_usage_ratio=0.30), "bonus"),
]

for label, perf, expected_kind in prf_cases:
    re_ = ReviewEngine()
    re_.record_week_performance(perf)
    prf = re_.calculate_prf()
    base = cfg.PRF_FIRST_WEEK_DEFAULT
    if expected_kind == "bonus":
        expected_val = round(base + cfg.PRF_GOOD_BONUS, 4)
        ok = abs(prf - expected_val) < 1e-6
    elif expected_kind == "penalty":
        expected_val = round(base - cfg.PRF_BAD_PENALTY, 4)
        ok = abs(prf - expected_val) < 1e-6
    else:
        expected_val = round(base + (cfg.PRF_NEUTRAL_TARGET - base) * cfg.PRF_NEUTRAL_PULL_FRACTION, 4)
        ok = abs(prf - expected_val) < 1e-6
    log("ReviewEngine (PRF formula)", label, f"PRF = {expected_val}", f"PRF = {prf}", ok)

# ---------------------------------------------------------------
# 3. ABSORPTION ENGINE — Stage 1 / Stage 2 gating logic
# ---------------------------------------------------------------
ae = AbsorptionEngine()

stage1_cases = [
    ("Small disruption (2h) well within reserve (5h) -> silent absorb", 2.0, 5.0, True),
    ("Disruption (6h) exceeds reserve (5h) -> falls through to Stage 2", 6.0, 5.0, False),
    ("Disruption exactly equals reserve (5h == 5h) -> absorbs (boundary, <=)", 5.0, 5.0, True),
]
for label, hours_lost, reserve, expect_absorb in stage1_cases:
    result = ae.try_silent_absorption(hours_lost, reserve)
    absorbed = result is not None
    ok = absorbed == expect_absorb
    log("AbsorptionEngine (Stage 1)", label, f"absorbs={expect_absorb}", f"absorbs={absorbed}", ok,
        json.dumps(result) if result else "None (fell through)")

stage2_cases = [
    ("Shortfall (3h) fits within both caps -> proposal returned", 3.0, 40.0, 168.0, 2.0, True),
    ("Shortfall (50h) exceeds any realistic cap -> None, falls to Stage 3", 50.0, 40.0, 168.0, 2.0, False),
    ("Points cap already used up this week (9.5 of 10 used) -> tight headroom", 2.0, 40.0, 168.0, 9.5, None),
]
for label, shortfall, cur_lifeload, cap, pts_used, expect in stage2_cases:
    result = ae.try_lifeload_renegotiation(shortfall, cur_lifeload, cap, pts_used)
    covered = result is not None
    if expect is None:
        ok = True  # informational case, just logging behavior
        note = f"covered={covered}, result={json.dumps(result) if result else None}"
    else:
        ok = covered == expect
        note = json.dumps(result) if result else "None (falls to Stage 3)"
    log("AbsorptionEngine (Stage 2)", label,
        f"covered={expect}" if expect is not None else "informational",
        f"covered={covered}", ok, note)

# ---------------------------------------------------------------
# SUMMARY
# ---------------------------------------------------------------
by_component = {}
for r in results:
    by_component.setdefault(r["component"], []).append(r)

print("="*100)
for comp, rows in by_component.items():
    passed = sum(1 for r in rows if r["passed"])
    total = len(rows)
    print(f"\n{comp}: {passed}/{total} passed")
    for r in rows:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"  [{mark}] {r['case']}")
        print(f"         expected: {r['expected']}")
        print(f"         actual:   {r['actual']}")
        if r["note"]:
            print(f"         note:     {r['note']}")

with open("eval_results.json", "w") as f:
    json.dump(results, f, indent=2)
print("\nSaved to eval_results.json")
