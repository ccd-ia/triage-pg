# 0026. Survival: scikit-survival estimators, C-index in PL/pgSQL

- Status: Accepted
- Date: 2026-07-03
- Deciders: Adolfo (scope), Claude (recommendation per the v1-completion plan)
- Status update (2026-07-03): Implemented — `triage[survival]` optional extra (scikit-survival 0.27);
  survival fit branch in `adapters/model.py` (`Surv.from_arrays` on the `(duration, event_observed)`
  pair); `triage.survival_ranks` + `triage.c_index` + `survival_metrics` dispatch (migration 0011),
  validated against `sksurv.metrics.concordance_index_censored` to 1e-9 on randomized tied/censored
  fixtures (`src/tests/test_survival.py`); runnable configs `example/dirtyduck/experiment-survival.yaml`
  and `example/chicago311/experiment-survival.yaml`.

ADR-0010 made the label schema survival-ready and deferred the implementation. This ADR records
the implementation's two hard-to-reverse choices. (Three-criteria check: *hard to reverse* — the
estimator library binds the config surface every survival `grid_config` names, and the metric
locus bakes into the evaluation contract; *surprising without context* — evaluation living in the
database is already unusual, a pairwise survival statistic in PL/pgSQL more so; *real trade-off* —
two credible libraries and two credible metric homes existed.)

## Decision

**1. Estimator library: scikit-survival, as the optional `triage[survival]` extra.** Its
estimators are sklearn-compatible (`fit(X, structured_y)`, `predict(X)` → risk), so the existing
catwalk seam barely changes: one branch in `_fit_estimator` builds `Surv.from_arrays(event,
duration)`, detected by the estimator's package (`sksurv.*` in the grid's `class_path`) — no
`problem_type` threading into the model builder, whose identity inputs are unchanged. Cox's
`coef_` flows through the existing linear-importance persistence (ADR-0011). The extra is
import-guarded: `problem_type: survival` without it fails at config-validation/run-start naming
the install command, never mid-run.

**2. C-index locus: PL/pgSQL (`triage.c_index`, migration 0011).** Harrell's C is a *ranking*
metric over `(score, duration, event_observed)` — all in PostgreSQL — so it belongs with the rest
of evaluation (ADR-0007) and lands on the same spine as precision@k/AUC (ADR-0010: "the C-index is
itself a ranking metric"). Semantics match `sksurv.metrics.concordance_index_censored` exactly
(comparable pairs: earlier event vs any later time, or equal-time event-vs-censored; tied risk =
0.5; equal-time event/event pairs excluded), enforced by a mandatory randomized cross-check test.
Higher score = higher risk = ranked first is the recorded orientation convention.

**3. Metric selection joins the config**: an `evaluation` block (the `triage.evaluate_model`
jsonb shape) selects metrics per experiment; absent, the problem-type default applies —
classification keeps the inherited set, the regression family gets `rmse/mae/r2`, survival gets
`c_index`. `regression_ranking` deliberately does NOT default to precision@k: it presumes a
binary outcome the continuous target lacks; declare threshold metrics explicitly when the
outcome semantics support them.

## Considered alternatives

- *lifelines* — rejected: DataFrame-first API would re-introduce pandas at the fit seam ADR-0019's
  cleanup just narrowed; scikit-survival's structured-array API drops into the numpy seam as-is.
- *C-index computed in Python at scoring time* — rejected as the default (evaluation lives in PG,
  ADR-0007; recomputability from the predictions table would be lost), but **recorded as the
  escape hatch**: the set-based pair join is O(n²) per (model, date) — measured fine at eval sizes
  (10⁵ predictions/split ⇒ a bounded self-join on an indexed working set), and if a client-scale
  cohort ever blows past it, a Python `concordance_index_censored` call writing the same
  `evaluations` row is a contained swap.
- *A survival-specific predictions/score column* — rejected: the risk score IS the ranking score;
  ADR-0006/0010's append-only `predictions.score` carries it unchanged.

## Consequences

- New optional dependency surface: `scikit-survival` (+ its `osqp` solver chain) only under the
  `survival` extra; core installs unaffected.
- Migration 0011 CREATE-OR-REPLACEs `evaluate_model` (adds the `survival_metrics` loop); its
  downgrade restores the 0002 body verbatim.
- The dashboard's metric catalog is data-driven, so `c_index` appears once evaluated — no UI
  schema change.
- Survival's deeper kit (Brier score, calibration-in-time, time-varying covariates) remains
  future work; the spine + C-index is the v1 contract.
