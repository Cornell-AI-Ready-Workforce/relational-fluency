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
| Director (`DIRECTOR_MODEL`): who speaks next in rooms, every participant turn | `3.1-flash-lite`: p50 1.1–1.3 s, 0 timeouts | p50 ~3 s; **3 of 22 calls timed out** into the cast[0] fallback; names one speaker only | p50 5.3 s, p90 8.7 s; 2/12 timeouts; one speaker only; least faithful routing | `3.5-flash-lite`: p50 1.06 s, 0 timeouts, names several speakers; two scope slips in 16 calls | **Keep.** `3.5-flash-lite` is the only candidate, pending a larger replay through `_run_group_turn` |
| Steering (`STEERING_MODEL`): between-turn persona shifts | `3.1-flash-lite`: 1.1 s, but shifts on most neutral turns and contradicts itself run to run; one ignored-tool reply | **5/16 timed out** (~11 s silence each) | best judgement, p50 3.1 s, tail 9–11 s of silence | `3.5-flash-lite`: 0.8 s, consistent, follows "no change on most turns" (may be too cautious) | **Researcher decision.** Steering is the study's manipulated variable, so change it only at a wave boundary, recorded |
| Text engine (`CLAUDE_MODEL`): text-mode actor, provenance | `3.1-flash-lite`: 0.8 s to first token | **every reply cut mid-sentence** at the 400-token cap (hidden reasoning); ~4 s even uncapped | 6.7 s to first token | none | **Keep** |
| Offline re-transcription (`TRANSCRIBE_MODEL`), `server/retranscribe.py`, `tools/recover_from_video.py` | `2.5-pro`: 15–19 s per 7-min session | same fidelity, 25% faster, no phantom lines; "Diane" for Dan without names; one empty-response crash | **cannot**: gateway 400 on audio input | – | **Switched to `3.8-flash`** with cast names in the prompt (Dan correct in the live re-check) and an empty-response retry |

Common to all three audio models: on 20 s of pure digital silence each one
invented speech at least once (e.g. "Thank you. Bye-bye."). The re-transcriber
needs an energy gate before audio is sent. That work belongs to issue #21.

## Not in scope here

The speech-to-speech model (`REALTIME_MODEL`: `gpt-realtime-2.1`,
`gpt-realtime-2`, `gpt-realtime-2.1-mini`, `nto.gemini-live-2.5-flash-native-audio`)
and live participant transcription (`whisper-1`) are evaluated under issues
#22 and #21.
