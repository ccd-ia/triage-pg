"""Greenfield model-builder lifecycle: train → predict → evaluate on the DAG (ADR-0011, ADR-0016).

Seeds tiny source tables with a *clean, separable signal*, builds a cohort + labels (F1) and a
TRAIN + TEST matrix (F2), then trains a model with :func:`triage.adapters.build_model` and
scores + evaluates it with :func:`triage.adapters.score_and_evaluate`. Asserts:

* the artifact DAG — a ``'built'`` ``model`` node whose single parent is the train matrix;
* ``triage.model_groups`` — a row keyed by ``model_group_hash``, and a SECOND model of the
  same family reuses that ``model_group_id``;
* ``triage.models`` — ``model_hash == model artifact_id``, ``train_matrix_uuid`` set, the
  ``artifact_uri`` joblib file present on disk;
* ``triage.feature_importances`` — populated (DecisionTree exposes ``feature_importances_``);
* ``triage.predictions`` — APPEND-ONLY: re-scoring appends new rows (with later ``scored_at``)
  referencing ``model_id`` + the test ``matrix_uuid``;
* ``triage.evaluations`` — rows written by ``evaluate_in_db`` with sane metric values
  (perfect-separation fixture -> precision@/recall@/AUC == 1.0);
* a cache hit on a second identical ``build_model`` (same artifact, no duplicate model).
"""

from datetime import date

import pytest
from sqlalchemy import text

from triage.adapters.cohort import build_cohort
from triage.adapters.imputation import ImputationPolicy
from triage.adapters.labels import build_labels
from triage.adapters.matrix import build_matrix
from triage.adapters.model import build_model, score_and_evaluate
from triage.adapters.temporal import TemporalConfig
from triage.derivation import as_uuid

TRAIN_AS_OF = date(2014, 1, 1)
TEST_AS_OF = date(2014, 7, 1)
LABEL_TIMESPAN = "6 months"
CLASS_PATH = "sklearn.tree.DecisionTreeClassifier"

COHORT_QUERY = "select customer_id as entity_id from customers where {as_of_date} is not null"
# Outcome read straight from a labels source in the [as_of, as_of+span) window.
LABEL_QUERY = (
    "select entity_id, outcome from label_src"
    " where knowledge_date >= date {as_of_date}"
    " and knowledge_date < date {as_of_date} + {label_timespan}"
)


def _temporal_config() -> TemporalConfig:
    return TemporalConfig.model_validate(
        {
            "feature_start_time": "2010-01-01",
            "feature_end_time": "2015-01-01",
            "label_start_time": "2013-01-01",
            "label_end_time": "2015-01-01",
            "model_update_frequency": "6 months",
            "training_as_of_date_frequencies": "1 month",
            "test_as_of_date_frequencies": "1 month",
            "max_training_histories": "6 months",
            "test_durations": "0 days",
            "label_timespans": "6 months",
        }
    )


def _featurizer_config() -> dict:
    """customers (target) ⋈ orders. The COUNT/SUM/MEAN of orders.amount is the signal."""
    return {
        "target": "customers",
        "max_depth": 2,
        "intervals": ["P3650D"],
        "aggregations": ["count", "sum", "mean"],
        "transformations": ["identity"],
        "entities": [
            {
                "alias": "customers",
                "id": "customer_id",
                "table": "customers",
                "temporal_ix": "signup_date",
                "variables": {"age": {"type": "numeric"}},
            },
            {
                "alias": "orders",
                "id": "order_id",
                "table": "orders",
                "temporal_ix": "order_date",
                "variables": {"amount": {"type": "numeric"}},
            },
        ],
        "relationships": [
            {
                "parent": {"entity": "customers", "key": "customer_id"},
                "child": {"entity": "orders", "key": "customer_id"},
                "temporal": {"mode": "as_of", "child_time": "order_date"},
            }
        ],
    }


def _seed_lineage(engine) -> str:
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into triage.experiments (experiment_hash, config, problem_type)"
                " values ('exp-model', '{}'::jsonb, 'classification')"
            )
        )
        run_id = conn.execute(
            text("insert into triage.runs (experiment_hash, profile) values ('exp-model', 'local') returning run_id")
        ).scalar_one()
    return str(run_id)


