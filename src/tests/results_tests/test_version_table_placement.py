"""Alembic's stamp table belongs to the schema its lineage owns, not to search_path.

Regression cover for the 2026-08-11 bug: ``triage db upgrade`` created all 41
``triage.*`` tables correctly (the migrations hardcode their schema) but wrote
``results_schema_versions`` unqualified, so PostgreSQL resolved it into the
*first* schema of the connecting role's ``search_path`` — ``public`` in
triage-pg's own databases, which is why it stayed invisible here, and the host
project's ``raw`` schema in the database where it surfaced.

The four cases below are the acceptance criteria: fresh database, hostile
search_path, a database stamped before the fix, and offline (``--sql``) mode.
``registry_schema`` carries the same contract for its own schema — see
``registry_schema_tests/test_registry_migration_smoke.py`` for its placement and
``downgrade base`` cases, plus the search_path case at the bottom of this file.
"""

import psycopg
import pytest
from alembic import script

from triage.component import registry_schema, results_schema

# No spaces: PGOPTIONS is whitespace-split into separate libpq options.
HOSTILE_SEARCH_PATH = "raw,clean,ontology,public"


def _stamp_schemas(pool, version_table="results_schema_versions"):
    """Every schema holding a table of that name — the whole point is that it is one."""
    with pool.connection() as conn:
        return {
            row["table_schema"]
            for row in conn.execute(
                "select table_schema from information_schema.tables "
                "where table_name = %s",
                (version_table,),
            ).fetchall()
        }


def _stamped_revision(pool, qualified="triage.results_schema_versions"):
    with pool.connection() as conn:
        rows = conn.execute(f"select version_num from {qualified}").fetchall()
    assert len(rows) == 1, f"expected exactly one stamp row, got {rows}"
    return rows[0]["version_num"]


def _head(db_url):
    cfg = results_schema.alembic_config(db_url)
    return script.ScriptDirectory.from_config(cfg).get_current_head()


def test_fresh_database_stamps_into_the_triage_schema(db_url, db_pool):
    """Fresh DB: the stamp lands in ``triage`` and nowhere else; re-running no-ops."""
    results_schema.upgrade_db(dburl=db_url, revision="head")

    assert _stamp_schemas(db_pool) == {"triage"}
    assert _stamped_revision(db_pool) == _head(db_url)

    # A second upgrade must be a no-op. It cannot silently re-run: migration 0001
    # would hit "type triage.problem_type already exists" and raise.
    results_schema.upgrade_db(dburl=db_url, revision="head")
    assert _stamp_schemas(db_pool) == {"triage"}
    assert _stamped_revision(db_pool) == _head(db_url)


def test_custom_search_path_does_not_capture_the_stamp(db_url, db_pool, monkeypatch):
    """The reproduction: a role whose search_path starts elsewhere keeps its schemas clean.

    ``PGOPTIONS`` is libpq's per-connection equivalent of the ``alter role … in
    database … set search_path`` that produced the bug in the field; it reaches
    alembic's own engine the same way the role setting would.
    """
    with db_pool.connection() as conn:
        conn.execute("create schema raw")
        conn.execute("create schema clean")
        conn.execute("create schema ontology")

    monkeypatch.setenv("PGOPTIONS", f"-c search_path={HOSTILE_SEARCH_PATH}")
    results_schema.upgrade_db(dburl=db_url, revision="head")

    assert _stamp_schemas(db_pool) == {"triage"}
    with db_pool.connection() as conn:
        host_tables = conn.execute(
            "select table_schema, table_name from information_schema.tables "
            "where table_schema in ('raw', 'clean', 'ontology')"
        ).fetchall()
    assert host_tables == [], f"triage-pg wrote into a host schema: {host_tables}"


def test_pre_fix_database_relocates_its_stamp_instead_of_re_running(db_url, db_pool):
    """A database stamped before the fix upgrades without re-running migration 0001.

    Simulates the pre-fix state exactly: the schema is fully migrated, but the stamp
    sits where ``search_path`` put it (``public`` on a default path). Without the
    relocation pre-flight alembic finds no stamp in ``triage``, replays 0001 against
    the populated schema, and dies on ``CREATE TYPE``.
    """
    results_schema.upgrade_db(dburl=db_url, revision="head")
    with db_pool.connection() as conn:
        conn.execute("alter table triage.results_schema_versions set schema public")
        # A sentinel proves the schema itself was carried across, not recreated.
        conn.execute("create table triage.zz_sentinel (id int)")
    assert _stamp_schemas(db_pool) == {"public"}

    results_schema.upgrade_db(dburl=db_url, revision="head")

    assert _stamp_schemas(db_pool) == {"triage"}
    assert _stamped_revision(db_pool) == _head(db_url)
    with db_pool.connection() as conn:
        sentinel = conn.execute("select to_regclass('triage.zz_sentinel')").fetchone()
    assert sentinel["to_regclass"] == "triage.zz_sentinel"


def test_upgrade_if_clean_accepts_a_pre_fix_database(db_url, db_pool):
    """``upgrade_if_clean`` reads the qualified stamp, and heals a pre-fix one.

    It is the guard the experiment path runs before touching a database, so the
    pre-fix layout must not read as "fresh install, migrate from scratch" — nor as
    a version mismatch.
    """
    results_schema.upgrade_db(dburl=db_url, revision="head")
    with db_pool.connection() as conn:
        conn.execute("alter table triage.results_schema_versions set schema public")

    results_schema.upgrade_if_clean(db_url)

    assert _stamp_schemas(db_pool) == {"triage"}
    assert _stamped_revision(db_pool) == _head(db_url)


