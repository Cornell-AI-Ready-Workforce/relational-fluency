# Architecture

Cost detail: `RelationalFluency_AWS_Cost_Estimation.pdf`.
Migration state and verified gateway findings: [`migration-plan.md`](migration-plan.md).

> **Superseded approach.** Until 2026-08 this document described voice
> encounters running on **ElevenLabs Agents** with a custom-LLM callback to a
> director–actor service, calling Anthropic through LiteLLM. ElevenLabs and
> Deepgram are retired, and models now go through the Cornell LiteLLM gateway.
> The design below replaces it.

## Overview

Voice encounters run as a **single speech-to-speech session** with Gemini Live,
reached through the Cornell LiteLLM gateway. **AWS** hosts the participant web
app, the session broker, and all study data.

```
Participant browser (via CloudResearch Connect → Qualtrics)
 ├─ web app UI ──────────────► ALB ─► Web app + session broker (Fargate)
 ├─ mic/speaker over WSS ◄──► session broker
 │                              ├─ relays audio both ways
 │                              ├─ end-of-turn detection (see note)
 │                              ├─ director: transcript → one stage direction/turn
 │                              │    └─► LiteLLM → Gemini Flash (text)
 │                              ├─► LiteLLM → Gemini Live (speech-to-speech, the actor)
 │                              └─► S3: audio, transcript, steering log
 └─ webcam (MediaRecorder) ── presigned upload ────────► S3 recordings

 S3 ─► CloudFront (signed URLs) ─► raters ─► Qualtrics (ESCI items)
                                     └─► gold labels ─► scorer + feedback models
                                                          └─► Phase-4 RCT
```

One **aligned record per encounter** in S3: video, audio, transcript, and
steering log under a single encounter id, so the modalities stay joined.

## The flows

1. CloudResearch Connect recruits and pays; Qualtrics issues the participant key
   and collects the WEIP baseline before the app opens.
2. Web app handles consent, webcam permission, and counterbalanced assignment of
   the four scenarios; state keyed by participant.
3. Live voice conversation over a WebSocket to the session broker.
4. The broker relays participant audio to Gemini Live and streams agent audio
   back, holding the gateway key so it never reaches the browser.
5. Each turn, the director reads the transcript and emits one stage direction;
   the actor follows it on the next turn. Every direction is logged.
6. Webcam video uploads browser → presigned S3 URL, never transiting app servers.
7. Raters stream recordings via CloudFront signed URLs and score 22 ESCI items
   in Qualtrics.
8. Ratings → reliability gates (ICC/κ) → scorer and feedback model training →
   Phase-4 RCT.

## Turn-taking is ours to implement

Gemini Live's native VAD is **not exposed through the LiteLLM bridge**:
`turn_detection` is accepted but inert, and without an explicit
`input_audio_buffer.commit` + `response.create` the model never replies. So the
broker owns end-of-turn detection (silence threshold on the participant stream)
and barge-in (dropping queued agent audio when the participant starts speaking).

This is the one capability the architecture slide assumes is free and is not.
Going direct to Google would restore it, at the cost of GCP credentials and
leaving the gateway. Exact working session config is in the migration plan.

## Key decisions

- **The agent is the measurement instrument.** It is frozen during collection;
  the only variation comes through the director loop. Fine-tuning applies to the
  scorer and feedback models, never to the encounter agent mid-study.
- **Fixed opening beat per scenario** for comparability; the agent improvises
  within its behavior policy afterwards.
- **Canonical scenario specs** live in `reddit-analysis/scenarios/S{1..4}-*.yaml`
  and are compiled into runnable personas, rather than being hand-copied into
  engine YAML.
- ElastiCache omitted at study scale.

## Known duplication

There are currently **two director–actor implementations**: `server/director.py`
plus `server/steering.py` (live, used by the running platform) and
`agents/src/agents/director_actor/` (built for the retired ElevenLabs
custom-LLM callback). These should converge on the `server/` one; `agents/`
retains value mainly for its persona text and scenario policies.
