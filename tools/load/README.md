# Load harness

```sh
python -m tools.load.run --gateway fake --ramp 1,5,10,20,30 --repeat 1 --tts-factors 1.0,3.0
```

By default, one server uses the repository Dockerfile, a 0.5 host CPU quota and 1 GiB of
memory. This is a host measurement, not a Fargate capacity estimate. Each
logical participant follows the server-assigned S1A–S4A Williams order. It
keeps the same identity and HTTP cookies across four full encounters and uses
the page's run-advance API. Sim sequences extend with their own stimulus
lines until the encounter completes. Production's default encounter ceiling
is 600 seconds. Four encounters take about 40 minutes per step.

`--scenarios S2A` selects one scenario. A subset rotates the selected IDs
without advancing the full study run. `--arrival burst` starts participants
together. `--arrival stagger:2` spaces arrivals by two seconds. `--repeat K`
runs each N K times. Each TTS factor gets a fresh runtime and its own pooled
N=1 baseline. `--continue-after-fail` continues to higher N after a FAIL.
The default stops at the first failed repeat and starts no further repeats or Ns.

`--webcam` queues 150,000 synthetic WebM bytes on absolute two-second ticks.
Uploads use `/api/sessions/{id}/video-chunk`, participant ownership and ordered
sequence numbers. A slow upload does not delay chunk creation. A failed
upload remains at the queue head and retries on a later tick. Upload errors
count as server errors. This measures upload/storage work, not video encoding.

`--steps 'tone:1,cycle:a1|a2:45' --scenarios S2A --duration 45 --warmup 5`
is a diagnostic. Short encounters are INVALID for capacity. Full runs require
`encounter_complete` and wait for closed manifests and rendered mixes.

## Fake upstream

Routes: GET `/v1/models`; POST `/v1/chat/completions` as STT JSON or actor SSE;
POST `/v1/messages` as director/steering tool responses; ElevenLabs WSS
`/v1/text-to-dialogue/stream-input`. HTTPS/WSS uses an ephemeral certificate.
The server trusts it plus public roots for explicitly enabled live mode.
TTS always uses a synthetic key and the fake.

The default TTS profile is `calibrated`, fitted to the orchestrator's
2026-11-12 ElevenLabs sample: 30 measured successes, with 15 single-sentence
requests for the first-audio/duration/generation fits. `--fake-tts-profile legacy`
selects the earlier connect=0 ms, first audio=150–350 ms, 160 WPM, factor=2.0
settings. JSON `tts_profile` also selects the base profile. Explicit JSON fields
override profile defaults; an explicit CLI profile overrides the JSON profile
name. Reports retain the resolved profile and all effective values.
An archived JSON without `tts_profile` that sets first-audio delay, WPM or
generation factor resolves to `legacy`, including its zero connect delay.
Zero connect/close delays consume no extra random draws, preserving the old
default legacy draw sequence for sequential reproductions. Scheduling still
affects concurrent runs.

STT accepts a single waveform anchor from the committed sim clips. Valid
speech fragments without an anchor get a canned transcript whose word count
scales with audio duration. `unmatched_fragment` counts these responses in
fake stats and step reports. STT choices include `index: 0`,
`finish_reason: "stop"` and an assistant message with a `content` key. The
request's model selects quiet output: `nto.gemini-3.5-transcribe` returns
`content: null` for silence/room tone, ignores the text prompt, and never
writes `"[no speech]"` (orchestrator gateway probe, 2026-11-12).
`nto.gemini-3.5-flash-lite` keeps the `"[no speech]"` text used by the
pre-D3 profile image. Malformed requests and unknown routes log protocol
errors and fail the run.

