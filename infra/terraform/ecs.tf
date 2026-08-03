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
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.study_data.arn}/steering-logs/*"]
  }
  statement {
    actions   = ["kms:GenerateDataKey"]
    resources = [aws_kms_key.study.arn]
  }
}

resource "aws_iam_role_policy" "task_s3" {
  name   = "put-steering-logs"
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
    name      = "agent"
    image     = var.container_image != "" ? var.container_image : "${aws_ecr_repository.agent.repository_url}:bootstrap"
    essential = true
    portMappings = [{ containerPort = 8100, protocol = "tcp" }]
    environment = [
      { name = "SCENARIO_ID",    value = var.scenario_id },
      { name = "ACTOR_MODEL",    value = var.actor_model },
      { name = "DIRECTOR_MODEL", value = var.director_model },
      { name = "PORT",           value = "8100" },
    ]
    secrets = [
      { name = "ANTHROPIC_API_KEY", valueFrom = aws_secretsmanager_secret.anthropic_key.arn },
      { name = "AGENT_API_KEY",     valueFrom = aws_secretsmanager_secret.agent_api_key.arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.agent.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "agent"
      }
    }
  }])
}

resource "aws_ecs_service" "agent" {
  name            = "agent"
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
    container_name   = "agent"
    container_port   = 8100
  }

  # New task must be healthy before old one drains: no dropped mid-encounter turns
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  lifecycle {
    ignore_changes = [desired_count] # allow manual scale-up during collection bursts
  }
}
