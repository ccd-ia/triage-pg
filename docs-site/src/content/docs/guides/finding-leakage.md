---
title: How to find leakage
description: Which kinds of leakage triage-pg prevents, which ones `triage check leakage` catches on DirtyDuck, which ones it cannot see, and how the threshold curve, the error trees and the crosstabs point at the rest.
sidebar:
  order: 5
---

Leakage is one of the checks these guides cover, next to data quality, fairness
and stability over time. The guides to the
[threshold curve](/triage-pg/guides/reading-the-threshold-curve/#leakage),
the [error trees](/triage-pg/guides/reading-the-error-trees/#leakage)
and the [crosstabs](/triage-pg/guides/reading-the-crosstabs/#leakage)
each have a leakage subsection. This page collects them and adds
`triage check leakage`, the command that tests the cohort, label and feature
definitions by running them.

The numbers come from the DirtyDuck tutorial database and the guides Experiment,
[`example/dirtyduck/experiment-guides.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment-guides.yaml),
unless a figure or a paragraph says it is illustrative. Model ids are the ones in
our run; on a fresh tutorial database they will differ.

## Leakage and the as_of_date

Ghani, Walsh and Wang define leakage as the case where a model "has access to data
at training/building time that it wouldn't have at test/deployment/prediction time"
([DSSG, 2020](https://dssgfellowship.org/2020/01/23/top-10-ways-your-machine-learning-models-may-have-leakage/)).
A model with a leak scores better in evaluation than it will in production, and
nothing on the leaderboard says so.

In triage-pg the moment that matters is the `as_of_date` *t*. Three artifacts are
built for it, and each has a boundary that later data must not cross (see
[point-in-time correctness](/triage-pg/concepts/point-in-time-correctness/)).
"Known" below means the date in the Source's `knowledge_date_column`.

| Artifact at *t* | What it may read | What `triage check leakage` deletes to test it |
|---|---|---|
| Cohort | rows known on or before *t* | rows known after *t* |
| Features | event rows known before *t*; entity rows known on or before *t* | event rows known on or after *t*; entity rows known after *t* |
| Labels for the timespan *lt* | rows known before *t* + *lt* | rows known on or after *t* + *lt* |

The entity rule differs from the event rule because an entity in the Cohort at *t*
is known at *t*.

triage-pg owns part of the pipeline and prevents leaks there by construction.
timechop makes the temporal splits, featurizer bounds every child stream by the
`as_of_date`, and the Matrix adapter fits fit-based imputation and categorical
encoding on the training split only.
The user writes three things that triage-pg cannot judge by reading them: the
cohort query, the label query, and the knowledge date each Source declares.
`triage check leakage` tests those three.

## The DSSG list, mapped to triage-pg

Ghani, Walsh and Wang list nine ways leakage enters a model; the tenth entry of
their post asks readers for more. The README of
[`dssg/randomize_your_data`](https://github.com/dssg/randomize_your_data), a
repository the post credits to Walsh and Wang, adds one that matters for temporal
data: train and test label windows must not overlap. Each row below has one of four statuses: prevented by construction,
caught by `triage check leakage`, caught by the test suite, or not caught.

| # | Leakage | Status in triage-pg | Where |
|---|---|---|---|
| 1 | A proxy for the outcome used as a feature | **Not caught** when the proxy is already in the raw data. The diagnostics give hints. | [Leakage in one segment](#leakage-in-one-segment) |
| 2 | Imputation fitted on the entire data set | **Prevented by construction.** Fit-based imputation is fitted on the training split; fit-free fills read no other row. **Caught by the test suite** if that code regresses. | `test_train_then_test_matrix_full_lifecycle` |
| 3 | Discretization or scaling fitted on the entire data set | **Prevented by construction** for categorical encoding, fitted on the training split, and **caught by the test suite** if that regresses. A scaler inside an estimator, such as the min-max scaler of `ScaledLogisticRegression`, is fitted with the estimator on the training Matrix. | `test_fit_cat_encoding_is_train_only_sorted_zero_reserved` |
| 4 | Feature selection on the entire data set | **Prevented by construction.** triage-pg has no selection step; feature groups are fixed in the config. | [Feature groups](/triage-pg/reference/configuration/) in the config |
| 5 | Future outcomes, or transformations of them, used as features | **Caught by `triage check leakage`** when a feature reads rows known on or after *t*. Not caught when the value sits in a row known earlier. | [A feature that reads past the as_of_date](#a-feature-that-reads-past-the-as_of_date) |
| 6 | k-fold cross-validation on temporal data | **Prevented by construction.** timechop makes temporal splits only. **Caught by the test suite** if a change lets test rows reach training. | timechop; [the null-data tests](#randomized-raw-data-and-the-null-data-tests) |
| 7 | Data that happened before *t* but arrived later | **Caught by `triage check leakage`** when the Source declares the true knowledge date. A suite test plants this leak on synthetic data, because DirtyDuck cannot express it. Not caught when the declared date is wrong. | [Blind spots of the check](#blind-spots-of-the-check) |
| 8 | Rows selected with information from the future | **Caught by `triage check leakage`** (cohort). | [A Cohort selected by future activity](#a-cohort-selected-by-future-activity) |
| 9 | Design choices made by people who know what happened later | **Not caught.** Audition over every test date and baselines in the grid limit it. | [Blind spots of the check](#blind-spots-of-the-check) |
| README | Train and test label windows that overlap | **Prevented by construction** when the label query honours `{label_timespan}`; **caught by `triage check leakage`** (labels) when it does not. | [A label window with no upper bound](#a-label-window-with-no-upper-bound) |

"Caught by the test suite" applies to triage-pg's own code, not to a user's config.
The unit tests named in rows 2 and 3 assert that the test Matrix receives the
statistics fitted on the training split, and the null-data tests
[below](#the-two-dssg-tests) fail when a planted change lets test rows reach
training. Row 4 needs no test: there is no selection step to regress.

## The check on DirtyDuck

```bash
just tutorial-up        # DirtyDuck on port 5440; PG* must point at it
uv run triage check leakage example/dirtyduck/experiment-guides.yaml --json leakage.json
```

For every `as_of_date` of the temporal config, the check builds the Cohort, the
labels and the features on all data. It then deletes the rows that each cut in the
table above removes, builds the same artifacts again, and compares them with the
first build. All of it runs in one transaction that ends in `ROLLBACK`, so the
database is left as it was (see [the privilege it needs](#the-privilege-the-check-needs)).

On the guides Experiment the check covered seven `as_of_date`s: 2015-01-01, which
is a training date only, and the six test dates from 2015-07-01 to 2016-10-01.

```
cohort — entities added or removed
  as_of_date   result
  2015-01-01   ok
  2015-07-01   ok
  ...
  2016-10-01   ok

labels — label rows that appear, disappear or change
  as_of_date   label timespan   result
  2015-01-01   6 months         ok
  ...
  2016-10-01   6 months         ok

features — feature rows that differ, and the columns that moved
  as_of_date   result
  2015-01-01   ok
  ...
  2016-10-01   ok

Checked 7 as_of_date(s) in 10.9s: no differences (exit 0).
```

Before you read exit 0 as a clean result, look for two more things. The output has no
`Not checked:` list, so every relation the queries read is a declared Source with a
knowledge date. The JSON's `sources_cut` names both Sources: `ontology_entities`, cut
on `start_time`, and `ontology_events`, cut on `date`.

| Exit code | Meaning |
|---|---|
| 0 | No differences on any date, among the Sources the check could cut |
| 1 | At least one artifact changed when the check deleted later rows: leakage |
| 2 | The check could not run: a missing privilege, a missing Source, a broken foreign key, a held lock, or a bad config |

`--dates 2016-10-01` restricts the check to one date. In our run that took 2.6 s,
against 10.9 s for all seven.

### How to read a finding

The planted label leak [below](#a-label-window-with-no-upper-bound) gives this row
at 2016-10-01:

```
  as_of_date   label timespan   result
  2016-10-01   6 months         6488 differ (+0 −5860 ~628)
```

The JSON report holds the same finding with every field:

```json
{
  "component": "labels",
  "as_of_date": "2016-10-01",
  "label_timespan": "6 months",
  "cut": "rows known on or after 2017-04-01",
  "added": 0,
  "removed": 5860,
  "changed": 628,
  "n_entities": 6488,
  "examples": [3, 6, 9, 14, 16, 17, 19, 20, 26, 29],
  "columns": {}
}
```

| Field | What it says |
|---|---|
| `component` | Which definition depends on later data: `cohort`, `labels` or `features` |
| `as_of_date`, `label_timespan` | Which artifact moved |
| `cut` | The rows the check deleted; the artifact should not depend on them |
| `added` | Entities, or label rows, present only after the cut: later data excluded them |
| `removed` | Present with all data and gone after the cut: later data put them there |
| `changed` | Present in both, with a different label or feature value |
| `n_entities` | `added` + `removed` + `changed` |
| `examples` | Up to ten `entity_id`s to inspect by hand |
| `columns` | Features only: each column that moved, with the number of rows it moved in |

The terminal prints up to five column names per date and then `(+N more columns)`;
the JSON lists all of them with their counts. featurizer's column names encode the
relationship and the aggregation, so the names point at the data path that leaks.

## Planted leaks on DirtyDuck

Each example below is a copy of `experiment-guides.yaml` with one change. None of
them is in the repository. Each section gives the change, the check's output and the
fix.

### A label window with no upper bound

The label query keeps the `{label_timespan}` placeholder but no longer uses it to
bound the window:

```sql
select entity_id,
       bool_or(result = 'fail')::integer as outcome
from ontology.events
where {as_of_date}::date <= date
  and {label_timespan} > interval '0 days'   -- present, but bounds nothing
group by entity_id
```

The check flags the labels on all seven dates; the Cohort and the features stay `ok`.

```
labels — label rows that appear, disappear or change
  as_of_date   label timespan   result
  2015-01-01   6 months         12231 differ (+0 −10390 ~1841)
  2015-07-01   6 months         10806 differ (+0 −9027 ~1779)
  2015-10-01   6 months         10170 differ (+0 −8666 ~1504)
  2016-01-01   6 months         8740 differ (+0 −7275 ~1465)
  2016-04-01   6 months         7789 differ (+0 −6532 ~1257)
  2016-07-01   6 months         7506 differ (+0 −6458 ~1048)
  2016-10-01   6 months         6488 differ (+0 −5860 ~628)

Checked 7 as_of_date(s) in 11.0s: leakage found in 7 check(s) (exit 1).
```

We ran both label queries directly against the database at 2016-10-01. The leaky one
labels 12,535 facilities and the correct six-month window labels 6,675. Of the 6,488 that differ,
5,860 have a label only because of an inspection after 2017-04-01, and 628 passed
every inspection in the window but failed a later one, so the leaky label is 1
instead of 0. The placeholder in the query did not protect it, because the check
runs the query instead of reading it.

A label that reaches past *t* + *lt* overlaps the label windows of later splits,
which is the README's overlapping-windows case. The fix is the upper bound that
`example/dirtyduck/experiment.yaml` has:
`and date < {as_of_date}::date + {label_timespan}`.

### A Cohort selected by future activity

The cohort query keeps only the active facilities that have an inspection on or after
the `as_of_date`:

```sql
select e.entity_id
from ontology.entities as e
where e.activity_period @> {as_of_date}::date
  and exists (
    select 1 from ontology.events as v
    where v.entity_id = e.entity_id
      and v.date >= {as_of_date}::date
  )
```

```
cohort — entities added or removed
  as_of_date   result
  2015-01-01   9281 differ (+0 −9281 ~0)
  2015-07-01   9657 differ (+0 −9657 ~0)
  2015-10-01   9941 differ (+0 −9941 ~0)
  2016-01-01   10328 differ (+0 −10328 ~0)
  2016-04-01   10176 differ (+0 −10176 ~0)
  2016-07-01   10139 differ (+0 −10139 ~0)
  2016-10-01   9829 differ (+0 −9829 ~0)

Checked 7 as_of_date(s) in 9.2s: leakage found in 7 check(s) (exit 1).
```

Every difference is a removal. At 2016-10-01 the leaky Cohort holds 9,829 of the
14,053 active facilities, and the cut removes all 9,829: each one is in the Cohort
only because of an inspection that had not happened yet. A model trained on that
Cohort learns about facilities that are going to be inspected, and the inspectors
at *t* do not know which ones those are. This is item 8 of the DSSG list.

The fix is to decide eligibility from what is known at *t* (here
`activity_period`) and leave "was it inspected later" to the label query, which
already returns no row for a facility with no inspection in the window.

### A feature that reads past the as_of_date

featurizer bounds each child stream by the `as_of_date` through the child entity's
`temporal_ix`. The planted change deletes one line, `temporal_ix: date`, from the
`inspections` entity in `feature_config`. featurizer logs
`Entity Entity(inspections) lacks temporal index; skipping interval-based aggregation`
and aggregates every inspection on record, including the ones after *t*.

```bash
uv run triage check leakage planted-feature-unbounded.yaml --dates 2016-10-01
```

```
features — feature rows that differ, and the columns that moved
  as_of_date   result
  2016-10-01   9829 differ (+0 −0 ~9829): COUNT(inspections.result), COUNT(inspections.risk),
               COUNT(inspections.type), STDDEV(inspections.n_serious), MEAN(inspections.n_serious)
               (+13 more columns)

Checked 1 as_of_date(s) in 1.4s: leakage found in 1 check(s) (exit 1).
```

The JSON lists 18 columns, from `COUNT(inspections.result)` (9,829 rows) and
`MEAN(inspections.n_serious)` (6,528) down to `NUNIQUE(inspections.risk)` (32). Every
name is an aggregate over `inspections` and none names a `facilities` attribute, so
the leak is on the facilities → inspections path. The 9,829 facilities are the ones
with an inspection on or after 2016-10-01. Their inspection counts move in every
case; a column such as `NUNIQUE(inspections.risk)` moves only where a later inspection
recorded a risk level the facility had not had before. The fix is to restore
`temporal_ix: date`.

Late-arriving data (item 7 of the DSSG list) needs a Source whose knowledge date
differs from the date featurizer bounds on. DirtyDuck cannot show it, because
`ontology.events` has one `date` column that is both the inspection date and the
declared knowledge date. The test suite plants it on synthetic data in
[`test_cli_late_arriving_data_names_the_feature_columns`](https://github.com/ccd-ia/triage-pg/blob/main/src/tests/test_leakage_check.py).
There the events Source declares `known_at`, 30 days after `occurred_at`, while
featurizer reads `occurred_at`. The check exits 1 and names
`SUM(events.amount)` and `SUM(events.amount|interval=P90D)`, and never the entity's own
`size` column.

### A table the config does not declare

The last planted change repeats the Cohort leak through `clean.inspections`, a table
no Source declares:

```sql
select e.entity_id
from ontology.entities as e
where e.activity_period @> {as_of_date}::date
  and exists (
    select 1 from clean.inspections as i
    where i.license_num = e.license_num
      and i.date >= {as_of_date}::date
  )
```

```
Not checked:
  clean.inspections: read by the cohort query but not declared as a Source: not cut, not checked
Checked 7 as_of_date(s) in 10.2s: no differences (exit 0).
```

The leak is the same as before, and the exit code is 0, because the check deletes
rows only from declared Sources. It finds the undeclared table in the query plan
(`explain (format json)`, with views expanded to their tables) and lists it under
`Not checked:`. Exit 0 with a non-empty list means "no difference among the rows the
check could cut", which is weaker than a clean result. Declare `clean.inspections`
as a Source with its `knowledge_date_column`, or read the declared `ontology.events`
instead. A CI job should fail on a non-empty `not_checked` in the JSON as well as on
exit 1.

## Blind spots of the check

The check compares each artifact with and without the rows its cut removes, by the
knowledge date each Source declares. A leak that does not take the form of a row known
after the cut passes it.

- **A knowledge date declared wrong.** If results reach the database days after the
  inspection, but the Source declares the inspection date, every feature reads them
  early and the check agrees with the declaration. DirtyDuck assumes
  `ontology.events.date` is when the result became known; only the people who run
  the inspection system can confirm that. Ask them, and declare a load timestamp if
  the system keeps one.
- **An outcome proxy already in the raw data.** A column such as a hypothetical
  `closed_after_failure` flag on the facility row, filled in after the outcome, lives
  in a row known at `start_time`. The check keeps that row at every cut. The
  diagnostics in [the next section](#leakage-in-one-segment) are where such a proxy
  shows.
- **Values updated in place after their knowledge date.** An `update` overwrites the
  value and leaves the knowledge date as it was, so every cut sees today's value. If
  a Source keeps only a current state (a facility's current risk level, a case's
  current status), keep its history as events instead, one row per change with its
  own knowledge date.
- **Rows with a NULL knowledge date.** The check deletes with
  `where <knowledge_date_column> > <cut>` (or `>=`), and a comparison with NULL is
  never true. Such rows survive every cut, so the check never tests a query or a
  feature that reads them.
  DirtyDuck has none: 0 of 74,191 inspections and 0 of 22,169 facilities. Count
  yours with `select count(*) from <relation> where <knowledge_date_column> is null`.
- **A DELETE trigger or rule that cancels the cut.** A `before delete` row trigger
  that returns NULL, or a `do instead` rule, keeps the rows the check meant to
  remove. The check then compares the data with itself, and the fingerprints match.
  DirtyDuck's two Sources have no user triggers and no rules. List yours with
  `select tgname from pg_trigger where tgrelid = '<relation>'::regclass and not tgisinternal`
  and `select rulename from pg_rules where tablename = '<table>'`. An `after delete`
  trigger, such as an audit log, runs inside the check's transaction, and the
  rollback undoes its writes; the suite tests this.
- **Tables read inside set-returning functions.** The query plan shows the function
  call, not the tables the function reads, so the check cannot list them as not
  checked.
- **A dependency the data does not exercise.** If the database holds no rows after a
  cut, the comparison at that cut passes whatever the query does. DirtyDuck holds
  13,623 inspections known on or after 2017-04-01, the label cut of the last test
  date, so every label cut in this Experiment had later rows to delete.
- **Choices made by people who knew what happened later.** triage-pg does not detect
  item 9 of the DSSG list. Two habits limit it: compare model groups over every test
  date with `triage audition` rather than on the one date someone looked at, and keep
  a baseline in the grid so every number has a floor. In the guides Experiment the
  audition pick, group 41 (mean precision@10_pct 0.3854 ± 0.0228 over six test dates),
  differs from group 39, which the leaderboard ranks first on a single test date. The
  constant-prior baseline scores 0.2629 ± 0.0182, the base rate.

![Precision and recall over the population for model 285 and for the constant-prior baseline, model 286, at 2016-10-01; the baseline stays at the base rate and its recall follows the diagonal](../../../assets/guides/worked-curve-floor.svg)

*The best group's latest model (285 in our run) against the constant-prior baseline
of the same Run and split, at 2016-10-01. The baseline's precision stays near the
base rate of 0.26 (0.249 at 10% of the population, 0.261 at 50%).*

## The privilege the check needs

The check deletes rows inside one transaction, reads on the same connection, and
always ends with `ROLLBACK`. Other sessions never see the deletes. The role that runs
it needs:

- `DELETE` on every Source that declares a `knowledge_date_column`;
- `TEMPORARY` on the database, for the commit guard: a deferred foreign key on a
  temporary table that makes any `COMMIT` on the connection fail;
- Sources that are tables. A Source declared as a view gets exit 2 and a message
  asking for the base tables, each with its knowledge date.

A role without `DELETE` gets exit 2 and a message that names the relations. For a
hypothetical role `analyst` with `SELECT` only on DirtyDuck's Sources it would read:

```
The leakage check could not run: role 'analyst' has no DELETE privilege on
ontology.entities, ontology.events. The check deletes inside one transaction and
always rolls back; it never commits. Run it with a role that has DELETE on those
Sources, such as the role that loaded them.
```

The project role that `triage project create --owner <role> --data-schema <schema>`
grants has `select, insert, update, delete` on every table in the data schemas and
`temporary` on the database, so it can run the check. A read-only analyst role
cannot.

Four further facts follow from the design:

- The module has no commit call, and a test enforces it. A tripwire test fails if the
  pinned featurizer commits a caller's connection.
- After the rollback, the check reads every cut Source's fingerprint (row count and
  maximum knowledge date) on a fresh connection and compares it with the one taken
  before the run.
- Deleted rows stay locked until the rollback. `lock_timeout = '5s'` makes the check
  exit 2 instead of waiting when a loader already holds locks on a Source.
- A rolled-back delete still writes WAL in proportion to the rows deleted. `--dates`
  limits the work on large Sources.

## Leakage in one segment

Leakage can live in one segment of the Cohort: one program whose results are
entered late, one office that records a field after the outcome, one upstream
system with a proxy column. Averaged over the whole population, a perfect ranking inside a
small segment moves the metrics only a little. Each diagnostic has a form that looks
inside segments, and each guide has a leakage subsection with the details.

**The threshold curve, per Subset.** `triage model curve <id> --subset <name>` re-ranks
the Subset as its own population. A leak shows as precision near 1 at the top of one
Subset's curve. In DirtyDuck no curve comes close. The best group's latest model
reaches precision 0.529 at k = 100 over everyone at 2016-10-01. Across the five
Subsets, the highest precision at the top 10% of a Subset is 0.4167, for zip 60647
(545 facilities). See
[the threshold-curve guide](/triage-pg/guides/reading-the-threshold-curve/#leakage).

![Illustrative threshold curves: with a feature that knows the outcome, precision stays at 1.0 for the top tenth of the population; the honest curve falls from the start](../../../assets/guides/curve-leakage-signature.svg)

*Illustrative, not DirtyDuck: a feature that knows the outcome for 40% of the
positives holds precision at 1.0 for the top tenth of the population. The honest
curve does not.*

**The error trees, suspicious leaves.**
`triage postmodel error-rules <id> --suspicious` lists fp leaves with an error rate
of at most 0.02 and confusion leaves whose true-positive share is at least 0.98, each
holding at least 30 rows. Such a leaf names the segment and the feature that splits
it off. For the best group's latest model, the command prints
`No suspiciously-right leaves for model 285 at error ≤ 0.02 and support ≥ 30.`, and
the same with `--max-error 0.1`. Over all 84 fp and confusion leaves of the best group
at 10_pct, the lowest fp error rate is 0.233, and the lowest share of non-TP rows in a
confusion leaf is 0.568. See
[the error-tree guide](/triage-pg/guides/reading-the-error-trees/#leakage).

![All 84 fp and confusion leaves of the best model group at 10_pct, by support and error; the suspicious zone, error at most 0.02 with at least 30 rows, is empty](../../../assets/guides/worked-suspicious-leaves.svg)

*Every fp and confusion leaf of the best group (41 in our run) on its six test dates.
The shaded suspicious zone is empty.*

**The crosstabs, near-total separation.** A feature whose nonzero rate is near 1 on
the list and near 0 off it is the signature of a proxy for the outcome. In DirtyDuck,
for the best group's latest model at 2016-10-01, the widest gap in nonzero rate is
0.644, for `STDDEV(inspections.n_critical)`: 0.913 on the list against 0.269 off it.
The feature nearest the leak pattern, `SUM(inspections.n_serious)` (a serious
violation on record), is nonzero for 0.9986 of the list but also for 0.416 of the
facilities off it. That is the model's main signal; a leak would sit near 0 off the
list. `triage postmodel crosstabs` compares the whole list with the rest, not each
Subset's list. See
[the crosstabs guide](/triage-pg/guides/reading-the-crosstabs/#leakage).

![Illustrative nonzero rates on the list and off it, with a leak signature of 0.97 against 0.02 in the last row](../../../assets/guides/crosstab-selected-vs-rest.svg)

*Illustrative values, not DirtyDuck. The last row is the leak signature: 0.97 on the
list, 0.02 off it.*

When a diagnostic points at a segment, run `triage check leakage` on the config. If it
flags a component, the finding's `examples` and `columns` say where to look. If it is
clean, go through the [blind spots of the check](#blind-spots-of-the-check) for the
Source behind that segment: a proxy in the raw data or a value updated in place are
the usual causes.

## The two DSSG tests

The `dssg/randomize_your_data` README proposes two tests: randomize the raw data, and
flip the test labels. triage-pg turns the second into the exact check above and keeps
the first as a test of its own code.

### Flipped test labels and the exact check

The README's label-flip test flips the test labels and retrains. Training performance
should stay the same, because nothing built for training may read the test labels, and
test performance should fall below chance. The first expectation follows from a
narrower property: nothing built for training depends on data known after its
horizon. `triage check leakage` tests this property directly.

- It removes all data known after each cut, not only the labels.
- It compares the artifacts themselves (the Cohort, the labels and the feature
  columns) per `as_of_date`, so the check retrains no model.
- It uses no statistics. A difference is a count of entities, with example ids and,
  for features, column names, not a p-value.

The README notes that flipped labels give more statistical power than randomized
columns. The exact check needs no statistical power at all; its limits are the
[blind spots](#blind-spots-of-the-check) listed above.

The suite keeps a further flip claim as an invariant of triage-pg's evaluation SQL. In
`src/tests/test_randomized_null.py`, flipping every label must turn AUC into 1 − AUC
to 1e-12, and precision@k into 1 − precision@k when every row in the top k has a
label (precision counts only the labeled rows).

### Randomized raw data and the null-data tests

The README's other test sorts every column of the raw data independently. Each column
keeps its own distribution, the links between columns break, and the model should
predict no better than chance. triage-pg runs this as a suite test of its own train,
score and evaluate path, in `src/tests/test_randomized_null.py`, not as a user
command:

- Five shuffles of a synthetic Source pair, each run end to end. The mean test AUC
  must lie within 0.5 ± 4σ/√5, where σ² = (n₁ + n₀ + 1)/(12·n₁·n₀) is the null
  variance of the Mann–Whitney AUC, and each precision@k within the base rate
  ± 4·√(p(1−p)/k).
- Controls that show the test can fail. Unshuffled, the Experiment must reach
  AUC ≥ 0.75. On shuffled data, a planted train-on-test leak must reach AUC ≥ 0.9,
  and features that read the label window must reach AUC ≥ 0.75.

The test is not a user command because it is statistical, needs a retrain for each
shuffle, and can fail on real data without any leakage, for the reason below.

### Unequal entity rates break the randomization null

An independent shuffle of each column keeps the marginal distribution of the
`entity_id` column too. An entity with 28 events still has 28 after the shuffle, now
spread over random rows. With a label such as "any failed inspection in the next six
months", an entity with many events is more likely to have one in any window, so its
past event count predicts the label on randomized data, and no feature leaked
anything.

DirtyDuck's inspection rates are far from uniform. The 18,909 facilities with at least
one inspection have between 1 and 28 (median 3, 75th percentile 6, 99th percentile
12). A randomized run on data like this can beat the base rate through event counts
alone, so a result above chance needs a second look before anyone calls it leakage.
We did not run the randomization on DirtyDuck; this is the mechanism, not a measured
result.

The suite's generator removes the cause in two ways, as its docstring explains. Every
entity has exactly one event per month, a uniform rate. And every non-key column is
permuted within its date, so dates stay in place and each entity keeps one event per
month. A permutation of `entity_id` across all dates would keep each entity's total
but scatter it over the windows; an entity with more events in its feature window
then has fewer left for its label window, which predicts an "any event" label for the
same reason one level down.

## References

- R. Ghani, J. Walsh, J. Wang. "Top 10 ways your Machine Learning models may have
  leakage". Data Science for Social Good Fellowship blog, January 2020. Accessed
  2026-10-28.
  <https://dssgfellowship.org/2020/01/23/top-10-ways-your-machine-learning-models-may-have-leakage/>
- J. Walsh, J. Wang. `dssg/randomize_your_data`, README. GitHub repository of Data
  Science for Social Good. The README names no authors; Ghani, Walsh and Wang (2020)
  credit Walsh and Wang with the initial work. Accessed 2026-10-28.
  <https://github.com/dssg/randomize_your_data>
- [Point-in-time correctness](/triage-pg/concepts/point-in-time-correctness/): the rule
  the check tests, and the imputation boundary that protects it.
- The [CLI reference](/triage-pg/reference/cli/) for `triage check leakage`,
  `triage model curve` and `triage postmodel error-rules`.
