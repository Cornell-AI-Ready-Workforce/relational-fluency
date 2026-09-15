terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # Shared state, so more than one person can deploy. The bucket and lock
  # table are created by infra/scripts/add-deployer.sh; migrate an existing
  # local state with `tofu init -migrate-state`.
  backend "s3" {
    bucket         = "relational-fluency-tfstate-540586745717"
    key            = "platform/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "relational-fluency-tflock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Study     = "IRB0151104"
    }
  }
}
