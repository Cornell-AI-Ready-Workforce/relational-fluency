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

  # Scheduling this key for deletion makes every object in the bucket and every
  # byte on the EFS filesystem permanently unreadable, the 21 pilot recordings
  # included. The data survives the key; nothing survives without it, and once
  # the waiting period elapses there is no recovery at any price. Refused at
  # plan time rather than discovered afterwards.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "study" {
  name          = "alias/${var.project}-study"
  target_key_id = aws_kms_key.study.key_id
}

# S3: study data (video/audio/transcripts/steering logs). Versioned + encrypted.
resource "aws_s3_bucket" "study_data" {
  bucket = "${var.project}-study-data"

  # This bucket already holds real pilot recordings. Terraform's state for this
  # stack is NOT in the account's state bucket, so whoever runs the first
  # `tofu apply` runs it from empty state against resources that already exist —
  # the situation in which a plan proposing to destroy and recreate a bucket is
  # most likely and least expected. prevent_destroy makes that a plan error
  # instead of a deletion.
  lifecycle {
    prevent_destroy = true
  }
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

# --- Retention ---
#
# The consent document promises participants a retention period in writing. Until
# this configuration existed that promise was enforced by nothing: the bucket
# had no lifecycle rule at all, so "we keep your recording for N and then delete
# it" was true only for as long as somebody remembered to delete things by hand
# — which, on a versioned bucket, has never once happened anywhere.
#
# THE PERIOD IS THE PI'S DECISION. On 2026-09-17 it was decided not to expire
# recordings at all, so the default is 0 and the rule below carries no
# `expiration` block. A positive number here makes S3 delete recordings on
# that schedule, so it is set deliberately, never guessed.
variable "study_data_retention_days" {
  description = "Days a study object is kept before S3 expires it. 0 (the default, decided by the PI on 2026-09-17) means no expiration rule: recordings are kept until somebody deletes them."
  type        = number
  default     = 0

  validation {
    condition     = var.study_data_retention_days >= 0
    error_message = "study_data_retention_days must be 0 (keep indefinitely) or a positive number of whole days."
  }
}

# VERSIONING CHANGES WHAT `expiration` MEANS, and this is the trap that makes a
# retention rule look finished while leaving every byte in place.
#
# aws_s3_bucket_versioning.study_data is Enabled. On a versioned bucket an
# `expiration` rule does NOT delete the object. It writes a delete marker and
# makes the current version noncurrent, where that version stays — stored,
# billed and fully readable by anyone with s3:GetObjectVersion — forever. A
# study that promised "deleted after N days" would be keeping all of it,
# and `aws s3 ls` would agree that the prefix was empty, because the plain LIST
# only sees current versions. So noncurrent versions are handled explicitly
# below, and this is the window for that second clock.
#
# This one DOES have a default, and the difference from the variable above is
# the point: it is not the retention period. It is an operational recovery window —
# how long a superseded copy stays available after something overwrites an
# object — and 30 days is a recovery window, not a promise made to a
# participant. See the worst-case note on the rules themselves.
variable "study_data_superseded_version_days" {
  description = "Days a superseded (noncurrent) object version survives after being replaced. An operational recovery window, not the retention period — see study_data_retention_days."
  type        = number
  default     = 30

  # Self-contained on purpose. The relationship between this window and the
  # retention period is checked as a precondition on the resource below, not
  # here: a `validation` block that reads another variable requires Terraform
  # 1.9 / OpenTofu 1.8, and versions.tf declares `required_version = ">= 1.6"`.
  # A rule that only works on a newer toolchain than the stack claims to support
  # fails as a confusing parse error on the older one, which is worse than the
  # check being fifty lines further down.
  validation {
    condition     = var.study_data_superseded_version_days >= 1
    error_message = "study_data_superseded_version_days must be at least 1 day."
  }
}

# WHAT THIS DOES AND DOES NOT GUARANTEE — read before applying.
#
# S3 can express "expire N days after the object was created" and "expire M days
# after a version became noncurrent". It cannot express "delete every version N
# days after the object first existed", which is what a retention promise
# actually means. So the true worst case for a byte in this bucket is
# retention_days + superseded_version_days: an object written on day 0,
# overwritten on day N-1, whose superseded copy then lives M more days. With the
# default M that is N+30. If the protocol's wording is strict enough that N+30
# is a deviation, set study_data_superseded_version_days to 1.
#
# SCOPE. Every rule that DELETES DATA is filtered to the two prefixes the task
# role can write (see the IAM policy in ecs.tf: encounters/*, steering-logs/*),
# rather than applied bucket-wide. An unfiltered expiration would also reach
# whatever an operator ever puts in this bucket by hand — an export, a scratch
# copy, a manual backup taken before a risky migration — and delete it on a
# schedule they did not know applied to them.
#
# The third rule is the exception and is deliberately bucket-wide, because it
# cannot delete data by construction: `expired_object_delete_marker` removes
# only delete markers that have no versions left underneath them. There is
# nothing for it to destroy, and confining it to two prefixes would leave the
# markers from a hand-deleted object sitting in every other prefix forever.
#
# THE PILOT RECORDINGS ARE IN SCOPE. The bucket holds 21 real pilot recordings
# under these prefixes. The day this applies, their clock is already running
# from their original upload date, and anything already older than
# study_data_retention_days is deleted on S3's first evaluation cycle — within
# 24-48 hours, with no further confirmation. That may be exactly right. It is
# not a decision an apply should make silently, so verify the ages first (see
# the handover commands) and get the answer from the PI, not from here.
resource "aws_s3_bucket_lifecycle_configuration" "study_data" {
  bucket = aws_s3_bucket.study_data.id

  # The versioning resource has to exist before the rules that depend on
  # versioned semantics, or a single apply can order them the other way and
  # create noncurrent-version rules against an unversioned bucket.
  depends_on = [aws_s3_bucket_versioning.study_data]

  lifecycle {
    # The two clocks in this file must point the same way. A superseded copy
    # that lingers longer than the retention period is the promise to the
    # participant being broken by a second timer nobody is looking at, and it
    # would be invisible: `aws s3 ls` shows current versions only, so the prefix
    # reads empty while every superseded recording is still there.
    precondition {
      condition     = var.study_data_retention_days == 0 || var.study_data_superseded_version_days <= var.study_data_retention_days
      error_message = "study_data_superseded_version_days must not exceed study_data_retention_days: a noncurrent version would then outlive the retention period the consent text promises."
    }
  }

  # Encounters: audio, transcripts, video, record.json.
  rule {
    id     = "study-records-retention"
    status = "Enabled"

    filter {
      prefix = "encounters/"
    }

    # No expiration when the retention period is 0: current recordings are
    # kept indefinitely and the rule only cleans up superseded versions, delete
    # markers and dead multipart uploads.
    dynamic "expiration" {
      for_each = var.study_data_retention_days > 0 ? [1] : []
      content {
        days = var.study_data_retention_days
      }
    }

    noncurrent_version_expiration {
      noncurrent_days = var.study_data_superseded_version_days
    }

    # A webcam PUT that dies mid-upload — a participant closing the tab, a
    # dropped connection at the end of an encounter — leaves multipart parts
    # that no LIST shows, that no expiration rule reaches, and that are billed
    # until somebody runs list-multipart-uploads, which nobody does.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Steering logs: the director's stage directions, one file per encounter.
  # Same clock as the encounter they describe — a steering log that outlived its
  # recording would be study data about a participant whose data was deleted.
  rule {
    id     = "steering-logs-retention"
    status = "Enabled"

    filter {
      prefix = "steering-logs/"
    }

    # No expiration when the retention period is 0: current recordings are
    # kept indefinitely and the rule only cleans up superseded versions, delete
    # markers and dead multipart uploads.
    dynamic "expiration" {
      for_each = var.study_data_retention_days > 0 ? [1] : []
      content {
        days = var.study_data_retention_days
      }
    }

    noncurrent_version_expiration {
      noncurrent_days = var.study_data_superseded_version_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Once every version under a delete marker has expired, the marker itself is
  # left behind: a zero-byte object that is invisible to `aws s3 ls`, counts
  # against every LIST page, and slowly makes listing the bucket slower and
  # more expensive for no stored data at all. It gets its own rule because S3
  # rejects expired_object_delete_marker in the same rule as a day count, and
  # an empty filter because a marker with nothing under it is not data — see
  # the SCOPE note above.
  rule {
    id     = "expired-delete-markers"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
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
      "https://${local.app_fqdn}",
      "http://127.0.0.1:8765",
      "http://localhost:8765",
    ]
    allowed_headers = ["*"]
    max_age_seconds = 3600
  }
}
