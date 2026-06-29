# 0015. Artifact DAG nodes: per-date data layer, stops at models, one identity

- Status: Accepted
- Date: 2026-06-11
- Status update (2026-06-28): Implemented — per-(config, as_of_date) cohort/labels/feature nodes, matrices per split-side, models the last cached node; cross-run cache reuse covered by the run_experiment E2E.

The derivation DAG (ADR-0013) gets these nodes and no others: cohort and labels
per **(config, as_of_date)**, features per **(feature group, as_of_date)** —
a feature group being an *adapter-defined sub-config* of the featurizer ER
graph, since featurizer itself is monolithic per run — matrices per split-side
(with the **test matrix taking the train matrix as a parent**, because it
consumes the train-fitted imputation statistics, ADR-0009), and models per
(class, hyperparameters, seed) × train matrix. The cached DAG **stops at
models**: predictions are append-only events with native lineage columns
(ADR-0006) and evaluations are recomputable SQL idempotent on their PKs
(ADR-0007). Derivation ids **replace** the inherited content hashes
(`model_hash` := artifact id, `matrix_uuid` := uuid5(artifact id),
`cohort_hash` := the per-date node id, and `labels` gains a `label_hash` PK
column — fixing a latent collision: the old PK carried no label-definition
discriminator) rather than living alongside them, so the shallow and deep
identities can never disagree.

## Considered alternatives
- *Per-experiment nodes* — rejected: no reuse across config iterations or
  experiments; any change rebuilds everything.
- *Full-config × date-set feature nodes (mirror featurizer's monolithic run)*
  — rejected: a one-group tweak or one-date extension rebuilds all features,
  worsening the ADR-0008 scale risk instead of mitigating it.
- *Nodes all the way down (predictions/evaluations as artifacts)* — rejected:
  duplicates lineage the domain tables already carry; fills the cache table
  with never-cacheable rows.
- *Parallel identity column alongside the inherited hashes* — rejected:
  two identity systems re-create the exact trap ADR-0013 removes.

## Consequences
- timechop's overlapping splits become cache hits; extending a date range
  builds only new dates.
- The adapter spec must define the feature-group ⇄ featurizer-sub-config
  mapping; shared parent-entity scans repeat per group (accepted cost).
- ~20k artifact/edge rows for a substantial experiment — negligible for PG.
- FK hardening between domain tables and `triage.artifacts` is deferred to the
  GC pass (delete semantics depend on retention).
