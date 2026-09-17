# Relational Fluency Platform

Research platform for the Cornell AI-Ready Workforce Initiative: participants
hold voice conversations with AI characters in workplace scenarios, and the
recorded encounters are used to measure **relational fluency**.

Study design: *Study Design Proposal v2* — Lee, Chun, Zhang, Slama, Joachims,
Kizilcec.

## What we measure

Four competencies from the ESCI **Relationship Management** cluster. Canonical
scenario specs live in [`reddit-analysis/scenarios/`](reddit-analysis/scenarios/),
grounded in an analysis of 39,301 r/antiwork posts.

| | Competency | Form A (Study 1) | Form B (reserved) |
|---|---|---|---|
| S1 | Conflict Management | Taken credit *(see the S4 note below)* | Hostile after-hours message |
| S2 | Influence | Promised raise + competing offer | Hybrid under an RTO mandate |
| S3 | Inspirational Leadership | After resignations over pay | After a commission cut |
| S4 | Teamwork | Planning an internal rollout | Preparing a client presentation |

**Eight encounters, two parallel forms per construct**, all authored and
compiled: the bank is `S1A S1B S2A S2B S3A S3B S4A S4B` in
[`scenarios/v3/`](scenarios/v3/), and CI holds that exact set
(`EXPECTED_V3` in `.github/workflows/ci.yml`). Forms of one construct share the
trigger sequence, the ESCI item map and the cast's voices, so a second attempt
can be served a form the participant has not met and the difference read as
skill change. Study 1 fields the A forms only (`DEFAULT_RUN_VARIANT=A`); the
B forms are reserved for a later study.
The per-form trigger map is generated from the specs into
[`docs/scenario-map.md`](docs/scenario-map.md).

**S1 form A is not assignable beside S4.** Every full session contains S4, and
S4 always turns on misattributed credit — which is also what S1-A is about.
Serving both in one session lets the Conflict Management and Teamwork measures
bleed into each other, so the canonical spec
([`reddit-analysis/scenarios/scenario-specifications.md`](reddit-analysis/scenarios/scenario-specifications.md),
"Variation assignment") requires S1 **B** whenever S4 is present. Study 1 pins
form A on every run, which takes the documented escape hatch (a pinned form is
honoured and the run is stamped `exclusion not applied`); the overlap is a
matter for the analysis plan, not the sampler — see `docs/OPERATIONS.md`.

> **Enforced in the sampler.** `runs.create` still draws each construct's form
> independently, which on its own would pair S1-A with an S4 form in about a
> third of all runs. The draw is therefore corrected once the whole run is
> known: `FORM_EXCLUSIONS` in [`server/runs.py`](server/runs.py) declares the
> rule as data — `("conflict_management", "A", "teamwork")` — and any run that
> drew both has its S1 form replaced with a permitted one before the run is
> written. The correction is recorded on the run document as `form_exclusions`
> rather than applied invisibly, because an analyst comparing forms across
> participants will otherwise see B over-sampled for no stated reason. So
> there is nothing for an operator to filter or discard; if you want to check
> it anyway, `form_exclusions` on a run says what was swapped and why.

Two defects in the shipped forms were found while the forms were being
measured against their siblings, and fixed on both forms of each construct
at once (the figures are in the comment headers of the scenario
files, and `tests/test_s1_parity.py` / `tests/test_s3_parity.py` hold the
repairs identical across forms):

- **Conflict Management conceded unearned.** Every counterpart gave the
  half-concession to a participant who attacked and demanded an apology —
  20 encounters of 20 on every form, under a brief whose next bullet said
  "harden and stop giving ground" — so `de_escalate` was being scored on a
  concession that arrived regardless. After the repair: 6/30 and 4/30 under
  hostility against 30/30 on the cooperative script, on both forms.
- **Inspirational Leadership leaked the private disclosure into the group
  room.** The 1:1 beat travels in the brief on every turn, and the S3A and S3B
  performers were both measured spending it during the team meeting. Each
  performer is now told, in the meeting section, that the private thing keeps
  until the door is shut.

An encounter is a sequence of **consecutive 1:1 conversations** — S1, for
example, is the instigating colleague first, then the peer. Characters never
share a turn.

