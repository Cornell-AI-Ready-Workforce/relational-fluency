# Scenario design (Research Note v3, 2026-08-11)

Authoritative structure for the encounters. Source: *Relational Fluency
Research Note v3*, slides 7–14. The bank holds **eight**, two forms per
construct (a third form per construct was authored in 2026-09 and retired the
same month for Study 1), and this document describes what is compiled.

## One skeleton per construct, two parallel forms

Forms exist to measure improvement: **attempt 1 → feedback → attempt 2 on a form
the participant has not met**. That only works if the forms are parallel forms
of the same measurement task, so A and B share:

- the same trigger sequence
- the same ESCI item map
- the same rubric and difficulty

Δ across attempts is then skill change, not an easier scenario. A pilot checks
difficulty equivalence; form order is counterbalanced.

**Attempt 2 does not run "the other variant".** That phrasing is from the
two-form design and it is now a defect rather than a description: with three
forms there is no "the other" one, and the `parallel_form:` scalar each spec
carries names only the sibling it was *written to match*. It is provenance.
**The routing authority is `server/scenarios_v3.parallel_forms()`**, which
returns every other form of the construct; `server/runs.sibling_run` chooses
from that list, rotating on attempt 1's run id so no form is unreachable, and
pins the result PER SLOT so a second attempt can serve a construct's unseen form
and one it has met rather than the same conversation twice. Nothing may route on
`parallel_form:` and nothing may count forms from this document.

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
authoring one changes only what the *actors* see. The steering controller
reads the unredacted retelling of `setup` instead, because it reasons about the
encounter rather than performing in it, and the leverage is what it needs.

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
| Conflict Management | C | Blamed in front of the manager | Nadia (was in the room, pushes) · Wes (told it as his) | 1:1 — Nadia catches you about Tuesday | 1:1 — Wes stops by your desk |
| Influence | A | Promised raise & competing offer | Morgan (budget-constrained manager) | 1:1 — making the case | 1:1 — the deflection ladder |
| Influence | B | Hybrid under an RTO mandate | Sasha (manager squeezed from above) | 1:1 — making the case | 1:1 — the deflection ladder |
| Influence | C | Stopping the Monday pack | Imani (manager exposed, not squeezed) | 1:1 — making the case | 1:1 — the deflection ladder |
| Inspirational Leadership | A | After resignations | Alex (cynic) · Jordan (disengaged) · Casey (anxious junior) | **group — team meeting** | 1:1 — brief one-on-ones |
| Inspirational Leadership | B | After a commission cut | Toni (cynic) · Lee (disengaged) · Ari (anxious junior) | **group — team meeting** | 1:1 — brief one-on-ones |
| Inspirational Leadership | C | A system nobody asked for | Bex (cynic) · Rafa (disengaged) · Noor (anxious junior) | **group — team meeting** | 1:1 — brief one-on-ones |
| Teamwork | A | Planning an internal rollout | Priya (excluded) · Dan (dominates) · Chris (neutral) | **group — 4-person working session** | **group — the close** |
| Teamwork | B | Preparing a client presentation | Priya · Dan · Chris | **group — working session** | **group — the close** |
| Teamwork | C | Writing up the outage | Yara (excluded) · Hugo (dominates) · Finn (neutral) | **group — 4-person working session** | **group — the close** |

**All eight are authored and compiled** into `scenarios/v3/` (`S1A S1B S2A S2B
S3A S3B S4A S4B`; the C forms were retired in 2026-09). Being in the bank is not
the same as being assignable, though: **S1-A must not be served in a session
that also contains S4.** Both turn on someone taking credit for the
participant's work, so pairing them makes the Conflict Management and Teamwork
measures bleed into each other. Every full session contains S4, so a random
draw gives a participant **S1-B**; Study 1 pins form A on every run and takes
the documented escape hatch instead (see `docs/OPERATIONS.md`). See "Variation
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
5. **Form pairing** must be modelled so the RCT can serve a form the
   participant has not met at attempt 2, with counterbalanced order. Not "the
   other variant": see the correction at the top of this document.
