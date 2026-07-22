# Roadmap

Six phases. Phases 1–3 can proceed in parallel; 4–5 depend on 1–3; 6 depends on Study 1 results.

## Phase 1 — Simulation app (`app/`)

Build the participant-facing web app that delivers AI simulation encounters.

- [ ] Define encounter flow (consent → instructions → simulation chat → post-task survey handoff)
- [ ] Chat UI for real-time conversation with the agent
- [ ] Session logging: full transcripts with timestamps, participant ID (Prolific PID pass-through)
- [ ] Completion codes / redirect back to Prolific
- [ ] Deployment target decision (Vercel + hosted agent API, or single host)

## Phase 2 — Conversational agent(s) (`agents/`)

Create the conversational AI agent(s) embedded in the app.

- [ ] Agent service API (FastAPI, `/chat` endpoint consumed by the app)
- [ ] Persona / scenario prompting system — one agent per scenario, configurable
- [ ] Guardrails: stay in character, turn limits, safety
- [ ] Transcript storage schema shared with `app/`

## Phase 3 — Scenario grounding via Reddit analysis (`reddit-analysis/`)

- [ ] Identify target subreddits and collection approach (API / Pushshift alternatives / existing corpora)
- [ ] Ethics check: Reddit data use policy + IRB stance on public data
- [ ] Qualitative/computational analysis → taxonomy of relational situations
- [ ] Author simulation scenarios (`reddit-analysis/scenarios/`) consumed by `agents/`

## Phase 4 — Recruitment (`studies/`)

- [ ] IRB approval
- [ ] Prolific: Study 1 participants (interact with simulations)
- [ ] Recruit raters
- [ ] Prolific: second participant set (validation / Study 2)
- [ ] Prescreening criteria, payment, attention checks

## Phase 5 — Ratings via Qualtrics (`studies/*/qualtrics/`)

- [ ] Rating instrument: dimensions of relationship-management skill
- [ ] Qualtrics survey (transcript presentation + rating scales)
- [ ] Rater training / calibration materials
- [ ] Inter-rater reliability plan (ICC / Krippendorff's alpha)

## Phase 6 — Fine-tuning & steering (`finetuning/`)

Steer agent models toward eliciting/recognizing good relationship-management skills, based on Study 1 findings.

- [ ] Extract behavioral markers of high-rated relationship management from Study 1
- [ ] Build preference / instruction datasets from rated transcripts
- [ ] Fine-tune or steer (SFT / DPO / prompt-level steering — decide per model access)
- [ ] Evaluate steered agents with the second participant set
