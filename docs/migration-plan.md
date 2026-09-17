# Migration plan — v1 platform → Study Design v2 architecture

Written 2026-08-19 against *Study Design Proposal v2* and the AWS + Google
architecture slide. Describes the gap between what this repo runs today and what
Phase 1 data collection requires.

## Target (from the proposal)

```
Browser                         AWS                        Google
consent → WEIP → encounter      session broker             Gemini Live
  → completion code             ├ relays audio both ways   (speech-to-speech)
voice over WebSocket            ├ director steers actor    turn-taking +
webcam → direct upload          └ records a/v/transcript   interruptions native
                                     ↓
                                Study data (S3, encrypted)
                                one aligned record per encounter
```

Phase 1 target: 100 Prolific participants × 4 encounters (one per ESCI
competency, counterbalanced), 7–12 min each = 400 encounters with audio,
transcript, and video.

## What already matches

- **Director–actor loop.** `server/director.py` + `server/steering.py` already
  implement a director reading the transcript and injecting a stage direction
  per turn, with a JSONL steering log. This is the proposal's closed-loop
  steering requirement, already built.
- **Multi-agent sessions.** `server/multi_agent_session.py` plus the `g*`
  scenarios run rooms with several characters — required for S3 (three team
  members) and S4 (four-person group task).
- **Encounter UI.** `static/v2.html` is the Zoom-style participant view.

## Gaps, in dependency order

### 1. Voice layer — single-agent DONE, group mode DONE

> **Status, 2026-09-14.** Both halves of this section are closed. Group rooms
> run on Gemini Live through `server/group_room.py` — one realtime session per
> character, as the recommendation in 1b proposed — and the v1 cascade is gone
> from the codebase. The text below is kept as the record of what was measured
> on 2026-08-19 and why the design went the way it did; the current behaviour
> of the two realtime families, and what each one does and does not honour, is
> in `REALTIME_FAMILIES` in `server/voice/realtime.py`, and the model question
> is the PI's (`PI-DECISION-realtime-model.md`, alongside the repository).

**Single-agent encounters now run on Gemini Live** (`server/voice/realtime.py`
+ `server/realtime_voice_session.py`), verified end to end through the real
`/ws/participant/voice` endpoint: participant audio in, input transcription,
agent audio and transcript out, both channels recorded, and the director
re-briefing the actor between turns. The browser protocol is unchanged, so the
existing UI needed no edits.

**Group scenarios still run the v1 cascade** (`server/voice/stt.py`,
`server/voice/tts.py`, `server/multi_agent_session.py`). That is the remaining
ElevenLabs/Deepgram dependency and the next thing to remove — S3 (three team
members) and S4 (four-person group task) both need it, so this blocks two of
the four study constructs.

The realtime session re-briefs the actor per turn via `update_instructions`,
which is also the mechanism a group runner would use to switch character and
voice between speakers.

### Verified working: `nto.gemini-live-2.5-flash` via Cornell LiteLLM

Confirmed end to end on 2026-08-19 with real speech: participant audio in,
input transcription, agent audio + transcript out, function calling. No Google
credentials needed — it runs on the Cornell gateway key.

**Connection**

- `wss://api.ai.it.cornell.edu/v1/realtime?model=nto.gemini-live-2.5-flash`
- Header `Authorization: Bearer <litellm-key>`
- The upgrade **requires HTTP/1.1** — over HTTP/2 the endpoint 404s.
- The WebRTC path (`/v1/realtime/client_secrets`) is not wired up for any model
  on this gateway. WebSocket is the only transport.

**Session config — keep it flat and minimal.** This is the trap that cost a day:
an over-specified `session.update` leaves the session alive but permanently
mute (`session.created` arrives, then nothing, forever — no error).

```jsonc
// works
{ "type": "session.update",
  "session": { "instructions": "...", "voice": "Puck",
               "tools": [ /* function defs */ ] } }
```

