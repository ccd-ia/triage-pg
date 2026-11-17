# 0037. Group-level reads take the latest Run's model on each split

- Status: Accepted
- Date: 2026-11-16
- Deciders: Adolfo De Unánue

## Context

A model group is identified by the estimator, its hyperparameters and the feature list (`_model_group_hash` in `adapters/model.py`). A model's identity covers more: the train matrix, the seed and the engine versions (ADR-0013, ADR-0016). ADR-0022 puts features, grid, imputation, source pins and the target-history keys in the Run. So a second Run of one Experiment that changes only those inputs fits a new model inside each existing group, on the same splits.

Issue #69 found this with a changed `history_query`. On the DirtyDuck database (2026-11-16), the same thing had already happened 116 times in two Experiments with no history at all. In 68 of those cuts the two models differed only by triage-pg version (1.1.x, 1.3.2 to 1.5.0). In the other 48 they came from two `feature_config`s that produce the same feature list. So upgrading triage-pg and re-running an Experiment was enough. The common-rows functions (`triage postmodel baseline`, `compare-rows`, `plot segments`, `plot win-consistency`) refused a group with two models on one date. Audition counted every model as a split, without warning, and averaged the old and new attempts.

## Decision

The view `triage.current_models` keeps one model per Experiment, group, train end, label timespan and forecast quantile. That model is the one from the latest Run of the Experiment that built or reused it, where reuse is a cache hit recorded in `run_artifacts`. A completed Run outranks a started or failed one, so a re-run in progress or a failed one does not replace the previous attempt. Ties go to the higher `model_id`.

Readers that aggregate a group over its dates read the view: audition, the leaderboard, `model_group_summary`, the coverage and common-rows functions, `entity_score_history`, `error_rule_recurrence`, `latest_model`, and the Python readers over them (group curves, error-tree leaves, a group's models, baseline comparison, structure). Per-model reads, lineage, bias rows, monitoring windows, the metric audit and `experiment_actuals` (a count of what was built) still read `triage.models`. The superseded models and their predictions stay in the database, because predictions are append-only (ADR-0006).

The refusal in the common-rows functions stays. It now fires only when two current models of one group score the same date, which needs two splits whose test dates overlap.

### Alternatives considered

- **Add the target-history keys to the group identity.** This fixes the history case only. A triage-pg upgrade would still produce two models per date. Adding engine versions as well would split every group at every release, and groups could no longer be compared across releases.
- **Keep refusing, with a message that names `triage reset --experiment`.** Audition would still average the two attempts without warning.

## Consequences

- An Experiment's audition and leaderboard describe its latest attempt on each split. To compare an earlier attempt with a later one, read per Run (`runs.plan`, the per-model views).
- `latest_model` (used by `selected_model` and forward scoring) now picks the latest completed Run's model at the latest train end. Before, it took the highest `model_id`.
- The leaderboard materialized view is rebuilt by migration 0039 and refreshed there.
- Whether a changed `history_query` should also make a new group (so the two forecasters are listed side by side) is left open. It can be added later without changing this rule.
