# triage-pg — Artifact Derivation DAG (Design)

- Status: **In progress** — source-data pinning (§3), node granularity (§4), and engine versions (§5) resolved 2026-06-11; GC and builder wiring still open (§6)
- Date: 2026-06-11
- Implements: ADR-0013 (derivation-hash identity), ADR-0014 (source-data pinning), ADR-0015 (node granularity), ADR-0016 (engine versions)
- Related: ADR-0006 (append-only predictions), schema-design.md §4.7–4.8 (sources + artifacts DDL)

## 1. Requirement

Every built artifact — cohort, labels, features, matrix, model, predictions,
evaluations — must have a **real dependency tree à la Guix**: its identity is a
hash over its **complete input closure**, and its dependency edges are explicit
and queryable. Four properties fall out, exactly as they do in the Guix store:

1. **Exact cache reuse** — same inputs ⇒ same hash ⇒ skip the build.
2. **Minimal incremental rebuilds** — a changed input invalidates precisely the
   downstream cone, nothing else.
3. **Provenance** — any artifact can answer "what exact inputs produced you?"
   in SQL.
4. **GC by reachability** — artifacts unreachable from any retained root
   (experiment) can be deleted; matters most for Parquet matrices on disk/S3.

### Why the inherited hashing falls short

Old triage's hashes are deterministic over **config text only**: matrix UUIDs
over a metadata dict (`architect/planner.py`), the experiment hash over the
config (`catwalk/utils.py`), model hashes over class path + hyperparameters +
matrix metadata + seed (`catwalk/model_trainers.py`), cohort/label table names
over the query strings (`experiments/base.py`). None of them cover the **state
of the source data**, the **code version**, or the **identity of upstream
artifacts** — so cache validity rests on a manual contract, stated verbatim in
the old docs: *"if the source data has changed, ensure that `replace` is set to
True."* The only pinning pattern that existed was advisory: put an
entity-matching model id into `user_metadata` so it perturbs the experiment
hash. This design makes that idea first-class and universal.

## 2. Identity scheme (sketch)

```
artifact_id = H( kind
              ∥ canonical(own_config)         -- the artifact's config slice
              ∥ sorted(parent_artifact_ids)   -- upstream artifacts (Merkle DAG)
              ∥ sorted(source_pins)           -- (source_name, version_label) pairs (§3)
              ∥ sorted(engine_versions) )     -- triage-pg, featurizer, … (§4, open)
```

- `H` = SHA-256; `canonical(·)` = canonical JSON (sorted keys, normalized
  scalars/dates/intervals). Implemented in `src/triage/derivation.py`.
- Parents make it a **Merkle DAG**: a matrix's id embeds the feature/cohort/
  label ids, a model's id embeds the matrix id, and so on — any upstream change
  ripples down automatically.
- **Build = lookup-or-create**: derivation id present and output exists ⇒ cache
  hit, reuse; otherwise build and record. This replaces the `replace` flag as
  the cache-correctness mechanism (a `--force` stays as an operator override).
- **Predictions are events, not cache entries** (ADR-0006): a scoring run's
  rows carry lineage (model id, matrix id) but are append-only and never
  deduplicated; `scored_at` is wall-clock history, not an input.

## 3. RESOLVED — Source-data pinning (2026-06-11, ADR-0014)

The hardest input to pin: a Postgres table has no cheap content hash. Decisions:

### 3.1 Sources are explicitly declared inputs

Cohort, label, and feature configs **declare** the source tables they read; SQL
is never parsed to discover them. Like Guix inputs, an undeclared input does
not exist for identity purposes. This converges with the featurizer ER-graph
config (adapter-spec pass): that config already enumerates the entity/event
tables.

### 3.2 Pins come from a registry table

`triage.sources` + `triage.source_versions` (DDL in schema-design.md §4.7).
Whoever loads data **bumps** the version: the ETL/loader as the natural caller,
`triage source bump <name>` for manual loads. A bump records a `version_label`
plus an advisory fingerprint (§3.4).

At experiment **plan time** the adapter resolves each declared source to its
current pin and **freezes** the sorted `(source_name, version_label)` pairs into
every downstream derivation hash. The run records the resolved pin set in
`triage.run_source_pins` — the `guix describe` analog: any artifact's closure
can answer "built against `events` at `v2026-06-10`".

### 3.3 Unpinned source ⇒ volatile, never cached, loud warning

