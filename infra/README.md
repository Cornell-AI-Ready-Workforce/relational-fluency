# Infrastructure — AWS (Terraform)

Terraform for the AWS side of `docs/architecture.md`, priced in
`docs/RelationalFluency_AWS_Cost_Estimation.pdf`: VPC (2 AZ, single NAT), ALB + ACM
(HTTPS), ECS Fargate service, ECR, KMS-encrypted S3 study-data bucket, Secrets
Manager, CloudWatch logs.

**Scope:** currently provisions the agent endpoint only. Serving the simulation
platform itself (web app + session broker) and adding CloudFront for rater
review land in a later pass, as part of the migration.

## Prerequisites

- AWS account + credentials (`aws sts get-caller-identity` works)
- Terraform ≥ 1.6, Docker, AWS CLI v2
- A Route 53 hosted zone. Cornell IT has already delegated
  `ai-ready-workforce.ai.cornell.edu` to Route 53, so subdomains are
  self-service. The study hostnames `rf.` and `api.rf.` under that zone still
  need records and an ACM certificate.

## Deploy (first time, ~20 minutes)

```bash
cd infra/terraform

# 1) Provision everything
terraform init
terraform apply -var domain_name=yourlab.org
# note the outputs: agent_url, ecr_repository

# 2) Set the two secrets (values never touch git or Terraform state)
# The LLM key is the Cornell LiteLLM virtual key (sk-...), used for both the
# Gemini Live actor and the director. The secret id is unchanged for now.
aws secretsmanager put-secret-value \
  --secret-id relational-fluency/anthropic-api-key --secret-string 'sk-...'
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

## Wire into ElevenLabs (superseded)

> Retired — voice no longer runs on ElevenLabs Agents. Kept only because the
> `agent_url` output and TLS wiring below are still how you verify the endpoint
> is reachable. Current design: [`../docs/architecture.md`](../docs/architecture.md).

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

- Serve the simulation platform (web app + session broker) on Fargate behind the
  ALB, with WSS for the participant audio stream — next build item
- `rf.` / `api.rf.` records in the delegated Route 53 zone + ACM certificate
- CloudFront + rater access (needs recordings to exist)
- RDS for participant keys and scenario assignment
- Remote Terraform state backend (uncomment in `versions.tf` after creating a
  state bucket)
