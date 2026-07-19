# Cloud runbook — running triage-pg experiments remotely on AWS

The cloud profile (ADRs 0003–0005, `docs/cloud-profile-spec.md`) runs one experiment as one
**AWS Batch job** against a **private RDS PostgreSQL** with **RDS IAM authentication** and
artifacts on **S3**. No database password ever exists: the job authenticates with a short-lived
IAM token minted from its Batch **task role**.

This runbook is the end-to-end live path — provision, bootstrap, build, run, verify, tear down —
plus the mental model you need to not fight the network, and a troubleshooting section keyed to
the exact errors this setup produces. It was validated end-to-end on real AWS on 2026-07-18
(a DirtyDuck experiment: 20 models, 268 860 predictions, 120 evaluations, all in one Batch job).

---

## 1. The mental model (read this first)

Three ideas prevent almost every dead end.

### 1.1 `triage run --profile cloud` is one command with two roles

- **On your laptop (the operator seat) it SUBMITS.** It serializes the experiment config,
  stages it to S3, and calls `submit_job`. It needs a valid AWS session for only a few seconds
  and **never touches the database**. Then it returns a Batch job id and exits.
- **Inside the Batch container it RUNS.** Batch injects `AWS_BATCH_JOB_ID`; `load_profile`
  detects it and runs the experiment **in-process** in that container, reading the config back
  from S3 and connecting to RDS with an IAM token. It does **not** re-submit.

The same argv (`triage run <config> --profile cloud`) drives both; the environment decides which
role it plays. You submit from the laptop; the heavy grid runs unattended on Batch.

### 1.2 Credentials: short-lived by design, and never on the DB path

- `aws sso login` / `aws login` gives **short-lived** (~1 day) credentials. That is fine — the
  submit needs a session for seconds only. **Do not** try to keep a laptop session alive for a
  long run; long runs live on Batch, whose **task role** rotates its own credentials and never
  expires.
- The job reads no password. Its task role holds `rds-db:connect` (scoped to `triage_*` users)
  and mints an IAM token per physical connection; TLS is forced (`sslmode=verify-full`).

### 1.3 The RDS is PRIVATE — two distinct reach paths

The database has **no public endpoint**. How you reach it depends on **where the code runs**:

- **Path A — in-VPC compute (the Batch job).** Reaches the private RDS **directly** over the
  VPC network. This requires the Batch compute environment's subnets + security group to sit in
  the RDS's VPC and be allowed by the RDS security group. The Terraform here wires exactly that:
  Batch and RDS share `vpc_id`/`private_subnet_ids`, and the RDS SG allows `:5432` from the
  batch-jobs SG. **Never route a Batch job's DB traffic through a bastion** — if a job "can't
  reach the DB", that is a VPC/SG wiring problem, not a tunnelling problem.
- **Path B — you, interactively, from your laptop** (bootstrap psql, dashboards, ad-hoc
  queries). The private RDS is **not** reachable directly, so you go through a **bastion SSM
  tunnel** (§5). This is a human convenience only.

> The one gotcha that produces almost all "can't reach the DB" confusion: **image-pull egress is
> a *separate* leg from RDS reachability.** A Fargate task in a private subnet with
> `assignPublicIp=DISABLED` still needs egress to **ECR + S3 + CloudWatch Logs** just to pull its
> image and ship logs — via a NAT gateway or (cleaner) VPC endpoints (§3). RDS being reachable
> does not imply ECR is.

---

## 2. Prerequisites

- **AWS account + operator credentials** able to create RDS/S3/ECR/Batch/IAM. Operator-side
  only; the application never holds them. `aws sts get-caller-identity` should succeed.
- **A VPC with two+ private subnets in different AZs** (the DB subnet group needs two AZs). This
  is bring-your-own-network — the module creates its own security groups but not the VPC/subnets.
  The subnets need working egress to ECR/S3/logs, **either** a NAT gateway **or** the VPC
  endpoints this module can create (`create_vpc_endpoints=true`, §3).
- **A bastion EC2 instance** in the VPC with the SSM agent + an instance profile allowing SSM, if
  you want operator psql/dashboard access to the private RDS (§5). Note its **private IP**.
- **Docker with buildx.** Fargate runs **linux/amd64**; if you build on Apple Silicon/arm64 you
  must cross-build (§6). The `featurizer` dependency is a public `git+https` pin (ADR-0016) — no
  deploy key, no ssh forwarding.
- **Terraform ≥ 1.6.**

---

## 3. Provision the footprint (GATED — costs real money)

```bash
cd infra/terraform
terraform init          # configure a remote backend (S3 + lock table) first for shared use;
                        # the module deliberately doesn't hardcode one.
```

Put your inputs in a gitignored `terraform.tfvars` (state and `tfplan*` are gitignored too — they
carry ARNs and secret references):

