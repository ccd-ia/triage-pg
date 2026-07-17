# 0029. Cloud validation runs on triage-pg's own RDS + IAM, not the shared egobytp instance

- Status: Accepted
- Date: 2026-07-17
- Deciders: Adolfo (call), Claude (recommendation + recon)

The cloud profile (ADR-0003–0005) is code-complete and moto-tested but had never
been exercised end-to-end against real AWS; closing that gate requires choosing a
concrete RDS + auth target. The backdrop is the org-wide "aws-cleaning" rollout: on
the **shared** egobytp RDS (`db-egobiernoytp-ccd`), DB access uses a Postgres `SET
ROLE` model and RDS IAM auth was **deferred** (poor fit for dbt/Dagster); unattended
workers assume a per-project service role that auto-switches to a NOLOGIN owner
(e.g. `conju_zac_svc` → `conju_zac_rw`). triage-pg's cloud profile instead assumes
**RDS IAM auth on its own Terraform-provisioned RDS** (a `registry` DB + per-project
DBs) — a different instance and a different auth model. This ADR records which target
the live validation uses, and why. (Three-criteria check: *hard to reverse* — the
target instance and auth model shape every future cloud deployment and the
aws-cleaning reconciliation; *surprising without context* — the org moved to a shared
instance + SET ROLE, so keeping a separate instance + IAM needs its rationale on the
record; *real trade-off* — a second RDS costs money vs. one instance + one auth model
is more consistent.)

## Decision

The end-to-end cloud validation runs on **triage-pg's own Terraform-provisioned RDS
with RDS IAM auth** — the self-contained footprint the code already assumes
(`profiles/auth.py` token-provider seam, ADR-0004; `infra/terraform/rds.tf` +
`iam.tf`). It does **not** reuse the shared egobytp RDS or the `conju_zac` databases
for this validation.

Rationale: this is the *shortest path to actually running the missing test* — no code
change to the auth model, no mapping of triage's registry/per-project DB layout onto a
shared instance — and it *isolates a first-ever shakeout from the shared,
production-adjacent RDS* (zero blast radius on egobytp/conju_zac data). The extra RDS
is temporary: `terraform destroy` after the validation.

## Considered alternatives

- *Reuse the shared egobytp RDS + `conju_zac` with SET ROLE* — the org-consistent end
  state (one instance, one auth model, aligned with aws-cleaning). Rejected **for the
  validation**: it requires dropping IAM auth here (reworking the `token_provider` seam
  to a service-role password + SET ROLE auto-switch), mapping the registry +
  per-project DB model onto the shared instance, and running the first live shakeout
  against a shared production-adjacent RDS. That is more rework and more risk than the
  goal — *prove the cloud path runs E2E* — warrants right now.

## Consequences

- No conflict with the aws-cleaning rollout: the validation footprint is entirely
  triage-owned and torn down afterward.
- The IAM-auth code path (ADR-0004) gets its first real exercise, as intended.
- A second RDS instance runs for the duration of the validation — a temporary cost,
  destroyed after.
- Adopting the shared-RDS + SET-ROLE model as triage-pg's *standing* cloud posture
  remains open and is **deliberately deferred** to a future ADR, to be decided on its
  own merits (org consistency vs. a dedicated instance) rather than under the pressure
  of the first shakeout. The `token_provider` seam keeps that migration cheap.
