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

**Master role**:
The cluster-level PostgreSQL role (`triage_admin`) used only at bootstrap, from the operator seat, to `CREATE DATABASE` / `CREATE USER` / run migrations. Its password lives in Secrets Manager and never reaches app config. Exists in the cloud profile only; the local profile has a single user who is both roles. Distinct from the Registry (a database, not a role).
_Avoid_: admin user, superuser, root, master DB

**Project role**:
The per-project PostgreSQL login role (`triage_<project>`) the pipeline runs as. Password-less: it authenticates with a short-lived RDS IAM token minted by the Batch task role (ADR-0004), and the task role's `rds-db:connect` is scoped to `triage_*`, so the naming is load-bearing. Because the Master role runs the migrations, it owns the objects they create — so the Project role must be *given ownership* of `triage.leaderboard`, since `REFRESH MATERIALIZED VIEW` is owner-only and no grant confers it (`triage project create --owner` / `triage project grant`).
_Avoid_: app user, service account, IAM user (the IAM principal is a separate thing)

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

**Entity**:
The object of interest an `entity_id` names — one entity type per table, described by the static "core" attributes that identify *who or what this is*, never by things that happened to it (those are Events) and never by its future (that is the label's business). An entity table carries a validity/state representation — a `daterange` (DirtyDuck's `activity_period` + GIST index is the reference) or an entity-state table — so "who was in the population at time *t*" is answered by the schema, indexed, rather than re-derived inside every cohort query. Place a spatial coordinate on the entity only when it provably cannot move (a facility does not relocate); a moving location belongs on the Event. A Source with `role = 'entity'` declares such a table.
_Avoid_: record, row, unit, subject

**Event**:
Something that happened at a point in spacetime, carrying: the involved Entity; when; a type discriminator; the event's own attributes — ordinarily a `jsonb` bag; and, where the data allows, a spatial coordinate (`geography(Point,4326)` + GIST, not untyped lat/lon doubles) placed on the event whenever the event can move. An Event has **two clocks**: occurrence time (when it happened) and knowledge time (when it became observable to us — what a Source's `knowledge_date_column` declares). Where the recording lag is genuinely zero, say so explicitly rather than implying it with a single collapsed date column. **Promotion rule** (companion, not a substitute for the jsonb clause): promote to a typed column anything you intend to featurize; keep `jsonb` for what you retain but do not model — jsonb costs the type system and the planner's selectivity estimates, and the Feature engine's DFS consumes columns. DirtyDuck is the worked example: `type`/`risk`/`result` are typed and featurized while the `violations` jsonb is retained detail. **Transaction-grained pattern**: when the unit of prediction is the arriving item (a DonorsChoose posting, a Chicago 311 filing), the entity legitimately *is* an event — but the durable actors it references (teacher, school, community area) still belong in their own Entity tables, not as bare text columns. A Source with `role = 'event'` declares such a table.
_Avoid_: transaction (as a generic synonym), log row, fact (warehouse jargon)

**Source**:
A declared input table read by cohort, label, or feature queries; only declared Sources enter artifact identity (no SQL parsing). Its `role` is exactly `entity` or `event` — the two terms above, which the schema enforces with a check constraint.
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

**Protected group**:
A row in `triage.protected_groups`: one entity's value for one protected attribute at one as_of_date (long format — an entity carries several attributes). Populated by the `bias_config` query; the SQL bias metrics group by it.
_Avoid_: demographic table, sensitive column (a protected attribute is the column; the group is a value)

**Fairness tree**:
The Aequitas decision tree routing an intervention to the disparity metric that carries its harm (punitive → FPR/FDR, assistive → FNR/FOR, representation → selection rate). In triage-pg it is a guidance wizard + `bias_config.intervention` preselect — it highlights, never hides.
_Avoid_: fairness score, bias threshold (τ is separate)

**Subset**:
A named cohort slice (`evaluation.subsets` query → `triage.subset_members`) that is treated as the POPULATION for its own evaluation rows: ranks are recomputed within it, so precision@k on a subset is the top-k of the subset's own ranking. Identity-neutral; full-cohort rows keep `subset_hash = ''`.
_Avoid_: filter (reads as post-hoc), segment, cohort (the subset slices the cohort)

**Crosstab**:
The persisted selected-vs-rest feature comparison (`triage.crosstabs`): per feature, the mean/std/nonzero-rate among the top-k versus the rest, plus their ratio — "what characterizes the list". Computed from the matrix by `triage postmodel crosstabs`.
_Avoid_: contingency table (this is a stat comparison, not counts), feature drift

**Error tree**:
The "predict on the errors" diagnostic: a shallow decision tree fitted on the model's mistakes at the top-k cut, whose leaf paths become human-readable rules with support and error rate (`triage.error_analysis`). Strictly diagnostic — never a score modifier.
_Avoid_: error model (implies stacking), boosting, residual model

## Relationships

- A **Registry** tracks many **Projects**; each **Project** is one database with many collaborating users.
- An **Experiment** runs within one **Project**, under one **Profile**.
- An **Experiment** builds **Matrices** keyed by (**Cohort** entity × **as_of_date**); the **Feature engine** generates the features and an **Adapter** assembles the **Matrix**.
- A trained model produces append-only **Predictions**; evaluation, leaderboards, and bias metrics run in PostgreSQL over the **Predictions** table.
- An **Experiment** freezes the current **Source version** of every declared **Source** at plan time; every artifact's **Derivation** embeds those pins plus its parents' Derivations (Merkle DAG).
- A **Submission** records that a **Principal** asked a **Project** to run an **Experiment**; the resulting Run and **Predictions** live in the **Project** database — the **Registry** keeps only the audit row.
- **Forward scores** append **Predictions** over time; monitoring compares each scoring window against the model group's **Reference window** (drift) and re-evaluates once labels arrive (realized outcomes). Survival experiments are evaluated by **C-index** on the same ranking spine.
- Fairness reads **Predictions** grouped by **Protected group** (with τ verdicts); the **Fairness tree** routes attention to the disparity that matters for the intervention. A **Subset** re-runs every metric with the slice as the population. **Crosstabs** and **Error trees** are persisted diagnostics computed once from the Matrix (`triage postmodel`), read by CLI and dashboard alike.

## Flagged ambiguities

- "model" was used for both a trained estimator artifact and a *model group* (a hyperparameter configuration shared across time splits) — keep them distinct: **model** = one trained artifact; **model group** = the configuration shared across temporal splits.
- "featurizer" names both the repo/engine and the act of feature generation — reserve **featurizer** for the engine; use "feature generation" for the activity.

## Example dialogue

> **Dev:** "When a user submits an **Experiment**, where do its **Predictions** land?"
> **Adolfo:** "In that **Project**'s database — never the **Registry**. The **Registry** only routes the job to the right database and records who ran it."
> **Dev:** "And the **Matrix** — is that in Postgres too?"
> **Adolfo:** "No. The **Matrix** is Parquet (S3 in the cloud **Profile**, local disk otherwise). Only the **Predictions** and evaluation live in the **Project** database, because that's all the in-Postgres metrics need."
