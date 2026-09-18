# Field notes: what driving the live voice model found

Engineering record of the live measurements behind the runner's recovery
constants. Moved here from the README on 2026-09-17 when that page was rewritten
as an introduction; nothing below was changed in the move. All figures are from
live runs on `nto.gemini-live-2.5-flash` through the Cornell gateway.

## What the participant hears

Everything the study records was complete on the turns a participant heard cut
off, which is why none of this was caught by the suite until someone listened.
**Five** mechanisms have been found. Four are below; the fifth — a mid-thought
pause ending the participant's turn — was found on 2026-09-14 by driving the
live voice model with a hesitant participant rather than a cooperative script,
and is in the next section. All figures are from live runs on
`nto.gemini-live-2.5-flash` through the Cornell gateway and are recorded beside
the code they describe (`tests/test_speech_cutoff.py`,
`tests/test_audio_recovery.py`, `tests/test_live_realism_pipeline.py`,
`tests/test_lost_participant_round.py`, the constants block of
`server/voice/realtime.py`).

- **The end-of-turn detector adapts to the room.** A fixed threshold called a
  fan or keyboard clatter "speech", and speech while a character is talking is
  a barge-in that throws away every audio buffer the page had scheduled — the
  character stopped dead mid-sentence. The bar is now the quietest 20 ms of
  the last 2.5 s times a margin (`VAD_NOISE_MARGIN`, 3.0; capped at
  `VAD_MAX_THRESHOLD` so a loud room can never make the participant
  uninterruptible). Measured: false cut-offs 5 in 22 agent turns before, 0 in
  19 after; genuine interjections still cancelled the speaker 6 of 12 times
  after against 2 of 9 before.
- **Encounter teardown lets the audio finish.** The gateway delivers a reply
  faster than it plays, so when the server closes the last turn the browser is
  still holding seconds of it; the page used to close the audio graph on the
  spot and the last line of every encounter was cut mid-sentence. It now
  releases the camera and microphone immediately and drains the scheduled
  audio first, bounded by `AUDIO_DRAIN_MAX_S` (12 s; the longest single reply
  seen live was 6.5 s).
- **A floor move no longer amputates a reply mid-relay.** In a group room a
  reply that was already being heard was suppressed the instant the director
  moved the floor — 2 of 19 relayed group turns, one written to the record as
  "I just want to". A reply that had the floor when it started now finishes.
- **A reply whose voice the gateway drops is asked for once more.** Two
  upstream faults, neither ours: a reply's audio stops mid-sentence with the
  caption whole (about 1 reply in 9), or its words arrive and its voice never
  does (before: 45 s of dead air per stall, median 47.4 s, with the
  participant's own turns refused meanwhile). The bridge now notices both —
  audio under half of what the words need, or words with no voice for
  `AUDIO_ABSENT_S` = 8 s — and retries the turn **once**, replacing the broken
  line on the page. Measured on the final wave: 4 stalls in 145 replies, all
  1:1, each now 7.4–8.5 s of dead air instead of 47; 0 retries over a talking
  participant; 0 fires on `gpt-realtime-2.1` over 48 replies. **Two things to
  know before a wave.** First, where a reply's *audio* is the thing lost, the
  only recipe measured to revive it is a user text nudge into the model's
  context — `(I didn't hear that - the audio dropped. Could you say it
  again?)` — which never enters the participant transcript and is written on
  the `audio_retry` event, but the character's recovered line answers *it*,
  and a rater will hear that (on the four live stalls the re-spoken line was
  delivered whole every time and repeated the lost words 0 of 4 times). The PI
  has to rule on that before a wave; it is in the decision memo. Second, that
  nudge is **not** what answers a *request* the gateway ignores: the live
  round of 2026-09-14 measured a nudge in front of a lost line drawing an
  answer to the nudge rather than to the participant, so a request with
  participant audio behind it — every 1:1 turn — is now re-asked with the
  participant's **own audio** instead (`REQUEST_UNANSWERED_S` = 6 s, then
  `REPLAY_UNANSWERED_S` = 4 s and a session rebuild with the line replayed).
  The nudge remains only where there is no audio to replay: a truncated reply,
  a group room, or a request with no speech behind it.
- **Use a headset.** With loudspeakers, playback bleeding into the microphone
  can count as "the participant is talking" and withhold a retry — the cost is
  the old behaviour, not a new one — and it is also what the adaptive floor
  is measuring. `tools/encounter_health.py` prints every retried turn per
  encounter (retried / recovered / delivered whole / unrecovered), so a wave
  can be checked for how much of it the participant actually heard.

## Driving the live voice model with a hesitant participant (2026-09-14)

