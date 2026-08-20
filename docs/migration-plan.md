# Migration plan — v1 platform → Study Design v2 architecture

Written 2026-08-19 against *Study Design Proposal v2* and the AWS + Google
architecture slide. Describes the gap between what this repo runs today and what
Phase 1 data collection requires.

## Target (from the proposal)

```
Browser                         AWS                        Google
consent → WEIP → encounter      session broker             Gemini Live
  → completion code             ├ relays audio both ways   (speech-to-speech)
voice over WebSocket            ├ director steers actor    turn-taking +
webcam → direct upload          └ records a/v/transcript   interruptions native
                                     ↓
                                Study data (S3, encrypted)
                                one aligned record per encounter
```

Phase 1 target: 100 Prolific participants × 4 encounters (one per ESCI
competency, counterbalanced), 7–12 min each = 400 encounters with audio,
transcript, and video.

## What already matches

- **Director–actor loop.** `server/director.py` + `server/steering.py` already
  implement a director reading the transcript and injecting a stage direction
  per turn, with a JSONL steering log. This is the proposal's closed-loop
  steering requirement, already built.
- **Multi-agent sessions.** `server/multi_agent_session.py` plus the `g*`
  scenarios run rooms with several characters — required for S3 (three team
  members) and S4 (four-person group task).
- **Encounter UI.** `static/v2.html` is the Zoom-style participant view.
- **Scoring/debrief scaffolding.** `server/rubrics.py`, `scoring.py`,
  `debrief.py` are the seed of the Phase 3 scorer.

## Gaps, in dependency order

### 1. Voice layer — single-agent DONE, group mode pending

**Single-agent encounters now run on Gemini Live** (`server/voice/realtime.py`
+ `server/realtime_voice_session.py`), verified end to end through the real
`/ws/participant/voice` endpoint: participant audio in, input transcription,
agent audio and transcript out, both channels recorded, and the director
re-briefing the actor between turns. The browser protocol is unchanged, so the
existing UI needed no edits.

**Group scenarios still run the v1 cascade** (`server/voice/stt.py`,
`server/voice/tts.py`, `server/multi_agent_session.py`). That is the remaining
ElevenLabs/Deepgram dependency and the next thing to remove — S3 (three team
members) and S4 (four-person group task) both need it, so this blocks two of
the four study constructs.

The realtime session re-briefs the actor per turn via `update_instructions`,
which is also the mechanism a group runner would use to switch character and
voice between speakers.

### Verified working: `nto.gemini-live-2.5-flash` via Cornell LiteLLM

Confirmed end to end on 2026-08-19 with real speech: participant audio in,
input transcription, agent audio + transcript out, function calling. No Google
credentials needed — it runs on the Cornell gateway key.

**Connection**

- `wss://api.ai.it.cornell.edu/v1/realtime?model=nto.gemini-live-2.5-flash`
- Header `Authorization: Bearer <litellm-key>`
- The upgrade **requires HTTP/1.1** — over HTTP/2 the endpoint 404s.
- The WebRTC path (`/v1/realtime/client_secrets`) is not wired up for any model
  on this gateway. WebSocket is the only transport.

**Session config — keep it flat and minimal.** This is the trap that cost a day:
an over-specified `session.update` leaves the session alive but permanently
mute (`session.created` arrives, then nothing, forever — no error).

```jsonc
// works
{ "type": "session.update",
  "session": { "instructions": "...", "voice": "Puck",
               "tools": [ /* function defs */ ] } }
```

Do **not** send `modalities` / `output_modalities`, the nested GA
`audio: { input: {...}, output: {...} }` block, `input_audio_format`,
`output_audio_format`, or `input_audio_transcription`. Any of these silently
kills the session.

**Audio + turn-taking**

- Input: 16 kHz mono PCM16, base64, via `input_audio_buffer.append`.
- Output: `response.output_audio.delta` (base64 PCM) plus
  `response.output_audio_transcript.delta`.
- Input transcription is on by default —
  `conversation.item.input_audio_transcription.completed` arrives without asking.
- **Server VAD does not work through the bridge.** `turn_detection` is accepted
  but has no effect: without an explicit
  `input_audio_buffer.commit` + `response.create`, the model never responds.

That last point contradicts "turn-taking & interruptions built in" on the
architecture slide. Through LiteLLM they are not built in, so **the broker must
run its own end-of-turn detection** (silence threshold on the participant
stream) and drive commits. Barge-in likewise has to be handled locally by
dropping queued agent audio when the participant starts speaking. Budget for
this; it is the main piece the gateway does not give us for free.

**Alternatives.** `gpt-realtime-2.1` also works on the same gateway and *does*
provide server VAD natively — useful as a comparison or fallback.
`nto.gemini-live-2.5-flash-native-audio` exists but was not re-tested after the
config fix. Going direct to Google for `gemini-3.1-flash-live-preview` remains
an option later (it would restore native VAD), but is not needed to start
Phase 1 and would require GCP credentials that are not on this machine.

Keep the voice layer behind one provider interface so the model stays a config
choice.

Reference implementation of the working browser↔broker↔gateway transport —
24 kHz PCM16 capture, gapless playback, barge-in, two-sided recording — is at
`~/Desktop/AI_Interview/_deprecated/nextjs-prototype/app/` (`server.mjs`,
`src/lib/realtime/websocketAdapter.ts`). Port, don't rewrite.

