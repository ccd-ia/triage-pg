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
triage-pg 1.5.0
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
  Feature columns                          107
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
  all                           facility_attrs,           107       32
                                inspection_history
  leave-one-out:facility_att…   inspection_history         80       32
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

80 of 107 match '*(inspections.*'  (0 truncated)
  ABS(facilities.COUNT(inspections.date))
  ABS(facilities.COUNT(inspections.date|interval=P1M))
  …
```

Use `--features '*'` to list every column. Where the 63-byte identifier cap
truncated a name, the output shows `label → column` so you can see what the
feature really is.

### `triage check leakage` — definitions that read the future

`analyze-config` reads the config; `triage check leakage` runs the cohort, label
and feature definitions against the project database and tests each one for
data it should not know yet. For every as_of_date `t` of the
temporal config, it builds all three on all data, deletes the rows of the
config's `sources` that each one must not see, builds them again and compares:

| component | rows deleted before the rebuild |
|---|---|
| cohort at `t` | rows known after `t` |
| features at `t` | event rows known on or after `t` |
| labels at `t` | rows known on or after `t + label_timespan` |

```console
$ triage check leakage example/dirtyduck/experiment-guides.yaml
cohort — entities added
      or removed

  as_of_date   result
 ━━━━━━━━━━━━━━━━━━━━━
  2015-01-01   ok
  …
  2016-10-01   ok
…
Checked 7 as_of_date(s) in 10.7s: no differences (exit 0).
```

The check reports each difference by component and date. The counts are rows
that appear after the cut (`+`), rows that disappear (`−`) and rows whose value
changed (`~`); for features, the result also names the columns that moved. In a
planted variant of the same config, the label query has no upper bound on its
window, so it reads inspections after `t + 6 months`, and the labels differ on
every date:

```console
  as_of_date   label timespan   result
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  2015-01-01   6 months         12231 differ (+0 −10390 ~1841)
  …
  2016-10-01   6 months         6488 differ (+0 −5860 ~628)
Checked 7 as_of_date(s) in 11.1s: leakage found in 7 check(s) (exit 1).
```

The exit code is 0 when nothing differs, 1 when something does (leakage), and
2 when the check could not run: a missing privilege or Source, a broken foreign
key, a lock held by another session, or a bad config. `--dates 2016-07-01,2016-10-01`
checks only those dates; `--json PATH` also writes the full report.

**The check never commits.** It runs on one connection, in one transaction that
always ends in `ROLLBACK`, so other sessions never see the deletes, and it
compares each Source's row count and latest knowledge date before and after the
run. It still needs privileges the read commands do not: `DELETE` on every
Source that declares a `knowledge_date_column`, and `TEMPORARY` on the database.
A role with `SELECT` only gets exit code 2 and a message naming the relations.
The deleted rows stay locked until the rollback, so run it when no loader is
writing to the Sources.

The check does not cut Sources without a `knowledge_date_column`, or tables the
queries read that the config does not declare; it lists both under **Not
checked**. Exit 0 with that list non-empty does not mean the config is clean.
[How to find leakage](/triage-pg/guides/finding-leakage/) covers what the check
can and cannot see.

### `triage check history` — gaps in the target history

A config with a `history_query` feeds the seasonal, Holt–Winters and Croston
baselines a series placed by calendar offset, one slot per `history_period`.
`triage check history` renders that query at the given dates, places each row
on the same grid the matrix build uses, and counts what would stop the run.
It builds nothing and writes nothing.

```console
$ triage check history example/dirtyduck/experiment-regression.yaml
history_period '6 month', width 8, as_of_date(s) 2017-01-01

  check                              count
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  series (entity × as_of_date)      18,909
  series with missing periods            0
  missing periods in all                 0
  rows off the period grid               0
  rows at or after the as_of_date        0
```

The tutorial's query returns a row for every six-month period, zeros included.
A copy of the config with the monthly query DirtyDuck used before 1.6.0, a
`group by` over the inspections with `history_period: '1 month'` and a width
of 24, has a gap wherever a facility had no inspection that month:

```console
$ triage check history experiment-regression-sparse.yaml
history_period '1 month', width 24, as_of_date(s) 2017-01-01

  check                               count
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  series (entity × as_of_date)       14,537
  series with missing periods        14,320
  missing periods in all            208,804
  rows off the period grid                0
  rows at or after the as_of_date         0

 entities with the most missing
             periods

  entity_id   missing   present
 ───────────────────────────────
          7        23         1
         41        23         1
  …
Return a row for every period, zeros included, or set fill_missing_periods:
zero.
```

A missing period is a gap only between the entity's first returned period and
the as_of_date; a series that starts later is not a gap. A row whose period is
off the grid, or at or after the as_of_date, is counted too: the matrix build
refuses both. Without `--as-of` the check renders the last test as_of_date of
the `temporal_config`; repeat `--as-of YYYY-MM-DD` for other dates.
`--json PATH` also writes the report. The exit code is 0 when the series is
complete, 1 when there are gaps or off-grid rows, and 2 when the check could
not run (no `history_query`, no `history_period`, a query error). The
[target history](/triage-pg/concepts/target-history/) page explains the
calendar and the two ways to fix a gap.

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

`triage model curve <id>` prints the threshold (Rayid) curve of one test date:
precision and recall at each cut of the scored population, next to that
population's base rate. The example is model 285 of
`example/dirtyduck/experiment-guides.yaml`, the experiment the guides use; ids
differ on another database.

```console
$ triage model curve 285 --points 10
Threshold curve — model 285 @ 2016-10-01
14053 scored row(s); base rate = labeled positives ÷ labeled rows

      k    % pop   Precision   Recall   Base rate
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   1406    10.0%      0.3881   0.1791      0.2627
   2811    20.0%      0.3607   0.3266      0.2627
   …
  14053   100.0%      0.2627   1.0000      0.2627
