# Relational Fluency

A research platform from the Cornell **AI-Ready Workforce Initiative**. A
participant holds four short voice conversations with AI characters in
workplace situations — a colleague who took credit for their work, a manager
who promised a raise, a team that has just lost two people — and the platform
records what they said, how they sounded, and what they looked like saying it.
Those recordings are the dataset from which we measure **relational fluency**:
how well a person handles the human side of work.

The first study on this platform is **Study 1**, a data-collection study.
Everything in this repository is in service of running it; the plan is
[`docs/study1-plan.md`](docs/study1-plan.md).

Study design: *Study Design Proposal v2* — Lee, Chun, Zhang, Slama, Joachims,
Kizilcec.

## What we measure

Four competencies from the ESCI **Relationship Management** cluster. Each has
one scenario, and each scenario is an *encounter*: two consecutive interactions
with a small cast, built around planted triggers that give the participant
something to respond to.

| | Construct | Scenario (form A, Study 1) | Cast | Shape |
|---|---|---|---|---|
| S1 | Conflict Management | Taken credit | Riley (colleague who pushes) · Sam (peer who took it) | 1:1 → 1:1 |
| S2 | Influence | Promised raise and a competing offer | Morgan (manager) | 1:1 → 1:1 |
| S3 | Inspirational Leadership | After resignations over pay | Alex · Jordan · Casey (a team) | group meeting → brief one-on-ones |
| S4 | Teamwork | Planning an internal rollout | Priya · Dan · Chris (a working group) | group → group |

Each construct also has a **form B**, a parallel form with the same trigger
sequence and the same ESCI item map on a different story. Study 1 fields form
A only (`DEFAULT_RUN_VARIANT=A`); B is reserved for a later study in which a
second attempt has to be on a form the participant has not met. The bank in
[`scenarios/v3/`](scenarios/v3/) is `S1A S1B S2A S2B S3A S3B S4A S4B`, CI holds
that exact set, and the per-form trigger map is generated into
[`docs/scenario-map.md`](docs/scenario-map.md). The scenario design is
[`docs/scenario-spec-v3.md`](docs/scenario-spec-v3.md); the constructs and
items are [`docs/competency-framework.md`](docs/competency-framework.md); the
canonical specs, grounded in an analysis of 39,301 r/antiwork posts, are in
[`reddit-analysis/scenarios/`](reddit-analysis/scenarios/).

## Study 1: the participant's path

```
CloudResearch Connect ── recruits and pays; participantId on the survey URL
        │
        ▼
Qualtrics, Survey 1 ──── consent (IRB text), self-report battery
        │                last page links to the app, in a new tab:
        │                /start?pid=${e://Field/participantId}&qid=${e://Field/ResponseID}
        ▼
This app ─────────────── one run, four encounters, form A, order counterbalanced
        │                entry check → camera and microphone check → four conversations
        │                each at least 7:00 (visible timer), wrap at 12:00, stop at 13:00
        │                voice both ways, live captions, webcam video, audio, transcript
        │                completion screen shows the code, then
        │                SURVEY_RETURN_URL?participantId=…&run_id=…&code=…
        ▼
Qualtrics, Survey 2 ──── remaining questions, then CloudResearch completion
```

Things about that path that the code enforces rather than hopes for:

- **Consent is taken in Qualtrics, before the app opens.** The app has no
  consent step and keeps no consent record. What ties an encounter to the
  person who consented is `qid`, the Qualtrics `ResponseID` on the entry link.
  A run without it is recorded but cannot be joined to a survey response.
- **Order is counterbalanced.** Each run gets a row of a 4×4 Williams Latin
  square, assigned round-robin per cohort, so every construct appears in every
  position equally often and each construct precedes each other one equally
  often. The row is written on the run document (`order`).
- **Seven minutes is a floor, not a target.** The character will not close an
  encounter before 7:00, the End button unlocks at 7:00, and the server refuses
  an advance before it. At 12:00 the character is told to wrap up; at 13:00 the
  encounter ends. *Stop and leave the study* works at any second; withdrawal is
  never gated.
- **Camera and microphone are both required.** A participant whose camera
  cannot be opened does not start; a machine with no microphone is told so and
  cannot skip past it.
- **Internal runs are marked.** The researcher's own door, `/test`, and the
  `cohort=internal` tag exist so the team's walkthroughs never land in the
  dataset. Study exports filter to `cohort=study`.

