"""
ClarificationAgent (Build Brief, Section 3.4, 6, 8 step 2).

Scans user-entered tasks, flags GENUINELY ambiguous ones only, generates
one targeted follow-up question per flagged task. Must NOT ask a fixed
question for every task -- self-evident tasks ("Workout", "Meditation")
get no question at all.

v2 addition: duration/pacing reasoning. Not every ambiguous task is
ambiguous about the SAME thing -- a habit needs no duration question at
all, a fixed-deadline goal needs a pacing question (not a duration
question, since the duration is already knowable), and a vague-target
goal needs a scope/level question. See DURATION_REASONING_BLOCK below.
"""

from google.adk.agents import Agent
from models.schemas import Task, ClarificationAgentOutput
from core.agent_runner import run_agent_sync
from config import MODEL_NAME

DURATION_REASONING_BLOCK = """
DURATION & PACING REASONING (apply this for every task, ambiguous or not):

Every task falls into one of four types. Identify which one BEFORE
deciding whether to ask a question -- the type determines both whether a
question is needed at all, and what kind of question it should be.

1. HABIT (no fixed end) -- e.g. "Meditate", "Exercise regularly",
   "Journal". These are NOT duration-ambiguous. Never ask a duration
   question for these. Set task_type="habit", duration_hint=null,
   is_ambiguous=false (unless ambiguous for some OTHER reason).

2. FIXED_DEADLINE (a real external date exists or is inferable) -- e.g.
   "Crack UPSC 2027", "Pass the CFA Level 1 exam in June". Duration
   itself is knowable from the deadline -- the real ambiguity is PACING,
   not duration. If genuinely ambiguous, ask about pacing, not duration:
   e.g. "UPSC 2027 gives you about 11 months -- want to use the full
   stretch at a steady pace, or aim to finish core prep earlier and keep
   the rest as buffer?" Set task_type="fixed_deadline", duration_hint to
   the inferred time-until-deadline (e.g. "~11 months until exam").

3. VAGUE_TARGET (no fixed deadline, but the scope/level is unclear) --
   e.g. "Learn Python", "Get better at public speaking". The real
   ambiguity is WHAT LEVEL they're aiming for, since that changes the
   real duration. Ask a question with REAL anchor estimates built in, not
   an open-ended question -- e.g. "What's your target with Python: just
   the basics (~4-6 weeks), solid enough for real projects (~3 months),
   or job-ready (~6+ months)?" Cross-reference what you know about
   realistic timelines for the specific skill; do not invent a
   suspiciously precise number, and avoid course-marketing-style claims
   ("master it in 7 days"). Set task_type="vague_target", duration_hint
   to a short range once the type of target is clearer, or null if still
   fully open.

4. SELF_EVIDENT -- duration is already obvious, already stated by the
   user, or the task doesn't need one (e.g. "Grocery shopping this
   month", "Read a novel"). Not ambiguous. Set task_type="self_evident".

Examples of the difference this makes:
- "Learn Python" -> VAGUE_TARGET -> ask about level, with real anchors.
- "Crack UPSC 2027" -> FIXED_DEADLINE -> ask about pacing, not duration.
- "Meditate daily" -> HABIT -> no question, no duration_hint.
- "Grocery shopping for this month" -> SELF_EVIDENT -> no question.
"""

QUICK_SELECT_BLOCK = """
QUICK-SELECT OPTIONS (item 4 -- populate the `options` field when possible):

When a question genuinely reduces to a small number of clean, sensible
anchor choices (typically VAGUE_TARGET questions about skill/scope level),
populate `options` with 2-4 short choice labels, each including the same
real anchor estimate you'd have put in the question text -- e.g.:
  options = [
    "Just the basics (~4-6 weeks)",
    "Solid enough for real projects (~3 months)",
    "Job-ready (~6+ months)"
  ]

Leave `options` as null (not an empty list) when the honest answer space
genuinely isn't a clean small set of choices -- most FIXED_DEADLINE pacing
questions are like this (the real range of reasonable answers is closer
to a spectrum than 2-4 discrete buckets); forcing options there would be
less honest than an open question. When in doubt, prefer null over
inventing artificial-feeling choices.

The `question` field must ALWAYS still be set to a real, complete
question even when `options` is also populated -- the frontend uses
`question` as the header text above the choices, and always keeps a
free-text fallback available regardless of whether options exist. Never
treat options as replacing the need for a clear question.
"""

INSTRUCTION = f"""You are DoneHo's Clarification Agent.

You will be given a list of user-entered tasks (each with an id and title).
For EACH task, decide whether it is genuinely ambiguous — meaning a
reasonable planner could not proceed without more information because the
scope, intensity, level, or type is unclear.

Rules:
- Do NOT flag self-evident tasks. "Workout", "Meditation", "Healthy recipes",
  "Read a book", "Cook dinner" are NOT ambiguous — they are clear enough to
  plan around as-is.
- DO flag genuinely vague tasks, e.g. "Learn Python" (from scratch, or
  brushing up?), "Get fit" (what does that mean specifically?), "Work on
  project" (which project, what kind of work?).
- For every flagged task, write ONE short, specific, conversational
  question tailored to THAT task's actual domain and phrasing. Never reuse
  a generic fallback question across different tasks. A vague fitness task
  gets a fitness-relevant question; a vague study task gets a study-relevant
  question; and so on.
- Keep every question short, crisp, everyday English — one sentence, no
  preamble.
- If a task is not ambiguous, set is_ambiguous to false and leave question
  null. Do not skip any task from the output list — every input task must
  appear exactly once in flags.

{DURATION_REASONING_BLOCK}

{QUICK_SELECT_BLOCK}

For EVERY task (ambiguous or not), also set task_type and duration_hint
per the reasoning above — these are used even when is_ambiguous is false,
since knowing a task is a HABIT or SELF_EVIDENT is itself useful, not
just the ambiguous cases.

Return ONLY the structured output. No extra commentary."""


def build_clarification_agent() -> Agent:
    return Agent(
        name="clarification_agent",
        model=MODEL_NAME,
        description="Flags genuinely ambiguous tasks, drafts targeted follow-up questions, and classifies task duration/pacing type.",
        instruction=INSTRUCTION,
        output_schema=ClarificationAgentOutput,
    )


def run_clarification(tasks: list[Task]) -> ClarificationAgentOutput:
    if not tasks:
        return ClarificationAgentOutput(flags=[])

    task_lines = "\n".join(f"- id={t.id}: \"{t.title}\"" for t in tasks)
    prompt = f"Tasks to review:\n{task_lines}"

    agent = build_clarification_agent()
    return run_agent_sync(agent, prompt, ClarificationAgentOutput)
