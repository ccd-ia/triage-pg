"""Greenfield forward scoring (:func:`triage.adapters.forward.predict_forward`).

Seeds a real experiment via ``run_experiment`` (reusing the orchestration test's fixtures),
then forward-scores an existing model at a NEW ``as_of_date`` that has no realized labels.
Asserts the production predictions, the production matrix + its train-matrix leakage parent
(the G2 widening), and the run provenance (purpose='forward_score' + prediction_date, ADR-0018).
"""

from datetime import date

from sqlalchemy import text

from tests.adapter_tests.test_run_orchestration import (
    _experiment_config,
    _seed_source,
)
from triage.adapters.forward import predict_forward
from triage.adapters.run import run_experiment

# A date later than every training/label window — its label window has no realized outcomes,
# so this exercises forward scoring on an UNLABELED production matrix (G1).
FORWARD_DATE = date(2014, 12, 1)


def _latest_model(engine):
    with engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "select model_id, model_group_id from triage.models"
                    + " order by train_end_time desc nulls last, model_id desc limit 1"
                )
            )
            .mappings()
            .one()
        )


def test_predict_forward_appends_production_predictions(db_engine_greenfield, tmp_path):
    engine = db_engine_greenfield
    _seed_source(engine)
    storage = str(tmp_path / "store")
    run_experiment(engine, _experiment_config(), storage_dir=storage, random_seed=42)
    model = _latest_model(engine)

    result = predict_forward(
        engine, model["model_id"], FORWARD_DATE, storage_dir=storage
    )

    # 6 customers scored at the new date (cohort returns all 6, features split-stable)
    assert result.num_predictions == 6
    assert result.model_id == model["model_id"]

    # ---- production predictions appended (append-only, ADR-0006), split_kind='production'
    with engine.connect() as conn:
        prod = conn.execute(
            text(
                "select count(*) from triage.predictions where split_kind = 'production'"
            )
        ).scalar_one()
    assert prod == 6

    # ---- a production matrix exists and carries the model's train matrix as a parent
    # (the ADR-0009 leakage edge — fit-based 'mean' stats flow forward, matrix.py G2 widening)
    with engine.connect() as conn:
        kind = conn.execute(
            text("select matrix_kind from triage.matrices where artifact_id = :a"),
            {"a": result.production_matrix_artifact_id},
        ).scalar_one()
        parents = set(
            conn.execute(
                text(
                    "select parent_id from triage.artifact_inputs where artifact_id = :a"
                ),
                {"a": result.production_matrix_artifact_id},
            ).scalars()
        )
        train_matrix_artifact = conn.execute(
            text(
                "select m.artifact_id from triage.models md"
                + " join triage.matrices m on m.matrix_uuid = md.train_matrix_uuid"
                + " where md.model_id = :mid"
            ),
            {"mid": model["model_id"]},
        ).scalar_one()
    assert kind == "production"
    assert train_matrix_artifact in parents

    # ---- run provenance: purpose='forward_score' + prediction_date (ADR-0018), completed
    with engine.connect() as conn:
        run = (
            conn.execute(
                text(
                    "select purpose, prediction_date, status from triage.runs"
                    + " where run_id = :r"
                ),
                {"r": result.run_id},
            )
            .mappings()
            .one()
        )
    assert run["purpose"] == "forward_score"
    assert run["prediction_date"] == FORWARD_DATE
    assert run["status"] == "completed"


def test_predict_forward_is_append_only_across_calls(db_engine_greenfield, tmp_path):
    """Re-scoring the same model/date appends a fresh batch — never overwrites (ADR-0006)."""
    engine = db_engine_greenfield
    _seed_source(engine)
    storage = str(tmp_path / "store")
    run_experiment(engine, _experiment_config(), storage_dir=storage, random_seed=42)
    model = _latest_model(engine)

    predict_forward(engine, model["model_id"], FORWARD_DATE, storage_dir=storage)
    predict_forward(engine, model["model_id"], FORWARD_DATE, storage_dir=storage)

    with engine.connect() as conn:
        prod = conn.execute(
            text(
                "select count(*) from triage.predictions where split_kind = 'production'"
            )
        ).scalar_one()
    assert prod == 12  # two append-only batches of 6
