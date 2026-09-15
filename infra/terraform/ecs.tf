resource "aws_ecs_cluster" "main" {
  name = var.project

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "agent" {
  name              = "/ecs/${var.project}/agent"
  retention_in_days = 90 # steering trail lives here; keep past each study wave
}

# --- IAM ---
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "${var.project}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.anthropic_key.arn,
      aws_secretsmanager_secret.agent_api_key.arn,
    ]
  }
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  name   = "read-secrets"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

resource "aws_iam_role" "task" {
  name               = "${var.project}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "task_s3" {
  # One aligned record per encounter: audio, transcript, steering log, video.
  # Write plus read-back, because the researcher view and the offline scorer
  # both read encounters the task itself wrote.
  statement {
    actions = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"]
    resources = [
      "${aws_s3_bucket.study_data.arn}/encounters/*",
      "${aws_s3_bucket.study_data.arn}/steering-logs/*",
    ]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.study_data.arn]
  }
  # Presigned webcam uploads are signed by the task but executed by the browser.
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.study.arn]
  }
}

resource "aws_iam_role_policy" "task_s3" {
  name   = "study-data-access"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_s3.json
}

# --- Persistent /data volume (EFS) ---
# The server writes every study record to DATA_DIR=/data: sessions/transcripts,
# run assignments, the sqlite index, and Qualtrics exports. Fargate's container
# filesystem does not survive a deploy or task retirement, so /data must be an
# EFS mount or the release flow destroys all records since the last export.
resource "aws_security_group" "efs" {
  name_prefix = "${var.project}-efs-"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "NFS from the platform task only"
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.agent.id]
  }

  # No egress block, deliberately. Declaring an aws_security_group with no
  # egress rules removes the all-traffic default AWS would otherwise attach, and
  # nothing breaks: security groups are stateful, so the mount target's replies
  # to an established NFS connection are allowed regardless. A mount target ENI
  # that could originate outbound traffic is a filesystem with a network path
  # out of the VPC, which is not a thing study recordings need.
  #
  # Note also that the ingress rule is by SECURITY GROUP and not by CIDR. Port
  # 2049 is an unauthenticated filesystem to whatever can route to it; the
  # private subnet CIDRs would technically work and would also admit every
  # future workload placed in them.
}

# The volume mounts with IAM authorization enabled, so the task role needs an
# explicit grant to mount and write through the access point. Without it the
# task cannot mount /data at all: new tasks never go healthy and, with
# minimum_healthy_percent = 100, every deploy sticks while the old task keeps
# serving.
data "aws_iam_policy_document" "task_efs" {
  statement {
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
    ]
    resources = [aws_efs_file_system.study.arn]

    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.study.arn]
    }
  }
}

resource "aws_iam_role_policy" "task_efs" {
  name   = "${var.project}-task-efs"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_efs.json
}

resource "aws_efs_file_system" "study" {
  creation_token = "${var.project}-study-data"

  # Encrypted at rest under the study's own customer-managed KMS key, not the
  # AWS-managed EFS default. Same key as the S3 bucket, so "the study data is
  # under one key we control, and rotation and revocation are one action" is a
  # true sentence rather than a true-of-S3-only one. The protocol says the
  # recordings are encrypted at rest; this is the line that makes it so for the
  # copy that lives on disk beside the transcripts.
  encrypted  = true
  kms_key_id = aws_kms_key.study.arn

  # The default (`bursting`) earns throughput in proportion to stored bytes,
  # and this filesystem stores very little: a few MB of JSON and WAV per
  # participant. A collection session with several concurrent encounters spends
  # the credit balance and EFS then throttles to the baseline, at which point
  # writes get slow rather than failed — nothing logs, nothing alarms, and the
  # operator sees encounters that take longer and complete. `elastic` has no
  # credit balance to exhaust and is metered per byte moved, which at this
  # volume is pennies. Do not set this back to bursting to save them.
  throughput_mode = "elastic"

  # The study's only copy of every run file, participant record, transcript,
  # WAV and the SQLite index lives here — S3 holds webcam video and nothing
  # else. A destroy, or any change that forces replacement, is not recoverable
  # by re-running an apply, because the participants cannot be re-run. This
  # turns that into a plan-time error instead of an outcome.
  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name = "${var.project}-study-data"
  }
}

