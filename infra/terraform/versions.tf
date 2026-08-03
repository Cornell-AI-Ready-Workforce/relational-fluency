terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # After first apply, migrate state to the created S3 bucket:
  # terraform init -migrate-state  (uncomment and fill in)
  # backend "s3" {
  #   bucket = "<project>-tfstate"
  #   key    = "agent-platform/terraform.tfstate"
  #   region = "us-east-1"
  # }
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
