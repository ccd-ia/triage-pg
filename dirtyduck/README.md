# DirtyDuck — the canonical triage-pg tutorial dataset

> This README is the terse runbook. For the narrated case study, read [the DirtyDuck case study (and the Dirty Duckling smoke test)](https://ccd-ia.github.io/triage-pg/tutorials/dirtyduck/).

The classic DSSG teaching problem, repackaged for triage-pg: **will a food facility fail
its next inspection within 6 months?** (Chicago food-inspections data in a self-contained
Postgres docker.) This is the dataset the quickstart's sibling paths, the problem-type
variants, and the E2E smokes all run against.

## Run it

```bash
just tutorial-up          # build + start the food DB (PostGIS image; host port $DIRTYDUCK_PG_PORT, default 5440)

# point triage at it (baked tutorial creds; the file is gitignored)
cat > dirtyduck-database.yaml <<'YAML'
host: 127.0.0.1
user: food_user
pass: some_password
port: 5440   # match DIRTYDUCK_PG_PORT if you overrode it
db: food
YAML

# create the triage results schema inside the food DB
DATABASE_URL=postgresql://food_user:some_password@127.0.0.1:5440/food \
  just alembic upgrade head

# the full pipeline: cohort → labels → featurizer → matrices → train → evaluate
uv run triage --dbfile dirtyduck-database.yaml run \
  example/dirtyduck/experiment.yaml --project-path /tmp/dirtyduck-run
```

Inspect the results — headless or in the browser (ADR-0012):

```bash
export DATABASE_URL=postgresql://food_user:some_password@127.0.0.1:5440/food
uv run triage leaderboard <experiment-hash>
uv run triage models <experiment-hash>            # groups: avg ± σ, max regret, fit time
uv run triage audition <experiment-hash>          # 8 selection rules + divergence check
uv run triage model show <model-id>

# diagnostics (persisted; also visible on the dashboard model card)
uv run triage postmodel crosstabs <model-id> -p 100_abs
uv run triage postmodel error-tree <model-id> -p 100_abs

just serve 8014           # the dashboard → http://127.0.0.1:8014/
just tutorial-shell       # psql, e.g.: select * from triage.leaderboard;
```

`just tutorial-down` stops it; `just tutorial-clean` removes containers/volumes/images.

## The ML problem

- **Entity**: a food facility (license). **as_of_date**: a monthly grid (timechop).
- **Cohort**: facilities *active at* the as_of_date (their `activity_period` covers it).
- **Label**: a failed inspection within the next 6 months → `problem_type: classification`.
- **Features** (featurizer, ADR-0008): facility attributes + the **inspection-history
  child stream** aggregated point-in-time-correctly via as-of joins (counts, failure
  sums, days-since, over multiple windows).

## Problem-type variants — same data, different targets

DirtyDuck doubles as the problem-type showcase (ADR-0010/0026); each variant is a
committed config against the same food DB:

| Config | `problem_type` | Target |
|---|---|---|
| [`example/dirtyduck/experiment.yaml`](../example/dirtyduck/experiment.yaml) | `classification` | fails an inspection within 6 months |
| [`example/dirtyduck/experiment-regression.yaml`](../example/dirtyduck/experiment-regression.yaml) | `regression_ranking` | violation COUNT over the label window, ranked (config-selectable metrics via the `evaluation:` block) |
| [`example/dirtyduck/experiment-survival.yaml`](../example/dirtyduck/experiment-survival.yaml) | `survival` | time-to-failure `(duration, event_observed)`; C-index in PL/pgSQL |
| [`example/dirtyduck/experiment-deepgrid.yaml`](../example/dirtyduck/experiment-deepgrid.yaml) | `classification` | a wider hyperparameter grid (+ a no-categoricals ablation twin) |

Run any of them exactly like the base config — only the YAML path changes.

## Optional config blocks worth trying here

```yaml
# fairness auditing (docs/fairness.md) — facility_type as the protected attribute:
bias_config:
  query: |
    select entity_id, facility_type
    from ontology.entities
    where start_time < '{as_of_date}'
  parameter: 100_abs
  intervention: punitive     # an inspection visit is a burden -> FPR/FDR parity

# cohort-slice evaluations (the subset is the population):
evaluation:
  subsets:
    - name: restaurants
      query: |
        select entity_id from ontology.entities
        where facility_type = 'restaurant' and start_time < '{as_of_date}'
```

Both are identity-neutral — adding them re-runs cheaply against the cached pipeline
without changing the experiment hash (ADR-0022).

## What's in the image

The `food_db/` docker builds `raw → clean → ontology` schemas at first start (the same
`pg-data-discovery` layering the other tutorials use), with a deterministic baked subset
of the City of Chicago food-inspections open dataset — no download needed. The
`ontology.*` tables (entities + inspection events) are exactly what the featurizer
config's ER-graph declares under `sources:` (ADR-0014).