Do **not** send `modalities` / `output_modalities`, the nested GA
`audio: { input: {...}, output: {...} }` block, `input_audio_format`,
`output_audio_format`, or `input_audio_transcription`. Any of these silently
kills the session.

**Audio + turn-taking**

- Input: 16 kHz mono PCM16, base64, via `input_audio_buffer.append`.
- Output: `response.output_audio.delta` (base64 PCM) plus
  `response.output_audio_transcript.delta`.
- Input transcription is on by default —
  `conversation.item.input_audio_transcription.completed` arrives without asking.
- **Server VAD does not work through the bridge.** `turn_detection` is accepted
  but has no effect: without an explicit
  `input_audio_buffer.commit` + `response.create`, the model never responds.

That last point contradicts "turn-taking & interruptions built in" on the
architecture slide. Through LiteLLM they are not built in, so **the broker must
run its own end-of-turn detection** (silence threshold on the participant
stream) and drive commits. Barge-in likewise has to be handled locally by
dropping queued agent audio when the participant starts speaking. Budget for
this; it is the main piece the gateway does not give us for free.

**`nto.gemini-live-2.5-flash-native-audio` works, with three route-specific
adaptations (found 2026-09-08 after it looked dead for a day).** The route
accepts a session and then stays silent forever if it is fed 16 kHz audio:
no transcription, no reply, no error. It wants **24 kHz PCM16 input**; the
client resamples per model (`input_rate_for_model`). Output is 24 kHz like
the other route (confirmed by pitch: 179 Hz vs 180 Hz for the same voice).
Also different on this route:

- It fires its own reply about 3.3 s after silence (the other route: ~1 s),
  so the broker waits longer before requesting one (`autofire_wait_for_model`).
- It accepts text conversation items. (This bullet used to add "the other
  route closes the socket on them". **That is no longer true and was probably
  never the item's fault** — see the "No longer true, 2026-09" note further
  down: re-probed 2026-09-10 and again 2026-09-14 on a flat session config,
  plain `nto.gemini-live-2.5-flash` accepts a user-role text item and answers
  it, first delta 0.23 s. The 1006 belonged to the 2026-08-19 over-specified
  session config. Nothing about the native-audio claim changes; both routes
  take text items, and `accepts_text_items` is True for every family in
  `REALTIME_FAMILIES` today.) Rooms use it: members hear only the
  participant's audio, and each colleague's finished line is injected as text
  (`GroupRoom.tell`).
  Fanning colleague audio into a native-audio member confused its turn
  detection: it reacted to colleagues with long replies and then never
  fired for the participant's next turn.
- A member still generating a reaction when the participant starts speaking
  is cancelled (`_cancel_stale_holds`), or it never answers the new turn.
- It emits many empty responses (logged as `empty_response`); harmless.

- Room members get **no tools** on this route: it calls `end_conversation`
  constantly and every call is an empty turn. That means `END_SEGMENT_TOOL` —
  the wiring that lets an actor end a group conversation and advance the
  encounter — is **not available on the route production runs**, and a group
  segment there ends the way it did before that tool existed: the director's
  turn budget, or the participant leaving. This is a real capability loss, it
  is per-family (`member_tools` in `REALTIME_FAMILIES`), and it is recorded
  here rather than absorbed silently.

Verified with the simulated participant: S2B 1:1 4/4 replies, ladder intact,
~2 s; S4A room 4 of 5 turns answered with correct name routing; S3A room with
interjection. Switch: `actor_model = "nto.gemini-live-2.5-flash-native-audio"`
in `terraform.tfvars`, apply, then a manual walkthrough before participants.

