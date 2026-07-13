---
title: Technical architecture
description: The two-tier database design, the results schema ERD, the pipeline and its derivation DAG, and how the cloud profile maps onto AWS.
sidebar:
  order: 1
  label: Architecture
---

Everything below is decided in the repo's 28 committed
[architecture decision records](https://github.com/ccd-ia/triage-pg/tree/main/docs/adr)
and audited against the code in
[`docs/adr-conformance.md`](https://github.com/ccd-ia/triage-pg/blob/main/docs/adr-conformance.md);
this page is the guided tour.

## Two tiers: a registry and one database per project

A triage-pg deployment is a small **registry** control-plane database
(projects, users, submissions, per-project routing) plus **one isolated
PostgreSQL database per Project**, each holding a `triage` schema (ADR-0002).
Teardown is `DROP DATABASE`; cross-project SQL is deliberately not native.
The dashboard's project switcher routes each request to the right project
pool via the registry; single-project use needs no registry at all — the
write surface simply reports itself read-only.

Plain PostgreSQL is a hard constraint (ADR-0003): no proprietary extensions,
so the same schema, PL/pgSQL functions, and views run identically on a
laptop, in Docker, self-hosted, or on RDS.

## The results schema

Only *decisions and outcomes* live in the database — predictions (append-only,
time-partitioned), evaluations, fairness metrics, lineage. Matrices are
Parquet on the filesystem or S3; models are binaries beside them. The backbone:

![The results-schema ERD: experiments, runs, the content-addressed artifacts DAG, cohorts/labels/matrices/models, append-only predictions, evaluations and bias metrics](../../../assets/reference/erd.svg)

Three edges carry the design:

- **`artifacts` + `artifact_inputs`** — every built thing (cohort, labels,
  feature group, matrix, model) is a **content-addressed node** whose id
  hashes its *complete input closure*: config, parent artifacts, pinned
  source versions, engine versions. Caching, provenance, and garbage
  collection are all the same mechanism — a re-run cache-hits any node whose
  closure is unchanged, and `triage gc` deletes exactly what no root reaches.
- **`predictions` (RESTRICT, append-only)** — a score is never *the* score;
  it's a row with a `scored_at` timestamp (ADR-0006). Monitoring falls out of
  this for free: drift, volume, and realized-outcome views are just SQL over
  the accumulating history.
- **`experiments` → `runs`** — an experiment *is the prediction problem*
  (cohort + label + temporal config; ADR-0022): features, grids, and
  imputation belong to the run, so adding features is a new *attempt*, not a
  new problem, and leaderboards stay comparable across attempts.

The full diagram with every FK and its `ON DELETE` behavior is in
[`docs/erd.md`](https://github.com/ccd-ia/triage-pg/blob/main/docs/erd.md);
the design rationale in
[`docs/schema-design.md`](https://github.com/ccd-ia/triage-pg/blob/main/docs/schema-design.md).

## The pipeline

![The pipeline: cohort+labels → features (DFS, as-of joins) → matrices → train+predict → in-database evaluation](../../../assets/tutorials/pipeline-5box.svg)

One pass of `triage run` (ADR-0012 — the CLI is the complete product; no UI
holds business logic):

1. **Experiment + run rows**, then **source pinning** — every declared source
   is version-pinned at plan time so cacheability is decidable;
2. **temporal splits** (timechop) fan into one cohort + one labels build over
   the union of dates;
3. **features** — featurizer's PostgreSQL-native Deep Feature Synthesis over
   the config's entity graph, point-in-time-correct via as-of joins
   (ADR-0008);
4. **matrices** per split (Parquet; fit-based imputation fitted on the train
   split only — the ADR-0009 leakage boundary);
5. **train × grid**, then **append predictions** and **evaluate in-database**
   (precision@k, AUC, regression metrics, survival C-index — PL/pgSQL,
   matching their scikit references to 1e-9).

Every stage is an artifact node, so interrupting and re-running resumes
rather than redoing.

## How it runs on AWS

The `local`/`cloud` split is a seam of three adapters — auth, storage,
execution (ADR-0003/0004/0005) — not a fork of the pipeline:

![The cloud profile: EventBridge or an operator submits one AWS Batch job per experiment; the container uses RDS IAM tokens and S3; the dashboard reads the project databases](../../../assets/reference/aws-profile.svg)

- **auth**: RDS IAM — per-project database roles issue short-lived tokens;
  no stored database passwords anywhere;
- **storage**: matrices and model binaries on S3, addressed by the same
  artifact hashes;
- **execution**: one AWS Batch job per experiment, running the same
  `ghcr.io/ccd-ia/triage-pg` image you can pull today; grid parallelism stays
  in-process. Scheduled scoring is an EventBridge rule invoking
  `triage score`.

The Terraform for all of it lives in
[`infra/terraform/`](https://github.com/ccd-ia/triage-pg/tree/main/infra/terraform)
with the operator's walkthrough in
[`docs/cloud-runbook.md`](https://github.com/ccd-ia/triage-pg/blob/main/docs/cloud-runbook.md).
**Honesty note**: the cloud profile is authored and offline-validated
(`terraform validate`, unit-tested seams); the live Batch end-to-end is the
open gate between `v1.0.0-rc` and `v1.0.0`. Nothing on this page pretends
otherwise.

## Where next

- The [dashboard tour](/triage-pg/reference/dashboard/) — every surface these
  tables feed, with screenshots.
- The [CLI tour](/triage-pg/reference/cli/) — the same surfaces, headless.
- The [tutorials](/triage-pg/tutorials/) to see the whole thing run.
