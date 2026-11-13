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

## What the characters said

The numbers above count what the pipeline did. None of them reads a reply: a
trigger that fired and a character who then said something else is a delivered
beat to all of them. So the check also runs `tools/sim/judge.py` on each
scenario's record. One model call holds every character line to the direction
it was handed, to the scenario file and to the fiction; two more, a few hundred
tokens each, are made only when the first flags something.

| Result | Meaning | Fails the scenario |
|---|---|---|
| beats N/M | turns handed a direction whose line made the move, in whole (`y`) or in part (`part`), of those judged; `lost` counts the beats nobody could judge (talked over, never heard, never spoken) | when N is more than `judge_made_drop` below the baseline's |
| esci>=3 N/M | ESCI items that got at least three beats that were made | no |
| FACT | a line states as true something the scenario says otherwise | always |
| ROLE | a line breaks the fiction (speaks as an AI, reads a direction aloud, narrates, speaks someone else's lines) | always |
| SAFE | a line crosses the bounds every brief ends with | always |
| rule | a line does what its own character's brief forbids outright | no |
| loop, stock | a character repeats its own line; a banned acknowledgement ("I hear you") | no |
| disputed | flags only some of the readings confirmed | no |

A flag is a finding only when the words it quotes are really in the scenario
(or in the line) and both second readings of that one line, made without the
first answer, agree. One that only some readings confirm is listed as
`disputed` and fails nothing. A beat is not made only when every reading says
so, and never on a line the participant talked over. A judge that cannot
answer fails the scenario: no verdict is not a pass. So does a scenario whose
baseline has no `judge` entry, or one recorded by another judge model or
rubric: re-record it. `--no-judge` skips all of this, and the report says so at
its top level; a baseline cannot be recorded with it.

The same judge runs on any recorded encounter:

```bash
python -m tools.sim.judge data/sessions/<id>          # one line, and what failed it
python -m tools.sim.judge --all data/sessions --only S3A,S4A --json out.json
```

There it holds an encounter to the scenario file of the build that recorded it,
read out of git by the commit in the record's provenance. A record that names
no build (a local server), or a build this checkout does not have, is judged on
its beats and the fiction only, and the line says `FACT, rule not judged`;
`--working-tree` holds a session to this checkout instead, for one it has just
recorded. It sends the encounter's transcript to the gateway model named by
`JUDGE_MODEL` (default: the director's), the gateway the encounter was
recorded through.

How well the model reads is measured, not assumed. `tools/sim/judge_cases.json`
holds six written encounters (S1A, S2A, S4A; 61 character lines) in which every
line is labelled clean or planted with one known defect, written by authors who
had not seen the judge's wording and checked by a second reader. On
2026-11-13, in each of three runs:

```
judge selftest: 6 encounters, 61 labelled lines, rubric 97e0ab8d, nto.gemini-4-argon  [12 calls, 43.4k in 1.7k out]
  FACT 6/6 found, 0 false   ROLE 3/3 found, 0 false   SAFE 3/3 found, 0 false   RULE 3/3 found, 0 false
  beats not made 9/9 found, 0 false of 31 made
```

That is 24 planted defects and 37 clean lines: enough to say the judge is not
missing the plain cases or inventing them, not enough to put a rate on either.
On real encounters the plain findings repeat run for run and a borderline one
does not: a character naming two leavers where the scenario of that build named
a third was a finding in some runs and unconfirmed in others, and a beat whose
cue is to wait was made in some and not in others. Read a finding that
appears once as a line to look at, and `disputed` as the same. Run
`python -m tools.sim.judge_selftest` after changing the judge's prompts, its
constants or `JUDGE_MODEL`, and put the new numbers here.

## The dashboard

`python -m tools.sim.dashboard_data OUT_DIR` flattens every report in
`tools/sim/reports` into `sim_runs.csv` (one row per scenario per run) and
`findings.csv` (what the judge found in each), the tables the QA dashboard
charts. `--encounters` and `--selftest` add the judge's results on recorded
encounters and its selftest score. Run it after a sim check and upload the
files to the dashboard again. Of a recorded encounter it writes counts and
places only, never a line.

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
check fails for it, saying so, until it is recorded. None is pending as
committed. All four were recorded on 2026-11-13 from build ac07319, pipeline
2026-11-06a / room pacing 2026-11-04a (s_1794603648_182505,
s_1794603870_d09625, s_1794604520_f5de09, s_1794604724_9551d2): the first run
of 11-06a, and the first to carry the judge's counts (beats made 8 of 8, 9 of
9, 7 of 7 and 9 of 10, rubric 97e0ab8d). Read by hand against the run before
it (11-04a, `reports/1469925.json`): lines answered 8 of 8 heard, 26 of 27, 8
of 9 and 30 of 34, against 8 of 8, 24 of 25, 8 of 9 and 28 of 34; the same
triggers fired (7, 9, 7, 10); no phantom turn, voice_error or reply_missing in
either; p50 / p90 1.9 / 2.7, 1.9 / 2.7, 2.8 / 3.3 and 2.5 / 3.1 s, against
2.2 / 2.6, 2.1 / 3.0, 2.8 / 3.3 and 2.6 / 3.6 s. Its report
(`reports/ac07319.json`) fails, as a first recording must (there was nothing
to compare with), and on one thing a baseline does not excuse: the judge's
FACT on S4A, where Priya said Support's ticket volume peaks on the 23rd (her
brief: in the first two weeks of the month, which is why Support wants the
23rd) and Chris then ruled that week out in place of his own beat. Before
11-06a, the latency audit of 2026-11-04 bumped PIPELINE_VERSION and
ROOM_PACING_VERSION with a live run but no re-recorded baseline
(`reports/1469925.json`). Before that, all four
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
