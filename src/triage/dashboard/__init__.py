"""Read-only dashboard JSON API + SSE (read-dashboard-spec §5; ADR-0012/0021).

A *thin* FastAPI app over the in-PG read surface (migration 0004): every endpoint is
a ``SELECT`` over a ``triage.*`` view/function through the psycopg3 pool, returning
JSON. No business/selection/metric logic lives here — it all lives in the views
(ADR-0012). Live progress is a ``LISTEN run_progress`` SSE stream (ADR-0021).

The app object is :data:`triage.dashboard.app.app`; build the pool from the project
DB URL via :func:`triage.cli.resolve_db_url` + :func:`triage.util.db.connection_pool`
(PG*/DATABASE_URL from the environment, never hardcoded — the project DB hard rule).
"""

from triage.dashboard.app import app, create_app

__all__ = ["app", "create_app"]
