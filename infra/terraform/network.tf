# VPC: 2 AZs, public subnets (ALB) + private subnets (Fargate), single NAT.
# Matches the cost estimation doc (~$40-44/mo for NAT + IPv4).

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.13"

  name = var.project
  cidr = "10.40.0.0/16"

  azs             = ["${var.region}a", "${var.region}b"]
  public_subnets  = ["10.40.0.0/20", "10.40.16.0/20"]
  private_subnets = ["10.40.128.0/20", "10.40.144.0/20"]

  enable_nat_gateway      = true
  single_nat_gateway      = true
  enable_dns_hostnames    = true
  map_public_ip_on_launch = false
}

resource "aws_security_group" "alb" {
  name_prefix = "${var.project}-alb-"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description = "HTTPS from anywhere (ElevenLabs + participants)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTP (redirected to HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "agent" {
  name_prefix = "${var.project}-agent-"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "Agent port from ALB only"
    from_port       = 8100
    to_port         = 8100
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    description = "Outbound to Anthropic/LiteLLM via NAT"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
