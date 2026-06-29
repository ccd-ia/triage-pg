# 0022. An Experiment is the problem (cohort+label+temporal); runs are attempts

- Status: Accepted
- Date: 2026-06-26
- Deciders: Adolfo, Claude
- Status update (2026-06-28): Implemented — `experiment_hash_for()` / `_problem_identity` in `adapters/run.py`; re-hash landed and the food DB re-seeded to one Experiment (commit 31539249, 2026-06-26).

`experiment_hash` was a content hash over the **whole** config (cohort + label + temporal +
features + grid + imputation, minus cosmetic name/description — ADR-0001, `adapters/run.py`).
So adding a feature or another model spawned a **new experiment**, and the 2026-06-25
cosmetic-strip fix (`_strip_cosmetic` dropping source `role`/`description`) was a symptom of the
same wrong cut: identity was tracking *display* and *modeling-effort* details, not the thing the
user is actually studying. Concretely, the four DirtyDuck "experiments"
(Cobalt-Euler-4 / Quartz-Galois-83 / Marble-Darwin-21 / Cobalt-Noether-17) all hash to **one**
`(cohort, label, temporal, problem_type)` — they are one problem attacked four ways
(different feature vocabularies + grids), not four experiments.

## Decision

**An Experiment is the prediction problem and its evaluation protocol:**
`experiment_hash = sha256(canonical_json(cohort_config + label_config + temporal_config +
problem_type))`. That triple fixes the matrix **rows** (cohort), the **target** `y` (label,
which carries `problem_type`), and the **train/test splits** (temporal). Everything else —
**feature_config (X), grid_config (models), imputation_config (preprocessing)** — is *how you
attack the problem* and belongs to the **Run**. A run records its varying config in `runs.plan`
so runs are distinguishable and reproducible. Re-running the identical problem+attempt is a
cache-hit replay (a second run that builds nothing); re-running the same problem with new
features or models is a new run of the **same** experiment.

This is chosen because the triple is exactly the **fair-comparison invariant**: with cohort,
label, and temporal fixed, every run evaluates on the *same* `y` over the *same* test splits, so
precision@k / AUC are directly comparable across runs. The experiment's audition/leaderboard
therefore reads as "the best attack on *this* problem, across every feature set and algorithm
tried" — which matches how the problem is actually iterated. Changing the cohort or temporal
config *is* a different problem (correctly a new experiment).

Artifact/derivation identity is **unchanged**: each artifact (cohort, labels, feature_group,
matrix, model) keeps its full-input-closure content hash (ADRs 0013–0017), and the
reproducibility guarantee is still at the artifact level. Only the human-facing **Experiment**
grouping moves up one level — from "a whole config" to "a problem with many runs."

## Consequences

- `experiment_hash_for` (`adapters/run.py`) hashes only the problem triple; the per-run
  feature/grid/imputation config is recorded on `runs.plan.attempt`. The old `_strip_cosmetic`
  machinery (name/description/source-role stripping) is **removed** — superseded by hashing the
  problem directly, which never includes those keys in the first place.
- **Source/data pinning is run-level, not experiment-level.** A source's `version_label` (which
  loaded snapshot) is NOT part of the experiment identity: re-running the same problem on newer
  data is the SAME experiment, a new run (the ADR-0006 monitoring use case). The pins still bind
  *artifact* identity (ADR-0014) and are recorded per run (`run_source_pins`).
- Experiment-scoped dashboard views (model_group_summary, evaluations, audition, the
  experiment-scoped model-group panel from M2) now aggregate **across runs** of the problem.
  Each model_group still encodes its `feature_list`, so a model's run/feature-set provenance
  stays visible — the one thing not to lose when comparing across runs.
- A **one-time re-hash / re-seed** is required: existing `experiment_hash` values were computed
  over the full config and won't match. We are greenfield/pre-v1, so this is acceptable; the
  food DB re-seed collapses the four rows into one experiment with four runs, which makes the
  2026-06-26 Marble→Cobalt-Noether merge unnecessary.
- This supersedes the implicit "experiment = full config" identity of ADR-0001. `CONTEXT.md`
  gains a distinct **Run** glossary term and a reworded **Experiment**.
- A run must persist enough of its feature/grid/imputation config to reproduce its models;
  `runs.plan` (jsonb, ADR-0021) is the home for it.
