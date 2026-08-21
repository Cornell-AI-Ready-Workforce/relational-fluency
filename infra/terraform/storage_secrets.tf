# ECR for the agent image
resource "aws_ecr_repository" "platform" {
  name                 = "${var.project}/platform"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "agent" {
  name                 = "${var.project}/agent"
  image_tag_mutability = "IMMUTABLE" # release-SHA images; audit which version ran

  image_scanning_configuration {
    scan_on_push = true
  }
}

# KMS key for study data
resource "aws_kms_key" "study" {
  description         = "${var.project} study data (recordings, transcripts, logs)"
  enable_key_rotation = true
}

resource "aws_kms_alias" "study" {
  name          = "alias/${var.project}-study"
  target_key_id = aws_kms_key.study.key_id
}

# S3: study data (video/audio/transcripts/steering logs). Versioned + encrypted.
resource "aws_s3_bucket" "study_data" {
  bucket = "${var.project}-study-data"
}

resource "aws_s3_bucket_versioning" "study_data" {
  bucket = aws_s3_bucket.study_data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "study_data" {
  bucket = aws_s3_bucket.study_data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.study.arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "study_data" {
  bucket                  = aws_s3_bucket.study_data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Secrets: values are set OUT OF BAND (aws secretsmanager put-secret-value),
# never in Terraform state or git.
resource "aws_secretsmanager_secret" "anthropic_key" {
  name = "${var.project}/anthropic-api-key"
}

resource "aws_secretsmanager_secret" "agent_api_key" {
  name = "${var.project}/agent-api-key" # shared bearer token with ElevenLabs
}

# Browser-direct webcam upload (IRB 6a: video goes straight to storage and
# never transits the model path). Presigned PUTs come from the app origins.
resource "aws_s3_bucket_cors_configuration" "study_data" {
  bucket = aws_s3_bucket.study_data.id

  cors_rule {
    allowed_methods = ["PUT"]
    allowed_origins = [
      "https://rf.ai-ready-workforce.ai.cornell.edu",
      "http://127.0.0.1:8765",
      "http://localhost:8765",
    ]
    allowed_headers = ["*"]
    max_age_seconds = 3600
  }
}
