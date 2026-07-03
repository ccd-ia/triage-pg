# triage-pg cloud profile — the full AWS footprint (ADRs 0003–0005, cloud-profile-spec §4/§5).
#
# GATED: authoring this module is part of the v1-completion plan; `terraform apply` runs only
# when the maintainer opens the cloud gate (account ready, cost accepted). Everything here is
# offline-verifiable with `terraform validate` / `fmt -check`.
#
# Deliberately out of Terraform's hands:
#   * per-project databases + PG roles — created by `triage project create` and the runbook's
#     role bootstrap (ADR-0002/0004), never by IaC;
#   * the container image build/push — the featurizer git+ssh dependency needs `--ssh default`
#     at build time (docs/cloud-runbook.md);
#   * remote state — configure a backend per deployment (documented in the runbook), not here.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = "triage-pg"
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
