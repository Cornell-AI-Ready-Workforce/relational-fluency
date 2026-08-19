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

### 1. Voice layer — the blocking item

Today: Deepgram STT → Claude → ElevenLabs TTS (`server/voice/stt.py`,
`server/voice/tts.py`). A cascade, retired by decision.

Target: a single speech-to-speech session with native turn-taking and
barge-in.

**Findings (verified 2026-08-19, Cornell LiteLLM gateway):**

| Model | Result |
|---|---|
| `gpt-realtime-2.1` | Works fully — speech in, transcription, audio + transcript out, server VAD |
| `nto.gemini-live-2.5-flash` | Session opens, then silence. No response events. |
| `nto.gemini-live-2.5-flash-native-audio` | Same — non-functional |

Transport notes: the WebRTC path (`/v1/realtime/client_secrets`) is not wired up
on the gateway; the working path is a **WebSocket** at
`/v1/realtime?model=…`, and the upgrade requires HTTP/1.1 (HTTP/2 returns 404).

**Consequence:** Gemini Live must be reached **directly on GCP credit**, as the
architecture slide shows — not proxied through Cornell LiteLLM. This needs
Google credentials, which are not present on this machine (no `gcloud`, no ADC,
no API key).

Design the voice layer behind one provider interface so the model is a config
choice: `gemini-3.1-flash-live-preview` (target), `gpt-realtime-2.1` (verified
fallback via LiteLLM, useful for pilots while GCP access is arranged).

Reference implementation of the working browser↔broker↔gateway transport —
24 kHz PCM16 capture, gapless playback, barge-in, two-sided recording — is at
`~/Desktop/AI_Interview/_deprecated/nextjs-prototype/app/` (`server.mjs`,
`src/lib/realtime/websocketAdapter.ts`). Port, don't rewrite.

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
DNS is ready — Cornell delegated `ai-ready-workforce.ai.cornell.edu` to
Route 53; `rf` and `api.rf` records still need creating, and an ACM cert
issued (do this early; it gates HTTPS and is independent of app work).

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