The pre-wave checks — the join key, the return URL, the port, a headset — are in
[`docs/OPERATIONS.md`](docs/OPERATIONS.md#read-this-before-collecting-anything)
and [`docs/TESTING-LOCALLY.md`](docs/TESTING-LOCALLY.md).

## How the app works

```
Browser (participant)              This server (FastAPI)              Cornell gateway
static/v2.html                     server/app.py                      LiteLLM → Gemini Live
  mic → PCM over WebSocket ──────▶ realtime_voice_session.py ────────▶ speech-to-speech
  ◀── agent audio + captions ───── relays audio both ways,            (one live session per
  webcam → S3 (presigned PUT)      detects end of turn, barge-in,       character)
                                   runs the encounter clock
                                   writes data/sessions/<id>/
```

- **The participant page** ([`static/v2.html`](static/v2.html)) is one file:
  the situation card, the camera and microphone check, the live conversation
  with captions and the timer, and the completion and withdrawal screens. It
  streams microphone PCM to the server and plays the character's audio back.
- **The broker** ([`server/app.py`](server/app.py)) mints runs and participant
  records, serves the pages, and owns the voice WebSocket. It holds the gateway
  key so it never reaches the browser.
- **The runner** ([`server/realtime_voice_session.py`](server/realtime_voice_session.py))
  is where an encounter lives: it opens a Gemini Live session per character
  through the Cornell LiteLLM gateway, relays audio, detects the end of the
  participant's turn itself (the gateway's own detection is inert through the
  bridge), handles barge-in, moves between the two interactions of an
  encounter, and enforces the floor, the wrap and the stop. S3 and S4 open in a
  **group room**, one live session per character with a director choosing who
  has the floor.
- **Scenarios** ([`scenarios/v3/*.yaml`](scenarios/v3/)) are specs — a setup
  written to the participant, a cast with briefs, two interactions with planted
  triggers mapped to ESCI items. [`server/scenarios_v3.py`](server/scenarios_v3.py)
  compiles them into the prompts each character receives; the rules every
  character shares (how to talk, when to stop) live in the compiler, not in
  each brief.
- **Runs** ([`server/runs.py`](server/runs.py)) are the unit of a participant's
  visit: four encounters, their order, the cohort, the Qualtrics id, the
  completion code. A run is a JSON document under `data/runs/`.
- **Storage** ([`server/storage.py`](server/storage.py)) writes each encounter
  to `data/sessions/<session_id>/` and indexes it in SQLite. Webcam video goes
  browser-direct to the study S3 bucket by presigned URL
  ([`server/video.py`](server/video.py)); when the server cannot sign one, the
  browser uploads to the app instead and the file lands beside the session.

One thing to know about the model. Every character carries its brief, its beats
and its personality from the moment the session opens, and that is what the
participant hears. The platform also has a *director* that composes a stage
direction per turn, but on the configured model (`nto.gemini-live-2.5-flash`)
a direction sent mid-encounter is accepted and silently discarded, so the
per-turn steering does not reach the character. The encounter is unaffected as
a recording; whether to stay on this model is a PI and IRB decision, written up
in the `PI-DECISION-realtime-model.md` memo that accompanies this repository.
The measurements behind that, and behind the runner's recovery constants, are
in [`docs/field-notes.md`](docs/field-notes.md).

## Repository map

| Path | What is there |
|---|---|
| `server/` | The FastAPI app, the voice runner, runs, storage, scenario compiler, Qualtrics client, record verification |
| `static/` | The participant page (`v2.html`), the researcher console, the demo and evidence views |
| `scenarios/v3/` | The eight scenario specs |
| `tests/` | The suite; `python -m pytest tests` runs it |
| `tools/` | `encounter_health.py` (did the encounter run as an experiment?), `gen_scenario_map.py` |
| `docs/` | The Study 1 plan, the operations runbook, the AWS deploy runbook, local testing, the scenario spec, field notes |
| `studies/study1/` | The rating instrument and ESCI item lists for the Qualtrics rating survey |
| `infra/terraform/` | The ECS/Fargate stack, the study bucket, secrets |
| `reddit-analysis/` | The corpus analysis the scenarios were grounded in |

## Quick start

**Python 3.11, 3.12 or 3.13.** 3.12 is the reference version — it is what the
production image and CI's pinned leg run. `requirements.txt` says what changes
on 3.13 and why it matters for the audio path.

Three blocks, one per shell. They are not interchangeable: `&&`, `source` and
the virtualenv layout all differ, and `python -m venv .venv && source
.venv/bin/activate` is a parse error in Windows PowerShell. Once the virtualenv
is activated, `python` is the project interpreter on all three platforms, and
every other command in these docs assumes that.

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
allow it for this window only:

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

