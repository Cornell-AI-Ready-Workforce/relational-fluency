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
  encrypted      = true
  kms_key_id     = aws_kms_key.study.arn

  tags = {
    Name = "${var.project}-study-data"
  }
}

resource "aws_efs_mount_target" "study" {
  for_each        = toset(module.vpc.private_subnets)
  file_system_id  = aws_efs_file_system.study.id
  subnet_id       = each.value
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
      { name = "REALTIME_MODEL", value = var.actor_model },
      { name = "DIRECTOR_MODEL", value = var.director_model },
      { name = "LLM_BASE_URL", value = var.llm_base_url },
      { name = "APP_HOST", value = local.app_fqdn },
      { name = "API_HOST", value = local.api_fqdn },
      { name = "S3_BUCKET", value = aws_s3_bucket.study_data.bucket },
      { name = "AWS_REGION", value = var.region },
      { name = "SURVEY_RETURN_URL", value = var.survey_return_url },
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
