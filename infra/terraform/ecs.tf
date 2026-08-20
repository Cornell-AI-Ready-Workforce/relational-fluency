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
      { name = "ACTOR_MODEL", value = var.actor_model },
      { name = "DIRECTOR_MODEL", value = var.director_model },
      { name = "LLM_BASE_URL", value = var.llm_base_url },
      { name = "APP_HOST", value = local.app_fqdn },
      { name = "API_HOST", value = local.api_fqdn },
      { name = "S3_BUCKET", value = aws_s3_bucket.study_data.bucket },
      { name = "AWS_REGION", value = var.region },
      { name = "HOST", value = "0.0.0.0" },
      { name = "PORT", value = "8080" },
    ]
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

  # New task must be healthy before old one drains: no dropped mid-encounter turns
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  lifecycle {
    ignore_changes = [desired_count] # allow manual scale-up during collection bursts
  }
}