**Phase 1 target:** 100 participants × 4 encounters (7–12 min, counterbalanced)
= 400 encounters with audio, transcript, and video.

## Target architecture

```
Browser (participant)              AWS                      Google
consent → WEIP → encounter    session broker            Gemini Live
  → completion code           ├ relays audio both ways  (speech-to-speech)
voice over WebSocket          ├ director steers actor
webcam → direct upload        └ records a/v/transcript
                                        ↓
                              Study data (S3, encrypted)
                              one aligned record per encounter
```

The **director–actor** split is the core method: the *actor* is the voice agent
the participant talks to; a separate *director* reads the transcript in real
time and injects one stage direction per turn, so the character follows a
behavior policy rather than drifting. Every direction is written to a steering
log alongside the audio and transcript.

> **On the model this deployment is configured for, the mid-encounter half of
> that does not arrive.** `REALTIME_MODEL` is `nto.gemini-live-2.5-flash`, and a
> stage direction sent *during* an encounter is composed, logged, and discarded
> in transit: measured across two 1:1 sessions and one group room, **5 sent, 0
> received**, no error, no warning. A character renamed twice mid-conversation
> kept answering with its original name.
>
> The conversation that results is fluent, in character, on topic, and
> **indistinguishable from a steered one** — a reviewer reading the transcript
> or listening to the audio has nothing to see. The only trace is a
> `steer_unacked` line in the event log and `tools/encounter_health.py` marking
> the encounter failed.
>
> What *does* land is the connect-time brief: character identity, backstory,
> personality, opening severity, and the planted beats that successive rounds of
> authoring moved into it. Characters are in character and hit their beats. The
> participant's own speech is fully recorded and transcribed in both 1:1 and
> group, so the dependent variable is intact.
>
> In a group room the platform now carries the director's per-turn direction
> to the addressed character over the same route the opening brief takes
> (`_direct_member` in `server/realtime_voice_session.py`). On the configured
> model it deliberately sends nothing — a mid-reply `session.update` on this
> family is how a character goes silent for the rest of the encounter — and
> records the direction with `delivered: false`; on `gpt-realtime-2.1` it goes
> out and its acknowledgement is recorded. Do not check delivery with the
> "say only BANANA" instruction: the gpt model echoes it back verbatim and then
> silently declines to follow it, so that test reports failure on the one model
> where delivery works. Use a direction the model has no reason to refuse
> ("what is your full name").
>
> Whether to stay on this model is a PI and IRB decision, because the consent
> form names the provider as the recipient of participant speech. It is written
> up in the `PI-DECISION-realtime-model.md` memo that accompanies this
> repository. Until it is decided, **do not read a good-sounding encounter as
> evidence that the steering works.**

## Migration status

The repository is mid-migration from the v1 stack to the architecture above.
Read [`docs/migration-plan.md`](docs/migration-plan.md) before starting work —
it carries the verified gateway findings, including a session-config trap that
silently breaks Gemini Live sessions.

| Area | Now | Target |
|---|---|---|
| Voice | **Gemini Live speech-to-speech** via Cornell LiteLLM — done | — |
| Models | **Cornell LiteLLM gateway** (Gemini) — done | — |
| Scenarios | **4 constructs × parallel forms**, compiled into `scenarios/v3/` — done | — |
| Storage | **ephemeral container disk** on the deployed task; only webcam video reaches S3 | encrypted S3 or a mounted volume, one aligned record per encounter |
| Entry | `/start` from Qualtrics with `participantId` + `qid` — done; researcher links `/v2?scenario=` for internal tests | CloudResearch Connect → Qualtrics → app → Qualtrics (two-survey chain) |
| Deploy | **ECS/Fargate behind an ALB, released by hand with the AWS CLI** | the same, under Terraform — once the missing state is recovered |

Verified 2026-08-19: `nto.gemini-live-2.5-flash` works end to end through the
Cornell gateway — audio in, transcription, audio + transcript out, function
calling. Server VAD is *inert* through that bridge, so the broker must do its
own end-of-turn detection and barge-in. Details and the exact working session
config are in the migration plan.

## Quick start

**Python 3.11, 3.12 or 3.13.** 3.12 is the reference version — it is what the
production image and CI's pinned leg run. `requirements.txt` says what changes
on 3.13 and why it matters for the audio path.

