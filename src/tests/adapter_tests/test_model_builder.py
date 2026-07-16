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
from typing import Any

import pytest

from triage.adapters.cohort import build_cohort
from triage.adapters.imputation import ImputationPolicy
from triage.adapters.labels import build_labels
from triage.adapters.matrix import build_matrix
from triage.adapters.model import build_model, score_and_evaluate
from triage.adapters.temporal import TemporalConfig
from triage.derivation import as_uuid
from triage.profiles.storage import LocalStorage

TRAIN_AS_OF = date(2014, 1, 1)
TEST_AS_OF = date(2014, 7, 1)
LABEL_TIMESPAN = "6 months"
CLASS_PATH = "sklearn.tree.DecisionTreeClassifier"

COHORT_QUERY = (
    "select customer_id as entity_id from customers where {as_of_date} is not null"
)
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


def _featurizer_config() -> dict[str, Any]:
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
    with engine.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values ('exp-model', '{}'::jsonb, 'classification')"
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile) values ('exp-model', 'local') returning run_id"
        ).fetchone()["run_id"]
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
    with engine.connection() as conn:
        conn.execute(
            "create table customers (customer_id bigint primary key, signup_date date, age int)"
        )
        conn.execute(
            "insert into customers (customer_id, signup_date, age)"
            " select g, date '2010-01-01', 30 + g from unnest(%(ids)s) as g",
            {"ids": _CUSTOMERS},
        )
        conn.execute(
            "create table orders"
            " (order_id bigint primary key, customer_id bigint,"
            "  order_date date, amount double precision)"
        )
        for order_id, customer_id, order_date, amount in _ORDERS:
            conn.execute(
                "insert into orders (order_id, customer_id, order_date, amount)"
                " values (%(oid)s, %(cid)s, cast(%(od)s as date), %(amt)s)",
                {"oid": order_id, "cid": customer_id, "od": order_date, "amt": amount},
            )
        conn.execute(
            "create table label_src (entity_id bigint, knowledge_date date, outcome double precision)"
        )
        # One label per customer in each of the two label windows.
        for as_of in (TRAIN_AS_OF, TEST_AS_OF):
            for customer_id, outcome in _LABELS.items():
                conn.execute(
                    "insert into label_src (entity_id, knowledge_date, outcome)"
                    " values (%(eid)s, cast(%(kd)s as date), %(out)s)",
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
    with engine.connection() as conn:
        for as_of in (TRAIN_AS_OF, TEST_AS_OF):
            for entity_id, value in groups.items():
                conn.execute(
                    "insert into triage.protected_groups"
                    " (entity_id, as_of_date, attribute_name, attribute_value)"
                    " values (%(eid)s, %(aod)s, 'grp', %(val)s)",
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
    policy = ImputationPolicy.model_validate(
        {"all": {"type": "zero"}, "mean": {"type": "mean"}}
    )
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
        storage=LocalStorage(),
        storage_root=storage,
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
        storage=LocalStorage(),
        storage_root=storage,
        train_matrix_artifact_id=train.matrix_artifact_id,
        source_pins=_PINS,
    )
    return train, test


def test_model_build_predict_evaluate_full_lifecycle(db_pool_greenfield, tmp_path):
    import os

    engine = db_pool_greenfield
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
        storage=LocalStorage(),
        storage_root=storage,
        train_end_time=TRAIN_AS_OF,
        training_label_timespan=LABEL_TIMESPAN,
        source_pins=_PINS,
    )
    assert model.cache_hit is False

    # artifact DAG: a built model node whose single parent is the train matrix
    with engine.connection() as conn:
        art = conn.execute(
            "select kind, status from triage.artifacts where artifact_id = %(a)s",
            {"a": model.model_artifact_id},
        ).fetchone()
        parents = [
            r["parent_id"]
            for r in conn.execute(
                "select parent_id from triage.artifact_inputs where artifact_id = %(a)s",
                {"a": model.model_artifact_id},
            ).fetchall()
        ]
    assert art["kind"] == "model"
    assert art["status"] == "built"
    assert parents == [train.matrix_artifact_id]

    # triage.models row: model_hash == artifact_id, train_matrix_uuid set, file on disk
    with engine.connection() as conn:
        mrow = conn.execute(
            "select model_id, model_group_id, model_hash, train_matrix_uuid,"
            " artifact_uri, artifact_format, model_size_bytes, random_seed,"
            " training_label_timespan from triage.models where model_id = %(m)s",
            {"m": model.model_id},
        ).fetchone()
    assert mrow["model_hash"] == model.model_artifact_id
    assert str(mrow["train_matrix_uuid"]) == str(as_uuid(train.matrix_artifact_id))
    assert mrow["artifact_format"] == "joblib"
    assert mrow["model_size_bytes"] > 0
    assert mrow["random_seed"] == 42
    assert os.path.exists(model.artifact_uri)

    # model_groups row keyed by hash
    with engine.connection() as conn:
        grow = conn.execute(
            "select model_group_hash, model_type, feature_list"
            " from triage.model_groups where model_group_id = %(g)s",
            {"g": model.model_group_id},
        ).fetchone()
    assert grow["model_group_hash"] == model.model_group_hash
    assert grow["model_type"] == CLASS_PATH
    assert set(grow["feature_list"]) == set(train.feature_names)

    # feature_importances populated (DecisionTree exposes feature_importances_)
    with engine.connection() as conn:
        n_imp = conn.execute(
            "select count(*) as n from triage.feature_importances where model_id = %(m)s",
            {"m": model.model_id},
        ).fetchone()["n"]
        top = conn.execute(
            "select feature, rank_abs, rank_pct from triage.feature_importances"
            " where model_id = %(m)s order by rank_abs limit 1",
            {"m": model.model_id},
        ).fetchone()
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
    with engine.connection() as conn:
        pred_rows = conn.execute(
            "select entity_id, split_kind, matrix_uuid from triage.predictions where model_id = %(m)s",
            {"m": model.model_id},
        ).fetchall()
    assert len(pred_rows) == 6
    assert all(r["split_kind"] == "test" for r in pred_rows)
    assert all(
        str(r["matrix_uuid"]) == str(as_uuid(test.matrix_artifact_id))
        for r in pred_rows
    )

    # evaluations: perfect separation -> precision@/recall@/AUC == 1.0
    with engine.connection() as conn:
        evals = {
            (r["metric"], r["parameter"]): r["value"]
            for r in conn.execute(
                "select metric, parameter, value, num_labeled, num_positive"
                " from triage.evaluations where model_id = %(m)s",
                {"m": model.model_id},
            ).fetchall()
        }
    assert evals[("auc_roc", "")] == pytest.approx(1.0)
    # precision@50_pct: top 3 of 6 are exactly the 3 positives -> 1.0
    assert evals[("precision@", "50_pct")] == pytest.approx(1.0)
    # recall@50_pct: those 3 are all the positives -> 1.0
    assert evals[("recall@", "50_pct")] == pytest.approx(1.0)