Director responses use valid cast IDs, with distinct speakers when two are
selected. Steering returns a valid adjustment on every fourth review and no
change otherwise. Actor SSE includes text, split tool-call deltas when a
scenario offers a tool, a final `finish_reason: "stop"` chunk, and `[DONE]`.
Director/steering use the Messages protocol's `stop_reason: "tool_use"`.
The current offered `end_conversation`
tool accepts `{}`. TTS handles registration, incremental input, flush,
keepalive and close, emits PCM16 at 24 kHz, then sends the final message.

Uniform distributions use the defaults below. `--fake-config file.json`
replaces selected fields. Equal low/high bounds give a constant. For example:

```json
{
  "seed": 20261112,
  "reply_words": {"low": 15, "high": 35},
  "tts_realtime_factor": 2.0,
  "second_speaker_probability": 0.25,
  "tool_call_probability": 0.01,
  "failures": {"tts_ws": [409, 429]}
}
```

| Setting | Default | Source and limit |
| --- | --- | --- |
| http_keepalive_s | 160 s | measured 2026-11-12: the Cornell gateway kept an idle keep-alive connection open > 150 s; the server's pool expires at 55 s. |
| models_ms | 10–30 ms | Assumption for a small local response. |
| stt_ms | 1,000–1,500 ms | Flash-lite observation, 2026-10-15, recorded in `server/voice/cascade.py:stt_model`; uniform shape is an assumption. |
| actor_first_ms | 1,400–2,600 ms | Argon first-token observation, 2026-10-15, in `server/voice/cascade.py:reasoning_effort`; uniform shape is an assumption. |
| token_ms | 25–55 ms per word-sized SSE piece | Uncalibrated assumption. |
| messages_ms | 300–900 ms | Uncalibrated director/steering assumption. |
| tts_connect_ms | 20.75–193.25 ms | Uniform fit to pooled connect p50=107 / p90=176 ms, 30 measured successes on 2026-11-12. Applied before WebSocket accept, separately from first audio. Local transport overhead is additional. |
| tts_first_ms | 486–696 ms | Rounded uniform fit to single-sentence text-to-first-audio p50=590.617 / p90=674.535 ms (n=15). Once per stream; handshake excluded. |
| reply_words | 15–35 words | Uncalibrated distribution centred on `server/engine.py:SPEECH_RULES`, about 25 words. |
| tts_realtime_factor | 9.3 | Rounded through-origin single-sentence audio-duration/generation-time fit (n=15; unrounded 9.3393). Each chunk sleeps audio-duration / factor, including its final chunk. |
| tts_words_per_minute | 189 | Rounded zero-intercept duration fit (n=15; unrounded 188.836). The unconstrained slope is not the fake's fit. |
| second_speaker_probability | 0.25 | Uncalibrated P(second speaker) in casts with at least two members. |
| tool_call_probability | 0.01 | Uncalibrated probability per actor response, only when the request offers a tool. `tool_call_every` overrides it for deterministic tests. |
| audio_chunk_ms | 100 | Packet-size assumption; a full PCM24k chunk has 4,800 bytes. |
| seed | 20261112 | Fixed seed; concurrent scheduling can change draw order. |

`--tts-factors 1.0,3.0` runs a ramp at each factor and reports capacities by
factor and a range. A missing capacity at either factor leaves the range
incomplete. The old sim report `tools/sim/reports/1469925.json` gives end-to-end
latency, not isolated stage calibration. Gateway timings, speaker counts and
tool rates remain assumptions. The pooled TTS profile does not reproduce
voice-specific effects, burstiness or sentence-dependent tail delays.

Calibration from future production records: pair each `turn_timing` interval
from `first_audio_from_gateway` to `assistant_done` with that reply's generated
audio duration. Divide audio duration by generation time to obtain the TTS
factor. Count words in `assistant_turn.text` for reply length. Use
`stage_direction` speaker counts for P(second speaker), and actor tool-call
counts divided by offered-tool responses for the tool rate. Save record IDs,
model/configuration, sample counts and distributions with any revised defaults.

## Measurements and verdict

