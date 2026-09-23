# Analysis database: the schema in docs/db-schema.sql, hosted as RDS Postgres so
# the team queries one shared copy of the study data instead of each analyst
# loading the S3 archive on a laptop. Decided 2026-09-23; Tanvi is the
# administrator (deployer user tanvi-cli).
#
# It holds a COPY. The encounter archive in S3 stays the record; this instance
# is filled from it by tools/load_analysis_db.py and can be dropped and rebuilt
# from the bucket at any time. That is why a small single-AZ instance with
# seven days of backups is enough.
#
# Reachability. The instance sits in the PUBLIC subnets with a public address,
# and the security group is what decides who can connect: nothing outside the
# VPC until an address is listed in analysis_db_allowed_cidrs (terraform.tfvars),
# plus the platform's own tasks, for a future scheduled load. Every connection
# must use TLS (rds.force_ssl). The alternative — a private instance behind a
# bastion or SSM tunnel — costs an EC2 instance and a runbook for every analyst;
# a CIDR allowlist that the administrator edits is the right size for a
# research team of four.
#
# Credentials. RDS manages the master password itself and keeps it in Secrets
# Manager (rds!db-...): nobody types it into Terraform, git, or the console.
# docs/db-roles.sql then creates the two roles people actually use — rf_loader
# (writes) and rf_analyst (reads everything except participant_identity).

variable "analysis_db_enabled" {
  description = "Create the analysis Postgres instance (docs/db-schema.md)."
  type        = bool
  default     = true
}

variable "analysis_db_instance_class" {
  type    = string
  default = "db.t4g.micro" # ~$13/month + 20 GB gp3 (~$2.30)
}

variable "analysis_db_allowed_cidrs" {
  description = "Addresses allowed to connect on 5432 (analysts' IPs as /32s, or a campus/VPN range). Empty = only the platform tasks."
  type        = list(string)
  default     = []
}

resource "aws_db_subnet_group" "analysis" {
  count      = var.analysis_db_enabled ? 1 : 0
  name       = "${var.project}-analysis"
  subnet_ids = module.vpc.public_subnets
  tags       = { project = var.project }
}

resource "aws_security_group" "analysis_db" {
  count       = var.analysis_db_enabled ? 1 : 0
  name_prefix = "${var.project}-analysis-db-"
  description = "Analysis Postgres: allowlisted analysts and the platform tasks"
  vpc_id      = module.vpc.vpc_id

  dynamic "ingress" {
    for_each = length(var.analysis_db_allowed_cidrs) > 0 ? [1] : []
    content {
      description = "Postgres from allowlisted analysts (TLS enforced by the parameter group)"
      from_port   = 5432
      to_port     = 5432
      protocol    = "tcp"
      cidr_blocks = var.analysis_db_allowed_cidrs
    }
  }
  ingress {
    description     = "Postgres from the platform tasks (scheduled loader)"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.agent.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { project = var.project }
}

resource "aws_db_parameter_group" "analysis" {
  count  = var.analysis_db_enabled ? 1 : 0
  name   = "${var.project}-analysis-pg16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  tags = { project = var.project }
}

resource "aws_db_instance" "analysis" {
  count      = var.analysis_db_enabled ? 1 : 0
  identifier = "${var.project}-analysis"

  engine         = "postgres"
  engine_version = "16"
  instance_class = var.analysis_db_instance_class

  allocated_storage     = 20
  max_allocated_storage = 100
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name                     = "rf"
  username                    = "rf_admin"
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.analysis[0].name
  vpc_security_group_ids = [aws_security_group.analysis_db[0].id]
  parameter_group_name   = aws_db_parameter_group.analysis[0].name
  publicly_accessible    = true
  multi_az               = false

  backup_retention_period    = 7
  backup_window              = "07:00-08:00" # 03:00-04:00 Eastern
  maintenance_window         = "Sun:08:00-Sun:09:00"
  deletion_protection        = true
  skip_final_snapshot        = false
  final_snapshot_identifier  = "${var.project}-analysis-final"
  copy_tags_to_snapshot      = true
  auto_minor_version_upgrade = true
  apply_immediately          = true

  performance_insights_enabled = false

  tags = {
    project = var.project
    role    = "analysis-db"
    owner   = "tanvi-cli"
  }
}

output "analysis_db_endpoint" {
  description = "host:port of the analysis Postgres (database rf)"
  value       = var.analysis_db_enabled ? aws_db_instance.analysis[0].endpoint : null
}

output "analysis_db_master_secret_arn" {
  description = "Secrets Manager secret holding rf_admin's password (RDS-managed)"
  value       = var.analysis_db_enabled ? aws_db_instance.analysis[0].master_user_secret[0].secret_arn : null
}