def test_downgrade_base_keeps_the_stamp_and_drops_everything_else(db_url, db_pool):
    """``downgrade base`` must not take alembic's bookkeeping down with the schema.

    Migration 0001 owns the schema the stamp now lives in, and alembic deletes the
    stamp row *after* the migration returns — a plain ``drop schema triage cascade``
    would fail with "expected to match one row when deleting". What survives is the
    residue a stock public-schema install leaves: an empty version table.
    """
    results_schema.upgrade_db(dburl=db_url, revision="head")
    results_schema.downgrade_db(dburl=db_url, revision="base")

    with db_pool.connection() as conn:
        remaining = {
            row["table_name"]
            for row in conn.execute(
                "select table_name from information_schema.tables "
                "where table_schema = 'triage'"
            ).fetchall()
        }
        stamp_rows = conn.execute(
            "select count(*) as n from triage.results_schema_versions"
        ).fetchone()["n"]
        leftovers = conn.execute(
            "select count(*) as n from pg_type t "
            "join pg_namespace n on n.oid = t.typnamespace "
            "where n.nspname = 'triage' and t.typtype = 'e'"
        ).fetchone()["n"]

    assert remaining == {"results_schema_versions"}
    assert stamp_rows == 0
    assert leftovers == 0, "the schema's enums outlived the teardown"

    # And the swapped-out schema is not left lying around under its transient name.
    with db_pool.connection() as conn:
        transient = conn.execute(
            "select 1 from pg_namespace where nspname = 'triage_downgrading'"
        ).fetchone()
    assert transient is None

    # The whole lineage replays onto that residue.
    results_schema.upgrade_db(dburl=db_url, revision="head")
    assert _stamp_schemas(db_pool) == {"triage"}
    assert _stamped_revision(db_pool) == _head(db_url)


def test_offline_mode_emits_a_schema_qualified_version_table(db_url, capsys):
    """``--sql`` writes a script that creates the schema before it writes the stamp."""
    from alembic import command

    command.upgrade(results_schema.alembic_config(db_url), "head", sql=True)
    emitted = capsys.readouterr().out

    assert "CREATE TABLE triage.results_schema_versions" in emitted
    assert "CREATE TABLE results_schema_versions" not in emitted
    assert "INSERT INTO triage.results_schema_versions" in emitted
    # Ordering matters: alembic writes the version table before migration 0001 (which
    # is what would otherwise create the schema), so the script must open with it.
    assert emitted.index("create schema if not exists triage") < emitted.index(
        "CREATE TABLE triage.results_schema_versions"
    )


def test_cli_db_upgrade_on_a_host_database(db_url, db_pool, tmp_path, monkeypatch):
    """The field path, end to end: ``triage db upgrade`` into a host project's database.

    The reported bug came through the CLI, not the library — same reproduction here,
    a role whose ``search_path`` starts with the host's own schemas. Everything
    triage-pg creates must be inside ``triage``, and the host's schemas untouched.
    """
    from typer.testing import CliRunner

    import triage.cli as cli

    with db_pool.connection() as conn:
        for schema in ("raw", "clean", "ontology"):
            conn.execute(f"create schema {schema}")
        conn.execute("create table raw.host_table (id int)")

    monkeypatch.chdir(tmp_path)  # never pick up the repo-root database.yaml / .env
    result = CliRunner().invoke(
        cli.app,
        ["db", "upgrade"],
        env={
            "DATABASE_URL": db_url,
            "PGOPTIONS": f"-c search_path={HOSTILE_SEARCH_PATH}",
        },
    )

    assert result.exit_code == 0, result.output
    assert _stamp_schemas(db_pool) == {"triage"}
    with db_pool.connection() as conn:
        host_tables = {
            (row["table_schema"], row["table_name"])
            for row in conn.execute(
                "select table_schema, table_name from information_schema.tables "
                "where table_schema in ('raw', 'clean', 'ontology')"
            ).fetchall()
        }
    assert host_tables == {("raw", "host_table")}


@pytest.mark.parametrize("search_path", [None, HOSTILE_SEARCH_PATH])
def test_registry_stamp_lands_in_the_registry_schema(
    postgresql, monkeypatch, search_path
):
    """The registry lineage pins its own stamp the same way, path or no path."""
    info = postgresql.info
    conninfo = f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"

    if search_path is not None:
        with psycopg.connect(conninfo) as conn:
            conn.execute("create schema raw")
            conn.execute("create schema clean")
            conn.execute("create schema ontology")
        monkeypatch.setenv("PGOPTIONS", f"-c search_path={search_path}")

    registry_schema.upgrade_registry_db(
        conninfo.replace("postgresql://", "postgresql+psycopg://", 1)
    )

    with psycopg.connect(conninfo) as conn:
        schemas = [
            row[0]
            for row in conn.execute(
                "select table_schema from information_schema.tables "
                "where table_name = 'registry_schema_versions'"
            ).fetchall()
        ]
    assert schemas == ["registry"]
