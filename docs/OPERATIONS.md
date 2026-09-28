# Operations cheat sheet

Checking the server is healthy and the data is intact. Every command here has
been run against the live stack.

Set these once per shell, from the repository root.

bash / zsh (macOS, Linux, Git Bash):

```bash
export RF=https://rf.ai-ready-workforce.ai.cornell.edu
export KEY=$(aws secretsmanager get-secret-value \
  --secret-id relational-fluency/agent-api-key --query SecretString --output text)
```

Windows PowerShell:

```powershell
$RF = "https://rf.ai-ready-workforce.ai.cornell.edu"
$KEY = aws secretsmanager get-secret-value --secret-id relational-fluency/agent-api-key --query SecretString --output text
```

Windows Command Prompt:

```bat
set RF=https://rf.ai-ready-workforce.ai.cornell.edu
for /f %i in ('aws secretsmanager get-secret-value --secret-id relational-fluency/agent-api-key --query SecretString --output text') do set KEY=%i
```

## Reading the rest of this page on Windows

Every block below is written for bash. `export` exists in neither Windows
shell, and three substitutions make the rest work in Windows PowerShell — they
are the only three you need:

1. **Write `curl.exe`, not `curl`.** In Windows PowerShell 5.1 — the version
   that ships with Windows 11, and what `powershell` launches — `curl` is an
   *alias* for `Invoke-WebRequest`, which has no `-s`. It fails with
   `Invoke-WebRequest : Cannot process command because of one or more missing
   mandatory parameters: Uri.`, which reads like a broken URL and sends you
   after the wrong bug entirely. `curl.exe` is the real curl and accepts every
   flag used on this page. (PowerShell 7 dropped the alias; `curl.exe` is
   correct in both, so use it always.)
2. **`$RF` and `$KEY` interpolate inside double quotes exactly as in bash**, so
   the bodies of the `curl` lines are otherwise unchanged.
3. **Line continuations**: bash's trailing `\` is a backtick `` ` `` in
   PowerShell and `^` in cmd. Simplest is to join the line into one.

In cmd.exe, additionally: variables are `%RF%` and `%KEY%`, and single-quoted
JSON bodies must be rewritten with escaped double quotes
(`-d "{\"name\":\"…\"}"`), because cmd does not treat `'` as quoting at all and
passes the apostrophes straight through to the server, which then rejects the
body as malformed JSON.

`python3` deliberately does not appear in these blocks. Inside an activated
virtualenv, `python` is the project interpreter on all three platforms; on
Windows there is no `python3.exe` from a python.org install, and the Windows
App Execution Alias of that name either opens the Microsoft Store or resolves
to the system interpreter rather than your virtualenv — so a `python3 -m
server.<module>` generalised from one of these lines fails with a confusing
`ModuleNotFoundError`.

---

## Read this before collecting anything