> **Where these facts live now.** Every per-route difference above — input
> sample rate, autofire wait, text items, colleague relay, how the floor is
> granted, member tools, whether `response.created` alone proves a reply
> started, the transcription language hint — is a **column on a row** in
> `REALTIME_FAMILIES` (`server/voice/realtime.py`), one row per family, rather
> than a substring test on the model name scattered across three modules.
> `gemini-live-native-audio` is its own row, and it has to be: `family_of()`
> would otherwise fold it into `gemini-live` and feed it 16 kHz, which is the
> permanent-silence failure described at the top of this block. **Setting
> `actor_model` and adding the row are one change, not two.**
>
> Two columns on the native-audio row are marked NOT PROBED and carried over
> conservatively: `honours_session_update` (False, so the runner records a
> stage direction as unacknowledged rather than claiming it landed) and
> `end_of_turn` (None, so the runner's own VAD_SILENCE_MS stands, which is what
> that route ran with in production). Probe them and say so in the row.
>
> **Three audio-recovery bars are also unprobed on this route**, and they are
> not columns: `RESPONSE_STALL_S` (45 s), `AUDIO_ABSENT_S` (8 s) and
> `REPLAY_UNANSWERED_S` (4 s) are single globals calibrated on plain flash (190
> replies for the audio bar, 502 closed replies for the stall bar, six waves).
> Two of the three are conservative on any route and stand as they are. The
> third was not: 4 s is shorter than the same route's own 4.5 s autofire wait,
> so a replayed turn was called unanswered and its session rebuilt before the
> route was due to start speaking. `_absent_bar()` now floors the replay bar by
> the family's `autofire_wait` for exactly that reason. If Phase 1 runs on this
> route, re-measure all three on it.

### What the per-family resolution switches OFF on the deployed route

Three mechanisms measured on plain flash do not run on
`nto.gemini-live-2.5-flash-native-audio`. None of them was deleted; each is a
column, and each is False or overridden there because that is what that route
was measured to need. Taken together they mean **the group path we measured is
not the group path production runs**, which is a fact for the model decision
rather than a bug to fix:

- **`END_SEGMENT_TOOL` reaches no room member** (`member_tools=False`, above).
  An actor cannot end a group conversation; the director's turn budget and the
  participant's own exit are what end a segment.
- **Colleague audio is not fanned to members** (`relay_colleagues_as_text=True`):
  `hear()` drops those members from the audio fan and `tell()` gives them the
  finished line as a text note instead. Our fan-out byte counters were measured
  on the route that still fans.
- **The floor is granted by a text nudge, not by pad-and-commit**
  (`grant_via_text_prompt=True`). The pad-and-commit path, and the fan-out byte
  counter that served as its `heard_something` signal, are plain-flash
  behaviour. A grant on the deployed route injects a nudge and asks.

`tests/test_origin_main_behaviours.py` holds each of these to what it was
measured to do, in both directions — what the deployed route does, and that
plain flash is untouched.

**Fallback for the announced deprecation of `nto.gemini-live-2.5-flash`
(verified 2026-09-08): `gpt-realtime-2.1` runs the whole platform.** The
client is model-family aware, so the switch is one setting:
`REALTIME_MODEL=gpt-realtime-2.1` (in Terraform: `actor_model` in
`terraform.tfvars`, then apply). What differs on that route, all handled in
`server/voice/realtime.py`:

- Voices: the bridge rejects Gemini voice names; each scenario voice maps to
  the nearest of `alloy, ash, ballad, coral, echo, sage, shimmer, verse,
  marin, cedar` (stable per character).
- Participant transcription is off unless asked for:
  `input_audio_transcription: {model: whisper-1}` is sent (accurate, e.g.
  "Rivera's team"). On this route the scribe only transcribes a committed
  buffer, so the broker commits it at its own turn end.
- Server VAD is switched off (`turn_detection: null`). Left on, every room
  member auto-replies whenever another character's fanned-in audio ends and
  is rejected with `conversation_already_has_active_response` (34 errors in
  one five-turn room). The broker's own silence detector drives turns, as it
  does for Gemini.
- The commit itself starts the reply; an explicit `response.create` on top is
  rejected and can double the reply, so it is not sent on this route. The room
  probes for the started reply rather than deciding by model name
  (`_gateway_answers_on_its_own`), and clears the response state after the
  commit so a latched flag cannot mute the next grant.
