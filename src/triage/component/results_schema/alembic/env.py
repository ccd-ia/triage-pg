# from __future__ import with_statement

import os
import re

import yaml
from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import URL

from triage.component.version_table import prepare_version_table

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# The stamp table is pinned into the schema this lineage owns, never left to the
# connecting role's search_path (see triage.component.version_table for the whole
# story). ``version_table_schema`` alone is not enough: alembic writes the table
# before migration 0001 creates the schema, so online mode also runs a pre-flight.
VERSION_TABLE = "results_schema_versions"
VERSION_TABLE_SCHEMA = "triage"

# The greenfield migrations are raw ``op.execute`` statements; autogenerate is
# never used, so there is no ORM ``Base.metadata`` to target.
target_metadata = None

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def get_excludes_from_config(config_, type_="tables"):
    excludes = config_.get(type_, None)
    if excludes is not None:
        excludes = excludes.split(",")
    excludes = excludes or []
    return excludes


excluded_tables = get_excludes_from_config(config.get_section("exclude"), "tables")
excluded_indices = get_excludes_from_config(config.get_section("exclude"), "indices")


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        for table_pat in excluded_tables:
            if re.match(table_pat, name):
                return False
        return True

    elif type_ == "index":
        for index_pat in excluded_indices:
            if re.match(index_pat, name):
                return False
        return True

    else:
        return True


url = None

if "url" in config.attributes:
    url = config.attributes["url"]

if not url:
    url = os.environ.get("DBURL", None)

if not url:
    # project convention: DATABASE_URL or PG* env vars (loaded by direnv) — ADR-0003 local profile
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

if not url and os.environ.get("PGDATABASE") and os.environ.get("PGHOST"):
    pgport = os.environ.get("PGPORT")
    url = URL.create(
        "postgresql+psycopg",
        host=os.environ["PGHOST"],
        port=int(pgport) if pgport else None,
        username=os.environ.get("PGUSER"),
        password=os.environ.get("PGPASSWORD"),
        database=os.environ["PGDATABASE"],
    )

if not url:
    db_config_file = context.get_x_argument(as_dictionary=True).get(
        "db_config_file", None
    )
    if not db_config_file:
        raise ValueError("No database connection information found")

    with open(db_config_file) as fd:
        config = yaml.full_load(fd)
        url = URL.create(
            "postgresql+psycopg",
            host=config["host"],
            username=config["user"],
            database=config["db"],
            password=config["pass"],
            port=config["port"],
        )


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        version_table=VERSION_TABLE,
        version_table_schema=VERSION_TABLE_SCHEMA,
        include_object=include_object,
        include_schemas=True,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        # Offline mode cannot inspect the target, so the emitted script carries the
        # schema creation itself — alembic writes ``CREATE TABLE triage.<version>``
        # as its first statement, before migration 0001 would create the schema.
        context.execute(f"create schema if not exists {VERSION_TABLE_SCHEMA};")
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    # By here the module-level resolution above has set url or raised.
    assert url is not None, "no database URL resolved for migrations"
    connectable = create_engine(url, poolclass=pool.NullPool, future=True)

    with connectable.connect() as connection:
        prepare_version_table(
            connection,
            schema=VERSION_TABLE_SCHEMA,
            version_table=VERSION_TABLE,
        )
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table=VERSION_TABLE,
            version_table_schema=VERSION_TABLE_SCHEMA,
            include_schemas=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