```

Without `--date YYYY-MM-DD` it reads the model's latest test date. `--subset`
takes a subset name or a `subset_hash` prefix and makes that subset the
population, ranking its members among themselves as the subset metrics do.
`--points N` prints the cuts at k = ⌈i × rows ÷ N⌉ for i = 1…N (default 20), so
the row at 10% of the population is the cut `precision@10_pct` uses. `--json`
prints the points instead of the table. A precision column that stays close to
the base rate means the model barely ranks that population; see
[Reading the threshold curve](/triage-pg/guides/reading-the-threshold-curve/).

### `triage experiment show` — one Experiment in one view

`experiment show` takes a config file or a hash prefix. It prints what the config
implies and what the database holds for that Experiment:

```console
$ triage experiment show experiment-guides.yaml --runs 3 --top 3
╭────────────────────────────── Experiment ──────────────────────────────╮
│ hash: 0494a5078c4a1ebc8ba1e5c9f5bec37a2086a221a3186fe2f4ef2aa9bbdb215d │
│ name: Slate-Planck-96                                                  │
│ problem_type: classification   task_framing: resource_prioritization   │
│ config: experiment-guides.yaml                                         │
╰────────────────────────────────────────────────────────────────────────╯
╭─ Validation ─╮
│ valid        │
╰──────────────╯
               Plan
  Statistic                 Value
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Temporal splits               3
  …
  Models to be trained         36

runs: 6   last: completed 2026-10-29T00:51:06.379233+00:00
  Run        State       Started                            Detail
  a3aec777   succeeded   2026-10-29T00:51:06.379233+00:00   12 models
  …
                          Model groups (auc_roc)
  Group   Algorithm                  Models   Avg ± σ           Max regret
  35      RandomForestClassifier     3        0.5928 ± 0.0151   0.0194
  …
                    Leaderboard (top)
  Group   Model   Metric    Param   As-of        Value
  37      273     auc_roc           2016-10-01   0.6142
  …