# Six customers; the COUNT of pre-as_of orders separates the classes perfectly:
# customers with >= 2 prior orders are positive (outcome 1), those with 0-1 are negative.
# This holds on BOTH the train (pre-2014-01-01) and test (pre-2014-07-01) windows, so a
# DecisionTree learns a clean threshold and scores the test split perfectly.
_CUSTOMERS = [1, 2, 3, 4, 5, 6]
# (order_id, customer_id, order_date, amount). All dated before the TRAIN as_of so both
# windows see the same per-customer counts (a clean, split-stable signal).
_ORDERS = [
    (101, 1, "2013-01-01", 50.0),
    (102, 1, "2013-02-01", 50.0),
    (103, 1, "2013-03-01", 50.0),  # customer 1: 3 orders -> positive
    (104, 2, "2013-01-01", 40.0),
    (105, 2, "2013-02-01", 40.0),  # customer 2: 2 orders -> positive
    (106, 3, "2013-01-01", 30.0),
    (107, 3, "2013-02-01", 30.0),  # customer 3: 2 orders -> positive
    (108, 4, "2013-01-01", 20.0),  # customer 4: 1 order  -> negative
    (109, 5, "2013-01-01", 10.0),  # customer 5: 1 order  -> negative
    # customer 6: 0 orders -> negative
]
# outcome by customer: positives 1,2,3 ; negatives 4,5,6. One label row per (customer, window).
_LABELS = {1: 1.0, 2: 1.0, 3: 1.0, 4: 0.0, 5: 0.0, 6: 0.0}


def _seed_source(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("create table customers (customer_id bigint primary key, signup_date date, age int)"))
        conn.execute(
            text(
                "insert into customers (customer_id, signup_date, age)"
                " select g, date '2010-01-01', 30 + g from unnest(:ids) as g"
            ),
            {"ids": _CUSTOMERS},
        )
        conn.execute(
            text(
                "create table orders"
                " (order_id bigint primary key, customer_id bigint,"
                "  order_date date, amount double precision)"
            )
        )
        for order_id, customer_id, order_date, amount in _ORDERS:
            conn.execute(
                text(
                    "insert into orders (order_id, customer_id, order_date, amount)"
                    " values (:oid, :cid, cast(:od as date), :amt)"
                ),
                {"oid": order_id, "cid": customer_id, "od": order_date, "amt": amount},
            )
        conn.execute(text("create table label_src (entity_id bigint, knowledge_date date, outcome double precision)"))
        # One label per customer in each of the two label windows.
        for as_of in (TRAIN_AS_OF, TEST_AS_OF):
            for customer_id, outcome in _LABELS.items():
                conn.execute(
                    text(
                        "insert into label_src (entity_id, knowledge_date, outcome)"
                        " values (:eid, cast(:kd as date), :out)"
                    ),
                    {
                        "eid": customer_id,
                        # knowledge_date inside [as_of, as_of + 6 months)
                        "kd": str(date(as_of.year, as_of.month + 1, 1)),
                        "out": outcome,
                    },
                )


def _seed_protected_groups(engine) -> None:
    """A binary protected attribute so the optional bias group-by has something to read."""
    groups = {1: "A", 2: "A", 3: "B", 4: "B", 5: "A", 6: "B"}
    with engine.begin() as conn:
        for as_of in (TRAIN_AS_OF, TEST_AS_OF):
            for entity_id, value in groups.items():
                conn.execute(
                    text(
                        "insert into triage.protected_groups"
                        " (entity_id, as_of_date, attribute_name, attribute_value)"
                        " values (:eid, :aod, 'grp', :val)"
                    ),
                    {"eid": entity_id, "aod": as_of, "val": value},
                )


