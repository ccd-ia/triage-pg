"""Smoke test: registry alembic env applies/reverts the initial registry schema.

Uses pytest-postgresql (the root conftest ``postgresql`` fixture) to get an
isolated throwaway database on a throwaway cluster — never touches any
shared/`food` DB. Drives the real ``registry_schema`` alembic env (via the
DATABASE_URL precedence path) through ``upgrade head`` -> assert ->
``downgrade base`` -> assert the control plane is gone.

"Gone" means the four tables the migration created, not the schema itself: the
stamp table is pinned into ``registry`` (see ``triage.component.version_table``)
and alembic keeps its bookkeeping across a downgrade, so an empty
``registry.registry_schema_versions`` survives — the same residue a stock
public-schema install leaves behind.
"""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
ALEMBIC_INI = SRC / "triage" / "component" / "registry_schema" / "alembic.ini"

EXPECTED_TABLES = {"projects", "users", "project_members", "submissions"}


def _run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["DATABASE_URL"] = database_url
    # DATABASE_URL has precedence in env.py, but drop stray PG*/DBURL so the
    # throwaway cluster is unambiguously the only target.
    for key in ("PGDATABASE", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "DBURL"):
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


def test_registry_migration_upgrade_and_downgrade(postgresql):
    info = postgresql.info
    base_url = f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"

    up = _run_alembic(base_url, "upgrade", "head")
    print("\n=== alembic-registry upgrade head ===")
    print((up.stdout + up.stderr).strip())
    assert up.returncode == 0, (
        f"upgrade failed:\nSTDOUT:{up.stdout}\nSTDERR:{up.stderr}"
    )

    engine = create_engine(
        base_url.replace("postgresql://", "postgresql+psycopg://", 1)
    )
    try:
        with engine.connect() as conn:
            schema_exists = conn.execute(
                text(
                    "select 1 from information_schema.schemata where schema_name = 'registry'"
                )
            ).scalar()
            assert schema_exists == 1

            tables = set(
                conn.execute(
                    text(
                        "select table_name from information_schema.tables "
                        "where table_schema = 'registry'"
                    )
                ).scalars()
            )
            print("=== \\dt registry.* (after upgrade head) ===")
            for name in sorted(tables):
                print(f"registry | {name} | table")
            assert EXPECTED_TABLES <= tables, (
                f"missing tables: {EXPECTED_TABLES - tables}"
            )

            # The stamp is pinned into the schema this lineage owns — never left to
            # the role's search_path, which would drop it in the host project's first
            # schema (the 2026-08-11 bug). Assert the placement, not just existence.
            version_table_schemas = set(
                conn.execute(
                    text(
                        "select table_schema from information_schema.tables "
                        "where table_name = 'registry_schema_versions'"
                    )
                ).scalars()
            )
            assert version_table_schemas == {"registry"}

        down = _run_alembic(base_url, "downgrade", "base")
        print("\n=== alembic-registry downgrade base ===")
        print((down.stdout + down.stderr).strip())
        assert down.returncode == 0, (
            f"downgrade failed:\nSTDOUT:{down.stdout}\nSTDERR:{down.stderr}"
        )

        with engine.connect() as conn:
            # ``downgrade base`` takes the control plane down but NOT alembic's own
            # bookkeeping: the stamp table lives in this schema, and alembic deletes
            # its row after the migration returns. So the schema survives holding
            # exactly one empty table — the same residue alembic leaves in ``public``
            # on a stock install. Everything the migration created is gone.
            remaining = set(
                conn.execute(
                    text(
                        "select table_name from information_schema.tables "
                        "where table_schema = 'registry'"
                    )
                ).scalars()
            )
            print("=== \\dt registry.* (after downgrade base) ===")
            for name in sorted(remaining):
                print(f"registry | {name} | table")
            assert remaining == {"registry_schema_versions"}
            assert not (EXPECTED_TABLES & remaining)

            stamp_rows = conn.execute(
                text("select count(*) from registry.registry_schema_versions")
            ).scalar()
            assert stamp_rows == 0
    finally:
        engine.dispose()
