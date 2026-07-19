"""
seed_patterns.py -- condensed, prompt-ready version of the founder's
curated reference document (DoneHo_Seed_Reference_v3.md, kept in the repo
root as the full source). This file is NOT the full document -- it's a
high-signal subset chosen to fit in a prompt without bloating every
NudgeAgent call. Update this file, not the full markdown, if you want to
change what the agent actually sees.

Every pattern here was either founder-verified or AI-discovered by
applying the founder's own reasoning style -- see the full markdown for
the complete 100-task set, all 36 cross-persona patterns, and the
reasoning behind each. This file distills that into what actually gets
fed to the model.
"""

SEED_RULES_BLOCK = """
CURATED REASONING RULES (apply these before generating anything):

1. Never pair two tasks that both demand full, undivided attention.
   Physically-busy-but-mentally-free tasks (chopping vegetables, folding
   laundry, walking, commuting) are what OTHER things get paired onto --
   not tasks that themselves need full attention.
2. The test is felt experience, not technical compatibility. If a
   pairing would make the user feel MORE loaded, it's the wrong pairing,
   even if it looks compatible on paper.
3. Physical safety overrides efficiency, always. Never suggest anything
   requiring visual or manual attention during self-driving -- voice-only,
   within stated limits. The identical suggestion is fine as a passenger
   or on public transport (train/bus/metro) -- state this distinction
   explicitly when a commute task comes up.
4. Some tasks are protected on principle, not just by cognitive load --
   meditation, prayer, digital detox, dedicated family/partner time,
   journaling. Never suggest combining these with anything else, even if
   a plausible pairing exists.
5. Caregiving-related combinations must be genuine overlap, never a
   workaround that uses the child/dependent to enable multitasking.
   The bar: would this suggestion sound reasonable if described directly
   to the parent/caregiver, or does it sound like exploiting their time
   with their child?
6. Every Smart Spend suggestion needs real trade-off reasoning -- time
   saved vs. cost, or quality vs. cost by item category (disposable items:
   optimize for cost; long-term/tactile items: optimize for quality) --
   never a bare "buy this" claim.
7. Favor reusable systems (a weekly chart, a standing agreement, an
   organizer) over suggesting the same one-off decision be made again
   next week.
8. Watch for "don't double-count effort" -- if an existing task already
   substantially serves a stated goal (e.g. a job that's already
   physically active), don't also suggest a separate task for that same
   goal on top of it.
9. The same task type can need a different tier depending on its actual
   purpose in the moment, not a fixed label. An "informational" call is
   different from a "decision-making" call of the same category.
"""

SEED_PATTERNS_BLOCK = """
CURATED EXAMPLE PATTERNS (these are illustrative, not exhaustive -- apply
the same STYLE of reasoning to whatever the user's actual tasks are):

- Chopping vegetables or sauteing (physically-busy, mentally-free) pairs
  well with audio learning (a podcast, a language lesson). The same
  audio content paired with something already demanding focus (e.g.
  office work) makes the user feel MORE exhausted, not less -- same
  audio content, opposite effect, depending on what it's paired with.
- A parent playing "mirror play" with a baby naturally puts them in
  front of a mirror -- a genuine, unforced opportunity to practice
  interview posture, tone, and body language at the same time.
- A robotic vacuum often fails in homes with a lot of everyday clutter on
  the floor. In that specific context, a human help service can be more
  time-and-cost-effective than the "obvious" automation purchase --
  don't default to recommending a gadget without checking real fit.
- For a working parent evaluating a job opportunity, factor in more than
  skill-fit alone -- commute accessibility and workplace-culture signals
  (e.g. public reviews) genuinely affect their caregiving capacity, not
  just their career satisfaction.
- Fast walking to/from a commute while hearing a learning podcast stacks
  three real benefits at once: exercise, revision time, and money saved
  versus paid transport -- the strongest suggestions stack several real
  benefits, not just one.
- For someone whose job already involves substantial physical activity
  (e.g. delivery work), don't layer a separate "workout" suggestion on
  top -- recognize the existing task already serves that goal. The same
  logic applies inside a shared household: if someone else already cooks
  the daily meal, don't generate a duplicate meal-prep task for it.
- A large volume of study material (e.g. many PDFs or long documents) can
  be converted into concise audio notes and consumed across a few passes
  at increased speed -- useful for any task with a large body of source
  material, not just one subject.
- New-parent errands are often unlocked by a specific window (e.g. a
  stroller nap), not run alongside another simultaneous task -- this is a
  timing-trigger pattern, distinct from a concurrent-activity pairing.
- A new, hard-to-stick habit is easier to sustain when anchored
  immediately before or after an already-reliable existing routine
  (e.g. right after brushing teeth), rather than given its own standalone
  reminder.
- For tasks prone to being deferred and forgotten (e.g. expense
  tracking), capturing at the exact moment it happens beats batching it
  for later -- the batching step is usually where the task quietly dies.
- Some tasks pair through PRESENCE, not activity -- e.g. working alongside
  another person on a quiet call without direct interaction ("body
  doubling") can help someone start a task they'd otherwise put off, even
  though the task's actual content stays fully demanding and unshared.
- The same commute task needs opposite suggestions depending on
  direction and context: a dawn commute home after a night shift needs
  calming content, not energizing content -- the standard "add learning
  audio to a commute" default can actively work against the user's real
  goal (in this case, sleep) if direction isn't considered.
- A task's normal pairing tier can be temporarily overridden by a real
  physical constraint (e.g. post-surgery recovery) even if that same task
  is normally easy to combine with something else -- the override is
  time-bound, not a permanent reclassification.
- The same task category can need different treatment depending on its
  actual purpose in the moment: an informational call can pair with a
  commute; a decision-making call of the exact same category (e.g.
  choosing a vendor) needs full, undistracted attention.
- Long-distance partners maintaining connection on a low-bandwidth day can
  do "parallel presence" -- both cooking dinner separately while on a
  call together -- a different combination axis (shared presence) than
  most pairings in this list (shared time-efficiency).
- When someone's available time comes in short, unpredictable bursts
  (e.g. early parenthood, or work with many small gaps), the right fix is
  often shrinking the task itself to fit the real window, not searching
  for something to pair it with.
- Suggestions defaulting to audio content need a stated visual/text
  fallback for users who can't rely on audio -- don't assume one format
  serves everyone by default.
- During a known difficult period (if the user has indicated one), lower
  the practical bar for what counts as a good day rather than holding a
  flat standard regardless of context.
"""


def get_seed_context() -> str:
    """Returns the full curated block to embed in NudgeAgent's instruction."""
    return SEED_RULES_BLOCK + "\n" + SEED_PATTERNS_BLOCK
