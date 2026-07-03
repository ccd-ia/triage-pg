variable "region" {
  description = "AWS region (becomes AWS_REGION for the app, cloud-profile-spec §5)."
  type        = string
}

variable "name_prefix" {
  description = "Resource name prefix (lets several environments share an account)."
  type        = string
  default     = "triage"
}

variable "vpc_id" {
  description = "VPC the RDS instance + Batch jobs live in (bring-your-own network)."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnets for RDS + Fargate (at least two AZs for the DB subnet group)."
  type        = list(string)
}

variable "operator_cidr_blocks" {
  description = "CIDRs allowed to reach PostgreSQL from outside the Batch jobs (e.g. an operator VPN). Empty = jobs only."
  type        = list(string)
  default     = []
}

# ------------------------------------------------------------------ RDS (ADR-0004)

variable "rds_engine_version" {
  description = "PostgreSQL engine version (plain PG — no extensions required, ADR-0003)."
  type        = string
  default     = "16.6"
}

variable "rds_instance_class" {
  description = "Instance class. Plain RDS over Aurora at teaching/consulting scale (plan Questionables)."
  type        = string
  default     = "db.t4g.small"
}

variable "rds_allocated_storage_gb" {
  description = "Storage (GiB). Matrices/models live on S3, so the DB stays small (predictions + evaluations)."
  type        = number
  default     = 50
}

variable "rds_master_username" {
  description = "Master username (bootstrap/maintenance only; day-to-day access is per-project IAM users, ADR-0004)."
  type        = string
  default     = "triage_admin"
}

# ------------------------------------------------------------------ Batch (ADR-0005)

variable "batch_max_vcpus" {
  description = "Compute-environment ceiling (cross-experiment parallelism = Batch's queue, ADR-0005)."
  type        = number
  default     = 8
}

variable "job_vcpus" {
  description = "vCPUs per experiment job. The grid runs SERIAL inside the container (ADR-0020) — size for one worker, not the grid."
  type        = string
  default     = "2"
}

variable "job_memory_mib" {
  description = "Memory per experiment job (matrices load in-process; size to the largest split)."
  type        = string
  default     = "8192"
}

variable "image_tag" {
  description = "The triage-pg image tag the job definition points at (pushed to the ECR repo this module creates)."
  type        = string
  default     = "latest"
}