# EFS automatic backups are ON for a filesystem created in the console and OFF
# for one created through the API — which is what Terraform uses. So the state
# this stack would have shipped is: one copy of the entire study, no snapshots,
# on a filesystem an export script with a wrong path can empty. AWS Backup's
# default EFS plan (daily, 35-day retention) is enough to survive that, and
# costs about what one participant's video does.
resource "aws_efs_backup_policy" "study" {
  file_system_id = aws_efs_file_system.study.id

  backup_policy {
    status = "ENABLED"
  }
}

# WHY count AND NOT for_each. This resource read
#
#     for_each = toset(module.vpc.private_subnets)
#
# and that cannot be planned. Subnet IDs do not exist until the VPC is created,
# and Terraform refuses to build a plan whose resource *addresses* depend on
# apply-time values: "Invalid for_each argument ... depends on resource
# attributes that cannot be determined until apply". On a fresh apply — and a
# fresh apply is the only kind this stack has ever had, since its state is not
# in the account's state bucket — that is a hard stop before anything is
# created, which is a large part of why this whole EFS section exists in code
# and in no deployed revision.
#
# `count` is fine where `for_each` is not: the NUMBER of private subnets is
# known from network.tf's literal two-CIDR list even though the IDs are not, so
# the addresses (`[0]`, `[1]`) are determined at plan time. The cost of count is
# that inserting a subnet in the middle of that list renumbers the rest; with a
# fixed two-AZ layout that is not a trade, it is a non-issue.
#
# One mount target per AZ the service runs tasks in. A task placed in an AZ
# with no mount target cannot reach the filesystem at all, and fails at mount —
# it never becomes healthy, so the symptom is a deploy that hangs rather than
# data loss. Keep this list and the service's network_configuration.subnets the
# same expression.
resource "aws_efs_mount_target" "study" {
  count           = length(module.vpc.private_subnets)
  file_system_id  = aws_efs_file_system.study.id
  subnet_id       = module.vpc.private_subnets[count.index]
  security_groups = [aws_security_group.efs.id]
}

# The container runs as a non-root user (uid 1000, see Dockerfile). A raw EFS
# root is owned root:root, so a non-root process would get EACCES writing study
# records to the mounted /data. This access point owns its root directory as
# 1000:1000 and forces all access to that uid/gid, so the app can actually write.
resource "aws_efs_access_point" "study" {
  file_system_id = aws_efs_file_system.study.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/study-data"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0755"
    }
  }

  tags = {
    Name = "${var.project}-study-data-ap"
  }
}

# --- The consent version the survey is showing ---
#
# Declared here, beside its one use, and deliberately WITHOUT a default, which
# is the whole point of it: `tofu apply` stops and asks rather than deploying a
# task that comes up healthy and records nothing. server/storage.py refuses to
# write any study consent until this names the approved Qualtrics wording, so a
# deployment missing it answers /health 200, mints a run for every arrival,
# closes every voice socket 4403 and collects an empty dataset — uniformly, from
# the first participant onward, with no symptom on any surface an operator
# watches. That failure cost is not one a convenient default can be worth: a
# default would be a version string this repository invented, stamped on real
# participants' records as the document they agreed to.
#
# Set it in infra/terraform/terraform.tfvars (committed, like container_image —
# it is not a secret, and the running wave's consent version belonging to git
# history is a feature), or pass -var upstream_consent_version=... for a one-off.
variable "upstream_consent_version" {
  description = "Version string of the consent block the Qualtrics survey is currently showing, e.g. cornell-irb-2026-09-v3. Recorded as consent_text_version on every participant. No default on purpose: an apply that has not been told must fail, not deploy."
  type        = string

  validation {
    # Empty and placeholder are the two ways this gets "set" without being
    # answered, and the app treats both as unset (storage.is_placeholder_value).
    # Caught at plan time instead, where it costs an apply rather than a wave.
    #
    # THIS REGEX IS NOT WRITTEN HERE. It is a copy of the string returned by
    # server/storage.py's consent_version_rule_pattern(), with every backslash
    # doubled for HCL, and tests/test_required_deployment_env.py fails — with
    # the exact string to paste — the moment the two stop agreeing. It used to
    # be an independent rule that happened to look similar, and the two drifted
    # the ONE way that costs a wave rather than an apply: `tofu plan` accepted
    # "xxx", the apply succeeded, and the app then treated the value as unset,
    # so the task came up healthy, minted a run per arrival and recorded
    # nothing. Do not edit this line by hand; change the Python and paste what
    # the test prints. Terraform cannot import it, so agreement is tested.
    #
    # Both sides match the trimmed value, case-insensitively.
    condition = (
      trimspace(var.upstream_consent_version) != "" &&
      !can(regex("(?i)fill[ _-]?in|TBD|TODO|XXX|placeholder|change[ _-]?me|^[\\[<{]|[\\]>}]$|^(?:\\-|\\-\\-|\\.|0|\\?|asdf|bar|foo|n\\.a\\.|n/a|na|nan|nil|none|null|tba|tbc|unknown)$",
      trimspace(var.upstream_consent_version)))
    )
    error_message = "upstream_consent_version must name the approved consent wording the Qualtrics survey shows (e.g. cornell-irb-2026-09-v3). Blank, a template marker (FILL IN, TODO, xxx, changeme, anything in brackets) or a word that admits to having no answer (unknown, none, n/a) means the task records no consent at all, and therefore no encounters. This is the same rule server/storage.py applies at runtime; it is refused here so it costs an apply instead of a wave."
  }
}

