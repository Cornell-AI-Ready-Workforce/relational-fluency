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

| | Competency | Runnable default | Parallel form |
|---|---|---|---|
| S1 | Conflict Management | **B** — Hostile after-hours message | (A — Taken credit: see the rule below) |
| S2 | Influence | A — Promised raise + competing offer | B — Hybrid under an RTO mandate |
| S3 | Inspirational Leadership | A — After resignations over pay | B — After a commission cut |
| S4 | Teamwork | A — Planning an internal rollout | B — Preparing a client presentation |

Both variations of each construct are authored and run: the compiled bank is
`S1A S1B S2A S2B S3A S3B S4A S4B` in [`scenarios/v3/`](scenarios/v3/).

**S1 variation A is not assignable in a normal session.** Every participant
does all four constructs, and S4 always turns on misattributed credit — which
is also what S1-A is about. Serving both in one session lets the Conflict
Management and Teamwork measures bleed into each other, so the canonical spec
([`reddit-analysis/scenarios/scenario-specifications.md`](reddit-analysis/scenarios/scenario-specifications.md),
"Variation assignment") requires S1 **B** or **C** whenever S4 is present. C is
not yet compiled into `scenarios/v3/`, so B is the S1 form the study runs.
S1-A stays in the bank for the single-scenario researcher links, where no S4 is
in play.

> **Enforced in the sampler.** `runs.create` still draws each construct's form
> independently, which on its own would pair S1-A with an S4 form in about half
> of all runs. The draw is therefore corrected once the whole run is known:
> `FORM_EXCLUSIONS` in [`server/runs.py`](server/runs.py) declares the rule as
> data — `("conflict_management", "A", "teamwork")` — and any run that drew both
> has its S1 form swapped to B before the run is written. The correction is
> recorded on the run document as `form_exclusions` rather than applied
> invisibly, because an analyst comparing forms across participants will
> otherwise see the B forms over-sampled for no stated reason. So there is
> nothing for an operator to filter or discard; if you want to check it anyway,
> `form_exclusions` on a run says what was swapped and why.

An encounter is a sequence of **consecutive 1:1 conversations** — S1, for
example, is the instigating colleague first, then the peer. Characters never
share a turn.

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

Open (to be updated):

- Participant, Zoom-style multi-agent: <http://127.0.0.1:8765/v2?scenario=hidden_profile_vendor>
- Participant, single-agent text: <http://127.0.0.1:8765/?scenario=missed_deadlines>
- Participant, single-agent voice: <http://127.0.0.1:8765/?scenario=missed_deadlines&mode=voice>
- Researcher view: <http://127.0.0.1:8765/researcher>
- Steering dashboard: <http://127.0.0.1:8765/director>

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

### Reading the steering trail

`/director` shows, per encounter, the participant's turns interleaved with each
actor's replies and — immediately above each reply — the stage direction that
produced it, with the planted trigger and ESCI items it was firing. Scene
headings mark where the interaction changed, so it is clear which character was
being steered.

It aligns to the scenario bank: each encounter is labelled by construct,
variant, and parallel form; scene headings use the interaction names from the
research note ("Hallway run-in with Sam") with what to observe; and coverage is
reported against the instrument — planted triggers reached out of those the
scenario specifies, and ESCI items exercised out of the construct's full set,
with unreached ones greyed.

This is the audit view for the closed-loop steering the study claims: a reply
and the instruction behind it are shown together rather than in separate logs,
and an encounter that only exercised part of the instrument is visible as such.

Operational commands — health, logs, pulling data off the server — are in
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).

### Transcription quality

The live participant transcript comes from the realtime bridge's own
transcriber, which cannot be swapped — passing a transcription model to
`session.update` is accepted and silently ignored. It is good enough to steer
on, but it drops words.

The recorded participant channel is therefore re-transcribed offline against a
stronger multimodal model, and stored *alongside* the live transcript rather
than replacing it: the live text is the record of what the agent actually heard
and reacted to, which is not the same thing as what was said.

