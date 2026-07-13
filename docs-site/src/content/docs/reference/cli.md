---
title: The command line, workflow by workflow
description: The triage CLI is the complete product — every surface, with real output.
sidebar:
  order: 3
  label: CLI tour
---

The CLI is not a companion to the dashboard — it's the **complete product**
(ADR-0012: headless-complete core). Everything below reads the same SQL views
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
triage-pg 1.0.0
```

## Set up a project database

```console
$ triage db upgrade          # alembic → the triage schema, idempotent
Database upgraded.
```

(`triage db history|stamp|downgrade` for the rest of the alembic surface;
`triage project create|drop|list` for registry-managed one-database-per-project
lifecycles.)

## Validate before running

```console
$ triage analyze-config example/dirtyduck/experiment.yaml
  Avg train as_of dates     2.5
  Model grid size             5
╭──────────── Label Configuration ────────────╮
│ Label name: failed_inspections              │
│ SQL: select entity_id, bool_or(result =     │
│ 'fail')::integer as outcome …               │
╰─────────────────────────────────────────────╯
```

The same validator backs the webapp's submission form — errors come back
path-addressed (`temporal_config.…`, `label_config.query`).

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
everywhere (ADR-0011).

## Operate

```console
$ triage score 20 2019-12-01
Forward-scored model 20 at 2019-12-01 (append-only).
```

The monitoring entrypoint (ADR-0027) — schedule it with cron or EventBridge;
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
