"""Migration 0021 — the model fit-geometry column, and the backfill that classifies it (#14).

The backfill has to tell two build paths apart on a database written before the column existed:
``triage run`` fits on the sorted feature-group projection that ``model_groups.feature_list``
records, and ``triage retrain`` fits on the train matrix's build order. ``triage.runs.purpose``
is the discriminator (ADR-0018), and getting it wrong is silent — hence this test.
"""

from __future__ import annotations

from triage.component import results_schema


def _has_feature_list(engine) -> bool:
    with engine.connection() as conn:
        return (
            conn.execute(
                "select count(*) as n from information_schema.columns"
                " where table_schema = 'triage' and table_name = 'models'"
                " and column_name = 'feature_list'"
            ).fetchone()["n"]
            == 1
        )


def test_0021_round_trips(db_url, db_pool_greenfield) -> None:
    """upgrade → downgrade → upgrade leaves the column present, so a rollback is real."""
    engine = db_pool_greenfield
    assert _has_feature_list(engine), "the fixture migrates to head"

    results_schema.downgrade_db(dburl=db_url, revision="0020_pinball_metric")
    assert not _has_feature_list(engine), "downgrade must drop the column"

    results_schema.upgrade_db(dburl=db_url, revision="0021_model_feature_geometry")
    assert _has_feature_list(engine), "upgrade must restore it"


def test_0021_backfill_tells_the_two_build_paths_apart(
    db_url, db_pool_greenfield
) -> None:
    """A retrain-purpose model backfills from the matrix build order, everything else from
    the model group's sorted list."""
    engine = db_pool_greenfield
    with engine.connection() as conn:
        conn.execute(
            "insert into triage.model_groups (model_group_hash, model_type,"
            " hyperparameters, feature_list) values ('g-bf', 'x.Y', '{}'::jsonb,"
            " ARRAY['a_sorted','b_sorted'])"
        )
        group_id = conn.execute(
            "select model_group_id from triage.model_groups where model_group_hash = 'g-bf'"
        ).fetchone()["model_group_id"]

        runs = {}
        for tag, purpose in (("exp", "experiment"), ("rt", "retrain")):
            conn.execute(
                "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
                " values (%(a)s, %(l)s, 'matrix', '{}'::jsonb)",
                {"a": f"mx-{tag}", "l": f"mxl-{tag}"},
            )
            conn.execute(
                "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind,"
                " storage_uri, feature_names)"
                " values (gen_random_uuid(), %(a)s, 'train', %(u)s,"
                " ARRAY['b_build','a_build'])",
                {"a": f"mx-{tag}", "u": f"file:///{tag}"},
            )
            conn.execute(
                "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
                " values (%(a)s, %(l)s, 'model', '{}'::jsonb)",
                {"a": f"m-{tag}", "l": f"ml-{tag}"},
            )
            runs[tag] = conn.execute(
                "insert into triage.runs (profile, status, purpose)"
                " values ('local', 'completed', %(p)s) returning run_id",
                {"p": purpose},
            ).fetchone()["run_id"]

        for tag in ("exp", "rt"):
            matrix_uuid = conn.execute(
                "select matrix_uuid from triage.matrices where artifact_id = %(a)s",
                {"a": f"mx-{tag}"},
            ).fetchone()["matrix_uuid"]
            conn.execute(
                "insert into triage.models (model_group_id, model_hash, run_id,"
                " train_matrix_uuid, artifact_uri, random_seed)"
                " values (%(g)s, %(h)s, %(r)s, %(u)s, 'file:///m', 0)",
                {
                    "g": group_id,
                    "h": f"m-{tag}",
                    "r": runs[tag],
                    "u": matrix_uuid,
                },
            )
        # A database written before 0021 has the rows and no recorded geometry.
        conn.execute("update triage.models set feature_list = null")

    results_schema.downgrade_db(dburl=db_url, revision="0020_pinball_metric")
    results_schema.upgrade_db(dburl=db_url, revision="0021_model_feature_geometry")

    with engine.connection() as conn:
        backfilled = {
            row["model_hash"]: list(row["feature_list"] or [])
            for row in conn.execute(
                "select model_hash, feature_list from triage.models"
                " where model_hash in ('m-exp', 'm-rt')"
            ).fetchall()
        }

    assert backfilled["m-exp"] == [
        "a_sorted",
        "b_sorted",
    ], "a run-path model was fitted on the sorted projection the model group records"
    assert backfilled["m-rt"] == ["b_build", "a_build"], (
        "a retrained model was fitted on the train matrix's build order — taking the group's"
        " list here is the mistake this backfill exists to avoid"
    )
