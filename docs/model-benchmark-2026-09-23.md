# Text-model benchmark, 2026-09-23

Question: should the platform's text models move to the newer models the
Cornell LiteLLM gateway now serves (`nto.gemini-3.5/3.6/3.7/3.8-flash`,
`nto.gemini-3.5-flash-lite`, `gpt-6-astra`)? The researcher's preferred
upgrade was `nto.gemini-3.8-flash`.

Method: each role was driven through the repo's own code path, with the
production prompt, tool schema, parser and timeouts. Only the model was
overridden. Inputs were real turns from the 2026-09-22/23 sessions. The
director numbers were re-measured independently and reproduced. Scripts and
raw results are in the session scratchpad
(`modelbench/`, `modelbench-gpt/`). The samples are small (6–16 calls per
model per role), so treat rates as indicative.

## Results

| Role (env var) | Current | nto.gemini-3.8-flash | gpt-6-astra | Best measured alternative | Decision |
|---|---|---|---|---|---|
| Director (`DIRECTOR_MODEL`): who speaks next in rooms, every participant turn | `3.1-flash-lite`: p50 1.1–1.3 s, 0 timeouts | p50 ~3 s; **3 of 22 calls timed out** into the cast[0] fallback; names one speaker only | p50 5.3 s, p90 8.7 s; 2/12 timeouts; one speaker only; least faithful routing | `3.5-flash-lite`: p50 1.06 s, 0 timeouts, names several speakers; two scope slips in 16 calls | **Switched to `3.5-flash-lite` on 2026-09-23** after a larger replay (below) |
| Steering (`STEERING_MODEL`): between-turn persona shifts | `3.1-flash-lite`: 1.1 s, but shifts on most neutral turns and contradicts itself run to run; one ignored-tool reply | **5/16 timed out** (~11 s silence each) | best judgement, p50 3.1 s, tail 9–11 s of silence | `3.5-flash-lite`: 0.8 s, consistent, follows "no change on most turns" (may be too cautious) | **Switched to `3.5-flash-lite` on 2026-09-23** (researcher's decision). Steering is the manipulated variable: every encounter now records `provenance.steering_model` (and `director_model`), so sessions before and after the switch can be told apart |
| Text engine (`CLAUDE_MODEL`): text-mode actor, provenance | `3.1-flash-lite`: 0.8 s to first token | **every reply cut mid-sentence** at the 400-token cap (hidden reasoning); ~4 s even uncapped | 6.7 s to first token | `3.5-flash-lite` trialled (below): shorter, but performs fewer planted beats | **Keep**; no live encounter uses it today |
| Offline re-transcription (`TRANSCRIBE_MODEL`), `server/retranscribe.py`, `tools/recover_from_video.py` | `2.5-pro`: 15–19 s per 7-min session | same fidelity, 25% faster, no phantom lines; "Diane" for Dan without names; one empty-response crash | **cannot**: gateway 400 on audio input | – | **Switched to `3.8-flash`** with cast names in the prompt (Dan correct in the live re-check) and an empty-response retry |

Common to all three audio models: on 20 s of pure digital silence each one
invented speech at least once (e.g. "Thank you. Bye-bye."). The re-transcriber
needs an energy gate before audio is sent. That work belongs to issue #21.

## Not in scope here

The speech-to-speech model (`REALTIME_MODEL`: `gpt-realtime-2.1`,
`gpt-realtime-2`, `gpt-realtime-2.1-mini`, `nto.gemini-live-2.5-flash-native-audio`)
and live participant transcription (`whisper-1`) are evaluated under issues
#22 and #21.

## Follow-up trial: `nto.gemini-3.5-flash-lite` for the director and the text engine

A larger replay, with every result independently rechecked on a re-run 15% subset.

**Director.** Every archived group-room participant turn went through `Director.route()`, and each routed list was then passed through the runner's own speaker pick. That is 87 calls per model; the six contested turns were sampled 3–5 times each.

| | `3.1-flash-lite` | `3.5-flash-lite` |
|---|---|---|
| latency p50 / p90 | 1.11 / 1.43 s | 1.00 / 1.19 s |
| timeouts, fallbacks, invalid output | 0 | 0 |
| same list on repeat (6 turns × 3) | 1/6 | 4/6 |
| **told Priya to raise or re-argue the withheld rollout fact** | **11/77** | **0/77** |
| named a character only to tell them to stay silent | 0 | 3/77 (now dropped in code: `director_silence_intent_dropped`) |
| S4A voices (real 09-23 turns) | Dan 52 · Priya 24 · Chris 24 % | Dan 70 · Priya 20 · Chris 10 % |
| rationale missing | 1/61 | 12/61 |

The director's directions reach the actors. In the 09-23 S4A session, one such Priya direction was followed at once by her revealing the fact unasked. **Switched** (researcher's decision, 2026-09-23); `DIRECTOR_MODEL=nto.gemini-3.1-flash-lite` restores the previous model.

**Text engine.** About 30 real states from S1–S4, 2 samples each. `3.5` is shorter (≈25 vs ≈30 words) and slightly faster. It performed fewer planted beats (18 vs 24 of 36), leaked Priya's withheld fact when unasked (2/4 vs 0/4), and once read a director note aloud. The trial also found that no live code path generates speech with `CLAUDE_MODEL`: voice encounters use the realtime model, so the setting only labels the record and the researcher's picker. **Kept.**
