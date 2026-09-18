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

 S3 / volume ─► pulled by the study team ─► raters in Qualtrics (ESCI)
                                              └─► gold labels ─► later phases
```

One **aligned record per encounter** in S3: video, audio, transcript, and
steering log under a single encounter id, so the modalities stay joined.

> **As deployed today, only the video half of that is true.** Webcam recordings
> go browser-direct to the study bucket; audio, transcript, events, and steering
> log are written to `/data` on the task, and nothing copies them to S3.
> `infra/terraform/ecs.tf` backs `/data` with an EFS filesystem, which makes
> those records survive a deploy, a crash, and task retirement — but that is
> Terraform source, not a fact about the running service, and it becomes true
> only once someone has run `tofu apply` against it. Check the live task
> definition before relying on it (`OPERATIONS.md`, "Read this before collecting
> anything"). Applied or not, EFS would be their only copy, and there is no
> retention or deletion path for them. See [`OPERATIONS.md`](OPERATIONS.md).

## The flows

1. CloudResearch Connect recruits and pays; Qualtrics issues the participant key
   and collects the WEIP baseline before the app opens.
2. Web app handles the camera and microphone check and the counterbalanced
   order of the four scenarios; state keyed by participant. Consent is taken in
   Qualtrics before the app opens, and the app holds no consent step.
3. Live voice conversation over a WebSocket to the session broker.
4. The broker relays participant audio to Gemini Live and streams agent audio
   back, holding the gateway key so it never reaches the browser.
5. Each turn, the director reads the transcript and emits one stage direction;
   the actor follows it on the next turn. Every direction is logged.
6. Webcam video uploads browser → presigned S3 URL, never transiting app servers.
   When this server cannot sign one — no AWS credentials, an unreachable or
   misconfigured bucket — the browser PUTs the recording to the app instead and
   it lands on disk beside the session. Same event, same key, same playback.
7. Recordings are pulled by the study team and rated in Qualtrics on the 22
   ESCI items (`studies/study1/qualtrics/`). Rating, reliability and modelling
   happen outside this application.

## Turn-taking is ours to implement

Gemini Live's native VAD is **not exposed through the LiteLLM bridge**:
`turn_detection` is accepted but inert, and without an explicit
`input_audio_buffer.commit` + `response.create` the model never replies. So the
broker owns end-of-turn detection (silence threshold on the participant stream)
and barge-in (dropping queued agent audio when the participant starts speaking).

This is the one capability the architecture slide assumes is free and is not.
Going direct to Google would restore it, at the cost of GCP credentials and
leaving the gateway. Exact working session config is in the migration plan.

Two consequences of owning it, both measured live and both in
`server/voice/realtime.py`: the silence threshold adapts to the room's noise
floor (a fixed one called a fan "speech" and barged the character in on
itself — false cut-offs 5 in 22 agent turns before, 0 in 19 after), and a reply
whose audio the gateway drops — mid-sentence, or before it starts — is detected
by comparing the audio delivered against the reply's own words and re-requested
once, with a text prompt the record shows (`audio_retry`). The second is a
recovery for an upstream fault, not a feature, and the retry prompt is a
methods question for the PI (`PI-DECISION-realtime-model.md`).

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
