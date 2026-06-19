"""Greenfield (``triage.*``) seeding helpers for the audition tests.

The audition tests used to seed via the inherited ORM factories
(ModelFactory / ModelGroupFactory / EvaluationFactory) writing into
``triage_metadata.*`` / ``test_results.*``. Audition now reads the greenfield
``triage.*`` schema, so these helpers INSERT directly into
``triage.model_groups`` / ``triage.models`` / ``triage.evaluations`` against the
``db_engine_greenfield`` fixture (alembic-upgraded to head).

FK ordering (``triage.evaluations.model_id`` -> ``triage.models.model_id`` ->
``triage.model_groups.model_group_id``):
  insert model_groups -> models -> evaluations.

``model_group_id`` / ``model_id`` are ``generated always as identity`` so we let
PostgreSQL assign them and read them back with RETURNING. ``model_hash`` is a
unique text column that FK-references ``triage.artifacts(artifact_id)``, so each
model needs a minimal ``triage.artifacts`` row (kind='model') inserted first to
satisfy that constraint. ``train_end_time`` is a ``date`` in greenfield.
``triage.evaluations`` carries one row per
(model_id, split_kind, as_of_date, subset_hash, metric, parameter); audition
reads ``split_kind = 'test'`` rows by their ``value`` column.
"""

import uuid

from sqlalchemy import text

from triage.component.audition.distance_from_best import DistanceFromBestTable


def insert_model_group(conn, model_type, *, model_group_id=None, config=None):
    """INSERT a ``triage.model_groups`` row; return its ``model_group_id``.

    ``model_group_id`` is ``generated always as identity``; pass an explicit id
    only when a test needs a deterministic value (then we use OVERRIDING SYSTEM
    VALUE). ``model_group_hash`` / ``hyperparameters`` / ``feature_list`` are
    NOT NULL, so we supply trivial unique/empty values.
    """
    group_hash = uuid.uuid4().hex
    if model_group_id is not None:
        row = conn.execute(
            text(
                "insert into triage.model_groups"
                " (model_group_id, model_group_hash, model_type, hyperparameters,"
                "  feature_list, config)"
                " overriding system value"
                " values (:mgid, :h, :mt, '{}'::jsonb, '{}'::text[],"
                "  cast(:cfg as jsonb))"
                " returning model_group_id"
            ),
            {
                "mgid": model_group_id,
                "h": group_hash,
                "mt": model_type,
                "cfg": config,
            },
        )
    else:
        row = conn.execute(
            text(
                "insert into triage.model_groups"
                " (model_group_hash, model_type, hyperparameters, feature_list, config)"
                " values (:h, :mt, '{}'::jsonb, '{}'::text[], cast(:cfg as jsonb))"
                " returning model_group_id"
            ),
            {"h": group_hash, "mt": model_type, "cfg": config},
        )
    return row.scalar_one()


def insert_model(conn, model_group_id, train_end_time, *, run_id=None):
    """INSERT a minimal ``triage.artifacts`` (kind='model') row + a
    ``triage.models`` row; return the generated ``model_id``.

    ``triage.models.model_hash`` FK-references ``triage.artifacts(artifact_id)``
    and is NOT NULL, so the artifact row must exist first. ``run_id`` is
    nullable; pass one (FK -> ``triage.runs``) only for experiment-hash tests.
    """
    artifact_id = uuid.uuid4().hex
    conn.execute(
        text(
            "insert into triage.artifacts"
            " (artifact_id, logical_id, kind, config, status)"
            " values (:aid, :aid, 'model', '{}'::jsonb, 'built')"
        ),
        {"aid": artifact_id},
    )
    row = conn.execute(
        text(
            "insert into triage.models"
            " (model_group_id, model_hash, run_id, train_end_time)"
            " values (:mgid, :h, :run_id, cast(:tet as date))"
            " returning model_id"
        ),
        {
            "mgid": model_group_id,
            "h": artifact_id,
            "run_id": run_id,
            "tet": str(train_end_time),
        },
    )
    return row.scalar_one()


def insert_evaluation(
    conn,
    model_id,
    metric,
    parameter,
    value,
    as_of_date,
    *,
    split_kind="test",
    subset_hash="",
):
    """INSERT one ``triage.evaluations`` row (audition reads ``split_kind='test'``)."""
    conn.execute(
        text(
            "insert into triage.evaluations"
            " (model_id, split_kind, as_of_date, subset_hash, metric, parameter, value)"
            " values (:mid, cast(:sk as triage.split_kind), cast(:aod as date),"
            "  :sh, :metric, :parameter, :value)"
        ),
        {
            "mid": model_id,
            "sk": split_kind,
            "aod": str(as_of_date),
            "sh": subset_hash,
            "metric": metric,
            "parameter": parameter,
            "value": value,
        },
    )