def _build_cohort_and_labels(engine, run_id, as_of_dates) -> tuple[str, str]:
    cohort_hash = build_cohort(
        engine,
        run_id,
        cohort_query_template=COHORT_QUERY,
        as_of_dates=as_of_dates,
        config={"query": COHORT_QUERY, "as_of_dates": [str(d) for d in as_of_dates]},
        source_pins={"customers": "v1"},
    )
    label_hash = build_labels(
        engine,
        run_id,
        cohort_artifact_id=cohort_hash,
        label_query_template=LABEL_QUERY,
        as_of_dates=as_of_dates,
        label_timespans=[LABEL_TIMESPAN],
        problem_type="classification",
        config={"query": LABEL_QUERY, "as_of_dates": [str(d) for d in as_of_dates]},
        source_pins={"label_src": "v1"},
    )
    return cohort_hash, label_hash


_PINS = {"customers": "v1", "orders": "v1", "label_src": "v1"}


def _build_matrices(engine, run_id, cohort, labels, storage):
    temporal = _temporal_config()
    policy = ImputationPolicy.model_validate({"all": {"type": "zero"}, "mean": {"type": "mean"}})
    train = build_matrix(
        engine,
        run_id,
        featurizer_config=_featurizer_config(),
        cohort_artifact_id=cohort,
        labels_artifact_id=labels,
        temporal_config=temporal,
        imputation_policy=policy,
        matrix_kind="train",
        as_of_dates=[TRAIN_AS_OF],
        label_timespan=LABEL_TIMESPAN,
        storage_dir=storage,
        lookback="6 months",
        source_pins=_PINS,
    )
    test = build_matrix(
        engine,
        run_id,
        featurizer_config=_featurizer_config(),
        cohort_artifact_id=cohort,
        labels_artifact_id=labels,
        temporal_config=temporal,
        imputation_policy=policy,
        matrix_kind="test",
        as_of_dates=[TEST_AS_OF],
        label_timespan=LABEL_TIMESPAN,
        storage_dir=storage,
        train_matrix_artifact_id=train.matrix_artifact_id,
        source_pins=_PINS,
    )
    return train, test


