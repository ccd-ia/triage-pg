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
| `overlap GROUP_A GROUP_B -p CUT` | Jaccard and rank correlation of the two groups' lists on each shared test date | none |

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