def create_sample_distance_table(engine):
    """Build + populate a sample distance table on the greenfield schema.

    Seeds two model groups ('stable'/'spiky') with three models each across
    2014/2015/2016, then directly INSERTs the distance-from-best rows (the same
    fixture rows the ORM version used). Returns (distance_table, model_groups)
    where ``model_groups`` maps name -> model_group_id.
    """
    with engine.begin() as conn:
        model_groups = {
            "stable": insert_model_group(conn, "myStableClassifier"),
            "spiky": insert_model_group(conn, "mySpikeClassifier"),
        }

        stable_grp = model_groups["stable"]
        spiky_grp = model_groups["spiky"]

        # train_end_time per model (the distance rows key off these dates)
        ends = {
            "stable_3y_ago": "2014-01-01",
            "stable_2y_ago": "2015-01-01",
            "stable_1y_ago": "2016-01-01",
            "spiky_3y_ago": "2014-01-01",
            "spiky_2y_ago": "2015-01-01",
            "spiky_1y_ago": "2016-01-01",
        }
        for name, end in ends.items():
            grp = stable_grp if name.startswith("stable") else spiky_grp
            insert_model(conn, grp, end)

    distance_table = DistanceFromBestTable(
        db_engine=engine,
        models_table="models",
        distance_table="dist_table",
        agg_type="worst",
    )
    distance_table._create()

    distance_rows = [
        (
            stable_grp,
            ends["stable_3y_ago"],
            "precision@",
            "100_abs",
            0.5,
            0.6,
            0.1,
            0.5,
            0.15,
        ),
        (
            stable_grp,
            ends["stable_2y_ago"],
            "precision@",
            "100_abs",
            0.5,
            0.84,
            0.34,
            0.5,
            0.18,
        ),
        (
            stable_grp,
            ends["stable_1y_ago"],
            "precision@",
            "100_abs",
            0.46,
            0.67,
            0.21,
            0.5,
            0.11,
        ),
        (
            spiky_grp,
            ends["spiky_3y_ago"],
            "precision@",
            "100_abs",
            0.45,
            0.6,
            0.15,
            0.5,
            0.19,
        ),
        (
            spiky_grp,
            ends["spiky_2y_ago"],
            "precision@",
            "100_abs",
            0.84,
            0.84,
            0.0,
            0.5,
            0.3,
        ),
        (
            spiky_grp,
            ends["spiky_1y_ago"],
            "precision@",
            "100_abs",
            0.45,
            0.67,
            0.22,
            0.5,
            0.12,
        ),
        (
            stable_grp,
            ends["stable_3y_ago"],
            "recall@",
            "100_abs",
            0.4,
            0.4,
            0.0,
            0.4,
            0.0,
        ),
        (
            stable_grp,
            ends["stable_2y_ago"],
            "recall@",
            "100_abs",
            0.5,
            0.5,
            0.0,
            0.5,
            0.0,
        ),
        (
            stable_grp,
            ends["stable_1y_ago"],
            "recall@",
            "100_abs",
            0.6,
            0.6,
            0.0,
            0.6,
            0.0,
        ),
        (
            spiky_grp,
            ends["spiky_3y_ago"],
            "recall@",
            "100_abs",
            0.65,
            0.65,
            0.0,
            0.65,
            0.0,
        ),
        (
            spiky_grp,
            ends["spiky_2y_ago"],
            "recall@",
            "100_abs",
            0.55,
            0.55,
            0.0,
            0.55,
            0.0,
        ),
        (
            spiky_grp,
            ends["spiky_1y_ago"],
            "recall@",
            "100_abs",
            0.45,
            0.45,
            0.0,
            0.45,
            0.0,
        ),
    ]

    with engine.begin() as conn:
        for dist_row in distance_rows:
            conn.execute(
                text(
                    "insert into dist_table values (:model_group_id, "
                    ":train_end_time, :metric, :parameter, :raw_value, :best_case, "
                    ":dist_from_best, :raw_value_next, :dist_from_best_case_next_time)"
                ),
                {
                    "model_group_id": dist_row[0],
                    "train_end_time": dist_row[1],
                    "metric": dist_row[2],
                    "parameter": dist_row[3],
                    "raw_value": dist_row[4],
                    "best_case": dist_row[5],
                    "dist_from_best": dist_row[6],
                    "raw_value_next": dist_row[7],
                    "dist_from_best_case_next_time": dist_row[8],
                },
            )

    return distance_table, model_groups
