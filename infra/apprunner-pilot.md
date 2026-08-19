# Pilot deployment — App Runner (no domain, no Terraform)

> **Superseded (2026-08).** This runbook deploys the director–actor endpoint as
> a custom LLM for **ElevenLabs Agents**, which is retired — voice now runs as a
> single Gemini Live speech-to-speech session through the Cornell LiteLLM
> gateway, hosted in the platform's own session broker. Kept because the Cornell
> AWS authentication steps (§0) and the App Runner mechanics are still accurate
> and useful. For current design see [`../docs/architecture.md`](../docs/architecture.md);
> for production deployment use `infra/terraform/`.

Fast path to a live HTTPS agent endpoint for the ElevenLabs pilot. Prereqs: AWS CLI
configured, Docker running. Cost: ~$45–55/mo while running; **pause the service
between pilot sessions** (~$2/mo paused). Production (data collection) uses
`infra/terraform/` instead — this runbook is pilot-only.

Every block below is copy-paste; replace only `<ANTHROPIC_KEY>`.

## 0. One-time: configure the AWS CLI (Cornell)

Cornell AWS does not use permanent access keys — CLI credentials are temporary, via
NetID + Duo. Two paths; **first ask whoever deployed the AIW staging environment how
they authenticate** (they solved this already, and their answer is your answer):

```bash
# Path A — AWS SSO / Identity Center (newer Cornell accounts):
brew install awscli
aws configure sso        # SSO start URL + region from the account owner
aws sso login

# Path B — awscli-login plugin (Shibboleth ECP, per Cornell Cloud docs):
pip install awscli-login
aws login configure      # ECP endpoint: https://shibidp.cit.cornell.edu/idp/profile/SAML2/SOAP/ECP
aws login                # NetID password + Duo

# Either way, verify before continuing:
aws sts get-caller-identity   # must print the Cornell account — stop here if it errors
```

Note: credentials are temporary (typically ~1–12 h) — rerun `aws sso login` /
`aws login` when commands start failing with auth errors mid-session.

## 0b. One-time: verify the LiteLLM proxy speaks Anthropic format

The agent calls models through the Cornell LiteLLM proxy. You need from the proxy
admin: the **base URL**, your **virtual key**, and the **model alias names**. Verify:

```bash
export LITELLM_BASE=https://<litellm-host>       # from the proxy admin
export LITELLM_KEY=sk-...                        # your LiteLLM key

curl -s $LITELLM_BASE/v1/messages \
  -H "x-api-key: $LITELLM_KEY" -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"<actor-model-alias>","max_tokens":50,
       "messages":[{"role":"user","content":"say ok"}]}'
```

If this returns a normal Anthropic-style response, you're set — the server needs no
code changes (the SDK reads `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY` from env).
If the proxy only exposes the OpenAI-compatible endpoint, stop and tell Claude — the
client needs a small adaptation.

## 1. Push the agent image to ECR

```bash
cd ~/relational-fluency
export AWS_REGION=us-east-1
export AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export REPO=$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/relational-fluency/agent
export SHA=$(git rev-parse --short HEAD)

aws ecr create-repository --repository-name relational-fluency/agent \
  --image-tag-mutability IMMUTABLE 2>/dev/null || true

aws ecr get-login-password | docker login --username AWS --password-stdin $REPO

# --platform matters: Apple Silicon Macs must build for amd64
docker build --platform linux/amd64 -t $REPO:$SHA agents/
docker push $REPO:$SHA
```

## 2. Secrets

```bash
# The "anthropic-api-key" secret holds your LiteLLM virtual key
aws secretsmanager create-secret --name relational-fluency/anthropic-api-key \
  --secret-string "$LITELLM_KEY"
export AGENT_KEY=$(openssl rand -hex 32)
aws secretsmanager create-secret --name relational-fluency/agent-api-key \
  --secret-string "$AGENT_KEY"
echo "ElevenLabs API key (save it): $AGENT_KEY"
```

## 3. IAM roles for App Runner (one-time)

```bash
# Role App Runner uses to pull from ECR
aws iam create-role --role-name AppRunnerECRAccess --assume-role-policy-document '{
  "Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"Service":"build.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name AppRunnerECRAccess \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess

# Instance role so the container can read the two secrets
aws iam create-role --role-name RelFluencyAgentInstance --assume-role-policy-document '{
  "Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"Service":"tasks.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam put-role-policy --role-name RelFluencyAgentInstance --policy-name read-secrets \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",
  \"Action\":\"secretsmanager:GetSecretValue\",
  \"Resource\":\"arn:aws:secretsmanager:$AWS_REGION:$AWS_ACCOUNT:secret:relational-fluency/*\"}]}"
sleep 10  # let IAM propagate
```