Every mode keeps the fake-TTS destination guard active, including local and
Docker ramp/capacity runs and smoke runs. It blocks and records non-fake
destinations before transport dispatch. Capacity and smoke servers journal
blocked destinations only: successful fake connects do no destination-journal
open/write/close. Latency mode explicitly enables allowed-destination journaling
for its independent connection proof. URI checks remain part of the tested
harness configuration; this guard is not OS-level network isolation.

Active concurrency starts at the `session` frame and ends at hang-up.
Conversing concurrency starts at the first final user transcript and is
secondary. Both are sampled every second independently of resource reads.
The measurement window starts after the last scheduled arrival plus
`--warmup` (30 seconds), and ends when the first logical participant finishes.
Inter-encounter gaps remain visible. Less than 90% of N is INVALID unless a
recorded server failure explains the deficit, in which case it is FAIL.

Primary latency, turn counts, reply_missing, missing ACKs, voice errors and
upstream attempts use this window. A timing row belongs to the window of its
participant speech end, even if its playback ACK is written later. Earliest
played reply per (session, speech end) prevents group-member double counting.
Whole-step event totals are separate under `metrics.whole_step`.
Generator-lag samples have timestamps and use only the measurement window.
The probe stops before close polling; events are parsed once after settling.

The resource sidecar samples server PID 1 RSS and cgroup-v2 `cpu.stat` every
second outside the server cgroup. CPU% is 100 times the CPU-usage delta divided
by elapsed wall time, on the same one-core scale as Docker stats. It does not
run `docker exec` in the server during measurement. Reports retain raw samples,
`nr_throttled` deltas and throttled seconds per step. Server loop lag uses the
maximum reported five-second-window p99 and max; it does not take a percentile
of percentiles. Resource and lag timestamps delimit the primary window.

Track 0 attempts retain recovered failures and queue waits. Rows with
`detail="slot_wait"` contribute to queue statistics and a separate count, not
sent-attempt/error/cancellation counts. Durable bytes include the rendered mix
and exclude write amplification. Close-burst time runs from last hang-up to
all closed manifests and rendered mixes.

The printed rule gives INVALID for generator errors, lag p99 > 50 ms, short
runs or missing required measurements. Server failures give FAIL and a count
per 100 attempted encounters: HTTP >= 500, server-initiated close, incomplete
full-length encounter, close timeout and non-2xx video upload. A server failure
does not become INVALID because it prevented latency or concurrency samples.
A recorded generator error or excessive measured generator lag takes priority.

Other FAIL thresholds:

- played p90 > max(1.5 × N=1 p90, N=1 p90 + 2 s);
- reply_missing per 100 turns > N=1 + 1;
- first_played_missing per 100 turns > N=1 + 1;
- server loop-lag p99 > 250 ms, RSS > 900 MB, or a new non-benign voice-error kind;
- unknown fake routes or malformed fake protocol.

N=1 repeats pool raw latency samples and count denominators. Capacity is the
largest tested N whose repeats and every lower tested N all PASS. A later PASS
after a lower FAIL/INVALID adds a non-monotonic warning and cannot raise that
capacity. Reports call it **capacity for the tested configuration on this
host**. Exit status: PASS 0, FAIL 1, INVALID 2.

## Docker isolation, storage and profiling

Fake mode has an internal network with exactly `{server, fake, relay}`.
Server and fake have only this network and publish no ports. The fixed asyncio
TCP relay runs from the same image, starts on the normal bridge, then joins
the internal network. Only its port 8080 is published on 127.0.0.1. It always
forwards to `server:8080`; it accepts no destination from clients or arguments.
Fake stats use `docker exec`; logs use `docker logs`. Topology inspection
checks exact membership, server/fake network exclusivity, relay command,
dual networks and localhost-only publication.

Startup and the explicit proof check public-IP TCP plus DNS and resolved TCP
destinations for `api.elevenlabs.io`, the production gateway host and
`s3.amazonaws.com` from the server. DNS may resolve; every TCP attempt must
fail. Resource and profile sidecars use `--network none`.

