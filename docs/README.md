# triage-pg documentation

**triage-pg** is a PostgreSQL-native, deliberately simplified fork of DSSG's `triage` for temporal
ML on tabular public-policy data. It keeps triage's pipeline shape — cohort → features → matrices →
train → predict → evaluate — on a much smaller surface area built around plain PostgreSQL.

These docs are plain Markdown (rendered on GitHub). The durable design lives in the
[ADRs](#architecture-decision-records-adrs) and [`../CONTEXT.md`](../CONTEXT.md) (the domain
glossary — use those terms exactly).

## Start here — the mental model

| Read | For |
|---|---|
| [`../CONTEXT.md`](../CONTEXT.md) | The glossary: Project, Registry, Profile, as_of_date, Cohort, Matrix, **Experiment**, **Run**, Feature engine, Adapter, Prediction. |
| [experiment-and-run.md](experiment-and-run.md) | The single most important model: an **Experiment is a problem**, a **Run is one attempt** (ADR-0022/0023). Caching, leaderboards, monitoring all follow from this. |
| [problem-types.md](problem-types.md) | The score→rank→evaluate **spine** and the `problem_type` switch — classification / regression-as-ranking / pure regression / survival-ready (ADR-0010). |

## Tutorials — three runnable datasets

Each ships a self-contained Postgres docker (`raw → clean → ontology` init SQL, a baked real-data
subset) plus a greenfield experiment config. All are early-warning-system (classification) problems;
together they span the featurizer patterns (child event streams, self-referential as-of history,
geographic/type backlog).

| Dataset | Problem | Featurizer ER-graph | Run it |
|---|---|---|---|
| **DirtyDuck** (`dirtyduck/`, `example/dirtyduck/greenfield.yaml`) | Will a food facility fail its next inspection within 6 months? | facility attrs + inspection-history child | `just tutorial-up` |
| **DonorsChoose** ([`../donorschoose/README.md`](../donorschoose/README.md), `example/donorschoose/greenfield.yaml`) | Will a posted project fail to be funded within 4 months? | project attrs + resources child + **self-referential teacher/school history** | `just donors-up` |
| **Chicago 311** ([`../chicago311/README.md`](../chicago311/README.md), `example/chicago311/greenfield.yaml`) | Will a filed service request miss the 14-day resolution SLA? | request attrs + **area/type backlog** as-of children | `just chi311-up` |

Each dataset README has the full end-to-end run recipe (start DB → `alembic upgrade head` into it →
`triage run`).

## Design specs

| Doc | What |
|---|---|
| [schema-design.md](schema-design.md) | The greenfield results schema: registry control plane + per-project `triage` schema (§8 records resolved decisions). |
| [erd.md](erd.md) | Entity-relationship diagram of the per-project `triage` schema (mermaid). |
| [adapter-spec.md](adapter-spec.md) | The triage-pg ↔ featurizer seam: timechop `temporal_config`, the featurizer ER-graph + cohort→target mapping, imputation policy wiring. |
| [derivation-dag.md](derivation-dag.md) | The Guix-style artifact derivation DAG: content-addressed identity over the full input closure (ADRs 0013–0017). |
| [featurizer-scale.md](featurizer-scale.md) | The scale validation for featurizer (ADR-0008): per-as_of_date cost is constant-to-sub-linear. |
| [read-dashboard-spec.md](read-dashboard-spec.md) | The read-only dashboard: in-PG views → FastAPI JSON + SSE (ADR-0012/0021). |
| [cloud-profile-spec.md](cloud-profile-spec.md) | The cloud profile: RDS/IAM auth + S3 storage + AWS Batch execution (ADRs 0003–0005). |

## Architecture Decision Records (ADRs)

The accepted, durable decisions ([`adr/`](adr/)). Read these before making architectural changes.

**Foundation & deployment**
- [0001](adr/0001-clean-fork-fresh-repo-greenfield-schema.md) Clean fork, fresh repo, greenfield schema
- [0002](adr/0002-database-per-project-shared-cluster.md) Database-per-project + registry control plane
- [0003](adr/0003-plain-postgres-substrate-two-profiles.md) Plain-PostgreSQL substrate, local/cloud profiles
- [0004](adr/0004-cloud-profile-rds-iam-auth.md) Cloud auth via RDS/IAM · [0005](adr/0005-aws-batch-execution.md) Execution via AWS Batch

**Pipeline & modeling**
- [0006](adr/0006-append-only-predictions.md) Append-only predictions · [0007](adr/0007-in-postgres-evaluation-and-sql-bias-metrics.md) In-PG evaluation + SQL bias metrics
- [0008](adr/0008-featurizer-as-feature-engine.md) featurizer is the feature engine · [0009](adr/0009-imputation-split-fit-free-vs-fit-based.md) Imputation split (fit-free / fit-based)
- [0010](adr/0010-problem-type-ranking-spine-survival-ready-labels.md) problem_type ranking spine, survival-ready labels
- [0011](adr/0011-no-standalone-postmodeling-module.md) No standalone postmodeling · [0012](adr/0012-headless-core-deferred-thin-uis.md) Headless-complete core
- [0020](adr/0020-defer-in-container-grid-parallelism.md) Defer in-container grid parallelism

**Artifact identity & derivation DAG**
- [0013](adr/0013-artifact-identity-derivation-hash.md) Derivation-hash identity · [0014](adr/0014-source-data-pinning.md) Source-data pinning
- [0015](adr/0015-artifact-dag-node-granularity.md) Node granularity · [0016](adr/0016-engine-versions-in-identity.md) Engine versions in identity
- [0017](adr/0017-gc-outputs-not-history.md) GC: outputs, not history · [0018](adr/0018-retrain-provenance-on-runs.md) Retrain provenance on runs

**Data layer, UI, experiment model**
- [0019](adr/0019-psycopg3-native-data-layer.md) psycopg3-native data layer · [0021](adr/0021-live-run-progress-via-pg-notify-sse.md) Live run progress via pg_notify → SSE
- [0022](adr/0022-experiment-is-the-problem-runs-are-attempts.md) An Experiment is the problem; runs are attempts
- [0023](adr/0023-feature-groups-and-strategies.md) Feature groups + mixing strategies
- [0024](adr/0024-write-webapp-registry-auth-seam.md) Write webapp: POST routes over the registry + a pluggable user-auth seam
