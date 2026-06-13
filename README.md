# Relational Fluency Platform

Scenario-based conversational AI for measuring relational fluency in voice interactions.
Cascade pipeline: **Deepgram STT → Claude → ElevenLabs TTS**, with researcher controls for
live steering of the model.

## Architecture

```
Browser (participant)         Browser (researcher)
  mic + speaker                 transcript + knobs
        │                              │
        └──────── WebSocket ───────────┘
                       │
                  FastAPI server
        ┌──────────────┼──────────────┐
   Deepgram STT     Claude         ElevenLabs TTS
                  (swappable
                   model)
```

## Quick start

```bash
cd ~/relational_fluency
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in API keys
python -m server.app
```

Open:
- Participant **v1 text**:  <http://127.0.0.1:8765/?scenario=missed_deadlines>
- Participant **v1 voice**: <http://127.0.0.1:8765/?scenario=missed_deadlines&mode=voice>
- Participant **v2 (Zoom-like)**: <http://127.0.0.1:8765/v2?scenario=hidden_profile_vendor>
- Researcher view: <http://127.0.0.1:8765/researcher>

Voice requires `DEEPGRAM_API_KEY` and `ELEVENLABS_API_KEY` in `.env`. Use Chrome
or Safari on `127.0.0.1` (mic permission requires a secure context — localhost
counts; remote IPs need HTTPS).

## Concepts

- **Scenarios** (`scenarios/*.yaml`) — situation, AI role, system prompt, persona defaults,
  branch checkpoints, voice + model choice.
- **Persona knobs** — warmth, formality, agreeableness, verbosity, etc. Each knob maps to a
  prompt fragment that gets composed into the system prompt.
- **Live steering** — researcher can inject inline notes ("be more skeptical"), adjust knobs,
  or trigger a scenario branch mid-conversation. Notes appear in the *next* model turn's
  system prompt and are logged.
- **Logs** — `logs/{session_id}.jsonl` with every turn, latency, knob change, steering note.

## Voice pipeline

```
Browser AudioWorklet (16 kHz PCM)
   ↕ WebSocket binary
Deepgram (nova-3, utterance_end_ms=1200) → final transcript
   → Claude streaming (system prompt composed fresh each turn)
      → ElevenLabs WS multi-stream-input (pcm_16000)
         → audio chunks → browser playback queue
```

While the assistant is speaking, the mic is muted (v1 — no barge-in yet). When the
last TTS chunk finishes playing, the mic re-opens automatically.

## V2 — Zoom-like multi-agent

`/v2?scenario=<id>` is the multi-agent voice UI: static photo tiles, mic-level on
the participant tile, active-speaker highlight on whoever's talking.

Three group scenarios ship:
- `hidden_profile_vendor` — 3 agents (Devi, Marcus, Priya). Hidden-profile
  decision dynamics from Stasser & Titus (1985).
- `blameful_retro` — 4 agents (Jordan, Sam, Ali, Rae). Post-incident review;
  Edmondson on psych safety + speaking up after failure.
- `dominated_brainstorm` — 4 agents (Victor, Lena, Theo, Mei). Hybrid meeting
  with hierarchy + coalition + remote asymmetry; Greer et al. (2018), Diehl &
  Stroebe (1987), MS Research CHI 2021.

Turn-taking is **director-routed**: after each user turn, a small Claude
(Haiku) call picks 0–3 speakers in order with optional per-speaker intent.
Speakers run sequentially through their AgentEngine (each agent's own persona,
voice_id, system prompt) and stream out via ElevenLabs. Per-agent audio files:
`assistant_audio_{agent_id}.wav`.

V1 single-agent scenarios still work at `/` exactly as before — backward compat
preserved by normalizing legacy YAMLs into a 1-element cast internally.

## Scoring (relational-skill measurement)

An offline judge measures the participant's relational skill from a saved
transcript, against the construct rubric for that scenario's `skill`. Two layers
in one structured Claude pass: per-turn **behavioral codes** (tagged + counted
into rates) and construct-level **1-5 rubric dimensions** with anchors, evidence
quotes, an overall score, strengths, and growth edges. Codebook + anchors live
in `server/rubrics.py`, grounded in `docs/REFERENCES.md`.

```bash
python -m server.scoring <session_id>          # score one (cached to score.json)
python -m server.scoring --all --force         # re-score everything
```

Judge model defaults to `claude-opus-4-8` (override via `--model` or
`JUDGE_MODEL`). Scoring never runs in the live conversation path. The researcher
view has a **Score session** panel (rubric bars, behavior chips, evidence), and
`POST/GET /api/sessions/{id}/score` expose it over HTTP. Result is written to
`data/sessions/{sid}/score.json` alongside the other artifacts.

Note: the researcher-page score panel is currently hidden. To bring it back,
set `SHOW_SCORE = true` in `static/researcher.html`. The CLI and HTTP
endpoints stay active regardless.

## Status

v0.4 — single-agent + multi-agent voice both work. 12 scenarios (8 single-agent
1:1, 4 group). Full researcher steering with per-agent knobs and notes. Dataset
captures separate user + per-agent assistant WAVs + SQLite index + manifest.
Offline scoring layer over the four relational-fluency constructs (above).

Known TODOs:
- Barge-in (interrupt assistant mid-turn)
- Real photos instead of initials placeholders
