# Deploying to AWS (study environment)

Serves `https://rf.ai-ready-workforce.ai.cornell.edu`. The stack described here
is **already up**: the hostname resolves, the ALB is in front of a running
Fargate task, and the certificate is issued. Sections 1 and 2 are the
first-time build-out and are kept for the record and for a second environment;
if you are here to ship a change, the section you want is
[4. Release](#4-release).

For the demo-only Fly path see [`DEPLOY.md`](DEPLOY.md); participant data must
not be collected there.

---

## Read this first: the runbook and the practice have diverged

This page used to describe one deployment flow — `tofu apply` — and that flow
had not been run against this service at the **12 September 2026** account
inspection. The findings below describe that inspection, not a current AWS
verification:

- **Every live revision of the task definition was registered by hand with the
  AWS CLI.** The family is `relational-fluency-agent`; the service is `platform`
  in cluster `relational-fluency`; revisions 35 through 38 existed and 38 was
  serving, at image tag `cabc1dd`.
- **The Terraform state for this stack was not in the account's state bucket.**
  That bucket held `bootstrap/` and `staging/` and nothing for this stack, and
  `infra/terraform/versions.tf` then had its S3 `backend "s3"` block commented
  out — the state these 50-odd resources were created from needed to be found
  on somebody's laptop or recovered.
- **Consequence, and this is the whole reason for the warning:** `tofu apply`
  run against empty state does **not** update the running service. With empty
  state, Terraform believes nothing exists, so it plans to *create* the S3
  study-data bucket, the ECR repositories, the IAM roles and the ACM
  certificate that are already there. Bucket and role creation fail on
  `AlreadyExists` partway through, leaving a half-built state file that matches
  neither reality nor the previous state.
- **`infra/terraform/terraform.tfvars` was corrected to `cabc1dd` on
  12 September**, matching the running image then; it had read `3cf8496`,
  two releases behind. The hazard is structural rather than
  fixed: whenever that pin lags, an apply that takes the file at its word would
  **roll production back** to an older build and report success doing it. The
  file carries a `deployed:` line recording verification status, and
  `tests/test_terraform_persistence.py` checks that the line exists; the test
  does not verify the live deployment.
- **`infra/terraform/ecs.tf` declared resources absent from those live
  revisions** — the EFS file system, its access point, the `/data` mount and the
  `DATA_DIR` environment entry. Those are the fix, not the state of the world;
  see [No persistent volume](#no-persistent-volume-yet), below.

**Repository update, 15 September 2026:** `versions.tf` now configures a shared
S3 backend with a DynamoDB lock table, and `terraform.tfvars` pins image
`3d3cbfc` from `main`. Its `deployed:` note explicitly marks that pin unverified.
The merge did not check or change AWS. Use
[Adding a second deployer](OPERATIONS.md#adding-a-second-deployer) to provision
the backend and migrate existing state, then verify the state and running
image before applying.

This page documents **two** paths; their status at the inspection was:

| Path | Status | Use it for |
|---|---|---|
| **CLI: register a task definition, update the service** | In use. Every revision 35–38 | Shipping a build, changing an environment variable, today |
| **OpenTofu / Terraform** | Blocked until the state is found or rebuilt | Everything structural — EFS, IAM, ALB — and, eventually, all of it |

Neither is the long-term answer on its own. The CLI path cannot create a
persistent volume, an IAM policy or a bucket rule, and a task definition edited
by hand drifts from `ecs.tf` a little more with every release. Getting back to
Terraform is real work with a real decision in it, and it is written up in
[Open questions](#open-questions) rather than pretended away here.

## What gets created

VPC (2 AZ, single NAT) · ALB + ACM certificate covering `rf.*` and `api.rf.*` ·
ECS Fargate service running the platform · ECR · KMS-encrypted S3 study-data
bucket · Secrets Manager · CloudWatch logs.

## Prerequisites

A release on the path in use needs the **AWS CLI v2**, **Docker** and
**Python** — and not `tofu`. Install OpenTofu when you are building a new
environment (section 1) or when the Terraform state has been recovered and that
path reopens; a release today never invokes it.

We use **OpenTofu** (`tofu`), the MPL-licensed fork. HashiCorp Terraform left
Homebrew core when it moved to the BUSL licence; `tofu` is a drop-in
replacement and reads these configs unchanged. Swap in `terraform` for `tofu`
below if you prefer the HashiCorp build (`brew tap hashicorp/tap`).

**Install `tofu`** — one line per platform. Homebrew is macOS-first: there is
no Homebrew on Windows, and a Linux researcher will not have Linuxbrew, so a
single `brew install` line is where a non-Mac operator stalls on the first
command of the runbook.

| OS | Command |
|---|---|
| macOS | `brew install opentofu` |
| Windows | `winget install --id=OpenTofu.Tofu -e` (or `choco install opentofu`) |
| Linux | your distribution's package, or the OpenTofu standalone installer |

If Homebrew is blocked by an untrusted tap, or no package manager route is
available, grab the release from `github.com/opentofu/opentofu` and verify it
against its `SHA256SUMS`.

> **Check the winget package id before relying on it.** It has not been
> verified against a live registry from this machine. If `winget` reports no
> match, take the current command from the OpenTofu install page.

**Install the AWS CLI v2** from AWS's own installer for your OS (macOS `.pkg`,
Windows `.msi`, Linux `.zip`) rather than `brew install awscli` — AWS ships a
first-party installer for all three and it is the supported route.

Then, in any shell on any of the three:

```
tofu version                    # confirms the install
aws sts get-caller-identity     # must succeed
docker info                     # must be running
```

## 1. Provision the infrastructure

> **Do not run this against the existing study account.** This section builds a
> stack from nothing, and the study stack is not nothing — see [Read this
> first](#read-this-first-the-runbook-and-the-practice-have-diverged). It is
> correct for a *new* environment (a staging account, a second study) and it is
> the record of how the current one was meant to come up. To change the running
> service, go to [4. Release](#4-release).

The first apply creates everything except a running task — there is no image
yet, so the service starts with zero healthy targets. That is expected.

```bash
cd infra/terraform
tofu init
tofu apply
```

Verified 2026-08-19, **into an empty account**: `tofu plan` was clean — 50
resources to add, none destroyed. That is the number this page has always
quoted, and it is worth being exact about what it means: a plan that proposes
50 creations is a plan that believes none of them exist. Against the study
account today it means the state is missing, not that the work is small.
Certificate validation adds DNS records automatically and takes a few minutes.
Note the outputs: `ecr_repository`, `app_url`, `api_url`, `study_data_bucket`.

Until a first apply completes in a new environment, its hostname does not
resolve at all — the DNS records are ALB aliases created by this process, so
"server not found" is the expected state beforehand.

**The apply will stop and ask for two variables, and both are the check
working rather than a fault.** `upstream_consent_version` names the approved
consent wording the Qualtrics survey is showing, it is recorded on every
participant as `consent_text_version`, and `server/storage.py` refuses to
record any study consent without it — so a task deployed without it answers
`/health` 200, takes every arrival, and collects nothing at all. It is given no
default for exactly that reason: the alternative to being asked here is finding
out from an empty dataset. `study_data_retention_days` is the number of days
the bucket keeps a recording before the lifecycle rule in
`storage_secrets.tf` expires it; it is the period the approved consent text
promises, that text still reads `[FILL IN: retention period …]`, and a default
here would be this repository inventing an IRB figure. See
[Lifecycle and retention](#webcam-recordings-and-the-study-bucket) before
answering it.

Answer it once, in `terraform.tfvars`, beside the committed image pin. It is not
a secret, and having the running wave's consent version in git history is worth
having:

```hcl
# infra/terraform/terraform.tfvars
upstream_consent_version = "cornell-irb-2026-09-v3"
```

Use your **survey's** consent version, not `config/consent.yaml`'s `version` —
that one names the text this platform used to show, and stamping it on somebody
who never saw it names the wrong document as the one they agreed to. Blank and
placeholder values (`[FILL IN: …]`, `TBD`, `changeme`) are rejected at plan time
and, if one somehow reaches the task, treated as unset by the app.

> **On the running study service this variable is not set at all.** Revision 38
> declares `API_HOST`, `APP_HOST`, `AWS_REGION`, `DIRECTOR_MODEL`, `HOST`,
> `LLM_BASE_URL`, `PORT`, `REALTIME_MODEL` and `S3_BUCKET`, and nothing else;
> `ANTHROPIC_API_KEY` and `SESSION_KEY` come correctly from Secrets Manager and
> must stay that way. `UPSTREAM_CONSENT_VERSION`, `CLAUDE_MODEL` and
> `SURVEY_RETURN_URL` are absent, and the Terraform variable that would have
> stopped an apply without the first of them cannot stop a CLI registration.
> Adding them is part of [4. Release](#4-release), and the wave-time symptom of
> the first one is in
> [`OPERATIONS.md`](OPERATIONS.md#the-consent-version-unset-this-records-nothing).

## 2. Set the secrets

Values never touch git or Terraform state.

**Generate the session key as its own visible step, and paste the result.** Do
not generate it inline in the shell. `--secret-string "$(openssl rand -hex 32)"`
works in bash, zsh and PowerShell, but cmd.exe has no command substitution: it
stores the literal 24-character text `$(openssl rand -hex 32)` as the secret,
`aws secretsmanager` accepts it without complaint, and nothing ever errors —
because the system works. That value is `SESSION_KEY`, the only credential in
the study. It gates `/researcher`, `/director`, `GET /api/runs` (every
CloudResearch participant key, Qualtrics response id and completion code),
`GET /api/encounters`, and every `download/<file>` and `download.zip` — every
participant's microphone WAV, agent WAV, transcript and video pointer. A
"random" key that is a fixed string printed in this repository is not a
credential.

Step 1 — generate, on any of the three platforms (this is the same command
`DEPLOY.md` uses for the Fly path; `python` is already a hard prerequisite,
`openssl` is not on a default Windows PATH):

```
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Step 2 — paste it. `<paste>` is the output of step 1, nothing else:

```
aws secretsmanager put-secret-value --secret-id relational-fluency/agent-api-key --secret-string <paste>
```

Step 3 — the Cornell LiteLLM virtual key, which serves both the realtime actor
and the director:

```
aws secretsmanager put-secret-value --secret-id relational-fluency/anthropic-api-key --secret-string sk-...
```

Step 4 — read both back and look at them:

```
aws secretsmanager get-secret-value --secret-id relational-fluency/agent-api-key --query SecretString --output text
```

It must look like random base64. If it looks like a shell command, or begins
with `$(`, or is 24 characters long, the substitution did not run: re-do steps
1 and 2 and rotate anything that ran against the old value.

## 3. Build and push the platform image

bash / zsh (macOS, Linux, Git Bash):

```bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
REGION=us-east-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com
REPO=$REGISTRY/relational-fluency/platform
SHA=$(git rev-parse --short HEAD)

aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin "$REGISTRY"

docker build --platform linux/amd64 -t $REPO:$SHA .
docker push $REPO:$SHA
```

Windows PowerShell:

```powershell
cd (git rev-parse --show-toplevel)
$REGION = "us-east-1"
$ACCOUNT = aws sts get-caller-identity --query Account --output text
$REGISTRY = "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
$REPO = "$REGISTRY/relational-fluency/platform"
$SHA = git rev-parse --short HEAD

aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REGISTRY

docker build --platform linux/amd64 -t "${REPO}:${SHA}" .
docker push "${REPO}:${SHA}"
```

**`$REPO` is spelled out rather than read from `tofu output -raw
ecr_repository`.** That output needs the state this stack does not have: with
no state it returns nothing, `docker build -t :$SHA` tags the image with a bare
colon, and the push fails with a message about the tag rather than about the
missing state. The repository is `<project>/platform` —
`relational-fluency/platform` — per `infra/terraform/storage_secrets.tf`, and
its registry host is the account and region.

The registry host is built from `$ACCOUNT` and `$REGION` rather than
extracted with `"${REPO%%/*}"`. That bash parameter expansion is silently wrong
in PowerShell, which parses `${REPO%%/*}` as a braced *variable name* —
`REPO%%/*`, which does not exist — and expands it to the empty string with no
error. `docker login --password-stdin ""` then fails with a message about a
missing or malformed registry, and the operator debugs their AWS credentials,
which are fine. cmd.exe has no equivalent construct at all.

`set -euo pipefail` on the bash block so a failed step cannot be followed by a
push or an apply. PowerShell has **no equivalent** — `$ErrorActionPreference`
does not apply to a native executable's exit code, so `docker`, `aws` and
`tofu` failures do not stop the block. Run the PowerShell version a statement
at a time, or check `$LASTEXITCODE` after each, and in particular confirm the
push succeeded before running the release step below.

`--platform linux/amd64` matters on Apple Silicon: Fargate will not run an
arm64 image on the default x86 platform, and the task fails with an exec
format error that is easy to misread as a crash loop.

## 4. Release

Image tags are immutable and deploys are explicit, so the running version
cannot change silently during a study wave.

**This is the step where the two paths differ, so pick one deliberately.**

### 4a. The path in use: register a task definition, point the service at it

This is how revisions 35, 36, 37 and 38 came to exist, and it is what to do
today. Nothing here needs Terraform or its state.

First, check that nobody is mid-encounter — a rollout retires the old task about
two minutes later and Fargate caps the stop timeout at 120 s, so a conversation
running on the old task is cut. `OPERATIONS.md` has the check under [Before
every deploy](OPERATIONS.md#before-every-deploy-is-anyone-mid-encounter).

Step 1 — take the revision that is serving right now as the starting point, so
you change one thing rather than re-declaring twenty:

```bash
REGION=us-east-1
CLUSTER=relational-fluency
SERVICE=platform
FAMILY=relational-fluency-agent

# What the SERVICE is running — not what is newest in the family.
TD=$(aws ecs describe-services --cluster $CLUSTER --services $SERVICE \
       --query "services[0].taskDefinition" --output text)
echo "$TD"                       # .../task-definition/relational-fluency-agent:38

aws ecs describe-task-definition --task-definition "$TD" \
  --query taskDefinition --output json > td.json
```

```powershell
$REGION  = "us-east-1"
$CLUSTER = "relational-fluency"
$SERVICE = "platform"
$FAMILY  = "relational-fluency-agent"

$TD = aws ecs describe-services --cluster $CLUSTER --services $SERVICE --query "services[0].taskDefinition" --output text
$TD
aws ecs describe-task-definition --task-definition $TD --query taskDefinition --output json > td.json
```

**Describe the ARN, not the family.** `--task-definition relational-fluency-agent`
resolves to the newest ACTIVE revision, which is not necessarily the one serving:
a revision registered and never deployed, or one someone rolled back from, is
still the newest. Building the next release on top of it silently re-deploys
whatever was in it. Going through the service's own `taskDefinition` field means
the thing you edit is the thing participants are talking to.

Step 2 — strip the read-only fields. `describe-task-definition` returns seven
keys that `register-task-definition` will not accept, and the refusal is a
generic `Unknown parameter` naming only the first one it hit, so removing them
one error at a time is a five-round trip nobody should spend:

```
python -c "import json;p='td.json';d=json.load(open(p));[d.pop(k,None) for k in ('taskDefinitionArn','revision','status','requiresAttributes','compatibilities','registeredAt','registeredBy','deregisteredAt')];json.dump(d,open(p,'w'),indent=2)"
```

Step 3 — make the change. Either edit `td.json` in an editor, or set the image
from the tag you just pushed:

```bash
python -c "import json,sys;p='td.json';d=json.load(open(p));d['containerDefinitions'][0]['image']=sys.argv[1];json.dump(d,open(p,'w'),indent=2)" "$REPO:$SHA"
```

```powershell
python -c "import json,sys;p='td.json';d=json.load(open(p));d['containerDefinitions'][0]['image']=sys.argv[1];json.dump(d,open(p,'w'),indent=2)" "${REPO}:${SHA}"
```

Environment variables live in `containerDefinitions[0].environment`, a list of
`{"name": …, "value": …}` objects. The task is missing four that the code
reads, and `infra/terraform/ecs.tf` sets all four — so the CLI revision and the
Terraform description agree once they are added:

| Name | Value | Why |
|---|---|---|
| `UPSTREAM_CONSENT_VERSION` | the survey's approved consent version | decides whether the wave records anything at all |
| `CLAUDE_MODEL` | `nto.gemini-3.1-flash-lite` (`text_model` in `variables.tf`) | the text engine, and what `provenance.text_model` on every record names; unset, the record names a code default |
| `SURVEY_RETURN_URL` | the survey's continuation link — see [Sending them back](OPERATIONS.md#sending-them-back) | where the completion button sends the participant; must be a URL that may receive the run id, completion code and participant key |
| `DATA_DIR` | `/data` | the image already sets it, so the app writes there either way; on the task it is visible to `describe-task-definition` and to the person trying to work out where the data went |

Add them in this file, in this step; there is no other door on this path.

Leave `secrets` exactly as it is. `ANTHROPIC_API_KEY` and `SESSION_KEY` are
injected from Secrets Manager by ARN; moving either into `environment` would
put the study's only credential into a task definition that
`describe-task-definition` prints to anyone with read access.

Step 4 — register, and note the revision number it prints:

```bash
aws ecs register-task-definition --cli-input-json file://td.json \
  --query "taskDefinition.[family,revision]" --output text
```

`file://td.json` is a path the CLI reads, not a URL, and it is relative to the
directory you are standing in. On Windows it is still `file://td.json` with
forward slashes.

Step 5 — point the service at that exact revision, not at the family. Naming
the family deploys whatever is newest, which is fine until two people are
working and is impossible to reconstruct afterwards:

```bash
aws ecs update-service --cluster $CLUSTER --service $SERVICE \
  --task-definition $FAMILY:39                       # the number step 4 printed

aws ecs describe-services --cluster $CLUSTER --services $SERVICE \
  --query "services[0].deployments[0].rolloutState" --output text
```

```powershell
aws ecs register-task-definition --cli-input-json file://td.json --query "taskDefinition.[family,revision]" --output text
aws ecs update-service --cluster $CLUSTER --service $SERVICE --task-definition "${FAMILY}:39"
aws ecs describe-services --cluster $CLUSTER --services $SERVICE --query "services[0].deployments[0].rolloutState" --output text
```

`"${FAMILY}:39"` and not `"$FAMILY:39"`: PowerShell reads a colon after a
variable name as a scope or drive qualifier (`$env:PATH`), so the unbraced form
does not expand to what you meant and `update-service` is handed a garbled
task-definition name.

`COMPLETED` means one task is serving and it is safe to start a conversation.
To redeploy the *same* task definition — after a secret rotates, say — use
`aws ecs update-service --cluster $CLUSTER --service $SERVICE
--force-new-deployment` rather than registering an identical revision.

Step 6 — write down what you did. There is no state file recording this and no
plan output to read back, so the deploy history is whatever people wrote in the
wave notes. The revision number, the image tag and the date are the minimum.

### 4b. The Terraform path, and why you cannot take it yet

```bash
tofu -chdir=infra/terraform apply -var container_image=$REPO:$SHA
```

PowerShell: `tofu -chdir=infra/terraform apply -var "container_image=${REPO}:${SHA}"`.

That command requires the state this stack was built from. `versions.tf` now
has an active shared backend, but the repository cannot establish that the
existing state has been migrated there. Run against empty state it proposes to
create the bucket, the ECR repositories, the IAM roles and the certificate that
already exist, fails partway on `AlreadyExists`, and leaves a state file that
describes neither the old world nor the new one. Find or rebuild the state
first; [Open questions](#open-questions) is where that work is written down.

Two notes that hold for whenever the path reopens:

- **`-var` rather than editing `infra/terraform/terraform.tfvars` in place.**
  This form needs no text editing and works identically in bash, zsh,
  PowerShell and cmd. Do **not** script the edit with `sed -i ''`, a
  macOS/BSD-only spelling that fails with exit 2 on GNU sed and leaves the file
  untouched — the release then re-applies the tag already pinned and the
  operator watches a successful deploy serve the previous build.
- **Check the committed pin against the service before every apply.**
  `terraform.tfvars` now pins `3d3cbfc`; the merge did not verify the running
  image. The pin and service matched at `cabc1dd` on 12 September, after the
  pin had lagged at `3cf8496`, two releases behind. An
  apply without `-var container_image=` against a stale pin would **roll
  production back** mid-study and report success. Whoever reopens the
  Terraform path re-verifies that pin and its `deployed:` line against
  `describe-services` in the same change, as a visible commit.

## Who can run which step

Find out which of these you hold **before** a wave, not at the deploy. Every
refusal below is an `AccessDenied` that arrives after the image is already
built and pushed, and two of them name something other than what is wrong.

| What you are doing | IAM actions it needs | Resource |
|---|---|---|
| See what is deployed | `ecs:DescribeServices`, `ecs:DescribeTaskDefinition`, `ecs:ListTasks`, `ecs:DescribeTasks` | cluster / service / family |
| Read the researcher key | `secretsmanager:GetSecretValue` (+ `kms:Decrypt` if that secret uses a customer key) | `relational-fluency/agent-api-key` |
| Log in to the registry | `ecr:GetAuthorizationToken` | `*` — this one is account-scoped and cannot be narrowed |
| Push the image | `ecr:BatchCheckLayerAvailability`, `ecr:InitiateLayerUpload`, `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`, `ecr:PutImage` | the `relational-fluency/platform` repository |
| Register a task definition | `ecs:RegisterTaskDefinition` **and** `iam:PassRole` | `ecs:*` on `*`; PassRole on `relational-fluency-task-execution` **and** `relational-fluency-task` |
| Deploy it | `ecs:UpdateService`, `ecs:DescribeServices` | the `platform` service |
| Read logs | `logs:FilterLogEvents`, `logs:DescribeLogGroups`, `logs:DescribeLogStreams` | `/ecs/relational-fluency/agent` |
| Check the load balancer | `elasticloadbalancing:DescribeTargetGroups`, `elasticloadbalancing:DescribeTargetHealth` | the target group |
| Look in the study bucket | `s3:ListBucket`, `s3:GetObject`, `kms:Decrypt` | the bucket and its key |
| Add the persistent volume (one-off) | `elasticfilesystem:CreateFileSystem`, `CreateAccessPoint`, `CreateMountTarget`, `DescribeFileSystems`; `ec2:CreateSecurityGroup`, `AuthorizeSecurityGroupIngress`, `DescribeSubnets`; `iam:PutRolePolicy` on the task role; then RegisterTaskDefinition + UpdateService again | see [No persistent volume](#no-persistent-volume-yet) |

**`iam:PassRole` is the one that catches people.** A person with every `ecs:*`
action still cannot register this task definition, because it names an
execution role and a task role and registering it is *passing* them. The
refusal reads `User ... is not authorized to perform: iam:PassRole on resource
... relational-fluency-task-execution`, which sends the reader to look at that
role — where they find nothing wrong, because the role is fine and it is their
own identity that lacks the grant.

The task role itself is separately verified and needs no change for a release:
`relational-fluency-task` already holds `s3:PutObject`/`GetObject` on
`encounters/*` and `steering-logs/*`, `s3:ListBucket`, and
`kms:GenerateDataKey` + `kms:Decrypt` on the study key.

## 5. Verify

```bash
curl -sS https://rf.ai-ready-workforce.ai.cornell.edu/health
dig +short rf.ai-ready-workforce.ai.cornell.edu                  # ALB addresses
```

**Read the word, not the status code.** `/health` answers HTTP 200 for as long
as the process can serve — deliberately, because the ALB target group matches
`200`, and a non-200 here would take every task out of service about ninety
seconds after it booted. What tells you whether this deployment can record a
study is the body:

```json
{"status": "ok",       "ready": true,  "config": {"ok": true,  "missing_required_env": []}}
{"status": "degraded", "ready": false, "config": {"ok": false, "missing_required_env": ["UPSTREAM_CONSENT_VERSION"]}}
```

`degraded` / `ready: false` is **not** a failed deploy and not a reason to roll
back. It means the task is serving but a required environment variable is unset
or still holds a placeholder — and in that state every study consent is
refused, every voice socket closes 4403, runs keep accumulating and **nothing
is recorded**. Set the variable named in `missing_required_env` on the service
and deploy again.

The task definition running in production today has no
`UPSTREAM_CONSENT_VERSION`, so the first deploy of this build **will** print
`degraded`. That is the alarm working, not the deploy failing. Point any uptime
monitor at `.ready == true` rather than at the HTTP status, which by design
cannot tell these apart.

Windows PowerShell — `curl` there is an alias for `Invoke-WebRequest` and
rejects `-sS`, and `dig` is not a Windows command:

```powershell
curl.exe -sS https://rf.ai-ready-workforce.ai.cornell.edu/health
Resolve-DnsName rf.ai-ready-workforce.ai.cornell.edu
```

Then open the app URL in a browser. Microphone capture requires HTTPS, which
the ALB provides — this is why encounters cannot be tested over a bare IP.

**`{"status":"ok"}` does not mean the deployment can collect anything.** A task
that is missing `UPSTREAM_CONSENT_VERSION` is healthy by every measure on this
page and records zero encounters, because no study consent can be written — and
a `/test` walkthrough will not show it either, since internal runs take a
different consent path on purpose. So verify the variable itself, not just the
probe:

```bash
TD=$(aws ecs describe-services --cluster relational-fluency --services platform \
      --query "services[0].taskDefinition" --output text)
aws ecs describe-task-definition --task-definition "$TD" \
  --query "taskDefinition.containerDefinitions[0].environment[?name=='UPSTREAM_CONSENT_VERSION']"
```

An empty list, or an entry whose value is blank or a placeholder, means the wave
would be lost in silence: `POST /api/consent` answers 503 naming the variable
(on the build deployed today, 404 `no such participant record` — the record
exists; the message is misleading), the record stays unconsented, and every
voice socket closes 4403. Go back to step 1. The
wave-time check, once participants are arriving, is in
[`OPERATIONS.md`](OPERATIONS.md#the-consent-version-unset-this-records-nothing).

## No persistent volume (yet)

`infra/terraform/ecs.tf` declares an EFS file system, an access point, a
`/data` mount point and `DATA_DIR=/data`. **None of that is deployed.** Checked
12 September 2026:

- Revisions 35, 36, 37 and 38 all have `volumes=[]` and no `mountPoints`.
- **There is no EFS file system in the account** for any of them to mount.
- The image's own `ENV DATA_DIR=/data` means the app does write to `/data` —
  but with no volume behind it, `/data` is the container's writable layer.

So every run document, participant record, transcript, WAV and the SQLite index
is destroyed by the next deploy, crash or task retirement. Only the webcam video
survives, because the browser PUTs it straight to S3. The one-line check, and
what its output means for a wave in progress, is at the top of
[`OPERATIONS.md`](OPERATIONS.md#read-this-before-collecting-anything); the
standing rule until a volume exists is that you pull each wave's data off the
server before the next deploy.

Closing it is not a release step. It is: create the file system, an access
point owning `/data` as uid 1000 (the Dockerfile's `appuser`), and a mount
target with a security group that lets the task's security group reach it on
2049; grant the task role `elasticfilesystem:ClientMount` and `ClientWrite`
scoped to that access point; then register a revision carrying both the
`volume` block and the container's `mountPoints`. `ecs.tf` already says all of
this in Terraform, which is the argument for fixing the state first rather than
reproducing it by hand — see [Open questions](#open-questions).

## Webcam recordings and the study bucket

The webcam recording is the artefact Phase 2 rates, and it is the only thing
this platform puts in S3. Session audio, transcripts, events and aligned records
are written to `/data` on the task and nothing copies them to the bucket — and
today, per the section above, that `/data` is the container's own disk. The
recording reaches the bucket without passing through
the application at all: the browser PUTs it straight to S3 with a URL the task
signs. That is IRB "What Participants See and Hear" 6a satisfied by
construction, because a recording the application never holds cannot transit the
model path.

### The five names, and which mechanism reads each

They are read by **two** mechanisms whose precedence runs in **opposite**
directions. That is the whole reason this section exists: get it wrong and
nothing errors, the wave simply comes back with no recordings.

| Name | Read by | Precedence | On Fargate |
|---|---|---|---|
| `S3_BUCKET` | `server/video.py`, through `server.llm.setting()` | **`.env` wins** over the shell | set by `infra/terraform/ecs.tf` |
| `AWS_REGION` | `server/video.py`, through `server.llm.setting()` | **`.env` wins** over the shell | set by `infra/terraform/ecs.tf` |
| `AWS_ACCESS_KEY_ID` | boto3's own credential chain | **the shell wins** over `.env` | unset — the task role supplies it |
| `AWS_SECRET_ACCESS_KEY` | boto3's own credential chain | **the shell wins** over `.env` | unset |
| `AWS_SESSION_TOKEN` | boto3's own credential chain | **the shell wins** over `.env` | unset (SSO / assume-role only) |

The bridge between the two is one line — `server/app.py` calls `load_dotenv()`
at import, which copies `.env` into the process environment *without* overriding
anything already there. Three things follow, and each has cost somebody an
afternoon somewhere:

- **A key exported in the shell beats a key written in `.env`.** If S3 is
  reaching the wrong account, look at the shell before the file.
- **A key in `.env` reaches boto3 only in a process that imported
  `server.app`.** An offline tool that imports `server.video` or
  `server.rater_packet` on its own never runs `load_dotenv()`, resolves no
  credentials, and reports every encounter as having no recording while the
  objects sit in the bucket. Export the credentials in the shell, or use
  `~/.aws`, for anything that is not the server.
- **The `.env` that counts is the one at the repository root**, wherever you are
  standing when you start the process. `python-dotenv` walks up from
  `server/llm.py`, so a `.env` beside the terminal's working directory is not
  read at all.

`AWS_DEFAULT_REGION` is **not read**. `server/video.py` takes its region from
`AWS_REGION` and passes it to every client explicitly, so the AWS CLI's
preferred spelling moves nothing: set it alone and the region stays `us-east-1`,
which is right until the day the bucket is somewhere else — at which point every
call goes to the wrong region and comes back as a `PermanentRedirect` naming
neither variable.

### Where to put them

**On AWS — nowhere.** The task role is the credential. `infra/terraform/ecs.tf`
grants `aws_iam_role.task` `s3:PutObject`, `s3:GetObject` and
`s3:AbortMultipartUpload` on `encounters/*` and `steering-logs/*`, `s3:ListBucket`
on the bucket, and `kms:GenerateDataKey` plus `kms:Decrypt` on the study key; it
sets `S3_BUCKET` and `AWS_REGION` in the task definition. Do not paste a
long-lived key into a task: it would outlive the role it replaced, and it cannot
get there through `.env` in any case — `.dockerignore` is an allowlist, `.env` is
not on it, and the image contains no `.env` at all.

**On a researcher's machine, for the server** — `.env` at the repository root,
using the block `.env.example` now carries. Both halves of the pair or neither;
see below.

**On a researcher's machine, for a CLI** — the shell, or `~/.aws`. Either of
these works and neither depends on `load_dotenv()` running:

```bash
aws sso login --profile relational-fluency     # temporary credentials
export AWS_PROFILE=relational-fluency
```

```powershell
aws sso login --profile relational-fluency
$env:AWS_PROFILE = "relational-fluency"
```

### Half a credential is worse than none

It is the likeliest paste error, so it is worth knowing exactly what it costs.
Both blank is a clean "no credentials": botocore ignores empty strings, the
chain resolves nothing, and `server/video.py` takes its known-no-credentials
shortcut — one credential-chain walk a minute rather than one per encounter.

An **ID with no secret is not that**. botocore raises `PartialCredentialsError`
out of client construction, the shortcut declines to guess (it answers "no
opinion" to anything it cannot read, deliberately, so that not-knowing never
looks like knowing), and every S3 call makes the attempt and logs a warning. The
recording still survives, because the presign route answers 503 and the browser
falls back to this server — but the console fills with a warning naming a
variable nobody has looked at. Fill both, or neither.

### Checking it worked, before a wave rather than during one

Both credentialed seams are probed at startup, and the bucket probe asks
*readable* and *writable* separately because they fail separately and are fixed
differently: `head_bucket` needs `s3:ListBucket`, while the presigned PUT the
browser executes is signed by this process and needs `s3:PutObject` plus the KMS
grant. A read-only preflight would pass happily on a task role that cannot store
a single recording.

The answer is the `storage` block on `/health`, and **read it with the
researcher key**. `/health` is the one route reachable from the open internet — it is exempt
from the Host-header guard, because the ALB addresses the task by a private IP
that can never be in the allowlist — so an unauthenticated caller is given a
projection of the block, `ok` / `checked` / `readable` / `writable` /
`error_code` and nothing else. With the researcher key it widens to the full
diagnosis: `credentials`, the `bucket` and `region` it actually used, and a
`detail` string off the botocore exception (redacted through `redact_key`, the
same scrubber the gateway's errors go through, because a secret with a stray
newline in it comes back with the signing header quoted whole).

```bash
KEY=$(aws secretsmanager get-secret-value \
        --secret-id relational-fluency/agent-api-key \
        --query SecretString --output text)
curl -sS "https://rf.ai-ready-workforce.ai.cornell.edu/health?key=$KEY" \
  | python -c "import json,sys; print(json.load(sys.stdin)['storage'])"
```

```powershell
$KEY = aws secretsmanager get-secret-value --secret-id relational-fluency/agent-api-key --query SecretString --output text
curl.exe -sS "https://rf.ai-ready-workforce.ai.cornell.edu/health?key=$KEY" | python -c "import json,sys; print(json.load(sys.stdin)['storage'])"
```

Each row says something different about what a wave will produce:

| `storage` says | What it means | What a wave produces |
|---|---|---|
| `ok: null, checked: false` | the probe has not run yet — an import, not a deployment | nothing known; ask again once the task is up |
| `credentials: false` (key required) | nothing in the chain resolved | every recording lands on the task's own disk, and dies with the next deploy |
| `readable: false` with an `error_code` | wrong bucket, wrong region, or `s3:ListBucket` missing | same |
| `readable: true, writable: false` | `s3:PutObject` or the KMS grant is missing | same, and the presigned PUT fails in the participant's browser |
| `ok: true` | signed, reachable, writable | recordings go to the bucket |

A credential that appears *after* boot needs no restart: the credential chain is
re-walked every 60 seconds, so `aws sso login` in the next terminal, or a task
role attached a moment late, starts working inside a minute.

### The local fallback, and why it must stay

When presigning is unavailable the browser PUTs the recording to this server
instead — `PUT /api/sessions/{id}/video` — and the bytes land next to the
session as `webcam.webm`. That path is what makes a credential-less laptop
usable at all: without it, every recording made on a developer machine is lost
when the page closes, which is why nobody had ever watched a recording play in
the rating console.

It is not a second-class path. It applies the presign route's authorisation
exactly — the same participant key check, the same session-owner check, the same
one-shot refusal once a recording exists — and it writes the **same**
`video_uploaded` event, with the same `type`, `key`, `bytes` and `status`
fields, so every reader of the trail (`rater_packet`, `encounter_record`,
`verify_record`, `video.upload_receipt`) sees one uniform fact. The only
difference is an additive `"via": "local"`, which is the one thing in the trail
that tells an operator the bytes are on a task's disk rather than in the bucket.
Playback does not care either: `/api/rater/video/{assignment_id}` prefers a
local file and falls back to S3, so a rater plays the same recording the same
way whichever leg carried it.

Making S3 real does not retire this. It narrows what it covers, which is worth
being exact about: the browser falls back only when the *presign* fails
(`presign_http_5xx`), not when the PUT to S3 fails. A bucket that is reachable
but refuses the write — a missing KMS grant, or a CORS rule that does not name
this deployment's origin — produces no fallback and no recording. That is the
argument for the two subsections below.

### What a real deployment needs that a laptop does not

**Bucket policy.** There is no `aws_s3_bucket_policy` on the study bucket today.
Access is granted entirely by the identity policy on the task role, and the
public access block (`block_public_acls`, `block_public_policy`,
`ignore_public_acls`, `restrict_public_buckets`, all true) is what stops the
bucket being opened by accident. That is a defensible baseline, and a resource
policy is still worth adding before real participant video lands, for the one
thing an identity policy cannot express — a deny that applies to *every*
principal, including one nobody has created yet:

- `aws:SecureTransport: false` → `Deny`. Objects are IRB video; an
  unencrypted-in-transit GET should not be possible at all, rather than merely
  not done.
- Optionally `aws:PrincipalOrgID` → deny anything outside the Cornell
  organisation, which bounds the blast radius of a mistakenly broad grant.

**CORS for the browser-direct PUT.** This one is load-bearing and easy to get
subtly wrong, because a CORS failure in the browser is a bare network error:
`static/v2.html` reports it as `network`, which is *not* one of the reasons that
trigger the local fallback, so a wrong CORS rule loses every recording in the
wave with nothing in any log naming CORS. `infra/terraform/storage_secrets.tf`
already carries the rule; what it has to keep saying:

- `allowed_methods = ["PUT"]` — and only PUT. Playback goes through the
  application now, not browser-direct from S3, so `GET` is not needed and adding
  it would re-open a surface that was deliberately closed.
- `allowed_origins` must list **every origin the participant page is served
  from**, and the allowlist is exactly three entries long:
  `https://rf.ai-ready-workforce.ai.cornell.edu`, `http://localhost:8765` and
  `http://127.0.0.1:8765` — the default `PORT`, spelled both ways because a
  browser treats those two hosts as different origins. **Local webcam testing
  therefore works on port 8765 and on no other port.** A researcher who has
  credentials AND runs the server on, say, 8000 is the case to watch:
  presigning succeeds, so the local fallback is never reached, and the PUT is
  refused at the browser's preflight with an error the page can only report as
  `network`. Add the origin, or run on 8765.
- `allowed_headers = ["*"]`. The presigned URL signs `host` and nothing else —
  `Content-Type` is deliberately left unsigned so Safari's MP4 and everyone
  else's WebM both land under the one `webcam.webm` key — but the browser still
  *sends* `Content-Type`, so the preflight asks for it and a narrowed header
  list refuses it.

**Lifecycle and retention.** There is no lifecycle configuration on the
**live** study bucket, and versioning is **enabled**. Two consequences that a
laptop never exhibits:

- Nothing expires. Recordings are kept until somebody deletes them by hand.
- A delete is not a delete. With versioning on, `DeleteObject` writes a delete
  marker and the bytes survive as a noncurrent version. `config/consent.yaml`
  tells participants they may request that their recordings be deleted, so
  honouring that request today takes a version-aware delete, not an ordinary
  one.

The rule is now **written**, in `infra/terraform/storage_secrets.tf`
(`aws_s3_bucket_lifecycle_configuration.study_data`), and deliberately left
unable to apply until the IRB supplies the number:

- `study_data_retention_days` has **no default**. It is the period the approved
  consent text promises, and that text still carries `[FILL IN: retention
  period for audio, video and transcripts]`. **The lifecycle rule and the
  consent text are one decision, and the consent text is the half that has to
  be settled first** — a bucket that expires objects at a date the form does
  not mention is deleting research data early, and a bucket that keeps them
  forever while the form promises a date is the same mistake pointing the other
  way. `tofu plan` stops and asks; do not answer it from this page.
- `study_data_superseded_version_days` (default 30) is how long a superseded
  version survives after being overwritten — an operational recovery window,
  not the IRB's number — and a precondition refuses any value longer than the
  retention period, because on a versioned bucket an `expiration` rule does
  **not** delete the bytes, it makes them noncurrent. The true worst case for a
  byte is retention + 30 days; set the window to 1 if the protocol's wording is
  strict enough that this matters.
- Every rule that deletes data is scoped to `encounters/` and `steering-logs/`
  — the two prefixes the task role can write — so a hand-made export or backup
  in the same bucket is never on a schedule nobody told its owner about. Each
  also aborts incomplete multipart uploads after 7 days (the task role holds
  `s3:AbortMultipartUpload`; a webcam PUT that dies mid-upload leaves parts that
  no listing shows and every bill includes). A bucket-wide rule clears delete
  markers with nothing under them, which cannot delete data by construction.
- **The 21 pilot recordings are in scope.** The day this applies, their clocks
  are already running from their upload dates, and anything older than the
  retention period is deleted on S3's first evaluation cycle, within 24–48
  hours, with no further confirmation. Verify their ages before applying and
  get the answer from the IRB, not from here.

## Troubleshooting

| Symptom | Cause |
|---|---|
| **503 from `POST /api/consent` naming `UPSTREAM_CONSENT_VERSION` (404 `no such participant record` on the build deployed today), or sockets closing 4403, or runs with no encounters** | **`UPSTREAM_CONSENT_VERSION` missing from the task.** It is missing today. On the old build the record exists and the message is misleading. See [step 5](#5-verify) |
| `tofu apply` stops asking for `upstream_consent_version` | Working as designed — [step 1](#1-provision-the-infrastructure). It has no default because a deployment without it collects nothing |
| `tofu apply` stops asking for `study_data_retention_days` | Working as designed — the bucket lifecycle rule needs the IRB's retention period and this repository is not entitled to invent it. See [Lifecycle and retention](#webcam-recordings-and-the-study-bucket) |
| `tofu plan` proposes to CREATE the bucket, roles or certificate | The state for this stack is missing. **Stop**; do not apply. See [Read this first](#read-this-first-the-runbook-and-the-practice-have-diverged) |
| `Unknown parameter in input: "taskDefinitionArn"` from `register-task-definition` | The read-only fields were not stripped from `td.json` — [step 4a](#4a-the-path-in-use-register-a-task-definition-point-the-service-at-it) |
| `not authorized to perform: iam:PassRole` | Your identity, not the role it names. See [Who can run which step](#who-can-run-which-step) |
| `server not found` | Records not created yet — apply has not completed |
| 503 from the ALB | No healthy targets: image missing, or task crashed. `aws logs tail /ecs/relational-fluency/agent --follow` |
| Task stops immediately | Wrong image architecture; rebuild with `--platform linux/amd64` |
| Certificate stuck pending | Validation records missing from the zone; re-run apply |
| Mic blocked in browser | Page not served over HTTPS |

## Open questions

These could not be settled from inside the repository, and each one is written
here as a question with somebody to ask rather than filled in with a plausible
procedure. Answering them is what closes the gap between this page and the
Terraform that is supposed to be the source of truth.

1. **Where is the Terraform state for this stack?** At the 12 September
   inspection, the account's state bucket held `bootstrap/` and `staging/`
   only. Ask whoever ran the original
   build-out whether a `terraform.tfstate` survives on their machine or in a
   backup. If it does: provision the shared backend described in
   [Adding a second deployer](OPERATIONS.md#adding-a-second-deployer), then run
   `tofu init -migrate-state` using the already-configured S3 backend, then
   `tofu plan` — and expect that plan to show
   drift from every hand-registered revision since. If it does not survive, the
   options are `tofu import` for each of the ~50 resources, or accepting the CLI
   path permanently and deleting the Terraform that describes an unmanaged
   stack. That is a decision for the person who owns the account, not a
   documentation change.
2. **Who holds `iam:PassRole` on the two ECS roles?** The
   [permissions table](#who-can-run-which-step) says what a release needs; it
   cannot say who has it. Ask the Cornell AWS administrator for the account, and
   have the answer before a wave rather than during one.
3. **What is the retention period for audio, video and transcripts?**
   `config/consent.yaml` still carries `[FILL IN: retention period …]`, and the
   bucket lifecycle rule and the consent text are one decision. The rule is
   written in `storage_secrets.tf` and waits on `study_data_retention_days`,
   which has no default. The IRB protocol settles it; the PI is who to ask.
4. **Is there a second copy of anything?** Nothing archives session audio,
   transcripts or events to S3, and the volume that would hold them does not
   exist yet. Whether the IRB data-management plan requires a backup, and where
   it would live, is a question for the PI and the IRB office.
5. **Did any of revisions 35–37 differ in a way worth keeping?** They were
   registered by hand and there is no record of what changed between them.
   `aws ecs describe-task-definition --task-definition relational-fluency-agent:35`
   (and 36, 37) will print them; nobody has diffed them.

## Not yet wired

- Serving `api.rf.*` as a distinct backend: the record and certificate exist,
  but the ALB currently routes both hostnames to the same target group.
- RDS for participant keys and scenario assignment (state is on the task today).
- CloudFront in front of the bucket. Not needed for rater review any more —
  playback is `/api/rater/video/{assignment_id}`, streamed by the application
  from a local file or from S3, so there is no signed URL to distribute and no
  storage credential in a rater's browser history.
