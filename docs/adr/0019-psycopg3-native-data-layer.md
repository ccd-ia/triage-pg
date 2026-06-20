# 0019. psycopg3-native data layer in application code; SQLAlchemy kept only for alembic

- Status: Accepted — **Implemented** 2026-06-20 (WS7, branch `feat/psycopg3-native`,
  commits C1 `96ba3717` / C2 `519889d6` / C3 `358b0898`). Application code is psycopg3-native
  on a `ConnectionPool`; SQLAlchemy remains only behind alembic. Full suite green
  (267 passed, 11 skipped); `grep sqlalchemy src/triage` hits only the alembic zones.
- Date: 2026-06-19 (decision); 2026-06-20 (implemented)
- Deciders: Adolfo De Unánue

## Context

After the greenfield strip (ADR-0001) removed the inherited ORM tree, triage-pg's
application code uses SQLAlchemy only as a thin execution layer: every greenfield
module (`adapters/*`, `artifacts.py`, `derivation.py`, `sources.py`,
`catwalk/{prediction_ranking,in_pg_evaluation}.py`) takes a SQLAlchemy `Engine`
and runs raw SQL through `engine.begin()`/`connect()` + `text(...)`. No ORM
remains — no declarative models, no `Session`, no query builder. The driver is
already psycopg3 (`postgresql+psycopg`), and the one place needing a native
connection (handing featurizer a cursor in `adapters/matrix.py`) already unwraps
it via `db_engine.raw_connection()`.

So SQLAlchemy Core is near-dead weight: it wraps connection/transaction
management and parameter binding, nothing the project leans on. The
"PostgreSQL-native, one-database, raw-SQL" ethos (ADR-0008 and the schema-design
doc) argues for dropping it from application code in favour of psycopg3 directly,
which also unlocks COPY-based bulk movement, server-side cursors, native type
adapters (the code already imports `psycopg.types.range`), and a connection pool.

## Decision

Convert all *application* code from SQLAlchemy Core to psycopg3-native:

- The `db_engine: Engine` parameter becomes a `psycopg_pool.ConnectionPool`;
  `with engine.connect()/begin()` becomes `with pool.connection() as conn:` (+
  `with conn.transaction():` for writes); `text("... :name ...")` + dict params
  becomes `conn.execute("... %(name)s ...", {...})` with `row_factory=dict_row`
  for mapping access. `util/db.py` exposes a pool factory; `cli.get_engine`
  becomes `get_pool`; the test fixtures yield a pool.

**Keep SQLAlchemy as a migration-only dependency.** `upgrade_db` / alembic stay
on SQLAlchemy; they build their own engine from the connection URL, decoupled
from the application pool. We do **not** replace alembic.

## Considered alternatives

- *Keep SQLAlchemy Core in application code* — rejected: it earns almost nothing
  here (no ORM, no query builder), and the `raw_connection()` unwrap for
  featurizer is an avoidable wart. The conversion is the right end state once the
  ORM (which mandated an `Engine`) is gone.
- *Drop SQLAlchemy entirely, including alembic (replace with yoyo / raw-SQL
  migrations)* — rejected: the greenfield migrations are already raw `op.execute`
  PL/pgSQL, and alembic's version-tracking is mature and tested; rewriting a
  working migration layer to shed one dev/infra dependency is high-risk,
  low-value. SQLAlchemy survives behind alembic only.

## Consequences

- The dominant change is paramstyle (`:name` → `%(name)s`) across every greenfield
  module — pervasive and silent-failure-prone, so the conversion is done
  module-by-module with the suite green after each, never batched.
- One real gain: the featurizer seam simplifies (pass `pool.connection()` straight
  to `to_arrow`, no proxy unwrap), and COPY / pooling become available.
- This must happen *after* the ORM strip: the inherited ORM required a SQLAlchemy
  `Engine`, so the engine could not be replaced while it lived.
