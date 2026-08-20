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

**Encounters recorded on the server are not persistent.** The Fargate task has no
volume and writes to the container filesystem; the S3 study bucket is still
empty. Every deploy, crash, or restart destroys whatever was recorded.

Until storage moves to S3, treat the deployed app as a pilot instrument only,
and **pull any encounter you care about before the next deploy** (see
[Getting data off the server](#getting-data-off-the-server)).

```bash
# Confirm the situation for yourself:
TD=$(aws ecs describe-services --cluster relational-fluency --services platform \
      --query "services[0].taskDefinition" --output text)
aws ecs describe-task-definition --task-definition "$TD" \
  --query "taskDefinition.[volumes,containerDefinitions[0].mountPoints]"   # [[],[]] = ephemeral
aws s3 ls s3://relational-fluency-study-data/ --recursive | head            # empty = nothing archived
```

---

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
# Participant URL for a scenario
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

The deployed app exposes each encounter as a zip. Do this **before** any deploy.

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
`cohort`, `completion_code`, whether it finished, and the `session_id` of every
encounter it produced, which is the key into the encounter records, audio, and
transcripts. Filter test traffic out with `?cohort=study`, or inspect only test
runs with `?cohort=internal`.

## The participant URL (Qualtrics → app → Qualtrics)

**Put this in Qualtrics**, at the point where participants move from the WEIP
survey to the encounters:

```
https://rf.ai-ready-workforce.ai.cornell.edu/start?pid=${e://Field/ParticipantKey}&key=<SESSION_KEY>
```

- `${e://Field/ParticipantKey}` is Qualtrics piped text — replace
  `ParticipantKey` with whatever the embedded field holding the CloudResearch
  Connect key is actually called in your survey.
- `<SESSION_KEY>` is the app's access gate, from Secrets Manager:
  `aws secretsmanager get-secret-value --secret-id relational-fluency/agent-api-key --query SecretString --output text`
  It stops drive-by access; it is not secret from participants.

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