```

The plan and the validation need no database: they are the numbers
`analyze-config` prints. The history is the runs, the model groups (on the
Experiment's most-evaluated metric) and the top of the leaderboard. A config
that has not run yet shows "not run yet" instead of a history. With a hash
prefix instead of a file, the command reads the config stored in
`triage.experiments`, so an Experiment whose file is gone still shows its plan.
`--runs N` and `--top N` set how many runs and leaderboard rows to show. `--json`
prints the same content as one JSON object. The command exits with code 2 when
the target is neither a file nor exactly one known hash prefix.

### `triage status --json --full` — the whole Project in one call

`status --json` prints the Project's health, sizes, last runs and pending work.
`--full` adds five keys: `experiments` (each with its run count and last
status), `configs` (every experiment config under the current directory, with
the experiment hash it belongs to), `sources`, `relations` (the `triage`
schema's tables and views, each with a row estimate) and `warnings`:

```console
$ triage status --json --full
{
  "contract": 1,
  "triage_version": "1.5.0",
  "project_root": "/…/example/dirtyduck",
  "project": "food",
  "database": { "connected": true, "detail": "pg 16" },
  …
  "configs": [
    {
      "path": "experiment-guides.yaml",
      "experiment_hash": "0494a5078c4a1ebc8ba1e5c9f5bec37a2086a221a3186fe2f4ef2aa9bbdb215d",
      "name": null,
      "problem_type": "classification",
      "error": null
    },
    …
```

A config joins its Experiment through `experiment_hash`. Several files can
share one hash, because the hash covers only the cohort, the label, the temporal
config and `problem_type`. A YAML file counts as a config when it has both a
`temporal_config` and a `label_config` key. A config that cannot be parsed is
listed with its parse error in `error`. The search skips `.venv`, `node_modules`,
`dbt_packages`, `target`, `docs-site` and every directory whose name starts with
a dot. It stops after 500 YAML files and says so in `warnings`. `--configs GLOB`
(repeatable) narrows it.

When the database is down, `database.connected` is `false`, `experiments`,
`sources` and `relations` are empty, `configs` is still filled, and the exit
code is 0. `--full` without `--json` exits with code 2. This is the call an
editor front end makes on each refresh. `contract` is a version number. A
release that renames or removes a key raises it.

### `triage check metrics` — the stored metrics against a second implementation

Every metric in `triage.evaluations` is computed by a SQL function. `triage
check metrics` recomputes each stored value with scikit-learn, numpy or
scikit-survival, over the rows the SQL function read, and reports the largest
gap per metric. Pick the models with one of `--model ID`, `--group ID` or
`--experiment HASH`:

```console
$ triage check metrics --experiment 396938287f6d
                    60 model(s), tolerance 1e-09

  metric         checked   failed   largest gap   worst row
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  mae            60        0        2.1e-13       model 514 · test · 2016-07-01 · stored 3.156516 vs 3.156516
  pinball@0.5    60        0        0             model 479 · test · 2015-07-01 · stored — vs —
  pinball@0.8    60        0        0             model 479 · test · 2015-07-01 · stored — vs —
  pinball@0.95   60        0        0             model 479 · test · 2015-07-01 · stored — vs —
  r2             60        0        1.43e-13      model 492 · test · 2015-07-01 · stored -2.184637 vs -2.184637
  rmse           60        0        5.6e-14       model 509 · test · 2016-07-01 · stored 4.347200 vs 4.347200

360 stored value(s) match.
```

On the same database the classification Experiment of the guides (72 models,
6,912 values, the largest gap 2.21e-14 on `brier`) and the survival
Experiment (24 `c_index` values, gap 0) pass too. A stored NULL matches when
the second implementation also gives no value, as `pinball@τ` does for a model
that does not forecast τ ([Evaluation](/triage-pg/reference/evaluation/)).

`--tolerance` sets the largest gap that passes (default 1e-9); `--json PATH`
also writes every row. A metric with no second implementation is listed as
not audited and never counts as passed. The exit code is 0 when every audited
row is within the tolerance, 1 when one is not or nothing could be audited,
and 2 for bad input.

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

### Coverage, and comparing groups on the same rows

A time-series baseline makes no prediction for a row it has no history for,
so each model group is evaluated on the rows it scored. The audition table has
a **Coverage** column, the mean share of the labeled rows a group scored over
its test dates. On the DirtyDuck regression Experiment the regressors cover
82.5% and the lag baselines 66.5%, so their MAEs are means over different
rows. `triage postmodel compare-rows` puts the groups on the rows all of them
scored. It reads only:

```console
$ triage postmodel compare-rows 396938287f6d --groups 72,75,79,81
  Date         Group   Labeled   Scored   Common   MAE own   MAE common   RMSE common   WAPE common   Bias common
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  2015-07-01   72      6755      5033     1271     3.3657    3.7940       4.8931        0.7435        1.6345
  2015-07-01   75      6755      5033     1271     2.9104    2.9740       4.7100        0.5828        -1.1031
  2015-07-01   79      6755      1271     1271     3.6585    3.6585       5.7338        0.7169        1.1456
  2015-07-01   81      6755      5033     1271     4.1240    4.5153       6.5370        0.8848        -1.5728
  …
  2017-01-01   72      7284      6228     6178     2.8892    2.8851       4.2885        0.5695        -0.0808
  2017-01-01   75      7284      6228     6178     2.7592    2.7588       4.4505        0.5446        -1.0657
  2017-01-01   79      7284      6178     6178     3.1784    3.1784       4.7981        0.6274        -0.0125
  2017-01-01   81      7284      6228     6178     3.8992    3.8983       5.6125        0.7696        -1.2972
```

On the first test date the six-month moving average (group 79) scored 1,271
of the 6,755 labeled rows, so the common rows are those 1,271; there the
Ridge regression (group 72) goes from an MAE of 3.37 on its own rows to 3.79.
`--groups` takes a comma-separated list (default: every group of the
Experiment); `--json` prints the rows.

`triage audition <hash> --common-rows` selects on those rows: it registers the
rows every group scored as a Subset (named `common:<md5>`, kept apart from
your own Subsets), evaluates each model on it, and runs the ranking and the
selection rules there. `--groups` narrows the groups, as above. This writes
evaluation rows; without the flag, audition reads what it always read.

`triage postmodel sensitivity <hash> --param alpha` compares the groups of
one estimator class whose hyperparameters differ only in `--param`:

```console
$ triage postmodel sensitivity 396938287f6d --param alpha
 sklearn.linear_model.Ridge: mae by
               alpha

  alpha   Group   Mean mae   Dates
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  0.1     72      3.0712     4
  1.0     73      3.0698     4
```

`--metric` (default `mae`) and `-p` choose the metric averaged over the
dates; `--model-type` names the class when several classes set the
parameter; `--common-rows` evaluates the groups on their common rows first.
It refuses groups that differ in another hyperparameter as well.

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
everywhere. [Reading the crosstabs](/triage-pg/guides/reading-the-crosstabs/)
works through a real list.

`error-tree --kind` chooses which mistakes the command fits a tree on. `fp`
reads the selected top k and asks which flags are wrong; `fn` reads the rows passed over
and asks which positives the list misses; `confusion` fits one tree over every
labeled row with four classes (TP, FP, FN, TN), and its first splits mostly
redraw the cut itself. `both`, the default, is `fp` and `fn`; `all` adds
`confusion`. `--depth` (default 3) and `--min-leaf` (default 30) keep the rules
short enough to read. The examples below are model 285 and its model group 41
from the same experiment as the curve above.

```console
$ triage postmodel error-tree 285 -p 10_pct --kind all
45 error rule(s) persisted.
…
```

`triage postmodel error-rules <id>` reads the persisted leaves back without
fitting a tree, highest error rate first. `--kind fp|fn|confusion` keeps one
kind and `--top` (default 15) sets the number of rows:

```console
$ triage postmodel error-rules 285 --kind fn --top 3
  As-of        Cut      Kind   Rule                                                               n    Errors   Rate
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  2016-07-01   10_pct   fn     facilities.zip_code=60622 > 0.5 AND RECENCY(inspections.date) >    32   24       0.75
                               250.5 AND COUNT(inspections.risk) > 2.5
  2016-10-01   10_pct   fn     MAX(inspections.n_serious) > 0.5 AND RECENCY(inspections.date) >   77   38       0.49
                               22.5 AND facilities.zip_code=60614 > 0.5
  …
```

With `--suspicious` it lists only the leaves that are suspiciously right: fp
leaves whose error rate is at most `--max-error` (default 0.02) and confusion
leaves whose TP share is at least 1 − `--max-error`, each holding at least
`--min-support` rows (default 30). Each leaf comes with its lift, the leaf's
1 − error rate divided by the model's `precision@` at the same date and cut. A
large leaf with almost no error can be a real signal, or a feature that knows
the outcome:

```console
$ triage postmodel error-rules 285 --suspicious
No suspiciously-right leaves for model 285 at error ≤ 0.02 and support ≥ 30.
```

`triage postmodel error-recurrence <group>` counts on how many of a model
group's test dates each rule recurs. Rules are matched by signature, which keeps
each split's feature and direction and drops its threshold. Run `error-tree` on
the group's models first and pass the same `-p` cut; `--kind` defaults to `fp`
and `--top` to 20.

```console
$ triage postmodel error-recurrence 41 -p 10_pct --kind fn --top 3
  Signature                                   Dates   Mean rate   Mean n   Example rule
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  TENURE(inspections.date)>|facilities.zip…   2/6     0.78        50.0     facilities.zip_code=60622 > 0.5 AND
                                                                           TENURE(inspections.date) > 548 AND
                                                                           TENURE(inspections.date) > 705
  …
```

A rule present on most dates is a stable failure mode and a candidate for a
missing feature; here the most frequent fn rules recur on 2 of 6 dates.
[Reading the error trees](/triage-pg/guides/reading-the-error-trees/) reads
these outputs on DirtyDuck.

### Problem types per command

A diagnostic is defined for some problem types only: a residual needs a
continuous forecast, a false positive needs a positive class. Each command
reads the model's `problem_type` from the database and refuses the others with
exit code 2 and a message naming the command that answers the nearest
question:

```console
$ triage postmodel residuals 285
triage postmodel residuals reads regression and regression_ranking models; model 285 is classification.
For a classification model use triage plot calibration 285 or triage postmodel error-tree 285.
```

| Command | classification | regression_ranking | regression | survival |
|---|---|---|---|---|
| `postmodel error-tree` | `fp`, `fn`, `confusion` | every kind | `abs-error`, `beats-baseline` | the classification kinds, with `--horizon H` |
| `postmodel residuals`, `crosstabs --by abs-error` | — | yes | yes | — |
| `postmodel baseline` (and `plot segments`, `plot win-consistency`) | — | yes | yes | — |
| `postmodel near-miss`, `postmodel prototypes` | yes | yes | — | — |
| `postmodel survival` | — | — | — | yes |

The other postmodel commands (`crosstabs` at a top-k cut, `error-rules`,
`error-recurrence`, `compare`, `compare-rows`, `structure`, `importance`,
`sensitivity`) are not gated. `error-tree --kind` takes `fp`, `fn`,
`confusion`, `both` and `all` for a classifier and `abs-error` and
`beats-baseline` for a regression model. A regression_ranking model reads both
families, and like `near-miss` and `prototypes` it reads positive as
`outcome > 0`. A survival model needs `--horizon H`, and no other type takes
it. `structure` and `importance` read every type but report only what applies:
`structure` gives the forecast structure for a regression model, the leaf
support for a tree and the mean-shift decomposition for a linear forecast, and
exits 1 when none of those apply (HistGradientBoosting and LightGBM included).

### Regression: residuals, baselines and model structure

The examples are the DirtyDuck regression Experiment
(`example/dirtyduck/experiment-regression.yaml`, `regression_ranking`, the
count of violations in the next six months). Model 526 is its random forest,
group 71, on the last test date, 2017-01-01.

```console
$ triage postmodel residuals 526 --top 3
                                     Errors per test date

  Date         n      Σy        MAE      RMSE     WAPE     Bias     Top 100   Top 1%   y ≤ ŷ
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  2017-01-01   6228   31496.0   3.0128   4.2517   0.5957   0.4187   10.1%     7.3%     68.8%

                      Errors by band, pooled over the dates

  Segment      Rows   Entities   Σy        MAE      WAPE     Bias      |e| share
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  no history   50     50         200.0     3.2285   0.8071   0.6881    0.9%
  (0, 10]      5678   5678       27625.0   2.9558   0.6075   0.5450    89.4%
  (10, 100]    500    500        3671.0    3.6377   0.4955   -1.0435   9.7%
…
Forecast structure on 2017-01-01: 48 distinct values; 33% of rows at 5.871; minimum 3.432, maximum 9.788; 0.0% negative
                       Largest errors

  Entity   Label   Date         Forecast   Outcome   Error
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  5034     —       2017-01-01   5.87       42.00     -36.13
  17570    —       2017-01-01   5.87       39.00     -33.13
  123      —       2017-01-01   6.00       39.00     -33.00
```

Bias is the mean of forecast − outcome. "Top 100" and "Top 1%" are the share of
the total absolute error held by the 100 and the 1% largest errors; "y ≤ ŷ" is
the share of rows at or below the forecast. The segments come from each
entity's labels before the date, on the same label-date grid as the target
lags: `band` cuts the mean prior label at 0, 10, 100 and 1,000, `sbc` is its
Syntetos–Boylan class (smooth, erratic, intermittent or lumpy, plus "no
demand" and "one demand period" for too short a history), and `cold-start`
sets apart the entities with no prior label. `--by
date|band|sbc|cold-start|subset:NAME` prints one table. `--top N` (default 20)
sets the largest errors listed. `--quantiles` adds, for a model that forecasts
a quantile τ, the gap between its point coverage and τ (a median forecast
should have about half the rows at or below it). It reads only.

`triage postmodel baseline GROUP` asks where a model group beats a baseline
group, on the rows both scored:

```console
$ triage postmodel baseline 71
Group 71 against baseline group 83 (chosen: lowest MAE on shared rows): 23476 shared rows on 4 test date(s).
                                          Model against baseline, per segment

                                       MAE      MAE       WAPE     WAPE      Bias                     Excess    Excess
  Cut       Segme…   Rows    Σy        model    base      model    base      model    Closer   Wins   |e|       share
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  band      no       4354    23007.0   3.1079   3.4753    0.5882   0.6577    0.2537   52.2%    yes    -1599.8   —
            history
  band      (0,      17330   82748.0   3.0303   3.1251    0.6346   0.6545    0.8424   48.7%    yes    -1643.9   —
            10]
  band      (10,     1792    14202.0   4.1643   4.7139    0.5255   0.5948    -1.11…   57.1%    yes    -984.8    —
            100]
  …
Segments won: 88.9%
Entities: 11518 — model wins 51.0%, ties 0.0%; the won ones hold 64.1% of Σy; median error ratio (model / baseline)
0.977
Dates won per entity — 611 entities on all 4
                    dates

  Dates won   Observed   Expected by chance
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  0           98         50.6
  1           140        175.4
  2           192        227.1
  3           117        130.1
  4           64         27.8

chi-square 105.59 on 4 df, p = 6.33e-22
P(win last date) 46.2%; P(win last | won all 3 earlier) 63.4% over 101 entities
Out of time: the 5478 entities the model won before 2017-01-01, on that date — MAE model 3.0474, baseline 3.4888 (2848
rows)
```

The first table compares the two groups per segment: MAE, WAPE and bias for
each, the share of rows where the model is closer, and the model's excess
absolute error (negative where it wins). The excess share is the segment's
part of the excess error summed over the segments of its cut where the model
loses. The second part pools each entity over the dates: the share of
entities the model wins (a tie is not a win), and whether wins land on the
same entities more often than chance. The chance count is a Poisson-binomial
over each date's win rate, tested with a chi-square. The last line is the
out-of-time check: the entities the model won on the earlier dates, scored on
the last one.

Without `--baseline GROUP`, the command picks the Experiment's baseline group
with the lowest MAE on the shared rows (here Croston over the six-month
violation totals, group 83). `--cut band|sbc|cold-start|subset:NAME|all` chooses the
segments (default `all`, which adds each Subset the Experiment's models were
evaluated on). `--entity-group-query FILE` runs a SQL file returning
`entity_id, group_key` and repeats the entity table at that coarser level.
`--experiment HASH` is needed only when the group has models in several
Experiments. It exits 1 when the groups share fewer than two test dates, since
the chance and out-of-time tables need two.

`triage postmodel structure MODEL` checks the fitted model:

```console
$ triage postmodel structure 526
Model 526: RandomForestRegressor (regression_ranking)
Forecast structure on 2017-01-01: 48 distinct values; 33% of rows at 5.871;
minimum 3.432, maximum 9.788; 0.0% negative
10 tree(s), 8–8 leaves each (mean 8.0); 0 leaves fitted on fewer than 2 training
rows, in 0 tree(s)

$ triage postmodel structure 527
Model 527: Ridge (regression_ranking)
Forecast structure on 2017-01-01: 6088 distinct values; 0% of rows at 4.842; minimum 0.2743, maximum 13.39; 0.0%
negative
Mean forecast 5.1403 on the train matrix, 4.9591 on the test matrix (stored 4.9591, gap 0.00000000): a shift of -0.1811
               Mean shift by feature family

  Family                     Features   β·Δx̄      |share|
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  inspections.risk           16         -2.0522   35.9%
  inspections.result         16         0.8001    13.6%
  inspections.type           16         0.6960    11.8%
  inspections.date           32         0.3971    37.3%
  …
```

A forest whose 6,228 forecasts take 48 values, a third of them the same one,
ranks in blocks. For a tree or a tree ensemble, `--min-rows N` (default 2)
lists the leaves fitted on fewer than N training rows. For a linear forecast
the change of the mean forecast from train to test is split by feature family,
β·(x̄ test − x̄ train), and checked against the stored predictions. A model
that forecasts through a link (a classifier's `predict_proba`) is not split.

`triage postmodel importance GROUP` reads `triage.feature_importances` and
gives each feature family's share of the group's importance per train end, and
how stable the top features are:

```console
$ triage postmodel importance 71
                                         Importance share by family — group 71

  Model   Train end    inspections.…   facilities.f…   inspections.d…   facilities.z…   inspections.t…   inspections.…
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  481     2015-01-01   32.2%           27.1%           23.7%            12.2%           2.6%             2.2%
  496     2015-07-01   39.5%           20.8%           15.6%            23.9%           0.0%             0.1%
  511     2016-01-01   44.3%           20.5%           14.2%            17.8%           0.6%             2.6%
  526     2016-07-01   49.8%           22.9%           7.1%             20.2%           0.0%             0.0%

top-5 Jaccard, model 481 → 496: 0.43
top-5 Jaccard, model 496 → 511: 0.67
top-5 Jaccard, model 511 → 526: 0.67
```

A family is the source column a feature is built from: every aggregation and
interval of `inspections.date` is one family. `--top N` (default 5) sets how
many features the Jaccard index compares between consecutive train ends.

### Regression error trees and crosstabs

`error-tree --kind abs-error` fits a tree on the absolute error, so its leaves
say where the forecast is far off. A leaf's rate is its mean |e| over the
date's MAE:

```console
$ triage postmodel error-tree 526 --kind abs-error
7 error rule(s) persisted.
                                                   abs_error leaves — model 526

  As-of        Rule                                                                 n      Mean |e|   × MAE   Bias     |e| share
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  2017-01-01   facilities.zip_code=60639 > 0.5 AND RECENCY(inspections.date) <=     77     8.477      2.81    -8.351   3.5%
               207.5 AND ABS(facilities.RECENCY(inspections.date|interval=P3M))
               <= 28.5
  …
```

`--kind beats-baseline` fits a tree on whether the model is closer to the
outcome than a baseline group (`--baseline GROUP`, default as for `postmodel
baseline`). It trains on the group's earlier test dates and reports each
leaf's win share on the training rows and on the model's own date, so a leaf
that wins only in training shows it:

```console
$ triage postmodel error-tree 526 --kind beats-baseline
8 error rule(s) persisted.
                                                beats_baseline leaves — model 526

  Rule                                                                       Train n   Train wins   Test n   Test wins   Gain
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ABS(facilities.TENURE(inspections.date)) > 555.5 AND                       2854      42.9%        1885     46.6%       -216.48
  NUNIQUE(inspections.date) <= 7.5 AND RECENCY(inspections.date) <= 175.5
  …
Fitted on 3 date(s) before 2017-01-01 against baseline:83: closer on 50.0% of the training rows and 50.1% of the test rows; …
```

`error-rules --kind abs-error|beats-baseline` reads those leaves back. With
`--suspicious`, an abs-error leaf is listed when its mean |e| is at most
`--mae-ratio` (default 0.1) of the date's MAE and it holds at least
`--min-support` rows: a large group the model forecasts almost exactly can be
a feature that knows the outcome. Its lift is the date's MAE over the leaf's.

`postmodel crosstabs MODEL --by abs-error:CUT` compares the rows with the
largest absolute errors (the top CUT, default `10_pct`) with the rest, and
persists them at parameter `abs_error_CUT`:

```console
$ triage postmodel crosstabs 526 --by abs-error:100_abs
321 crosstab row(s) persisted.
       Crosstabs — model 526 @ abs_error_100_abs, selected vs rest (top |log ratio| of means)

  As-of        Feature                                                    Selected   Rest     Ratio
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  2017-01-01   facilities.zip_code=60639                                  0.2500     0.0237   10.54
  2017-01-01   facilities.zip_code=60640                                  0.0700     0.0258   2.72
  …
```

A quarter of the 100 largest errors are facilities in one ZIP code that holds
2.4% of the rest, the same ZIP code the abs-error tree splits on first.

### Survival: the censoring-aware set

`triage postmodel survival MODEL --horizon H` computes a survival model's
diagnostics once, from the scored matrix and the model file, persists them to
`triage.survival_diagnostics`, and prints them. The example is model 413, the
scaled Cox model of the DirtyDuck survival Experiment
(`example/dirtyduck/experiment-survival.yaml`, days to the next failed
inspection), at 90 days:

```console
$ triage postmodel survival 413 --horizon 90
527 survival diagnostic row(s) persisted.
Model 413 (ScaledCoxPHSurvivalAnalysis) on 2017-01-01, horizon 90: 6228 labeled
rows, 839 events by the horizon, KM probability of the event 13.5%
                 Risk deciles at horizon 90

  Decile   Rows   Events ≤ 90   KM P(event)   Lift   Score
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1        622    122           19.6%         1.46   0.687
  2        623    109           17.5%         1.30   0.411
  …
  10       623    41            6.6%          0.49   -0.801

Log-rank across the risk tertiles: p = 5.74e-19 (chi² 84.00)
Uno's C up to the horizon: 0.5841 (839 rows before 90)
                   Over time

  t    AUC(t)   Brier(t)   Brier, training KM
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  9    0.585    0.010      0.010
  …
  90   0.590    0.116      0.117

   Calibration at horizon 90, per decile of
                predicted S(90)
…
```

Decile 1 holds the highest risk scores. The KM probability of the event by
the horizon is read per decile, so censored rows count as far as they were
followed. The log-rank test compares the Kaplan–Meier curves of the risk
tertiles. Uno's C and AUC(t) weight the rows by the inverse probability of
censoring, read from the train matrix's labels. Brier(t) and the calibration
at the horizon (with ICI and E50) need an estimator with
`predict_survival_function`; the Brier of the training Kaplan–Meier, which
ignores the features, is printed beside it. `--horizon` is in the units of
the label's duration and is required. `--times 7,14,30,60` sets the AUC(t)
and Brier(t) grid (default: ten evenly spaced times up to the horizon). A
value that cannot be computed is stored as NULL with its reason. Audition
keeps ranking survival models on the stored C-index.

`error-tree --horizon H` makes a survival label binary (an observed event at a
duration ≤ H), drops the rows censored before H, and fits the classification
kinds. The leaves are stored at parameter `<cut>|horizon=<H>`, so two horizons
do not replace each other:

```console
$ triage postmodel error-tree 407 --kind confusion --horizon 90 -p 10_pct
8 error rule(s) persisted.
                                      Error rules — model 407 @ 10_pct|horizon=90

  As-of        Kind        Rule                                                                   n      Errors   Rate
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  2016-07-01   confusion   ABS(facilities.NUNIQUE(inspections.result)) > 2.5 AND                  236    165      0.70
                           ABS(facilities.RECENCY(inspections.date)) > 75.5 AND
                           COUNT(inspections.result) > 5.5
  …
```

### Near miss, prototypes and list overlap across k

`triage postmodel near-miss MODEL -p CUT` splits the population ranking of
each scored date into three bands: the selected top k, the near miss (the
next rows) and the rest. It reads only:

```console
$ triage postmodel near-miss 285 -p 10_pct --date 2016-10-01
                 Near miss — model 285 at 2016-10-01, top 10_pct, band 10_pct

  Band        Ranks        Rows    Labeled   Positive   Base rate   Mean outcome   Mean score
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Selected    1–1406       1406    688       267        38.8%       0.3881         0.4407
  Near miss   1407–2812    1406    662       220        33.2%       0.3323         0.3405
  Rest        2813–14053   11241   4325      1004       23.2%       0.2321         0.2308
```

Model 285 is the logistic regression the guides use. The facilities just
below its 10% cut fail at 33.2%, closer to the list's 38.8% than to the rest's
23.2%: a larger budget would still find failures at almost the list's rate.
`--width CUT` sets the size of the near-miss band (default: the cut itself);
`--date` keeps one date; `--json` prints the rows. For a regression_ranking
model the base rate counts `outcome > 0` and the mean outcome carries the
continuous value.

`postmodel crosstabs MODEL --against near-miss` compares the list with the
near-miss band instead of with every row below the cut, and persists those rows
beside the usual ones:

```console
$ triage postmodel crosstabs 321 -p 10_pct --against near-miss --top 5
690 crosstab row(s) persisted.
       Crosstabs — model 321 @ 10_pct, selected vs near miss (top |log ratio| of means)

  As-of        Feature                                          Selected   Near miss   Ratio
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  2016-07-01   facilities.facility_type=mobile food dispenser   0.0007     0.0036      0.20
  2016-10-01   facilities.facility_type=mobile food dispenser   0.0021     0.0007      3.00
  2016-10-01   STDDEV(inspections.n_critical|interval=P6M)      0.4023     0.1742      2.31
  …
```

`triage postmodel prototypes MODEL -p CUT` splits each date's labeled rows into
TP, FP, FN and TN at the cut, z-scores the features over those rows, and keeps
the entities nearest each class's centroid, with their largest deviations
from it. It persists to `triage.prototypes` and fits nothing:

```console
$ triage postmodel prototypes 285 -p 10_pct
24 prototype row(s) persisted.
                Prototypes — model 285 at 2016-07-01, top 10_pct

  Class   Rows   Rank   Entity   Distance   Largest deviations
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  TP      242    1      4463     6.364      MEDIAN(inspections.n_serious) -1.45σ, …
  …
  FP      405    1      6825     5.365      MEDIAN(inspections.n_serious) -1.29σ, NUNIQUE(inspections.type) -1.15σ, …
  …
```

`-n` (default 3) sets the prototypes per class, `--deviations` (default 3) the
deviations printed per prototype (10 are persisted), and `--date` keeps one
date. A feature that is constant over a date's labeled rows is left out of
the distance and reported.

`postmodel compare A B --sweep CUTS` repeats the list overlap at several cuts,
to show how the two lists' agreement changes as k grows:

```console
$ triage postmodel compare 283 285 --sweep 50_abs,100_abs,200_abs,500_abs
        List overlap as k grows — m283 vs m285

  Cut       As-of        k(a)   k(b)   ∩     Jaccard
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  50_abs    2016-07-01   50     50     17    0.205
  50_abs    2016-10-01   50     50     14    0.163
  100_abs   2016-07-01   100    100    41    0.258
  …
  500_abs   2016-10-01   500    500    275   0.379

Rank correlation over the entities both scored (it does not depend on k): 2016-07-01 0.951; 2016-10-01 0.951
```

The random forest (model 283) and the logistic regression agree on a fifth of
their 50 first facilities and on more than a third of their first 500, while
their scores' rank correlation over every facility is 0.95. `--sweep` replaces
`-p`; the two cannot be combined.

## Plot

`triage plot` draws the diagnostics above as figures for reports, notebooks
and slides. Each command reads the same queries as the dashboard, so a figure
and a panel show the same numbers. It needs no dashboard extra.

```console
$ uv run triage plot leaves 41 -p 10_pct -o leaves.pdf
wrote leaves.pdf
84 leaves, 0 in the suspicious zone
```

The command prints the path it wrote and one line with the main numbers. The
format follows the extension of `-o`: `.svg` on a transparent background (for
pages with a light and a dark theme), `.png` and `.pdf` on white. Two runs over
the same rows write the same SVG bytes. Without `-o` the file is
`<command>-<id>.svg` in the current directory.

| Command | Draws | Options |
|---|---|---|
| `curve MODEL` | precision and recall against the share of the population, one line per series, each with its base rate | `--date D`, `--subset S` (repeat for several), `--compare MODEL2`, `--points N` (default 101) |
| `curve-dates GROUP` | the group's precision curve on each test date, and the base rate of each date | `--subset S`, `--points N` |
| `error-rules MODEL` | the leaves with the most errors, each against the population error rate at the cut | `--date D`, `-p CUT`, `--kind fp\|fn\|confusion` (default: fp and fn), `--top N` (default 12) |
| `leaves GROUP -p CUT` | every fp and confusion leaf of the group by size and wrong share, with the suspicious zone | `--max-error` (default 0.02), `--min-support` (default 30) |
| `recurrence GROUP -p CUT` | the error-rule signatures by test date | `--kind fp\|fn\|confusion` (default fp) |
| `crosstabs MODEL` | the features that most separate the list from the rest | `-p CUT`, `--date D`, `--stat mean\|nonzero`, `--compare MODEL2`, `--top N` (default 15), `--feature F` (repeat; replaces `--top`) |
| `crosstabs-dates GROUP` | the top features' statistic on every test date of the group | `-p CUT`, `--stat mean\|nonzero`, `--top N` (default 4, at most 8) |
| `overlap GROUP_A GROUP_B -p CUT` | Jaccard and rank correlation of the two groups' lists on each shared test date | `--sweep CUTS` instead of `-p`: the Jaccard index against k, one line per pair of models and date |
| `calibration MODEL` | mean score against the realized rate (a classifier) or the mean outcome (the other types) per score bucket, on the latest test date | `--bins N` (default 10; tied scores share a bucket) |
| `scores MODEL` | a histogram of the test scores: over [0, 1] with the positives for a classifier, over the score range otherwise, events and censored rows apart for survival | none |
| `gains MODEL` | the share of the outcome captured against the share of rows selected, with the random diagonal and the lift | `--date D`, `--points N` (default 100) |
| `forecast-actual MODEL` | forecast against outcome as a hexbin on log1p axes, with the diagonal | none (at most 200,000 rows, sampled by entity beyond that) |
| `residuals MODEL` | forecast − outcome against the forecast, as a hexbin | none |
| `bias MODEL` | mean forecast − outcome per test date or per segment | `--by date\|band\|sbc\|cold-start\|subset:NAME` (default date) |
| `forecast-time MODEL` | Σ forecast and Σ outcome per test date, one panel per segment | `--cut band\|sbc\|cold-start\|subset:NAME` (default band) |
| `segments GROUP` | MAE per segment for the group and its baseline, as paired dots | `--baseline GROUP`, `--cut … \|all` (default all) |
| `win-consistency GROUP` | entities by the number of test dates the model won, against the count expected by chance | `--baseline GROUP` |
| `importance GROUP` | each feature family's share of the importance, one stacked bar per train end | none |
| `km MODEL` | Kaplan–Meier curves by risk tertile, and the KM probability of the event by the horizon per risk decile | `--horizon H`, `--date D` (default: the set computed most recently) |
| `brier MODEL` | Brier(t) over time, beside the Brier of the training Kaplan–Meier | `--horizon H`, `--date D` |
| `survival-calibration MODEL` | predicted S(horizon) against the KM survival per decile, with ICI and E50 | `--horizon H`, `--date D` |

`gains` reads classification, regression and regression_ranking models and
exits 1 on a date whose shares are undefined (a negative outcome).
`forecast-actual`, `residuals`, `bias` and `forecast-time` read the two
regression types; `segments` and `win-consistency` read the same rows as
`triage postmodel baseline`. `km`, `brier` and `survival-calibration` draw
what `triage postmodel survival` persisted, so run it first; `brier` and
`survival-calibration` need an estimator with `predict_survival_function` and
exit 1 when the value is undefined everywhere. A plot of the wrong problem
type exits 2 with the same message as the postmodel commands.

```console
$ uv run triage plot km 413 --horizon 90 -o km.png
wrote km.png
2017-01-01 at horizon 90: KM event probability 0.07–0.20 across 10 deciles, decile 1 19.6% against 13.5% overall (lift 1.46); log-rank p = 5.74e-19 across the tertiles
```

A Subset is given by name or by a prefix of its `subset_hash`. A figure holds
at most 8 series: Subsets, test dates, compared models or features.

The exit code is 0 when the file is written. It is 1 when the database holds
nothing to draw, for example a model with no error trees; the message names
the command that creates the rows (`triage postmodel error-tree`,
`triage postmodel crosstabs`). It is 2 for bad input (an unknown model, group,
Subset or feature, an unsupported extension, more than 8 series) and for a
database or file error.

Each worked figure in the guides carries the `triage plot` command that draws
it on your own data.

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

`triage serve` starts the dashboard from any project that has the `dashboard`
extra (`uv add 'triage[dashboard] @ git+https://github.com/ccd-ia/triage-pg.git@v1.5.0'`). It finds the database the same way
every command does: `database.yaml`, then `DATABASE_URL`, then `PG*`. It
refuses the global `--dbfile` (exit code 2), because the dashboard would not
read that file. It passes `--host` (default `127.0.0.1`), `--port` (default
8000) and `--reload` to uvicorn.

### `triage reset` — start over after the data changes

When a source is reloaded and the old data was wrong, every result built on it
is wrong too. `triage reset` deletes those results and keeps the
configuration. It works in a database of its own and in a host database that
carries the `triage` schema beside its own tables.

```console
$ triage reset --all                       # dry run: rows per table, files to delete
$ triage reset --all --yes --confirm food --bump-sources reload-2026-11
```

It deletes experiments, runs, models, model groups, matrices, predictions,
evaluations, bias metrics, crosstabs, error rules, feature importances,
cohorts, labels and the artifact records. It keeps `sources`,
`source_versions`, `metric_directions` and the schema version, so the schema
and the source registry survive. The matrix and model files are deleted after
the database commit, through the same storage layer as `triage gc --delete`.
A file that cannot be deleted is listed and the command exits 1. The database
change is not undone.

| Option | What it does |
|---|---|
| `--all` | Every result. It also clears `protected_groups` and the Subsets, which the next run rebuilds from the new data. |
| `--before <timestamp>` | Runs started before the timestamp, and experiments created before it that keep no later run. A cohort, matrix or model that a later run still uses is kept. |
| `--experiment <hash>` | One experiment and its runs; repeat for several. A unique prefix is enough. |
| `--yes --confirm <database>` | Delete. Without `--yes` the command is a dry run. Without `--confirm` it asks for the database name, and it refuses when stdin is not a terminal. |
| `--bump-sources <label>` | After the reset, pin every registered source at this label, so the next runs record the new data version. |
| `--json` | The counts, the kept tables and the file paths as JSON, for scripts and agents. |

`triage gc` is the opposite case: it deletes only outputs that nothing uses and
keeps every row of history. Use `reset` when the history itself is wrong.

## Where next

- [Architecture](/triage-pg/reference/architecture/) — the tables these
  commands read and write.
- [The dashboard tour](/triage-pg/reference/dashboard/) — the same surfaces,
  rendered.
- The guides on reading these outputs: the
  [threshold curve](/triage-pg/guides/reading-the-threshold-curve/), the
  [error trees](/triage-pg/guides/reading-the-error-trees/), the
  [crosstabs](/triage-pg/guides/reading-the-crosstabs/), the
  [model card](/triage-pg/guides/reading-the-model-card/), and
  [how to find leakage](/triage-pg/guides/finding-leakage/).
- The [Dirty Duckling smoke test](/triage-pg/tutorials/dirtyduckling/) to run
  this end to end yourself.
