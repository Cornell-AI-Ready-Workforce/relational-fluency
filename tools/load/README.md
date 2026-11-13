# Load harness

```sh
python -m tools.load.run --gateway fake --ramp 1,5,10,20,30 --repeat 3
```

The default uses one server from the repository Dockerfile, capped at 0.5 CPU
and 1 GiB. Each logical participant follows the server-assigned S1A–S4A
Williams order, using the same internal participant identity and HTTP cookie
jar across four full encounters. Run advancement uses the page's API. The
N=1 baseline therefore includes every scenario. Each encounter uses the
committed sim sequence and extends shorter sequences with their own stimulus
lines until the encounter completes. The default ceiling is production's
600 seconds per encounter. A default step can take about forty minutes.

`--scenarios S2A` restricts the workload to one scenario. Selected subsets use
a rotating order and do not advance the full four-scenario study run.
`--arrival burst` starts participants together; `--arrival stagger:2` spaces
them two seconds apart. `--webcam` sends 150,000 opaque synthetic WebM bytes
every two seconds to `/api/sessions/{id}/video-chunk`, with owner and sequence.
This exercises upload/storage cost, not browser video encoding or decoding.

`--steps 'tone:1,cycle:a1|a2:45' --scenarios S2A --duration 45 --warmup 5`
is a short diagnostic. It exercises the protocols but receives INVALID for
capacity. Full runs wait for `encounter_complete`. A failed generator or a
missing required measurement cannot receive PASS.

## Fake upstream

The fake implements `/v1/models`, ordinary and SSE `/v1/chat/completions`,
Anthropic `/v1/messages`, and the ElevenLabs dialogue websocket. The Docker
mode uses HTTPS/WSS with an ephemeral certificate. The platform trusts that
certificate plus the public roots, so the explicit live-gateway mode can also
use HTTPS. The fake TTS key is synthetic in both modes.

STT matches exact waveform anchors from `tools/sim/stim/*.ulaw` and returns
their committed transcripts. It handles leading/trailing quiet and joined
utterances. Unknown speech fails the fake protocol check instead of returning
a convenient canned transcript. Director choices come from the actual cast
IDs in the request. Steering returns a valid adjustment on every fourth review
and no change otherwise. Actor SSE includes text, optional split tool-call
deltas, and `[DONE]`. TTS accepts registration, incremental inputs/flush,
keepalive, and close; it sends PCM16 at 24 kHz in paced chunks and then final.

All distributions are uniform. A JSON file passed to `--fake-config` can
replace any field; unspecified fields keep the defaults. Equal bounds give a
constant. Example:

```json
{
  "seed": 20261112,
  "actor_first_ms": {"low": 1400, "high": 2600},
  "reply_words": {"low": 25, "high": 55},
  "failures": {"tts_ws": [409, 429]},
  "tool_call_every": 0
}
```

| Setting | Default | Source and limit |
| --- | --- | --- |
| models_ms | 10–30 ms | Harness assumption for a small local authenticated response; no stage measurement. |
| stt_ms | 1,000–1,500 ms | Local observation documented in `server/voice/cascade.py:stt_model`, measured 2026-10-15 for flash-lite. The uniform shape is an assumption. |
| actor_first_ms | 1,400–2,600 ms | Argon first-token observation in `server/voice/cascade.py:reasoning_effort`, measured 2026-10-15. The uniform shape is an assumption. |
| token_ms | 25–55 ms per word-sized SSE piece | Harness assumption, not a measured gateway distribution. |
| messages_ms | 300–900 ms | Harness assumption for director/steering; no isolated stage measurements available. |
| tts_first_ms | 150–350 ms | Harness assumption. No ElevenLabs key or current live calibration exists. |
| reply_words | 25–55 words | Workload assumption anchored to `server/engine.py:SPEECH_RULES` (about 25 words) and its documented 11/24/25-word observations; the upper tail exercises longer replies and queue holding. |
| tts_words_per_minute | 160 | Harness assumption for audio duration; synthetic tone, not speech. |
| audio_chunk_ms | 100 | Harness packet/cadence assumption; each full chunk is 4,800 bytes at 24 kHz PCM16. |
| seed | 20261112 | Fixed pseudo-random seed; concurrent request scheduling can change draw order. |

The old `tools/sim/reports/1469925.json` contains end-to-end measurements.
Those values do not calibrate individual upstream stages. These defaults are
a reproducible synthetic configuration, not a production capacity claim.

## Measurements and decisions

Every second the generator samples participants that have received their first
final transcript and have not hung up. This sampler is independent of Docker
resource collection. Achieved concurrency is the arithmetic mean after the
last scheduled arrival plus `--warmup` (default 30 seconds), through the first
logical participant's final hang-up. Startup, inter-encounter gaps, and all raw
samples remain in the report. Less than 90% of requested N is INVALID.

