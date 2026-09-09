# Deploying to AWS (study environment)

Brings up `https://rf.ai-ready-workforce.ai.cornell.edu`. Until the first
`terraform apply` completes, that hostname does not resolve at all — the DNS
records are ALB aliases created by this process, so "server not found" is the
expected state beforehand.

For the demo-only Fly path see [`DEPLOY.md`](DEPLOY.md); participant data must
not be collected there.

## What gets created

VPC (2 AZ, single NAT) · ALB + ACM certificate covering `rf.*` and `api.rf.*` ·
ECS Fargate service running the platform · ECR · KMS-encrypted S3 study-data
bucket · Secrets Manager · CloudWatch logs.

## Prerequisites

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

The first apply creates everything except a running task — there is no image
yet, so the service starts with zero healthy targets. That is expected.

```bash
cd infra/terraform
tofu init
tofu apply
```

Verified 2026-08-19: `tofu plan` is clean — 50 resources to add, none destroyed.
Certificate validation adds DNS records automatically and takes a few minutes.
Note the outputs: `ecr_repository`, `app_url`, `api_url`, `study_data_bucket`.

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
REPO=$(tofu -chdir=infra/terraform output -raw ecr_repository)
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
$REPO = tofu -chdir=infra/terraform output -raw ecr_repository
$SHA = git rev-parse --short HEAD

aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REGISTRY

docker build --platform linux/amd64 -t "${REPO}:${SHA}" .
docker push "${REPO}:${SHA}"
```

The registry host is now built from `$ACCOUNT` and `$REGION` rather than
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

```bash
tofu -chdir=infra/terraform apply -var container_image=$REPO:$SHA
```

PowerShell: `tofu -chdir=infra/terraform apply -var "container_image=${REPO}:${SHA}"`.

`-var` rather than editing `infra/terraform/terraform.tfvars` in place. This
form needs no text editing and works identically in bash, zsh, PowerShell and
cmd. If the committed pin in `terraform.tfvars` also has to move (it is
committed on purpose, per that file's own header), edit it by hand and commit
it as a separate, visible step — do **not** script it with `sed -i ''`, which
is a macOS/BSD-only spelling that fails with exit 2 on GNU sed and leaves the
file untouched. `docs/OPERATIONS.md` has the same procedure and the same note.

## 5. Verify

```bash
curl -sS https://rf.ai-ready-workforce.ai.cornell.edu/health     # {"status":"ok"}
dig +short rf.ai-ready-workforce.ai.cornell.edu                  # ALB addresses
```

Windows PowerShell — `curl` there is an alias for `Invoke-WebRequest` and
rejects `-sS`, and `dig` is not a Windows command:

```powershell
curl.exe -sS https://rf.ai-ready-workforce.ai.cornell.edu/health
Resolve-DnsName rf.ai-ready-workforce.ai.cornell.edu
```

Then open the app URL in a browser. Microphone capture requires HTTPS, which
the ALB provides — this is why encounters cannot be tested over a bare IP.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `server not found` | Records not created yet — apply has not completed |
| 503 from the ALB | No healthy targets: image missing, or task crashed. `aws logs tail /ecs/relational-fluency/agent --follow` |
| Task stops immediately | Wrong image architecture; rebuild with `--platform linux/amd64` |
| Certificate stuck pending | Validation records missing from the zone; re-run apply |
| Mic blocked in browser | Page not served over HTTPS |

## Not yet wired

- Serving `api.rf.*` as a distinct backend: the record and certificate exist,
  but the ALB currently routes both hostnames to the same target group.
- RDS for participant keys and scenario assignment (state is on the task today).
- CloudFront signed URLs for rater review.