## 4. Create the service

```bash
export ANTHROPIC_ARN=$(aws secretsmanager describe-secret \
  --secret-id relational-fluency/anthropic-api-key --query ARN --output text)
export AGENTKEY_ARN=$(aws secretsmanager describe-secret \
  --secret-id relational-fluency/agent-api-key --query ARN --output text)

aws apprunner create-service --service-name relational-fluency-agent \
  --source-configuration "{
    \"ImageRepository\": {
      \"ImageIdentifier\": \"$REPO:$SHA\",
      \"ImageRepositoryType\": \"ECR\",
      \"ImageConfiguration\": {
        \"Port\": \"8100\",
        \"RuntimeEnvironmentVariables\": {
          \"SCENARIO_ID\": \"S2A\",
          \"ACTOR_MODEL\": \"<actor-alias-on-litellm>\",
          \"DIRECTOR_MODEL\": \"gemini-3.6-flash\",
          \"ANTHROPIC_BASE_URL\": \"$LITELLM_BASE\"
        },
        \"RuntimeEnvironmentSecrets\": {
          \"ANTHROPIC_API_KEY\": \"$ANTHROPIC_ARN\",
          \"AGENT_API_KEY\": \"$AGENTKEY_ARN\"
        }
      }
    },
    \"AuthenticationConfiguration\": {
      \"AccessRoleArn\": \"arn:aws:iam::$AWS_ACCOUNT:role/AppRunnerECRAccess\"
    },
    \"AutoDeploymentsEnabled\": false
  }" \
  --instance-configuration "{
    \"Cpu\": \"1024\", \"Memory\": \"2048\",
    \"InstanceRoleArn\": \"arn:aws:iam::$AWS_ACCOUNT:role/RelFluencyAgentInstance\"
  }" \
  --health-check-configuration '{"Protocol":"HTTP","Path":"/health"}'
```

## 5. Verify (service takes ~3–5 min to go RUNNING)

```bash
aws apprunner list-services --query "ServiceSummaryList[?ServiceName=='relational-fluency-agent'].[Status,ServiceUrl]" --output table
export AGENT_URL=$(aws apprunner list-services \
  --query "ServiceSummaryList[?ServiceName=='relational-fluency-agent'].ServiceUrl" --output text)

curl https://$AGENT_URL/health
# expect: {"status":"ok","scenario":"S2A",...}

# full round-trip test (director + actor + auth):
curl -s https://$AGENT_URL/v1/chat/completions \
  -H "Authorization: Bearer $AGENT_KEY" -H "Content-Type: application/json" \
  -d '{"stream": false, "messages": [{"role":"user","content":"Hi Morgan, thanks for meeting. I want to talk about my compensation."}]}'
```

## 6. Wire ElevenLabs

Agent settings → LLM → **Custom LLM**:
- Server URL: `https://<ServiceUrl>/v1`
- API key: the `$AGENT_KEY` value from step 2
- First message: `Thanks for grabbing time — I've got a hard stop in twenty, but I wanted us to talk properly. What's on your mind?`
- Dynamic variables: `participant_name`, `company_name`

## Operations

```bash
# pause when not piloting (compute billing stops)
aws apprunner pause-service --service-arn <arn>
aws apprunner resume-service --service-arn <arn>

# deploy a new version: build+push a new SHA (step 1), then
aws apprunner update-service --service-arn <arn> --source-configuration "...ImageIdentifier: $REPO:$NEWSHA..."

# steering trail (App Runner application logs)
aws logs tail /aws/apprunner/relational-fluency-agent --follow --filter-pattern STEERING
```

## Known pilot-only caveats

- **SSE streaming may be buffered** by App Runner's proxy layer. Symptom: Morgan's
  replies arrive all-at-once with a longer pause instead of streaming. Acceptable for
  piloting the *policy*; if the latency bothers pilot users, that's the trigger to
  move to the Terraform/ALB stack (ALB passes SSE through cleanly).
- `*.awsapprunner.com` URL is fine for ElevenLabs; production gets `agent.<domain>`
  via the Terraform stack once the team answers the Route 53 question.
- Steering logs go to App Runner's log group, not `/ecs/...`; export before ratings.