### 1b. Group rooms — blocked on a gateway constraint

S3 and S4 need several characters live in one room (Research Note v3: S3-A opens
with a public challenge in a team meeting; S4 is "one live 4-person session").
Half the constructs depend on it.

**What works.** `server/realtime_voice_session.py` sequences a group turn: the
director picks an ordered speaker list, and each character takes the floor via
`update_instructions` with its own persona and Gemini voice. Verified: the
director routes correctly (`director_route ['jordan','sam']`), the first
character speaks in role, and its audio and transcript reach the participant.

**What does not.** Every speaker *after the first* times out. Through the
LiteLLM bridge a conversation appears to yield exactly **one response per
committed participant turn** — a second `response.create` produces no events at
all, and no error. Tried and ruled out:

- waiting for `response.done` before handing over the floor (the gateway rejects
  overlapping responses with `conversation_already_has_active_response`, so this
  is necessary but not sufficient)
- forcing the in-flight flag down after a timeout
- committing a short silent frame before the second `response.create`, to give
  the model fresh input to answer

Text conversation items are not an option either: injecting one closes the
socket with 1006.

**Recommended next step: one realtime session per character.** Open N
connections — one per agent, each permanently briefed as its own character with
its own voice — and have the broker fan participant audio out to all of them
while serialising which one is allowed to answer. That sidesteps the
one-response-per-turn limit entirely and removes the per-turn re-briefing
latency, at the cost of N concurrent sessions per encounter (relevant to the
gateway quota question in the cost estimate).

### 2. Scenarios → the four ESCI competencies

Today: 13 ad-hoc scenarios (`scenarios/*.yaml`) from earlier exploration —
`missed_deadlines`, `credit_taken`, `hidden_profile_vendor`, etc.

Target: exactly four constructs, 2–3 variations each, from
`reddit-analysis/scenarios/S{1..4}-*.yaml`:

| | Competency | Variation A |
|---|---|---|
| S1 | Conflict Management | Taken credit |
| S2 | Influence | Promised raise + competing offer |
| S3 | Inspirational Leadership | After resignations over pay |
| S4 | Teamwork | Planning an internal rollout |

The canonical specs are richer than the engine's schema — they carry
`ai_partners[]` (named roles + behavior policies), `fixed_opening_prompt`,
`pressure_points`, `focal_esci_items`, `duration_minutes`. The engine wants a
rendered `system_prompt` per character.

Work: a loader that compiles a canonical spec into runnable personas, so the
research spec stays the single source of truth rather than being hand-copied
into engine YAML. Several existing scenarios are close relatives of the
canonical ones (`04_credit_taken` ≈ S1-A) and can seed the persona text.

### 3. Storage → S3

Today: local `data/` and `logs/*.jsonl`.

Target: encrypted S3, one aligned record per encounter (video + audio +
transcript + steering log + participant key), with webcam video uploaded
directly from the browser via presigned URL.

Bucket exists: `rf-study-data-540586745717` (us-east-1, currently empty).
Credentials resolve through the AWS default chain — CLI profile locally, task
role on Fargate. No access keys in env files.

### 4. Participant flow → Connect/Qualtrics round trip

Today: `/v2?scenario=…` opens straight into an encounter.

Target (Phase 1 deployment flow): CloudResearch Connect → Qualtrics
(participant key + WEIP baseline) → simulation app (consent → 4 counterbalanced
encounters → completion code) → Qualtrics (app feedback).

Work: participant key as the join credential, consent + webcam permission gate,
counterbalanced scenario assignment persisted per participant, completion-code
issuance, and resumability across four 7–12 minute encounters.

### 5. Deployment → AWS

Today: `fly.toml` + Dockerfile.

Target: ECS/Fargate behind an ALB with HTTPS/WSS, per `infra/terraform/`.
DNS is wired in code — `infra/terraform/dns_tls.tf` now provisions one ACM cert
covering both study hostnames and alias records pointing at the ALB:

| Hostname | Role |
|---|---|
| `rf.ai-ready-workforce.ai.cornell.edu` | participant entrance (app + broker WSS) |
| `api.rf.ai-ready-workforce.ai.cornell.edu` | backend API |

The zone (`Z03157053G6CGLIYWMAH4`) is delegated to Route 53, so this is
self-service. The records do not exist yet because they are ALB aliases — they
come into being with the first `terraform apply`, which also validates the
certificate via DNS. Nothing in this Terraform is deployed yet (no state, no
ACM certs; the `aiw-staging` ALB in the account is a separate environment).

## Open items for the team

- **RCT sizing conflicts between documents.** The RCT Study Flow slide shows
  N=300 in three arms (self-reflection / feedback without fine-tuning /
  feedback with fine-tuning). The Phase 4 text specifies 100 new participants in
  two arms (≈50 each). The cost estimate assumes 650 production encounters,
  while Phase 1 alone is 400. These need reconciling before budget or IRB
  amendments are filed.
- **Gemini Live availability.** `gemini-3.1-flash-live-preview` needs GCP
  project binding and a confirmed quota for concurrent live sessions before it
  can carry data collection.
- **Video consent + retention** must be settled before Phase 1 (flagged as an
  open question in the proposal).
