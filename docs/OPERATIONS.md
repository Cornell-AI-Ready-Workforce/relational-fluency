# Operations cheat sheet

Checking the server is healthy and the data is intact. Every command here has
been run against the live stack.

Set these once per shell:

```bash
cd ~/relational_fluency
export RF=https://rf.ai-ready-workforce.ai.cornell.edu
export KEY=$(aws secretsmanager get-secret-value \
  --secret-id relational-fluency/agent-api-key --query SecretString --output text)
```

---

## ⚠️ Read this before collecting anything

**Pull any encounter you care about, and do it before the next deploy** (see
[Getting data off the server](#getting-data-off-the-server)). That is the
standing rule. Everything below is why it is still the rule.

**Ask the running service whether `/data` is persistent — do not assume it.**
The EFS filesystem and its `/data` mount are written in
`infra/terraform/ecs.tf` (`aws_efs_file_system.study`, `aws_efs_access_point.study`,
the `mountPoints` entry on the platform container), but Terraform source is not
a running service: it protects nothing until someone runs `tofu apply`, and the
release pinned in `infra/terraform/terraform.tfvars` was cut before those
resources existed. Between merging the branch and applying it, the deployed task
is still the ephemeral one. So run the check first and read the paragraph after
it in light of the answer:

```bash
TD=$(aws ecs describe-services --cluster relational-fluency --services platform \
      --query "services[0].taskDefinition" --output text)
aws ecs describe-task-definition --task-definition "$TD" \
  --query "taskDefinition.[volumes,containerDefinitions[0].mountPoints]"
# [[],[]]                        → ephemeral task, no volume. Every deploy,
#                                  crash, or task retirement destroys whatever
#                                  was recorded since your last pull.
# an EFS volume + a /data mount  → records survive a deploy, a crash, and task
#                                  retirement. Nothing else changes.
```

Once that command shows the volume and the mount, pulling data stops being a
race against the next rollout and becomes redundancy. It does not become
optional, because nothing downstream of the write exists yet:

- **Nothing archives to S3.** Only the webcam video goes to the study bucket
  (uploaded browser-direct by `server/video.py`). Session audio, transcripts,
  events, and manifests live on one filesystem and nowhere else — no second
  copy, no backup policy, no snapshot schedule. A deleted access point, a
  fat-fingered `tofu destroy`, or a corrupted write takes the only copy of an
  irreplaceable encounter with it.
- **There is no deletion path.** No retention rule and no per-participant erase,
  so a withdrawal request under the IRB data-management plan has to be carried
  out by hand on the volume. Know that before you promise a participant one.

```bash
aws s3 ls s3://relational-fluency-study-data/ --recursive | head   # video only; no session records
```

---

## Before every deploy: is anyone mid-encounter?

A rollout starts a new task and retires the old one about two minutes later,
which cuts any conversation running on the old task (Fargate caps the stop
timeout at 120 s, so no drain setting can save it). Twice now a team member's
test conversation "stopped after a few turns" because it began during a
rollout. Two rules:

1. Do not apply while anyone is in an encounter. Check first, and wait until
   it reports zero:

```bash
curl -s https://rf.ai-ready-workforce.ai.cornell.edu/health | python3 -c "import json,sys; print('active sessions:', json.load(sys.stdin).get('active_sessions'))"
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
curl -s $RF/health | python3 -m json.tool
```

`status: ok` means the process is serving. `gateway.ok: true` means it can reach
the model gateway — if that is `false`, pages load but **no encounter will
work**, and `gateway.detail` says why.

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
curl -s "$RF/api/scenarios?key=$KEY" | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('study scenarios:', [x['id'] for x in d if x.get('study')])"
```

Expect all eight: `S1A S1B S2A S2B S3A S3B S4A S4B`.

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

```bash
# What is on disk locally
ls -t data/sessions | head
python3 -c "
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
to S3, so still pull each wave and keep it under the IRB data-management plan.

```bash
# List encounters on the server
curl -s "$RF/api/encounters?key=$KEY" | python3 -c "
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
curl -s "$RF/api/encounters?key=$KEY" | python3 -c "
import json,sys
print('\n'.join(e['id'] for e in json.load(sys.stdin)))" | while read SID; do
  curl -s -o "$SID.zip" "$RF/api/sessions/$SID/download.zip?key=$KEY"
  echo "pulled $SID"
done
```

## Reading the steering trail

```bash
echo "$RF/director?key=$KEY"
```

Shows each encounter labelled by construct and variant, scene headings from the
research note, every stage direction above the reply it produced, and coverage
(triggers reached out of planned, ESCI items exercised).

## Deploying

```bash
SHA=$(git rev-parse --short HEAD)
REPO=$(tofu -chdir=infra/terraform output -raw ecr_repository)
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "${REPO%%/*}"
docker build --platform linux/amd64 -t $REPO:$SHA .        # amd64 matters on Apple Silicon
docker push $REPO:$SHA
sed -i '' "s|platform:[a-f0-9]*\"|platform:$SHA\"|" infra/terraform/terraform.tfvars
tofu -chdir=infra/terraform apply
```

Rollout waits for the new task to pass health checks before draining the old
one, so an encounter in progress is not cut off — but **anything recorded on the
old task is gone**. Pull first.

## When something is wrong

| Symptom | First check |
|---|---|
| Page loads, mic "does not work" | `curl -s $RF/health` — if `gateway.ok` is false, no encounter can run |
| WebSocket opens then closes instantly | Application logs — a server-side exception during session creation looks exactly like a dead mic |
| 503 from the domain | Target health, then service events: usually no healthy task |
| `No scenario: SxX` | Deployed image predates the scenario bank — check the running image tag |
| Agent replies but no transcript | `verify_record` — look for `transcript_missing` |
| Encounter ends after ~3 turns | `INTERACTION_MIN_TURNS` / `INTERACTION_MIN_SECONDS` on the task |

---

## The two URLs

### 1. Internal testing (bug hunting)

```
https://rf.ai-ready-workforce.ai.cornell.edu/test?name=jennie&variant=A
```

- `name` labels the run so a bug report can say whose session it was.
- `variant=A` or `variant=B` pins all four scenarios to one form; omit for the
  randomized mix.
- These runs are tagged `cohort=internal` and are excluded from study data by
  that tag; they can never be mistaken for a participant.

### 2. The study URL (Qualtrics → app → Qualtrics)

Full linkage: CloudResearch key ties recruitment to the survey, the Qualtrics
response id ties the survey response to the app run, and the completion code
carried back ties the run to the follow-up survey.

```
https://rf.ai-ready-workforce.ai.cornell.edu/start?pid=${e://Field/ParticipantKey}&qid=${e://Field/ResponseID}
```

`ResponseID` is built into Qualtrics (pipe it via embedded data); `qid` is
stored on the run, so each run knows exactly which survey response preceded it.

### Joining the data afterwards

```bash
curl -s "$RF/api/runs?key=$KEY" | python3 -m json.tool
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

**Check this after the first arrivals of every wave, before the wave fills up:**

```bash
# Should be empty. Anything here is a real participant you cannot attribute.
curl -s "$RF/api/runs?key=$KEY&cohort=unattributed" | python3 -c "
import json,sys
rows=json.load(sys.stdin)
print('unattributed runs:', len(rows))
for r in rows:
    print(' ', r['run_id'], r.get('participant_key_status'), r.get('created_at'))"
```

A non-empty result means fix the Qualtrics `ParticipantKey` piping now (see
[The participant URL](#the-participant-url-qualtrics--app--qualtrics)). The runs
already recorded can only be re-joined by hand, and `/api/runs` does not carry
the raw value — read it off the run document on the volume, where `/start`
stored it:

```bash
curl -s "$RF/api/runs?key=$KEY&cohort=unattributed" \
  | python3 -c "import json,sys; print('\n'.join(r['run_id'] for r in json.load(sys.stdin)))"
# then, per run id, on the volume ($DATA_DIR/runs — /data/runs on the server):
python3 -c "import json;d=json.load(open('/data/runs/<run_id>.json'));print(d['participant_key_status'], repr(d['raw_participant_key']))"
```

That value is only useful if the broken pipe happened to send something
identifying; often it is an empty string, and then the recruitment record and
the recording cannot be joined at all. The server also prints a `WARNING` line
per bad arrival, but a stdout line is not a check; this query is.

## The participant URL (Qualtrics → app → Qualtrics)

**Put this in Qualtrics**, at the point where participants move from the WEIP
survey to the encounters:

```
https://rf.ai-ready-workforce.ai.cornell.edu/start?pid=${e://Field/ParticipantKey}
```

- `${e://Field/ParticipantKey}` is Qualtrics piped text — replace
  `ParticipantKey` with whatever the embedded field holding the CloudResearch
  Connect key is actually called in your survey.

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