**Use port 8765.** It is the default, and the study bucket's CORS allowlist
names `http://127.0.0.1:8765` and `http://localhost:8765` as the only local
origins. On any other port the webcam upload is blocked and reported as a
"network" error, which does not trigger the local fallback: the recording is
lost. Mic and webcam capture also need a secure context — `127.0.0.1` counts,
any remote host needs HTTPS.

Then open:

| | |
|---|---|
| Can it record at all? | <http://127.0.0.1:8765/health> — check this first, every time |
| Walk the study yourself | <http://127.0.0.1:8765/start?pid=selftest1&cohort=internal> |
| Researcher console | <http://127.0.0.1:8765/researcher> |
| Evidence trace | <http://127.0.0.1:8765/evidence> |
| Demo view, for showing the lab | <http://127.0.0.1:8765/demo> |

`&cohort=internal` is the researcher's own way in on a local checkout: it skips
the seven-minute floor and tags the run so every study export drops it. Use it
on every link you type by hand. A walkthrough of the whole study, with the
things a participant sees that look like bugs and are not, is
[`docs/TESTING-LOCALLY.md`](docs/TESTING-LOCALLY.md).

> **Your local webcam tests upload to the real study S3 bucket** when `.env`
> holds working AWS credentials. `&cohort=internal` keeps the run out of the
> study set; it does not stop the upload, so test objects need sweeping up
> before a wave.

## Configuration

Everything is read from `.env`; [`.env.example`](.env.example) documents each
variable. The ones that decide whether a wave works:

| Variable | What it does |
|---|---|
| `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` | The Cornell LiteLLM gateway and a virtual key for it. Every model call goes through here. |
| `REALTIME_MODEL`, `REALTIME_VOICE` | The speech-to-speech model and its default voice. Changing the model family is an IRB matter, because the consent form names the provider. |
| `DEFAULT_RUN_VARIANT` | `A` for Study 1. Which parallel form every construct is served. |
| `ENCOUNTER_MIN_SECONDS`, `ENCOUNTER_WRAP_SECONDS`, `ENCOUNTER_MAX_SECONDS` | The encounter clock: 420, 720, 780 by default. |
| `SESSION_KEY` | The researcher credential. Gates `/test`, `/researcher` and the downloads on any deployment; empty on a laptop. |
| `SURVEY_RETURN_URL`, `SURVEY_RETURN_LABEL` | Where the completion screen sends the participant: the second Qualtrics survey. |
| `QUALTRICS_API_TOKEN`, `QUALTRICS_SURVEY_ID`, `QUALTRICS_BASE_URL` | For pulling survey responses and joining them to runs. The base URL has to be the datacenter host (`yul1`), not the brand host. |
| `STUDY_CONTACT_NAME`, `STUDY_CONTACT_EMAIL`, `STUDY_IRB_PROTOCOL` | The contact sentence on every completion and withdrawal screen. |
| `DATA_DIR` | Where runs, sessions and the index are written. On the deployed task this must be the persistent volume. |
| `AWS_*` | Credentials that let the server sign webcam upload URLs for the study bucket. |

Model clients are built in [`server/llm.py`](server/llm.py) from `.env`, never
from ambient environment variables: the desktop app exports an
`ANTHROPIC_BASE_URL` of its own, and a gateway key sent to the wrong provider
is a 401 mid-encounter. Startup runs a preflight against the configured gateway
and `/health` reports the same, so a misconfigured endpoint is visible before
anyone joins.

```bash
curl -s localhost:8765/health | python -m json.tool
```

## Browsers

Participants are recruited from the public through CloudResearch, so this is a
requirement, not a preference: **Chrome, Firefox and Safari, on macOS, Windows
and Linux**. A participant whose recording silently fails is an encounter lost,
and because they were paid it is an encounter lost expensively.

|  | macOS | Windows | Linux |
|---|---|---|---|
| **Chrome** | supported | supported | supported |
| **Firefox** | supported | supported | supported |
| **Safari** | supported | Safari does not exist on this OS | Safari does not exist on this OS |

Minimum versions are deliberately not stated: nobody has run the page against
a pinned older build on real hardware, and a number recalled from memory is
worse than none because a screener will be written against it. Confirm on real
machines before one goes into the CloudResearch screener.

Recordings arrive in two containers, and both must play. The page picks the
first `MediaRecorder` type the browser admits, so Chrome and Firefox produce
**WebM/VP8+Opus** and Safari produces **MP4/H.264**. A browser that admits
neither cannot record and is told so at the camera check.

## The data