SUT storage uses a Docker named volume. Close polling reads only manifests
and mix existence through the observer. One shared poller batches all pending
session IDs in one exec, with at least 0.5 s between polls. Workers await their
own session's result. `docker cp` exports encounter results
after each step, before deleting the volume. This does not model EFS latency.
The fake mounts only its cert, key, fake config and a read-only source-code
snapshot with the sim stimuli. The snapshot excludes reports and run data.
It cannot read server.env or server data. Production logging is retained
at info level with access logging; query `key=` values are redacted.

Reports record host CPU model, Docker architecture, VM CPU count, quota and
storage mount type. Each run first executes a fixed ten-second calibration
in a container with the same CPU/memory caps: JSON parse, base64 decode and
`audioop.ratecv` resampling of 100 ms PCM frames from 48 to 16 kHz. The score
is frames/second with a version and checksum. `--cpu-scale 0.8` makes the host
quota 0.4 CPU. `--fargate-reference file.json` accepts a stored score of the
same version and reports the host/reference ratio. No Fargate reference is
included and no AWS operation runs. No automatic capacity extrapolation is
made. Local debugging remains **not isolated, not CPU-capped**.

Profiling never runs in measured repeats. At the first FAIL only, if py-spy
is available, one extra repeat at that N is profile-only and excluded from
verdicts and capacity. Docker requires `--profile-image <local-image-with-py-spy>`.
The profiler shares the server PID namespace, gets SYS_PTRACE on the sidecar
only, and uses `--nonblocking` without `--native`. The server retains its
normal capabilities and seccomp policy. Missing py-spy or attach failure is
recorded. Default production images have no py-spy.

## Live gateway

`--gateway live --allow-live` loads only the gateway key. The default live cap
is 20 and requires a compatible ramp. TTS remains fake. Live mode uses a normal
Docker network for gateway egress and has no fake-mode isolation claim.
The 100 ms telemetry watcher kills the server before cancelling participants
or waiting for close. Every live guard uses the same rule: stop immediately on
the first gateway 429, 401 or 403 for any purpose, including steering/director.
Only reply-path purposes `preflight` (models or messages), `stt` and `actor`
count in the >5% error-rate rule, which applies after at least 20 such attempts.
Steering, director, unknown purposes, fake TTS and slot-only waits contribute
neither errors nor attempts to that rate. Configuration records the counted
purpose list and the shared rule. JSON and Markdown retain every gateway error's
route, purpose, outcome and detail, including recovered errors on a PASS.
Rate-stop `attempts`/`errors` are the counted reply-path values;
`gateway_attempts`/`gateway_errors` retain the totals across all gateway purposes.
The stop's route/purpose/outcome/detail identify the last counted error, with
per-purpose counted error totals and a separately labeled threshold-crossing
`trigger` row. A successful trigger does not replace the error attribution.
It also stops if observer output is stale for more than two seconds, the
observer log task ends, or the observer reports an error. Malformed observer
lines are counted and retained in the log; the live stop records the cause
and observer status. Each step reports `observer_telemetry` diagnostics.
Route, purpose, concurrency and counts are saved. Late attempts and startup
attempts also pass through the guard. Tests use mocks only.

Reports default to `tools/load/reports/<run>/report.{json,md}`. Each factor has
its own artifact subdirectory. Generated reports/data are ignored by Git.
Runtime errors still produce a report. A report directory must be fresh.

## Docker checks

Real Docker pytest checks run only with `RF_LOAD_DOCKER_TESTS=1`. Ordinary
pytest skips them before any daemon access or image build.

Ceiling-end smoke (also works with `--backend local`):

