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
and only the two credentials taken from `.env`. AWS credentials are stripped
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
check fails for it, saying so, until it is recorded. **All four are pending as
committed.** S1A and S2A were recorded on 24c / 24b (a736d51) and are not
comparable with 28a; S3A and S4A were never recorded. The gateway refused
every realtime connection from about 22:44 UTC on 2026-09-28 and still did at
01:01 UTC on 2026-09-29, so record all four with `python -m tools.sim.check
--write-baseline` on the first run that can reach the gateway, in the PR that
ships 28a.

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
