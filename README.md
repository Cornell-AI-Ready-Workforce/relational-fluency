# Relational Fluency Platform

Research platform for the Cornell AI-Ready Workforce Initiative: participants
hold voice conversations with AI characters in workplace scenarios, and the
recorded encounters are used to measure **relational fluency**.

Study design: *Study Design Proposal v2* — Lee, Chun, Zhang, Slama, Joachims,
Kizilcec.

## What we measure

Four competencies from the ESCI **Relationship Management** cluster. Canonical
scenario specs live in [`reddit-analysis/scenarios/`](reddit-analysis/scenarios/),
grounded in an analysis of 39,301 r/antiwork posts.

| | Competency | Variation A |
|---|---|---|
| S1 | Conflict Management | Taken credit |
| S2 | Influence | Promised raise + competing offer |
| S3 | Inspirational Leadership | After resignations over pay |
| S4 | Teamwork | Planning an internal rollout |

**Phase 1 target:** 100 participants × 4 encounters (7–12 min, counterbalanced)
= 400 encounters with audio, transcript, and video.

## Target architecture

```
Browser (participant)              AWS                      Google
consent → WEIP → encounter    session broker            Gemini Live
  → completion code           ├ relays audio both ways  (speech-to-speech)
voice over WebSocket          ├ director steers actor
webcam → direct upload        └ records a/v/transcript
                                        ↓
                              Study data (S3, encrypted)
                              one aligned record per encounter
```

The **director–actor** split is the core method: the *actor* is the voice agent
the participant talks to; a separate *director* reads the transcript in real
time and injects one stage direction per turn, so the character follows a
behavior policy rather than drifting. Every direction is written to a steering
log alongside the audio and transcript.

## Migration status

The repository is mid-migration from the v1 stack to the architecture above.
Read [`docs/migration-plan.md`](docs/migration-plan.md) before starting work —
it carries the verified gateway findings, including a session-config trap that
silently breaks Gemini Live sessions.

| Area | Now | Target |
|---|---|---|
| Voice | Deepgram STT → Claude → ElevenLabs TTS (cascade) | Gemini Live speech-to-speech via Cornell LiteLLM |
| Models | Anthropic API direct | Cornell LiteLLM gateway |
| Scenarios | 13 exploratory YAMLs in `scenarios/` | Compiled from canonical S1–S4 specs |
| Storage | local `data/` + `logs/` | encrypted S3, one aligned record per encounter |
| Entry | direct `?scenario=` links | CloudResearch Connect → Qualtrics → app → completion code |
| Deploy | Fly.io | ECS/Fargate behind ALB (`infra/terraform/`) |

Verified 2026-08-19: `nto.gemini-live-2.5-flash` works end to end through the
Cornell gateway — audio in, transcription, audio + transcript out, function
calling. Server VAD is *inert* through that bridge, so the broker must do its
own end-of-turn detection and barge-in. Details and the exact working session
config are in the migration plan.

## Quick start

```bash
cd ~/relational_fluency
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in API keys
python -m server.app
```

Open:

- Participant, Zoom-style multi-agent: <http://127.0.0.1:8765/v2?scenario=hidden_profile_vendor>
- Participant, single-agent text: <http://127.0.0.1:8765/?scenario=missed_deadlines>
- Participant, single-agent voice: <http://127.0.0.1:8765/?scenario=missed_deadlines&mode=voice>
- Researcher view: <http://127.0.0.1:8765/researcher>

Mic capture needs a secure context — `127.0.0.1` counts, remote hosts need
HTTPS. Chrome or Safari.

## Concepts

- **Scenarios** (`scenarios/*.yaml`) — situation, cast, system prompts, persona
  defaults, branch checkpoints, voice and model choice. Single-agent YAMLs are
  normalized internally into a one-element cast, so v1 and v2 share one engine.
- **Persona knobs** — warmth, formality, agreeableness, verbosity. Each maps to
  a prompt fragment composed into the system prompt at turn time.
- **Live steering** — researchers inject inline notes ("be more skeptical"),
  adjust knobs, or trigger a scenario branch mid-conversation. Notes enter the
  next turn's system prompt and are logged.
- **Turn routing (multi-agent)** — after each participant turn, a small model
  call picks 0–3 speakers in order with optional per-speaker intent; each runs
  through its own engine with its own persona and voice.
- **Logs** — `logs/{session_id}.jsonl` records every turn, latency, knob change,
  and steering note. Session artifacts (participant WAV, per-agent WAVs,
  transcript, manifest) land under `data/sessions/{session_id}/`.

## Scoring

An offline judge scores a saved transcript against the rubric for that
scenario's construct: per-turn behavioral codes plus construct-level 1–5
dimensions with anchors, evidence quotes, strengths, and growth edges. Codebook
and anchors are in `server/rubrics.py`, grounded in `docs/REFERENCES.md`.

```bash
python -m server.scoring <session_id>     # cached to score.json
python -m server.scoring --all --force    # re-score everything
```

Scoring never runs in the live conversation path. Results are written to
`data/sessions/{sid}/score.json` and exposed at
`GET`/`POST /api/sessions/{id}/score`. The researcher page has a score panel,
currently hidden behind `SHOW_SCORE = true` in `static/researcher.html`.

This offline judge is the seed of the Phase 3 scorer, which will be benchmarked
against human ICC/κ on the ESCI items rather than these interim rubrics.

## Privacy

Participant audio, video, and transcripts are PII. `data/`, `logs/*.jsonl`,
`*.wav`, and `.env` are gitignored and must stay that way. Study data belongs in
the encrypted S3 bucket under the IRB data-management plan, never in the repo.

## Status

**v0.4** — single-agent and multi-agent voice both work on the v1 cascade.
13 scenarios (8 single-agent, 5 group). Researcher steering with per-agent knobs
and notes. Dataset capture with separate participant and per-agent WAVs, SQLite
index, and manifest. Offline scoring across the relational-fluency constructs.

Next, in dependency order (see the migration plan): replace the voice cascade
with Gemini Live plus broker-side turn detection; compile S1–S4 into runnable
multi-agent sessions; move storage to S3; add the Connect/Qualtrics round trip;
deploy to Fargate.

Known gaps: no barge-in yet (mic is muted while the assistant speaks); photo
tiles use initials placeholders.
