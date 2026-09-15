#!/usr/bin/env bash
# Give a second person deploy access to the Relational Fluency platform.
#
# Creates (idempotently):
#   1. an IAM user with PowerUserAccess plus the narrow IAM rights Terraform
#      needs for this project (read IAM, pass the two task roles to ECS,
#      manage only relational-fluency-* roles);
#   2. a private, versioned S3 bucket and a DynamoDB lock table for SHARED
#      Terraform state, so two people cannot deploy from two diverging
#      local state files.
#
# It does NOT create the access key: run the printed command yourself so the
# secret is shown once, in your terminal, and hand it over on a secure channel.
#
# Usage:  infra/scripts/add-deployer.sh <username>      e.g. add-deployer.sh ben-cli
set -euo pipefail

USER="${1:?usage: add-deployer.sh <iam-username>}"
ACCT="540586745717"
REGION="us-east-1"
STATE_BUCKET="relational-fluency-tfstate-${ACCT}"
LOCK_TABLE="relational-fluency-tflock"

echo "== IAM user: ${USER}"
aws iam get-user --user-name "$USER" >/dev/null 2>&1 \
  || aws iam create-user --user-name "$USER" \
       --tags Key=project,Value=relational-fluency Key=role,Value=deployer >/dev/null
aws iam attach-user-policy --user-name "$USER" \
  --policy-arn arn:aws:iam::aws:policy/PowerUserAccess

POLICY=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "ReadIamForTerraformPlan", "Effect": "Allow",
     "Action": ["iam:Get*", "iam:List*"], "Resource": "*"},
    {"Sid": "PassProjectRolesToEcs", "Effect": "Allow", "Action": "iam:PassRole",
     "Resource": ["arn:aws:iam::${ACCT}:role/relational-fluency-task",
                  "arn:aws:iam::${ACCT}:role/relational-fluency-task-execution"],
     "Condition": {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}}},
    {"Sid": "ManageOnlyProjectRoles", "Effect": "Allow",
     "Action": ["iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:AttachRolePolicy",
                "iam:DetachRolePolicy", "iam:UpdateAssumeRolePolicy", "iam:TagRole"],
     "Resource": "arn:aws:iam::${ACCT}:role/relational-fluency-*"}
  ]
}
JSON
)
aws iam put-user-policy --user-name "$USER" \
  --policy-name relational-fluency-deployer --policy-document "$POLICY"
echo "   PowerUserAccess + relational-fluency-deployer attached"

echo "== Shared Terraform state: s3://${STATE_BUCKET}"
if ! aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
  aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$REGION" >/dev/null
fi
aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$STATE_BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket "$STATE_BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws dynamodb describe-table --table-name "$LOCK_TABLE" >/dev/null 2>&1 \
  || aws dynamodb create-table --table-name "$LOCK_TABLE" \
       --attribute-definitions AttributeName=LockID,AttributeType=S \
       --key-schema AttributeName=LockID,KeyType=HASH \
       --billing-mode PAY_PER_REQUEST >/dev/null
echo "   bucket versioned, encrypted, private; lock table ${LOCK_TABLE} ready"

cat <<EOF

== Next steps (you)
1. Move your local Terraform state into the shared bucket (one time, from
   infra/terraform; answers 'yes' to copy the existing state):
     cd $(cd "$(dirname "$0")/../terraform" && pwd) && tofu init -migrate-state

2. Mint the access key for ${USER} (shown ONCE; send it on a secure channel):
     aws iam create-access-key --user-name ${USER}

== Next steps (${USER})
   aws configure            # paste the key; region us-east-1
   git clone git@github.com:Cornell-AI-Ready-Workforce/relational-fluency.git
   cd relational-fluency/infra/terraform && tofu init && tofu plan
   Deploy steps: docs/OPERATIONS.md, section "Releasing a build".
EOF
