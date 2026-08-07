# 0031. SHAP interpretability is TreeSHAP-only, on demand, into `individual_importances`

- Status: Accepted
- Date: 2026-08-07
- Deciders: Adolfo (call), Claude (recommendation + recon)

ADR-0011 deliberately deferred SHAP-level interpretability at v1.0.0, dissolving
postmodeling into train-time importances + SQL views + dashboard panels; v1.2.0 was
picked, during the v1.1.0 semver discussion, as the target to pick SHAP back up. The
obvious precedent — ADR-0011's train-time importances — does not scale here:
`triage.individual_importances` (present since migration 0001, PK `(model_id,
entity_id, as_of_date, feature, method)`, `results_schema/versions/0001_initial_triage_schema.py:290-299`)
already has a `method` discriminator built for exactly this, but persisting SHAP for
the whole cohort at train time is ≈22k entities × 147 features × 32 models ≈ 100M rows
on the DirtyDuck tutorial alone. `diagnostics/error_tree.py` (~217, 278) already
writes per-entity attributions on demand, via `triage postmodel`, as
`method='linear_contrib'` rows — computed only for selected entities, never at train
time. This ADR fixes the v1.2.0 scope before implementation starts. (Three-criteria
check: *hard to reverse* — storage shape, compute trigger, and estimator scope fix
what the v1.2.0 implementation plan builds against; *surprising without context* — a
reader following ADR-0011's "importances at train time" precedent would reasonably
expect SHAP to persist per-cohort-per-model too, which the cardinality above rules
out; *real trade-off* — persist-at-train, a new table, and KernelSHAP were all live
options and each was rejected for a specific reason.)

## Decision

v1.2.0 SHAP interpretability is scoped as: **TreeSHAP-only, computed on demand,
persisted into the existing `triage.individual_importances` table, behind an optional
extra.**

1. **Storage** — new rows with `method='shap_tree'` in the existing
   `individual_importances` table. No migration, no new table.
2. **Compute trigger** — on demand, via a new `triage postmodel` subcommand, for
   selected/top-k entities — the same pattern `error_tree.py` already uses for
   `linear_contrib`. Never computed at train time, never for the full cohort.
3. **Estimator scope** — TreeSHAP only, for the tree family already in the real grids
   (`DecisionTreeClassifier`, `RandomForestClassifier`). `ScaledLogisticRegression`
   keeps its existing exact β·x `linear_contrib` path (attribution is already exact
   there; SHAP would be redundant). Dummy/heuristic baselines have no meaningful
   attribution. Any unsupported estimator fails loud ("not supported") — never a
   silent skip.
4. **Dependency** — `shap` ships in a new optional extra, matching the existing
   `baselines`/`cloud`/`survival`/`oidc` pattern that keeps the base install lean.

## Considered alternatives

- **Persist-at-train for the whole cohort**, extending ADR-0011's importances
  precedent directly. Rejected: the cardinality class is different from a
  per-model importance vector — ~100M rows per tutorial DB, matrix-sized compute per
  model, for a quantity most users will only inspect for a handful of entities.
- **A new dedicated table** for SHAP values. Rejected: the existing PK
  `(model_id, entity_id, as_of_date, feature, method)` already fits; base-value and
  additivity handling are implementation details for the v1.2.0 plan, not schema
  drivers.
- **KernelSHAP for model-agnostic coverage** (Dummy/heuristic baselines included).
  Rejected for v1.2.0: ≈1000× the compute of TreeSHAP plus open background-dataset
  design questions. Left as a separate, later decision on its own merits if
  model-agnostic coverage is ever needed.

## Consequences

- No schema migration required; `individual_importances` gains a `method='shap_tree'`
  value alongside `linear_contrib`.
- The `triage postmodel` surface grows a subcommand; the base install stays lean since
  `shap` is opt-in.
- Non-tree estimators report SHAP as unsupported rather than silently producing
  nothing or a misleading approximation.
- This ADR records scope only — no SHAP code exists in the tree yet. Implementation is
  the v1.2.0 plan's job; the post-v1.1.0 close-out plan's Phase 6 gate is
  `git diff --stat` showing no changes under `src/triage/component/catwalk/`.