A declared source with no registered version is an **impure input**: every
derivation touching it is marked non-cacheable (always rebuilt downstream), and
a warning explains how to register/bump. Rationale: zero setup friction for
teaching/DirtyDuck, while *never silently stale* — the failure mode of manual
pinning is a wasted rebuild, not a wrong cache hit.

### 3.4 Advisory drift detection (in v1)

At bump and at build time we capture a cheap fingerprint per source —
`row_count` plus `max(knowledge_date_column)` when declared. If a later run
sees the fingerprint move while the pin did not, it **warns loudly** ("source
changed but nobody bumped the pin"). Fingerprints are advisory only — they
**never enter identity**, because they are unsound as identity (a backfill can
leave both unchanged).

### 3.5 Considered alternatives (rejected)

- **Config-inline version stamps** — per-experiment copies drift; it is
  `replace=True` with extra steps unless every config author is disciplined.
- **Automatic fingerprint-as-identity** (`max(updated_at)`, row counts) —
  unsound: false cache hits on backfills/corrections; not all tables carry an
  update column.
- **Full content hashing** of source tables — sound but prohibitively expensive
  at consulting scale; would dominate pipeline runtime.
- **Per-table triggers maintaining a version counter** — invasive DDL on data
  the project may not own stylistically; offers little over loader bumps.

## 4. RESOLVED — Node granularity (2026-06-11, ADR-0015)

What gets an artifact node, layer by layer:

| Layer | Node grain | Output |
|---|---|---|
| Cohort | (cohort config, as_of_date) | date-slice of `triage.cohorts` |
| Labels | (label config, as_of_date, label_timespan) | date-slice of `triage.labels` |
| Features | (feature group, as_of_date) | date-slice of the group's feature table |
| Matrix | one per split-side (train / test) | one Parquet file |
| Model | (class, hyperparameters, seed) × train matrix | one model artifact |

### 4.1 Per-as_of_date data layer

timechop makes consecutive splits overlap heavily in as_of_dates, so per-date
nodes turn that overlap into cache hits — within an experiment, across
config-iteration re-runs, and across experiments. Extending the date range
builds only the new dates. This is also the main mitigation for the ADR-0008
scale risk (featurizer re-runs its aggregation CTEs per as_of_date with no
reuse): cached dates are simply never re-run.

Scale check: 7 years monthly (84 dates) × (15 groups + cohort + labels) ≈ 1.4k
data-layer nodes per configuration; with matrices, edges, and a 50-point model
grid, ~20k rows per substantial experiment — trivial for PostgreSQL.

### 4.2 Feature groups are an adapter concept

featurizer itself is monolithic per run (one CTE tree, one wide output, the
full date list in one pass — `planner.py`/`executor.py`) and has no group
concept. The **adapter** defines a feature group as a named sub-config (a
subset of entities/relationships/intervals) and invokes featurizer once per
(group, as_of_date). This matches the old `feature_aggregations` mental model
— tweaking one group, the most common iteration, rebuilds only that group.
Cost accepted: shared parent-entity scans repeat per group. The exact
group⇄sub-config mapping belongs to the featurizer ER-graph section of the
adapter spec.

### 4.3 The cached DAG stops at models

Predictions are **append-only events** (ADR-0006) with native lineage columns
(`model_id`, `matrix_uuid`) — recording them as artifact rows would duplicate
lineage and fill the cache table with never-cacheable entries. Evaluations and
bias metrics are **cheap recomputable SQL** (ADR-0007), idempotent on their
primary keys. `triage.artifacts` therefore holds only expensive, materialized,
cacheable things.

### 4.4 One identity system

Derivation ids **replace** the inherited content hashes rather than living
alongside them (two identity systems would re-create the shallow-hash trap):

- `models.model_hash` := the model node's `artifact_id`.
- `matrices.matrix_uuid` := `uuid5(artifact_id)` (storage URIs keep a uuid);
  `matrices.artifact_id` carries the join back to the DAG.
- `cohorts.cohort_hash` := the cohort@(config, date) node's `artifact_id`.
- `triage.labels` gains `label_hash` (the labels node's `artifact_id`) **in its
  primary key** — fixing a latent hole: the previous PK had no label-definition
  discriminator, so two different label queries would have collided.
- `experiments.experiment_hash` stays a config hash — an experiment is a
  *request/root*, not a built artifact.

FK hardening between domain tables and `triage.artifacts` is deferred to the
GC pass (delete/cascade semantics depend on retention decisions).

### 4.5 Fit-based imputation lives inside the matrix node

Stored matrices are post-imputation; the imputation policy is part of the
matrix node's config; fitted train-split statistics persist as matrix metadata
for provenance. The **test matrix takes the train matrix as a parent** —
it consumes the fitted statistics (ADR-0009), so the leakage boundary is an
explicit DAG edge. Changing imputation policy rebuilds matrices but reuses all
cached per-date data nodes (a cheap join + fill).

### 4.6 Storage

`triage.artifacts` (id, kind, cacheable, canonical config, frozen pins, engine
versions, output_ref, status, run, timestamps) + `triage.artifact_inputs`
edges — DDL in schema-design.md §4.8, operations in `src/triage/artifacts.py`
(lookup/cache-hit, begin/mark-built/mark-failed, recursive-CTE closure and
dependents queries).

## 5. RESOLVED — Engine versions (2026-06-11, ADR-0016)

### 5.1 The compiler-vs-runtime criterion

A version enters identity iff it can change the artifact's **output bytes
given identical config and inputs**. Engines are *compilers* — their releases
change outputs by design: featurizer maps config → SQL (a boundary fix like
`>=`→`<` moves events in/out of windows, changing feature values); sklearn
gives different coefficients for the same (matrix, hyperparameters, seed)
across versions and guarantees no cross-version equivalence; triage-pg's
assembly/imputation logic determines matrix bytes. PostgreSQL and Python are
*runtimes* — semantically transparent by contract (SQL semantics are
standardized; Python behavior is captured by the hashed package versions).
Same judgment Guix makes: hash the compiler and recipe, not the kernel.

Documented residual risks accepted with the runtime exclusion: float
aggregation order under parallel plans, collation changes affecting text
ordering (the ranking path is already shielded by the deterministic
entity_id tiebreak, schema-design §8.3), and pickle load-compat across Python
minors (a load-time concern, not build identity). These are recorded at the
**run** level (`runs.triage_version`, `runs.git_hash`) for forensics.

### 5.2 Per-kind relevance map

| Kind | Engines in identity |
|---|---|
| cohort / labels | triage-pg |
| feature_group | triage-pg + featurizer |
| matrix | triage-pg |
| model | triage-pg + the estimator's distribution (e.g. scikit-learn) |

Implemented by `derivation.engine_versions_for(kind, estimator_class_path)`.
Invalidation propagates through DAG edges on its own: an sklearn bump rebuilds
models only; a featurizer bump rebuilds feature groups → matrices → models.

### 5.3 Release versions, not git hashes

Identity uses installed release versions (`importlib.metadata`). Git hashes in
identity would invalidate every cache on every commit — unusable while
developing triage-pg itself. The contract: a dev change that alters build
outputs requires a version bump (or an explicit `--force` rebuild);
`runs.git_hash` keeps the forensic trail.

### 5.4 Strict identity + opt-in logical fallback

Engine versions **always** hash — two artifacts built by different engines are
different artifacts. The configurable part is only what a miss caused purely
by engine drift means. Each derivation therefore carries two parallel Merkle
chains: the strict `id`, and a `logical_id` computed without engine versions
**over the parents' logical ids** (so drift anywhere upstream doesn't break
fallback matching transitively). `cache_hit(policy="exact")` is the default;
`policy="logical"` falls back to the latest built artifact with the same
`logical_id`, with a loud ENGINE-DRIFT REUSE warning naming both version sets.
Escape hatch for known-benign bumps and laptop↔cloud version skew — never the
silent default.

### 5.5 Considered alternatives (rejected)

- **No engine versions in identity** — after any engine upgrade the cache
  silently serves outputs of the old behavior; invisible wrongness vs the
  visible, bounded cost of a rebuild.
- **Git hash in identity** — purity at the cost of disabling caching during
  development.
- **Full environment manifest (uv.lock hash)** — any dependency bump, even a
  dev tool, invalidates the entire store.
- **Manual engine epoch counters** — relies on humans remembering; the failure
  mode pinning was designed to remove.

## 6. OPEN questions (next discussion rounds)

1. **GC roots and retention.** Experiments as roots; unreachable artifacts
   (especially Parquet matrices) collectible. Decides the deferred FK
   hardening (§4.4) and interacts with append-only predictions retention
   (ADR-0006: quarterly partitions, keep-forever default).
2. **Builder wiring.** Which code paths compute and record derivations — lands
   with the adapter implementation (the CONTEXT.md Adapter responsibility
   "derivations (cache keys)").
