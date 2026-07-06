# DonorsChoose (KDD Cup 2014) — Early-Warning-System tutorial dataset

A second triage-pg tutorial dataset alongside DirtyDuck, for a **binary early-warning
problem**: *will a newly-posted classroom project FAIL to be fully funded within 4 months?*
(the positive class is the project that needs help). Same packaging pattern as DirtyDuck —
a Postgres image with `raw → clean → ontology` init SQL.

## What's in the image

`docker compose up` builds, at first start, three layers (the
[`pg-data-discovery`](https://) methodology):

1. **`raw.*`** (`01_create_raw.sql`) — every column `text`, loaded by `COPY` from `/data`.
2. **`clean.*`** (`02_create_clean.sql`) — typed, snake_case, **nulls preserved** (no imputation).
3. **`ontology.*`** (`03_create_ontology.sql`) — the entity-state-event graph:
   - **`ontology.entities` = projects** (the target). Static attributes are point-in-time
     features; `teacher_acctid` / `schoolid` let the featurizer config synthesize prior-project
     history via a self-referential as-of join.
   - **`ontology.events` = resources** (line items known at posting → a leakage-free child
     stream featurizer aggregates).
   - **`ontology.project_funding`** (view) — the realized 4-month funding outcome, for EDA /
     teaching. Donations are the **label source only** (never features: zero at posting + leakage).

## Data: baked subset vs the real Kaggle data

The image ships a **deterministic ~3,000-project real-data subset** (2012–2013) + its donations /
resources / outcomes (~8 MB), so it runs with no download. The full KDD Cup 2014 data is
Kaggle-login-gated (`kaggle competitions download -c kdd-cup-2014-predicting-excitement-at-donors-choose`).
To use the **full data**, drop the four real CSVs into `donors_db/real/` and mount them — the
loader is identical:

```yaml
# in docker-compose.yml, under donors_db.volumes:
- ./donors_db/real:/data:ro
```

then `docker compose down -v && docker compose up -d` (the `-v` lets init re-run).
`donors_db/real/` is gitignored (the full CSVs are ~1.6 GB).

## The ML problem (triage-pg)

- **Entity**: a posted project. **as_of_date**: a monthly grid. **Cohort**: projects posted in
  the window. **Label**: unfunded within 4 months of posting (derived from donations vs
  `total_price`), `problem_type: classification`. Full-data base rate ≈ **34.6% unfunded**
  (2011–2013); clean low-cardinality categoricals, 0% null on feature columns.
- **Features (featurizer / ADR-0008)**: the project's own attributes (one-hot categoricals +
  numerics) + featurizer aggregates over the resources child stream + a self-referential as-of
  join for the teacher's/school's prior-project history (43.5% of teachers, 76% of schools have
  repeat projects). See `example/donorschoose/greenfield.yaml` (the triage experiment config).

## Recipes

```bash
just donors-up        # build + start the DB (port $DONORS_PG_PORT, default 5436)
just donors-shell     # psql into it
just donors-down      # stop
just donors-clean     # remove container + image + volume
```

## Running the experiment end-to-end

The greenfield run writes its results (`triage.*` schema: cohorts, labels, matrices, models,
predictions, evaluations) into the **same** database that holds the source `ontology.*` data — so
the steps are: start the DB, point triage at it, create the `triage.*` schema, then run.

```bash
# 1. start the tutorial DB (default host port 5436; override with DONORS_PG_PORT if taken)
just donors-up

# 2. a dev DB config for triage (baked tutorial creds; gitignored, recreate as needed)
cat > donorschoose-database.yaml <<'YAML'
host: 127.0.0.1
user: donors_user
pass: some_password
port: 5436
db: donors
YAML

# 3. create the triage results schema inside the donors DB (DATABASE_URL overrides the recipe's
#    default database.yaml target)
DATABASE_URL=postgresql://donors_user:some_password@127.0.0.1:5436/donors \
  just alembic upgrade head

# 4. run cohort → labels → matrices → train → predict → evaluate
uv run triage --dbfile donorschoose-database.yaml run \
  example/donorschoose/greenfield.yaml --project-path /tmp/donors-run
```

A successful run reports something like *1 run, 20 models, 1820 predictions, 120 evaluations*
across 4 temporal splits (6-month retrain cadence over 2012–2013). The featurizer ER-graph
(`example/donorschoose/greenfield.yaml`) synthesizes ~175 features per matrix: the project's own
one-hot attributes + numeric ask, resource-line-item aggregations, and the **self-referential
teacher/school prior-posting history** (an as-of self-join on `teacher_acctid` / `schoolid`). On
the baked subset the signal is modest (test AUC ≈ 0.58, average-precision ≈ 0.42 over a ~0.32
base rate); the teacher/school history features carry far more weight on the full KDD data, where
43.5% of teachers and 76% of schools have repeat projects.

## Inspect + diagnose (after the run)

```bash
export DATABASE_URL=postgresql://donors_user:some_password@127.0.0.1:5436/donors
uv run triage leaderboard <experiment-hash>
uv run triage models <experiment-hash>            # groups: avg ± σ, max regret, fit time
uv run triage audition <experiment-hash>
uv run triage model show <model-id>
uv run triage postmodel crosstabs <model-id> -p 100_abs
uv run triage postmodel error-tree <model-id> -p 100_abs
just serve 8014
```

See `docs/postmodeling.md` for what each diagnostic answers, and `docs/fairness.md` for
the `bias_config:` block (e.g. audit by `poverty_level` — an assistive intervention, so
the fairness tree routes attention to FNR/FOR parity).
