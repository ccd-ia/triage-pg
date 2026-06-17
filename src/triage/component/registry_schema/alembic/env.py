# Registry-database alembic environment (ADR-0002).
#
# Mirrors triage.component.results_schema.alembic.env but targets the separate
# *registry* control-plane DB and uses its own version table
# (``registry_schema_versions``) so the two alembic histories never collide.

import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import URL

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Raw-SQL DDL migration (mirrors the greenfield results baseline) — there is no
# SQLAlchemy model metadata to autogenerate against.
target_metadata = None


# --------------------------------------------------------------------------- #
# Credential resolution — identical precedence to results_schema/env.py:       #
#   1. an explicit url passed via config.attributes (programmatic callers)      #
#   2. DBURL                                                                     #
#   3. DATABASE_URL (rewritten to the psycopg driver)                           #
#   4. PG* env vars (PGHOST/PGUSER/PGDATABASE/PGPASSWORD/PGPORT)                 #
#   5. -x db_config_file=<yaml> override                                        #
# All loaded by direnv from .envrc for the ADR-0003 local profile.              #
# --------------------------------------------------------------------------- #
url = None

if "url" in config.attributes:
    url = config.attributes["url"]

if not url:
    url = os.environ.get("DBURL", None)

if not url:
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

if not url and os.environ.get("PGDATABASE") and os.environ.get("PGHOST"):
    url = URL.create(
        "postgresql+psycopg",
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT"),
        username=os.environ.get("PGUSER"),
        password=os.environ.get("PGPASSWORD"),
        database=os.environ["PGDATABASE"],
    )

if not url:
    import yaml

    db_config_file = context.get_x_argument("db_config_file").get(
        "db_config_file", None
    )
    if not db_config_file:
        raise ValueError(
            "No registry database connection information found — set DATABASE_URL "
            "or PG* env vars (loaded by direnv from .envrc), or pass "
            "-x db_config_file=<yaml>."
        )

    with open(db_config_file) as fd:
        db_config = yaml.full_load(fd)
        url = URL.create(
            "postgresql+psycopg",
            host=db_config["host"],
            username=db_config["user"],
            database=db_config["db"],
            password=db_config["pass"],
            port=db_config["port"],
        )


def run_migrations_offline():
    """Run migrations in 'offline' mode."""
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        version_table="registry_schema_versions",
        include_schemas=True,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode."""
    connectable = create_engine(url, poolclass=pool.NullPool, future=True)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table="registry_schema_versions",
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
