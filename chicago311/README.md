# Chicago 311 Service Requests — Early-Warning-System tutorial dataset

> This README is the terse runbook. For the narrated case study, read [the Chicago 311 tutorial](https://ccd-ia.github.io/triage-pg/tutorials/chicago311/).

A third triage-pg tutorial dataset (alongside DirtyDuck and DonorsChoose), for a **binary
early-warning problem**: *for a service request that was just filed, will it take LONGER than the
SLA (14 days) to resolve?* (the positive class is the slow request an operator would escalate).
Same packaging pattern: a Postgres image with `raw → clean → ontology` init SQL.

## What's in the image

`just chi311-up` builds, at first start, three layers (the `pg-data-discovery` methodology):

1. **`raw.requests`** (`01_create_raw.sql`) — every column `text`, loaded by `COPY` from `/data`.
2. **`clean.requests`** (`02_create_clean.sql`) — typed, snake_case, **nulls preserved** (no imputation).
3. **`ontology.*`** (`03_create_ontology.sql`) — the entity-state-event graph:
   - **`ontology.entities` = service requests** (the target). Filing-time attributes (type,
     department, origin, location, time) are point-in-time features; `community_area` /
     `sr_short_code` let the featurizer config aggregate geographic + type **backlog** via as-of joins.
   - **`ontology.events` = filing events** (`entity_id`, `community_area`, `sr_type`,
     `sr_short_code`, `date`) — a leakage-free child stream carrying only where/what/when, never
     the resolution, so the as-of aggregations stay point-in-time-correct.
   - **`ontology.request_resolution`** (view) — realized `days_to_close` + the 14-day SLA breach,
     for EDA / teaching. `closed_date` is the **label source only** (it is the future being predicted).

## Data: baked subset vs a larger export

The image ships a **deterministic real-data subset**: 30,654 field-service requests filed in **2019**
across three contrasting community areas (25 Austin, 22 Logan Square, 49 Roseland) and five
crew-dispatch request types (Pothole, Street Light Out, Graffiti Removal, Weed Removal, Rodent
Baiting), ordered by `sr_number` (~9 MB), so it runs with no download. It is a column- and
row-projection of the City's open dataset `v6vf-nfxy`; reproduce it exactly with:

```bash
curl -G 'https://data.cityofchicago.org/resource/v6vf-nfxy.csv' \
  --data-urlencode '$select=sr_number,sr_type,sr_short_code,owner_department,status,origin,created_date,closed_date,last_modified_date,street_address,zip_code,community_area,ward,police_district,latitude,longitude,created_day_of_week,created_hour,created_month,duplicate,legacy_record' \
  --data-urlencode "\$where=created_date>='2019-01-01T00:00:00' AND created_date<'2020-01-01T00:00:00' AND sr_type IN ('Pothole in Street Complaint','Street Light Out Complaint','Graffiti Removal Request','Rodent Baiting/Rat Complaint','Weed Removal Request') AND community_area IN (25,22,49)" \
  --data-urlencode '$order=sr_number' --data-urlencode '$limit=50000' \
  -o chi311_db/real/requests.csv
```

To run on that (or any other) export instead of the baked subset, drop a CSV with the **same
21-column header** into `chi311_db/real/requests.csv`, uncomment the `:/data/requests.csv` volume in
`docker-compose.yml`, then `docker compose down -v && docker compose up -d` (the `-v` lets init
re-run). `chi311_db/real/` is gitignored.

## The ML problem (triage-pg)

- **Entity**: a filed request. **as_of_date**: a monthly grid. **Cohort**: requests filed in the
  prior month. **Label**: not resolved within 14 days of filing (`closed_date` vs `created_date`),
  `problem_type: classification`. Subset base rate ≈ **27% slow**.
- **Features (featurizer / ADR-0008)**: the request's own attributes (one-hot `sr_type` /
  `owner_department` / `origin`, numeric `ward` / time-of-filing) + as-of **backlog** aggregations —
  recent request volume in the same community area (`area_backlog`) and recent demand for the same
  service type (`type_demand`). See `example/chicago311/experiment.yaml`.
- **Why it's a good teaching contrast**: most of the signal lives in one categorical — `sr_type`
  structurally determines resolution speed (Pothole ~73% slow / median 37 days vs Graffiti ~0.4% /
  same-day), so an honest model reaches AUC ≈ 0.87 with **no leakage** (resolution is never a
  feature). DonorsChoose, by contrast, has diffuse signal.

## Recipes

```bash
just chi311-up        # build + start the DB (port $CHI311_PG_PORT, default 5438)
just chi311-shell     # psql into it
just chi311-down      # stop
just chi311-clean     # remove container + image + volume
```

## Running the experiment end-to-end

The greenfield run writes its results (`triage.*` schema) into the **same** database that holds the
source `ontology.*` data: start the DB, point triage at it, create the `triage.*` schema, then run.

```bash
# 1. start the tutorial DB (default host port 5438; override with CHI311_PG_PORT if taken)
just chi311-up

# 2. a dev DB config for triage (baked tutorial creds; gitignored, recreate as needed)
cat > chicago311-database.yaml <<'YAML'
host: 127.0.0.1
user: chi311_user
pass: some_password
port: 5438
db: chi311
YAML

# 3. create the triage results schema inside the chi311 DB
DATABASE_URL=postgresql://chi311_user:some_password@127.0.0.1:5438/chi311 \
  just alembic upgrade head

# 4. run cohort → labels → matrices → train → predict → evaluate
uv run triage --dbfile chicago311-database.yaml run \
  example/chicago311/experiment.yaml --project-path /tmp/chi311-run
```

A successful run reports something like *1 run, 20 models, 58,190 predictions, 120 evaluations*
across 4 quarterly splits; ~28 features/matrix (request one-hot + numeric attributes + area/type
backlog counts). Test AUC averages ≈ 0.87 — high because `sr_type` carries most of the signal, and
honest because resolution timing is never fed in as a feature.

## Inspect + diagnose (after the run)

```bash
export DATABASE_URL=postgresql://chi311_user:some_password@127.0.0.1:5438/chi311
uv run triage leaderboard <experiment-hash>       # the leaderboard, headless (ADR-0012)
uv run triage models <experiment-hash>            # groups: avg ± σ, max regret, fit time
uv run triage audition <experiment-hash>          # 8 selection rules + divergence
uv run triage model show <model-id>               # card + calibration deciles
uv run triage postmodel crosstabs <model-id> -p 300_abs    # what characterizes the top-k
uv run triage postmodel error-tree <model-id> -p 300_abs   # where the model fails
just serve 8014                                   # → the dashboard
```

Fairness by geography (the honest protected-attribute proxy here — `docs/fairness.md`)
and per-area subset evaluations are one config block each; both worked examples live in
`docs/fairness.md` and `docs/quickstart.md`. A survival variant of this problem
(time-to-resolution, `problem_type: survival`) ships as
`example/chicago311/experiment-survival.yaml`.