```hcl
region             = "us-east-1"
vpc_id             = "vpc-xxxxxxxx"
private_subnet_ids = ["subnet-aaaa", "subnet-bbbb"]

# Operator access to the private RDS goes through a bastion SSM tunnel (§5); allow the
# bastion's PRIVATE IP so the RDS SG lets the tunnel in. Empty = Batch jobs only.
operator_cidr_blocks = ["10.0.10.128/32"]

# Create ECR/logs interface endpoints so Fargate can pull the image + ship logs WITHOUT a NAT.
# Leave false if your private subnets already have a working NAT gateway.
create_vpc_endpoints = true
# If your VPC has no S3 gateway endpoint yet, also give the route tables to attach one to
# (ECR layer blobs live in S3). Leave empty if a gateway endpoint already exists.
# endpoint_s3_route_table_ids = ["rtb-xxxx"]
```

```bash
terraform apply
terraform output          # AWS_REGION, TRIAGE_RDS_HOST/PORT/DB/USER, TRIAGE_S3_BUCKET,
                          # TRIAGE_BATCH_QUEUE, TRIAGE_BATCH_JOB_DEF, ecr_repository_url
```

### 3.1 Batch-job network egress (the ECR-pull leg)

A Fargate task with `assignPublicIp=DISABLED` (the design — the job stays private) reaches AWS
service endpoints only through one of:

| Option | What to do | Trade-off |
| --- | --- | --- |
| **VPC endpoints** (recommended) | `create_vpc_endpoints=true` — creates interface endpoints for `ecr.api`, `ecr.dkr`, `logs` + reuses/creates an `s3` gateway | NAT-free, stays private, ~3 endpoints' hourly cost |
| **NAT gateway** | Leave `create_vpc_endpoints=false`; ensure the private subnets' route table sends `0.0.0.0/0` to an **available** NAT | Simpler if you already run a NAT; NAT hourly + data cost |

You need **all four** legs for an image pull: `ecr.api` (auth), `ecr.dkr` (registry), `s3` (layer
blobs), and `logs` (the awslogs driver). Missing any one gives the ECR-pull timeout in §11.
Interface endpoints require the VPC to have **DNS hostnames + DNS support enabled** (so the
default `*.amazonaws.com` names resolve to the endpoint ENIs).

---

## 4. Bootstrap the control plane, the project, and its role

The master password lives in Secrets Manager and is used **only here**, from the operator seat:

```bash
export TRIAGE_RDS_HOST=$(terraform output -raw TRIAGE_RDS_HOST)
# fetch the master password (bootstrap only — never stored in app config):
aws secretsmanager get-secret-value \
  --secret-id "$(terraform output -raw rds_master_secret_arn)" \
  --query SecretString --output text        # -> {"username":"triage_admin","password":"…"}
```

All psql below runs **through the bastion tunnel** (§5) since the RDS is private — open the tunnel
first, then connect to `host=localhost port=54321`.

### 4.1 Registry control plane (once per cluster)

```bash
# the module created an empty `registry` database; put the control-plane schema on it:
DATABASE_URL="postgresql://triage_admin:<pw>@localhost:54321/registry" \
  just alembic-registry upgrade head
```

### 4.2 Per-project role (once per project)

```sql
-- as master, on `registry` (or any db): one IAM-authenticated login role per project.
-- The task role's rds-db:connect is scoped to triage_* — keep the naming.
create user triage_myproject with login;
grant rds_iam to triage_myproject;
```

### 4.3 Create the project database + schema

```bash
export TRIAGE_REGISTRY_URL="postgresql://triage_admin:<pw>@localhost:54321/registry"
export TRIAGE_MAINT_URL="postgresql://triage_admin:<pw>@localhost:54321/postgres"
uv run triage project create myproject --display-name "My Project"
# -> registry row + CREATE DATABASE myproject + the triage schema at alembic head
```

### 4.4 Grant the project role its database — and give it OWNERSHIP it actually needs

`triage project create` runs the migrations **as the master**, so the master owns the schema
objects. A plain `GRANT usage, create` is enough for tables, but **not** for
`REFRESH MATERIALIZED VIEW`: the pipeline refreshes `triage.leaderboard` after every run, and
`REFRESH` requires **ownership** — a GRANT cannot confer it. Hand the matview to the project role:

```sql
-- as master:
grant all on database myproject to triage_myproject;

\c myproject
grant usage, create on schema triage to triage_myproject;
grant usage, create on schema raw, clean, ontology to triage_myproject;  -- as your data needs

-- Ownership transfer so the per-project role can REFRESH its matview (the leaderboard, ADR-0007).
-- The master must be a member of the target role to reassign ownership to it:
grant triage_myproject to triage_admin;
alter materialized view triage.leaderboard owner to triage_myproject;
```

