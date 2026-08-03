# Infrastructure — director–actor agent on AWS

Terraform for the agent endpoint described in `docs/architecture.md` and priced in
`docs/RelationalFluency_AWS_Cost_Estimation.pdf`: VPC (2 AZ, single NAT), ALB + ACM
(HTTPS), ECS Fargate service, ECR, KMS-encrypted S3 study-data bucket, Secrets
Manager, CloudWatch logs. **Scope: the agent endpoint only** — the participant web
app, RDS, and CloudFront land in a later pass once the Phase-1 app exists.

## Prerequisites

- AWS account + credentials (`aws sts get-caller-identity` works)
- Terraform ≥ 1.6, Docker, AWS CLI v2
- A Route 53 hosted zone for a domain you control (e.g. `yourlab.org`)

## Deploy (first time, ~20 minutes)

```bash
cd infra/terraform

# 1) Provision everything
terraform init
terraform apply -var domain_name=yourlab.org
# note the outputs: agent_url, ecr_repository

# 2) Set the two secrets (values never touch git or Terraform state)
aws secretsmanager put-secret-value \
  --secret-id relational-fluency/anthropic-api-key --secret-string 'sk-ant-...'
aws secretsmanager put-secret-value \
  --secret-id relational-fluency/agent-api-key --secret-string "$(openssl rand -hex 32)"

# 3) Build and push the agent image (from repo root)
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1
REPO=$AWS_ACCOUNT.dkr.ecr.$REGION.amazonaws.com/relational-fluency/agent
SHA=$(git rev-parse --short HEAD)

aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REPO
docker build -t $REPO:$SHA agents/
docker push $REPO:$SHA

# 4) Point the service at the image
terraform apply -var domain_name=yourlab.org -var container_image=$REPO:$SHA

# 5) Verify
curl https://agent.yourlab.org/health
```

## Wire into ElevenLabs

In the agent's settings → LLM → **Custom LLM**:

- Server URL: the `agent_url` output (`https://agent.yourlab.org/v1`)
- API key: the value you stored in `relational-fluency/agent-api-key`
- First message: the scenario's fixed opening line (see
  `agents/src/agents/director_actor/scenarios.py`)
- Dynamic variables: `participant_name`, `company_name`

## Operations

- **Steering trail:** every turn logs a `STEERING {...}` line → CloudWatch group
  `/ecs/relational-fluency/agent` (90-day retention). Export per-wave to
  `s3://relational-fluency-study-data/steering-logs/` before ratings begin.
- **Deploys are release-SHA images** (ECR tags immutable). The image tag serving
  each study wave is the auditable agent version. **Freeze during collection.**
- **Scale for collection bursts:** `aws ecs update-service --cluster
  relational-fluency --service agent --desired-count 2` (Terraform ignores manual
  count changes by design).
- **Model pinning:** `actor_model` / `director_model` are Terraform variables →
  environment variables. Set snapshots explicitly; record them in the wave notes.
- **Costs:** tracked against `docs/RelationalFluency_AWS_Cost_Estimation.pdf`
  (~$128–148/mo). Set an AWS Budget alarm at $160.

## What's deliberately NOT here yet

- Web app service + RDS (needs the Phase-1 app first)
- CloudFront + rater access (needs recordings to exist)
- Post-call sync worker (ElevenLabs → S3) — next build item
- Remote Terraform state backend (uncomment in `versions.tf` after creating a
  state bucket)
