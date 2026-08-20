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

Each construct has **two variations**; variation B is being authored. An
encounter is a sequence of **consecutive 1:1 conversations** — S1, for example,
is the instigating colleague first, then the peer. Characters never share a
turn.

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
| Voice | **Gemini Live speech-to-speech** via Cornell LiteLLM — done | — |
| Models | **Cornell LiteLLM gateway** (Gemini) — done | — |
| Scenarios | 13 exploratory YAMLs in `scenarios/` | 4 constructs × 2 variations, compiled from canonical specs |
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
- **Consecutive 1:1 segments** — a scenario's cast is played one character at a
  time, in order. The actor calls `end_conversation` when its segment reaches a
  natural close, and the runner re-briefs the live session as the next
  character with a different voice.
- **The encounter record** — every session writes to
  `data/sessions/{session_id}/`:

  | File | What it holds |
  |---|---|
  | `record.json` | the aligned record: transcript with participant and agent turns in order, each agent turn paired with the stage direction that shaped it, plus provenance (gateway + models), audio/video pointers, and counts |
  | `events.jsonl` | raw append-only trail — every turn, latency, knob change, director route, and stage direction |
  | `user_audio.wav` | participant channel |
  | `assistant_audio*.wav` | agent channel, one per character |
  | `manifest.json` | session metadata and durations |

  `record.json` is the analysis-facing view, built from `events.jsonl` at close;
  raters, the scorer, and Phase-3 training read it rather than replaying events.
  A turn with `stage_direction: null` ran unsteered — distinguishable from a
  direction that went unrecorded.

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

**v0.5** — voice encounters run on Gemini Live speech-to-speech through the
Cornell LiteLLM gateway; ElevenLabs and Deepgram are gone from the codebase
entirely. Scenarios play as consecutive 1:1 conversations. Researcher steering
with per-agent knobs and notes. Dataset capture with separate participant and
agent WAVs, SQLite index, and manifest. Offline scoring across the constructs.

Next, in dependency order (see the migration plan): replace the voice cascade
with Gemini Live plus broker-side turn detection; compile S1–S4 into runnable
multi-agent sessions; move storage to S3; add the Connect/Qualtrics round trip;
deploy to Fargate.

Known gaps: the legacy `g*` scenarios were authored as group rooms and read
oddly when played 1:1 — they are superseded by the S1–S4 bank. Photo tiles use
initials placeholders.
