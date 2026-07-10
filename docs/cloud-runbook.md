# Cloud runbook — from `terraform apply` to a live Batch experiment

The cloud profile (ADRs 0003–0005, `docs/cloud-profile-spec.md`) is code-complete and
moto-tested; this runbook is the **gated** live path: provision the footprint, bootstrap the
per-project roles, push the image, run one experiment as a Batch job, and observe it. Every
step is idempotent-or-explicit; nothing here writes a credential into a file or table
(ADR-0002/0004 — IAM tokens + the task role are the only credential surfaces).

## 0. Prerequisites

- An AWS account + credentials with rights to create RDS/S3/ECR/Batch/IAM (operator-side only;
  the app never holds them).
- A VPC with **two+ private subnets** (the module brings its own security groups, not a VPC).
- Docker. The featurizer dependency is a **public git+https pin** on ccd-ia/featurizer
  (v0.7.0+, ADR-0016) — no deploy key or ssh forwarding; CI builds the image too
  (the former operator-side-only blocker is gone).
- Terraform ≥ 1.6.

## 1. Provision (GATED — costs real money)

```bash
cd infra/terraform
# remote state: configure a backend of your choice first (S3+lock table recommended);
# the module deliberately doesn't hardcode one.
terraform init
terraform apply \
  -var region=us-east-1 \
  -var vpc_id=vpc-XXXX \
  -var 'private_subnet_ids=["subnet-a","subnet-b"]' \
  -var 'operator_cidr_blocks=["203.0.113.0/24"]'   # your VPN/bastion; empty = jobs only
```

Wire the environment from the outputs (direnv-friendly — they ARE the spec §5 variables):

```bash
terraform output            # AWS_REGION, TRIAGE_RDS_HOST/PORT/DB/USER, TRIAGE_S3_BUCKET,
                            # TRIAGE_BATCH_QUEUE, TRIAGE_BATCH_JOB_DEF, ecr_repository_url
```

## 2. Bootstrap the control plane + per-project roles (once per cluster / per project)

The master password lives in Secrets Manager (`terraform output rds_master_secret_arn`) and is
used **only here**, from the operator seat:

```sql
-- as the master user, on the registry database the module created:
--   psql "host=$TRIAGE_RDS_HOST dbname=registry user=triage_admin password=<from secrets manager>"

-- registry schema:
--   DATABASE_URL=postgresql://triage_admin:<pw>@$TRIAGE_RDS_HOST:5432/registry \
--     just alembic-registry upgrade head

-- ONE PG role per project, IAM-authenticated (ADR-0004; the task role's rds-db:connect is
-- scoped to triage_* users):
create user triage_myproject with login;
grant rds_iam to triage_myproject;
```

Create the project itself (registry row + database + triage schema — ADR-0002 lifecycle):

```bash
export TRIAGE_REGISTRY_URL="postgresql://triage_admin:<pw>@$TRIAGE_RDS_HOST:5432/registry"
export TRIAGE_MAINT_URL="postgresql://triage_admin:<pw>@$TRIAGE_RDS_HOST:5432/postgres"
uv run triage project create myproject --display-name "My Project"
# then, still as master: grant the project role its database
#   grant all on database myproject to triage_myproject;
#   \c myproject
#   grant usage, create on schema triage, raw, clean, ontology to triage_myproject;  -- as applicable
```

Load your source data into the project database (`raw → clean → ontology`), then `triage
source register/bump` the pins (ADR-0014).

## 3. Build + push the image

```bash
aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin $(terraform output -raw ecr_repository_url | cut -d/ -f1)
docker build -t $(terraform output -raw ecr_repository_url):latest .
docker push $(terraform output -raw ecr_repository_url):latest
```

## 4. Run one experiment as a Batch job

The submit happens from the operator seat with the §5 env set (the outputs above), plus the
per-project identity as **submit-time overrides** (the job definition deliberately bakes only
the cluster-level vars):

```bash
export AWS_REGION=… TRIAGE_RDS_HOST=… TRIAGE_RDS_PORT=5432 \
       TRIAGE_RDS_DB=myproject TRIAGE_RDS_USER=triage_myproject \
       TRIAGE_S3_BUCKET=… TRIAGE_BATCH_QUEUE=… TRIAGE_BATCH_JOB_DEF=…

uv run triage run my-experiment.yaml --profile cloud
# → "Submitted AWS Batch job <id> (config staged to s3://…/staging/…)" — async by design
```

Inside the job, the container re-resolves `--profile cloud` from its own environment,
authenticates to RDS with a per-connection IAM token (no password anywhere), reads the config
from S3, and runs the experiment in-process; matrices/models land under
`s3://$TRIAGE_S3_BUCKET/…` and predictions/evaluations in the project database.

## 5. Observe

```bash
# Batch status backfill (cloud-profile-spec §7): reports every run with a job id; a FAILED
# job whose run row is still 'started' (hard container death) is marked failed with the reason.
uv run triage runs status --all-pending

# dashboard against RDS (dbname-swap routing needs nothing extra on a shared cluster, ADR-0025):
export TRIAGE_REGISTRY_URL=…   # as above
DATABASE_URL="postgresql://triage_admin:<pw>@$TRIAGE_RDS_HOST:5432/myproject" just serve 8014
```

CloudWatch logs: `/aws/batch/triage` (90-day retention).

## 6. Teardown

```bash
# projects first (DROP DATABASE + tombstone), then the footprint:
uv run triage project drop myproject --confirm myproject
terraform destroy    # deletion_protection on the DB must be lifted explicitly first — deliberate
```

## Known limits (recorded, not hidden)

- ~~Image builds are operator-side~~ resolved 2026-07-10: featurizer is public
  (ccd-ia/featurizer, git+https) — CI and operators build identically, no keys.
- The grid runs **serial** inside a job (ADR-0020) — size `job_vcpus`/`job_memory_mib` for one
  worker, split big grids across experiments.
- Per-project GRANTs (step 2) are manual master-user SQL by design: Terraform owns the
  cluster, `triage project create` owns databases, and role grants stay a deliberate,
  auditable human act.