def test_model_build_predict_evaluate_full_lifecycle(db_engine_greenfield, tmp_path):
    import os

    engine = db_engine_greenfield
    run_id = _seed_lineage(engine)
    _seed_source(engine)
    _seed_protected_groups(engine)
    storage = str(tmp_path / "store")
    cohort, labels = _build_cohort_and_labels(engine, run_id, [TRAIN_AS_OF, TEST_AS_OF])
    train, test = _build_matrices(engine, run_id, cohort, labels, storage)

    # ---- TRAIN the model
    model = build_model(
        engine,
        run_id,
        train_matrix_result=train,
        class_path=CLASS_PATH,
        hyperparameters={"max_depth": 3},
        random_seed=42,
        storage_dir=storage,
        train_end_time=TRAIN_AS_OF,
        training_label_timespan=LABEL_TIMESPAN,
        source_pins=_PINS,
    )
    assert model.cache_hit is False

    # artifact DAG: a built model node whose single parent is the train matrix
    with engine.connect() as conn:
        art = (
            conn.execute(
                text("select kind, status from triage.artifacts where artifact_id = :a"),
                {"a": model.model_artifact_id},
            )
            .mappings()
            .one()
        )
        parents = (
            conn.execute(
                text("select parent_id from triage.artifact_inputs where artifact_id = :a"),
                {"a": model.model_artifact_id},
            )
            .scalars()
            .all()
        )
    assert art["kind"] == "model"
    assert art["status"] == "built"
    assert parents == [train.matrix_artifact_id]

    # triage.models row: model_hash == artifact_id, train_matrix_uuid set, file on disk
    with engine.connect() as conn:
        mrow = (
            conn.execute(
                text(
                    "select model_id, model_group_id, model_hash, train_matrix_uuid,"
                    " artifact_uri, artifact_format, model_size_bytes, random_seed,"
                    " training_label_timespan from triage.models where model_id = :m"
                ),
                {"m": model.model_id},
            )
            .mappings()
            .one()
        )
    assert mrow["model_hash"] == model.model_artifact_id
    assert str(mrow["train_matrix_uuid"]) == str(as_uuid(train.matrix_artifact_id))
    assert mrow["artifact_format"] == "joblib"
    assert mrow["model_size_bytes"] > 0
    assert mrow["random_seed"] == 42
    assert os.path.exists(model.artifact_uri)

    # model_groups row keyed by hash
    with engine.connect() as conn:
        grow = (
            conn.execute(
                text(
                    "select model_group_hash, model_type, feature_list"
                    " from triage.model_groups where model_group_id = :g"
                ),
                {"g": model.model_group_id},
            )
            .mappings()
            .one()
        )
    assert grow["model_group_hash"] == model.model_group_hash
    assert grow["model_type"] == CLASS_PATH
    assert set(grow["feature_list"]) == set(train.feature_names)

    # feature_importances populated (DecisionTree exposes feature_importances_)
    with engine.connect() as conn:
        n_imp = conn.execute(
            text("select count(*) from triage.feature_importances where model_id = :m"),
            {"m": model.model_id},
        ).scalar_one()
        top = (
            conn.execute(
                text(
                    "select feature, rank_abs, rank_pct from triage.feature_importances"
                    " where model_id = :m order by rank_abs limit 1"
                ),
                {"m": model.model_id},
            )
            .mappings()
            .one()
        )
    assert n_imp == len(train.feature_names)
    assert top["rank_abs"] == 1

    # ---- SCORE + EVALUATE on the test matrix
    # Threshold 50_pct selects the top 3 of 6 entities — exactly the 3 positives under the
    # perfect-separation fixture, so precision@ and recall@ at that cut are both 1.0.
    metric_config = {
        "metrics": ["precision@", "recall@", "auc_roc"],
        "thresholds": ["50_pct"],
    }
    result = score_and_evaluate(
        engine,
        model.model_id,
        model.estimator,
        test_matrix_result=test,
        as_of_date=TEST_AS_OF,
        label_timespan=LABEL_TIMESPAN,
        metric_config=metric_config,
        compute_bias=True,
        bias_parameter="50_pct",
    )
    assert result.num_predictions == 6  # all six test-cohort entities scored
    assert result.num_evaluations > 0
    assert result.num_bias_metrics > 0

    # predictions reference model_id + the test matrix_uuid
    with engine.connect() as conn:
        pred_rows = (
            conn.execute(
                text("select entity_id, split_kind, matrix_uuid from triage.predictions where model_id = :m"),
                {"m": model.model_id},
            )
            .mappings()
            .all()
        )
    assert len(pred_rows) == 6
    assert all(r["split_kind"] == "test" for r in pred_rows)
    assert all(str(r["matrix_uuid"]) == str(as_uuid(test.matrix_artifact_id)) for r in pred_rows)

    # evaluations: perfect separation -> precision@/recall@/AUC == 1.0
    with engine.connect() as conn:
        evals = {
            (r["metric"], r["parameter"]): r["value"]
            for r in conn.execute(
                text(
                    "select metric, parameter, value, num_labeled, num_positive"
                    " from triage.evaluations where model_id = :m"
                ),
                {"m": model.model_id},
            ).mappings()
        }
    assert evals[("auc_roc", "")] == pytest.approx(1.0)
    # precision@50_pct: top 3 of 6 are exactly the 3 positives -> 1.0
    assert evals[("precision@", "50_pct")] == pytest.approx(1.0)
    # recall@50_pct: those 3 are all the positives -> 1.0
    assert evals[("recall@", "50_pct")] == pytest.approx(1.0)


def test_predictions_are_append_only(db_engine_greenfield, tmp_path):
    """Re-scoring the same model+matrix APPENDS rows (ADR-0006), never overwrites."""
    engine = db_engine_greenfield
    run_id = _seed_lineage(engine)
    _seed_source(engine)
    storage = str(tmp_path / "store")
    cohort, labels = _build_cohort_and_labels(engine, run_id, [TRAIN_AS_OF, TEST_AS_OF])
    train, test = _build_matrices(engine, run_id, cohort, labels, storage)
    model = build_model(
        engine,
        run_id,
        train_matrix_result=train,
        class_path=CLASS_PATH,
        hyperparameters={"max_depth": 3},
        random_seed=7,
        storage_dir=storage,
        source_pins=_PINS,
    )

    first = score_and_evaluate(engine, model.model_id, model.estimator, test, TEST_AS_OF, LABEL_TIMESPAN)
    second = score_and_evaluate(engine, model.model_id, model.estimator, test, TEST_AS_OF, LABEL_TIMESPAN)
    assert first.num_predictions == 6
    assert second.num_predictions == 6

    with engine.connect() as conn:
        total = conn.execute(
            text("select count(*) from triage.predictions where model_id = :m"),
            {"m": model.model_id},
        ).scalar_one()
        # latest_predictions collapses the two scoring runs back to one row per entity
        latest = conn.execute(
            text("select count(*) from triage.latest_predictions where model_id = :m"),
            {"m": model.model_id},
        ).scalar_one()
        distinct_scored_at = conn.execute(
            text("select count(distinct scored_at) from triage.predictions where model_id = :m"),
            {"m": model.model_id},
        ).scalar_one()
    assert total == 12  # 6 entities × 2 scoring runs — appended, not overwritten
    assert latest == 6
    assert distinct_scored_at == 2  # each run got its own scored_at default


