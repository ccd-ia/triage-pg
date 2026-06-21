# 0021. Live run progress via pg_notify → SSE, with REST poll for state

- Status: Accepted
- Date: 2026-06-20

The read dashboard's run-monitor (ADR-0012, `docs/read-dashboard-spec.md`) shows a run's
pipeline DAG progressing online. We get there with **two halves**: REST reads of a
`triage.run_progress` view (reconstructed from `artifacts.built_by_run` + `status`, which
surfaces *in-flight* nodes that the post-build `run_artifacts` usage edges would miss) for
initial-load and reconnect **state**, and **`pg_notify('run_progress', …)` → SSE** for live
**deltas** — the core emits a notification inside the existing `begin_artifact` /
`mark_built` / `mark_failed` and run status transitions (NOTIFY fires on COMMIT, and each
builder commits per `with pool.connection()`, so deltas land at node granularity). Chosen
over pure polling (laggy, constant DB load for a real-time view) and over a GraphQL
subscription layer (PostGraphile/Hasura — a second engine, against the one-database,
plain-PostgreSQL ethos of ADR-0003). The progress emission is core **telemetry**, not UI
business logic, so it stays ADR-0012-clean; the only new state is a `runs.plan` jsonb
recording planned node counts so the DAG can show `N/M`.

## Consequences
- The headless core now emits a documented `run_progress` NOTIFY channel — a new, small
  coupling that the CLI and any future UI can both consume; the channel payload shape
  becomes a contract.
- The SSE backend holds one long-lived `LISTEN` connection (psycopg3 `conn.notifies()`),
  separate from the request-serving pool.
- Everything still works headlessly with no listener attached (NOTIFY with no LISTENer is a
  no-op); polling the `run_progress` view remains a complete fallback.
- A Batch-submitted cloud run (ADR-0005) emits the same notifications from inside the job,
  so the monitor is profile-agnostic.