Three blocks, not one POSIX block with caveats. `&&`, `source`, `cp` and the
virtualenv layout itself all differ, and a block a reader has to translate
line-by-line is a block that does not run: `python -m venv .venv && source
.venv/bin/activate` is a **parse error** in Windows PowerShell 5.1 — the whole
line is refused before anything executes — and there is no `.venv/bin/` on
Windows to source even once the `&&` is fixed. Pick your shell.

Once the virtualenv is activated, `python` is the project interpreter on all
three platforms, and every other command in these docs assumes that.

### macOS and Linux (bash, zsh)

```bash
git clone <repo-url> relational-fluency
cd relational-fluency
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # then edit: ANTHROPIC_API_KEY=<Cornell LiteLLM virtual key>
python -m server.app
```

### Windows — PowerShell

```powershell
git clone <repo-url> relational-fluency
cd relational-fluency
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m server.app
```

If activation is refused with "running scripts is disabled on this system",
allow it for this window only — this changes nothing outside the current
PowerShell session:

```powershell
Set-ExecutionPolicy -Scope Process RemoteSigned
```

### Windows — Command Prompt (cmd.exe)

```bat
git clone <repo-url> relational-fluency
cd relational-fluency
py -3.12 -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
copy .env.example .env
python -m server.app
```

Both Windows blocks use `py -3.12` to name the reference interpreter
explicitly. If 3.12 is not installed, `py -3` picks the newest you do have —
fine, as long as it is 3.11, 3.12 or 3.13. Both blocks were run verbatim on
Windows 11 / PowerShell 5.1 and cmd.exe: after activation, `python` resolves to
`.venv\Scripts\python.exe` in each.

**Use port 8765.** It is the default, and it is not a preference: the study
bucket's CORS allowlist has exactly three origins, of which
`http://127.0.0.1:8765` and `http://localhost:8765` are the only local ones. On
any other port the webcam upload is blocked, and the page reports the
CORS-blocked PUT as a "network" error, which does **not** trigger the local
fallback — the recording is lost with nothing naming the cause. Everything else
works on any port; [`docs/TESTING-LOCALLY.md`](docs/TESTING-LOCALLY.md) has the
table of which surfaces care.

Then open:

| | |
|---|---|
| Is it able to record at all? | <http://127.0.0.1:8765/health> — **check this first, every time** |
| Walk the study yourself | <http://127.0.0.1:8765/start?pid=selftest1&cohort=internal> |
| Researcher console | <http://127.0.0.1:8765/researcher> |
| Steering trail | <http://127.0.0.1:8765/director> |
| Evidence trace | <http://127.0.0.1:8765/evidence> |
| Demo view, for showing the lab | <http://127.0.0.1:8765/demo> — reads `/health` and says what will and will not work *before* you present |

Two things about those participant links that nothing else tells you, and both
cost an afternoon if you find them the hard way:

- **A link with `?pid=` and no `&qid=` is a permanent dead end.** It stops on
  *"We could not confirm your consent record"*, and the card's advice to try
  again in a few minutes can never succeed, because `qid` is the only evidence
  this platform has that anyone consented. That is the check working, not a
  broken install.
- **`&cohort=internal` is the researcher's own way in.** On a checkout with no
  `SESSION_KEY` it satisfies the consent-provenance check with no `qid`, skips
  the seven-minute encounter gate and the 180-second advance floor, and tags the
  run so every study export drops it. Use it on every link you type by hand, or
  your own walkthroughs end up in the dataset.

`/health` deserves the same emphasis: `status: ok`, `ready: true`,
`config.missing_required_env: []` means consent will be recorded; `degraded` /
`false` means every participant hits the blocking card and **nothing is
recorded**, while the server keeps answering 200 and keeps assigning runs. The
boot warning that names the cause is printed a few seconds *after* uvicorn's
"Uvicorn running on http://127.0.0.1:8765" line — that is, after the line that
invites you to open your browser — so check `/health` rather than trusting the
scrollback.

Mic and webcam capture need a secure context — `127.0.0.1` counts, any remote
host needs HTTPS.