- `response.created` arrives well before the first audio delta here, so on this
  family `created` alone is taken as proof a reply started
  (`autofire_at_created`). On plain flash it is not: there, a `created` that
  never becomes a delta does happen, and treating it as a reply latches the
  auto-fire flag and mutes the encounter for good.

### Which forms a participant gets, and which mechanism is in charge

Two designs exist in this codebase and they are **not** reconciled here,
because it is not a documentation question.

1. **Variant A only (the default).** `DEFAULT_RUN_VARIANT=A` pins S1A, S2A,
   S3A, S4A on every study run, with the construct order counterbalanced per
   participant. This is what Phase 1 shipped with and it is what the merged
   code does out of the box.
2. **Three forms per construct, drawn per slot.** Twelve scenarios (S1A/B/C …
   S4A/B/C), two of each construct's three forms used and the third held back
   as a reserve so a second attempt has material the participant has not met;
   `FORM_EXCLUSIONS` applied to the completed draw with a digest-rotated
   replacement (2000 seeds: 50.7 / 49.3). This is reached with
   `DEFAULT_RUN_VARIANT=random`.

`DEFAULT_RUN_VARIANT` is the switch between them and it defaults to **A**, so
the default behaviour after this merge is design 1. Design 2 is fully present
and reachable by configuration.

