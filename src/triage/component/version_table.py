"""Schema placement for alembic's version (stamp) table.

Both migration lineages pin their stamp table into the schema that lineage owns
— ``triage.results_schema_versions`` and ``registry.registry_schema_versions``
— instead of letting PostgreSQL resolve the bare name through the connecting
role's ``search_path``.

Why it matters: unqualified, the stamp lands in whatever schema happens to be
*first* on that role's path. In triage-pg's own databases that is ``public``, so
nothing looks wrong; in a host project's database it is the host's first schema.
Found 2026-08-11 standing v1.1.1 up against a project whose role carries
``search_path = raw, clean, ontology, …``: all 41 ``triage.*`` tables were
created correctly (the migrations hardcode their schema) and only the stamp
floated — into ``raw``, a schema triage-pg does not own.

Pinning it creates two ordering problems, both solved here because both must
happen *before* ``context.configure``:

1. **Chicken-and-egg.** Alembic writes the version table before the first
   migration runs — and the first migration is what creates the schema. So the
   schema has to exist first.
2. **Upgrade path.** A database stamped by a pre-fix triage-pg holds its stamp
   in the search_path-resolved location. Alembic would look in the owned schema,
   find nothing, and re-run migration 0001 against an already-populated database
   (``CREATE TABLE`` collisions). So a legacy stamp is relocated first.

The mirror-image constraint lives in the ``0001`` migrations: their ``downgrade``
must not take the stamp down with the schema — see the schema-swap comment there.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from triage.logging import get_logger

logger = get_logger(__name__)

__all__ = ("prepare_version_table",)


def prepare_version_table(
    connection: Connection, *, schema: str, version_table: str
) -> None:
    """Make ``schema.version_table`` the alembic stamp location, and commit.

    Creates ``schema`` when absent, then moves a pre-fix stamp table into it if
    one is found in the search_path-resolved location. Committed before
    ``context.configure`` so alembic's own version-table bootstrap sees the
    finished state — a fresh database ends with an empty schema if the
    migrations then fail, which is harmless.
    """
    connection.execute(text(f'create schema if not exists "{schema}"'))

    legacy_schema = _legacy_stamp_schema(
        connection, schema=schema, version_table=version_table
    )
    if legacy_schema is not None:
        logger.info(
            "Relocating alembic stamp table %s.%s -> %s.%s (it was created "
            "unqualified by triage-pg <= v1.1.1 and resolved through search_path)",
            legacy_schema,
            version_table,
            schema,
            version_table,
        )
        connection.execute(
            text(
                f'alter table "{legacy_schema}"."{version_table}" set schema "{schema}"'
            )
        )

    connection.commit()


def _legacy_stamp_schema(
    connection: Connection, *, schema: str, version_table: str
) -> str | None:
    """Schema holding a pre-fix stamp table, or ``None`` if there is none to move.

    ``None`` covers both the fresh database and the already-correct one. The two
    candidates are exactly where the old, unqualified code could have put it:
    whatever ``search_path`` resolves the bare name to, and ``public`` — which
    the pre-fix code would still have chosen on a default path even though a
    customized path may no longer include it.
    """
    if _regclass_schema(connection, f'"{schema}"."{version_table}"') is not None:
        return None

    for candidate in (f'"{version_table}"', f'public."{version_table}"'):
        found = _regclass_schema(connection, candidate)
        if found is not None:
            return found
    return None


def _regclass_schema(connection: Connection, qualified_name: str) -> str | None:
    """Namespace of ``qualified_name``, or ``None`` when the relation is absent.

    ``to_regclass`` is the non-throwing lookup: it returns NULL rather than
    erroring when the name does not resolve (including when a schema in the
    name does not exist).
    """
    return connection.execute(
        text(
            "select n.nspname from pg_class c "
            "join pg_namespace n on n.oid = c.relnamespace "
            "where c.oid = to_regclass(:name)"
        ),
        {"name": qualified_name},
    ).scalar()
