# 0010. problem_type switch on a ranking spine; survival-ready labels

- Status: Accepted
- Date: 2026-06-04

triage-pg's architecture is a problem-type-agnostic **ranking/prioritization spine**: produce a score → rank entities → evaluate the ranking. A `problem_type` discriminator on the experiment selects three swaps on that spine — how to rank, the ranking metric, and the label shape — supporting **classification** (rank by P(y=1); AUC, precision@k), **regression-as-ranking** (rank by predicted value; precision@k + RMSE), and **pure regression** (RMSE/MAE/R², ranking incidental). Regression-as-ranking is the primary mode for continuous targets.

The greenfield **label schema is designed survival-ready now**: it carries optional `(duration, event_observed)` columns (nullable for classification/regression) alongside `outcome`, so **survival** can be added later as a bolt-on problem_type (rank by predicted risk/hazard; C-index) **without a schema migration** — cheap insurance, since survival-as-ranking (time-to-recidivism, time-to-eviction, …) is squarely in the target domain. The C-index is itself a ranking metric, so survival lands on the same spine as the others.

## Considered alternatives
- *Classification only (current triage)* — rejected: regression and survival are explicit goals.
- *Single-`outcome` label, migrate for survival later* — rejected: the schema is greenfield now; two nullable columns + the discriminator make survival a bolt-on, not a migration.

## Consequences
- Survival remains a substantial future build (censoring-aware label generation + evaluation, survival estimators, C-index/Brier metrics); the *schema* is ready, the *implementation* is deferred.
- The PL/pgSQL metric set (ADR-0007) is organized by problem_type.
- Binary triage discarded censoring information ("no event in window" = 0); the survival-ready label preserves it for the future survival path.
