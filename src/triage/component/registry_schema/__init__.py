"""Registry control-plane database schema (ADR-0002).

The *registry* is a distinct control-plane PostgreSQL database — separate from
the per-project ``triage`` results schema (see
``triage.component.results_schema``). It holds the multi-tenant control plane:
projects, users, membership, and an append-only submission audit trail (the
authoritative table/column shapes live in ``docs/schema-design.md`` §3).

This package mirrors ``results_schema`` but maintains its **own** alembic
history with a dedicated version table (``registry_schema_versions``), so the
two migration lineages never collide. Run it with::

    just alembic-registry upgrade head

It deliberately carries no SQLAlchemy ``Base`` / model metadata: like the
greenfield results baseline, the migration is raw-SQL DDL, so there is nothing
to autogenerate against.
"""

import os.path

from alembic import command
from alembic.config import Config

__all__ = ("upgrade_registry_db", "downgrade_registry_db", "registry_alembic_config")


def registry_alembic_config(dburl):
    """Alembic ``Config`` for the registry lineage, with ``url`` set programmatically.

    Mirrors ``results_schema.alembic_config``: the registry ``env.py`` reads
    ``config.attributes["url"]`` first (before DBURL/DATABASE_URL/PG*), so passing the URL here
    targets an explicit database without touching the environment — used by tests and by
    ``upgrade_registry_db``.
    """
    dir_path = os.path.dirname(os.path.abspath(__file__))
    config = Config(os.path.join(dir_path, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(dir_path, "alembic"))
    config.attributes["url"] = dburl
    return config


def upgrade_registry_db(dburl, revision="head"):
    """Apply the registry control-plane migrations to ``dburl`` (ADR-0002)."""
    command.upgrade(registry_alembic_config(dburl), revision)


def downgrade_registry_db(dburl, revision="-1"):
    """Roll back the registry control-plane migrations on ``dburl``."""
    command.downgrade(registry_alembic_config(dburl), revision)