**Pull any encounter you care about, and do it before the next deploy** (see
[Getting data off the server](#getting-data-off-the-server)). That is the
standing rule. Everything below is why it is still the rule.

**Since 17 September 2026 the task has a persistent volume.** `tofu apply`
created EFS file system `fs-09e2d30bae3ce9239` with two mount targets and an
access point, and registered revision **41** of `relational-fluency-agent`,
which carries volume `study-data` mounted at `/data` and `DATA_DIR=/data`; the
service rolled over to it at 23:45 and `/health` stayed green. Records now
survive a deploy, a crash and task retirement. Before that (revisions 35–40)
the task carried `volumes=[]` and no EFS file system existed, so every record
died with the task; any encounter recorded on those revisions that was not
pulled is gone. Every closed encounter is also archived to the study bucket
(`server/archive.py`) once an image carrying that code is deployed.

Ask the running service rather than trusting either this page or that one — it
is one command and it is the only answer that counts:

```bash
TD=$(aws ecs describe-services --cluster relational-fluency --services platform \
      --query "services[0].taskDefinition" --output text)
aws ecs describe-task-definition --task-definition "$TD" \
  --query "taskDefinition.[volumes,containerDefinitions[0].mountPoints]"
# an EFS volume + a /data mount  → what revision 41 returns: records survive a
#                                  deploy, a crash, and task retirement.
# [[],[]]                        → volumes=[] and no mount points: an ephemeral
#                                  task (revisions 35–40). Pull before every
#                                  deploy if you ever see this again.
```

```powershell
$TD = aws ecs describe-services --cluster relational-fluency --services platform --query "services[0].taskDefinition" --output text
aws ecs describe-task-definition --task-definition "$TD" --query "taskDefinition.[volumes,containerDefinitions[0].mountPoints]"
```

The two outputs differ by a bracket, which is exactly why it is worth knowing
what you are looking at: `[[],[]]` is two empty lists — no volume declared on
the task, no mount point on the container — and anything else is a volume and a
mount, printed in full.

If one day that command shows the volume and the mount, pulling data stops being
a race against the next rollout and becomes redundancy. It does not become
optional, because nothing downstream of the write exists yet:

- **Nothing archives to S3.** Only the webcam video goes to the study bucket
  (uploaded browser-direct by `server/video.py`). Session audio, transcripts,
  events, and manifests live on one filesystem and nowhere else — no second
  copy, no backup policy, no snapshot schedule. A deleted access point, a
  fat-fingered `tofu destroy`, or a corrupted write takes the only copy of an
  irreplaceable encounter with it.
- **There is no deletion path.** No retention rule and no per-participant erase,
  so a withdrawal request under the study's data-management plan has to be carried
  out by hand on the volume. Know that before you promise a participant one.
  The bucket half of that request is harder than it looks: the study bucket is
  versioned, so an ordinary delete leaves the bytes behind as a noncurrent
  version. See [Deleting one participant's
  recording](#deleting-one-participants-recording).

```bash
aws s3 ls s3://relational-fluency-study-data/ --recursive | head   # video only; no session records
```

### Which of the two places is this wave's video in?

Now that AWS credentials exist, a recording can be in either of two places and
the encounter looks identical from the outside. One event field says which.
`via: "local"` on the `video_uploaded` event means the browser could not get a
presigned URL and PUT the bytes to the app instead, so they are on the task's
own disk under `sessions/<id>/webcam.webm` — which is EFS if the volume check
above passed, and ephemeral if it did not. No `via` at all means the bytes went
browser-direct to the bucket.

`$KEY` is the researcher key set at the top of this page. `/health` withholds
the `credentials`, `bucket`, `region` and `detail` fields from an
unauthenticated caller, so a keyless read of the storage block will not tell you
which of the failures below you are looking at.

```bash
curl -sS "https://rf.ai-ready-workforce.ai.cornell.edu/health?key=$KEY" \
  | python -c "import json,sys; print(json.load(sys.stdin)['storage'])"
aws s3 ls s3://relational-fluency-study-data/encounters/ --recursive --summarize | tail -3
```

```powershell
curl.exe -sS "https://rf.ai-ready-workforce.ai.cornell.edu/health?key=$KEY" | python -c "import json,sys; print(json.load(sys.stdin)['storage'])"
aws s3 ls s3://relational-fluency-study-data/encounters/ --recursive --summarize | Select-Object -Last 3
```

A wave whose `storage.ok` is true and whose object count is far below the
encounter count has been falling back silently: check the events for `via`, and
pull those recordings off the task before the next deploy. `storage.ok` false
means **every** recording in that wave is on the task's disk, and the countdown
is the next rollout.

Neither state loses a recording by itself. The difference is entirely about
what survives a deploy.

### The third state, which loses the recording: CORS

There is a failure that produces neither of the two states above, and it is the
one worth memorising because nothing anywhere names it.

The browser PUTs the recording straight to S3, so the bucket's **CORS
allowlist** decides whether that PUT is allowed to leave the page at all. The
allowlist has exactly three origins
(`infra/terraform/storage_secrets.tf`):

```
https://rf.ai-ready-workforce.ai.cornell.edu
http://localhost:8765
http://127.0.0.1:8765
```

`8765` is the app's default `PORT`, spelled twice because a browser treats
`localhost` and `127.0.0.1` as different origins. **Local webcam testing works
on port 8765 and on no other port.** Serve the app on 8000 with AWS credentials
present and the sequence is: presigning succeeds, the browser refuses the PUT at
its preflight, and no recording is made.

What makes it expensive is how it is reported. A CORS refusal is not an HTTP
status the page can see — the fetch simply throws — so `static/v2.html` records
the reason as `network`. And `network` is deliberately **not** one of the
reasons that trigger the local upload fallback (`presigningIsUnavailable`
admits only `presign_http_5xx` and `presign_no_url`, because a `network` reason
usually means this very server was unreachable and re-sending 45 MB to the same
host would not end differently). So the bytes go nowhere, the fallback is not
attempted, and no log line on either side says "CORS".

Two consequences for an operator:

- **Running locally: use port 8765**, or add your origin to the allowlist before
  you test. If every local recording is "failing to upload", check the port
  before anything else.
- **Changing the participant-facing hostname is a bucket change too.** A new
  origin that is not in the allowlist loses every recording in the wave, with
  the encounters otherwise looking perfect. `via` will not tell you — there is
  no `video_uploaded` event to carry it.

### Deleting one participant's recording

The bucket is **versioned**, which is right for research data and wrong for the
sentence in the consent form that offers a participant deletion. An ordinary
`aws s3 rm` writes a delete marker: the object stops being listed and the bytes
stay, as a noncurrent version, indefinitely. Honouring a withdrawal takes a
version-aware delete of the encounter's key, plus the local copy if that
encounter fell back, plus the session directory on `/data` — which holds the
audio and the transcript and is not in S3 at all. There is no script for this
yet; do it by hand, confirm each of the three, and write down what was removed.

---

## Which scenarios a participant gets

Phase 1 uses variant A only: every study run is S1A, S2A, S3A, S4A, in an
order taken from a balanced 4×4 Latin square (Williams design, `WILLIAMS_4` in
`server/runs.py`): participants are assigned the square's rows in rotation per
cohort, so each construct sits in each position equally often and follows each
other construct equally often. The row is recorded on the run as
`order.row` and exported by `/api/runs`. This is the code default
(`DEFAULT_RUN_VARIANT=A`); set it to `B` to pin the other form or `random`
for a per-construct coin flip. An explicit `variant=` on an internal test
link still overrides it, and the RCT's second attempt always flips forms.

Transcription is hinted to English on every route (`TRANSCRIPTION_LANG=en`;
blank to disable) and the actors are told to speak English regardless of
what they think they heard.

Two more knobs on the gpt realtime route (`server/voice/realtime.py`), both
read per session and both written to every record's provenance
(`input_transcription_model`, `max_output_tokens`), so a change shows up in
the data without anyone having to remember when it was made:

- `INPUT_TRANSCRIPTION_MODEL` — the live participant transcriber. Default
  `gpt-4o-transcribe` since pipeline `2026-09-23b` (issue #21); set
  `whisper-1` to go back. Only the gpt route sends it; the Gemini routes
  transcribe on their own and ignore this.
- `REALTIME_MAX_OUTPUT_TOKENS` — the per-reply output-token cap, audio
  included (~20 tokens per second of voice). Default `1200` (about a minute
  of speech), a runaway guard only; blank or `0` sends no cap. The 380 used
  from 2026-09-18 cut replies mid-word at 10.5-14 s (issue #23). A value
  that is not a positive integer is ignored and the default stands.

Participant-turn integrity (pipeline `2026-09-23c`, issues #21 and #24;
`server/voice/realtime.py`, `_record_user_turn` in
`server/realtime_voice_session.py`). Every value below is written to each
record's provenance under `turn_gate`, and every transcript a rule withholds
is written as a `user_turn_suppressed` event with its text and `reason`
(`probe_pad`, `no_speech`, `replay_duplicate`), never discarded:

- A silence probe (the 1:1 watchdog's handoff and beat probes) clears the
  gateway buffer before its pad and commit, and the pad's transcript is
  suppressed as `probe_pad`. Not a knob: that transcript is of nobody.
- `PARTICIPANT_DROP_VOICED_MS` — default `80`. A transcript of nothing but
  punctuation or fillers ("." / "Um..." / "Mhm.") over at most this much
  voiced audio is suppressed as `no_speech`. `-1` turns the drop off.
- `PARTICIPANT_MIN_VOICED_MS` — default `600`. A turn with less voiced audio
  than this is still recorded and captioned, tagged `low_confidence` (on the
  `user_turn` event and in the record), and kept out of the steering review;
  the director sees it only when it names a cast member. `0` tags nothing.
- `INPUT_BUFFER_RESTART` — default `1`. On the participant's first
  `speech_started` after a commit, the gateway's input buffer is cleared and
  the last `INPUT_PREROLL_MS` (default `600`) re-sent, so a turn is no longer
  transcribed together with everything the microphone sent since the last
  one (`input_buffer_cleared`). gpt route only (1:1 and the room's scribe);
  never while a turn end is being confirmed or a room turn awaits its
  transcript. `0` restores the old behaviour.
- `PARTICIPANT_DEDUPE_OVERLAP` — default `0.9`, the share of the shorter
  line's words that makes two transcripts within 5 s one utterance on a room
  route with a second transcriber (was 0.6). Only native-audio rooms have
  one: from `2026-09-23g` the filter does not run on gpt rooms, whose scribe
  is the only transcriber (`turn_gate.room_dedupe_second_source`). The 0.9
  bar has not been measured against native-audio member-vs-scribe pairs;
  check it before a native-audio pilot.
- `PARTICIPANT_LOW_CONFIDENCE_DIRECTOR` — default `named`: a
  `low_confidence` turn reaches the room director only when it names a cast
  member. `all` gives the director every one of them (a real "No." is about
  350 ms of voice); the steering review skips them either way. From
  `2026-09-23g`.
- `ROOM_MERGE_QUEUED_TURNS` — default `1`: utterances spoken while a room's
  floor is held are routed together when it frees
  (`user_turns_merged_for_routing`); each stays its own `user_turn`. `0`
  routes on the latest alone. Tracked by `room_pacing_version` `2026-09-23b`.

Voiced audio is counted per commit against the runner's VAD bar at that
moment; on the Gemini routes the gateway commits on its own, `voiced_ms` is
null and none of the voiced-audio rules apply.

Participant audio goes to the gpt route at 24 kHz (the page captures 16 kHz
and the server resamples). Before pipeline `2026-09-23b` it went at 16 kHz
and the gateway read it as 24 kHz, so every earlier gpt encounter's actor and
live transcriber heard the participant 1.5x fast; use the offline
re-transcription for those. Under `2026-09-23b` itself the 24 kHz reached only
sessions built with an explicit model, which the runner and the room do not
do, so a `2026-09-23b` gpt encounter was still sent 16 kHz even though its
provenance says 24000; the rate follows each session's model from
`2026-09-23c`. The native-audio route was affected the same way: its runner-
and room-built sessions sent 16 kHz raw before `2026-09-23c` (provenance on
`23a`/`23b` says 24000 and `audioop.ratecv`, which is wrong) and 24 kHz
resampled from `23c`. The gateway reads that route at 24 kHz as well
(measured 2026-09-24: 157 input audio tokens at 24 kHz against 108 for the
same audio at 16 kHz), so earlier native-audio encounters were also heard
1.5x fast.

> **What A-only does to the S1/Teamwork rule.** `FORM_EXCLUSIONS` in
> `server/runs.py` bars **S1A from any run that also contains Teamwork** — the
> two overlap on grounded content (1,631 shared groundings against 77 for the
> alternative), which is a discriminant-validity problem, and the rule exists
> to keep them apart. That rule has ONE documented escape hatch: a form the
> **caller pinned** is honoured as asked and the run is stamped `"form was
> pinned by the caller; exclusion not applied"`. `DEFAULT_RUN_VARIANT=A` pins
> S1A on **every run**, so the escape hatch is taken on every run and the
> exclusion is, in practice, **off for the whole of Phase 1**. Nothing errors
> and nothing looks wrong: the runs record the exclusion as deliberately not
> applied, which is exactly what the stamp is for. An operator reading this
> page must not have to discover it by counting pairings in the data.
>
> This is a study-design question, not an ops setting, and it is **for the PI**:
> either Phase 1 accepts the S1A + Teamwork pairing and says so in the analysis
> plan, or `DEFAULT_RUN_VARIANT=random` restores the per-construct draw and
> the exclusion starts applying again. Both mechanisms exist in the merged
> code; the default is A.

## What changed on 2026-09-23 (pipeline_version 2026-09-23a to 2026-09-23g)

Fixes for issues #21-#25, landed mid-study. Every record carries
`pipeline_version` and `room_pacing_version` (on `realtime_session_started`,
and in `/health` under `gateway`), plus the knob values that ran (`turn_gate`,
`pacing`, `record`, `cancelled_output`, `input_rate`,
`input_transcription_model`, `max_output_tokens`). From `23g` record.json's
`provenance` carries all of them too, and the analysis DB's `encounter` row
has `pipeline_version`, `room_pacing_version` and `pipeline_provenance` (the
loader adds the columns to an older database). Split the archive on those
fields, not on deploy dates. Each change below is reversible without a code
change unless it says "no knob". The full per-version notes are the comment
above `PIPELINE_VERSION` in `server/llm.py`.

| Version | Change | To reverse |
|---|---|---|
| 23a | Instrumentation only: `turn_timing`, page `play_start`/`play_end` acks, `client_audio_settings`, and `cap_truncated`/`response_status` on `assistant_turn`. | nothing to reverse |
| 23b | gpt route: participant audio sent at 24 kHz. It was sent at 16 kHz and read by the gateway as 24 kHz. | no knob (defect) |
| 23c | The input rate follows each session's model: runner- and room-built gpt AND native-audio sessions went from 16 kHz raw to 24 kHz resampled. | no knob (defect) |
| 23b | Live transcriber `gpt-4o-transcribe` (was `whisper-1`). | `INPUT_TRANSCRIPTION_MODEL=whisper-1` |
| 23b | Reply cap 1200 tokens (was 380). | `REALTIME_MAX_OUTPUT_TOKENS=380` |
| 23b | Page end-of-encounter drain up to 45 s (was 12). | no knob (`AUDIO_DRAIN_MAX_S` in `static/v2.html`) |
| 23c | Silence probe commits its pad on a cleared buffer; the pad's transcript is suppressed (`probe_pad`). | no knob |
| 23c | Filler/punctuation-only transcript over at most 80 ms of voice is suppressed (`no_speech`). | `PARTICIPANT_DROP_VOICED_MS=-1` |
| 23c | Turn with under 600 ms of voice is tagged `low_confidence` and kept out of steering (and out of the director unless it names someone). | `PARTICIPANT_MIN_VOICED_MS=0` |
| 23c | Gateway buffer cleared on the first `speech_started` after a commit, with 600 ms pre-roll. | `INPUT_BUFFER_RESTART=0` (`INPUT_PREROLL_MS`) |
| 23c | Room near-duplicate filter needs 90% overlap (was 60%). | `PARTICIPANT_DEDUPE_OVERLAP=0.6` |
| 23c | A replayed line's transcript is not a second `user_turn`. | no knob |
| room 23b | Utterances queued behind a held floor are routed together. | `ROOM_MERGE_QUEUED_TURNS=0` |
| 23d | gpt route: what the gateway sends after a barge-in cancel is dropped (`cancelled_output_dropped`), not played or recorded as a second turn. | `CANCELLED_OUTPUT_DISCARD=0` |
| 23d | A reply delivered as several output items is recorded as all of them. Before, the last item replaced the others. | no knob |
| room 23c | gpt floor grant is the commit alone. A `response.create` is sent only if nothing starts within 3 s (6 s from room `24a`). | `ROOM_COMMIT_ONLY_GRANT=0` (`ROOM_GRANT_UNANSWERED_S`) |
| room 23c | A held reply with no audio, or one cut short by the suppression cancel, is not adopted (`held_reply_refused`). | `ROOM_ADOPT_GUARD=0` |
| room 23c | A participant resuming within 1.5 s of their own commit does not cancel the reply to it (`split_turn_extended`). | `ROOM_SPLIT_TURN_S=0` |
| 23e | Room `heard_seconds`/`total_seconds` are per turn (they accumulated across a member's turns). | no knob (record fix) |
| 23e | Silence probe counts from when the last reply finished playing, checked every 1 s. | `PROBE_IDLE_FROM_PLAYBACK=0`, `PROBE_TICK_SECONDS=12` |
| 23f | A reply read as a deferral ("I'll wait for Casey.") is blanked only when none of its audio was relayed. A spoken one keeps its text and is flagged `deferral`. The match's name slots are case-sensitive, so lines such as "That's for you to set." are no longer blanked. | `DEFERRAL_BLANK_AUDIBLE=1` (the regex fix has no knob) |
| 23g | gpt rooms: the near-duplicate filter is off (the scribe is the only transcriber). | no knob (defect) |
| 23g | An interrupted turn that played audio and had no transcript before the cancel takes its text from the dropped tail (`interrupted_text_from_cancelled_output`); it is no longer `transcript_missing`. | `CANCELLED_OUTPUT_DISCARD=0` restores the pre-23d relay |
| 23g | A barge-in during a retry's window drops the retry's reply as `cancelled_output_dropped` instead of playing it. | `CANCELLED_OUTPUT_DISCARD=0` |
| 23g | Record: knob blocks in record.json provenance; a stale `playback_cut` from an adopted hold no longer written; `turn_timing` rows tied to the participant turn of their grant, `commit_sent` only for a commit that went out. | record only |
| room 23d | A refused hold no longer swallows an empty fresh reply's done (the floor was held for 45 s); a cancelled reply's tail no longer counts as the commit-only grant's answer; Gemini rooms no longer route one utterance behind after a late transcript. | no knob (defects) |
| 23f | Interrupted and cap-truncated `assistant_turn`s carry `generated_text`, `heard_text` and `heard_estimate`. `heard_text` is the words that fit in the audio relayed, at the character's own measured rate or `HEARD_TEXT_WPM` (170). `text` is unchanged. The record also carries `heard_text` beside `text`. | record only; `HEARD_TEXT_WPM`, `HEARD_TEXT_CALIBRATE=0` |

Caveats for analysis:

- **Archived gpt and native-audio encounters heard participants at 1.5x.**
  This covers everything before `2026-09-23c`, including `23b`. On `23b` the 24 kHz fix
  reached only sessions built with an explicit model, even though provenance
  says 24000. The actor and the live transcriber both heard the participant
  fast and pitched up. Treat those live participant transcripts, and the
  actor's reactions to tone, as not comparable. Use the offline
  re-transcription (`python -m server.retranscribe`).
- **Turns may be cap-cut from 2026-09-18 16:04 EDT (commit 7ec0e00) to
  `23b`.** The 380-token cap stopped replies mid-word at about 10.5-14 s. The
  record kept the words the text stream had run ahead to, which were never
  spoken. `cap_truncated` exists only from `23a`; before that, a missing value
  means unknown, not uncut.
- **Room `playback_cut` values before `23e` are invalid archive-wide.**
  `heard_seconds`/`total_seconds` accumulated across turns (159.5 s, 284.4 s).
- **`heard_text` is an estimate.** On an interrupted turn it is an upper
  bound: the page drops audio it had queued at the barge-in, and the page's
  own `play_end` acks say what it played. Which column raters score
  (`text`/generated or `heard_text`) is the researcher's decision.
- **Other effects on earlier records.** Before `23d`, 1:1 `agent_audio_short`
  events were mostly phantom tail turns after a barge-in, and a two-item
  reply lost its first item. Before `23f`, an audible deferral was recorded as
  `transcript_missing`; its text is in that turn's `deferral_output` event.
  Before `23c`, phantom participant turns also reached `steering_pair` and
  `knob_set` rows.

## What changed on 2026-09-24 (pipeline_version 2026-09-24a, room_pacing_version 2026-09-24a)

Follow-ups to issues #21 and #24, calibrated on the tester's S3A session
`s_1790217895_4025d8`. Replayed through the runner's own VAD at its bar (500),
the two phantom lines ("I'm not a cat. I'm a cat. ..." and "Goodbye. Will
Lego play more games ...") had 300-400 ms of voice, the same as the real short
lines ("Thank you." 380-680 ms, "Does that sound good?" 660 ms, "Two."
720 ms). A voice floor alone could not tell them apart, so there are two gates.
The phantoms carried 47-63 words per voiced second, and every real line ran
1.4-6.1. The new knobs are in `turn_gate` and `pacing` on every record. Each
row can be reversed without a code change.

| Version | Change | Knob (default) / to reverse |
|---|---|---|
| 24a | Voice floor before a commit: when the VAD ends a turn with less voiced audio than the floor since the last commit, the turn is not committed. The gateway buffer is cleared and the event `participant_turn_discarded` (`voiced_ms`, reason `too_little_voice`) is written. No reply is started. This applies to 1:1 and to a room's scribe; a room turn is not routed and does not take the floor. The audio stays in `user_audio.wav`. | `PARTICIPANT_COMMIT_MIN_VOICED_MS` (300); `0` turns it off |
| 24a | Rate gate: when a transcript has more words per voiced second than the limit, over less voiced audio than the ceiling, it is written as `user_turn_suppressed` (reason `implausible_rate`, with `text`, `words`, `voiced_ms` and `words_per_voiced_s`). It never becomes a user turn, a caption, steering input or director input. Words over 0 ms of voice ("Sure." on silence) are caught too. | `PARTICIPANT_MAX_WORDS_PER_VOICED_S` (8; `0` turns it off), `PARTICIPANT_RATE_GATE_MAX_VOICED_MS` (1500) |
| 24a | 1:1: when the reply to a rate-gated turn has played no audio, it is cancelled and its tail dropped (`suppressed_turn_reply_cancelled`, with any generated text). A reply that already played is kept and written as `reply_to_suppressed_turn`. | follows the rate gate |
| room 24a | In a room, when every line that arrived for a turn was withheld from the director (suppressed, or `low_confidence` and naming no one), nobody is routed or answers. The event `group_turn_skipped` is written with the text. A turn where nothing arrived at all still goes to the director, as before. | `PARTICIPANT_LOW_CONFIDENCE_DIRECTOR=all` sends short lines to the director again |
| 24a | S1 hand-off: when the timebox has run out and the participant has been silent `HANDOFF_IDLE_S` after the last reply finished playing, the closing line is briefed and the character is prompted to say it (`handoff_probed`). Before, it waited for the participant's next turn plus 12 s, which was about 50 s of dead air in the sim. | `HANDOFF_IDLE_S` (3) |
| room 24a | The gpt commit-only grant waits 6 s for the reply before sending the one fallback `response.create` (it was 3 s: a reply took 5.09 s and the fallback was refused). The bridge's own 6 s unanswered bar waits out that window. | `ROOM_GRANT_UNANSWERED_S` (6, max 15; `3` restores the old wait) |
| 24a | `verify_record` counts a planted beat as reached only when a character performed it. A beat with `stage_direction_unperformed` or `trigger_undelivered` is taken off the count and listed separately (`unperformed: ...`, `undelivered: ...`). The dashboard's `triggers_fired` still lists every beat whose brief was delivered. | report only |

Caveats for analysis:

- Before `24a`, phantom participant lines of this kind were recorded as
  `user_turn` (on `23c`-`23g`, `low_confidence` when their voice was under
  600 ms), and rooms routed on them. Search `user_turn` rows with
  `voiced_ms` < 1500 and more than 8 words per voiced second to find them in
  the archive.
- `voiced_ms` is counted after the 600 ms pre-roll restart. A slow onset
  loses the voice that came before the pre-roll ("Thank you." was 680 ms since
  the last commit but 380 ms as counted). That is why the floor is 300 and not
  higher.
- `verify_record` coverage before and after `24a` differs by any
  `stage_direction_unperformed` rows. Re-run it on the archive rather than
  comparing old reports.

## What changed on 2026-09-24, review fixes (pipeline_version 2026-09-24b, room_pacing_version 2026-09-24b)

Fixes from the review of `24a`. The rate-gate numbers come from replaying the
tester's `s_1790217895_4025d8` audio at +6 to -12 dB through the runner's own
VAD. The voiced count of a real line shrinks when the speaker is a little
quieter, but its words stay the same. At 2.5 dB quieter, "Does that sound
good?" counted 460 ms, or 8.7 words per voiced second, which `24a` would have
suppressed. The span from the first voiced frame to the last stays put. Over
the span, every real line ran at most 6.2 words per second from +6 to -6 dB,
and the phantoms ran 44-56 at every level.

| Version | Change | Knob (default) / to reverse |
|---|---|---|
| 24b | The rate gate divides by the voiced span, not the voiced count. The limit is now 16 (was 8), about 2.6x above the real lines and 2.6x below the phantoms. `voiced_span_ms` is on `user_turn` and `user_turn_suppressed`, and `turn_gate.participant_rate_over` says `voiced_span`. The ceiling still applies to the voiced count. | `PARTICIPANT_RATE_OVER` (`voiced_span`; `voiced_count` with `PARTICIPANT_MAX_WORDS_PER_VOICED_S=8` restores `24a`), `PARTICIPANT_MAX_WORDS_PER_VOICED_S` (16) |
| room 24b | A `low_confidence` line that names nobody no longer skips the room turn. `24a` left real one-word answers unanswered ("Yes.", "Two.", "Thank you.", and "Casey?" transcribed as "TC?"). Those lines route as they did before `24a`, on context. Only a gate suppression (`implausible_rate`, `no_speech`, `probe_pad`) writes `group_turn_skipped`. | `PARTICIPANT_LOW_CONFIDENCE_DIRECTOR=all` still hands short lines to the director as text |
| 24b | 1:1: a suppressed turn's transcript that arrives after a later commit (the participant's next turn, or a probe) no longer cancels that later commit's reply. It is written as `reply_to_suppressed_turn` with `kept: "later_commit"`. | none |
| 24b | 1:1: when a reply is withdrawn, the planted beat it was briefed to perform is given back. The event is `trigger_undelivered` (reason `reply_withdrawn`, same `index`), and the next turn fires the same beat again. If a brief is being sent at that moment, `stage_direction_unperformed` (reason `reply_withdrawn`) is written instead. `verify_record` nets out both. | none |
| 24b | Sometimes the bridge's cancel of a reply that had no id yet reached the gateway too early, and the gateway answered `response_cancel_not_active`. The bridge now cancels that reply again once it is named. The reply's `cancelled_output_dropped` row carries `recancelled: true`, and the error no longer reaches the page. | none |
| 24b | S1 hand-off: the watchdog does not brief or probe the closing line while an earlier reply is still being finalized (its steering review can take 11 s). The timebox advances only once the reply that speaks the closing line has taken its note. When the next character's session is refused, the hand-off stays spent, so no second closing line is asked for, and the next turn retries the advance. | `HANDOFF_IDLE_S` (3), as before |

Caveats for analysis:

- On `24a`, a real short line from a quiet speaker could be
  `user_turn_suppressed{implausible_rate}`. The text is on the row. Rows with
  `words_per_voiced_s` under about 16 are worth reading before treating them
  as phantoms.
- On `24a`, a room turn whose only line was `low_confidence` has
  `group_turn_skipped` with reason `low_confidence`. That participant line got
  no reply.

## What changed on 2026-09-24, after verification (pipeline_version 2026-09-24c)

| Version | Change | Knob (default) / to reverse |
|---|---|---|
| 24c | A transcript with no letter or digit at all ("..." / "." / "```" / "。") is written as `user_turn_suppressed{no_speech}` at any voiced level, never as a turn. In a room the turn is skipped. The final verification saw three of these, over 300–500 ms of playback bleed or breath, answered as `low_confidence` turns. Fillers with letters ("Hmm.", "Okay") are unchanged. | `PARTICIPANT_DROP_WORDLESS` (1; 0 restores `24b`) |
| 24c | 1:1: a gateway `response_cancel_not_active` with no reply in flight is recorded as `voice_error` but no longer sent to the page. At the S2A i1→i2 boundary it painted "Something went wrong. Please try again." and marked the next socket drop as fatal. This was already live before this branch. | none |

What the verification established about the tester's S3A "phantoms"
(`s_1790217895_4025d8`): the two long invented sentences ("I'm not a cat…",
"Goodbye. Will Lego play more games…") were real short utterances, "Hello?"
and "Casey?". The new pipeline transcribes them correctly, 3 of 3 times each.
The invented text came from the old pipeline: 16 kHz audio read as 24 kHz,
whisper-1, and buffers of up to 44 s. Earlier live transcripts should be
read with that in mind. The offline re-transcription is the analysis copy.

## What changed on 2026-09-28 (pipeline_version 2026-09-28a, room_pacing_version 2026-09-28a)

The researchers' decisions of 2026-09-28 on the end of an encounter (#34) and
the turn cue (#49), with two issue #21 follow-ups and one room latency fix
(#25). The end policy is described in full in the next section.

| Version | Change | Knob (default) / to reverse |
|---|---|---|
| 28a | End policy: from 7:00 the participant may move on (End unlocks on every link type, with a notice; `move_on_open`); nothing ends the encounter by itself before 12:00. In the last interaction the auto-advance and the actor's `end_conversation` are held (`auto_end_held`); a held call is answered with a `function_call_output` (`tool_call_answered`), and on gpt the 1:1 character is asked to carry on (`held_call_reply`). The warning is at 11:00 and the stop at 12:00, on the runner's own clock as well as at a finished turn. | `ENCOUNTER_MIN_SECONDS` (420), `ENCOUNTER_WRAP_SECONDS` (660), `ENCOUNTER_MAX_SECONDS` (720); `REALTIME_TOOL_CALL_CONTINUES` (the output's wording) |
| 28a | Turn cue: the header pill says "<Name> is speaking" while that character's audio plays, "Listening…" once the participant starts speaking, and "You can speak now" when the runner sends `turn_open` (the page has acked the end of the last line's audio and nothing is queued or being generated), in 1:1 and in rooms alike. "Your turn" at generation end, its 4-second switch and "You speak first" in rooms are gone. Each `turn_open` is an event, to read against `turn_timing`. | none |
| 28a | A participant transcript made only of sound tags ("(laughter)", "[background noise]", "[Music]", "(inaudible)") is `user_turn_suppressed{no_speech}` with `annotation_only: true`, at any voiced level; in a room the turn is skipped. A word outside the tags keeps the line ("Yeah (laughs)"). | `PARTICIPANT_DROP_ANNOTATIONS` (1; 0 restores `24c`) |
| 28a | 1:1: a `no_speech` suppression withdraws the reply its commit started, as the rate gate does (`suppressed_turn_reply_cancelled` / `reply_to_suppressed_turn` with reason `no_speech`), and gives back the beat that commit fired. | follows the no_speech rules |
| room 28a | The post-turn steering review runs after the room's floor is released, as a tracked task, one review at a time; the next routed turn no longer waits for it (0.8-1.2 s on `24c`). `knob_set` rows are unchanged (`delivered: false`); a shift may reach a member one brief later than before. A review that raises is `voice_error` with `where: room_steer`. | none |

Caveats for analysis:

- Before `28a` the last interaction could end at the first finished turn past
  7:00 (`interaction_complete` just after 420 s, with no participant
  move-on); from `28a` a last interaction ends by the participant's move-on
  or `ceiling_reached` at 12:00. Encounter durations from the two sides of
  `28a` are not comparable.
- A held gpt call's reply (`held_call_reply` requested) is a character line
  that follows the character's own previous line with no participant turn
  between them.

## The seven-minute floor, and the twelve-minute stop

Every encounter runs **at least 7:00** and **at most 12:00**, measured from the
moment the voice socket opens. Three environment variables carry it —
`ENCOUNTER_MIN_SECONDS` (420), `ENCOUNTER_WRAP_SECONDS` (660) and
`ENCOUNTER_MAX_SECONDS` (720) — read by `storage.encounter_timing()`, served to
the page on the run (`timing`) and again by the runner itself on every link
type (the `encounter_clock` frame, which also lines the page's timer up with
the server's clock), so the ring that fills next to the timer and the server's
refusals agree to the second. The end policy is the researchers' of
2026-09-28 (issue #34); before it the wrap was 12:00, the stop 13:00, and the
last interaction ended by itself at the first turn past 7:00 once its beats
were spent or the character had called `end_conversation`.

- **Floor.** From 7:00 the participant may move on whenever they are ready:
  **End conversation** unlocks, the ring is full, and a neutral notice says so
  (the runner sends `move_on_open` and records it). Before it End is locked and
  says why, the participant's *move on* is held (event `floor_held`), and
  `POST /api/run/{id}/advance` answers **409** if a page asks anyway. The gate
  holds on **every link type**: study runs, internal `/test` runs and direct
  researcher links.
- **Nothing ends it by itself before 12:00.** In the last interaction the
  auto-advance after the last planted beat and the actor's `end_conversation`
  are held until the stop (event `auto_end_held`, once per reason). A held
  call is answered with a `function_call_output` saying the conversation goes
  on and not to call the tool again yet (`REALTIME_TOOL_CALL_CONTINUES`); on
  the gpt route the character is then asked to carry on
  (`tool_call_answered`, `held_call_reply`), so a goodbye is not followed by
  silence. Moving from one interaction to the next inside an encounter (S1's
  hand-off, S2's i1 to i2, room interactions) is unchanged.
- **Withdrawal is never gated.** *Stop and leave the study* works at any second;
  that is the consent promise, and it is a different control from End.
- **Warning and stop.** At 11:00 the runner records `ceiling_wrap` and the page
  shows that the conversation ends automatically in about a minute; at 12:00
  the encounter completes (`ceiling_reached`), from the runner's own clock
  (its watchdog tick, not only at a finished turn) and from the page's,
  whichever comes first.
- **Internal runs** (`cohort=internal`, the `/test` door) are exempt from the
  409 at `/advance`, but the page holds End until 7:00 for them too. Lower
  `ENCOUNTER_MIN_SECONDS` on a test deployment to walk the study quickly.

## The base URL also forwards participants (second route in)

**This is not the link this page tells you to paste.** The links to paste are
in [The participant URL](#the-participant-url-qualtrics--app--qualtrics)
below. This section documents a second way in that also works.

The base URL forwards a visitor to the study entry when an id is in the query,
so this is a whole, working link on its own:

```
https://rf.ai-ready-workforce.ai.cornell.edu/?participantId=${e://Field/participantId}&qid=${e://Field/ResponseID}
```

`participantId` is the Survey Flow embedded field set from the CloudResearch
Connect URL; `ResponseID` is Qualtrics' own id for that response. The app
stores both on the run, so `python -m server.qualtrics join` can match each
survey response to its four encounters. A participant who reopens the link
lands back in the same run at the encounter they were on. The bare base URL
with **no** parameters still goes to the researcher landing page and is still
key-checked.

Two things to know before choosing it:

- **It selects `/start`, the study run** — four constructs, one encounter
  each — the same run the pasted link below starts.
- **`&qid=` is as mandatory here as it is there.** The forward carries the
  query through unchanged, so a base-URL link missing `${e://Field/ResponseID}`
  produces exactly what the warning below describes: a run with no
  `qualtrics_id`, which cannot be joined to its survey response.

The forward runs *before* the researcher-key check, which is the point of it —
a participant arriving on the base URL used to hit that check. It does not
weaken the door: the forwarded request goes through `entry_params` and the
link-probe filter like any other, so a scanner or a link unfurler fetching the
base URL is shown the entry check page and **mints no run**.

## Adding a second deployer

Never share `jinsook-cli` (it is AdministratorAccess). Give the person their
own IAM user with deploy-scoped rights, and move Terraform state to the shared
bucket so two laptops cannot hold diverging copies of what is deployed:

```bash
infra/scripts/add-deployer.sh <username>      # e.g. ben-cli; idempotent
```

The script prints the two follow-ups: `tofu init -migrate-state` (once, by
whoever holds the current local state) and `aws iam create-access-key` (run
it yourself; the secret shows once; hand it over on a secure channel, never
email or chat). The new user has PowerUserAccess plus IAM read access, the
right to pass the two task roles to ECS, and the listed policy, trust-policy,
and tagging permissions on `relational-fluency-*` roles. Creating or deleting
IAM roles still needs an admin. They also need: access to the GitHub org repo,
Docker, and OpenTofu.

## Before every deploy: is anyone mid-encounter?

A rollout starts a new task and retires the old one about two minutes later,
which cuts any conversation running on the old task (Fargate caps the stop
timeout at 120 s, so no drain setting can save it). Twice now a team member's
test conversation "stopped after a few turns" because it began during a
rollout. Two rules:

1. Do not apply while anyone is in an encounter. `tools/deploy.sh` checks
   this itself and refuses to plan while it is non-zero; by hand, check first
   and wait until it reports zero:

```bash
curl -s https://rf.ai-ready-workforce.ai.cornell.edu/health | python -c "import json,sys; print('active sessions:', json.load(sys.stdin).get('active_sessions'))"
```

2. After `tofu apply`, wait for the rollout to finish before anyone tests:

```bash
aws ecs describe-services --cluster relational-fluency --services platform --query 'services[0].deployments[0].rolloutState' --output text
```

`COMPLETED` means one task is serving and it is safe to start a conversation.
Until then a page can load on the old task and be cut off minutes later. If a
participant is hit anyway, the app shows a Connection lost notice with a
Reconnect button.

**Reconnect does not resume the encounter — it starts a replacement one.** The
button calls the same `startSession()` as a fresh page load, and the server
mints a new session id, a new `data/sessions/<id>/` directory, new WAV files,
and an empty conversation history. Two consequences an operator has to know:

- **The encounter is split across two session records.** The first half —
  audio, transcript, stage directions, fired triggers — stays in the abandoned
  session directory; the second half is a separate record. Nothing links them,
  and `/api/runs` reports only the session the run advanced on, so the first
  fragment looks orphaned.
- **The participant replays the scenario from the top.** Trigger index and turn
  count restart at zero, so they meet the planted beats a second time. Treat
  that encounter as compromised for scoring, and say so in the wave notes.

Which is the real reason for rule 1 above: do not apply while anyone is in an
encounter. Reconnect keeps a participant from being stranded; it does not save
the measurement.

## Is the server up?

```bash
curl -s $RF/health | python -m json.tool
```

`status: ok` means the process is serving. `gateway.ok: true` means it can reach
the model gateway — if that is `false`, pages load but **no encounter will
work**, and `gateway.detail` says why.

`build` is the commit the serving image was built from (`BUILD_SHA`, baked in
at `docker build`), and after a deploy it must equal the tag pinned on `main`.
`null` means the image was built without `--build-arg BUILD_SHA`, or before it
existed (every image up to `0066b10`). The same value is on the participant
page as a small build tag, in `/api/run/config`, and in every encounter's
`provenance.build`; the `prod-build-drift` workflow compares it with `main`'s
pin every morning.

```powershell
(Invoke-RestMethod "$RF/health").build
```

```bash
# What is actually deployed, and did the rollout finish?
aws ecs describe-services --cluster relational-fluency --services platform \
  --query "services[0].[runningCount,pendingCount,deployments[0].rolloutState]" --output text

aws ecs describe-tasks --cluster relational-fluency \
  --tasks $(aws ecs list-tasks --cluster relational-fluency --desired-status RUNNING \
            --query "taskArns[0]" --output text) \
  --query "tasks[0].containers[0].image" --output text
```

```bash
# Is the load balancer sending traffic to a healthy task?
aws elbv2 describe-target-health --target-group-arn \
  $(aws elbv2 describe-target-groups --names relational-fluency-agent \
    --query "TargetGroups[0].TargetGroupArn" --output text) \
  --query "TargetHealthDescriptions[].[Target.Id,TargetHealth.State,TargetHealth.Reason]" --output text
```

## Logs

AWS CLI v1 has no `logs tail`; use `filter-log-events`.

```bash
# Last 15 minutes, application lines only
aws logs filter-log-events --log-group-name /ecs/relational-fluency/agent \
  --start-time $(( ($(date +%s) - 900) * 1000 )) \
  --query "events[].message" --output text | tr '\t' '\n' | grep -v "^INFO: *10\."
```

```bash
# Errors only
aws logs filter-log-events --log-group-name /ecs/relational-fluency/agent \
  --start-time $(( ($(date +%s) - 3600) * 1000 )) \
  --filter-pattern "Error" --query "events[].message" --output text | tr '\t' '\n' | tail -30
```

```bash
# Why did a task stop?
aws ecs describe-services --cluster relational-fluency --services platform \
  --query "services[0].events[:5].message" --output text | tr '\t' '\n'
```

## Is the app actually usable?

```bash
curl -s "$RF/api/scenarios?key=$KEY" | python -c "
import json,sys; d=json.load(sys.stdin)
print('study scenarios:', [x['id'] for x in d if x.get('study')])"
```

Expect all eight: `S1A S1B S2A S2B S3A S3B S4A S4B` — two parallel forms per
construct. Fewer means the deployed image predates a form,
or a spec stopped loading (a spec missing a required key is dropped silently
by the loader; CI's `EXPECTED_V3` is what notices).

```bash
# Direct link to one scenario, for your OWN testing. The key is only needed when
# the deployment sets PARTICIPANT_KEY_REQUIRED; either way this link carries
# SESSION_KEY, so never send it to a recruited participant. The link recruits
# get is in "The participant URL" below, and it has no key in it.
echo "$RF/v2?scenario=S1A&key=$KEY"
```

## Is the data being stored properly?

`verify_record` is the check that matters — it reads a capture and reports
whether it is scoreable.

```bash
python -m server.verify_record <session_id>   # one encounter
python -m server.verify_record --all          # every local encounter
```

It reports, per encounter: both transcript sides present, both audio channels
non-trivial, the steering trail logged and paired to replies, **planted triggers
fired against the scenario's plan**, ESCI items exercised, and provenance
(which gateway and models served it).

A `FAIL` on *every agent turn transcribed* or a low trigger count means the
encounter is not scoreable — an encounter that fired 2 of 4 triggers never
reached half its scored moments.

`verify_record` cannot tell you whether the *manipulation* happened. That is
`tools/encounter_health.py`, which reads the event trail rather than any
status field and fails an encounter whose director never answered, whose
stage directions were never acknowledged (`steer_unacked` — every mid-encounter
direction on the configured model, by design), or whose group room lost its
participant-transcription channel:

```bash
python tools/encounter_health.py data/sessions/<session_id>
python tools/encounter_health.py --all data/sessions     # exit 0 only if every encounter passes
```

### What a wave sounds like: lost audio, and the retry

On `nto.gemini-live-2.5-flash` through the gateway (the plain sibling of the
native-audio model the study now runs; the bars below were calibrated there and
have not been re-measured on the native-audio route) a reply's voice can stop
short of its own caption (about 1 reply in 9) or never start at all (before
this build, 45 s of dead air per stall, median 47.4 s, with the participant's
own turns refused meanwhile). The bridge now notices both and asks the gateway
**once** more for the turn (`AUDIO_ABSENT_S` = 8 s in `server/voice/realtime.py`).
Measured on the final wave: 4 stalls in 145 replies, all one-to-one, each now
7.4–8.5 s of silence instead of 47; 0 retries over a talking participant;
0 fires on `gpt-realtime-2.1`.

A turn the gateway never answers at all is a **different** fault and is no
longer left to that 45 s watchdog. Since 2026-09-14 a request with no frame
behind it is called unanswered at `REQUEST_UNANSWERED_S` = 6 s and re-asked
with the participant's own audio; unanswered again at `REPLAY_UNANSWERED_S`
= 4 s, the session is rebuilt and the line replayed into it (`RECONNECT_LIMIT`
= 2 rebuilds per encounter). From the participant's chair that is 9–22 s of
quiet — the seven live recoveries of that day measured 9.2, 9.3, 11.8, 14.9,
15.3, 19.2, 21.7 s, median 14.9 — and then the character answers what they
actually said, rather than an encounter in which every later line was lost.
Expect roughly one per 90–120 s of talking on this gateway. `RESPONSE_STALL_S`
(45 s) now only backstops replies the runner did not request.

Four things an operator needs to know about it:

- **The retry is a text prompt to the model** — `(I didn't hear that - the
  audio dropped. Could you say it again?)` — because for a reply whose *audio*
  was lost it is the only thing measured to revive it. It never enters the
  participant transcript
  and is written on the `audio_retry` event, but the character's recovered
  line answers it, and a rater will hear that: on the four live stalls the
  re-spoken line was delivered whole every time and repeated the lost words
  0 of 4 times. **The PI must rule on this before a wave** (it is in the
  decision memo); until then, tell raters what `audio_retry` on a turn means.
- **The nudge is not used where there is audio to replay.** A nudge in front
  of a *lost line* was measured to draw an answer to the nudge rather than to
  the participant — a dropped *"Good morning."* came back as *"You booked this
  meeting. What's on your mind."* — so every 1:1 request is re-asked with the
  participant's own audio instead, which needs no explaining to a rater
  (`participant_turn_replayed` on the record). The nudge remains only for a
  truncated reply, a group room, and a request with no speech behind it.
- **`encounter_health` reports it per encounter** — `audio lost upstream: N
  retried, N recovered, N delivered whole, N unrecovered` — so a wave can be
  checked for how much of it the participant actually heard, and a gateway
  that is failing more often than the pilot measured shows up here rather
  than in a rater's puzzlement.
- **Participants must wear a headset — a requirement, not advice.** With
  loudspeakers the character's own words land inside the participant's
  transcript and the transcriber hallucinates on the bleed, which corrupts the
  channel the study measures; the bleed also counts as "the participant is
  talking" and withholds a retry. The end-of-turn detector adapts to the room's
  noise floor (a fan or keyboard clatter no longer cuts a character off — false
  cut-offs 5 in 22 agent turns before, 0 in 19 after; genuine interjections
  still cancel the speaker, 6 of 12 after against 2 of 9 before), but a
  loudspeaker is not room noise.

```bash
# What is on disk locally
ls -t data/sessions | head
python -c "
import json,glob,os
for d in sorted(glob.glob('data/sessions/*'), key=os.path.getmtime, reverse=True)[:5]:
    m=json.load(open(d+'/manifest.json'))
    print(os.path.basename(d), m.get('scenario'), m.get('participant_id'))"
```

Each encounter directory holds:

| File | Contents |
|---|---|
| `record.json` | aligned record — transcript with each agent turn's stage direction, provenance, counts |
| `events.jsonl` | raw trail: every turn, trigger firing, direction, latency |
| `user_audio.wav` | participant channel |
| `assistant_audio*.wav` | agent channel per character |
| `manifest.json` | session metadata |

## Getting data off the server

The deployed app exposes each encounter as a zip. Do this **before** any deploy
until you have confirmed, with the `describe-task-definition` check at the top
of this page, that the running task actually mounts the EFS `/data` volume;
until then a rollout still takes the records with it. Once it does mount,
pulling is no longer a race — but EFS remains the only copy and nothing archives
to S3, so still pull each wave and keep it under the study's data-management plan.

```bash
# List encounters on the server
curl -s "$RF/api/encounters?key=$KEY" | python -c "
import json,sys
for e in json.load(sys.stdin)[:20]:
    print(e['id'], e.get('scenario'), e.get('participant_id'))"
```

```bash
# Pull one encounter (audio + transcript + events)
SID=s_xxxxxxxxxx_xxxxxx
curl -s -o "$SID.zip" "$RF/api/sessions/$SID/download.zip?key=$KEY" && unzip -l "$SID.zip"
```

```bash
# Pull everything currently on the server
mkdir -p server-pull && cd server-pull
curl -s "$RF/api/encounters?key=$KEY" | python -c "
import json,sys
print('\n'.join(e['id'] for e in json.load(sys.stdin)))" | while read SID; do
  curl -s -o "$SID.zip" "$RF/api/sessions/$SID/download.zip?key=$KEY"
  echo "pulled $SID"
done
```

## How many sessions / participants (durable, survives redeploys)

CloudWatch keeps every voice session start. From the terminal (a few minutes
for a 7-day window):

> `python`, not `python3`: an activated venv provides `python` on all three
> platforms, and `python3` does not exist on a Windows checkout — which is
> where half of this project's operators are. `python3` is reserved on this
> page for creating the virtualenv on macOS/Linux, paired with `py -3.12` for
> Windows, and tests/test_deploy_portability.py enforces that. (The line below
> is otherwise exactly as it arrived on origin/main 23d98a3.)

```bash
DAYS=7; START=$(( ($(date +%s) - DAYS*86400) * 1000 )); aws logs filter-log-events --log-group-name /ecs/relational-fluency/agent --start-time $START --filter-pattern '"/ws/participant/voice" "[accepted]"' --query 'events[*].[timestamp,message]' --output json | python -c "
import json,sys,re,datetime,collections
ev=json.load(sys.stdin); rows=[]
for ts,msg in ev:
    m=re.search(r'scenario=(\w+)&participant_id=([\w\-]+)', msg)
    if m: rows.append((datetime.datetime.fromtimestamp(ts/1000).strftime('%a %b %d'), m.group(1), m.group(2)))
print('voice sessions:', len(rows), '| distinct participants:', len({p for _,_,p in rows}))
print('per day:', dict(collections.Counter(d for d,_,_ in rows)))
print('per scenario:', dict(collections.Counter(s for _,s,_ in rows)))"
```

Or in the console, CloudWatch Logs Insights on `/ecs/relational-fluency/agent`:

```
fields @timestamp, @message
| filter @message like "/ws/participant/voice" and @message like "[accepted]"
| parse @message /scenario=(?<scenario>\w+)&participant_id=(?<pid>[\w-]+)/
| stats count() as sessions, count_distinct(pid) as participants by bin(1d)
```

Simulator runs (`server` verification) count like people here. Real study
participants are the `cohort=study` runs in `/api/runs` once the Qualtrics
link is live.

## Reading the steering trail

```bash
echo "$RF/director?key=$KEY"
```

Shows each encounter labelled by construct and variant, scene headings from the
research note, every stage direction above the reply it produced, and coverage
(triggers reached out of planned, ESCI items exercised).

## Releasing a build

**The runbook, in order.** Every step is a separate, visible action, and the
only one that changes production is step 6:

1. **Merge** the change to `main` by PR, CI green.
2. **Build** the image from that commit with `BUILD_SHA`: the
   `build-platform-image` workflow (Actions tab, Run workflow, on `main`), or
   the commands below. The tag and `BUILD_SHA` are the same short SHA.
3. **Pin it by PR**: set `container_image` in
   `infra/terraform/terraform.tfvars` to the new tag and update its
   `deployed:` line, in a PR of its own. Merge it.
4. **Sim check** on the updated `main`: `python -m tools.sim.check` (about 25
   minutes, needs the gateway; [`tools/sim/README.md`](../tools/sim/README.md)).
   Commit the report it writes to `tools/sim/reports/<tag>.json`.
5. **Plan through the guard**, from an up-to-date `main`:
   `git switch main`, `git pull --ff-only`, `tools/deploy.sh`.
6. **Apply** the saved plan, between collection sessions:
   `tofu -chdir=infra/terraform apply tfplan.bin`.
7. **Verify**: the rollout reads `COMPLETED` and `/health` reports `"build"`
   equal to the tag (see [Is the server up?](#is-the-server-up)).

`tools/deploy.sh` (bash; on Windows, Git Bash or WSL) refuses to plan unless
the working tree is clean, `HEAD` is exactly `origin/main` after a fetch, the
pinned tag is a commit on `main`'s history and exists in ECR, and production
reports `active_sessions` 0 and no build newer than the pin; it warns when the
tag has no passing sim report, checks the plan's own before and after images,
and prints the apply command rather than running it. What each refusal means,
and the two overrides (`--allow-active-sessions`, `--allow-rollback`), are in
[`DEPLOY-AWS.md`](DEPLOY-AWS.md#4a-what-toolsdeploysh-checks-and-its-two-overrides).

**Why there is a guard.** On 24 September 2026 a `tofu apply` ran from a
branch behind `main` whose `terraform.tfvars` still pinned `ca77c2f`, and
replaced `4798e64` in production. It said "Apply complete!", went healthy, and
stayed that way for four days, while testers reported bugs against a build
nobody believed was running. The Terraform state has been in the shared state
bucket since 17 September, so `tofu apply` does what the checkout it runs from
says; the only question is whether that checkout is `main`. The history before
that, when the state was not in the account's state bucket and every revision
was registered by hand, is in
[`DEPLOY-AWS.md`](DEPLOY-AWS.md#read-this-first-the-runbook-and-the-practice-have-diverged).

Build and push by hand, when the workflow is unavailable (from a clean
checkout of the commit being released):

```bash
set -euo pipefail
REGION=us-east-1
SHA=$(git rev-parse --short HEAD)
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com
REPO=$REGISTRY/relational-fluency/platform
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin "$REGISTRY"
docker build --platform linux/amd64 --build-arg BUILD_SHA=$SHA -t $REPO:$SHA .        # amd64 matters on Apple Silicon
docker push $REPO:$SHA
```

Windows PowerShell — same procedure, one statement at a time (PowerShell has no
`set -e`: `$ErrorActionPreference` does not cover a native executable's exit
code, so check the output of each line before running the next):

```powershell
$REGION = "us-east-1"
$SHA = git rev-parse --short HEAD
$ACCOUNT = aws sts get-caller-identity --query Account --output text
$REGISTRY = "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
$REPO = "$REGISTRY/relational-fluency/platform"
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REGISTRY
docker build --platform linux/amd64 --build-arg "BUILD_SHA=${SHA}" -t "${REPO}:${SHA}" .
docker push "${REPO}:${SHA}"
```

The repository URL used to come from `tofu ... output -raw ecr_repository`,
which needs the state this stack does not have: with no state the output is
empty, `docker build -t :$SHA` builds an image tagged with a bare colon, and the
push fails naming the tag rather than the missing state. Build the registry host
from `$ACCOUNT` and `$REGION` instead — and not from `"${REPO%%/*}"`, a bash
expansion PowerShell parses as a braced *variable name* (`REPO%%/*`), finds
nothing for, and expands to the empty string with no error at all, so
`docker login --password-stdin ""` fails with a message about a missing registry
that sends the operator after AWS credentials which are fine.

`--build-arg BUILD_SHA=$SHA`, with the same `$SHA` as the tag, is what lets
the running image say which build it is. Without it `/health` reports
`"build": null`, the page shows no build tag, the drift check fails, and the
tag cannot be fixed afterwards, because ECR tags are immutable.

If Terraform itself cannot run, the break-glass CLI release
(`register-task-definition`, `update-service`) is written out step by step in
[`DEPLOY-AWS.md`](DEPLOY-AWS.md#4b-break-glass-register-a-task-definition-by-hand),
with the IAM permission each step needs. It is not duplicated here, because two
copies of a release procedure diverge.

*No `sed -i ''`, on either path.* That is the macOS/BSD spelling. On GNU sed —
every Linux box, and Git Bash on Windows — `-i` takes its suffix attached, so
`''` is read as the *script*, `s|platform:...|` is read as a *filename*, and the
command exits 2 with `sed: can't read s|platform:...`, leaving
`terraform.tfvars` untouched. The old block had no `set -e`, so the next line
ran anyway and re-applied the tag that was already pinned: the operator builds a
new image, pushes it, watches a deploy succeed — and participants keep hitting
the previous build. Because tags are immutable and deploys are manual and
scheduled between collection sessions, that is discovered, if at all, during the
next wave. The committed pin in `infra/terraform/terraform.tfvars` moves by
hand, in its own PR (step 3 above); if a scripted edit is genuinely wanted, use
`python -c`, which is a prerequisite on all three platforms.

Rollout waits for the new task to pass health checks before draining the old
one, so an encounter in progress is not cut off at the switch, but the old task
is stopped 120 s later regardless and the conversation on it ends there. What
it had already recorded is on the EFS volume at `/data` and survives; the rest
of that encounter does not. That is what `active_sessions` 0 is for.

## When something is wrong

| Symptom | First check |
|---|---|
| Page loads, mic "does not work" | `curl -s $RF/health` — if `gateway.ok` is false, no encounter can run |
| A run in `/api/runs` has `exits` (`status: "mic_failed"` or `"camera_failed"`) and `withdrawn: null` | The participant's microphone or camera would not start and they left through "I can't get my microphone working". Not a withdrawal: the run is still open to them, nothing was stamped on their record, and the survey link they were offered carried `status=mic_failed` (or `camera_failed`). `capture_kind` is the browser's reason (`denied`, `missing`, `unanswered`, ...) |
| WebSocket opens then closes instantly | Application logs — a server-side exception during session creation looks exactly like a dead mic (4403 specifically means the participant record is missing or withdrawn) |
| 503 from the domain | Target health, then service events: usually no healthy task |
| `No scenario: SxX` | Deployed image predates the scenario bank — check the running image tag |
| A tester reports something a merged fix should have changed | `/health` `build` against the tag pinned on `main`; the `prod-build-drift` workflow says the same every morning. On 2026-09-24 production silently ran a rolled-back image for four days |
| Agent replies but no transcript | `verify_record` — look for `transcript_missing` |
| Encounter ends after ~3 turns | `INTERACTION_MIN_TURNS` / `INTERACTION_MIN_SECONDS` on the task |
| Every run is `cohort=unattributed` | The Qualtrics embedded field name. It is `participantId`; an unknown field pipes as the empty string and reports nothing — see [The participant URL](#the-participant-url-qualtrics--app--qualtrics) |
| No webcam recording, no `video_uploaded` event, "network" in the client | The bucket's CORS allowlist does not name the origin the page was served from, and a CORS refusal does not reach the local fallback — see [The third state](#the-third-state-which-loses-the-recording-cors) |
| Yesterday's encounters are gone | The task has no persistent volume and something redeployed or restarted it — see [Read this before collecting anything](#read-this-before-collecting-anything) |
| A character goes quiet for ~8 s and then starts the line again, or its reply answers "could you say it again" | The gateway dropped that reply's audio and the bridge retried it once — `audio_retry` on the turn. Expected on the configured model at a few per 145 replies; a wave doing much worse than that is the gateway, and `python tools/encounter_health.py` will say how often — see [What a wave sounds like](#what-a-wave-sounds-like-lost-audio-and-the-retry) |
| A character goes quiet for 45 s | A turn the gateway never answered at all; the watchdog closes it. Seen after one-word utterances. Nothing to fix on the task |
| Characters keep stopping mid-sentence | Room noise or loudspeaker bleed being read as the participant. Headset first; the end-of-turn floor adapts to steady noise, not to the character's own voice coming back through the mic |
| Qualtrics export fails while `whoami` succeeds | `QUALTRICS_BASE_URL` is the brand host, not the datacenter host — see [Pulling the survey responses out of Qualtrics](#pulling-the-survey-responses-out-of-qualtrics) |

---

## The URLs

There are four. The first one needs the researcher key and is the only one that
honours `variant`; the other three go to people you are not standing next to.
Read the warning under the first before you copy anything from this section into
Qualtrics.

> **On your own laptop there is a fifth, and it is the one you will actually
> type.** A local checkout has no `SESSION_KEY`, so the `/test` link's `&key=` is
> not available to you as a way of proving who you are. Append
> **`&cohort=internal`** to the `/start` link instead: it skips the seven-minute
> encounter floor and the 180-second advance floor (both are disabled for a run
> whose cohort is `internal`), and tags the run so every study export drops it.
> A bare `?pid=whatever` link also runs, but its run has no `qualtrics_id` and
> cannot be joined to a survey response.
>
> The links themselves, written out, are in
> [`docs/TESTING-LOCALLY.md`](TESTING-LOCALLY.md) — deliberately there and not
> here, because a URL on *this* page is one somebody may paste into Qualtrics,
> and a `/start` link without `&qid=` must never be one of those.
>
> On a **deployed** server the same suffix on a keyless link is *ignored* and a
> `WARNING` naming it is logged, which is the backstop described below — so this
> is a local-testing technique, not a second participant link.

### 1. Internal testing and lab demos (bug hunting)

```
https://rf.ai-ready-workforce.ai.cornell.edu/test?name=jennie&variant=A&key=$KEY
```

- **`key=` is mandatory on any deployment with `SESSION_KEY` set**, which is
  every deployed one. This door is `check_key`-gated — GET `/test` and the
  voice socket reached live audio and webcam capture in two requests from
  anywhere on the internet before it was — so without the
  key it answers **401** and no run is created. It is still open on a local
  checkout with no `SESSION_KEY`, the way `/researcher` and the download routes
  are. It is the researcher's own credential: it belongs in a link you paste
  into your own browser, never in one that goes to Qualtrics.
- `name` labels the run so a bug report can say whose session it was.
- `variant=A` or `variant=B` pins all four scenarios to one form; omit for the
  randomized mix. A letter no form carries is refused with a 400, here as on
  the participant links.
- `qid=` is neither needed nor read here: an internal run has no Qualtrics
  response to join to, and is excluded from the study data by its cohort.
- These runs are tagged `cohort=internal` and are excluded from study data by
  that tag; they can never be mistaken for a participant.

> **`variant=` and `cohort=` belong to this link and to no other — and since an
> earlier round, a stray one on a participant link is no longer a disaster.**
> Without the researcher key the server **ignores** both and prints a `WARNING`
> naming the parameter it threw away: the participant gets the run the study
> intends. Ignored rather than refused on purpose, because a 400 at the door
> mid-study costs the encounter outright, and the safe reading of a stray
> parameter on a recruited person's link is the run they should have had
> anyway. So a copy-paste slip costs you a line in CloudWatch, not the wave.
>
> The residual risk is narrow and worth naming exactly, because it is the one
> the backstop cannot cover: **copied together with `&key=`**, both are honoured
> — you are holding the credential, so the server does what you asked. Then
> `variant=A` pins all four encounters to one parallel form and the
> counterbalancing is gone with every record still looking perfect, and
> `cohort=internal` tags real participants as lab traffic that every analysis
> filter drops. That is the reason this link, key and all, must never be the one
> you paste into Qualtrics. Copy the participant links from the next section.

### 2. The participant-facing links

These go into Qualtrics; the canonical wording, and what each parameter
must and must not carry, is [The participant
URL](#the-participant-url-qualtrics--app--qualtrics) below.

| Link | Who clicks it | Mandatory parameters |
|---|---|---|
| `/start` | A participant arriving from the survey | `pid=`, **`qid=`** |

> **`&qid=` is mandatory on the `/start` link.** It carries the Qualtrics
> `ResponseID`, the join key between a run and the survey response that sent
> the participant here. Without a usable one the encounter still runs and is
> still recorded, but **it cannot be joined to its survey response** — and
> that is the whole wave, not one participant, because the link is one
> template. The full wording is in [The participant
> URL](#the-participant-url-qualtrics--app--qualtrics) below — read it before
> you paste anything into the survey.


Full linkage: the CloudResearch key ties recruitment to the survey, the
Qualtrics response id ties the survey response to the app run, and the
completion code carried back ties the run to the follow-up survey.

### Joining the data afterwards

```bash
curl -s "$RF/api/runs?key=$KEY" | python -m json.tool
```

One row per run: `participant_id` (CloudResearch key), `qualtrics_id`,
`cohort`, `participant_key_status`, `completion_code`, whether it finished, and
the `session_id` of every encounter it produced, which is the key into the
encounter records, audio, and transcripts.

There are **three** cohorts, not two:

| `cohort` | What it holds |
|---|---|
| `study` | A real arrival whose participant key validated. This is the dataset. |
| `internal` | Your own `/test` runs. Never participant data. |
| `unattributed` | A **real participant** whose key did not arrive usably — the Qualtrics field was empty, still an unreplaced `${e://Field/…}` placeholder, or otherwise malformed. `/start` refuses to turn them away mid-study, so the run proceeds under a synthetic `unattributed_<hex>` id (`server/app.py`), with the reason in `participant_key_status` and what actually arrived kept on the run document as `raw_participant_key`. |

So `?cohort=study` is the right filter for analysis, but it is the **wrong**
filter for checking that a wave is going well: an `unattributed` run is a paid
participant whose recording you have and whose recruitment record you cannot
join to it. A broken piping expression fails for *every* arrival, so this is
all-or-nothing — catch it on the first few, not at analysis time.

**Check this after the first arrivals of every wave, before the wave fills up.**
This is the one command on this page that has to work on whatever machine the
person watching the wave happens to be sitting at, so all three forms are
written out in full — do not translate it under time pressure.

bash / zsh (macOS, Linux, Git Bash):

```bash
# Should be empty. Anything here is a real participant you cannot attribute.
curl -s "$RF/api/runs?key=$KEY&cohort=unattributed" | python -c "
import json,sys
rows=json.load(sys.stdin)
print('unattributed runs:', len(rows))
for r in rows:
    print(' ', r['run_id'], r.get('participant_key_status'), r.get('created_at'))"
```

Windows PowerShell — no `curl` at all here; `Invoke-RestMethod` parses the JSON
for you, so there is nothing to pipe into Python:

```powershell
# Should be empty. Anything here is a real participant you cannot attribute.
$rows = Invoke-RestMethod -Uri "$RF/api/runs?key=$KEY&cohort=unattributed"
"unattributed runs: $($rows.Count)"
$rows | Select-Object run_id, participant_key_status, created_at | Format-Table
```

Windows Command Prompt — `curl.exe` is present on Windows 10 1803 and later,
and the URL must be quoted because `&` is a command separator in cmd:

```bat
curl.exe -s "%RF%/api/runs?key=%KEY%&cohort=unattributed" > runs.json
python -c "import json;rows=json.load(open('runs.json'));print('unattributed runs:',len(rows));[print(' ',r['run_id'],r.get('participant_key_status'),r.get('created_at')) for r in rows]"
```

A non-empty result means fix the Qualtrics `participantId` piping now (see
[The participant URL](#the-participant-url-qualtrics--app--qualtrics)) — and
the likeliest cause is the field name itself, because an embedded field
Qualtrics does not recognise pipes as the empty string rather than as an error.
The runs
already recorded can only be re-joined by hand, and `/api/runs` does not carry
the raw value — read it off the run document on the volume, where `/start`
stored it:

```bash
curl -s "$RF/api/runs?key=$KEY&cohort=unattributed" \
  | python -c "import json,sys; print('\n'.join(r['run_id'] for r in json.load(sys.stdin)))"
# then, per run id, on the volume ($DATA_DIR/runs — /data/runs on the server):
python -c "import json;d=json.load(open('/data/runs/<run_id>.json'));print(d['participant_key_status'], repr(d['raw_participant_key']))"
```

That value is only useful if the broken pipe happened to send something
identifying; often it is an empty string, and then the recruitment record and
the recording cannot be joined at all. The server also prints a `WARNING` line
per bad arrival, but a stdout line is not a check; this query is.

## Pulling the survey responses out of Qualtrics

The analysis-side half of the join: `server/qualtrics.py` exports the survey's
responses over the API and merges them with `/api/runs`, so "who replied and
how" is one table with the run id, completion code and encounter session ids
attached. It runs on your own machine, against `QUALTRICS_API_TOKEN`,
`QUALTRICS_SURVEY_ID` and `QUALTRICS_BASE_URL` in `.env`.

```bash
python -m server.qualtrics whoami     # credentials work
python -m server.qualtrics export     # raw responses to data/qualtrics/
python -m server.qualtrics join       # responses merged with runs
```

**The datacenter trap, and it only fires on the one call that matters.**
`QUALTRICS_BASE_URL` must be the **datacenter** host, `https://yul1.qualtrics.com`
— not the brand vanity host `cornell.qualtrics.com`. They are not
interchangeable, and the difference is invisible until the export:

- `cornell.qualtrics.com` answers `/whoami` and `/surveys` perfectly happily. So
  a setup check passes and the value looks right, possibly for months.
- The same host refuses `/export-responses` with *"This endpoint is unavailable
  through datacenter proxying, please retry using the url of the datacenter the
  API user belongs to: yul1.qualtrics.com"*.
- **`whoami` reports the datacenter as `viawest`.** That name is not routable
  and is not what belongs in `QUALTRICS_BASE_URL`. The only place the correct
  host appears is in the refusal message above — so read the error, not the
  field.

`_raise()` in `server/qualtrics.py` exists for exactly this: httpx's own
`raise_for_status()` renders the refusal as `Client error '400 Bad Request'`
plus a link to the MDN page for 400, and throws the body — the part naming the
host — away. An error that carries the answer and discards it is worse than no
error.

The token is a credential. It travels only in the `X-API-TOKEN` header, stays
out of git, and does not belong in a shell command you paste into a chat or a
ticket.

Two things to check on the joined output before trusting it:

- **The primary join is the Qualtrics `ResponseID`**, matched against each run's
  `qualtrics_id` — which is what `&qid=` on the entry link is for. If `qid`
  piping was working, everything joins on this and nothing else is needed.
- **The fallback join is by participant key**, for responses collected before
  `qid` piping existed. It looks for the key under a short list of field
  spellings in `server/qualtrics.py`; check that your survey's field name
  (`participantId`, per [The participant
  URL](#the-participant-url-qualtrics--app--qualtrics)) is in that list before
  relying on it, because a field it does not know about is not an error — the
  response simply comes back `UNLINKED`, and a table of unlinked responses looks
  the same whether the survey field is missing or merely unrecognised.

## The participant URL (Qualtrics → app → Qualtrics)

**Put this in Qualtrics.** It goes at the point where participants move from
the WEIP survey to the encounters. Copy it from here, whole.

```
https://rf.ai-ready-workforce.ai.cornell.edu/start?pid=${e://Field/participantId}&qid=${e://Field/ResponseID}
```

- **The field is `participantId`.** Checked against the live survey, which
  declares three embedded data fields — `participantId`, `assignmentId` and
  `projectId` — and pipes `participantId` and `ResponseID`. This page used to
  say `ParticipantKey`, which the survey does not declare, and that is the
  expensive kind of wrong: Qualtrics substitutes an unknown field with the
  **empty string** and reports nothing, so every link works, every participant
  is recorded, and every run lands in cohort `unattributed` with no recruitment
  record attached to it. `assignmentId` and `projectId` are CloudResearch's own
  identifiers; nothing in this platform reads them today, and they are named
  here so that a person comparing this page against the survey in front of them
  can tell "a field I am not using" from "a field that is missing".
- `${e://Field/participantId}` is Qualtrics piped text. If your survey spells
  the field differently, change the text **inside** the braces and leave `pid=`
  alone. The query-parameter spellings the app accepts are `pid`,
  `participant_id`, `participantId` and `PROLIFIC_PID` (`entry_params` in
  `server/app.py`).
  > **Changed 2026-09-15.** This bullet used to say `participantId` "is not
  > among them". It is now — it was added to `entry_params` in the same change
  > that made the base URL forward participants (see
  > [the section above](#the-base-url-also-forwards-participants-second-route-in)),
  > because that forward puts `participantId` in the query. `pid=` is still the
  > spelling to paste in the link: it is the one every other line on this page,
  > and every worked example, uses.
- `/start` is the full four-construct run: one encounter per construct, in a
  counterbalanced order, recorded on the run under `construct_pool`.

> **`&qid=` is mandatory on the `/start` link.** It carries the Qualtrics
> `ResponseID`, the join key between a run and the Survey 1 response — the one
> that holds the participant's consent and self-report. Consent itself is taken
> in Qualtrics; this platform shows no consent form and keeps no consent
> record, so the `ResponseID` is what ties an encounter to the person who
> consented to it. A link without it, or one whose `${e://Field/ResponseID}`
> never got replaced, is **not refused**: the participant plays all four
> encounters and everything is recorded, and the run carries no `qualtrics_id`.
> **Every run in the wave is then unjoinable**, and nothing on the participant's
> screen or in `/health` says so. `ResponseID` is built into Qualtrics; pipe it
> via embedded data, and check the first arrivals (below).
>
> **Do not append `&variant=` or `&cohort=`.** Neither is silently honoured any
> more: on a link without the researcher key both are **ignored, and a
> `WARNING` naming the discarded parameter is printed to the application log**,
> so a stray one costs you a line in CloudWatch rather than the wave. That is a
> backstop, not a licence — it only holds for a participant link. Copied from
> the `/test` link *with* its `&key=` still attached, `variant=A` is honoured,
> and then it pins all four encounters to one parallel form: the
> counterbalancing is gone and every record still looks perfect. `cohort=` the
> same way, marking real participants as lab traffic that every analysis filter
> drops. Both are documented, above, on the `/test` link — they belong to that
> link alone, and it is the nearest URL on this page to these ones, which is
> exactly where a copy-paste typo comes from.
>
> **And do not append `&key=`** — see the warning below, which is about the
> researcher credential and is the most expensive mistake on this page.

After the first few arrivals, check the wave two ways: no `unattributed` runs
(the `participantId` piping worked, [above](#joining-the-data-afterwards)) and
every run carrying a `qualtrics_id` (the `qid` piping worked — it is the join
key to the survey response). Both fail all-or-nothing, so the first three
participants tell you about all hundred.

> **Never put `SESSION_KEY` in the participant link.** An earlier version of this
> page told you to append `&key=<SESSION_KEY>`. Do not. SESSION_KEY is not a
> "drive-by" nuisance gate — it is the *only* credential in the system, and it
> is the one `check_key` demands for `/researcher`, `/director`, `GET
> /api/runs` (every participant's CloudResearch key, Qualtrics response id and
> completion code), `GET /api/encounters`, the per-encounter record, and every
> `download/<file>` and `download.zip` (every participant's microphone WAV,
> agent WAV, transcript, events and video pointers).
>
> The participant link is handed to every recruited person and ends up in their
> address bar, their browser history, and any screenshot or forum post they
> make of it. One participant reading `key=…` out of their own URL can list all
> sessions and download the entire study dataset. `server/app.py` says the same
> thing in `check_key`'s docstring; this page was the side that was wrong.
>
> Participant routes do not need the key: `check_participant` only enforces it
> when `PARTICIPANT_KEY_REQUIRED` is set, which is off in the normal
> deployment. `/start` forwards a key into the participant URL only in that
> configuration — the one case where a participant genuinely needs one, e.g.
> while the study is not yet open to recruits. If you set
> `PARTICIPANT_KEY_REQUIRED`, understand that you are choosing to publish the
> dataset key to your participants, and unset it before recruitment opens.

`SESSION_KEY` itself lives in Secrets Manager and belongs only in a researcher's
own shell (the `KEY` export at the top of this page):

```bash
aws secretsmanager get-secret-value --secret-id relational-fluency/agent-api-key \
  --query SecretString --output text
```

`/start` assigns a four-encounter run and redirects into the first. A
participant who closes the tab and reopens the same link **resumes their run**
rather than starting a second one.

### Sending them back

Set the Qualtrics continuation link so the app can return them:

```bash
# infra/terraform/terraform.tfvars
survey_return_url = "https://cornell.qualtrics.com/jfe/form/SV_xxxxx?..."
```

then `tofu apply`. After the fourth encounter the participant sees their
completion code and a **Return to the survey** button, which appends:

```
?run=<run_id>&code=RF-XXXXXXXX&pid=<participant key>
```

Capture `code` in Qualtrics as proof of completion. A partial run yields
`RF-PARTIAL-…`, so an unfinished session is visibly not a finished one.

With `survey_return_url` unset the participant still sees the completion code
and is told to return to the survey — they are never stranded — but the
one-click return is missing.

> **Set to the Qualtrics *host* rather than a continuation link, the button is
> worse than missing, and that is what a checkout has today.** `.env` currently
> carries `SURVEY_RETURN_URL=https://cornell.qualtrics.com`. Measured, with
> redirects followed: that URL ends at
> `https://shibidp.cit.cornell.edu/idp/profile/SAML2/Redirect/SSO?execution=e1s1`,
> HTTP 200, page title **"Cornell University Web Login"**. A CloudResearch
> participant has no Cornell NetID, so the last button of the study drops every
> completer on a staff SSO login page — and it takes their run id, completion
> code and participant key there with it, on the query string.
>
> Two consequences: paste the survey's own **end-of-survey / continue** URL here
> before any wave, and understand that whatever host you configure **receives
> the run id, the completion code and the participant key**, so it must be a URL
> it is acceptable to send those three things to.

### The survey completion code

When the survey checks one study-wide code, the app shows that code on a
finished run instead of the run's own `RF-XXXXXXXX`. It is also what `code=`
carries on the return link. A run that is not finished never shows it: a
withdrawal keeps its `RF-PARTIAL-…` code, which does not pass the survey's
check. Exports (`/api/runs`, the Qualtrics join) keep the per-run code.

The value is a secret because this repository is public; a code in git is a
code anyone can type without doing the study. Order matters, because ECS will
not start a task whose secret has no value:

```bash
# 1. Create the (empty) secret: the normal plan/apply, with
#    survey_completion_code_enabled = false in terraform.tfvars
tofu plan -out tfplan.bin && tofu apply tfplan.bin
# 2. Put the code (never in git)
aws secretsmanager put-secret-value --region us-east-1 \
  --secret-id relational-fluency/survey-completion-code --secret-string 'THE-CODE'
# 3. terraform.tfvars: survey_completion_code_enabled = true, then plan/apply again
```

The task reads the secret when it starts, so a later change to the value needs
a new deployment (`aws ecs update-service ... --force-new-deployment`).
Locally, set `SURVEY_COMPLETION_CODE` in `.env`.

## Analysis database

The schema in [`db-schema.sql`](db-schema.sql) runs as **RDS Postgres 16**,
instance `relational-fluency-analysis`, database `rf`
(`infra/terraform/analysis_db.tf`). It is a *copy* of the study data loaded
from the S3 archive by `tools/load_analysis_db.py`; the archive stays the
record, and the instance can be dropped and rebuilt from the bucket.
**Tanvi (`tanvi-cli`) administers it.** Cost is about $16/month.

### First-time setup (administrator)

```bash
# 1. Create it (part of the normal tofu plan/apply; takes ~10 minutes)
cd infra/terraform && tofu apply

# 2. Where it is, and the master password RDS generated (never in git)
tofu output -raw analysis_db_endpoint
SECRET=$(tofu output -raw analysis_db_master_secret_arn)
aws secretsmanager get-secret-value --secret-id "$SECRET" --region us-east-1 --query SecretString --output text
#    -> {"username":"rf_admin","password":"..."}  (paste the password when psql asks)

# 3. Let your own address in, then re-apply. Find it with: curl -s https://checkip.amazonaws.com
#    infra/terraform/terraform.tfvars:
#      analysis_db_allowed_cidrs = ["203.0.113.7/32"]
tofu apply

# 4. Schema and roles (TLS is required; psql negotiates it by default)
HOST=$(tofu output -raw analysis_db_endpoint)
psql "host=${HOST%:*} dbname=rf user=rf_admin sslmode=require" -f ../../docs/db-schema.sql
psql "host=${HOST%:*} dbname=rf user=rf_admin sslmode=require" -f ../../docs/db-roles.sql
psql "host=${HOST%:*} dbname=rf user=rf_admin sslmode=require" -c '\password rf_loader'
psql "host=${HOST%:*} dbname=rf user=rf_admin sslmode=require" -c '\password rf_analyst'
```

### Loading (whoever runs the refresh)

```bash
aws s3 sync s3://relational-fluency-study-data/encounters/ ~/RF_archive/encounters/ \
    --exclude "*" --include "*.json" --include "*.jsonl" --include "*.md" --include "*.csv"
KEY=$(aws secretsmanager get-secret-value --secret-id relational-fluency/agent-api-key --region us-east-1 --query SecretString --output text)
curl -s "https://rf.ai-ready-workforce.ai.cornell.edu/api/runs?key=$KEY" > ~/RF_archive/runs.json
.venv/bin/python tools/load_analysis_db.py \
    --dsn "postgresql://rf_loader@${HOST%:*}/rf?sslmode=require" \
    --archive ~/RF_archive/encounters --scenarios scenarios/v3 --s3-media --runs ~/RF_archive/runs.json
```

It is idempotent: rerun after every sync. `PGPASSWORD=...` in the environment
avoids the prompt.

### Giving an analyst access

1. Add their address to `analysis_db_allowed_cidrs` in `terraform.tfvars`
   and `tofu apply` (the security group is the only door; nothing else
   changes).
2. Give them the `rf_analyst` password. That role reads every table and view
   except `participant_identity`, and may write ratings. Connection string:
   `postgresql://rf_analyst@<endpoint>/rf?sslmode=require`, schema `rf`.
3. Remove the address when they leave the project.

Never hand out `rf_admin`; it exists to run the two SQL files above.
