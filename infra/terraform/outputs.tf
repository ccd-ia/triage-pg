# Outputs are exactly the cloud-profile-spec §5 environment variables — wiring the operator's
# direnv (or the dashboard host) is copy-paste from `terraform output`.

output "AWS_REGION" {
  value = var.region
}

output "TRIAGE_RDS_HOST" {
  value = aws_db_instance.triage.address
}

output "TRIAGE_RDS_PORT" {
  value = "5432"
}

output "TRIAGE_RDS_DB" {
  description = "The registry control-plane database (per-project DBs via `triage project create`)."
  value       = aws_db_instance.triage.db_name
}

output "TRIAGE_RDS_USER" {
  description = "Per-project IAM users (triage_<slug>) are the day-to-day identities; this is the bootstrap master."
  value       = var.rds_master_username
}

output "TRIAGE_S3_BUCKET" {
  value = aws_s3_bucket.artifacts.bucket
}

output "TRIAGE_BATCH_QUEUE" {
  value = aws_batch_job_queue.triage.name
}

output "TRIAGE_BATCH_JOB_DEF" {
  value = aws_batch_job_definition.triage.name
}

output "ecr_repository_url" {
  description = "Push the triage-pg image here (docs/cloud-runbook.md — build needs --ssh default)."
  value       = aws_ecr_repository.triage.repository_url
}

output "rds_master_secret_arn" {
  description = "The RDS-managed master password (Secrets Manager) — bootstrap only, never app config."
  value       = try(aws_db_instance.triage.master_user_secret[0].secret_arn, null)
}
