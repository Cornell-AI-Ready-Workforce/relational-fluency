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

```bash
brew install opentofu           # or, if brew is blocked by an untrusted tap,
                                # grab the release from github.com/opentofu/opentofu
                                # and verify against its SHA256SUMS
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

```bash
# Cornell LiteLLM virtual key — serves both the realtime actor and the director
aws secretsmanager put-secret-value \
  --secret-id relational-fluency/anthropic-api-key \
  --secret-string 'sk-...'

# Gate on the participant URL (?key=...); use a real random value
aws secretsmanager put-secret-value \
  --secret-id relational-fluency/agent-api-key \
  --secret-string "$(openssl rand -hex 32)"
```

## 3. Build and push the platform image

```bash
cd "$(git rev-parse --show-toplevel)"
REGION=us-east-1
REPO=$(tofu -chdir=infra/terraform output -raw ecr_repository)
SHA=$(git rev-parse --short HEAD)

aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin "${REPO%%/*}"

docker build --platform linux/amd64 -t $REPO:$SHA .
docker push $REPO:$SHA
```

`--platform linux/amd64` matters on Apple Silicon: Fargate will not run an
arm64 image on the default x86 platform, and the task fails with an exec
format error that is easy to misread as a crash loop.

## 4. Release

Image tags are immutable and deploys are explicit, so the running version
cannot change silently during a study wave.

```bash
tofu -chdir=infra/terraform apply -var container_image=$REPO:$SHA
```

## 5. Verify

```bash
curl -sS https://rf.ai-ready-workforce.ai.cornell.edu/health     # {"status":"ok"}
dig +short rf.ai-ready-workforce.ai.cornell.edu                  # ALB addresses
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
