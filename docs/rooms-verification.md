# Group rooms: live verification (Study 1, E3.1)

**Result: the S3A and S4A rooms run on the current build, on the model the
study fields, and better than on the reference build.** Ten rooms on `HEAD`,
six on `53bc440`, all on `nto.gemini-live-2.5-flash-native-audio`, 2026-09-17.

## How

A scripted participant drove `/ws/participant/voice` the way the page does:
16 kHz PCM, room noise when silent, a synthesized line (macOS `say`), then it
listened until every audio chunk had played, waited for a second speaker, and
spoke again. Eight on-brief lines in the first interaction, `advance`, four in
the second. Counts come from `events.jsonl` and `record.json`. The driver and
analyzer are not in the repository; the session ids are in the run data.

## Counts

| build | scenario | rooms | agent turns | empty | no reply | cut-offs | lines heard /12 | beats fired | opener spoken | context notes in captions |
|---|---|---|---|---|---|---|---|---|---|---|
| HEAD | S3A | 5 | 104 | 7 | 0 | 2 | 59/60 | 15/15 | 5/5 | 8 |
| HEAD | S4A | 5 | 121 | 5 | 1 | 4 | 58/60 | 20/20 | 5/5 | 0 |
| 53bc440 | S3A | 3 | 45 | 5 | 11 | n/r | 32/36 | n/r | 0/3 | 0 |
| 53bc440 | S4A | 3 | 57 | 4 | 29 | n/r | 36/36 | n/r | 0/3 | 3 |

- *empty*: a character turn with no caption (audio with nothing transcribed,
  or a narrated stage note). *no reply*: a granted floor that produced nothing.
  *cut-offs*: a reply cut before it played out. *n/r*: not recorded by that
  build.
- The reference build never opens the scene (it waits for the participant) and
  loses about one granted turn in four to an empty response. `HEAD` opens every
  room with the lead's line, fires every planted beat, and loses one turn in
  225.
- The `HEAD` cut-offs are a second speaker starting 3–5 s after the first
  finished, as the participant began talking. Real participants will meet this.
- The `HEAD` S3A context notes were characters captioning (and twice speaking)
  the room's private notes in forms the stripper missed. Fixed in
  `study1/native-audio-model` (PR 18); two rooms re-run on the fix show 0
  surviving notes, with narration-only replies recorded as stage directions
  instead of lines.

## Open

- **A room does not recover when the gateway drops every socket at once.** The
  1:1 path reconnects; the room records `group_turn_no_members` and stays
  silent until the participant leaves. Seen once on each build when the laptop
  slept mid-run; a network blip would do the same.
- About one turn per room has audio and no caption; the offline
  re-transcription covers the participant channel, not this.
- A character sometimes narrates ("(Casey stays silent.)") instead of speaking;
  the caption is now dropped, the audio is what it is.
