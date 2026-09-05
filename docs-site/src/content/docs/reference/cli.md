---
title: The command line, workflow by workflow
description: The triage CLI is the complete product — every surface, with real output.
sidebar:
  order: 3
  label: CLI tour
---

The CLI is not a companion to the dashboard — it's the **complete product**
(headless-complete core). Everything below reads the same SQL views
the dashboard renders. All output shown is real, captured against the
tutorial databases.

Two ergonomics used throughout:

- **connection resolution**: `--dbfile <yaml>` › `database.yaml` in cwd ›
  `DATABASE_URL` › `PG*` env vars. The startup log prints the resolved URL
  with the password masked.
- **hash prefixes**: anywhere a command takes an experiment or artifact hash,
  a git-style unique prefix works (`b9e38fd8` for
  `b9e38fd8f366…`).

## Sanity

```console
$ uv run triage --version
triage-pg 1.1.6
```

## Set up a project database

```console
$ triage db upgrade          # alembic → the triage schema, idempotent
Database upgraded.
```

(`triage db history|stamp|downgrade` for the rest of the alembic surface;
`triage project create|grant|drop|list` for registry-managed one-database-per-project
lifecycles.)

`project create --owner <role>` and `project grant <slug> --owner <role>` are cloud-profile
only: they grant a per-project PostgreSQL role the database and hand it **ownership** of the
`triage` matviews, which `REFRESH MATERIALIZED VIEW` requires and no grant can confer. The
local profile needs neither — you already own what you create. Both commands also take a
repeatable `--data-schema <name>` extending the same grants to your own schemas (whatever
you name them): `create` creates them in the fresh database ready to load into, while
`grant` requires them to exist (it is the repair tool — a typo must not silently provision
an empty schema). Ownership of your data objects is never transferred. See
[the cloud runbook §4.0](https://github.com/ccd-ia/triage-pg/blob/main/docs/cloud-runbook.md).

## Validate before running

```console
$ triage analyze-config example/dirtyduck/experiment.yaml
  Statistic                              Value
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Config Version                            v8
  Problem type                  classification
  Feature columns                          147
  Cohorts                                    1
  Temporal splits                            4
  Distinct as_of dates                       5
  Label timespans                     6 months
  Matrices to build      8  (4 train + 4 test)
  Model grid size                            8
  Model groups                               8
  Feature-group runs                         1
  Models to be trained                      32
╭──────────── Label Configuration ────────────╮
│ Label name: failed_inspections              │
│ SQL: select entity_id, bool_or(result =     │
│ 'fail')::integer as outcome …               │
╰─────────────────────────────────────────────╯
```

Three of these counts scale differently, and the difference is the whole point
of reading this table before committing a grid:

| count | formula | does a fan-out multiply it? |
|---|---|---|
| **Matrices to build** | `2 × splits` | **no** |
| **Model groups** | `grid × runs` | yes |
| **Models to be trained** | `grid × splits × runs` | yes |

**Models to be trained** is the number to budget against. The grid is trained
once per train matrix, and there is one train matrix per split *per feature
subset* — so a three-way fan-out over a 32-model experiment is 96 fits, not 32.

**Matrices** is the one that does *not* multiply: featurizer runs once per split
and every subset is a column projection of the same Parquet file, so a 4-run,
4-split fan-out builds 8 matrices, not 32. **Model groups** do multiply, because
the feature list is part of a group's identity — which is what makes a fan-out's
leaderboard comparable, the same estimator on different features being a
different group tracked across splits.

All of this comes from the config alone; no database is touched.

The same validator backs the webapp's submission form — errors come back
path-addressed (`temporal_config.…`, `label_config.query`).

### The fan-out, and the baselines it can break

When `feature_config.feature_groups` declares a partition and a strategy, the
command prints each run the experiment expands into, and cross-checks every
name-pinned baseline against the columns that run will have:

```console
$ triage analyze-config experiment-fanout.yaml
  Matrices to build      8  (4 train + 4 test)
  Model grid size                            8
  Model groups                              24
  Feature-group runs                         3
  Models to be trained                      96

  Run                           Groups                Columns   Models
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  all                           facility_attrs,           147       32
                                inspection_history
  leave-one-out:facility_att…   inspection_history        120       32
  leave-one-out:inspection_h…   facility_attrs             27       32

╭──────────── Baseline pre-flight ────────────╮
│ BaselineRankMultiFeature pins               │
│ COUNT(inspections.result) — absent from run │
│ leave-one-out:inspection_history            │
╰─────────────────────────────────────────────╯
```

The DSSG baselines (`BaselineRankMultiFeature`, `SimpleThresholder`,
`PercentileRankOneFeature`, `LinearRanker`) select their feature columns **by
name**, while a `leave-one-out` sweep exists precisely to *remove* groups. Drop
the group holding the pinned column and that run dies with
`BaselineFeatureNotInMatrix` — after its matrix is already built. Pin the
baseline to a column present in every run, or narrow the strategies.

**`triage run` refuses such a config outright**, before building anything:

```console
$ triage run experiment-fanout.yaml
BaselinePreflightError: grid_config declares baseline(s) that select feature
columns BY NAME, and the feature_groups fan-out removes those columns from at
least one run:
  - BaselineRankMultiFeature pins COUNT(inspections.result) — absent from run
'leave-one-out:inspection_history'
```

This is a deliberate all-or-nothing choice. Letting the run proceed would build
and train the runs that *do* work, then die on the one that cannot — leaving you
with two-thirds of a grid and a traceback. Refusing costs a one-line config edit
and gives you all the runs. If the baseline names a column `feature_config` never
produces at all, the message says that instead: no strategy would help, the name
is wrong.

### `--estimate` — the data behind the config

Everything above is config-only. `--estimate` additionally counts what the
cohort and label queries would produce, by rendering the same templates the
builders render and wrapping them in a counting projection:

```console
$ triage --dbfile dirtyduck-database.yaml analyze-config \
    example/dirtyduck/experiment.yaml --estimate

  as_of_date   timespan   cohort   labels   labeled   base rate
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  2015-01-01   6 months   11,279    6,382     6,382      0.2490
  2015-07-01   6 months   12,289    6,755     6,755      0.2446
  2016-01-01   6 months   13,363    7,394     7,394      0.2713
  2016-07-01   6 months   13,859    6,978     6,978      0.2710
  2017-01-01   6 months   14,261    7,284     7,284      0.2769

cohort rows: 65,051   label rows: 34,793
```

The last column is the base rate for `classification`, the event rate for
`survival`, and the mean target for the regression types. This runs one query
per as_of_date, so `--estimate-dates N` samples the first N instead (the output
says so, because a sampled total is not a total).

`--features GLOB` answers a different question: which feature columns a
`feature_groups.definitions` glob would actually catch. It matches by the same
rule partitioning uses — each column's full label as well as its physical name —
so what it prints is what the run would group. Needs no database.

```console
$ triage analyze-config example/dirtyduck/experiment.yaml --features '*(inspections.*'

120 of 147 match '*(inspections.*'  (0 truncated)
  ABS(facilities.COUNT(inspections.date))
  ABS(facilities.COUNT(inspections.date|interval=P1M))
  …
```

Use `--features '*'` to list every column. Where the 63-byte identifier cap
truncated a name, the output shows `label → column` so you can see what the
feature really is.

## Run

```console
$ triage run example/dirtyduck/experiment.yaml --project-path /tmp/dirtyduck-run
…
Experiment b9e38fd8f366… completed: 1 run(s), 20 model(s), 268860 prediction(s),
120 evaluation(s).
storage: /tmp/dirtyduck-run
```

Re-running is always safe: artifacts are content-addressed, so unchanged
stages cache-hit and the run resumes where inputs actually changed.

## Read results

```console
$ triage leaderboard b9e38fd8
  Group   Model   Algorithm              Metric    As-of        Value
  5       20      ScaledLogisticRegre…   auc_roc   2017-01-01   0.5751
  4       19      ScaledLogisticRegre…   auc_roc   2017-01-01   0.5748
  …

$ triage models b9e38fd8
  Group   Algorithm            Models   Avg ± σ           Max regret   Avg fit
  5       ScaledLogisticReg…   4        0.5850 ± 0.0279   0.0118       0.8s
  4       ScaledLogisticReg…   4        0.5823 ± 0.0162   0.0207       0.1s
  …
```

`triage models <hash> --group N` drills into one group's members;
`triage model show <id>` prints a model's card with calibration deciles.

## Select a model

```console
$ triage audition b9e38fd8
  Group   Splits   Avg ± σ           Dist. from best (avg)   Max regret   Regret next time (max)
  5       4        0.5850 ± 0.0279   0.0032                  0.0118       0.0118
  4       4        0.5823 ± 0.0162   0.0060                  0.0207       0.0207
  …
```

The DSSG selection rules over the in-PG audition views: pick for stability
across splits, not one lucky cell. `--json` on the read commands emits
machine-readable output for scripting.

## Diagnose

```console
$ triage postmodel crosstabs 20 -p 100_abs
441 crosstab row(s) persisted.
  As-of        Feature                            Selected   Rest     Ratio
  2017-01-01   facilities.zip_code=60622          0.6800     0.0280   24.32
  2017-01-01   facilities.facility_type=mobile…   0.0300     0.0056   5.38
  …
```

Crosstabs answer "what characterizes the top-k?"; `triage postmodel
error-tree <id>` fits a shallow interpretable tree on the model's mistakes
("where does it fail?"); `triage postmodel compare <a> <b>` computes list
overlap. Computed once from the matrix, persisted to PostgreSQL, readable
everywhere.

## Operate

```console
$ triage score 20 2019-12-01
Forward-scored model 20 at 2019-12-01 (append-only).
```

The monitoring entrypoint — schedule it with cron or EventBridge;
each invocation appends `scored_at`-stamped predictions and the monitoring
views (drift, volume, realized outcomes) accumulate. Bookkeeping surfaces:
`triage source list` (version pins), `triage archive <hash>` (soft-archive an
experiment), `triage gc` (collect artifacts unreachable from any root),
`triage runs status` (AWS Batch backfill in the cloud profile).

## Where next

- [Architecture](/triage-pg/reference/architecture/) — the tables these
  commands read and write.
- [The dashboard tour](/triage-pg/reference/dashboard/) — the same surfaces,
  rendered.
- The [Dirty Duckling smoke test](/triage-pg/tutorials/dirtyduckling/) to run
  this end to end yourself.