> Without this the run still **succeeds**, but logs a non-fatal
> `Could not refresh triage.leaderboard (non-fatal): must be owner of materialized view` and the
> leaderboard read view goes stale until someone with ownership refreshes it. If a future schema
> migration drops+recreates the matview, re-run the `alter … owner` (ownership resets to whoever
> ran the migration). Any *new* refreshed matview added later needs the same one-liner.

### 4.5 Load source data

Load your data into the project database (`raw → clean → ontology`), then pin sources:

```bash
uv run triage source register …   # then `triage source bump` (ADR-0014 derivation pins)
```

Data with domain enum types / PostGIS: create those types + `CREATE EXTENSION` in the project DB
before restoring, or the `\copy`/`pg_restore` will fail on missing types.

---

## 5. Operator access to the private RDS (bastion SSM tunnel)

The RDS has no public endpoint (§1.3, path B). To psql / run the dashboard / run bootstrap SQL
from your laptop, port-forward through a bastion EC2 instance via SSM (no SSH key, no open port):

```bash
aws ssm start-session \
  --target <bastion-instance-id> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$TRIAGE_RDS_HOST\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"54321\"]}"
# leave this running; in another terminal connect to localhost:54321
```

```bash
# psql as master (bootstrap):
psql "host=localhost port=54321 dbname=myproject user=triage_admin password=<pw>"
```

Requirements: the bastion has the SSM agent + an instance profile with SSM permissions; your
operator credentials can `ssm:StartSession`; and `operator_cidr_blocks` (§3) includes the
**bastion's private IP** so the RDS SG admits the tunnelled connection.

> The Batch job never uses this tunnel — it reaches RDS directly in-VPC (§1.3, path A).

---

## 6. Build + push the image

Fargate runs **linux/amd64**. The image bakes the RDS global CA bundle (for `verify-full`) and
the `triage` CLI.

```bash
ECR_URL=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${ECR_URL%%/*}"
```

**On linux/amd64 hosts:**

```bash
docker build --target base -t "$ECR_URL:latest" .
docker push "$ECR_URL:latest"
```

**On Apple Silicon / any arm64 host — cross-build with buildx and push in one step:**

```bash
docker buildx build --platform linux/amd64 --target base --provenance=false \
  -t "$ECR_URL:latest" --push .
```

`--target base` is the minimal CLI runtime the Batch job needs (the default target also builds the
dashboard/SPA, which the job never uses). `--provenance=false` keeps a single-arch manifest so
Fargate resolves the image cleanly.

---

## 7. Run one experiment as a Batch job

Set the spec §5 environment (the Terraform outputs), plus the per-project identity — which rides
as **submit-time container overrides** (the job definition bakes only the cluster-level vars, so
one job definition serves every project):

```bash
export AWS_REGION=us-east-1 \
       TRIAGE_RDS_HOST="$(terraform output -raw TRIAGE_RDS_HOST)" \
       TRIAGE_RDS_PORT=5432 \
       TRIAGE_RDS_DB=myproject \
       TRIAGE_RDS_USER=triage_myproject \
       TRIAGE_S3_BUCKET="$(terraform output -raw TRIAGE_S3_BUCKET)" \
       TRIAGE_BATCH_QUEUE="$(terraform output -raw TRIAGE_BATCH_QUEUE)" \
       TRIAGE_BATCH_JOB_DEF="$(terraform output -raw TRIAGE_BATCH_JOB_DEF)"

uv run triage run my-experiment.yaml --profile cloud
# -> Submitted AWS Batch job <id> (config staged to s3://…/config.json) — async by design
```

No DB tunnel is needed for the submit (§1.1). Inside the job the container re-resolves
`--profile cloud`, sees `AWS_BATCH_JOB_ID`, runs the experiment in-process, authenticates to RDS
with a per-connection IAM token, reads the config from S3, writes matrices/models under
`s3://$TRIAGE_S3_BUCKET/…`, and appends predictions/evaluations to the project database.

---

## 8. Observe and verify

Do not trust the Batch status alone — verify the artifacts.

```bash
JOB=<job-id>

# 1. Batch lifecycle: expect STARTING -> RUNNING -> SUCCEEDED (exit code 0).
aws batch describe-jobs --jobs "$JOB" \
  --query 'jobs[0].{status:status,exit:container.exitCode,reason:statusReason}'

# 2. The application's own summary (proves the pipeline ran, not just the container):
STREAM=$(aws batch describe-jobs --jobs "$JOB" --query 'jobs[0].container.logStreamName' --output text)
aws logs get-log-events --log-group-name /aws/batch/triage --log-stream-name "$STREAM" \
  --limit 300 --query 'events[].message' --output text | tr '\t' '\n' | tail -20
#   -> "Experiment …completed: N run(s), N model(s), N prediction(s), N evaluation(s)."

# 3. Artifacts physically on S3 (matrices .parquet + models .joblib):
aws s3 ls "s3://$TRIAGE_S3_BUCKET/" --recursive | tail -20

# 4. Predictions/evaluations in the project DB (through the tunnel, §5):
psql "host=localhost port=54321 dbname=myproject user=triage_admin password=<pw>" -c \
  "select count(*) from triage.predictions;  select count(*) from triage.evaluations;"
```

