# triage-pg

A PostgreSQL-native, deliberately simplified fork of DSSG's triage for temporal ML on tabular public-policy data — built for teaching, consulting, and (eventually) production monitoring.

## Language

**Project**:
An isolated tenant workspace, realized as one PostgreSQL database in the shared cluster.
_Avoid_: tenant, workspace, namespace

**Registry**:
The control-plane database holding all projects, users, per-project routing/connection info, permissions, and webapp auth.
_Avoid_: catalog, metadata DB, master DB

**Profile**:
A deployment configuration selecting the auth/storage/execution adapters — `local` (standalone PG + password + local FS + in-process) or `cloud` (RDS+IAM + S3 + AWS Batch).
_Avoid_: mode, environment, backend

**as_of_date**:
The point in time at which a prediction is made; features for that row may use only data knowable strictly before it.
_Avoid_: prediction date, snapshot date, reference date

**Cohort**:
The set of entities eligible for prediction at a given as_of_date.
_Avoid_: population, sample, universe

**Matrix**:
The `(entity_id, as_of_date)`-keyed feature table for training or testing; stored as Parquet.
_Avoid_: dataset, dataframe, feature table

**Experiment**:
A prediction **problem** and its evaluation protocol — identified by `cohort_config + label_config + temporal_config + problem_type` (the matrix rows, the target `y`, and the train/test splits). Features, model grid, and imputation are NOT part of an Experiment's identity; they vary per **Run** (ADR-0022). Changing the cohort, label, or temporal config is a different Experiment.
_Avoid_: config, model search, the whole pipeline config

**Run**:
One attempt at an Experiment's problem — a single execution with a specific `feature_config + grid_config + imputation_config`. Many Runs share one Experiment (different feature sets / grids); their model groups are compared on the same fixed `y` and splits. A Run that rebuilds nothing (all cache hits) is a *replay* (ADR-0022).
_Avoid_: job, experiment, trial

**Feature engine (featurizer)**:
The standalone Deep Feature Synthesis SQL-generation engine that synthesizes point-in-time-correct features; it knows nothing of triage concepts.
_Avoid_: collate, feature generator

**Adapter**:
triage-pg-side glue mapping triage concepts (timechop splits, cohort, labels, imputation policy, derivations/cache keys) onto featurizer, storage, auth, and execution.
_Avoid_: connector, plugin, driver

**Prediction**:
An append-only scored row for an `(entity_id, as_of_date)` carrying a `scored_at` timestamp; never overwritten.
_Avoid_: score, output, result

**Source**:
A declared input table read by cohort, label, or feature queries; only declared Sources enter artifact identity (no SQL parsing).
_Avoid_: raw table, input data, from_obj

**Source version (pin)**:
The registry-recorded version label of a Source, bumped on each data load and frozen into derivation hashes at plan time; a Source without one is volatile (never cached, loudly warned).
_Avoid_: snapshot, data hash, freshness stamp

**Derivation**:
An artifact's identity — the hash over its complete input closure: own config, parent Derivations, Source pins, and engine versions. Cache reuse, provenance, and GC key off it.
_Avoid_: cache key (alone), UUID, content hash

**Submission**:
An append-only Registry record of one experiment submitted through the write webapp — who submitted which config, to which Project, under which Profile (and the Batch job id in cloud). The audit row, never the run itself.
_Avoid_: job, request, run (the Run lives in the Project database)

**Principal**:
The resolved caller identity the write webapp's routes see (user id, email, admin flag) — produced by the pluggable auth backend (trusted header locally, OIDC for shared deployments), never a raw header or cookie.
_Avoid_: user object, session, account

**Forward score**:
A scoring-only invocation of a trained model at a prediction date (`triage score`) — no labels, no evaluation at scoring time; appends Predictions under run purpose `forward_score`. The recurring unit of monitoring.
_Avoid_: predictlist (the inherited alias), inference job, retrain

**Reference window**:
The pinned `scored_at` window a deployed model group's score distribution is compared against for drift — by convention its validation period; pinned, never rolling, so drift is always "versus what we validated".
_Avoid_: baseline (ambiguous), training window, rolling average

**C-index**:
Harrell's concordance index — the survival ranking metric (of two comparable entities, how often the earlier-failing one carries the higher risk score); computed in PostgreSQL (`triage.c_index`) on the same spine as precision@k/AUC.
_Avoid_: concordance (alone), survival AUC, accuracy

## Relationships

- A **Registry** tracks many **Projects**; each **Project** is one database with many collaborating users.
- An **Experiment** runs within one **Project**, under one **Profile**.
- An **Experiment** builds **Matrices** keyed by (**Cohort** entity × **as_of_date**); the **Feature engine** generates the features and an **Adapter** assembles the **Matrix**.
- A trained model produces append-only **Predictions**; evaluation, leaderboards, and bias metrics run in PostgreSQL over the **Predictions** table.
- An **Experiment** freezes the current **Source version** of every declared **Source** at plan time; every artifact's **Derivation** embeds those pins plus its parents' Derivations (Merkle DAG).
- A **Submission** records that a **Principal** asked a **Project** to run an **Experiment**; the resulting Run and **Predictions** live in the **Project** database — the **Registry** keeps only the audit row.
- **Forward scores** append **Predictions** over time; monitoring compares each scoring window against the model group's **Reference window** (drift) and re-evaluates once labels arrive (realized outcomes). Survival experiments are evaluated by **C-index** on the same ranking spine.

## Flagged ambiguities

- "model" was used for both a trained estimator artifact and a *model group* (a hyperparameter configuration shared across time splits) — keep them distinct: **model** = one trained artifact; **model group** = the configuration shared across temporal splits.
- "featurizer" names both the repo/engine and the act of feature generation — reserve **featurizer** for the engine; use "feature generation" for the activity.

## Example dialogue

> **Dev:** "When a user submits an **Experiment**, where do its **Predictions** land?"
> **Adolfo:** "In that **Project**'s database — never the **Registry**. The **Registry** only routes the job to the right database and records who ran it."
> **Dev:** "And the **Matrix** — is that in Postgres too?"
> **Adolfo:** "No. The **Matrix** is Parquet (S3 in the cloud **Profile**, local disk otherwise). Only the **Predictions** and evaluation live in the **Project** database, because that's all the in-Postgres metrics need."
