import pytest
from alembic import command, script

from triage.component import results_schema


# given a new db -> that's why we are using db_pool/db_url instead of the greenfield-schema fixtures
def test_upgrade_if_clean_upgrades_if_clean(db_url, db_pool):
    results_schema.upgrade_if_clean(db_url)
    with db_pool.connection() as conn:
        db_version = conn.execute(
            "select version_num from results_schema_versions"
        ).fetchone()["version_num"]
        alembic_cfg = results_schema.alembic_config(db_url)
        assert (
            db_version
            == script.ScriptDirectory.from_config(alembic_cfg).get_current_head()
        )


def test_upgrade_if_clean_does_not_upgrade_if_not_clean(db_url):
    command.upgrade(results_schema.alembic_config(dburl=db_url), "head")
    command.downgrade(results_schema.alembic_config(dburl=db_url), "-1")
    with pytest.raises(ValueError):
        results_schema.upgrade_if_clean(db_url)