> **Your local webcam tests upload to the real study S3 bucket.** The AWS
> credentials in `.env` work, and the browser PUTs straight to
> `s3://<study bucket>/encounters/<session id>/webcam.webm` from your laptop.
> `&cohort=internal` keeps the *run* out of the study set; it does not stop the
> upload, so test objects need sweeping up before a wave.

**Walking the study yourself, end to end, is
[`docs/TESTING-LOCALLY.md`](docs/TESTING-LOCALLY.md)** — including the things a
participant sees that look like bugs and are not (a refresh restarts the
conversation from the top; the fiction gate and the audio check are remembered
per *run*, so a new participant id asks again; a machine with no
microphone is told so and cannot skip past it), and the things that are bugs
and are already known.

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

### What driving the live voice model found (2026-09-14)

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

## Browsers

Participants are recruited from the public through CloudResearch, so this is a
requirement, not a preference: **Chrome, Firefox and Safari, on macOS, Windows
and Linux**. A participant whose recording silently fails is an encounter lost,
and because they were paid it is an encounter lost expensively.

Safari ships only on macOS (and iOS), so the grid is seven real combinations
rather than nine. Saying that out loud saves someone an afternoon looking for a
Windows Safari build to test on.

|  | macOS | Windows | Linux |
|---|---|---|---|
| **Chrome** | supported | supported | supported |
| **Firefox** | supported | supported | supported |
| **Safari** | supported | Safari does not exist on this OS | Safari does not exist on this OS |

**Minimum versions are deliberately not stated.** Nobody has run the
participant page against a pinned older build on real hardware, and a version
number recalled from memory is worse than no number, because a screener will be
written against it. Confirm on real machines before one goes into the
CloudResearch screener.

**Recordings arrive in two different containers, and both must play.**
`static/v2.html` picks the first `MediaRecorder` type the browser admits, in
this order: `video/webm;codecs=vp8,opus`, `video/webm`,
`video/mp4;codecs=h264,aac`, `video/mp4`. So Chrome and Firefox participants
produce **WebM/VP8+Opus**, and Safari — which implements `MediaRecorder` but
supports only MP4/H.264 — produces **MP4/H.264**. If a browser admits none of the
four, the page writes a line into the transcript saying the conversation will
not be captured on camera, rather than letting the live camera tile imply a
recording is being made.

## Concepts

- **Scenarios** (`scenarios/*.yaml`) — situation, cast, system prompts, persona
  defaults, branch checkpoints, voice and model choice. Single-agent YAMLs are
  normalized internally into a one-element cast, so v1 and v2 share one engine.
- **Persona knobs** — warmth, formality, agreeableness, verbosity. Each maps to
  a prompt fragment composed into the system prompt at turn time.
- **Live steering** — researchers inject inline notes ("be more skeptical"),
  adjust knobs, or trigger a scenario branch mid-conversation. Notes enter the
  next turn's system prompt and are logged.
- **Consecutive 1:1 segments** — a scenario's cast is played one character at a
  time, in order. The actor calls `end_conversation` when its segment reaches a
  natural close, and the runner re-briefs the live session as the next
  character with a different voice.
- **The encounter record** — every session writes to
  `data/sessions/{session_id}/`:

  | File | What it holds |
  |---|---|
  | `record.json` | the aligned record: transcript with participant and agent turns in order, each agent turn paired with the stage direction that shaped it, plus provenance (gateway + models), audio/video pointers, and counts |
  | `events.jsonl` | raw append-only trail — every turn, latency, knob change, director route, and stage direction |
  | `user_audio.wav` | participant channel |
  | `assistant_audio*.wav` | agent channel, one per character |
  | `manifest.json` | session metadata and durations |

### Reading the steering trail

`/director` shows, per encounter, the participant's turns interleaved with each
actor's replies and — immediately above each reply — the stage direction that
produced it, with the planted trigger and ESCI items it was firing. Scene
headings mark where the interaction changed, so it is clear which character was
being steered.

It aligns to the scenario bank: each encounter is labelled by construct,
variant, and parallel form; scene headings use the interaction names from the
research note ("Hallway run-in with Sam") with what to observe; and coverage is
reported against the instrument — planted triggers reached out of those the
scenario specifies, and ESCI items exercised out of the construct's full set,
with unreached ones greyed.

