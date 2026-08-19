# Architecture

Interactive diagram: `architecture-diagram.html` (also in the Cowork artifact gallery).
Cost detail: `RelationalFluency_AWS_Cost_Estimation.pdf`.

## Overview

Voice encounters run on **ElevenLabs Agents** (STT, turn-taking, TTS); **AWS** hosts
the participant web app, the director–actor steering endpoint, and all study data.

```
Participant browser (Prolific worker)
 ├─ web app UI ───────────────► ALB ─► Web app (Fargate) ─► RDS (PIDs, assignment)
 ├─ ElevenLabs voice widget ◄─► ElevenLabs Agents
 │                                  └─ per turn ─► ALB ─► Director–actor (Fargate)
 │                                                   ├─► LiteLLM → Anthropic
 │                                                   │    (Haiku director, Sonnet actor)
 │                                                   └─► S3 steering logs
 └─ webcam (MediaRecorder) ── presigned upload ────────► S3 recordings
                                                          ▲
 Post-call worker: ElevenLabs audio+transcript ──────────┘ (keyed by conversation_id)

 S3 ─► CloudFront (signed URLs) ─► raters ─► Qualtrics (ESCI items)
                                     └─► gold labels ─► scorer + feedback models
                                                          └─► Phase-4 RCT
```

## The ten flows

1. Prolific sends the participant with PID; completion code returns at the end.
2. Web app handles consent, session, scenario/variation assignment (state in RDS).
3. Live voice conversation (WebRTC) between participant and the ElevenLabs agent.
4. Each turn, ElevenLabs calls our custom-LLM endpoint through the ALB.
5. Director (Haiku) classifies conversation state → one-line stage direction;
   actor (Sonnet) speaks the next line. See `agents/src/agents/director_actor/`.
6. Every stage direction is logged to S3 — the steering audit trail.
7. Webcam video uploads browser → presigned S3 URL (never transits app servers/NAT).
8. Post-call worker pulls ElevenLabs audio + transcript via API into S3, keyed by
   `conversation_id` so video / audio / transcript stay aligned per encounter.
9. Raters stream recordings via CloudFront signed URLs.
10. Qualtrics ratings → reliability gates (ICC/κ) → scorer & feedback model training.

## Key decisions (see docs/decisions/ and project notes)

- The agent is the measurement instrument: frozen during collection; steering happens
  only through the director loop. Fine-tuning applies to the scorer/feedback models.
- ElastiCache omitted at study scale; ngrok is pilot-only (ALB in production).
- Fixed First Message lives in ElevenLabs config; personas and director policies in
  `agents/src/agents/director_actor/scenarios.py`.
