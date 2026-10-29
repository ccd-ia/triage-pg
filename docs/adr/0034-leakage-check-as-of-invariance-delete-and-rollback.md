# 0034. The leakage check tests as-of invariance by delete and rollback

- Status: Accepted
- Date: 2026-10-28
- Deciders: Adolfo De Unánue

## Context

triage-pg protects point-in-time correctness in the code it owns. timechop generates the
temporal splits, featurizer bounds every child stream with `as_of_boundary: exclusive`, and
fit-based imputation and categorical encoding are fitted on the training split only
(ADR-0009). Three inputs are written by the user, and triage-pg could not inspect them: the
cohort query, the label query, and the `knowledge_date_column` each Source declares. A label
query whose window ignores `{label_timespan}`, a cohort query that selects entities by later
activity, or a featurizer entity whose `temporal_ix` is the event date while its rows arrive
later each produce a model that scores better in evaluation than it will in production.

Ghani, Walsh and Wang ("Top 10 ways your Machine Learning models may have leakage", DSSG,
2020) and the `dssg/randomize_your_data` README (Wang and Walsh, 2018) propose two tests:
retrain on randomized raw data, and retrain with flipped test labels. Both are statistical
and both need a full retrain per trial. The flipped-label test rests on a narrower property
that can be tested directly: an artifact built for an `as_of_date` must not depend on data
known after the moment it is defined at.

## Decision

`triage check leakage CONFIG` tests that property exactly, per `as_of_date` and per
component, with no statistics and no retraining.

- **The property.** For every distinct `as_of_date` `t` of the temporal config, and every
  label timespan `lt` that date is used with, the artifact must not change when the
  declared Sources lose their rows known:
  - after `t`, for the cohort;
  - on or after `t` in event Sources (and Sources with no `role`) and after `t` in entity
    Sources, for the features (an entity in the cohort at `t` is known at `t`;
    strictly-before applies to events);
  - on or after `t + lt`, for the labels, the end of the window `[t, t + lt)`.
- **How data is cut.** The check opens its own connection and runs everything in one
  transaction that always ends in `ROLLBACK`. It builds the cohort, labels and features on
  all data, then deletes the rows each cut does not keep, in descending cut order, so the
  deletes are cumulative and each row is deleted at most once. After each cut
  it rebuilds the artifacts that cut applies to and compares them with the baseline. Cohort
  and label SQL run as plain `SELECT`s rendered by the builders' own helpers. featurizer runs
  through `to_arrow(connection=conn)` on the baseline cohort pairs, inside a savepoint that is
  rolled back.
- **Nothing can be committed.** The module has no commit call, and a test enforces it. A
  deferred foreign key on a temporary table makes any `COMMIT` on the connection fail. A
  tripwire test proves that the pinned featurizer never commits a caller's connection. After
  the rollback, the check reads every cut Source's fingerprint (row count and maximum
  knowledge date) again on a fresh connection, and it must equal the one taken before the
  run.
- **What is not cut is reported.** The check reads a Source without a
  `knowledge_date_column` uncut. It finds the relations that the cohort query, the label
  query or featurizer read (entity tables and `graph_relationships` edge tables) but the
  config does not declare, from the planner's `explain (format json)` output (views expand to base tables, so the check parses
  no SQL, ADR-0014). The report lists both as not checked.
- **The CLI contract.** `check` is a new command group for checks that need the project
  database and change nothing. Exit code 0 means no differences, 1 means differences found,
  2 means the check could not run (privilege, missing Source, broken foreign key, held lock,
  bad config). `--dates` limits the plan and `--json` writes the full report. Results are
  not persisted: the run ends in `ROLLBACK`, so persisting would need a second connection and
  a new table.

### Alternatives considered

- **A scratch-database copy.** Rejected: it copies all Source data and needs `CREATEDB` and
  `pg_dump`.
- **Rename and view.** Rename each Source inside the transaction and put a filtered view in
  its place. Rejected: it takes an `ACCESS EXCLUSIVE` lock on every Source, needs table ownership, and
  views that depend on a Source keep pointing at the renamed table by OID.
- **The randomization test as a user command.** Rejected for users: it is statistical, needs
  a retrain per trial, and can fail without any leakage when entity event rates differ. It is
  kept as an internal null-data test of triage-pg's own spine.

## Consequences

- The check needs `DELETE` privilege on every Source that declares a knowledge date, and the
  `TEMPORARY` privilege on the database. A role with `SELECT` only gets exit code 2 and a
  message naming the relations.
- The transaction holds one snapshot for the whole run, and its deleted rows stay locked
  until the rollback, so a concurrent writer to those rows waits. `lock_timeout = '5s'`
  makes the check fail fast when a loader already holds the locks.
- A rolled-back delete still writes WAL in proportion to each Source's deleted tail. On the
  DirtyDuck tutorial (22,169 entities, 74,191 inspections, 5 as_of_dates) the whole check
  took about 5 seconds.
- The check cannot see a `knowledge_date_column` that is itself declared wrong, an outcome
  proxy already present in the raw data, values updated in place after their knowledge date,
  or relations hidden inside set-returning functions. Those stay with the diagnostics and the
  reading guides.
- A featurizer release that commits on a caller's connection fails the tripwire test, and
  the commit guard turns such a commit into exit code 2 with every cut undone.