This is the audit view for the closed-loop steering the study claims: a reply
and the instruction behind it are shown together rather than in separate logs,
and an encounter that only exercised part of the instrument is visible as such.

Operational commands — health, logs, pulling data off the server — are in
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).

### Deploying, and one thing to know before you read the runbook

The study service runs on ECS/Fargate and **every live task-definition revision
was registered by hand with the AWS CLI**. The Terraform under
`infra/terraform/` describes the stack correctly, but its state is not in the
account, so `tofu apply` is not the release procedure today — run from empty
state it would try to create infrastructure that already exists. The image tag
committed in `terraform.tfvars` is pinned to the one serving participants
(`cabc1dd`, with a `deployed:` line saying which revision that was verified
against); it has been behind before, and an apply against a stale pin rolls
production back and reports success. The release path in use, the permissions
each step needs, and what it will take to get back to Terraform are in
[`docs/DEPLOY-AWS.md`](docs/DEPLOY-AWS.md#read-this-first-the-runbook-and-the-practice-have-diverged).

Two consequences reach anyone running a wave rather than a deploy: the task has
**no persistent volume**, so session audio, transcripts and run documents live
on the container's own disk until the next rollout; and the deployed task is
missing four variables the code reads — `UPSTREAM_CONSENT_VERSION`, without
which a wave records nothing while looking healthy, plus `CLAUDE_MODEL`,
`SURVEY_RETURN_URL` and `DATA_DIR`. Both checks are in
[`docs/OPERATIONS.md`](docs/OPERATIONS.md#read-this-before-collecting-anything).

### Transcription quality

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

### Checking a capture

```bash
python -m server.verify_record <session_id>   # one encounter
python -m server.verify_record --all          # a whole collection wave
```

Reports whether both transcript sides and both audio channels are present and
non-trivial, whether the steering trail was logged and paired to replies, how
many planted triggers fired out of the scenario's plan, how many distinct ESCI
items were exercised, and whether the record can say which gateway produced it.
Run it during the pilot and on a sample during collection — an encounter missing
participant audio is cheap to catch on day one and impossible to recover later.

```bash
python tools/encounter_health.py data/sessions/<session_id>   # did the manipulation happen?
python tools/encounter_health.py --all data/sessions
```

`encounter_health` answers the question `verify_record` cannot: whether the
parts that make the encounter an *experiment* ran — a stage direction that was
acknowledged, a director that answered, a scribe channel that stayed up. On the
configured model every mid-encounter direction goes `steer_unacked`, so this
tool reports FAIL on a healthy-sounding Gemini encounter by design; it also
prints how many replies had their voice lost upstream and were retried. Exit
code is 0 only when every encounter passes.

  `record.json` is the analysis-facing view, built from `events.jsonl` at close;
  analysis reads it rather than replaying events.
  A turn with `stage_direction: null` ran unsteered — distinguishable from a
  direction that went unrecorded.

## Model configuration

Every model client is built in [`server/llm.py`](server/llm.py) from explicit
configuration — never from ambient environment variables. This matters more than
it sounds: the desktop app exports `ANTHROPIC_BASE_URL=https://api.anthropic.com`,
and `load_dotenv()` does not override variables that already exist, so a
gateway key was being sent to the wrong provider and every director call
returned 401. For a measurement instrument, the endpoint that served an
encounter has to be deliberate and recorded, not inherited from a shell.

Consequences of that rule:

- `.env` wins over ambient environment for gateway settings.
- Startup runs a preflight against the configured gateway and prints what it
  ignored; `/health` reports the same, so a misconfigured endpoint is visible
  before anyone joins rather than as a 401 mid-encounter.
- The resolved gateway and model names are written into each session's
  `record.json` under `provenance`.

```bash
curl -s localhost:8765/health | python -m json.tool
```

`python`, not `python3`: the python.org Windows installer creates `python.exe`
and the `py` launcher and no `python3.exe`, while Windows ships an App
Execution Alias at `%LOCALAPPDATA%\Microsoft\WindowsApps\python3.exe` that
opens the Microsoft Store — or, where it does resolve, resolves to the system
interpreter rather than the activated virtualenv. In an activated venv
`python` is the project interpreter on all three platforms. In PowerShell,
also write `curl.exe`; see the note at the top of
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Before a wave: `UPSTREAM_CONSENT_VERSION`

Consent is taken in the Qualtrics survey now, before a participant ever reaches
`/start`. This platform records only that one exists upstream — and it will not
record even that until `UPSTREAM_CONSENT_VERSION` names the approved wording the
survey is showing (e.g. `cornell-irb-2026-09-v3`). Set it in `.env` locally, and
on AWS through the `upstream_consent_version` Terraform variable, which
deliberately has no default so an apply cannot skip it.

Unset, a whole wave is lost in silence: `/health` stays 200, `/start` keeps
assigning runs, and every one of them records nothing, because
`POST /api/consent` is refused and the voice socket then closes 4403. `/test`
runs are recorded under internal-test provenance and do **not** exercise this
path, so walking the study yourself proves nothing about it. The checks that do
are in [`docs/OPERATIONS.md`](docs/OPERATIONS.md#the-consent-version-unset-this-records-nothing),
along with the blanks in `config/consent.yaml` that must be filled before
fielding (eleven `[FILL IN: …]` markers today; `version` is still
`v0.2-2026-09-draft` and `irb_status.reviewed` is `false`).

### Five things that each cost an afternoon

None of them is a bug, and each is written up in full in
[`docs/OPERATIONS.md`](docs/OPERATIONS.md) or
[`docs/TESTING-LOCALLY.md`](docs/TESTING-LOCALLY.md); this is the list to read
before the first participant.

1. **Port 8765**, locally. The study bucket's CORS allowlist names
   `http://127.0.0.1:8765` and `http://localhost:8765` and no other local
   origin, and a CORS-refused webcam PUT is reported as `network`, which does
   not reach the local fallback. Any other port loses every recording silently.
2. **`&qid=` on every `/start…` link.** It carries the Qualtrics `ResponseID`,
   the only evidence this platform has that anyone consented. Without it the
   arrival is refused, permanently, and the encounter is not recorded. The
   piped field for the participant key is `participantId`.
3. **`QUALTRICS_BASE_URL` is the datacenter host, `https://yul1.qualtrics.com`**
   — not `cornell.qualtrics.com`, which answers `whoami` and `surveys`
   perfectly and then refuses the one call that matters, `export-responses`.
4. **`SURVEY_RETURN_URL` must be the survey's own continuation link**, not the
   Qualtrics host: `https://cornell.qualtrics.com` on its own redirects
   completers to Cornell's SSO login, and whatever host is configured receives
   the run id, completion code and participant key on the query string.
5. **A headset.** Loudspeaker playback bleeding into the microphone is read as
   the participant talking; see [What the participant hears](#what-the-participant-hears).

## Privacy

Participant audio, video, and transcripts are PII. `data/`, `logs/*.jsonl`,
`*.wav`, and `.env` are gitignored and must stay that way. Study data belongs in
the encrypted S3 bucket under the IRB data-management plan, never in the repo.

## Status

**v0.5** — voice encounters run on Gemini Live speech-to-speech through the
Cornell LiteLLM gateway; ElevenLabs and Deepgram are gone from the codebase
entirely. Scenarios play as consecutive 1:1 conversations. Researcher steering
with per-agent knobs and notes. Dataset capture with separate participant and
agent WAVs, SQLite index, and manifest.

Deployed on ECS/Fargate behind an ALB at
`rf.ai-ready-workforce.ai.cornell.edu`. Next, in dependency order: set the four
environment variables the deployed task is missing
(`UPSTREAM_CONSENT_VERSION` first, without which a wave records nothing; then
`CLAUDE_MODEL`, `SURVEY_RETURN_URL`, `DATA_DIR`); give the task a persistent
volume so an encounter survives a deploy; recover or rebuild the Terraform
state so the infrastructure stops being edited by hand; archive session
records to S3 rather than only the webcam video. The first three are written up
in [`docs/DEPLOY-AWS.md`](docs/DEPLOY-AWS.md#open-questions), and the model
decision — which realtime model the study fields on — is the PI's, in the
`PI-DECISION-realtime-model.md` memo that accompanies this repository.

Known gaps: the legacy `g*` scenarios were authored as group rooms and read
oddly when played 1:1 — they are superseded by the S1–S4 bank. Photo tiles use
initials placeholders.