def test_predictions_are_append_only(db_pool_greenfield, tmp_path):
    """Re-scoring the same model+matrix APPENDS rows (ADR-0006), never overwrites."""
    engine = db_pool_greenfield
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
        storage=LocalStorage(),
        storage_root=storage,
        source_pins=_PINS,
    )

    first = score_and_evaluate(
        engine, model.model_id, model.estimator, test, TEST_AS_OF, LABEL_TIMESPAN
    )
    second = score_and_evaluate(
        engine, model.model_id, model.estimator, test, TEST_AS_OF, LABEL_TIMESPAN
    )
    assert first.num_predictions == 6
    assert second.num_predictions == 6

    with engine.connection() as conn:
        total = conn.execute(
            "select count(*) as n from triage.predictions where model_id = %(m)s",
            {"m": model.model_id},
        ).fetchone()["n"]
        # latest_predictions collapses the two scoring runs back to one row per entity
        latest = conn.execute(
            "select count(*) as n from triage.latest_predictions where model_id = %(m)s",
            {"m": model.model_id},
        ).fetchone()["n"]
        distinct_scored_at = conn.execute(
            "select count(distinct scored_at) as n from triage.predictions where model_id = %(m)s",
            {"m": model.model_id},
        ).fetchone()["n"]
    assert total == 12  # 6 entities × 2 scoring runs — appended, not overwritten
    assert latest == 6
    assert distinct_scored_at == 2  # each run got its own scored_at default


