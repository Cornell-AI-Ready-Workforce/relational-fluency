# Director–actor agent (ElevenLabs custom LLM)

> **Superseded transport (2026-08).** The director–actor *method* below is
> current and central to the study. Its packaging is not: this service answers
> `POST /v1/chat/completions` as a custom LLM for ElevenLabs Agents, which is
> retired. The live implementation is `server/director.py` + `server/steering.py`
> in the platform, driving Gemini Live through the session broker. The lasting
> value here is the persona text and director policies in `scenarios.py`.
> These two implementations should converge — see
> [`../../../../docs/architecture.md`](../../../../docs/architecture.md).

Closed-loop steering for the simulation encounters (Research Note v2): a cheap
**director** model reads the transcript each turn, classifies the conversation state
against the scenario's pressure points and yield conditions, and injects a one-line
stage direction; the **actor** model plays the character with that direction appended
to its system prompt. Every direction is logged to `logs/<conversation>.jsonl` — the
steering trail is part of the study's audit record.

```
participant audio ── ElevenLabs (STT, turn-taking, TTS)
                          │  POST /v1/chat/completions (OpenAI format)
                          ▼
                  server.py ── director.py (state + stage direction)
                          │           │
                          ▼           ▼
                     actor reply   logs/*.jsonl
```

## Try it locally (text only, no voice)

```bash
cd agents && pip install -e ".[dev]"
export ANTHROPIC_API_KEY=sk-ant-...
python -m agents.director_actor.simulate --show-director
```

`--show-director` prints the director's state and stage direction before each Morgan
reply — use this to tune `scenarios.py` before touching voice.

## Wire into ElevenLabs

1. Run the server: `uvicorn agents.director_actor.server:app --port 8100`
2. Expose it: `ngrok http 8100` (or deploy)
3. In the ElevenLabs agent settings:
   - **LLM → Custom LLM**: server URL = the ngrok URL, model = anything (ignored)
   - **First message**: the scenario's fixed opening line (see `scenarios.py`) — the
     verbatim opening lives in ElevenLabs, not in the prompt
   - **System prompt**: can be left minimal; the server supplies the persona
   - **Dynamic variables**: pass `participant_name` / `company_name` per conversation
4. Max call duration ~15 min; enable transcript + audio retention (raters score from
   these).

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `SCENARIO_ID` | `S2A` | which scenario in `scenarios.py` |
| `ACTOR_MODEL` | `claude-sonnet-4-5` | the character |
| `DIRECTOR_MODEL` | `claude-haiku-4-5` | the controller |
| `STEERING_LOG_DIR` | `logs` | steering-trail JSONL output |

## Design rules

- The director never talks to the participant; it only directs the actor.
- The actor never sees the yield-condition rubric — only the persona and the current
  direction. This keeps the character natural while the policy stays enforced.
- Fine-tuning is NOT part of this loop: the agent is the measurement instrument and
  stays frozen during data collection. If prompt+director steering proves
  insufficient in the pilot, a LoRA-tuned actor can be served behind this same
  endpoint — decided before Phase 1 launch, then frozen.
- Adding scenarios = adding an entry to `scenarios.py` (persona + director policy +
  first message). S3/S4 (multi-party) will need persona multiplexing — one actor call
  per character or a single actor voicing labeled characters; decide during the pilot.
