# 0002. One database per project in a shared cluster, with a registry control plane

- Status: Accepted
- Date: 2026-06-04
- Status update (2026-06-28): Partially implemented — `registry` control-plane schema built (`registry_schema/.../0001_initial_registry_schema.py`: projects/users/project_members/submissions) and per-project DBs in use; cross-project routing wiring still partial.
- Status update (2026-07-03): **Implemented in full.** Routing landed via ADR-0025 (commit `76eb46fc`); the project **lifecycle** gap closed the same day (v1-completion plan Phase 2): `triage project create/drop/list` (`src/triage/project_lifecycle.py` — registry row → CREATE DATABASE → triage schema at head; drop = `DROP DATABASE … WITH (FORCE)` + a `status='dropped'` tombstone, registry migration 0002). The webapp still creates registry rows only (least privilege) and reports `database_ready`. Conformance: `docs/adr-conformance.md`.

Each **project** is an isolated PostgreSQL **database** inside one shared cluster (database-level isolation; teardown is `DROP DATABASE`). A single triage-pg instance serves many projects and routes each incoming experiment to the right project database; the cross-cutting state that makes that possible — projects, users, per-project routing/connection info, permissions, webapp auth — lives in a dedicated **registry database**, since it cannot live inside any per-project DB. Multiple users collaborate within a project.

## Considered alternatives
- *Schema-per-project* — rejected: complicates the single privileged control-plane connection and forces per-schema migrations; database-level isolation is cleaner for confidential client data and teardown.
- *Row-level `project_id` + RLS (single DB)* — rejected: enables cheap cross-project analytics but rests isolation on RLS-policy correctness; one wrong policy leaks client data.
- *A separate cluster/server per project* — rejected: heavy ops; database-level isolation in one shared cluster meets the confidentiality bar (no client requires a dedicated instance).

## Consequences
- Cross-project SQL (e.g. a teacher's leaderboard spanning students) is not native; it would need `postgres_fdw` or app-side aggregation if ever required.
- All project DBs are co-located in one cluster.