# WHICH STUDY DESIGN THE DEPLOYED SERVICE RUNS, and the language the transcript
# is taken in. Both arrived with origin/main cabc1dd as code defaults and had no
# way through the task definition at all until 2026-09-15, which meant
# docs/OPERATIONS.md told an operator to "set it to B to pin the other form or
# random for a per-construct coin flip" against a deployment where there was
# nothing to set. The defaults below are exactly the code defaults, so declaring
# them changes nothing about what runs; it makes them reachable.
variable "default_run_variant" {
  description = "A (Phase 1: pin S1A/S2A/S3A/S4A, construct order counterbalanced), B (pin the other form), or random (the per-slot draw over twelve forms with FORM_EXCLUSIONS applied). A pinned form takes the exclusion table's caller hatch, so A switches the S1A/Teamwork exclusion off study-wide — deliberate, and the PI's call."
  type        = string
  default     = "A"

  validation {
    condition     = contains(["A", "B", "random"], var.default_run_variant)
    error_message = "default_run_variant must be A, B or random. Anything else is read by server/runs.py as a variant letter and refused at the entry link, which turns participants away at /start rather than failing here."
  }
}

variable "transcription_lang" {
  description = "Language every realtime session is told the conversation is in (ISO code, e.g. en). Rides on whatever input transcription the model family already asks for, tells the actor to speak it whatever it thinks it heard, and names it in the scribe's brief. Empty string sends no hint at all."
  type        = string
  default     = "en"
}

