# Quickstart — zero to a running, inspectable experiment

The fastest way to see triage-pg work end-to-end is the **Chicago 311 tutorial dataset**
(real Socrata data, baked into a docker Postgres). Then, for your own data, the
**project lifecycle** provisions an isolated per-project database in one command
(ADR-0002). Both paths end at the dashboard.

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker, a `just` runner
(`brew install just`), and Node (only for rebuilding the dashboard SPA).

```bash
uv sync --extra dev --extra dashboard
```

## Path A — the tutorial dataset (single project, ~5 minutes)

```bash
# 1. start the Chicago 311 DB (30,654 real 2019 service requests; host port 5438)
just chi311-up

# 2. point triage at it (baked tutorial creds; the file is gitignored)
cat > chicago311-database.yaml <<'YAML'
host: 127.0.0.1
user: chi311_user
pass: some_password
port: 5438
db: chi311
YAML

# 3. create the triage results schema inside that same DB
DATABASE_URL=postgresql://chi311_user:some_password@127.0.0.1:5438/chi311 \
  just alembic upgrade head

# 4. run the experiment: cohort → labels → features → matrices → train → evaluate
uv run triage --dbfile chicago311-database.yaml run \
  example/chicago311/greenfield.yaml --project-path /tmp/chi311-run
```

The run prints its experiment hash and per-run model/prediction/evaluation counts. Inspect it:

```bash
# headless (ADR-0012): everything is SQL over the in-PG views…
just chi311-shell   # then e.g.:  select * from triage.leaderboard;

# …or the same views as Rich tables, straight from the CLI:
export DATABASE_URL=postgresql://chi311_user:some_password@127.0.0.1:5438/chi311
uv run triage leaderboard <experiment-hash>       # the leaderboard matview
uv run triage models <experiment-hash>            # groups: avg ± σ, max regret, fit time
uv run triage audition <experiment-hash>          # all 8 selection rules + divergence
uv run triage model show <model-id>               # one model's card + calibration

# or the dashboard
just serve 8014
# → http://127.0.0.1:8014/
```

![The experiments list](images/experiments-list.png)

Diagnose a model (computed from its matrix once, persisted, then visible on the
dashboard's model card too — `docs/postmodeling.md`):

```bash
uv run triage postmodel crosstabs <model-id> -p 100_abs    # what characterizes the top-k
uv run triage postmodel error-tree <model-id> -p 100_abs   # where the model fails (fp/fn rules)
uv run triage postmodel compare <model-a> <model-b>        # do two models flag the same entities?
```

Fairness auditing (`docs/fairness.md`) and cohort-slice evaluations are one config block
each — `bias_config:` populates `triage.protected_groups` and writes per-group
disparities + τ verdicts; `evaluation.subsets:` evaluates every metric on named cohort
slices (the subset is the population). Both are identity-neutral: adding them does not
change the experiment hash.

The other two tutorial datasets work identically: `just tutorial-up` (DirtyDuck food
inspections, default port 5434 — override with DIRTYDUCK_PG_PORT) and `just donors-up` (DonorsChoose KDD Cup 2014, port 5436), each
with an `example/<dataset>/greenfield.yaml` and a README describing its problem.

## Path B — your own project (the ADR-0002 lifecycle)

A *Project* is one isolated PostgreSQL database plus a row in the **registry** control
plane. With a cluster and a registry database available:

```bash
# one-time: create the registry schema in its database
DATABASE_URL=<registry-db-url> just alembic-registry upgrade head

# the lifecycle commands need these two env vars (direnv-friendly):
export TRIAGE_REGISTRY_URL=<registry-db-url>
# optional — defaults to the registry cluster's 'postgres' database:
# export TRIAGE_MAINT_URL=<cluster-maintenance-url>

# registry row → CREATE DATABASE → triage schema at head, in one gesture
uv run triage project create myproject --display-name "My Project"

uv run triage project list
uv run triage project drop myproject --confirm myproject   # teardown = DROP DATABASE
```

Load your source data into the new database (`raw` → `clean` → `ontology` schemas — see the
tutorial `*_db/` dockers for the pattern), declare it under `sources:` in your config
(ADR-0014 pinning), then `triage run your-greenfield.yaml`.

## The dashboard across many projects (ADR-0025)

One dashboard instance serves every registry project via the top-bar **project switcher**:

```bash
export TRIAGE_REGISTRY_URL=<registry-db-url>
# only needed when projects live in separate clusters/containers (the tutorial dockers do):
export TRIAGE_PROJECT_DB_MAP='{"chi311": "postgresql://…:5438/chi311", "food": "postgresql://…:5434/food"}'
just serve 8014
```

Switching projects re-points every panel at that project's database; on a shared cluster
(or cloud RDS) no map is needed — the routing swaps the database name on the base
connection and the registry stays credential-free (ADR-0002).

## Submitting from the webapp (ADR-0024)

With a registry configured, **Projects** and **Submissions** appear in the dashboard nav.
The submit form takes a config three ways — pick a committed example, upload a
`.yaml`/`.json`, or paste — and **Validate** dry-runs it server-side (`POST
/api/validate-config`): you get the derived experiment hash, split/grid counts, and
path-addressed errors before anything runs. Submit is enabled once the exact text on
screen validates clean; the run itself goes through the same `run_experiment` path as
`triage run`, and every submission lands in the append-only audit trail.

## Where to go next

- `docs/README.md` — the documentation index (mental model, problem types, ADRs).
- `docs/experiment-and-run.md` — what an Experiment *is* (ADR-0022) and how runs, caching,
  and the artifact derivation DAG behave.
- `CONTEXT.md` — the domain glossary; `docs/adr/` — every architecture decision.
