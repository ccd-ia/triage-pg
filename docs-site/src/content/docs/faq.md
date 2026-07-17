---
title: FAQ
description: The questions users actually hit — 0-entity matrices, stale scores, adding an estimator, ignored feature groups, and the editable-install estimator error.
sidebar:
  order: 2
---

The questions that come up over and over, and where each answer lives in the
design. Every answer here traces to a concrete behaviour — an ADR, a CLI flag,
or a config rule — not folklore.

---

## Why did my run produce 0-entity (empty) matrices?

Almost always a **cache-identity leak**: the cohort and labels artifacts must
include the `as_of_dates` they were built for in their identity. If those dates
don't enter the hash, a later run over a *different* temporal grid can silently
cache-hit a cohort/labels artifact that was populated at other dates. The matrix
then inner-joins your entities against a cohort that is empty *for the dates you
asked about* — and you get zero rows.

This is exactly why forward scoring and retraining fold the single scoring date
back into the cohort/label config before building (see
`triage.adapters.forward.dated_config`): the experiment path pins its dates
upstream in the temporal config and deliberately excludes them from cohort/label
identity, but a one-off date is *not* derivable from the config, so it must enter
identity or the inner join comes back empty. If you're assembling artifacts
outside the normal `run` path, make sure the dates are part of what you hash.

See [identity and caching](/triage-pg/concepts/identity-and-caching/) and
[point-in-time correctness](/triage-pg/concepts/point-in-time-correctness/).

---

## Why isn't my latest score the latest?

Because predictions are **append-only** (ADR-0006). Every scoring run *inserts*
rows carrying a `scored_at` wall-clock timestamp; nothing is ever overwritten,
and the table is time-partitioned. Re-scoring the same model at the same
`as_of_date` doesn't replace the old row — it adds a new one. So `(model,
entity, as_of_date)` is **not** unique, and a naive read gives you whichever row
the planner returned, not the newest.

To read "current", pick the maximum `scored_at` per key:

```sql
select distinct on (model_id, entity_id, as_of_date)
       model_id, entity_id, as_of_date, score, scored_at
from triage.predictions
order by model_id, entity_id, as_of_date, scored_at desc
```

This is a concept to teach, not a bug: "a score is not the latest score." The
payoff is that prediction history, drift, and trajectories are already recorded
and become a `GROUP BY` later, with no migration. See
[the data model](/triage-pg/concepts/the-data-model/).

---

## I get `ValueError: Cannot resolve a distribution for estimator module 'triage'`

The estimator library's version enters **model identity** (ADR-0016), so
`engine_versions_for('model', …)` has to reverse-map the estimator's import
package to its installed distribution. It does that via
`importlib.metadata.packages_distributions()`, which reads `top_level.txt` /
`RECORD` — and a PEP 660 **editable install** (a plain `uv sync` of this project,
as in CI) frequently omits those, so triage-pg's own `triage.*` estimators
resolve to nothing even though the distribution *is* installed.

Current triage-pg handles this: when the reverse-map is empty it **falls back to
the module name** as the distribution name (which holds for `triage`, `sklearn`,
`sksurv`, …), producing the same `(name, version)` a populated reverse-map would.
If you hit this error, you're on an older build — update and re-run `uv sync`. See
[identity and caching](/triage-pg/concepts/identity-and-caching/).

---

## How do I add an estimator?

Put its **dotted `class_path`** under `grid_config`, with each hyperparameter as a
list — the run sweeps the cartesian product. Any `sklearn.*` estimator class
works directly, and triage-pg ships
`triage.component.catwalk.estimators.classifiers.ScaledLogisticRegression` (a
minmax-scaler + logistic regression, so the persisted coefficients sit on
comparable [0, 1]-scaled features):

```yaml
grid_config:
  'sklearn.ensemble.RandomForestClassifier':
    n_estimators: [10, 100]
    max_depth: [3, 5]
  'triage.component.catwalk.estimators.classifiers.ScaledLogisticRegression':
    C: [0.01, 1.0]
    penalty: ['l2']
    max_iter: [1000]
```

Any importable class with a scikit-learn-style `fit`/`predict` interface is fair
game — the class path is resolved at train time. See the
[configuration reference](/triage-pg/reference/configuration/).

---

## My feature group is being ignored.

`feature_groups` must be **nested under `feature_config`** (ADR-0023). A
top-level `feature_groups:` key at the root of the experiment config is not read
by the adapter — it's silently ignored, so you get today's default behaviour (one
implicit group, one Run) with no error. The config validator now emits a warning
when it sees a stray top-level `feature_groups`, but the fix is to move it inside
`feature_config`:

```yaml
feature_config:
  # … your feature definitions …
  feature_groups:
    group_by: source_entity
    strategies: [all, leave-one-out, leave-one-in]
```

The adapter strips `feature_groups` back out before featurizer (or the
`feature_group` node identity) ever sees it — featurizer stays group-agnostic
(ADR-0008); grouping is a triage-pg concern over featurizer's columns. See the
[configuration reference](/triage-pg/reference/configuration/).

---

## `triage score` scattered Parquet files into my cron directory.

Fixed. `--project-path` (the matrix output root) now **defaults to the model's
own artifact root** — the parent of its recorded `artifact_uri` — so a bare
scheduled line like `triage score --model-id 42 --as-of-date 2026-07-01` writes
its production matrix beside the model's existing artifacts, not into whatever
CWD the scheduler happened to hand it. The same default applies to
`retrainpredict`, so retrained artifacts land next to the originals.

Pass `--project-path` only when you actually want to redirect output somewhere
else. If you're seeing Parquets in your cron directory, you're on an older build
where the default was the CWD — update.

---

## The tutorial fails at `db upgrade` / can't connect.

Two usual causes:

1. **No connection file.** The CLI needs to know how to reach the tutorial DB.
   Create the git-ignored `dirtyduck-database.yaml` and pass it with `--dbfile`
   (the [Dirty Duckling tutorial](/triage-pg/tutorials/dirtyduckling/) has the
   exact contents). Without it, the CLI falls back to your ambient `PG*` env and
   connects to the wrong place — or nowhere.

2. **The DB is still loading.** The tutorial container does a first-boot data
   load that takes a few minutes; connecting before it finishes fails. Wait until
   Postgres is actually accepting connections:

   ```bash
   pg_isready -h 127.0.0.1 -p 5440
   # 127.0.0.1:5440 - accepting connections
   ```

   Only then run `uv run triage --dbfile dirtyduck-database.yaml db upgrade`. If
   port 5440 is taken, pick another and adjust `dirtyduck-database.yaml` to match.