def test_model_group_reused_across_models(db_pool_greenfield, tmp_path):
    """A second model of the same family (estimator + hyperparams + features) reuses the group.

    Two models differing only in ``random_seed`` (not part of group identity) must mint
    distinct model rows + artifacts but share one ``model_group_id``.
    """
    engine = db_pool_greenfield
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
        storage=LocalStorage(),
        storage_root=storage,
        source_pins=_PINS,
    )
    m2 = build_model(
        engine,
        run_id,
        train_matrix_result=train,
        class_path=CLASS_PATH,
        hyperparameters={"max_depth": 3},
        random_seed=2,  # different seed -> different model, same group
        storage=LocalStorage(),
        storage_root=storage,
        source_pins=_PINS,
    )
    assert m1.model_id != m2.model_id
    assert m1.model_artifact_id != m2.model_artifact_id
    assert m1.model_group_id == m2.model_group_id
    assert m1.model_group_hash == m2.model_group_hash

    with engine.connection() as conn:
        n_groups = conn.execute(
            "select count(*) as n from triage.model_groups"
        ).fetchone()["n"]
    assert n_groups == 1


def test_build_model_cache_hit_on_rerun(db_pool_greenfield, tmp_path):
    """A second identical build_model is a cache hit: same artifact, no duplicate model row."""
    engine = db_pool_greenfield
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
        storage=LocalStorage(),
        storage_root=storage,
        source_pins=_PINS,
    )
    kwargs: dict[str, Any] = dict(
        train_matrix_result=train,
        class_path=CLASS_PATH,
        hyperparameters={"max_depth": 3},
        random_seed=99,
        storage=LocalStorage(),
        storage_root=storage,
        source_pins=_PINS,
    )
    first = build_model(engine, run_id, **kwargs)
    assert first.cache_hit is False
    second = build_model(engine, run_id, **kwargs)
    assert second.cache_hit is True
    assert second.model_id == first.model_id
    assert second.model_artifact_id == first.model_artifact_id

    with engine.connection() as conn:
        n_models = conn.execute("select count(*) as n from triage.models").fetchone()[
            "n"
        ]
        n_model_artifacts = conn.execute(
            "select count(*) as n from triage.artifacts where kind = 'model'"
        ).fetchone()["n"]
    assert n_models == 1
    assert n_model_artifacts == 1


def test_feature_importance_values_linear_betas_and_odds():
    """Linear estimators (coef_) -> kind='coef', signed β, odds-ratio exp(β), ranking |β|."""
    import math

    import numpy as np

    from triage.adapters.model import _feature_importance_values

    class _Linear:
        coef_ = np.array([[0.5, -1.0, 0.0]])

    fi: dict[str, Any] | None = _feature_importance_values(_Linear(), 3)
    assert fi is not None
    assert fi["kind"] == "coef"
    assert list(fi["signed"]) == [0.5, -1.0, 0.0]
    assert fi["ranking"][1] == 1.0  # |-1.0| used for ranking
    assert math.isclose(fi["odds"][0], math.exp(0.5))
    assert math.isclose(fi["odds"][1], math.exp(-1.0))


def test_feature_importance_values_tree_gini():
    """Tree/ensemble estimators (feature_importances_) -> kind='gini'; no betas/odds."""
    import numpy as np

    from triage.adapters.model import _feature_importance_values

    class _Tree:
        feature_importances_ = np.array([0.7, 0.3])

    fi: dict[str, Any] | None = _feature_importance_values(_Tree(), 2)
    assert fi is not None
    assert fi["kind"] == "gini"
    assert fi["signed"] is None and fi["odds"] is None
    assert list(fi["ranking"]) == [0.7, 0.3]