**The consequence the PI has to rule on:** `FORM_EXCLUSIONS` bars S1A from any
run that also contains Teamwork (they overlap on grounded content, 1,631 shared
groundings against 77), and its one escape hatch is "the caller pinned this
form, honour it and say so". `DEFAULT_RUN_VARIANT=A` takes that hatch on every
run, so under design 1 the exclusion never applies. See
`docs/OPERATIONS.md` → "Which scenarios a participant gets".
- Cost of the fallback: group replies take 5 to 7 s (a fresh generation per
  turn; the Gemini route plays held replies in about 1 s), the model is more
  literal about its brief (occasional meta remarks like "let me close things
  out"), and per-character voices sound different. Verified with the
  simulated participant: 1:1 (S2B) 4/4 replies with the ladder intact, group
  (S4A) routed correctly with zero errors, interjection stops playback.

**Alternatives.** `gpt-realtime-2.1` also works on the same gateway and *does*
provide server VAD natively — useful as a comparison or fallback.
`nto.gemini-live-2.5-flash-native-audio` exists but was not re-tested after the
config fix. Going direct to Google for `gemini-3.1-flash-live-preview` remains
an option later (it would restore native VAD), but is not needed to start
Phase 1 and would require GCP credentials that are not on this machine.

Keep the voice layer behind one provider interface so the model stays a config
choice.

Reference implementation of the working browser↔broker↔gateway transport —
24 kHz PCM16 capture, gapless playback, barge-in, two-sided recording — is at
`~/Desktop/AI_Interview/_deprecated/nextjs-prototype/app/` (`server.mjs`,
`src/lib/realtime/websocketAdapter.ts`). Port, don't rewrite.

### 1b. Group rooms — blocked on a gateway constraint

S3 and S4 need several characters live in one room (Research Note v3: S3-A opens
with a public challenge in a team meeting; S4 is "one live 4-person session").
Half the constructs depend on it.

**What works.** `server/realtime_voice_session.py` sequences a group turn: the
director picks an ordered speaker list, and each character takes the floor via
`update_instructions` with its own persona and Gemini voice. Verified: the
director routes correctly (`director_route ['jordan','sam']`), the first
character speaks in role, and its audio and transcript reach the participant.

**What does not.** Every speaker *after the first* times out. Through the
LiteLLM bridge a conversation appears to yield exactly **one response per
committed participant turn** — a second `response.create` produces no events at
all, and no error. Tried and ruled out:

- waiting for `response.done` before handing over the floor (the gateway rejects
  overlapping responses with `conversation_already_has_active_response`, so this
  is necessary but not sufficient)
- forcing the in-flight flag down after a timeout
- committing a short silent frame before the second `response.create`, to give
  the model fresh input to answer

Text conversation items are not an option either: injecting one closes the
socket with 1006.

> **No longer true, 2026-09.** With the flat session config above, a user-role
> text `conversation.item.create` followed by `response.create` is accepted by
> the Gemini session and answered — first delta 0.23 s later, a complete reply
> with its audio stream closed. That is exactly what the audio-recovery retry in
> `server/voice/realtime.py` (`retry_response`) sends to revive a reply whose
> voice the gateway dropped, and it is the only recipe measured to do so. What
> is still untested is whether a *stage direction* sent that way is obeyed as an
> instruction rather than answered as a line the participant said; the 1006 was
> almost certainly the over-specified session config, not the item itself.

**Recommended next step: one realtime session per character.** Open N
connections — one per agent, each permanently briefed as its own character with
its own voice — and have the broker fan participant audio out to all of them
while serialising which one is allowed to answer. That sidesteps the
one-response-per-turn limit entirely and removes the per-turn re-briefing
latency, at the cost of N concurrent sessions per encounter (relevant to the
gateway quota question in the cost estimate).

### 2. Scenarios → the four ESCI competencies

Today: 13 ad-hoc scenarios (`scenarios/*.yaml`) from earlier exploration —
`missed_deadlines`, `credit_taken`, `hidden_profile_vendor`, etc.

Target: exactly four constructs, three parallel forms each, from
`reddit-analysis/scenarios/S{1..4}-*.yaml`. **Done — twelve forms are
compiled into `scenarios/v3/`:**

| | Competency | Form A | Form B | Form C |
|---|---|---|---|---|
| S1 | Conflict Management | Taken credit (barred beside S4, see below) | Hostile after-hours message | Blamed in front of the manager |
| S2 | Influence | Promised raise + competing offer | Hybrid under an RTO mandate | Stopping the Monday pack |
| S3 | Inspirational Leadership | After resignations over pay | After a commission cut | A system nobody asked for |
| S4 | Teamwork | Planning an internal rollout | Preparing a client presentation | Writing up the outage |

**S1-A is not assignable beside S4.** Every full session contains S4 and S4
always involves misattributed credit, which is also S1-A's situation; running
both in one session bleeds the Conflict Management and Teamwork constructs
together. The canonical spec's assignment rule
(`reddit-analysis/scenarios/scenario-specifications.md`, "Variation assignment")
therefore requires S1 B or C in any session containing S4. The grounding data
agrees: `reddit-analysis/situation-taxonomy.md` §3 calls blame/public
humiliation (1,631 posts) "the best-attested S1 trigger — supporting the
assignment rule that prefers S1-C (with S1-B) over S1-A", against 77 for credit
misattribution. S1C is that form, compiled.

Done: the rule is machine-readable and enforced. `server/runs.py` still draws
each construct's form independently — the draw cannot see the run as a whole —
but `FORM_EXCLUSIONS`, a construct → forbidden-form → co-occurring-construct
table, is applied to the completed draw inside `runs.create`, and any run that
came up S1-A alongside an S4 form has its S1 replaced with a permitted form
before it is written, rotated across B and C on a digest of the draw so
neither is over-served (measured over 2000 seeds: 50.7 / 49.3). The swap is
recorded on the run document as `form_exclusions`, so an analyst can see which
assignments were corrected rather than drawn. Adding the next exclusion is a
row in the table, not a second special case.

Also done, with the third forms: per-slot form selection, so a run that gives
a construct two of the four slots serves two different forms and holds the
third back for a second attempt (`construct_pool` on the run document;
`tests/test_reserve_draw.py`). The routing authority for "which forms are
parallel" is `scenarios_v3.parallel_forms()`, derived from `construct`; the
`parallel_form:` scalar in each spec is provenance, not routing — see
`scenario-spec-v3.md`.

The canonical specs are richer than the engine's schema — they carry
`ai_partners[]` (named roles + behavior policies), `fixed_opening_prompt`,
`pressure_points`, `focal_esci_items`, `duration_minutes`. The engine wants a
rendered `system_prompt` per character.

Work: a loader that compiles a canonical spec into runnable personas, so the
research spec stays the single source of truth rather than being hand-copied
into engine YAML. Several existing scenarios are close relatives of the
canonical ones (`04_credit_taken` ≈ S1-A) and can seed the persona text.

### 3. Storage → S3

Today: local `data/` and `logs/*.jsonl`.

Target: encrypted S3, one aligned record per encounter (video + audio +
transcript + steering log + participant key), with webcam video uploaded
directly from the browser via presigned URL.

Bucket exists: `rf-study-data-540586745717` (us-east-1, currently empty).
Credentials resolve through the AWS default chain — CLI profile locally, task
role on Fargate. No access keys in env files.

> **That bucket name is historical and is not the one the code uses.**
> `infra/terraform/storage_secrets.tf` creates `relational-fluency-study-data`
> and `server/video.py` defaults to it; the name above named an earlier,
> hand-made bucket. Pointing an operator at it is the kind of mistake that
> succeeds — the upload lands, in a bucket nothing else reads. The names that
> are actually read, and where each one goes, are in `.env.example` and
> [`DEPLOY-AWS.md`](DEPLOY-AWS.md#webcam-recordings-and-the-study-bucket).
> "No access keys in env files" has also softened: a researcher running the
> server on their own machine may put them in `.env`, which is gitignored and
> never enters the image. On Fargate the sentence still holds exactly.

### 4. Participant flow → Connect/Qualtrics round trip

Internal test links (`/v2?scenario=…`) open straight into an encounter; participants arrive through `/start`.

Target (Phase 1 deployment flow): CloudResearch Connect → Qualtrics
(participant key + WEIP baseline) → simulation app (consent → 4 counterbalanced
encounters → completion code) → Qualtrics (app feedback).

Work: participant key as the join credential, consent + webcam permission gate,
counterbalanced scenario assignment persisted per participant, completion-code
issuance, and resumability across four 7–12 minute encounters.

### 5. Deployment → AWS

Previously: Fly.io (`fly.toml`, removed 2026-09). Today: ECS/Fargate, released with the AWS CLI.

Target: ECS/Fargate behind an ALB with HTTPS/WSS, per `infra/terraform/`.
DNS is wired in code — `infra/terraform/dns_tls.tf` now provisions one ACM cert
covering both study hostnames and alias records pointing at the ALB:

| Hostname | Role |
|---|---|
| `rf.ai-ready-workforce.ai.cornell.edu` | participant entrance (app + broker WSS) |
| `api.rf.ai-ready-workforce.ai.cornell.edu` | backend API |

The zone (`Z03157053G6CGLIYWMAH4`) is delegated to Route 53, so this is
self-service. The records do not exist yet because they are ALB aliases — they
come into being with the first `terraform apply`, which also validates the
certificate via DNS. Nothing in this Terraform is deployed yet (no state, no
ACM certs; the `aiw-staging` ALB in the account is a separate environment).

## Open items for the team

- **RCT sizing conflicts between documents.** The RCT Study Flow slide shows
  N=300 in three arms (self-reflection / feedback without fine-tuning /
  feedback with fine-tuning). The Phase 4 text specifies 100 new participants in
  two arms (≈50 each). The cost estimate assumes 650 production encounters,
  while Phase 1 alone is 400. These need reconciling before budget or IRB
  amendments are filed.
- **Gemini Live availability.** `gemini-3.1-flash-live-preview` needs GCP
  project binding and a confirmed quota for concurrent live sessions before it
  can carry data collection.
- **Video consent + retention** must be settled before Phase 1 (flagged as an
  open question in the proposal).
