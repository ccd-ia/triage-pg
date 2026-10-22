# 0033. Threshold metrics rank the full population

- Status: Accepted — **Implemented** 2026-10-22 (migration `0024_population_topk`, issue #21)
- Date: 2026-10-22
- Deciders: Adolfo De Unánue

## Context

`triage.labeled_ranks` filtered `outcome is not null` before `row_number()`, and
`resolve_k` turned `N_pct` into a count of labeled rows. So `precision@100_abs` meant
"the top 100 among the entities that have a label".

DSSG triage ranked every scored test row, applied the threshold, and only then dropped
the rows whose label was NaN (`catwalk/evaluation.py`, `_compute_evaluations`). Its
`precision@100_abs` meant "of the 100 entities we would act on, the share of the labeled
ones that turned out positive", reported with `num_labeled_above_threshold`.

The two definitions agree when every test row has a label. They differ under selective
labels (`task_framing: resource_prioritization`), where an entity has no label because
nobody inspected it. On the DirtyDuck tutorial, 41–47% of each test split is labeled, and
1,042 of its 1,048 stored threshold rows moved when it was recomputed. The largest move was
0.26.

Migration 0007 had moved the dashboard curve onto labeled-only ranks. Its fault was mixed
denominators: a population rank was divided by a labeled total, so `pct` reached 228%. The
population rank itself was not the problem. Meanwhile, the diagnostics (`top_k_entities`,
crosstabs, the error tree's cut) already ranked the full population, so two parts of
triage-pg disagreed on what "top-k" meant.

## Decision

A top-k cut ranks **every scored row of the population**. That is the full test cohort, or
the subset when one is given, unlabeled members included. Metrics that compare a
prediction with a label count only the labeled rows inside or outside the cut.

- `triage.population_ranks` is `labeled_ranks` with a left join to the labels.
  `precision_at_k`, `recall_at_k`, `compute_bias_metrics`, `model_threshold_curve` and the
  error tree use it.
- `k` resolves against the population size. **`ceil` is kept.** DSSG used `int()`, so a
  percentage threshold can select one more row than DSSG did.
- Precision is TP over the labeled rows inside the cut. That denominator is stored in
  `evaluations.num_labeled_above_threshold`. Precision is NULL when the cut holds no labeled
  row.
- `value_expected`/`value_std` are the exact moments under random ranking. Labeled rows in
  the cut are hypergeometric, and precision is conditional on at least one.
  `value_worst`/`value_best` stay analytic extremes over the boundary tie block. They are
  not DSSG's `na_position` sort conventions.
- Bias `group_size`, `num_selected` and `selection_rate` count every row. Precision, FDR,
  FOR and NPV divide by labeled rows.
- Rank-free metrics (AUC, average precision, Brier, log loss, ECE, the regression metrics,
  the C-index, calibration) keep `labeled_ranks`.
- Migration 0024 recomputes every stored threshold and bias row in place, so no table mixes
  the two definitions.

## Consequences

Numbers reported by triage-pg and by DSSG triage are comparable for selective-labels
problems, up to the rounding of percentage thresholds. Every stored precision@/recall@ value
moves for any project whose test rows are not all labeled. The Upgrading note of v1.3.0
says so.

A top-k that holds many unlabeled rows now shows it: `num_labeled_above_threshold` far
below `k` means the precision rests on few labels, which the labeled-only definition hid by
construction.

A recomputed bias row keeps its stored reference group as an explicit pin, because group
sizes now include unlabeled rows and "largest group" could change. Downgrading 0024
restores the functions, not the old values.
