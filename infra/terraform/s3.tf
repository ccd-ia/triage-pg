# The artifact bucket (cloud-profile-spec §3): Parquet matrices + joblib models under
# per-scope prefixes, plus the config staging area the Batch submit writes (§4).
# Versioned; only the staging/ prefix expires (artifacts are GC'd by `triage gc`, ADR-0017 —
# a lifecycle rule deleting artifacts would silently break cache-hit rebuild guarantees).

resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.name_prefix}-artifacts-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-staged-configs"
    status = "Enabled"
    filter {
      prefix = "staging/"
    }
    expiration {
      days = 30
    }
  }
}