Generator loop-lag p99 above 50 ms is INVALID. Server loop-lag reports use
`LOOP_LAG_REPORT_S=5`; the step uses the maximum window p99 and maximum lag,
without taking percentiles of p99 values. CPU is from Docker stats. RSS is
the actual server PID 1 `VmRSS`, sampled separately from Docker's working-set
memory display. Both resource collection and concurrency are scheduled at
one second; actual sample timestamps are retained.

Latency is computed once per session and participant speech end, taking the
earliest played reply across members. Missing ACKs are counted separately.
The sim analyzer supplies lines said/heard/answered and its benign voice-error
exclusions. Upstream attempts, recovered failures, duration, and TTS queue wait
come from Track 0 telemetry. Cancelled attempts are reported separately and
are not errors; HTTP errors, timeouts, unexpected closure, and exceptions are.
Bytes written means the sum of durable encounter file sizes, including the
rendered conversation mix; it does not measure file-write amplification.
Close-burst time starts at the last client hang-up request and ends when all
session manifests are closed and every conversation mix exists.

N=1 repeats pool raw latency samples. Each step prints and saves this rule:

```text
INVALID if achieved concurrency < 90% of N or generator loop-lag p99 > 50 ms,
or required measurements/full encounters are missing. Otherwise FAIL if
played p90 > max(1.5 * N=1 p90, N=1 p90 + 2 s), reply_missing per 100
participant turns > N=1 + 1, server loop-lag p99 > 250 ms, RSS > 900 MB,
or a non-benign voice_error kind was absent at N=1.
```

Unknown fake routes/protocol errors fail the overall run. An invalid generator
still gives its step INVALID. Capacity is the highest N with every repeat PASS,
called **capacity for the tested configuration**. No valid N=1 baseline means
no capacity result. Exit codes are 0 for PASS, 1 for FAIL, and 2 for INVALID.

If py-spy is available for the target, each step samples ten seconds after
warmup, without `--native`. The first failing step's sampled top 20 functions
are included. The default production image does not install py-spy; an image
with it can be selected with `--image`. The container permits ptrace for this
optional profile. Absence or attach failure is reported explicitly.

## Isolation and live limits

Fake mode creates one `--internal` Docker network with exactly two members:
the server and fake upstream. The generator connects through localhost-bound
published ports. No host/extra network is attached. Startup and an explicit
proof both require TCP connections from the server to public IPs to fail.
Topology inspection also checks the internal flag and exact membership.

Environment values from `infra/terraform/ecs.tf` are resolved first. Fake
endpoint, synthetic credentials, and harness overrides are applied last.
Storage preflight and archiving are disabled. Webcam salvage is deferred for
one day so short-lived synthetic runs do not attempt backup promotion. The
`.env` readers cannot replace these overrides. No real AWS credentials enter
the containers. Run directories contain only synthetic encounter data.

`--backend local` is for debugging and is labelled **not isolated, not
CPU-capped**. It cannot run the isolation proof.

Live mode requires `--gateway live --allow-live`; its default maximum
concurrency is 20. A larger ramp is rejected unless the explicit cap changes.
Only the gateway key is loaded for this mode. TTS stays fake. A 100 ms
telemetry watcher stops on the first observed gateway 429, even if the SDK
later recovers, or when upstream attempt errors exceed 5%. It cancels active
participants and records route, purpose, and actual concurrency. Late terminal
attempts are checked before the next step. Missing startup telemetry and a
lost, truncated, or unreadable telemetry file stop the run.
No live gateway call is made by the test suite.

Reports are JSON and Markdown under `tools/load/reports/<run>/` by default.
An explicit report directory must not already contain a run.
Generated reports and synthetic data are ignored by Git. Explicitly selected
reports can be kept later. Runtime startup failures also produce a report.

## Docker smoke commands

```sh
python -m tools.load.run --backend docker --gateway fake --ramp 1,2 --repeat 1 \
  --scenarios S2A --steps 'tone:1,cycle:a1|a2:45' --duration 45 --warmup 5 \
  --webcam --report-dir /private/tmp/rf-load-smoke
python -m tools.load.run --backend docker --gateway fake --isolation-only \
  --report-dir /private/tmp/rf-load-isolation
```

The first command runs an N=1 baseline diagnostic followed by N=2. A clean
short smoke is INVALID for capacity and exits 2. A fake protocol error exits
1 and must be investigated. The isolation command must exit 0 and save
`isolation.passed=true` in `report.json`. Omit `--duration` and `--steps` for
a full encounter test. Existing local images can be selected with `--image`;
otherwise the image is built from the repository Dockerfile when absent.