def test_model_group_reused_across_models(db_engine_greenfield, tmp_path):
    """A second model of the same family (estimator + hyperparams + features) reuses the group.

    Two models differing only in ``random_seed`` (not part of group identity) must mint
    distinct model rows + artifacts but share one ``model_group_id``.
    """
    engine = db_engine_greenfield
    run_id = _seed_lineage(engine)
    _seed_source(engine)
    storage = str(tmp_path / "store")
    cohort, labels = _build_cohort_and_labels(engine, run_id, [TRAIN_AS_OF, TEST_AS_OF])
    train, _ = _build_matrices(engine, run_id, cohort, labels, storage)

    m1 = build_model(
        engine,
        run_id,
        train_matrix_result=train,
        class_path=CLASS_PATH,
        hyperparameters={"max_depth": 3},
        random_seed=1,
        storage_dir=storage,
        source_pins=_PINS,
    )
    m2 = build_model(
        engine,
        run_id,
        train_matrix_result=train,
        class_path=CLASS_PATH,
        hyperparameters={"max_depth": 3},
        random_seed=2,  # different seed -> different model, same group
        storage_dir=storage,
        source_pins=_PINS,
    )
    assert m1.model_id != m2.model_id
    assert m1.model_artifact_id != m2.model_artifact_id
    assert m1.model_group_id == m2.model_group_id
    assert m1.model_group_hash == m2.model_group_hash

    with engine.connect() as conn:
        n_groups = conn.execute(text("select count(*) from triage.model_groups")).scalar_one()
    assert n_groups == 1


def test_build_model_cache_hit_on_rerun(db_engine_greenfield, tmp_path):
    """A second identical build_model is a cache hit: same artifact, no duplicate model row."""
    engine = db_engine_greenfield
    run_id = _seed_lineage(engine)
    _seed_source(engine)
    storage = str(tmp_path / "store")
    cohort, labels = _build_cohort_and_labels(engine, run_id, [TRAIN_AS_OF])
    train = build_matrix(
        engine,
        run_id,
        featurizer_config=_featurizer_config(),
        cohort_artifact_id=cohort,
        labels_artifact_id=labels,
        temporal_config=_temporal_config(),
        imputation_policy=ImputationPolicy.model_validate({"all": {"type": "zero"}}),
        matrix_kind="train",
        as_of_dates=[TRAIN_AS_OF],
        label_timespan=LABEL_TIMESPAN,
        storage_dir=storage,
        source_pins=_PINS,
    )
    kwargs = dict(
        train_matrix_result=train,
        class_path=CLASS_PATH,
        hyperparameters={"max_depth": 3},
        random_seed=99,
        storage_dir=storage,
        source_pins=_PINS,
    )
    first = build_model(engine, run_id, **kwargs)
    assert first.cache_hit is False
    second = build_model(engine, run_id, **kwargs)
    assert second.cache_hit is True
    assert second.model_id == first.model_id
    assert second.model_artifact_id == first.model_artifact_id

    with engine.connect() as conn:
        n_models = conn.execute(text("select count(*) from triage.models")).scalar_one()
        n_model_artifacts = conn.execute(
            text("select count(*) from triage.artifacts where kind = 'model'")
        ).scalar_one()
    assert n_models == 1
    assert n_model_artifacts == 1
