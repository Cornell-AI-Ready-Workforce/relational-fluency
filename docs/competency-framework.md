# Competency framework: tying social awareness → relationship management → scenarios

Based on Goleman-style EI competencies. This doc defines how the four **relationship
management (RM)** sub-competencies are operationalized as simulation scenarios, and how the
two **social awareness (SA)** competencies are embedded as the *sensing layer* every
scenario probes.

## The core design logic

Social awareness is the input, relationship management is the output:

```
                    ┌─ Empathy ──────────────┐   sense feelings/perspectives
  SENSING (SA)      │                        │   from cues the AGENT EMITS
                    └─ Organizational ───────┘   read power/dynamics
                       awareness                 from context the SCENARIO PLANTS
                              │
                              ▼
                    ┌─ Influence ────────────┐
  APPLYING (RM)     ├─ Conflict management ──┤   observable in what the
                    ├─ Inspirational ────────┤   PARTICIPANT SAYS AND DOES
                    │  leadership            │
                    └─ Teamwork ─────────────┘
```

This gives each scenario a three-part anatomy:

1. **SA probes (inputs planted in the scenario).** The agent *emits* emotional cues
   (hesitation, deflection, frustration under the surface) → empathy is measurable as
   whether the participant detects and responds to them. The scenario brief and the
   agent's utterances *plant* organizational facts (who has power, who is watching,
   what pressures the counterpart is under) → organizational awareness is measurable
   as whether the participant uses them.
2. **RM demand (the task).** The participant's goal can only be achieved by applying
   the primary RM competency — e.g. the agent is scripted not to yield to demands,
   only to skilled influence.
3. **Behavioral markers (outputs for raters).** Each scenario lists high/low anchor
   behaviors; raters score the primary competency (weighted), secondary competencies,
   and both SA competencies from the transcript.

All 22 Construct-4 items are rated for every encounter: real encounters blend
competencies (conflict management usually requires influence), and rating all
competencies per transcript lets the factor structure be tested empirically rather
than assumed. With one scenario per competency, competency-level consistency is
estimated across pressure points within the encounter (and across variations between
participants) rather than across parallel scenarios.

## Mapping RM sub-competencies to the r/antiwork evidence base

From the topic model (`reddit-analysis/ANALYSIS.md`, 99,347 posts) and keyword mining:

| RM competency | Grounding topics (share) | Recurring real situations |
|---|---|---|
| **Influence** | pay/raises (6.8%), RTO (6.1%), interviews (7.0%); ~3.2% match influence patterns | negotiating a raise (often w/ outside offer), pitching schedule/hybrid arrangements, pushing back on policy |
| **Conflict management** | boss conflict (9.9%), firings/HR (6.9%), manager stories (7.6%); ~4.8% match conflict patterns | confrontations with coworkers/bosses, blame disputes, escalation threats, tense resignations |
| **Inspirational leadership** | layoffs/company events (5.9%), burnout (15.2%); ~4.7% match leadership patterns | demoralized teams after cuts, low-wage supervisors squeezed between management and staff, unpopular changes |
| **Teamwork** | manager/coworker stories, hours/shifts (7.0%); ~0.6% explicit but pervasive as subtext | uneven workload, covering shifts, credit-taking, group deliverables |

Design rule from the ethics note: scenarios are **composites of themes** — never
verbatim or identifiable stories.

## Scenario matrix (Study 1: 4 scenarios × 2–3 variations, per IRB overview)

One voice-based scenario per competency (~7–12 min each; every participant completes
all four). Each has 2–3 substantive variations (assigned across participants) plus
surface randomization (names, industry, channel). Full specs:
`reddit-analysis/scenarios/scenario-specifications.md` (+ per-scenario YAMLs).

| ID | Competency | Participant role | Encounter | Variations |
|---|---|---|---|---|
| S1 | Conflict mgmt | peer | An instigating colleague urges a public call-out; the participant instead confronts the offending peer directly and works through defensiveness to resolution | A taken credit · B hostile after-hours message · C public blame |
| S2 | Influence | employee (upward) | Negotiation with a sympathetic but constrained manager who deploys realistic deflections; leverage must be used without becoming a threat | A raise + outside offer · B hybrid under RTO · C team resources |
| S3 | Insp. leadership | interim team lead | Leading a demoralized 3-person team (cynic, disengaged star, anxious junior) through a meeting and 1:1s after a morale shock | A resignations over pay · B commission cuts · C mandated change |
| S4 | Teamwork | peer (4-person group) | Group working session with a dominating teammate and a quietly excluded one; ends with credited work allocation | A rollout plan · B client presentation · C backlog triage |

Assignment rule: S4 always involves misattributed credit, so S1 runs as Variation B or
C in the same session (avoids construct bleed between Conflict Management and Teamwork).

Instead of separate empathy/org-awareness cue lists, each scenario defines 4–5
**pressure points** — pre-defined moments the AI steers toward, each mapped to the
ESCI items it makes observable, with skill-shown / skill-missed anchors. SA cues are
embedded in the partner personas (e.g. Jordan's silence, Casey's challenge) and in
planted organizational facts.

## How this feeds the rest of the pipeline

- **Agents (Phase 2):** persona + `empathy_cues` become the agent's emotional script —
  the agent must *withhold* cooperation until the participant demonstrates the target
  competency (scripted resistance is what makes the measure discriminating).
- **Qualtrics (Phase 5):** the behavioral markers become the rating anchors. Each
  transcript is rated on all 4 RM + 2 SA competencies (primary competency analyzed as
  the scenario's target; the full matrix supports multitrait-multimethod analysis —
  2 scenarios per competency = internal consistency check).
- **Fine-tuning (Phase 6):** markers that correlate with high rater scores in Study 1
  define the steering objective for the agent models.
