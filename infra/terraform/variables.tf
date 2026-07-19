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

variable "create_vpc_endpoints" {
  description = "Create ECR/logs interface endpoints so Fargate can pull images without a NAT (throwaway/no-egress footprints). Prod BYO networks with a working NAT leave this false."
  type        = bool
  default     = false
}

variable "endpoint_s3_route_table_ids" {
  description = "Route tables to attach an S3 gateway endpoint to (ECR layer blobs). Empty = skip (the VPC already has an S3 gateway, as in the validation footprint)."
  type        = list(string)
  default     = []
}

# ------------------------------------------------------------------ RDS (ADR-0004)

variable "rds_engine_version" {
  # RDS deprecates old minors over time (16.6 was pulled — min available is 16.9);
  # 16.14 is the current latest 16.x. Bump when RDS retires it.
  description = "PostgreSQL engine version (plain PG — no extensions required, ADR-0003)."
  type        = string
  default     = "16.14"
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

# --- Throwaway-validation knobs: production-safe defaults; test footprint flips them so
# --- `terraform destroy` removes 100% (no deletion-protection block, no surviving snapshot,
# --- no non-empty bucket/repo). Set in the gitignored terraform.tfvars for the test.
variable "rds_deletion_protection" {
  description = "RDS deletion protection. Prod-safe default true; false for throwaway footprints."
  type        = bool
  default     = true
}
variable "rds_skip_final_snapshot" {
  description = "Skip the RDS final snapshot on destroy. Prod default false; true for throwaway footprints."
  type        = bool
  default     = false
}
variable "s3_force_destroy" {
  description = "Let terraform empty + delete the artifacts bucket on destroy. Prod false; true for throwaway."
  type        = bool
  default     = false
}
variable "ecr_force_delete" {
  description = "Let terraform delete the ECR repo even with images present. Prod false; true for throwaway."
  type        = bool
  default     = false
}
