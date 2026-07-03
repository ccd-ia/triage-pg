# coding: utf-8
"""psycopg3 connection helpers (ADR-0019).

The application-side data layer is psycopg3-native: this module exposes the
``ConnectionPool`` factory every adapter uses. SQLAlchemy is no longer imported
here — it survives only behind alembic (``triage.component.results_schema``).
"""

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from triage.logging import get_logger

logger = get_logger(__name__)


def libpq_conninfo(dburl) -> str:
    """Strip SQLAlchemy's ``+psycopg`` driver tag so a URL is a libpq conninfo string.

    ``cli.resolve_db_url`` and the test fixtures build ``postgresql+psycopg://…`` URLs (the
    form SQLAlchemy/alembic want); psycopg3/libpq wants the bare ``postgresql://…`` form.
    Accepts a string only — pass the password-bearing string from
    ``cli._compose_db_url`` / ``render_as_string(hide_password=False)``, never a bare
    ``str(URL)`` (which masks the password).
    """
    if not isinstance(dburl, str):
        raise TypeError(
            "connection_pool expects a database URL string (password-bearing); got "
            f"{type(dburl).__name__}."
        )
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if dburl.startswith(prefix):
            return "postgresql://" + dburl[len(prefix) :]
    return dburl


def swap_dbname(base_url: str, database_name: str) -> str:
    """Return ``base_url`` with only its database (path) segment replaced by ``database_name``.

    Preserves scheme (incl. the ``+psycopg`` tag), credentials, host, port, and query — only the
    database changes. This is the ADR-0002 shared-cluster / cloud-RDS routing primitive, shared by
    the dashboard project switcher (ADR-0025) and the project lifecycle (``triage project``).
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(base_url)
    return urlunsplit(
        (parts.scheme, parts.netloc, f"/{database_name}", parts.query, parts.fragment)
    )


def connection_pool(
    dburl, *, min_size: int = 1, max_size: int = 10, **kwargs
) -> ConnectionPool:
    """Open a psycopg3 ``ConnectionPool`` for the project database (ADR-0019).

    The single application-side connection factory. Every greenfield adapter takes the
    returned pool and runs raw SQL through ``with pool.connection() as conn`` — which commits
    on clean block exit and rolls back on exception (covering both the old ``engine.connect()``
    read path and the ``engine.begin()`` write-transaction path). Rows come back as dicts
    (``row_factory=dict_row``) so call sites use mapping access.
    """
    pool = ConnectionPool(
        libpq_conninfo(dburl),
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row},
        open=True,
        **kwargs,
    )
    return pool
