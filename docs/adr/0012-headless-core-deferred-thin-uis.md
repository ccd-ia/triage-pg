# 0012. Headless-complete core; UIs are deferred thin frontends

- Status: Accepted
- Date: 2026-06-04
- Status update (2026-06-28): Implemented — headless core (CLI + SQL/views) complete; read dashboard shipped (`dashboard/` + `frontend/`); write webapp deferred (in progress).
- Status update (2026-07-03): The write webapp shipped (ADR-0024, commits `b657b17f`/`e9310683`) with per-project routing (ADR-0025) — "deferred (in progress)" above is superseded; both thin frontends now exist, business-logic-free as required.

triage-pg v1 is **headless**: experiments are submitted via CLI and results are read via SQL/`psql` over the in-PG views (ADR-0007). The core must be **fully usable without any UI**. Both UI surfaces are **post-v1 thin frontends** with no business logic of their own — a **read dashboard** (leaderboards / metrics / monitoring over the SQL views) comes first, then a **write webapp** (config submission + project/user management over the registry).

## Consequences
- No business logic may live in a UI; anything a UI does must first exist in the CLI/API/SQL views. This keeps the tool scriptable and headlessly testable.
- Deferring the UIs carries no architectural debt: the registry + SQL views (designed now) are the contract both frontends will consume.
