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
| tts_first_ms | 150–350 ms | Uncalibrated assumption; no ElevenLabs key. |
| reply_words | 15–35 words | Uncalibrated distribution centred on `server/engine.py:SPEECH_RULES`, about 25 words. |
| tts_realtime_factor | 2.0 | Uncalibrated assumption: generation speed divided by playback speed. Each chunk sleeps audio-duration / factor. |
| tts_words_per_minute | 160 | Assumption for synthetic audio duration. |
| second_speaker_probability | 0.25 | Uncalibrated P(second speaker) in casts with at least two members. |
| tool_call_probability | 0.01 | Uncalibrated probability per actor response, only when the request offers a tool. `tool_call_every` overrides it for deterministic tests. |
| audio_chunk_ms | 100 | Packet-size assumption; a full PCM24k chunk has 4,800 bytes. |
| seed | 20261112 | Fixed seed; concurrent scheduling can change draw order. |

`--tts-factors 1.0,3.0` runs a ramp at each factor and reports capacities by
factor and a range. A missing capacity at either factor leaves the range
incomplete. The old sim report `tools/sim/reports/1469925.json` gives end-to-end
latency, not isolated stage calibration. These timings, speaker counts and
tool rates remain assumptions.

Calibration from future production records: pair each `turn_timing` interval
from `first_audio_from_gateway` to `assistant_done` with that reply's generated
audio duration. Divide audio duration by generation time to obtain the TTS
factor. Count words in `assistant_turn.text` for reply length. Use
`stage_direction` speaker counts for P(second speaker), and actor tool-call
counts divided by offered-tool responses for the tool rate. Save record IDs,
model/configuration, sample counts and distributions with any revised defaults.

## Measurements and verdict

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
or waiting for close. It stops on the first gateway 429 or gateway-only attempt
error rate > 5%. Fake TTS and slot-wait-only rows never dilute that denominator.
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
single-encounter participants spanning all four scenarios. Each speaks a short
committed line, then sends tone through the 60-second server ceiling. Webcam,
playback for each participant, closed manifests, rendered mixes and valid
instrumentation are required. Post-completion closure is diagnostic; an earlier
closure is a failure. The diagnostic count itself is not an assertion.

`--encounters-per-participant all` is the unchanged workload default.
Integers 1–4 limit the number of full encounters for each participant. The full
mix follows the first/current encounter assigned by the server's Williams run
and advances after each completed encounter, including the last requested one.
It never substitutes a different scenario before advancing. The report records
actual scenario counts and encounter positions, rather than promising a fixed
index rotation. Selected scenario subsets retain their existing rotation and
no-advance behavior. K=1 omits later run positions and three transitions; it is
**a different workload from K=all**, not proof of equivalent sustained capacity.
Retain a final K=all pre-pilot run.

For K=1, one N=1 baseline repeat consists of four sequential fresh participants,
each completing one full encounter. Each gets its own warmup and measurement
window. The server-assigned first encounters must cover the selected scenarios;
missing coverage makes the baseline INVALID. Each unit validates independently.
The repeat pools these units, and all N=1 repeats freeze the allowed voice-error
kinds before a higher N runs. Numeric K=2–4 similarly uses separate N=1 units
with that K. A step and baseline with different K are INVALID. Higher-N reference
latency distributions and missing-reply/ACK rates are weighted by the step's
measured participant-turn counts per scenario. The report includes those weights.
Participant state resets between encounters; identity/cookies persist for the
requested K, while the server runtime and gateway pool persist across ramp steps.

`--fail-fast` is on for full-length steps; `--no-fail-fast` disables it. It stops
on irreversible observations inside the measurement window: server errors,
fake route/protocol errors, RSS >900 MB, a completed five-second server lag
window with p99 >250 ms, or a voice-error kind outside the frozen N=1 set.
Encounter events stream from the observer, fake errors come from logs, and
worker exceptions are observed as they finish. Recovered errors remain recorded.
Latency percentiles and missing-reply/ACK rates never cause an early stop.
Generator errors and excessive generator lag still give INVALID. An intentional
stop excuses measurements it prevented, and does not excuse recorded low achieved
concurrency or other independent invalid evidence. Teardown and the one optional
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
