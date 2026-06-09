# triage-pg

**A PostgreSQL-native, deliberately simplified fork of [`triage`](https://github.com/dssg/triage) for temporal machine learning on tabular public-policy data.**

triage-pg builds end-to-end early-warning and resource-prioritization models — cohort selection, temporally-correct feature generation, training, prediction, and in-database evaluation — with PostgreSQL as the single substrate, aimed at teaching and consulting.

> **Status: early.** The architecture and database schema are designed (see [`docs/adr/`](docs/adr/) and [`docs/schema-design.md`](docs/schema-design.md)); the first runnable slice is under construction. Not yet released or installable.

## Acknowledgment — built on DSSG's triage

triage-pg is a fork of **[triage](https://github.com/dssg/triage)**, created by the **Center for Data Science and Public Policy (DSaPP) at the University of Chicago** and maintained at **Carnegie Mellon University**. The hard, valuable ideas at the heart of this project are theirs: temporal cross-validation (`timechop`), leakage-safe feature engineering, reproducible model governance and hashing, bias auditing, and the whole "operational design questions → modeling choices" framing. The feature engine triage-pg adopts, [`featurizer`](https://github.com/nanounanue/featurizer), is likewise a DSSG-lineage Deep Feature Synthesis project.

triage-pg stands on that work, **preserves its MIT license and copyright**, and keeps the full git history for attribution. Thank you to the triage authors and community.

If you want the original, full-featured, battle-tested toolkit, use **[dssg/triage](https://github.com/dssg/triage)** — it is actively maintained and supports a great deal that triage-pg deliberately drops.

## Why a separate project?

An effort to modernize triage *in place* (PR #994 on dssg/triage) accumulated too much friction against the existing test suite, dependency surface, and backward-compatibility constraints to be worth continuing. Rather than keep fighting that, triage-pg starts from triage's modernized core and takes a different, **opinionated and intentionally breaking** direction — one that does not belong upstream because it removes and reshapes things the original supports:

- **PostgreSQL as the whole substrate.** Evaluation, leaderboards, and bias metrics run *in the database* (PL/pgSQL over a predictions table), not in pandas. No Aequitas dependency — fairness metrics are SQL group-bys.
- **A modern feature engine.** Feature generation moves from Collate to [`featurizer`](https://github.com/nanounanue/featurizer), a PostgreSQL-native Deep Feature Synthesis engine that is point-in-time-correct via as-of joins.
- **More problem types.** Beyond binary classification: regression-as-ranking, pure regression, and a survival-ready label schema, selected by a `problem_type` switch.
- **Append-only, monitoring-ready predictions** — timestamped and partitioned, so prediction history is captured from day one.
- **Multi-tenant by database.** One PostgreSQL database per project plus a registry control plane, with two deployment profiles: a **local** profile (standalone PostgreSQL — laptops, teaching, tests) and a **cloud** profile (RDS/Aurora + IAM auth + S3 + AWS Batch).
- **Smaller surface.** No standalone postmodeling module (it dissolves into SQL views + a dashboard); `rq` and multicore orchestration removed; modern tooling throughout (uv, ruff, loguru, typer, psycopg3, Python 3.12).

The full rationale — decision by decision — lives in the Architecture Decision Records under [`docs/adr/`](docs/adr/), with the domain glossary in [`CONTEXT.md`](CONTEXT.md) and the results-database design in [`docs/schema-design.md`](docs/schema-design.md).

## Development

triage-pg uses the Astral toolchain:

```bash
uv sync --extra dev          # create / sync the environment
just --list                  # list available recipes
just test                    # run the test suite
just alembic upgrade head    # apply the results-schema migration (needs a PostgreSQL)
```

Database connection comes from `DATABASE_URL` or the standard `PG*` environment variables (e.g. loaded via direnv) — there are no hardcoded credentials.

## License

MIT — see [`LICENSE`](LICENSE). triage-pg is a derivative work of triage (© 2019 Data Science and Public Policy, University of Chicago); the original copyright notice and MIT terms are preserved.