Backfill run rows from Batch (a hard container death leaves a run 'started'; this marks it):

```bash
uv run triage runs status --all-pending
```

Dashboard against the project DB (through the tunnel):

```bash
export TRIAGE_REGISTRY_URL="postgresql://triage_admin:<pw>@localhost:54321/registry"
DATABASE_URL="postgresql://triage_admin:<pw>@localhost:54321/myproject" just serve 8014
```

CloudWatch logs live under `/aws/batch/triage` (90-day retention).

---

## 9. Teardown

```bash
# projects first (DROP DATABASE + registry tombstone), then the footprint:
uv run triage project drop myproject --confirm myproject

# the throwaway-footprint tfvars flip deletion_protection/final-snapshot/force-destroy so
# `terraform destroy` removes 100% (prod defaults keep the DB protected — deliberate):
terraform destroy
```

If you leave the footprint up between sessions, the cost floor is the RDS instance plus any
interface endpoints (a stopped NAT costs nothing). Tear down or stop when idle.

---

## 10. What is deliberately NOT in Terraform

- **Per-project databases + PG roles + grants** — created by `triage project create` and the §4
  master SQL. Terraform owns the *cluster*, not the projects; role grants stay a deliberate,
  auditable human act (ADR-0002/0004).
- **The container image** — built/pushed by you or CI (§6).
- **Remote state backend** — configured per deployment, not hardcoded.
- **The VPC/subnets/NAT/bastion** — bring-your-own network.

---

## 11. Troubleshooting (keyed to real errors)

**Job FAILED at `STARTING`:
`ResourceInitializationError … cannot pull registry auth from Amazon ECR … dial tcp … i/o timeout`.**
The Fargate task has no egress to ECR. Either the private subnets' `0.0.0.0/0 → NAT` route is
missing/blackholed, or there are no VPC endpoints. Fix with `create_vpc_endpoints=true` (§3.1) or
a working NAT. Confirm the NAT is `available`:
`aws ec2 describe-nat-gateways --filter Name=state,Values=available`. Remember you need all of
`ecr.api`, `ecr.dkr`, `s3` (gateway), and `logs`.

**Container exits 2 immediately:
`Invalid value: Database connection not provided. Use one of: --dbfile / DATABASE_URL / PGHOST …`.**
The image is **stale** — it predates the in-container execution wiring. Rebuild and push (§6),
then re-submit. (Current images run the experiment in-process when `AWS_BATCH_JOB_ID` is set and
never hit the generic DB resolver.)

**Container fails on connect with a TLS/SSL certificate verification error.**
The RDS CA bundle is missing at `/etc/ssl/certs/rds-combined-ca-bundle.pem` (CloudAuth forces
`sslmode=verify-full`). Rebuild from the current Dockerfile, which bakes the all-regions bundle.

**`Could not refresh triage.leaderboard (non-fatal): must be owner of materialized view`.**
The project role doesn't own the matview. Apply the ownership transfer in §4.4. Non-fatal: the
run still writes all predictions/evaluations.

**Job can't reach the DB even though ECR pull worked.**
A VPC/SG problem, never a reason to tunnel from the job (§1.3). Check: Batch subnets are in the
RDS's VPC; the RDS SG allows `:5432` from the batch-jobs SG; the IAM user has `grant rds_iam`; the
task role's `rds-db:connect` covers the `triage_<project>` user (it is scoped to `triage_*`).

**`aws … ExpiredToken` while submitting.**
Your laptop session lapsed (~1 day, by design). `aws sso login` again and re-submit — the submit
needs only seconds. Never move a long run onto the laptop to dodge this; that is what Batch is for.

**Operator `psql` to the RDS hangs/refuses.**
The RDS is private. Open the bastion SSM tunnel (§5) and connect to `localhost:54321`; ensure
`operator_cidr_blocks` includes the bastion's private IP.

---

## 12. Known limits (recorded, not hidden)

- The grid runs **serial** inside a job (ADR-0020) — size `job_vcpus`/`job_memory_mib` for one
  worker; split large grids across experiments (cross-experiment parallelism is Batch's queue).
- One Batch job per experiment; the CLI returns immediately and does not stream job logs
  (`runs status` + CloudWatch are the observability surface).
- Matrices/models live on S3, not in the DB (only predictions + evaluations are in Postgres).
