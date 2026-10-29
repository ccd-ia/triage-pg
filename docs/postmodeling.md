# Postmodeling diagnostics

ADR-0011 dissolved the standalone postmodeling module into three things: importances
persisted at train time, SQL views, and dashboard panels. This page is the operator's
map of what shipped where — every diagnostic is **headless-first** (ADR-0012): the CLI
and the dashboard read the same tables and functions.

## The data flow (the ADR-0011 pattern)

Matrices live in Parquet (local FS or S3), **not** in PostgreSQL — so the two
diagnostics that need feature *values* follow one pattern:

```mermaid
flowchart LR
    M[matrix.parquet\nfeature values] -->|reads once| C[triage postmodel\ncrosstabs / error-tree]
    C -->|persists| T[(triage.crosstabs\ntriage.error_analysis\nindividual_importances)]
    T --> D[dashboard model card]
    T --> Q[psql / triage model show]
```

Everything else (calibration, windowed rollups, list overlap, score histograms, Rayid
curves) needs only `(entity_id, score, label)` — which is already in PostgreSQL — and is
computed by SQL functions on read.

![One model's card: score distribution, Rayid curve, calibration deciles, crosstabs and error rules — every panel reads a persisted table or SQL function](images/model-sheet.png)

## What each surface answers

| Question | Surface | Where |
|---|---|---|
| Which model group should I pick? | audition ranking, 8 selection rules, regret + regret-next-time | `triage audition` · Audition tab · `triage.audition`/`audition_pick()` (migrations 0005/0013) |
| How do the groups compare at a glance? | avg ± σ, max regret, avg fit time per group | `triage models <hash>` · Model Groups tab |
| Is this split's model an outlier in its group? | window mean vs group avg ± σ (z-chip); the group sheet's band | model sheet / group sheet · `evaluations_windowed` ⋈ `triage.audition` |
| Is the model calibrated? | decile mean score vs realized rate | model card calibration panel · `triage model show` · `monitoring_calibration()` (migration 0012) · guide: [reading the model card](https://ccd-ia.github.io/triage-pg/guides/reading-the-model-card/) |
| What characterizes the selected top-k? | per-feature mean/std/nonzero-rate, selected vs rest | **`triage postmodel crosstabs`** → `triage.crosstabs` → model-card panel · guide: [reading the crosstabs](https://ccd-ia.github.io/triage-pg/guides/reading-the-crosstabs/) |
| *Where* does the model fail? | error-tree rules (fp: wrong flags; fn: missed need) with support + error rate | **`triage postmodel error-tree`** → `triage.error_analysis` → model-card panel · guide: [reading the error trees](https://ccd-ia.github.io/triage-pg/guides/reading-the-error-trees/) |
| What separates TP, FP, FN and TN? | one confusion tree over every labeled row, with each leaf's class counts | **`triage postmodel error-tree --kind confusion`** (or `all`) → `triage.error_analysis.class_counts` → model card's Confusion tab · guide: [reading the error trees](https://ccd-ia.github.io/triage-pg/guides/reading-the-error-trees/) |
| Is any leaf suspiciously right? | large leaves with almost no error (fp error ≤ 0.02, or confusion leaves ≥ 98% TP, ≥ 30 rows by default), with lift over `precision@` | `triage postmodel error-rules --suspicious` · the model card's "suspiciously right" chip · `error_rules_suspicious()` · guides: [reading the error trees](https://ccd-ia.github.io/triage-pg/guides/reading-the-error-trees/), [finding leakage](https://ccd-ia.github.io/triage-pg/guides/finding-leakage/) |
| Does a failure mode recur over time? | on how many of a model group's test dates each rule signature (features and split directions, no thresholds) appears | `triage postmodel error-recurrence <group>` · group sheet recurrence table · `error_rule_recurrence()` · guide: [reading the error trees](https://ccd-ia.github.io/triage-pg/guides/reading-the-error-trees/) |
| Why is THIS entity ranked high? | per-entity β·x contributions (linear models) | persisted by `error-tree` → `individual_importances` → `/models/{id}/entities/{id}/importances` |
| Do two models flag the same people? | top-k intersection, Jaccard, Spearman ρ | `triage postmodel compare` · compare modal · `list_overlap()` (migration 0016) · guide: [reading the model card](https://ccd-ia.github.io/triage-pg/guides/reading-the-model-card/) |
| Is the model fair enough for THIS intervention? | per-group disparities + τ verdicts + the fairness tree | Bias tab wizard · [`fairness.md`](fairness.md) (migration 0014) |
| Does a slice of the cohort fare worse? | every metric on a named subset (the subset is the population) | `evaluation.subsets:` config → subset selector on the heatmap (migration 0015) · guide: [reading the model card](https://ccd-ia.github.io/triage-pg/guides/reading-the-model-card/) |
| Does the model rank the whole population, or inside a subset? | precision and recall at every cut of one test date, against that population's base rate; a subset is ranked among its own members | `triage model curve <id> [--subset]` · the model card's curve and its population selector · `model_threshold_curve(model, as_of_date, subset_hash)` · guide: [reading the threshold curve](https://ccd-ia.github.io/triage-pg/guides/reading-the-threshold-curve/) |

## The error tree is a diagnostic, not a booster

"Predict on the errors" here means: fit a *shallow, readable* tree on the error
indicator so its leaves say **where** the model fails
(`inspections_1y <= 1.5 AND facility_age <= 2.5 → 80% of flags wrong`). Feed that into
feature engineering, cohort definitions, or intervention design. It is deliberately
**not** used to modify scores: a stacked error-corrector would blur model identity
(what is the hash of a model pair?) and invite test-set leakage. If an error signal is
strong enough to act on, promote it to a feature and retrain — through the normal,
governed pipeline.

## Recipes

```bash
# after a run — hash from the run output, model ids from `triage models <hash> -g <group>`
uv run triage postmodel crosstabs 42 -p 100_abs
uv run triage postmodel error-tree 42 -p 100_abs --depth 3 --min-leaf 30
uv run triage postmodel compare 42 57 -p 100_abs
uv run triage model show 42
```

Re-running any diagnostic upserts in place (idempotent). The dashboard panels show an
explicit empty-state naming the CLI command until a diagnostic has been persisted —
nothing is computed behind your back.
