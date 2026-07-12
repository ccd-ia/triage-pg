---
title: "Dirty Duckling — the smoke test"
description: Prove your triage-pg installation works, end to end, in about ten minutes — then take the five-minute dashboard tour.
sidebar:
  order: 1
  label: Dirty Duckling (smoke test)
---

This page does one job: **prove your setup works**. Every step has a PASS
criterion; if all of them hold, your machine can run everything else on this
site. It is triage-pg's homage to DSSG triage's
[Dirty Duckling](https://dssg.github.io/triage/dirtyduck/dirty_duckling/) —
the fast way to test the waters before the full
[DirtyDuck case study](/triage-pg/tutorials/) *(lands next in this series)*.

You need: **Docker**, **[uv](https://docs.astral.sh/uv/)**, and a checkout of
[ccd-ia/triage-pg](https://github.com/ccd-ia/triage-pg). Everything runs from
the repo root. About ten minutes total; the food-inspections database and the
experiment run entirely on your machine.

## Step 1 — the CLI exists

```bash
uv sync --extra dev --extra dashboard
uv run triage --version
```

**PASS:** the version prints:

```text
triage-pg 1.0.0
```

**If it fails:** `uv: command not found` → install uv
(`curl -LsSf https://astral.sh/uv/install.sh | sh`). A Python resolution error
→ you need Python 3.12+ (`uv python install 3.12`).

## Step 2 — the tutorial database is up

```bash
just tutorial-up          # docker compose: builds + starts the food DB
pg_isready -h 127.0.0.1 -p 5440
```

**PASS:**

```text
127.0.0.1:5440 - accepting connections
```

**If it fails:** Docker isn't running (start Docker Desktop / `dockerd`), or
port 5440 is taken — set another port and re-run:
`export DIRTYDUCK_PG_PORT=5444 && just tutorial-up` (then use that port and
adjust `dirtyduck-database.yaml` accordingly). First build takes a few minutes;
`just tutorial-logs` shows progress.

## Step 3 — the results schema exists

```bash
uv run triage --dbfile dirtyduck-database.yaml db upgrade
```

**PASS:** migrations stream by and it ends with:

```text
Database upgraded.
```

The food database ships with the *source* tables (`raw`, `clean`,
`ontology.*`); this creates the `triage` schema — experiments, runs, the
artifact DAG, append-only predictions, the in-PG evaluation functions — via
alembic, idempotently (re-running is a no-op).

## Step 4 — the config validates

```bash
uv run triage --dbfile dirtyduck-database.yaml analyze-config example/dirtyduck/experiment.yaml
```

**PASS:** a panel report with **no errors** — the temporal splits, a model grid
of 5, and the cohort/label SQL summaries:

```text
  Avg train as_of dates     2.5
  Model grid size             5
╭──────────── Label Configuration ────────────╮
│ Label name: failed_inspections              │
│ SQL: select entity_id, bool_or(result =     │
│ 'fail')::integer as outcome from            │
│ ontology.events where {as_of_date}…         │
╰─────────────────────────────────────────────╯
```

This is the same validator the write-webapp runs before accepting a
submission — errors come back path-addressed (`temporal_config.…`,
`label_config.query`) so you know exactly what to fix.

## Step 5 — the pipeline runs end to end

```bash
uv run triage --dbfile dirtyduck-database.yaml run \
  example/dirtyduck/experiment.yaml --project-path /tmp/dirtyduck-run
```

One command walks the whole pipeline — a few minutes on a laptop:

![The pipeline: cohort+labels → features (DFS, as-of joins) → matrices → train+predict → in-database evaluation](../../../assets/tutorials/pipeline-5box.svg)

It builds the cohort and labels, generates point-in-time-correct features
(featurizer's as-of joins), assembles train/test matrices per temporal split,
trains a small grid, appends predictions, and evaluates in-database.

**PASS:** the terminal ends with exactly this shape:

```text
Experiment b9e38fd8f366… completed: 1 run(s), 20 model(s), 268860 prediction(s),
120 evaluation(s).
  run <your-run-id>… (all-features): 20 model(s), 268860 prediction(s), 120
evaluation(s).
storage: /tmp/dirtyduck-run
```

Two things to check beyond the counts:

- **Your experiment hash must be `b9e38fd8f366…` too.** The hash is computed
  from the *problem* (cohort + label + temporal config, nothing else) — if
  yours differs, you edited the config; that's a different experiment, which
  is exactly the reproducibility contract working.
- The run id after it is yours alone — every attempt gets a fresh one.

**If it fails** mid-run, the error names the failing stage (cohort, labels,
features, matrix, model). Re-running is safe: completed artifacts are
content-addressed and cache-hit, so a re-run resumes instead of redoing.

## Step 6 — the results are queryable

```bash
uv run triage --dbfile dirtyduck-database.yaml leaderboard b9e38fd8
```

**PASS:** a ranked table — 5 model groups × 4 test splits (as-of dates
2015-07 → 2017-01), `auc_roc` by default, logistic regressions and tree
ensembles trading places at the top:

```text
  Group   Model   Algorithm              Metric    As-of        Value
  5       20      ScaledLogisticRegre…   auc_roc   2017-01-01   0.5751
  4       19      ScaledLogisticRegre…   auc_roc   2017-01-01   0.5748
  3       18      RandomForestClassif…   auc_roc   2017-01-01   0.5612
  …
```

Hash prefixes work everywhere the CLI takes a hash, git-style.

## PASS — now the five-minute tour

Your installation works. Point the dashboard at the same database and look at
what you just built:

```bash
just serve 8001    # then open http://127.0.0.1:8001
```

(The dashboard reads the same `PG*`/dbfile resolution as the CLI; the quickest
route is `cp dirtyduck-database.yaml database.yaml` before serving.)

![The experiment overview: model groups × temporal splits, best-in-split outlined](../../../assets/tutorials/experiment-overview.png)

Five things worth 60 seconds each:

1. **The experiment header** — the `classification` pill and the per-split
   cohort / %-labeled / base-rate sparklines. The hash chip is the same
   `b9e38fd8…` the CLI printed.
2. **The heatmap** (Overview tab) — model groups × splits; the outlined cell
   is the best model per split; click one to open its model card.
3. **A model card** — threshold curves (precision/recall as you sweep the
   list size k), score histogram, feature importances.
4. **The Derivation tab** — the content-addressed artifact DAG the run built;
   re-run the same command and watch everything cache-hit.
5. **The Audition tab** — DSSG's model-selection rules (distance from best,
   regret) computed in PostgreSQL.

## Where next

- The full **DirtyDuck case study** — same data, the whole discussion: early
  warning vs resource prioritization, leakage, fairness, model selection
  *(next in this series)*.
- The [onboarding one-pager](https://ccd-ia.github.io/triage-pg/onboarding.html)
  for the system-at-a-glance view.
- `just tutorial-down` stops the database; `just tutorial-clean` removes it
  entirely (containers, images, volumes).