# --- Task definition & service ---
resource "aws_ecs_task_definition" "agent" {
  family                   = "${var.project}-agent"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name         = "platform"
    image        = var.container_image != "" ? var.container_image : "${aws_ecr_repository.platform.repository_url}:bootstrap"
    essential    = true
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    environment = [
      # The live realtime path reads REALTIME_MODEL, not ACTOR_MODEL.
      # Switching the live model (e.g. to the gpt-realtime-2.1 fallback for the
      # Gemini live deprecation) is: set actor_model in terraform.tfvars, apply.
      # Whatever you set must have a row in server/voice/realtime.py
      # REALTIME_FAMILIES, or require_capabilities() resolves it into the wrong
      # family and the route misbehaves silently (16 kHz into native-audio is
      # permanent silence — see docs/migration-plan.md).
      { name = "REALTIME_MODEL", value = var.actor_model },
      { name = "DIRECTOR_MODEL", value = var.director_model },
      # CLAUDE_MODEL is NOT a provenance-only label. Three readers take it:
      # server/engine.py:26 and server/claude_engine.py:20 as DEFAULT_MODEL (the
      # text-mode engine), server/app.py:670 as `sc.model or DEFAULT_MODEL` (the
      # default in the researcher's pre-start model picker), and
      # server/llm.py:117 as provenance.text_model, which lands in record.json.
      #
      # It was left unset once, so provenance reported llm.py's hardcoded
      # default. The repair pinned it to var.director_model, which fixed the
      # record by moving the engine: a text-mode encounter and the picker's
      # default silently followed the director instead of the model the
      # researcher configured. Both halves of that were wrong for the same
      # reason — one variable cannot describe two jobs.
      #
      # So they are two variables now, and both statements are true at once.
      # The director's model is recorded on its own path: every stage_direction
      # event carries director_model straight off the live Director
      # (server/realtime_voice_session.py:750, :2498, :2574, :2745), so nothing
      # about the director depends on this line. That frees provenance's single
      # text_model field to mean what its readers assume — the text engine that
      # this deployment would actually run — which is exactly var.text_model,
      # whose default is the same nto.gemini-3.1-flash-lite the code and
      # .env.example already use. Set the two independently; do not re-pin them.
      { name = "CLAUDE_MODEL", value = var.text_model },
      { name = "LLM_BASE_URL", value = var.llm_base_url },
      { name = "APP_HOST", value = local.app_fqdn },
      { name = "API_HOST", value = local.api_fqdn },
      { name = "S3_BUCKET", value = aws_s3_bucket.study_data.bucket },
      { name = "AWS_REGION", value = var.region },
      { name = "SURVEY_RETURN_URL", value = var.survey_return_url },
      # Which approved consent wording the Qualtrics survey is showing. Without
      # it server/storage.py records no study consent, so the task serves a
      # green /health, opens a run for every arrival and captures nothing —
      # see the variable above, which has no default so an apply cannot skip it.
      { name = "UPSTREAM_CONSENT_VERSION", value = var.upstream_consent_version },
      # The study design and the transcript language. See the two variables
      # above: both are read by server/ and neither had an entry here, so the
      # deployed service ran on code defaults an operator could not change and
      # OPERATIONS.md described a switch that was not wired to anything.
      { name = "DEFAULT_RUN_VARIANT", value = var.default_run_variant },
      { name = "TRANSCRIPTION_LANG", value = var.transcription_lang },
      # The EFS volume below mounts at /data, but the only thing that made the
      # app WRITE there was ENV DATA_DIR=/data in the Dockerfile — nothing in
      # this file. server/storage.py:43 falls back to <repo>/data when DATA_DIR
      # is unset, which on Fargate is the container filesystem: the app would
      # come up healthy, serve encounters, and write every session, run and
      # rating to a disk that the next deploy destroys, with the EFS mount
      # sitting empty beside it and nothing saying so. State it here so the
      # mount and the write path are one declaration in one file.
      { name = "DATA_DIR", value = "/data" },
      { name = "HOST", value = "0.0.0.0" },
      { name = "PORT", value = "8080" },
    ]
    mountPoints = [{
      sourceVolume  = "study-data"
      containerPath = "/data"
      readOnly      = false
    }]
    # Fargate hard-caps stopTimeout at 120s, so a draining task is SIGKILLed 120s
    # after SIGTERM regardless of the ALB deregistration_delay — this does NOT let
    # a multi-minute voice encounter finish. A deploy rolled mid-encounter will
    # drop that session at the 120s mark. Deploy only between collection sessions,
    # or add a drain step that waits for active sessions to end first.
    stopTimeout = 120
    secrets = [
      # Cornell LiteLLM virtual key — serves both the realtime actor and the director.
      { name = "ANTHROPIC_API_KEY", valueFrom = aws_secretsmanager_secret.anthropic_key.arn },
      { name = "SESSION_KEY", valueFrom = aws_secretsmanager_secret.agent_api_key.arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.agent.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "platform"
      }
    }
  }])

  volume {
    name = "study-data"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.study.id
      transit_encryption = "ENABLED"
      # Mount through the access point so the non-root container (uid 1000) owns
      # /data and can write; without this the EFS root is root-owned and writes
      # fail with EACCES on Fargate.
      authorization_config {
        access_point_id = aws_efs_access_point.study.id
        iam             = "ENABLED"
      }
    }
  }
}

resource "aws_ecs_service" "agent" {
  name            = "platform"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.agent.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = module.vpc.private_subnets
    security_groups  = [aws_security_group.agent.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.agent.arn
    container_name   = "platform"
    container_port   = 8080
  }

  # New task must be healthy before the old one drains, so NEW connections never
  # hit a cold task. In-flight sessions on the draining task are still cut at the
  # 120s Fargate stopTimeout (see stopTimeout above) — deploy between sessions.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  lifecycle {
    ignore_changes = [desired_count] # allow manual scale-up during collection bursts
  }
}
