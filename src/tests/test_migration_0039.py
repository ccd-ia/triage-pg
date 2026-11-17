"""Migration 0039 — group-level reads take the latest Run's model (issue #69).

The revision rewrites eight existing objects so they read ``triage.current_models`` instead
of ``triage.models``, and changes nothing else in them. The test reads each object's
definition from the catalog at 0038, at 0039 and after the downgrade: 0039 must be 0038
with that one relation swapped, and the downgrade must give back 0038 exactly.
"""

from __future__ import annotations

from triage.component import results_schema

REVISION = "0039_current_models"
PREVIOUS = "0038_curve_positive_outcome"

VIEWS = ("audition_distances_by_subset", "model_group_summary", "leaderboard")
FUNCTIONS = (
    "latest_model",
    "scored_coverage",
    "materialize_common_rows",
    "entity_score_history",
    "error_rule_recurrence",
)


def _definitions(pool) -> dict[str, str]:
    with pool.connection() as conn:
        views = conn.execute(
            "select c.relname as name, pg_get_viewdef(c.oid) as def"
            " from pg_class c join pg_namespace n on n.oid = c.relnamespace"
            " where n.nspname = 'triage' and c.relname = any(%(v)s)",
            {"v": list(VIEWS)},
        ).fetchall()
        functions = conn.execute(
            "select p.proname as name, p.prosrc as def"
            " from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
            " where n.nspname = 'triage' and p.proname = any(%(f)s)",
            {"f": list(FUNCTIONS)},
        ).fetchall()
    return {r["name"]: r["def"] for r in [*views, *functions]}


def test_the_revision_swaps_only_the_models_relation_and_round_trips(db_url, db_pool):
    results_schema.upgrade_db(dburl=db_url, revision=PREVIOUS)
    before = _definitions(db_pool)
    assert set(before) == {*VIEWS, *FUNCTIONS}

    results_schema.upgrade_db(dburl=db_url, revision=REVISION)
    after = _definitions(db_pool)
    for name, text in before.items():
        assert "triage.models " in text, name
        assert after[name] == text.replace(
            "triage.models ", "triage.current_models "
        ), name

    results_schema.downgrade_db(dburl=db_url, revision=PREVIOUS)
    assert _definitions(db_pool) == before
    with db_pool.connection() as conn:
        gone = conn.execute(
            "select to_regclass('triage.current_models') as r"
        ).fetchone()
    assert gone["r"] is None