```sh
python -m tools.load.run --backend docker --gateway fake --ramp 1,2 \
  --scenarios S2A --steps 'tone:1,cycle:a1|a2:15,tone:3' --encounter-seconds 15 \
  --warmup 1 --webcam --report-dir /private/tmp/rf-load-ceiling-smoke
```

`--encounter-seconds` sets server ENCOUNTER_MIN_SECONDS=0, WRAP=80% of the
requested ceiling, and MAX to the ceiling through the harness environment.
It does not use the client `--duration` path. This diagnostic is INVALID for
capacity and normally exits 2. Each timeline must have `completed=true`,
`completion_reason="ceiling"`, no `duration_limit_reached`, and no server or
generator errors. Close errors observed after completion remain in
`post_complete_close`, including task, code/reason and sent/received close
frames. Step metrics and the Markdown report retain these diagnostics; they
do not fail the step. The same close before completion remains a server error.

```sh
python -m tools.load.run --backend docker --gateway fake --isolation-only \
  --report-dir /private/tmp/rf-load-r1-isolation
python -m tools.load.run --backend docker --gateway fake --ramp 1,2 \
  --scenarios S2A --steps 'tone:1,cycle:a1|a2:45' --duration 45 --warmup 5 \
  --webcam --report-dir /private/tmp/rf-load-r1-smoke
python -m tools.load.run --backend docker --gateway fake --ramp 1,5,10,20,30 \
  --repeat 1 --tts-factors 1.0,3.0 --continue-after-fail \
  --report-dir /private/tmp/rf-load-r1-full
```

Isolation must exit 0 with topology and egress passed. A clean short smoke
exits 2 with both Ns, nonzero measurements and no server/fake/generator errors.
The full command visits all Ns even after FAIL, retaining prefix capacity.
Its default four-scenario workload takes about 6 h 40 min, plus setup and
close settling. An available profiler can add one 40-minute repeat. Existing
local images can be selected with `--image`; otherwise the repo image builds
when absent. Each command uses a fresh report directory.


## Fast iteration

```sh
python -m tools.load.run --smoke --gateway fake --report-dir /private/tmp/rf-load-fast-smoke
python -m tools.load.run --gateway fake --encounters-per-participant 1 \
  --ramp-search 20 --search-ceiling 40 --tts-factors 1.0,3.0 --webcam \
  --profile-image rf-load-profile:f3b8cfb --report-dir /private/tmp/rf-load-fast
```

The CLI runs a smoke gate before a long ramp, once per configured TTS factor.
`--skip-smoke` opts out and records the omission. `--smoke` runs only the gate.
The gate has its own runtime and removes its containers before the capacity
runtime starts. It cannot provide a capacity estimate: `capacity_eligible=false`.
The diagnostic verdict requires a successful same-TLS-socket HTTP probe (one
response, six seconds idle, another response with no reconnect), then five
single-encounter participants spanning all four scenarios. A room-only subset
(`--scenarios S3A,S4A`, or either room alone) instead runs one N=1 participant
per selected room in sequence, so `--max-concurrency 1` is sufficient. Each speaks a short
committed line, then sends tone through the 60-second server ceiling. Webcam,
playback for each participant, closed manifests, rendered mixes and valid
instrumentation are required. Post-completion closure is diagnostic; an earlier
closure is a failure. The diagnostic count itself is not an assertion.
The smoke concurrency window starts at the last session frame and ends at the
first server completion. Setup and artifact settling do not lower achieved
concurrency. Speech/playback evidence covers each complete encounter.

`--encounters-per-participant all` is the unchanged workload default.
Integers 1–3 limit the number of full encounters for each participant. K=4 is
rejected; use `all` for the complete selected run. The full
mix follows the first/current encounter assigned by the server's Williams run
and advances after each completed encounter, including the last requested one.
It never substitutes a different scenario before advancing. The report records
actual scenario counts and encounter positions, rather than promising a fixed
index rotation. Selected scenario subsets retain their existing rotation and
no-advance behavior. K=1 omits later run positions and three transitions; it is
**a different workload from K=all**, not proof of equivalent sustained capacity.
Retain a final K=all pre-pilot run.