Every naturalness and parity figure this repository reported before that date
was measured on the **text** model, through `engine._system_prompt`. That proxy
never saw the audio path, the gateway's own turn detection, or its silent
socket deaths, and it was wrong about all three. Eleven encounters were then
driven on the configured realtime model with a hesitant participant —
hesitation, mid-thought pauses, one-word answers. Only live measurement
describes the live experience.

- **A mid-thought pause ended the participant's turn** — the fifth cut-off
  mechanism. A 700–1300 ms pause closed the turn twice over: the runner's own
  900 ms end-of-turn, and the gateway's default detection under it. *"I need
  more context. [900 ms] What do you mean?"* arrived as two transcripts, drew
  two replies, and the first was cut off by the second half of the
  participant's own sentence — which is also the doubled caption bubbles and
  the empty agent turns. Turn detection is now set at connect time to
  `server_vad` with a **1500 ms** silence window (probed and measured
  honoured: reply 2.51 s after speech end against 1.27 s bare; 3000 ms drew no
  `session.updated` at all), and the runner's own end of turn is raised to
  match, so it cannot be the thing that splits the turn.
- **The gateway died silently mid-encounter.** In every 1:1 encounter past
  roughly 90 s it stopped sending frames of any kind and dropped the TCP
  connection 22–38 s later with no close frame; every line spoken into it was
  lost and the page then asked the participant to repeat it. A request with no
  frame behind it is now called unanswered at `REQUEST_UNANSWERED_S` = 6 s
  (every healthy reply's first frame arrived inside 2.6 s) and re-asked with
  the participant's own audio; unanswered again at `REPLAY_UNANSWERED_S` = 4 s,
  the session is rebuilt and the line is replayed into it, bounded by
  `RECONNECT_LIMIT` = 2 rebuilds per encounter. **The cost is a pause**:
  measured over the seven live recoveries of the day, 9.2, 9.3, 11.8, 14.9,
  15.3, 19.2 and 21.7 s of quiet — **median 14.9 s**, roughly one per 90–120 s
  of talking — and then the character answers what the participant actually
  said. A request the gateway ignores no longer falls through to the 45 s
  watchdog; that watchdog now only backstops replies the runner did not
  request.
- **No character ever framed the meeting.** Each interaction's `opening:`
  travelled only by a mid-session `session.update`, which this family ignores
  (0 acks in 21 runs), so it was never spoken. It is now folded into the
  connect-time brief on any family whose row says steering is inert, and which
  route carried it is written to the record as `opening_framing`. The Influence
  manager went from a mean of 7.0 words a reply to 14.8, with a real opening line.
- **Group rooms are not runnable live on this gateway.** Across four live group
  encounters: 55 character turns, 11 of them empty, and 102 replies fired by a
  character that had not been given the floor — none of which happened once
  across seven 1:1 encounters (49 turns, 0 empty, 0 unfloored). In a room every
  member is fed the other characters' audio labelled as though it came from the
  participant. That is the room architecture on this gateway, not a brief, so
  no scenario rewrite reaches it. Demo and field 1:1 only on Gemini.


## Transcription quality

The live participant transcript comes from the realtime bridge's own
transcriber, which cannot be swapped — passing a transcription model to
`session.update` is accepted and silently ignored. It is good enough to steer
on, but it drops words, and on a **hesitant** speaker it drops a great many.

Measured on 2026-09-14 by aligning every hesitant line driven into the gateway
against the transcript it returned: of **39** lines, **26** came back word for
word and **13** did not — 3 lost the clause before the mid-line pause, 5 came
back mangled (*"Yeah. I'll work on it, I guess."* → *"io.me. I guess."*), and 5
came back with nothing recoverable in common with what was said, 3 of those
producing no participant transcript at all (39 lines in, 36 transcripts back).
**No tails were truncated**: the loss is at the head of a line, before a pause,
not at the end of it.

The character *hears* the audio and answers it correctly. What is damaged is
the record and the on-screen caption — so a rater reading a hesitant
participant's transcript is reading something lossier than what the character
heard, and any text coding of the participant channel inherits that loss.
Where the transcriber knows it failed it writes a literal `{}`, which is
scrubbed and flagged (`garbled`, or `user_turn_untranscribed` for a line that
was nothing else); where it silently drops a clause, nothing can flag it.

The recorded participant channel is therefore re-transcribed offline against a
stronger multimodal model, and stored *alongside* the live transcript rather
than replacing it: the live text is the record of what the agent actually heard
and reacted to, which is not the same thing as what was said.

```bash
python -m server.retranscribe <session_id>
python -m server.retranscribe --all
```
