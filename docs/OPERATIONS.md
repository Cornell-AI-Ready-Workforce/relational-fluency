# Operations cheat sheet

Checking the server is healthy and the data is intact. Every command here has
been run against the live stack.

Set these once per shell, from the repository root.

bash / zsh (macOS, Linux, Git Bash):

```bash
export RF=https://rf.ai-ready-workforce.ai.cornell.edu
export KEY=$(aws secretsmanager get-secret-value \
  --secret-id relational-fluency/agent-api-key --query SecretString --output text)
```

Windows PowerShell:

```powershell
$RF = "https://rf.ai-ready-workforce.ai.cornell.edu"
$KEY = aws secretsmanager get-secret-value --secret-id relational-fluency/agent-api-key --query SecretString --output text
```

Windows Command Prompt:

```bat
set RF=https://rf.ai-ready-workforce.ai.cornell.edu
for /f %i in ('aws secretsmanager get-secret-value --secret-id relational-fluency/agent-api-key --query SecretString --output text') do set KEY=%i
```

## Reading the rest of this page on Windows

Every block below is written for bash. `export` exists in neither Windows
shell, and three substitutions make the rest work in Windows PowerShell — they
are the only three you need:

1. **Write `curl.exe`, not `curl`.** In Windows PowerShell 5.1 — the version
   that ships with Windows 11, and what `powershell` launches — `curl` is an
   *alias* for `Invoke-WebRequest`, which has no `-s`. It fails with
   `Invoke-WebRequest : Cannot process command because of one or more missing
   mandatory parameters: Uri.`, which reads like a broken URL and sends you
   after the wrong bug entirely. `curl.exe` is the real curl and accepts every
   flag used on this page. (PowerShell 7 dropped the alias; `curl.exe` is
   correct in both, so use it always.)
2. **`$RF` and `$KEY` interpolate inside double quotes exactly as in bash**, so
   the bodies of the `curl` lines are otherwise unchanged.
3. **Line continuations**: bash's trailing `\` is a backtick `` ` `` in
   PowerShell and `^` in cmd. Simplest is to join the line into one.

In cmd.exe, additionally: variables are `%RF%` and `%KEY%`, and single-quoted
JSON bodies must be rewritten with escaped double quotes
(`-d "{\"name\":\"…\"}"`), because cmd does not treat `'` as quoting at all and
passes the apostrophes straight through to the server, which then rejects the
body as malformed JSON.

`python3` deliberately does not appear in these blocks. Inside an activated
virtualenv, `python` is the project interpreter on all three platforms; on
Windows there is no `python3.exe` from a python.org install, and the Windows
App Execution Alias of that name either opens the Microsoft Store or resolves
to the system interpreter rather than your virtualenv — so a `python3 -m
server.<module>` generalised from one of these lines fails with a confusing
`ModuleNotFoundError`.

---

## The consent version: unset, this records nothing

**Check this before the first participant of every wave.** One environment
variable on the task, `UPSTREAM_CONSENT_VERSION`, decides whether the wave
collects anything at all.

Consent is taken in Qualtrics now, before anybody reaches `/start`, so this
platform never sees the text a participant agreed to and cannot work out which
version it was. `server/storage.py` therefore refuses to record any study
consent until this variable names the approved wording. Unset — or set to a
placeholder like `[FILL IN: ...]`, which is treated as unset — here is the whole
of what happens, measured rather than described:

- `POST /api/consent` answers **503**, naming `UPSTREAM_CONSENT_VERSION` in the
  body and `consent_version_unset` as the reason, and the participant page
  shows the blocking card. (On the build deployed today, which predates this
  branch, the same refusal answers **404 `no such participant record`** — the
  record exists, the message is wrong, and it sends you looking for a missing
  participant; the participant is fine and the configuration is not. A link
  with no `qid` now answers **409** `no_survey_response_id` rather than the
  same 404.)
- The record stays `consent_given: false`, so the voice socket closes **4403**
  the instant they try to speak.
- `/health` answers **200**. `/start` keeps working. Runs keep being created,
  one per arrival, each with an empty `encounters` list.
- **Zero encounters are recorded, uniformly, from the first arrival onward.**
  There is no partial failure and nobody gets through. The first evidence is an
  empty dataset.

**`/test` does not catch it.** An internal run is recorded under
`internal_test` provenance, which deliberately needs no Qualtrics response and
no upstream version, so a lab member can walk the whole study, watch four
encounters record perfectly, and learn nothing about whether a real participant
can consent. Only a `/start` arrival exercises this path.

Check the deployed task carries it:

```bash
TD=$(aws ecs describe-services --cluster relational-fluency --services platform \
      --query "services[0].taskDefinition" --output text)
aws ecs describe-task-definition --task-definition "$TD" \
  --query "taskDefinition.containerDefinitions[0].environment[?name=='UPSTREAM_CONSENT_VERSION']"
# [] or a blank value → the wave will record nothing. Fix before recruiting.
```

```powershell
$TD = aws ecs describe-services --cluster relational-fluency --services platform --query "services[0].taskDefinition" --output text
aws ecs describe-task-definition --task-definition "$TD" --query "taskDefinition.containerDefinitions[0].environment[?name=='UPSTREAM_CONSENT_VERSION']"
```

**On the deployed service today this variable is not set at all**, which is why
this section is first on the page. Revision 38's environment carries
`API_HOST`, `APP_HOST`, `AWS_REGION`, `DIRECTOR_MODEL`, `HOST`, `LLM_BASE_URL`,
`PORT`, `REALTIME_MODEL` and `S3_BUCKET`, and nothing else —
`UPSTREAM_CONSENT_VERSION`, `CLAUDE_MODEL` and `SURVEY_RETURN_URL` are all
missing. (`ANTHROPIC_API_KEY` and `SESSION_KEY` are correctly injected from
Secrets Manager and must stay that way.)