Every encounter writes `data/sessions/<session_id>/`:

| File | What it holds |
|---|---|
| `record.json` | The aligned record: participant and character turns in order, each character turn paired with the stage direction that shaped it, provenance (gateway and models), audio and video pointers, counts |
| `events.jsonl` | The raw append-only trail: every turn, latency, floor hold, wrap, retry, director route |
| `user_audio.wav` | The participant's channel |
| `assistant_audio*.wav` | The character channel, one file per character |
| `manifest.json` | Session metadata and durations |
| `webcam.webm` or `.mp4` | The video, when it fell back to the local path; otherwise it is in S3 under `encounters/<session_id>/` |

Runs are `data/runs/<run_id>.json` and carry the order, the cohort, the
Qualtrics id and the completion code; `GET /api/runs` exports them with the
researcher key. Participant audio, video and transcripts are PII: `data/`,
`logs/`, `*.wav` and `.env` are gitignored and must stay that way, and study
data belongs in the encrypted S3 bucket under the IRB data-management plan,
never in the repository.

Checking a capture, and joining it to the survey:

```bash
python -m server.verify_record <session_id>     # both audio channels, both transcript sides, the steering trail
python tools/encounter_health.py --all data/sessions   # did the encounter run as an experiment?
python -m server.retranscribe <session_id>      # a stronger offline transcript, stored beside the live one
python -m server.qualtrics join                 # survey responses ↔ runs, on the Qualtrics ResponseID
```

The live transcript is what the character heard and reacted to; it drops words
on a hesitant speaker, which is why the participant channel is re-transcribed
offline and both are kept. Recordings are then rated in Qualtrics on the 22
ESCI items ([`studies/study1/qualtrics/`](studies/study1/qualtrics/)); rating,
reliability and modelling happen outside this application.

## Testing

```bash
python -m pytest tests -q
```

The suite drives the participant page through node as well as the server
through Python, so node 20 is needed for the page tests (they skip without it
on a laptop and fail without it in CI). CI runs the suite on Ubuntu, macOS and
Windows across Python 3.11–3.13 and checks the scenario bank compiles to the
expected eight.

## Deploying

The study service runs on ECS/Fargate behind an ALB at
`rf.ai-ready-workforce.ai.cornell.edu`, from the image `Dockerfile` builds.
Every live task-definition revision so far was registered by hand with the AWS
CLI: the Terraform under `infra/terraform/` describes the stack, but its state
is not in the account, so `tofu apply` is not the release procedure today. The
release path in use, the permissions each step needs, and what it will take to
get back to Terraform are in
[`docs/DEPLOY-AWS.md`](docs/DEPLOY-AWS.md#read-this-first-the-runbook-and-the-practice-have-diverged).
Operating a wave — health, logs, the participant URLs, pulling data off the
server — is [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Where things stand

The app runs Study 1's path end to end on a laptop: entry, the camera and
microphone check, four counterbalanced form-A encounters with the seven-minute
floor, the completion code and the return link. Consent has been moved out of
the app entirely, the camera is required, and the code that was not on this
path — the rater pipeline, the scoring and debrief, the legacy scenarios, the
second study arm, the text-only channel — has been removed.

Open before a wave, in dependency order, tracked in
[`docs/study1-plan.md`](docs/study1-plan.md):

1. Verify the S3 and S4 group rooms live on the current build.
2. Give the deployed task a persistent volume so an encounter survives a deploy,
   and archive session records to S3 rather than only the webcam video.
3. Build Survey 2 and set `SURVEY_RETURN_URL`; extend the join script to both
   surveys.
4. Play through the four scenarios and settle the scenario text.
5. Confirm webcam capture on all seven browser and OS combinations.
6. Decide the realtime model (PI, then IRB).

## Further reading

- [`docs/study1-plan.md`](docs/study1-plan.md) — the plan, decisions and open items
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — running a wave
- [`docs/TESTING-LOCALLY.md`](docs/TESTING-LOCALLY.md) — walking the study on your own machine
- [`docs/DEPLOY-AWS.md`](docs/DEPLOY-AWS.md) — the AWS stack and the release path
- [`docs/architecture.md`](docs/architecture.md) — components and flows in more depth
- [`docs/scenario-spec-v3.md`](docs/scenario-spec-v3.md) — how an encounter is designed
- [`docs/competency-framework.md`](docs/competency-framework.md) — the constructs and ESCI items
- [`docs/field-notes.md`](docs/field-notes.md) — what driving the live voice model found
- [`docs/roadmap.md`](docs/roadmap.md) — the phases beyond Study 1
