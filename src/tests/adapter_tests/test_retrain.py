"""Greenfield retrain (:func:`triage.adapters.retrain.retrain` / ``retrain_and_predict``).

Seeds a real experiment via ``run_experiment``, then retrains the resulting model group at a
new prediction date and (separately) retrains-and-predicts. Asserts the retrained model
rejoins its ORIGINAL group (feature_list unchanged, G5), the run provenance
(purpose='retrain' + prediction_date, ADR-0018), and that retrain_and_predict appends
production predictions for the fresh model.
"""

from datetime import date

from tests.adapter_tests.test_run_orchestration import (
    _experiment_config,
    _seed_source,
)
from triage.adapters.retrain import retrain, retrain_and_predict
from triage.adapters.run import run_experiment
from triage.profiles.storage import LocalStorage

# prediction date -> train cut = prediction_date - 6 months = 2014-04-01, whose label window
# [2014-04-01, 2014-10-01) contains the 2014-07-15 label rows — so the retrain has labeled data.
PREDICTION_DATE = date(2014, 10, 1)


def _the_group(engine) -> int:
    with engine.connection() as conn:
        return conn.execute(
            "select model_group_id from triage.model_groups limit 1"
        ).fetchone()["model_group_id"]


def test_retrain_rejoins_original_group(db_pool_greenfield, tmp_path):
    engine = db_pool_greenfield
    _seed_source(engine)
    storage = str(tmp_path / "store")
    run_experiment(
        engine,
        _experiment_config(),
        storage=LocalStorage(),
        storage_root=storage,
        random_seed=42,
    )
    group_id = _the_group(engine)

    with engine.connection() as conn:
        models_before = conn.execute(
            "select count(*) as n from triage.models"
        ).fetchone()["n"]

    result = retrain(engine, group_id, PREDICTION_DATE, storage_dir=storage)

    # the retrained model joined the SAME group (feature_list unchanged, G5)
    assert result.model_group_id == group_id
    assert result.train_as_of_date == date(2014, 4, 1)  # 2014-10-01 minus 6 months

    # a new model was trained (the train cut is a fresh date) and no new group minted
    with engine.connection() as conn:
        models_after = conn.execute(
            "select count(*) as n from triage.models"
        ).fetchone()["n"]
        n_groups = conn.execute(
            "select count(*) as n from triage.model_groups"
        ).fetchone()["n"]
    assert models_after == models_before + 1
    assert n_groups == 1

    # run provenance: purpose='retrain' + prediction_date (ADR-0018)
    with engine.connection() as conn:
        run = conn.execute(
            "select purpose, prediction_date, status from triage.runs"
            + " where run_id = %(r)s",
            {"r": result.run_id},
        ).fetchone()
    assert run["purpose"] == "retrain"
    assert run["prediction_date"] == PREDICTION_DATE
    assert run["status"] == "completed"


def test_retrain_and_predict_scores_the_fresh_model(db_pool_greenfield, tmp_path):
    engine = db_pool_greenfield
    _seed_source(engine)
    storage = str(tmp_path / "store")
    run_experiment(
        engine,
        _experiment_config(),
        storage=LocalStorage(),
        storage_root=storage,
        random_seed=42,
    )
    group_id = _the_group(engine)

    retrain_result, forward_result = retrain_and_predict(
        engine, group_id, PREDICTION_DATE, storage_dir=storage
    )

    # the forward score is of the freshly retrained model
    assert forward_result.model_id == retrain_result.retrain_model_id
    assert forward_result.num_predictions == 6

    with engine.connection() as conn:
        prod = conn.execute(
            "select count(*) as n from triage.predictions where split_kind = 'production'"
        ).fetchone()["n"]
        # the two operations are two runs, with the expected purposes
        purposes = {
            r["purpose"]
            for r in conn.execute(
                "select purpose from triage.runs where purpose in"
                + " ('retrain', 'forward_score')"
            ).fetchall()
        }
    assert prod == 6
    assert purposes == {"retrain", "forward_score"}
