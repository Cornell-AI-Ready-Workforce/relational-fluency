# Roadmap

Phases follow *Study Design Proposal v2*. The earlier six-phase plan (organized
around building `app/` and a separate `/chat` agent service) is superseded —
that app was removed when the simulation platform became the repo root, and the
agent service was written for the retired ElevenLabs callback.

## Objectives

1. **Establish that relational fluency can be reliably measured** from simulated
   encounters — human raters agree with each other per construct (ICC/κ).
2. **Show an algorithm can score at human-level reliability** — model–human
   agreement matching or beating human–human agreement per construct.
3. **Test whether machine feedback causally improves performance** — targeted,
   transcript-grounded feedback vs. self-reflection on a second attempt.

## Phase 0 — Platform migration (in progress)

See [`migration-plan.md`](migration-plan.md) for detail and dependency order.

- [x] Verify a speech-to-speech model through the Cornell gateway
      (`nto.gemini-live-2.5-flash`, working config recorded)
- [ ] Replace the Deepgram → Claude → ElevenLabs cascade with Gemini Live,
      including broker-side end-of-turn detection and barge-in
- [ ] Compile canonical S1–S4 specs into runnable multi-agent sessions
- [ ] Move study data to encrypted S3, one aligned record per encounter
- [ ] Consent → WEIP handoff → counterbalanced encounters → completion code
- [ ] Deploy to ECS/Fargate behind ALB; create `rf` / `api.rf` DNS records and
      issue the ACM certificate (independent of app work — start early)
- [ ] Pilot n=5–10 to tune agent difficulty and confirm 7–12 min elicits signal

## Phase 1 — Collect encounters

100 participants × 4 encounters (one per construct, counterbalanced) = **400
encounters** with audio, transcript, and video.

- [ ] IRB amendment covering video capture, consent language, and retention
- [ ] Recruitment via CloudResearch Connect; WEIP baseline in Qualtrics
- [ ] Monitor completion, dropout, and encounter length during collection

## Phase 2 — Human gold labels

- [ ] 2–3 independent raters per video; 22 ESCI items in Qualtrics
- [ ] Open-ended "what could this person have done better?" for the feedback model
- [ ] ICC / weighted κ per construct **before** modeling; refine or drop
      low-agreement dimensions
- [ ] Small expert-rated subsample (~40) to anchor crowd quality — Kumar et al.
      found crowdworkers systematically inflate ratings vs. experts

## Phase 3 — Train the scorer and feedback model

- [ ] Two approaches: interpretable features (LIWC, politeness, prosody,
      turn-taking) + regularized model; and LLM-as-judge with few-shot expert
      examples
- [ ] Evaluate with weighted κ / ICC against gold labels, benchmarked to
      human–human reliability
- [ ] Feedback model outputs 2–3 quotable moments + one concrete alternative move
      each; human review of a sample before it reaches participants

## Phase 4 — RCT

- [ ] Attempt 1 → feedback (treatment) vs. self-reflection (control) → attempt 2
- [ ] Primary outcome: Δ score attempt 2 − attempt 1; human raters confirm on a
      subsample
- [ ] Matched time-on-task across arms so the contrast is feedback content
- [ ] Mixed model with condition × attempt, scenario as a factor

**Unresolved:** the RCT Study Flow slide specifies N=300 across three arms
(self-reflection / feedback without fine-tuning / feedback with fine-tuning),
while the Phase 4 text specifies 100 new participants in two arms of ~50. The
cost estimate assumes 650 production encounters against 400 in Phase 1. Settle
before budget and IRB filings.

## Open questions (from the proposal)

- Raters: crowd, trained crowd, or experts?
- Rate from video, or transcript + audio only (cheaper, less privacy burden)?
- Phase 4 within- or between-subjects?
- Same scenario twice, or a matched parallel form (rehearsal vs. transfer)?
- Are these the right four constructs?