Setting it means adding it to `containerDefinitions[0].environment` and
registering a new task-definition revision — the CLI release path, written out
in
[`DEPLOY-AWS.md`](DEPLOY-AWS.md#4a-the-path-in-use-register-a-task-definition-point-the-service-at-it).
Use your survey's own consent version, not `config/consent.yaml`'s.

`infra/terraform/terraform.tfvars` is where it belongs *once the Terraform path
works again* — as `upstream_consent_version = "cornell-irb-2026-09-v3"`, where
the variable has **no default**, so an apply that has not been told stops and
says so rather than deploying a task that records nothing. That safety net does
not exist on the CLI path: a hand-registered revision omitting this variable
registers happily and deploys green. Until then the check below is the net.
It is not a secret; commit it, the way `container_image` is committed, so the
running wave's consent version is visible in git history. `.env.example`
documents the same variable for a laptop or the Fly path.

Then confirm on the live wave, after the first arrival: every run should have a
`session_id` under `encounters`. Runs accumulating with nothing under them is
this failure and no other.

```bash
curl -s "$RF/api/runs?key=$KEY" | python -c "
import json,sys
rows=json.load(sys.stdin)
empty=[r['run_id'] for r in rows if not r.get('encounters')]
print('runs:', len(rows), 'with no encounter yet:', len(empty))
print(empty[:5])"
```

A few empty runs are ordinary — somebody opened the link and has not started
talking yet. *Every* run empty, once people have had time to speak, is the
consent version.

---

## Before fielding: the blanks in `config/consent.yaml`

The consent *form* is in Qualtrics now, but `config/consent.yaml` did not stop
mattering: the participant page still reads its `contact:` block, and that block
is what every card naming a human being is built from — the withdrawal card, the
decline card, the closing card, and the card a participant sees when their
consent record cannot be confirmed. The file ships as a template with eleven
`[FILL IN: ...]` markers in it, and **three of them are the ones a participant
actually runs into**:

| Field | What it must hold | What breaks while it is a `[FILL IN: ...]` |
|---|---|---|
| `contact.pi_name` | The PI's name, as the IRB protocol has it | No name on any card |
| `contact.email` | The study contact address (validated as an address) | **No way to ask for deletion.** The withdrawal card offers the right; the address is missing |
| `contact.irb_protocol` | The IRB protocol number | Nothing for a participant to quote to the IRB office |

With all three unfilled, a participant who stops mid-study is not shown a blank
and is not shown `[FILL IN: ...]` either — the page refuses to print a
placeholder as somebody's contact details, and degrades to *"contact whoever
sent you this study link, the consent form you were shown carries no contact
details for the research team, which is a fault on our side."* That sentence is
honest and it is still a failure: the deletion right the consent form promises
has no address on it, and the participant has to go back through recruitment to
exercise it. Fill all three before the first arrival.

The rest of the template is checked by `server/consent_check.py`, which reports
one combined reason at boot. All of it has to be answered before fielding:

- `version` — must not be the shipped `v0.1-2026-06` and must not read as a
  draft. It is recorded per participant as `consent_text_version`, so two waves
  under the same unedited string cannot be told apart afterwards. (This is the
  *local* version. It is **not** what
  [`UPSTREAM_CONSENT_VERSION`](#the-consent-version-unset-this-records-nothing)
  should be set to: that names the survey's wording, which is the text a
  participant actually read.)
- `irb_status.reviewed: true` — the human act. Nothing in the system can tell
  approved wording from a plausible draft, so a person says so here.
- The eight `[FILL IN: ...]` markers in `body` — the lab and institution, the
  time commitment, what the provider may do with audio, whether recordings are
  shown outside the team, the retention period, compensation, eligibility, and
  what stopping means for payment. `body` must also say that live microphone
  audio is transmitted to the model provider; the guard checks for it.

**Boot says so, and boot does not stop.** A task whose consent config is still
the template prints

```
  WARNING: config/consent.yaml is not fit to field: … Do not recruit participants until this is fixed.
```

and then serves normally — deliberately, because refusing to start would take
out every laptop and every CI run, and no process can tell a recruiting
deployment from a rehearsal. So the warning is one line in the log an operator
reads after a deploy, and this page is the other place it is written down. Check
it deliberately:

```bash
aws logs tail /ecs/relational-fluency/agent --since 10m | grep -i "not fit to field"
# no output = the consent config passed its checks at the last boot
```

---

## Read this before collecting anything

**Pull any encounter you care about, and do it before the next deploy** (see
[Getting data off the server](#getting-data-off-the-server)). That is the
standing rule. Everything below is why it is still the rule.

**Today there is no persistent volume, and that is measured, not suspected.**
Checked against the account on 12 September 2026: revisions 35, 36, 37 and 38
of `relational-fluency-agent` all carry `volumes=[]` and no `mountPoints`, and
**no EFS file system exists in the account** for any of them to mount. The
image's own `ENV DATA_DIR=/data` means the app writes to `/data` regardless, so
the path in every other command on this page is right — but with no volume
behind it, `/data` is the container's writable layer and it goes with the task.

The EFS file system, its access point and the `/data` mount *are* written in
`infra/terraform/ecs.tf` (`aws_efs_file_system.study`,
`aws_efs_access_point.study`, the `mountPoints` entry on the platform
container). Terraform source is not a running service: it protects nothing
until someone applies it, and that stack's state is not in the account's state
bucket, so applying it is itself blocked — see
[`DEPLOY-AWS.md`](DEPLOY-AWS.md#read-this-first-the-runbook-and-the-practice-have-diverged).
Do not read the presence of those resources in the repository as protection.

Ask the running service rather than trusting either this page or that one — it
is one command and it is the only answer that counts:

```bash
TD=$(aws ecs describe-services --cluster relational-fluency --services platform \
      --query "services[0].taskDefinition" --output text)
aws ecs describe-task-definition --task-definition "$TD" \
  --query "taskDefinition.[volumes,containerDefinitions[0].mountPoints]"
# [[],[]]                        → what it returns today: volumes=[] and no
#                                  mount points. Ephemeral task. Every deploy,
#                                  crash, or task retirement destroys whatever
#                                  was recorded since your last pull.
# an EFS volume + a /data mount  → records survive a deploy, a crash, and task
#                                  retirement. Nothing else changes.
```

```powershell
$TD = aws ecs describe-services --cluster relational-fluency --services platform --query "services[0].taskDefinition" --output text
aws ecs describe-task-definition --task-definition "$TD" --query "taskDefinition.[volumes,containerDefinitions[0].mountPoints]"
```

The two outputs differ by a bracket, which is exactly why it is worth knowing
what you are looking at: `[[],[]]` is two empty lists — no volume declared on
the task, no mount point on the container — and anything else is a volume and a
mount, printed in full.

If one day that command shows the volume and the mount, pulling data stops being
a race against the next rollout and becomes redundancy. It does not become
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
  The bucket half of that request is harder than it looks: the study bucket is
  versioned, so an ordinary delete leaves the bytes behind as a noncurrent
  version. See [Deleting one participant's
  recording](#deleting-one-participants-recording).

```bash
aws s3 ls s3://relational-fluency-study-data/ --recursive | head   # video only; no session records
```

### Which of the two places is this wave's video in?

Now that AWS credentials exist, a recording can be in either of two places and
the encounter looks identical from the outside. One event field says which.
`via: "local"` on the `video_uploaded` event means the browser could not get a
presigned URL and PUT the bytes to the app instead, so they are on the task's
own disk under `sessions/<id>/webcam.webm` — which is EFS if the volume check
above passed, and ephemeral if it did not. No `via` at all means the bytes went
browser-direct to the bucket.

`$KEY` is the researcher key set at the top of this page. `/health` withholds
the `credentials`, `bucket`, `region` and `detail` fields from an
unauthenticated caller, so a keyless read of the storage block will not tell you
which of the failures below you are looking at.

```bash
curl -sS "https://rf.ai-ready-workforce.ai.cornell.edu/health?key=$KEY" \
  | python -c "import json,sys; print(json.load(sys.stdin)['storage'])"
aws s3 ls s3://relational-fluency-study-data/encounters/ --recursive --summarize | tail -3
```

```powershell
curl.exe -sS "https://rf.ai-ready-workforce.ai.cornell.edu/health?key=$KEY" | python -c "import json,sys; print(json.load(sys.stdin)['storage'])"
aws s3 ls s3://relational-fluency-study-data/encounters/ --recursive --summarize | Select-Object -Last 3
```

A wave whose `storage.ok` is true and whose object count is far below the
encounter count has been falling back silently: check the events for `via`, and
pull those recordings off the task before the next deploy. `storage.ok` false
means **every** recording in that wave is on the task's disk, and the countdown
is the next rollout.

Neither state loses a recording by itself. The difference is entirely about
what survives a deploy.

### The third state, which loses the recording: CORS

There is a failure that produces neither of the two states above, and it is the
one worth memorising because nothing anywhere names it.

The browser PUTs the recording straight to S3, so the bucket's **CORS
allowlist** decides whether that PUT is allowed to leave the page at all. The
allowlist has exactly three origins
(`infra/terraform/storage_secrets.tf`):

```
https://rf.ai-ready-workforce.ai.cornell.edu
http://localhost:8765
http://127.0.0.1:8765
```

`8765` is the app's default `PORT`, spelled twice because a browser treats
`localhost` and `127.0.0.1` as different origins. **Local webcam testing works
on port 8765 and on no other port.** Serve the app on 8000 with AWS credentials
present and the sequence is: presigning succeeds, the browser refuses the PUT at
its preflight, and no recording is made.

What makes it expensive is how it is reported. A CORS refusal is not an HTTP
status the page can see — the fetch simply throws — so `static/v2.html` records
the reason as `network`. And `network` is deliberately **not** one of the
reasons that trigger the local upload fallback (`presigningIsUnavailable`
admits only `presign_http_5xx` and `presign_no_url`, because a `network` reason
usually means this very server was unreachable and re-sending 45 MB to the same
host would not end differently). So the bytes go nowhere, the fallback is not
attempted, and no log line on either side says "CORS".

Two consequences for an operator:

- **Running locally: use port 8765**, or add your origin to the allowlist before
  you test. If every local recording is "failing to upload", check the port
  before anything else.
- **Changing the participant-facing hostname is a bucket change too.** A new
  origin that is not in the allowlist loses every recording in the wave, with
  the encounters otherwise looking perfect. `via` will not tell you — there is
  no `video_uploaded` event to carry it.

### Deleting one participant's recording

The bucket is **versioned**, which is right for research data and wrong for the
sentence in the consent form that offers a participant deletion. An ordinary
`aws s3 rm` writes a delete marker: the object stops being listed and the bytes
stay, as a noncurrent version, indefinitely. Honouring a withdrawal takes a
version-aware delete of the encounter's key, plus the local copy if that
encounter fell back, plus the session directory on `/data` — which holds the
audio and the transcript and is not in S3 at all. There is no script for this
yet; do it by hand, confirm each of the three, and write down what was removed.

---

## Which scenarios a participant gets

Phase 1 uses variant A only: every study run is S1A, S2A, S3A, S4A in a
counterbalanced order (shuffled per participant). This is the code default
(`DEFAULT_RUN_VARIANT=A`); set it to `B` to pin the other form or `random`
for a per-construct coin flip. An explicit `variant=` on an internal test
link still overrides it, and the RCT's second attempt always flips forms.

Transcription is hinted to English on every route (`TRANSCRIPTION_LANG=en`;
blank to disable) and the actors are told to speak English regardless of
what they think they heard.

> **What A-only does to the S1/Teamwork rule.** `FORM_EXCLUSIONS` in
> `server/runs.py` bars **S1A from any run that also contains Teamwork** — the
> two overlap on grounded content (1,631 shared groundings against 77 for the
> alternative), which is a discriminant-validity problem, and the rule exists
> to keep them apart. That rule has ONE documented escape hatch: a form the
> **caller pinned** is honoured as asked and the run is stamped `"form was
> pinned by the caller; exclusion not applied"`. `DEFAULT_RUN_VARIANT=A` pins
> S1A on **every run**, so the escape hatch is taken on every run and the
> exclusion is, in practice, **off for the whole of Phase 1**. Nothing errors
> and nothing looks wrong: the runs record the exclusion as deliberately not
> applied, which is exactly what the stamp is for. An operator reading this
> page must not have to discover it by counting pairings in the data.
>
> This is a study-design question, not an ops setting, and it is **for the PI**:
> either Phase 1 accepts the S1A + Teamwork pairing and says so in the analysis
> plan, or `DEFAULT_RUN_VARIANT=random` restores the per-construct draw (and
> with it the three-forms-per-construct bank and the held-back reserve form)
> and the exclusion starts applying again. Both mechanisms exist in the merged
> code; the default is A.

## The base URL also forwards participants (second route in)

**This is not the link this page tells you to paste.** The links to paste are
in [The participant URL](#the-participant-url-qualtrics--app--qualtrics)
below. This section documents a second way in that also works.

The base URL forwards a visitor to the study entry when an id is in the query,
so this is a whole, working link on its own:

```
https://rf.ai-ready-workforce.ai.cornell.edu/?participantId=${e://Field/participantId}&qid=${e://Field/ResponseID}
```

`participantId` is the Survey Flow embedded field set from the CloudResearch
Connect URL; `ResponseID` is Qualtrics' own id for that response. The app
stores both on the run, so `python -m server.qualtrics join` can match each
survey response to its four encounters. A participant who reopens the link
lands back in the same run at the encounter they were on. The bare base URL
with **no** parameters still goes to the researcher landing page and is still
key-checked.

Two things to know before choosing it:

- **It selects `/start`, the study run** — four constructs, one encounter
  each — the same run the pasted link below starts.
- **`&qid=` is as mandatory here as it is there.** The forward carries the
  query through unchanged, so a base-URL link missing `${e://Field/ResponseID}`
  fails in exactly the way the warning below describes: no consent record, and
  the voice socket closes 4403.

The forward runs *before* the researcher-key check, which is the point of it —
a participant arriving on the base URL used to hit that check. It does not
weaken the door: the forwarded request goes through `entry_params` and the
link-probe filter like any other, so a scanner or a link unfurler fetching the
base URL is shown the entry check page and **mints no run**.

## Adding a second deployer

Never share `jinsook-cli` (it is AdministratorAccess). Give the person their
own IAM user with deploy-scoped rights, and move Terraform state to the shared
bucket so two laptops cannot hold diverging copies of what is deployed:

```bash
infra/scripts/add-deployer.sh <username>      # e.g. ben-cli; idempotent
```

The script prints the two follow-ups: `tofu init -migrate-state` (once, by
whoever holds the current local state) and `aws iam create-access-key` (run
it yourself; the secret shows once; hand it over on a secure channel, never
email or chat). The new user has PowerUserAccess plus IAM read access, the
right to pass the two task roles to ECS, and the listed policy, trust-policy,
and tagging permissions on `relational-fluency-*` roles. Creating or deleting
IAM roles still needs an admin. They also need: access to the GitHub org repo,
Docker, and OpenTofu.

## Before every deploy: is anyone mid-encounter?

A rollout starts a new task and retires the old one about two minutes later,
which cuts any conversation running on the old task (Fargate caps the stop
timeout at 120 s, so no drain setting can save it). Twice now a team member's
test conversation "stopped after a few turns" because it began during a
rollout. Two rules:

1. Do not apply while anyone is in an encounter. Check first, and wait until
   it reports zero:

```bash
curl -s https://rf.ai-ready-workforce.ai.cornell.edu/health | python -c "import json,sys; print('active sessions:', json.load(sys.stdin).get('active_sessions'))"
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
curl -s $RF/health | python -m json.tool
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
curl -s "$RF/api/scenarios?key=$KEY" | python -c "
import json,sys; d=json.load(sys.stdin)
print('study scenarios:', [x['id'] for x in d if x.get('study')])"
```

Expect all twelve: `S1A S1B S1C S2A S2B S2C S3A S3B S3C S4A S4B S4C` — three
parallel forms per construct. Fewer means the deployed image predates a form,
or a spec stopped loading (a spec missing a required key is dropped silently
by the loader; CI's `EXPECTED_V3` is what notices).

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

`verify_record` cannot tell you whether the *manipulation* happened. That is
`tools/encounter_health.py`, which reads the event trail rather than any
status field and fails an encounter whose director never answered, whose
stage directions were never acknowledged (`steer_unacked` — every mid-encounter
direction on the configured model, by design), or whose group room lost its
participant-transcription channel:

```bash
python tools/encounter_health.py data/sessions/<session_id>
python tools/encounter_health.py --all data/sessions     # exit 0 only if every encounter passes
```

### What a wave sounds like: lost audio, and the retry

On `nto.gemini-live-2.5-flash` through the gateway a reply's voice can stop
short of its own caption (about 1 reply in 9) or never start at all (before
this build, 45 s of dead air per stall, median 47.4 s, with the participant's
own turns refused meanwhile). The bridge now notices both and asks the gateway
**once** more for the turn (`AUDIO_ABSENT_S` = 8 s in `server/voice/realtime.py`).
Measured on the final wave: 4 stalls in 145 replies, all one-to-one, each now
7.4–8.5 s of silence instead of 47; 0 retries over a talking participant;
0 fires on `gpt-realtime-2.1`.

A turn the gateway never answers at all is a **different** fault and is no
longer left to that 45 s watchdog. Since 2026-09-14 a request with no frame
behind it is called unanswered at `REQUEST_UNANSWERED_S` = 6 s and re-asked
with the participant's own audio; unanswered again at `REPLAY_UNANSWERED_S`
= 4 s, the session is rebuilt and the line replayed into it (`RECONNECT_LIMIT`
= 2 rebuilds per encounter). From the participant's chair that is 9–22 s of
quiet — the seven live recoveries of that day measured 9.2, 9.3, 11.8, 14.9,
15.3, 19.2, 21.7 s, median 14.9 — and then the character answers what they
actually said, rather than an encounter in which every later line was lost.
Expect roughly one per 90–120 s of talking on this gateway. `RESPONSE_STALL_S`
(45 s) now only backstops replies the runner did not request.

Four things an operator needs to know about it:

- **The retry is a text prompt to the model** — `(I didn't hear that - the
  audio dropped. Could you say it again?)` — because for a reply whose *audio*
  was lost it is the only thing measured to revive it. It never enters the
  participant transcript
  and is written on the `audio_retry` event, but the character's recovered
  line answers it, and a rater will hear that: on the four live stalls the
  re-spoken line was delivered whole every time and repeated the lost words
  0 of 4 times. **The PI must rule on this before a wave** (it is in the
  decision memo); until then, tell raters what `audio_retry` on a turn means.
- **The nudge is not used where there is audio to replay.** A nudge in front
  of a *lost line* was measured to draw an answer to the nudge rather than to
  the participant — a dropped *"Good morning."* came back as *"You booked this
  meeting. What's on your mind."* — so every 1:1 request is re-asked with the
  participant's own audio instead, which needs no explaining to a rater
  (`participant_turn_replayed` on the record). The nudge remains only for a
  truncated reply, a group room, and a request with no speech behind it.
- **`encounter_health` reports it per encounter** — `audio lost upstream: N
  retried, N recovered, N delivered whole, N unrecovered` — so a wave can be
  checked for how much of it the participant actually heard, and a gateway
  that is failing more often than the pilot measured shows up here rather
  than in a rater's puzzlement.
- **Participants must wear a headset — a requirement, not advice.** With
  loudspeakers the character's own words land inside the participant's
  transcript and the transcriber hallucinates on the bleed, which corrupts the
  channel the study measures; the bleed also counts as "the participant is
  talking" and withholds a retry. The end-of-turn detector adapts to the room's
  noise floor (a fan or keyboard clatter no longer cuts a character off — false
  cut-offs 5 in 22 agent turns before, 0 in 19 after; genuine interjections
  still cancel the speaker, 6 of 12 after against 2 of 9 before), but a
  loudspeaker is not room noise.

```bash
# What is on disk locally
ls -t data/sessions | head
python -c "
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
curl -s "$RF/api/encounters?key=$KEY" | python -c "
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
curl -s "$RF/api/encounters?key=$KEY" | python -c "
import json,sys
print('\n'.join(e['id'] for e in json.load(sys.stdin)))" | while read SID; do
  curl -s -o "$SID.zip" "$RF/api/sessions/$SID/download.zip?key=$KEY"
  echo "pulled $SID"
done
```

## How many sessions / participants (durable, survives redeploys)

CloudWatch keeps every voice session start. From the terminal (a few minutes
for a 7-day window):

> `python`, not `python3`: an activated venv provides `python` on all three
> platforms, and `python3` does not exist on a Windows checkout — which is
> where half of this project's operators are. `python3` is reserved on this
> page for creating the virtualenv on macOS/Linux, paired with `py -3.12` for
> Windows, and tests/test_deploy_portability.py enforces that. (The line below
> is otherwise exactly as it arrived on origin/main 23d98a3.)

```bash
DAYS=7; START=$(( ($(date +%s) - DAYS*86400) * 1000 )); aws logs filter-log-events --log-group-name /ecs/relational-fluency/agent --start-time $START --filter-pattern '"/ws/participant/voice" "[accepted]"' --query 'events[*].[timestamp,message]' --output json | python -c "
import json,sys,re,datetime,collections
ev=json.load(sys.stdin); rows=[]
for ts,msg in ev:
    m=re.search(r'scenario=(\w+)&participant_id=([\w\-]+)', msg)
    if m: rows.append((datetime.datetime.fromtimestamp(ts/1000).strftime('%a %b %d'), m.group(1), m.group(2)))
print('voice sessions:', len(rows), '| distinct participants:', len({p for _,_,p in rows}))
print('per day:', dict(collections.Counter(d for d,_,_ in rows)))
print('per scenario:', dict(collections.Counter(s for _,s,_ in rows)))"
```

Or in the console, CloudWatch Logs Insights on `/ecs/relational-fluency/agent`:

```
fields @timestamp, @message
| filter @message like "/ws/participant/voice" and @message like "[accepted]"
| parse @message /scenario=(?<scenario>\w+)&participant_id=(?<pid>[\w-]+)/
| stats count() as sessions, count_distinct(pid) as participants by bin(1d)
```

Simulator runs (`server` verification) count like people here. Real study
participants are the `cohort=study` runs in `/api/runs` once the Qualtrics
link is live.

## Reading the steering trail

```bash
echo "$RF/director?key=$KEY"
```

Shows each encounter labelled by construct and variant, scene headings from the
research note, every stage direction above the reply it produced, and coverage
(triggers reached out of planned, ESCI items exercised).

## Deploying

**The runbook and the practice diverged, so read this before you copy
anything.** This page used to end with `tofu apply`. At the 12 September
inspection, revisions 35, 36, 37 and 38 of `relational-fluency-agent` had been
registered by hand with the AWS CLI, and the stack's Terraform state was not
in the account's state bucket. `infra/terraform/versions.tf` now configures a
shared S3 backend with locking; [Adding a second deployer](#adding-a-second-deployer)
describes the setup and state migration. That configuration does not prove the
existing state has been migrated: verify it before applying. Run from empty
state, `tofu apply` does not update the service: it proposes to *create* the
bucket, the ECR repositories, the IAM roles and the certificate that already
exist. Compare `container_image` in `infra/terraform/terraform.tfvars` with the
running task definition before applying; the repository pin has been **behind**
before, and using a stale pin rolls production back. A pin change in Git does
not itself deploy that image. The earlier inspection and the work needed to
reopen the Terraform path are recorded in
[`DEPLOY-AWS.md`](DEPLOY-AWS.md#read-this-first-the-runbook-and-the-practice-have-diverged).

Build and push is unchanged and is the same on either path:

```bash
set -euo pipefail
REGION=us-east-1
SHA=$(git rev-parse --short HEAD)
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com
REPO=$REGISTRY/relational-fluency/platform
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin "$REGISTRY"
docker build --platform linux/amd64 -t $REPO:$SHA .        # amd64 matters on Apple Silicon
docker push $REPO:$SHA
```

Windows PowerShell — same procedure, one statement at a time (PowerShell has no
`set -e`: `$ErrorActionPreference` does not cover a native executable's exit
code, so check the output of each line before running the next):

```powershell
$REGION = "us-east-1"
$SHA = git rev-parse --short HEAD
$ACCOUNT = aws sts get-caller-identity --query Account --output text
$REGISTRY = "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
$REPO = "$REGISTRY/relational-fluency/platform"
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REGISTRY
docker build --platform linux/amd64 -t "${REPO}:${SHA}" .
docker push "${REPO}:${SHA}"
```

The repository URL used to come from `tofu ... output -raw ecr_repository`,
which needs the state this stack does not have: with no state the output is
empty, `docker build -t :$SHA` builds an image tagged with a bare colon, and the
push fails naming the tag rather than the missing state. Build the registry host
from `$ACCOUNT` and `$REGION` instead — and not from `"${REPO%%/*}"`, a bash
expansion PowerShell parses as a braced *variable name* (`REPO%%/*`), finds
nothing for, and expands to the empty string with no error at all, so
`docker login --password-stdin ""` fails with a message about a missing registry
that sends the operator after AWS credentials which are fine.

**Then release.** The sequence that actually ships a build —
`describe-task-definition` into a file, strip the read-only keys, change the
image, `register-task-definition`, `update-service` — is written out step by
step in
[`DEPLOY-AWS.md`](DEPLOY-AWS.md#4a-the-path-in-use-register-a-task-definition-point-the-service-at-it),
along with which IAM permission each step needs. It is not duplicated here,
because two copies of a release procedure diverge and this is a page about
checking things rather than about changing them.

*No `sed -i ''`, on either path.* That is the macOS/BSD spelling. On GNU sed —
every Linux box, and Git Bash on Windows — `-i` takes its suffix attached, so
`''` is read as the *script*, `s|platform:...|` is read as a *filename*, and the
command exits 2 with `sed: can't read s|platform:...`, leaving
`terraform.tfvars` untouched. The old block had no `set -e`, so the next line
ran anyway and re-applied the tag that was already pinned: the operator builds a
new image, pushes it, watches a deploy succeed — and participants keep hitting
the previous build. Because tags are immutable and deploys are manual and
scheduled between collection sessions, that is discovered, if at all, during the
next wave. If the committed pin in `infra/terraform/terraform.tfvars` has to
move — it is committed on purpose, per that file's own header — edit it by hand
and commit it as a separate, visible step; if a scripted edit is genuinely
wanted, use `python -c`, which is a prerequisite on all three platforms.

Rollout waits for the new task to pass health checks before draining the old
one, so an encounter in progress is not cut off at the switch — but the old task
is stopped 120 s later regardless, and **anything recorded on it is gone**,
because nothing is mounted at `/data`. Pull first.

## When something is wrong

| Symptom | First check |
|---|---|
| **"We could not confirm your consent record", or a 503 from `POST /api/consent`, or every socket closing 4403** | **`UPSTREAM_CONSENT_VERSION` on the task.** Unset or a placeholder and no study consent can be recorded at all — see [The consent version](#the-consent-version-unset-this-records-nothing). The 503 body names the variable. On the build deployed today the same failure is a 404 saying "no such participant record"; the record exists. Do not go looking for it. |
| A 409 `no_survey_response_id` from `POST /api/consent` for one participant | Their entry link carried no usable `&qid=` — the survey's redirect is not piping `${e://Field/ResponseID}`. Fix the link; that participant re-enters — see [The participant URL](#the-participant-url-qualtrics--app--qualtrics) |
| Runs are accumulating but the encounter count stays at zero | Same variable. `/health` is 200, `/start` works, and nothing else fails |
| Page loads, mic "does not work" | `curl -s $RF/health` — if `gateway.ok` is false, no encounter can run |
| WebSocket opens then closes instantly | Application logs — a server-side exception during session creation looks exactly like a dead mic (and 4403 specifically is the consent gate, one row above) |
| 503 from the domain | Target health, then service events: usually no healthy task |
| `No scenario: SxX` | Deployed image predates the scenario bank — check the running image tag |
| Agent replies but no transcript | `verify_record` — look for `transcript_missing` |
| Encounter ends after ~3 turns | `INTERACTION_MIN_TURNS` / `INTERACTION_MIN_SECONDS` on the task |
| Every run is `cohort=unattributed` | The Qualtrics embedded field name. It is `participantId`; an unknown field pipes as the empty string and reports nothing — see [The participant URL](#the-participant-url-qualtrics--app--qualtrics) |
| No webcam recording, no `video_uploaded` event, "network" in the client | The bucket's CORS allowlist does not name the origin the page was served from, and a CORS refusal does not reach the local fallback — see [The third state](#the-third-state-which-loses-the-recording-cors) |
| Yesterday's encounters are gone | The task has no persistent volume and something redeployed or restarted it — see [Read this before collecting anything](#read-this-before-collecting-anything) |
| A character goes quiet for ~8 s and then starts the line again, or its reply answers "could you say it again" | The gateway dropped that reply's audio and the bridge retried it once — `audio_retry` on the turn. Expected on the configured model at a few per 145 replies; a wave doing much worse than that is the gateway, and `python tools/encounter_health.py` will say how often — see [What a wave sounds like](#what-a-wave-sounds-like-lost-audio-and-the-retry) |
| A character goes quiet for 45 s | A turn the gateway never answered at all; the watchdog closes it. Seen after one-word utterances. Nothing to fix on the task |
| Characters keep stopping mid-sentence | Room noise or loudspeaker bleed being read as the participant. Headset first; the end-of-turn floor adapts to steady noise, not to the character's own voice coming back through the mic |
| Qualtrics export fails while `whoami` succeeds | `QUALTRICS_BASE_URL` is the brand host, not the datacenter host — see [Pulling the survey responses out of Qualtrics](#pulling-the-survey-responses-out-of-qualtrics) |

---

## The URLs

There are four. The first one needs the researcher key and is the only one that
honours `variant`; the other three go to people you are not standing next to.
Read the warning under the first before you copy anything from this section into
Qualtrics.

> **On your own laptop there is a fifth, and it is the one you will actually
> type.** A local checkout has no `SESSION_KEY`, so the `/test` link's `&key=` is
> not available to you as a way of proving who you are — and a bare
> `?pid=whatever` participant link is a **permanent dead end** on *"We could not
> confirm your consent record"*, because it carries no `qid`. Appending
> **`&cohort=internal`** on the `/start` link is the way through: it satisfies
> the consent-provenance check with no `qid`, skips the seven-minute encounter
> gate and the 180-second advance floor (both are disabled for a run whose
> cohort is `internal`), and tags the run so every study export drops it. Both
> arms were walked end to end that way on a local checkout.
>
> The links themselves, written out, are in
> [`docs/TESTING-LOCALLY.md`](TESTING-LOCALLY.md) — deliberately there and not
> here, because a URL on *this* page is one somebody may paste into Qualtrics,
> and a `/start` link without `&qid=` must never be one of those.
>
> On a **deployed** server the same suffix on a keyless link is *ignored* and a
> `WARNING` naming it is logged, which is the backstop described below — so this
> is a local-testing technique, not a second participant link.

### 1. Internal testing and lab demos (bug hunting)

```
https://rf.ai-ready-workforce.ai.cornell.edu/test?name=jennie&variant=A&key=$KEY
```

- **`key=` is mandatory on any deployment with `SESSION_KEY` set**, which is
  every deployed one. This door is `check_key`-gated — GET `/test`, POST
  `/api/consent` and the voice socket reached live audio and webcam capture in
  three requests from anywhere on the internet before it was — so without the
  key it answers **401** and no run is created. It is still open on a local
  checkout with no `SESSION_KEY`, the way `/researcher` and the download routes
  are. It is the researcher's own credential: it belongs in a link you paste
  into your own browser, never in one that goes to Qualtrics.
- `name` labels the run so a bug report can say whose session it was.
- `variant=A` or `variant=B` pins all four scenarios to one form; omit for the
  randomized mix. A letter no form carries is refused with a 400, here as on
  the participant links.
- `qid=` is neither needed nor read here: an internal run is recorded under
  `internal_test` provenance, which needs no Qualtrics response and no
  `UPSTREAM_CONSENT_VERSION`. That is also why walking the study through this
  door proves nothing about whether a real participant can consent — see
  [The consent version](#the-consent-version-unset-this-records-nothing).
- These runs are tagged `cohort=internal` and are excluded from study data by
  that tag; they can never be mistaken for a participant.

> **`variant=` and `cohort=` belong to this link and to no other — and since an
> earlier round, a stray one on a participant link is no longer a disaster.**
> Without the researcher key the server **ignores** both and prints a `WARNING`
> naming the parameter it threw away: the participant gets the run the study
> intends. Ignored rather than refused on purpose, because a 400 at the door
> mid-study costs the encounter outright, and the safe reading of a stray
> parameter on a recruited person's link is the run they should have had
> anyway. So a copy-paste slip costs you a line in CloudWatch, not the wave.
>
> The residual risk is narrow and worth naming exactly, because it is the one
> the backstop cannot cover: **copied together with `&key=`**, both are honoured
> — you are holding the credential, so the server does what you asked. Then
> `variant=A` pins all four encounters to one parallel form and the
> counterbalancing is gone with every record still looking perfect, and
> `cohort=internal` tags real participants as lab traffic that every analysis
> filter drops. That is the reason this link, key and all, must never be the one
> you paste into Qualtrics. Copy the participant links from the next section.

### 2. The participant-facing links

These go into Qualtrics; the canonical wording, and what each parameter
must and must not carry, is [The participant
URL](#the-participant-url-qualtrics--app--qualtrics) below.

| Link | Who clicks it | Mandatory parameters |
|---|---|---|
| `/start` | A participant arriving from the survey | `pid=`, **`qid=`** |

> **`&qid=` is mandatory on the `/start` link.** It carries
> the Qualtrics `ResponseID`, which is the only evidence this platform has that
> anybody consented at all; without a usable one the arrival is refused, the
> voice socket closes 4403 and **the encounter is not recorded**. The full
> wording is in [The participant
> URL](#the-participant-url-qualtrics--app--qualtrics) below — read it before
> you paste anything into the survey.


Full linkage: the CloudResearch key ties recruitment to the survey, the
Qualtrics response id ties the survey response to the app run, and the
completion code carried back ties the run to the follow-up survey.

### Joining the data afterwards

```bash
curl -s "$RF/api/runs?key=$KEY" | python -m json.tool
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

**Check this after the first arrivals of every wave, before the wave fills up.**
This is the one command on this page that has to work on whatever machine the
person watching the wave happens to be sitting at, so all three forms are
written out in full — do not translate it under time pressure.

bash / zsh (macOS, Linux, Git Bash):

```bash
# Should be empty. Anything here is a real participant you cannot attribute.
curl -s "$RF/api/runs?key=$KEY&cohort=unattributed" | python -c "
import json,sys
rows=json.load(sys.stdin)
print('unattributed runs:', len(rows))
for r in rows:
    print(' ', r['run_id'], r.get('participant_key_status'), r.get('created_at'))"
```

Windows PowerShell — no `curl` at all here; `Invoke-RestMethod` parses the JSON
for you, so there is nothing to pipe into Python:

```powershell
# Should be empty. Anything here is a real participant you cannot attribute.
$rows = Invoke-RestMethod -Uri "$RF/api/runs?key=$KEY&cohort=unattributed"
"unattributed runs: $($rows.Count)"
$rows | Select-Object run_id, participant_key_status, created_at | Format-Table
```

Windows Command Prompt — `curl.exe` is present on Windows 10 1803 and later,
and the URL must be quoted because `&` is a command separator in cmd:

```bat
curl.exe -s "%RF%/api/runs?key=%KEY%&cohort=unattributed" > runs.json
python -c "import json;rows=json.load(open('runs.json'));print('unattributed runs:',len(rows));[print(' ',r['run_id'],r.get('participant_key_status'),r.get('created_at')) for r in rows]"
```

A non-empty result means fix the Qualtrics `participantId` piping now (see
[The participant URL](#the-participant-url-qualtrics--app--qualtrics)) — and
the likeliest cause is the field name itself, because an embedded field
Qualtrics does not recognise pipes as the empty string rather than as an error.
The runs
already recorded can only be re-joined by hand, and `/api/runs` does not carry
the raw value — read it off the run document on the volume, where `/start`
stored it:

```bash
curl -s "$RF/api/runs?key=$KEY&cohort=unattributed" \
  | python -c "import json,sys; print('\n'.join(r['run_id'] for r in json.load(sys.stdin)))"
# then, per run id, on the volume ($DATA_DIR/runs — /data/runs on the server):
python -c "import json;d=json.load(open('/data/runs/<run_id>.json'));print(d['participant_key_status'], repr(d['raw_participant_key']))"
```

That value is only useful if the broken pipe happened to send something
identifying; often it is an empty string, and then the recruitment record and
the recording cannot be joined at all. The server also prints a `WARNING` line
per bad arrival, but a stdout line is not a check; this query is.

## Pulling the survey responses out of Qualtrics

The analysis-side half of the join: `server/qualtrics.py` exports the survey's
responses over the API and merges them with `/api/runs`, so "who replied and
how" is one table with the run id, completion code and encounter session ids
attached. It runs on your own machine, against `QUALTRICS_API_TOKEN`,
`QUALTRICS_SURVEY_ID` and `QUALTRICS_BASE_URL` in `.env`.

```bash
python -m server.qualtrics whoami     # credentials work
python -m server.qualtrics export     # raw responses to data/qualtrics/
python -m server.qualtrics join       # responses merged with runs
```

**The datacenter trap, and it only fires on the one call that matters.**
`QUALTRICS_BASE_URL` must be the **datacenter** host, `https://yul1.qualtrics.com`
— not the brand vanity host `cornell.qualtrics.com`. They are not
interchangeable, and the difference is invisible until the export:

- `cornell.qualtrics.com` answers `/whoami` and `/surveys` perfectly happily. So
  a setup check passes and the value looks right, possibly for months.
- The same host refuses `/export-responses` with *"This endpoint is unavailable
  through datacenter proxying, please retry using the url of the datacenter the
  API user belongs to: yul1.qualtrics.com"*.
- **`whoami` reports the datacenter as `viawest`.** That name is not routable
  and is not what belongs in `QUALTRICS_BASE_URL`. The only place the correct
  host appears is in the refusal message above — so read the error, not the
  field.

`_raise()` in `server/qualtrics.py` exists for exactly this: httpx's own
`raise_for_status()` renders the refusal as `Client error '400 Bad Request'`
plus a link to the MDN page for 400, and throws the body — the part naming the
host — away. An error that carries the answer and discards it is worse than no
error.

The token is a credential. It travels only in the `X-API-TOKEN` header, stays
out of git, and does not belong in a shell command you paste into a chat or a
ticket.

Two things to check on the joined output before trusting it:

- **The primary join is the Qualtrics `ResponseID`**, matched against each run's
  `qualtrics_id` — which is what `&qid=` on the entry link is for. If `qid`
  piping was working, everything joins on this and nothing else is needed.
- **The fallback join is by participant key**, for responses collected before
  `qid` piping existed. It looks for the key under a short list of field
  spellings in `server/qualtrics.py`; check that your survey's field name
  (`participantId`, per [The participant
  URL](#the-participant-url-qualtrics--app--qualtrics)) is in that list before
  relying on it, because a field it does not know about is not an error — the
  response simply comes back `UNLINKED`, and a table of unlinked responses looks
  the same whether the survey field is missing or merely unrecognised.

## The participant URL (Qualtrics → app → Qualtrics)

**Put this in Qualtrics.** It goes at the point where participants move from
the WEIP survey to the encounters. Copy it from here, whole.

```
https://rf.ai-ready-workforce.ai.cornell.edu/start?pid=${e://Field/participantId}&qid=${e://Field/ResponseID}
```

- **The field is `participantId`.** Checked against the live survey, which
  declares three embedded data fields — `participantId`, `assignmentId` and
  `projectId` — and pipes `participantId` and `ResponseID`. This page used to
  say `ParticipantKey`, which the survey does not declare, and that is the
  expensive kind of wrong: Qualtrics substitutes an unknown field with the
  **empty string** and reports nothing, so every link works, every participant
  is recorded, and every run lands in cohort `unattributed` with no recruitment
  record attached to it. `assignmentId` and `projectId` are CloudResearch's own
  identifiers; nothing in this platform reads them today, and they are named
  here so that a person comparing this page against the survey in front of them
  can tell "a field I am not using" from "a field that is missing".
- `${e://Field/participantId}` is Qualtrics piped text. If your survey spells
  the field differently, change the text **inside** the braces and leave `pid=`
  alone. The query-parameter spellings the app accepts are `pid`,
  `participant_id`, `participantId` and `PROLIFIC_PID` (`entry_params` in
  `server/app.py`).
  > **Changed 2026-09-15.** This bullet used to say `participantId` "is not
  > among them". It is now — it was added to `entry_params` in the same change
  > that made the base URL forward participants (see
  > [the section above](#the-base-url-also-forwards-participants-second-route-in)),
  > because that forward puts `participantId` in the query. `pid=` is still the
  > spelling to paste in the link: it is the one every other line on this page,
  > and every worked example, uses.
- `/start` is the full four-construct run: one encounter per construct, in a
  counterbalanced order, recorded on the run under `construct_pool`.

> **`&qid=` is mandatory on the `/start` link.** It carries the Qualtrics
> `ResponseID`, and since consent moved upstream that response id is the only
> evidence this platform has that anybody consented at all: `server/storage.py`
> records a study consent *only* against a usable `qid`. A link without it, or
> one whose `${e://Field/ResponseID}` never got replaced, is refused —
> `POST /api/consent` answers 409 `no_survey_response_id` (404 on the build
> deployed today), the record stays unconsented, and the voice socket closes
> 4403. **The participant is turned away and the encounter is not
> recorded.** `ResponseID` is built into Qualtrics; pipe it via embedded data.
>
> **Do not append `&variant=` or `&cohort=`.** Neither is silently honoured any
> more: on a link without the researcher key both are **ignored, and a
> `WARNING` naming the discarded parameter is printed to the application log**,
> so a stray one costs you a line in CloudWatch rather than the wave. That is a
> backstop, not a licence — it only holds for a participant link. Copied from
> the `/test` link *with* its `&key=` still attached, `variant=A` is honoured,
> and then it pins all four encounters to one parallel form: the
> counterbalancing is gone and every record still looks perfect. `cohort=` the
> same way, marking real participants as lab traffic that every analysis filter
> drops. Both are documented, above, on the `/test` link — they belong to that
> link alone, and it is the nearest URL on this page to these ones, which is
> exactly where a copy-paste typo comes from.
>
> **And do not append `&key=`** — see the warning below, which is about the
> researcher credential and is the most expensive mistake on this page.

After the first few arrivals, check the wave two ways: no `unattributed` runs
(the `participantId` piping worked, [above](#joining-the-data-afterwards)) and
no runs sitting with zero encounters (the `qid` piping and the consent version
worked, [above](#the-consent-version-unset-this-records-nothing)). Both fail
all-or-nothing, so the first three participants tell you about all hundred.

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

> **Set to the Qualtrics *host* rather than a continuation link, the button is
> worse than missing, and that is what a checkout has today.** `.env` currently
> carries `SURVEY_RETURN_URL=https://cornell.qualtrics.com`. Measured, with
> redirects followed: that URL ends at
> `https://shibidp.cit.cornell.edu/idp/profile/SAML2/Redirect/SSO?execution=e1s1`,
> HTTP 200, page title **"Cornell University Web Login"**. A CloudResearch
> participant has no Cornell NetID, so the last button of the study drops every
> completer on a staff SSO login page — and it takes their run id, completion
> code and participant key there with it, on the query string.
>
> Two consequences: paste the survey's own **end-of-survey / continue** URL here
> before any wave, and understand that whatever host you configure **receives
> the run id, the completion code and the participant key**, so it must be a URL
> it is acceptable to send those three things to.