For K=1, one N=1 baseline repeat uses one sequential fresh participant per
selected scenario (four for the default mix), each completing one full encounter.
Each gets its own warmup and measurement
window. The server-assigned first encounters must cover the selected scenarios;
missing coverage makes the baseline INVALID. Each unit validates independently.
The repeat pools these units, and all N=1 repeats freeze the allowed voice-error
kinds before a higher N runs. A subset uses one unit per selected scenario,
with successive rotation offsets so every selected scenario is represented.
Numeric K=2–3 similarly uses separate N=1 units
with that K. A step and baseline with different K are INVALID. Higher-N reference
latency distributions and missing-reply/ACK rates are weighted by the step's
measured participant-turn counts per scenario. The report includes those weights.
Participant state resets between encounters; identity/cookies persist for the
requested K. The server runtime and gateway pool persist across ramp steps,
except that every search point below a recorded FAIL gets a fresh runtime,
including fresh fake/relay/observer state and CPU calibration. The N=1 reference
stays frozen. Reports retain each runtime's generation, directory, startup
checks and calibration. Fixed ramps keep their original runtime policy.
Reports also record the fake source SHA-256. Quiet flash-lite now emits
`[no speech]`; event mixes differ from the earlier fake's empty transcripts.

`--fail-fast` is on for full-length steps; `--no-fail-fast` disables it. It stops
on recorded server errors and fake route/protocol errors, including during
warmup, because the final verdict already fails these errors. RSS >900 MB,
a completed five-second server lag
window with p99 >250 ms, or a voice-error kind outside the frozen N=1 set
trigger a stop only inside the measurement window.
Encounter events stream from the observer, fake errors come from logs, and
worker exceptions are observed as they finish. Recovered errors remain recorded.
Latency percentiles and missing-reply/ACK rates never cause an early stop.
Generator errors and excessive generator lag still give INVALID. An intentional
stop excuses measurements it prevented, and does not excuse recorded low achieved
concurrency before the stop or other independent invalid evidence. The
measurement window ends at the stop timestamp, before cancellation lowers
concurrency. Live attempt/observer guards continue through artifact settling
and kill the server immediately on a live safety stop. Teardown and the one optional
extra profile-only repeat still run; profiling never affects capacity verdicts.

`--ramp-search [target]` replaces a fixed ramp list. Its default target is 20.
N=1 must PASS; FAIL or INVALID there stops the search. Above N=1, INVALID retries
once at the same N and aborts if still INVALID, without changing search bounds.
PASS increases by `--search-step` (default 10), clamped to `--search-ceiling`
(default 40) and the live cap. FAIL bisects the passing/failing bracket until
`--search-resolution` (default 5) is reached. A custom target uses that same
increment rule. All PASS terminates at the effective ceiling. Each decision is
printed and saved. Superseded INVALID attempts remain in history; a recorded
FAIL is retained. Capacity uses the sorted passing prefix of resolved tested
points. Untested inclusive intervals and non-monotonic results are explicit;
search does not establish that skipped participant counts pass.

The N=1 baseline still costs about 40 minutes per factor. Three or four total
search points cost about 60–70 minutes per factor before setup, smoke, settling
and profiling. The default bracket can require 80 minutes per factor; two
factors with the default search normally need about 2 h 20 min–2 h 40 min plus smoke/setup. A first-failure profile
adds one approximately 10-minute K=1 repeat. INVALID retries add another full
step each. These are workload-duration estimates, not measured runtimes.

The low-level `run_experiment` API runs the capacity experiment only; the CLI's
`run_with_smoke` orchestration owns the isolated gate. Direct API callers must
choose that orchestration if they require the same gate. Reports record the
choice. Real Docker probe checks require `RF_LOAD_DOCKER_TESTS=1`.