def test_evaluation_is_per_as_of_date(db_pool_greenfield, tmp_path):
    """A test split spanning several as_of_dates is evaluated ONCE PER PREDICTION TIME
    (WS1), not collapsed at max(test_dates); triage.evaluations_windowed rolls them up.
    """
    engine = db_pool_greenfield
    run_id = _seed_lineage(engine)
    _seed_source(engine)

    # A SECOND test as_of_date beyond TEST_AS_OF, with its own label window
    # [2015-01-01, 2015-07-01) -> a knowledge_date of 2015-02-01.
    test_as_of_2 = date(2015, 1, 1)
    with engine.connection() as conn:
        for customer_id, outcome in _LABELS.items():
            conn.execute(
                "insert into label_src (entity_id, knowledge_date, outcome)"
                " values (%(eid)s, cast(%(kd)s as date), %(out)s)",
                {"eid": customer_id, "kd": "2015-02-01", "out": outcome},
            )

    storage = str(tmp_path / "store")
    test_dates = [TEST_AS_OF, test_as_of_2]
    cohort, labels = _build_cohort_and_labels(
        engine, run_id, [TRAIN_AS_OF, *test_dates]
    )

    temporal = _temporal_config()
    policy = ImputationPolicy.model_validate(
        {"all": {"type": "zero"}, "mean": {"type": "mean"}}
    )
    common: dict[str, Any] = dict(
        featurizer_config=_featurizer_config(),
        cohort_artifact_id=cohort,
        labels_artifact_id=labels,
        temporal_config=temporal,
        imputation_policy=policy,
        label_timespan=LABEL_TIMESPAN,
        storage=LocalStorage(),
        storage_root=storage,
        source_pins=_PINS,
    )
    train = build_matrix(
        engine,
        run_id,
        matrix_kind="train",
        as_of_dates=[TRAIN_AS_OF],
        lookback="6 months",
        **common,
    )
    test = build_matrix(
        engine,
        run_id,
        matrix_kind="test",
        as_of_dates=test_dates,
        train_matrix_artifact_id=train.matrix_artifact_id,
        **common,
    )
    model = build_model(
        engine,
        run_id,
        train_matrix_result=train,
        class_path=CLASS_PATH,
        hyperparameters={"max_depth": 3},
        random_seed=42,
        storage=LocalStorage(),
        storage_root=storage,
        train_end_time=TRAIN_AS_OF,
        training_label_timespan=LABEL_TIMESPAN,
        source_pins=_PINS,
    )

    # as_of_date=None -> evaluate EVERY distinct test as_of_date.
    result = score_and_evaluate(
        engine,
        model.model_id,
        model.estimator,
        test,
        as_of_date=None,
        label_timespan=LABEL_TIMESPAN,
        metric_config={"metrics": ["precision@", "auc_roc"], "thresholds": ["50_pct"]},
    )
    assert result.num_predictions == 12  # 6 entities × 2 prediction times

    with engine.connection() as conn:
        eval_dates = [
            r["as_of_date"]
            for r in conn.execute(
                "select distinct as_of_date from triage.evaluations"
                " where model_id = %(m)s order by as_of_date",
                {"m": model.model_id},
            ).fetchall()
        ]
        windowed = conn.execute(
            "select n_as_of_dates, window_start, window_end"
            " from triage.evaluations_windowed"
            " where model_id = %(m)s and metric = 'auc_roc' and parameter = ''",
            {"m": model.model_id},
        ).fetchone()
    # one evaluation row-set PER prediction time (not just max(test_dates))
    assert eval_dates == [TEST_AS_OF, test_as_of_2]
    # the windowed view rolls the two prediction times up
    assert windowed["n_as_of_dates"] == 2
    assert windowed["window_start"] == TEST_AS_OF
    assert windowed["window_end"] == test_as_of_2
