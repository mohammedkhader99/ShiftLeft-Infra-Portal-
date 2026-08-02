# The request's real AWS resource. Provisions exactly one thing, branching on
# var.resource_kind:
#   - aws-bucket : a private, versioned S3 bucket (mirrors the OCI bucket).

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0.0"
    }
  }
}

# Credentials come from the standard AWS env vars (AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY) supplied by the orchestrator — never held in code or git.
provider "aws" {
  region = var.region
}

# --- Object storage (resource_kind = aws-bucket) -----------------------------

resource "aws_s3_bucket" "env" {
  count  = var.resource_kind == "aws-bucket" ? 1 : 0
  bucket = var.bucket_name
  tags   = var.tags
}

# Retain prior object versions (recovery from overwrite; clears the scanner's
# versioning-disabled finding).
resource "aws_s3_bucket_versioning" "env" {
  count  = var.resource_kind == "aws-bucket" ? 1 : 0
  bucket = aws_s3_bucket.env[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

# Private-only: block all public access (mirrors the OCI bucket's no-public
# posture; clears the scanner's public-access finding).
resource "aws_s3_bucket_public_access_block" "env" {
  count                   = var.resource_kind == "aws-bucket" ? 1 : 0
  bucket                  = aws_s3_bucket.env[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Customer-managed encryption (SSE-KMS) when a key is configured (AWS_KMS_KEY_ARN),
# otherwise AWS-managed (SSE-S3). A CMK clears the scanner's no-CMK finding for
# restricted/confidential data.
resource "aws_s3_bucket_server_side_encryption_configuration" "env" {
  count  = var.resource_kind == "aws-bucket" ? 1 : 0
  bucket = aws_s3_bucket.env[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.kms_key_arn != "" ? "aws:kms" : "AES256"
      kms_master_key_id = var.kms_key_arn != "" ? var.kms_key_arn : null
    }
  }
}

output "bucket" {
  value = try(aws_s3_bucket.env[0].id, null)
}