```bash
python -m server.retranscribe <session_id>
python -m server.retranscribe --all
```

### Checking a capture

```bash
python -m server.verify_record <session_id>   # one encounter
python -m server.verify_record --all          # a whole collection wave
```

Reports whether both transcript sides and both audio channels are present and
non-trivial, whether the steering trail was logged and paired to replies, how
many planted triggers fired out of the scenario's plan, how many distinct ESCI
items were exercised, and whether the record can say which gateway produced it.
Run it during the pilot and on a sample during collection — an encounter missing
participant audio is cheap to catch on day one and impossible to recover later.

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

## Phase 2 — human rating and reliability

The human ICC/κ that benchmark is measured against comes from here. Two to three
independent raters score every recorded encounter on all 22 ESCI Relationship
Management items, and reliability is computed per construct **before** anything
is modelled. Full operational guide: [`docs/RATING.md`](docs/RATING.md).

> **The ESCI items are a proprietary instrument**, reproduced in this repository
> for research reference only, and **licensing for this use is not confirmed in
> writing**. The warning is attached to them in the console, in the exported item
> bank, and in the instrument doc. Resolve it before fielding.

Raters work in a console this platform serves, at `/rate`:

```
/rate?token=rt_…      the rater's own link — queue, video, transcript, 22 items
```

- **What they rate** is the webcam recording, which carries the mixed
  conversation audio, with the aligned transcript beside it. The per-channel
  WAVs cannot be played back as a conversation; an encounter whose video upload
  failed is rated from the transcript alone, and the console says so.
- **What they never see** — a rater holds a scoped `rt_` token, never the
  session key, and the packet is blinded: the situation the participant saw, the
  counterparts, the video and the transcript, and none of the participant key,
  the stage directions, the planted triggers, their ESCI tags, the actor briefs,
  or the judge's score. Encounters are identified to a rater by an opaque rating
  code. Another rater's assignment answers 404, not 403.
- **N/A is a real answer**, stored as `null` and never as a number. Nothing is
  pre-selected, and submit is refused until all 22 items carry a 1–5 or an N/A.

```bash
# researcher side, with the usual SESSION_KEY
curl -sX POST "$RF/api/raters?key=$KEY"            -d '{"name":"…","kind":"trained"}'
curl -sX POST "$RF/api/raters/$RID/token?key=$KEY" -d '{"days":30}'   # shown once
curl -sX POST "$RF/api/rater-assignments?key=$KEY" -d '{"cohort":"study","rater_ids":[…],"per_encounter":3,"seed":1}'
curl -s      "$RF/api/ratings?cohort=study&key=$KEY"       # the export
python -m server.reliability --cohort study                # ICC(2,1)/ICC(2,k), weighted κ, α
```

Statistics are pure Python — no numpy, no scipy, no new runtime dependency for a
22 × 27 matrix. The Qualtrics route stays open (`esci_items.csv` imports, and
`ratings.import_qualtrics` ingests an export), but the console is the working
path, because the platform cannot get answers back out of Qualtrics without a
survey and an API round trip that do not exist.

## Model configuration

Every model client is built in [`server/llm.py`](server/llm.py) from explicit
configuration — never from ambient environment variables. This matters more than
it sounds: the desktop app exports `ANTHROPIC_BASE_URL=https://api.anthropic.com`,
and `load_dotenv()` does not override variables that already exist, so a
gateway key was being sent to the wrong provider and every director call
returned 401. For a measurement instrument, the endpoint that served an
encounter has to be deliberate and recorded, not inherited from a shell.

Consequences of that rule:

- `.env` wins over ambient environment for gateway settings.
- Startup runs a preflight against the configured gateway and prints what it
  ignored; `/health` reports the same, so a misconfigured endpoint is visible
  before anyone joins rather than as a 401 mid-encounter.
- The resolved gateway and model names are written into each session's
  `record.json` under `provenance`.

```bash
curl -s localhost:8765/health | python3 -m json.tool
```

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