## Room runs against a selected checkout

```sh
python -m tools.load.run --backend local --server-checkout /Users/haocheng/code/rf-integ2 \
  --gateway live --allow-live --max-concurrency 1 --scenarios S3A,S4A \
  --fake-tts-profile calibrated --smoke --report-dir /private/tmp/rf-room-smoke
```

Replace `--smoke` with `--ramp 1 --webcam` for full-length room encounters.
The full run first completes the room smoke gate in a separate runtime.
The default K=all runs S3A then S4A at N=1; subset runs do not advance the
participant's Williams run. A room smoke is diagnostic and cannot set capacity.

`--server-checkout` is optional; the default uses this harness checkout.
An explicit checkout requires `--backend local`. Parsing refuses sources
without upstream telemetry, `ELEVENLABS_WS_URL` or `STORAGE_PREFLIGHT` support
before reading keys or starting processes. Room/ramp runs do not require reply
traces. Pure main `e949063` lacks these seams; compare with a separate checkout
at `317556f` (main plus the upstream measurement seam). The operator creates
that checkout. The harness does not create or modify server checkouts.

The selected checkout supplies server code and Terraform production settings.
Reports record its commit, dirty state and model settings. Every session has
an `events` path in JSON and Markdown, including smoke sessions, for reading
`user_turn` and `assistant_turn` text. Session files stay in the report directory.
The existing shared live guard covers startup and attempts through settling.
Every settled step of an explicit checkout run also requires positive fake-side
TTS connections matching server ElevenLabs attempts, excluding slot waits.
A blocked destination or count mismatch fails the step. This proof uses the
blocked-only destination journal; allowed destinations are journaled only in
latency mode. Local runs have no CPU quota or network isolation.

## Reply-trace latency diagnostics

```sh
python -m tools.load.latency --server-checkout "$RF_LATENCY_SERVER_CHECKOUT" \
  --gateway live --allow-live --participants 1 --scenarios S1A,S2A --turns 24 \
  --seed 20261112 --fake-tts-profile calibrated \
  --report-dir "$HOME/code/rf-load-runs/latency-a6"
```

Set `LITELLM_API_KEY` in the operator's environment, or use the existing
harness checkout's `.env` credential loader. The key is never written into
the report. `--live-url` overrides the selected checkout's production gateway.
The local backend imports server code and production configuration from
`--server-checkout`; harness tools come from this checkout. It suppresses the
selected checkout's `.env` readers before importing the server. The TTS URL
always points to the local fake and the ElevenLabs key is synthetic. No real
ElevenLabs request is required. This backend has no CPU cap or egress isolation.

`RF_LATENCY_SERVER_CHECKOUT` must name the integration checkout that includes
D3/D4/reply traces and the upstream seam (`f1fb333`, `317556f`). Pure rf-a6
does not have the seam and is refused before a key is read or a child starts.
The source preflight names missing telemetry API definitions, an
`ELEVENLABS_WS_URL` configuration read, a `STORAGE_PREFLIGHT` configuration
read, or the latency trace schema/header contract. The same seam check applies
to any selected checkout passed directly to `Runtime`. It reads Python ASTs;
it does not import or execute the selected server and does not build/merge the
integration checkout.

The server bootstrap guards modern/public/legacy WebSocket connect entry
points before importing the selected server. Only the configured fake origin
and path are allowed; queries are not logged. Wrong destinations are recorded
and blocked before transport dispatch in every mode. Latency mode additionally
journals allowed connects; ramp/capacity and smoke modes journal blocked
destinations only. At run end the server is stopped while
the fake remains available. A positive fake-side TTS connection count must
exactly match both the server's ElevenLabs attempt rows (excluding slot-only
waits) and allowed destination records. Any blocked destination, zero count,
unavailable proof or mismatch fails the diagnostic. Proof counters and reasons
are saved in JSON/Markdown. This is a conservative proof: interrupted attempts
that never reach the fake can make the diagnostic fail. It does not alter the
frozen server telemetry interface or provide OS-level egress isolation.

