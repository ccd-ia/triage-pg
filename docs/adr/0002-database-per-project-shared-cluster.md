# 0002. One database per project in a shared cluster, with a registry control plane

- Status: Accepted
- Date: 2026-06-04

Each **project** is an isolated PostgreSQL **database** inside one shared cluster (database-level isolation; teardown is `DROP DATABASE`). A single triage-pg instance serves many projects and routes each incoming experiment to the right project database; the cross-cutting state that makes that possible — projects, users, per-project routing/connection info, permissions, webapp auth — lives in a dedicated **registry database**, since it cannot live inside any per-project DB. Multiple users collaborate within a project.

## Considered alternatives
- *Schema-per-project* — rejected: complicates the single privileged control-plane connection and forces per-schema migrations; database-level isolation is cleaner for confidential client data and teardown.
- *Row-level `project_id` + RLS (single DB)* — rejected: enables cheap cross-project analytics but rests isolation on RLS-policy correctness; one wrong policy leaks client data.
- *A separate cluster/server per project* — rejected: heavy ops; database-level isolation in one shared cluster meets the confidentiality bar (no client requires a dedicated instance).

## Consequences
- Cross-project SQL (e.g. a teacher's leaderboard spanning students) is not native; it would need `postgres_fdw` or app-side aggregation if ever required.
- All project DBs are co-located in one cluster.
