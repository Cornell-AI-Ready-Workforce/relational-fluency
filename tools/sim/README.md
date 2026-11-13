# Pre-deploy sim check

A simulated participant talks to this checkout's server through the same
protocol the participant page uses, in the four default encounters (S1A, S2A,
S3A, S4A), and the result is compared with a committed baseline. It is the
last step before `tools/deploy.sh` in the release runbook
([docs/OPERATIONS.md, "Releasing a build"](../../docs/OPERATIONS.md#releasing-a-build)).

It needs the model gateway, so it runs by hand, not in CI. The offline half
(analysis, comparison, stimuli) is tested in `tests/test_sim_analysis.py`.

## Run it

From the repository root, with the project venv active and a `.env` holding
the gateway key (`ANTHROPIC_API_KEY`) and, if the server should require one,
`SESSION_KEY`:

```bash
python -m tools.sim.check                     # all four, about 35 minutes
python -m tools.sim.check --scenarios S2A     # one, about 13 minutes
```

It starts its own server from this checkout (`tools/sim/serve.py`) on a free
port, configured the way production is: the task definition's environment
from `infra/terraform/ecs.tf`, with `var.*` resolved from `terraform.tfvars`,
and only the credentials (the task's `secrets`) taken from `.env`. AWS credentials are stripped
and archiving is off, so nothing it records reaches the study bucket; sessions
go to a temporary directory that the command prints, with `server.log` and a
`<scenario>.timeline.json` per run beside them.

It writes `tools/sim/reports/<build>.json` and exits 0 only if every scenario
ran and passed. `<build>` is the tag pinned in `terraform.tfvars` when this
checkout's image contents (`Dockerfile`, `requirements.txt`, `server/`,
`static/`, `scenarios/`) are identical to that commit's, which is the case on
main right after a pin PR; otherwise HEAD's short SHA, and `<sha>-dirty` when
those paths have uncommitted changes. `tools/deploy.sh` warns when the build
it is about to plan has no passing report. Commit the report, so the record of
what was checked before a deploy is in git.

## What is measured

Per scenario, from the session's `events.jsonl` and the driver's own timeline
(`tools/sim/analyze.py` has the exact definitions):

| Metric | Meaning |
|---|---|
| lines said / heard / answered | stimulus lines sent; turned into a participant turn; followed by a character reply starting to play |
| phantom turns | participant turns that match nothing said (#21) |
| refused creates | the gateway refusing an extra `response.create` |
| voice_error | voice errors other than the two known benign kinds |
| reply_missing | turns the gateway never answered and the runner re-asked |
| triggers fired | planted triggers that fired |
| spoke first | character replies played before the participant opened the conversation (the room tone each sequence begins with, and S1's hand-off) |
| speech end to first played, p50 / p90 | the wait a participant actually hears |

`baseline.json` holds the accepted values per scenario and the tolerances
(`tolerances`, overridable per scenario). Only regressions fail; a run that
does better than the baseline passes. Spoke first is the exception: since
pipeline 2026-09-28b any at all fails, whatever the baseline.

## When it fails

Read the `failures` list in the report, then the session: it is under the
printed directory, in `sessions/<session id>/`, and the usual tools work on it
as on any encounter (`python tools/encounter_health.py <dir>/sessions/<id>`;
`python -m server.verify_record <id>` with `DATA_DIR` set to that directory). A
live model is not deterministic, so one borderline failure is worth one re-run
before it is treated as a regression; two in a row is a regression.

## Re-recording the baseline

When a change is meant to move these numbers (a new `PIPELINE_VERSION`, a new
realtime model, a changed sequence or stimulus line), re-record in the same
PR and say why in the commit:

```bash
python -m tools.sim.check --write-baseline
```

It keeps `tolerances` and replaces the per-scenario values and the `recorded`
block; each scenario's entry also records the run it came from (build,
`pipeline_version`, `room_pacing_version`, session) and a fingerprint of its
sequence and stimulus, and a test fails in CI when a sequence or a stimulus
file changes without a re-recorded baseline. The baseline names the realtime
model it was recorded on, and a report against a server running another model
fails rather than comparing. The same holds per scenario for the versions: a
report fails a scenario whose baseline was recorded on another
`pipeline_version` or `room_pacing_version` than `/health` reports, saying to
re-record it, and a CI test fails when `server/llm.py`'s versions move past a
committed baseline. A new version is a different experiment: 28a runs S2A to
the 720 s ceiling where 24c ended it about 451 s in, and the count limits
(`phantom_turns`, `voice_error`, `reply_missing`, ...) were calibrated on the
shorter run.

A scenario listed under `pending` has no baseline yet, with the reason; the
check fails for it, saying so, until it is recorded. All four are pending as
committed: the latency audit of 2026-11-04 bumped PIPELINE_VERSION and
ROOM_PACING_VERSION without a live run (see `pending`). Before that, all four
were recorded on the cascade (`cascade-argon-elevenlabs`:
Argon hears and replies, flash-lite transcribes, ElevenLabs eleven_v4 speaks),
pipeline 2026-10-14a / room pacing 29b, on 2026-10-15: S1A, S2A and S4A from
build ac5e5c9 (s_1792090452_90b16a, s_1792090674_44dec7, s_1792091509_666571)
and S3A from 488ad6b (s_1792092205_0207e6), re-run alone because the first S3A
run's one voice_error was ElevenLabs adding a never-used library voice to the
account under three concurrent requests (409 already_running, now retried).
The gateway closed nothing in any of them. The gpt-realtime-2.1 values these
replace are at `git show 9361af6:tools/sim/baseline.json`; against them the
cascade answers about two seconds later per turn one-to-one (p50 4.1 / 3.8 s
against 2.4 / 1.8 s) and about level in rooms, with every line heard, no
phantom turns and every trigger fired.

A baseline compares one `pipeline_version` with itself, so a merge that brings
another version's change in under the same label needs a re-record that the
version guard cannot ask for. That happened to 10-01a: it was first recorded on
18a54be (`reports/18a54be.json`, S4A from `reports/18a54be-S4A-rerun.json`)
before #67 merged into it, and those values were replaced because they
described a build without the speech check.

With nothing to compare against, a version's first run cannot fail on a
regression, so before writing it as the baseline, read its report against the
version before by hand (30a: `git show 2df0cdf:tools/sim/baseline.json`, build
2ca289b), above all S3A and S4A: `lines_answered` (8 of 9 and 25 of 30 on
30a), `speech_end_to_first_played_s` p50 / p90 (4.538 / 7.28 and 4.406 /
6.021 s), `reply_missing` and `voice_error` (0), and in the sessions' events
any `member_memory_late`, `member_memory_error` or `member_memory_skipped`
`busy` / `no_item`. A drop in lines answered or a rise in latency there is
10-01a's ordering wait or its system-role nudges until shown otherwise, and is
not a baseline. 0c271c7's run answered 8 of 9 and 25 of 30, at 3.862 / 5.88
and 4.027 / 6.379 s, with none of those events (7 lines replaced, 4 told lines
corrected, no wait).

For 10-14a, read the first run against those 10-01a values (`git show
01f10f0:tools/sim/baseline.json`). A run in which the gateway closes nothing
does not reach any of 10-14a's code, so it should match 10-01a within the
tolerances; if a member's socket was closed, the sessions' events say what
happened (`room_member_dropped`, then `room_member_reconnected` or
`room_member_lost`, and `floor_regranted` for a holder that had not been
heard), and S4A's 01f10f0 run, which lost all three members at 154.7 s and
answered 5 of 47 lines, is the failure it replaces: a run that answers far
fewer lines than 10-01a with any of those events in it is not a baseline.

## The stimulus

`stim/<name>.ulaw` are synthetic speech from macOS `say` (voices Samantha and
Daniel), 16 kHz mono stored as G.711 µ-law to halve their size; `stim.py`
decodes them to the PCM the server takes. `stim/lines.txt` is the transcript of
each, which is what heard and phantom are measured against. No participant's
voice is in this directory, and none may be: this repository is public.

`python -m tools.sim.make_stim <name>` regenerates a line (macOS only). Only
lines a default sequence uses are committed, and a test keeps it that way.
Room tone is generated in code (`stim.room_tone`), not recorded.

## Against another server

`--server http://127.0.0.1:PORT` drives a server you started yourself (pass
`--build` if its `/health` names none). A non-local `--server` needs
`--allow-remote`: the check exists to test a build before it is deployed, and
pointed at production it records internal sessions there and spends its
gateway budget on the build already running.

## Offline latency breakdown

```bash
python -m tools.sim.latency /path/to/sim-run --json-out /tmp/latency.json --markdown-out /tmp/latency.md
python -m tools.sim.latency data/sessions/*/record.json --json-out /tmp/latency.json
python -m tools.sim.latency tools/sim/reports/1469925.json --json-out /tmp/sim-latency.json
```

Inputs can be run directories, `record.json` files, event JSONL files, or saved
sim reports. The tool reads sibling `events.jsonl` files for records. Directory
scans use `events.jsonl` and `*.events.jsonl`. They skip raw debug logs. Give
other JSONL names explicitly. Bad JSON inputs appear as issues while valid
inputs continue. The tool makes no network calls.

Each reply with a participant speech end has a row. The tool groups rows by
scenario, room or 1:1, encounter reply, participant reply, and family/model.
Encounter reply separates the first reply at start, the first after each 1:1
handoff (`first_after_handoff`), and later replies. Start and handoff boundaries
come from the recorded participant wait and acceptance events. Without these
events, the tool uses page turn order and cannot identify handoffs.

Participant reply uses `reply_index`: 0 is first; later values are follow-ups
to the same speech end. The sim summary uses the earliest played reply. These
definitions differ if reply 0 has no played ACK and a later reply plays.

The tables keep small groups, reply row counts, and missing played ACK counts.
They show errored and incomplete sim scenarios. Each stage has n, p50, p90, and the mean of its share of
the same reply's total. `share_n` counts positive totals for that stage.

The breakdown uses speech end to commit, room commit to grant, gateway audio
to client send, and client send to playback ACK. The unexplained residual
covers every remaining span. JSON records each interval's exact endpoint
fields. Transcript arrival and director decision are separate diagnostics.
Cascade durations can overlap. `llm_first_token_ms` records first sentence
release, `llm_total_ms` includes TTS waits, and `stt_ms` can be stale. Room
member timings do not measure scribe STT. These fields do not reduce the
residual.

`record.json` alone has no stage timings. Saved sim reports retain total
quantiles and n only. The tool keeps those values and marks other results
unavailable. It does not infer stages or reply groups from quantiles. Playback
ACK timing includes return network time. It does not measure hearing. The sim
does not model the browser audio path. Mode uses session metadata. The tool
cannot separate mixed room and 1:1 interactions within one session. Both
outputs list these limits and the boundaries not recorded.

Files with `reply_trace` or `commit_trace` events also produce a `trace` section
in JSON and a reply-path table in Markdown. The reader supports trace schema
version 1, `clock: encounter_s`, and seconds. Event `t` and `wall` date teardown;
they do not date the stages. Unknown versions and clocks are listed without
parsing their stage data. Inputs without trace events keep the same output.

The trace joins `reply_trace.page_turn` to `turn_timing.turn` within each session.
It joins `commit_item_ids` and `release_commit_item_id` to their exact
`commit_trace.commit_item_id`. It does not join by event order or nearby time.
The coverage counts show traces without a timing row, timing rows without a
trace, rejected schemas and unreferenced commits. Duplicate identities remain
unjoined. Cancelled and failed replies retain their observed partial stages.
Copied sessions are removed before the coarse and trace calculations. A known
session ID identifies copies; anonymous exports use the event-file content
hash. A second pass removes byte-identical event files across all ID groups,
including files in renamed export folders. The copy with the most timing/trace
events wins within a session ID. Retained sources keep the original Path order.
Dropped source paths, the
retained path and the reason appear in JSON and Markdown. Copied saved reports
and repeated summary session IDs are also excluded.

| Trace section | Meaning |
|---|---|
| `provider_audio` | Critical path from the commit's `participant.vad_detected` to `reply.stages.tts_first_audio`. |
| `adapter_output` | That path through `first_output_emitted`. |
| `client_audio` | Critical path through any transcript hold to `turn_timing.first_audio_to_client`. |
| `played_ack` | Client path through `turn_timing.first_audio_played`. |
| `felt_*` | The corresponding path rooted at `last_voiced_estimate`, with trailing VAD silence as an exposed step. These are sample-position estimates. |
| `work` | Full generation, slot wait, connection and other observed spans. These spans can overlap. |
| `commit_work` | STT and transcript decision spans, tied to their commit IDs. |
| `overlap` | Intersections of generation, connection and STT spans. |
| `readiness_gap` | TTS readiness after sentence readiness, sentence readiness after connection, or transcript readiness after held audio. |

The critical path follows causal edges. Request, content and sentence stamps
form one branch. Slot acquisition and connection form another. The first text
send waits for both sentence and connection readiness. The later prerequisite
selects the path at that join. Transcript decision and held audio form a
separate join at hold release. A different release commit does not inherit the
reply's participant clock or STT history.

Each group retains scenario, mode, family/model and the first, handoff and later
labels from its matched timing row. Origin is a separate group field, so probe
and replay work does not enter participant-commit work statistics. A trace
without a timing row keeps unavailable group labels as unknown. Each stage
reports n, p50 and p90. Commit work counts
commit spans per reply; its n can exceed the reply count. Missing stamps stay
missing. Each path reports an unexplained residual when a dependency is absent
or has invalid order. Complete recorded paths have no unexplained time.

Path-stage statistics exclude incomplete and clock-unverified paths. The n and
quantiles describe replies where a stage is critical. `path_n` counts all
complete paths. `critical_share` is critical n divided by path n.
`mean_contribution_s` averages the stage's time across complete paths, with zero
for replies where the stage is not critical. The Markdown table shows both
values. Work and overlap statistics retain their own observed spans. A group
also reports tied joins, counted once per response and join across all targets.
Ties use a deterministic path choice; the count exposes this ambiguity.

Do not add work, overlaps, readiness gaps and critical-path stages together.
Readiness gaps describe observed waits. They do not predict an optimization's
gain. The legacy coarse split stays available for comparison and for untraced
rows. Its missing-boundary list does not describe the stamps present in a trace.

Trace stamps use monotonic elapsed time after attachment. Legacy client and
playback stamps use wall elapsed time. Only paired VAD marks estimate and guard
wall-minus-trace offset. Each session reports their median and spread. Neither
the gateway pair nor `commit_sent` is an anchor: both include real work.
A VAD offset or spread above 5 ms marks client and ACK paths
`cross_clock_unverified` and excludes them from statistics. Offsets within 1 ms
are left uncorrected because the VAD mark has its own rounding and runner hop.
Larger verified offsets are subtracted and recorded. The clamp bound is 1 ms
plus the applied offset's magnitude, at most 6 ms. Changed fields and deltas
remain recorded. Larger reversals stay invalid. Pure trace edges stay strict.

For unheld replies, `output_to_gateway` exposes the emit-to-bridge hop as an
estimate under the applied VAD correction. The raw gateway-minus-VAD estimate
is also recorded. A separate `gateway_to_client` step uses the exact wall-to-wall
interval. A gateway rounding clamp moves its client/ACK endpoints together to
preserve those wall intervals. Queue latency never enters the clock guard or
gets removed as a clock offset. VAD correction remains limited by observation
delay and timestamp resolution.

JSON keeps raw stamps, corrected stamps, offsets and clamps. Markdown shows
clock status, offset and clamp count. A clock step after the last paired mark
cannot be detected by these anchors alone. Last voice is a sample-position
estimate. Its felt totals and exposed silence step appear beside the VAD-rooted
totals in the Markdown summary. Playback ACK measures scheduling, not hearing.
This trace covers the 1:1 cascade. Room routing, floor waits and browser receipt
still need separate observations.