Each participant completes one scripted encounter per selected 1:1 scenario.
`--turns` selects the number of committed synthetic lines, shuffled by the
fixed seed and repeated as needed. Each `say` waits for playback/settling;
these are scripted participant utterances, not a promise of exactly one model
request per utterance. A timeout defaults to 60 seconds per turn plus 60;
the diagnostic server ceiling is another 30 seconds later. The participant
hangs up after its script. Live attempt and observer safety guards remain on.
`--tts-slots 1` changes the server's `ELEVENLABS_CONCURRENCY` for contention
diagnostics. Group scenarios are rejected. No N=1 reference or capacity verdict
is calculated; `capacity_eligible=false`.

Raw, complete session events are copied without rewriting to
`sessions/<session_id>/events.jsonl` in the report directory after artifact
settling. The runtime directory retains original events and other session
artifacts. Reports index `reply_trace`, `commit_trace` and `turn_timing` counts,
the selected checkout revision/dirty state, workload seed and effective fake
profile. Missing traces or incomplete scripts fail the diagnostic. The command
prints the gateway request count, including startup/preflight attempts and
excluding TTS and slot-only waits. The count comes from upstream-attempt
telemetry; it is not a token or billing estimate. Server teardown must finish
to preserve the buffered A6 traces. Playback is a byte-duration simulation;
browser receipt, scheduling and clock telemetry remain outside this harness.
The command also prints its verdict. Startup, between-scenario and final
gateway attempts pass through the live safety guard. Reply traces must use
`units: "seconds"`, and their count must cover observed assistant starts;
session counts remain adjacent to sent-line and reply-start counts in reports.
Every SIGTERM sets an interruption flag and cancels the main task, including
repeated signals. An interrupted run is INVALID even if worker code absorbs
cancellation. Shielded cleanup still stops/reaps both children and writes the
report; another SIGTERM cannot cancel that cleanup task.
A nonempty report directory, including one left without report.json after an
interrupted run, is refused. SIGKILL cannot run cleanup; inspect and stop any
surviving processes before choosing a new directory.

Fake-only parity settings can be supplied with `--fake-config` and
`--gateway fake`. Live mode sends actor/STT work to the gateway, so these fake
gateway settings affect only fake-mode tests:

```json
{
  "actor_sse_mode": "fragmented",
  "actor_replies": ["A first reply with no punctuation", "A first reply ending in punctuation."],
  "stt_plan": [{"delay_ms": 2000}, {"delay_ms": 0}, {"status": 503}, {"content": null}, {"content": ""}],
  "tts_connect_ms": {"low": 1000, "high": 1000},
  "tts_close_ms": {"low": 500, "high": 500},
  "tts_final_flush_only": false
}
```

Fragmented SSE splits words, punctuation and whitespace into separate deltas.
It adds no terminal whitespace. The two example replies therefore release
their first sentence only at the model's final flush. All actor/STT successful
choices retain `finish_reason: "stop"`. `stt_plan` entries select successive
non-streaming audio request arrivals; delays can reverse completion order,
status injects a failure, and an explicit content key can give empty/no-speech.
Cancellation of a delayed hearing remains a client/server operation and is
logged by the fake when its handler is cancelled. Commit item IDs are absent
from the gateway payload, so a fake cannot address those IDs directly; the
server retains the request-to-commit association. Concurrent sessions and
speech-check requests share the arrival counter. Use N=1 and a known request
sequence for controlled cases. `tts_final_flush_only=true` additionally holds
provider audio until `close_socket`; it is a fault scenario, not part of the
calibrated profile. Slow connect/close and `--tts-slots` provide contention and
cancellation stimuli; D4 joins, held/withheld output and early ACK decisions
belong to the selected server and participant path, not fabricated trace rows.
