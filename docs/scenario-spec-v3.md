# Scenario design (Research Note v3, 2026-08-11)

Authoritative structure for the eight encounters. Source: *Relational Fluency
Research Note v3*, slides 7–14.

## One skeleton per construct, two parallel variants

Variants exist to measure improvement: **attempt 1 → feedback → attempt 2 on the
other variant**. That only works if the variants are parallel forms of the same
measurement task, so A and B share:

- the same trigger sequence
- the same ESCI item map
- the same rubric and difficulty

Δ across attempts is then skill change, not an easier scenario. A pilot checks
difficulty equivalence; form order is counterbalanced. This settles the deck's
open question "same scenario twice, or a parallel form?" — attempt 2 runs the
other variant. A third variant per construct can be added on the same skeleton.

## Two structural rules that drive the implementation

**Context lands in-scene.** No briefing document. The participant sees at most
2–3 sentences of setup; everything else arrives through the opening agent's
first turns — a colleague venting, a manager's greeting, a meeting already
underway.

*The actors cannot be handed that same text.* A spec's `setup:` is written **to**
the participant, in the second person ("You are a senior analyst… You now hold a
written competing offer"). Pasted into a character's system prompt it briefs Sam
as the analyst whose work Sam took, and it tells S2's Morgan about the competing
offer before the participant has played it — which is the thing the Influence
construct measures. So actors get a third-person retelling of the setup with the
participant's private holdings removed. Three optional keys control that:

| Key | What it does |
|---|---|
| `private_setup:` | The setup sentences, or the clauses inside them, that are the participant's to reveal. Named explicitly, they are removed from the actors' scene and nothing is guessed. |
| `assets:` | The facts the participant can deploy. With no `private_setup`, a setup sentence that reads as a restatement of an asset is dropped by a word-overlap heuristic, and that removal is logged as a `WARNING` for a human to check. |
| `actor_setup:` | The actors' scene, written by hand. When present it is used **verbatim**: the rewrite and the redaction are skipped entirely and nothing is inferred. |

`actor_setup` is the escape hatch, and the reason it exists is that the derived
version rests on two heuristics — a pronoun rewrite ("you" → "they"/"them") and,
without `private_setup`, a guess at which sentence gives a secret away. Neither
reads every phrasing. `server/scenarios_v3.py` logs a `WARNING` when the rewrite
leaves doubtful text or when the redaction empties the scene; that warning is the
signal to author an `actor_setup`. Write it in the third person, naming the
participant ("The participant is a senior analyst at…"), and leave their leverage
out — an actor who knows it cannot play the encounter honestly.

None of the three is required and none is validated, so a spec loads and compiles
without them; today none of the eight defines `actor_setup`. Note also that
authoring one changes only what the *actors* see. The debrief judge and the
steering controller read the unredacted retelling of `setup` instead, because
they reason about the encounter rather than perform in it, and the leverage is
exactly what they need in order to score it.

**Silence is data, not a gap.** Each encounter is two interactions with an
ordered set of planted triggers, each tied to specific ESCI items. *If the
participant stays silent at an opening, the agent probes* — so avoidance becomes
scoreable behavior rather than missing data. The runner therefore needs a
no-speech timeout that prompts the agent to probe, not just a silence detector
that closes turns.

## The eight encounters

Interaction mode matters: **1:1** is one character at a time; **group** is
several characters in one live room, where the dynamics between them (talking
over, relabelling ideas) are themselves the measurement.

| Construct | Var | Title | Agents | Interaction 1 | Interaction 2 |
|---|---|---|---|---|---|
| Conflict Management | A | Taken credit | Riley (colleague, pushes) · Sam (peer) | 1:1 — Riley corners you | 1:1 — hallway run-in with Sam |
| Conflict Management | B | Hostile after-hours message | Mel (urges reply-all) · Drew (sender) | 1:1 — Mel pings you first thing | 1:1 — coffee-machine run-in with Drew |
| Influence | A | Promised raise & competing offer | Morgan (budget-constrained manager) | 1:1 — making the case | 1:1 — the deflection ladder |
| Influence | B | Hybrid under an RTO mandate | Sasha (manager squeezed from above) | 1:1 — making the case | 1:1 — the deflection ladder |
| Inspirational Leadership | A | After resignations | Alex (cynic) · Jordan (disengaged) · Casey (anxious junior) | **group — team meeting** | 1:1 — brief one-on-ones |
| Inspirational Leadership | B | After a commission cut | Toni (cynic) · Lee (disengaged) · Ari (anxious junior) | **group — team meeting** | 1:1 — brief one-on-ones |
| Teamwork | A | Planning an internal rollout | Priya (excluded) · Dan (dominates) · Chris (neutral) | **group — 4-person working session** | **group — the close** |
| Teamwork | B | Preparing a client presentation | Priya · Dan · Chris | **group — working session** | **group — the close** |

**All eight are authored and compiled** into `scenarios/v3/` (`S1A S1B S2A S2B
S3A S3B S4A S4B`). Being in the bank is not the same as being assignable,
though: **S1-A must not be served in a session that also contains S4.** Both
turn on someone taking credit for the participant's work, so pairing them makes
the Conflict Management and Teamwork measures bleed into each other. Every
session contains S4, so the S1 form a study participant gets is **B** (the
canonical spec also allows C, which is not compiled yet). See "Variation
assignment" in `reddit-analysis/scenarios/scenario-specifications.md`. The rule
is enforced rather than left on paper: `FORM_EXCLUSIONS` in `server/runs.py`
carries it as data and `runs.create` corrects any draw that violated it, noting
the swap on the run document as `form_exclusions`.

**Group rooms are required.** S3-A opens with a team meeting whose planted
trigger is Alex challenging the participant *publicly*; S4 is explicitly "one
live 4-person session" where Dan talks over Priya and relabels Chris's idea.
Those dynamics cannot be reproduced as consecutive 1:1 conversations — the
simultaneity is the instrument.

## Planted triggers

Triggers fire in a fixed order and each maps to specific ESCI items. Examples:

- **S1-A, interaction 1** — Riley: *"Sam presented your analysis again — third
  time. Reply-all with the original file."* On hesitation: *"Or just let it go.
  Not worth the drama."* Observe: retaliate, avoid, or commit to raising it.
- **S1-A, interaction 2** — if the participant stays quiet, Sam probes; when
  raised, Sam turns defensive (*"I did most of the legwork anyway"*), then
  offers a face-saving half-concession.
- **S2, interaction 2** — a fixed deflection ladder: no budget → everyone's
  stretched → can't set a precedent. The last rung is contradicted by the
  precedent the participant holds. The manager moves only when handed something
  defensible upward; the natural close is a dated commitment.
- **S4, interaction 1** — Dan talks over Priya → Priya fades (*"never mind"*) →
  Dan restates Chris's idea as his own → decisions close with Priya silent.

Each slide carries scored sample answers (a high-scoring and a low-scoring reply
per trigger). These are the natural few-shot anchors for the Phase 3 LLM judge
and should be carried into the scenario files rather than left in the deck.

## Implementation consequences

1. **Scenario schema needs interactions.** An encounter is two interactions,
   each with a mode (1:1 or group), a cast, and an ordered trigger list.
2. **Group support must be rebuilt on Gemini Live.** The v1 multi-agent runner
   was deleted with the vendor cascade (commit `d2b896c`, recoverable); S3 and
   S4 — half the constructs — cannot run without a replacement.
3. **Triggers need to be first-class**, fired in order and logged with their
   ESCI item ids, so the steering log shows which trigger produced which
   response. This is what makes an encounter scoreable.
4. **Probe-on-silence** belongs in the runner alongside turn detection.
5. **Variant pairing** must be modelled so the RCT can serve the other variant
   at attempt 2, with counterbalanced order.
