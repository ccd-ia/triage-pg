"""Greenfield run orchestration end-to-end (ADR-0012, ADR-0014).

Seeds tiny, split-stable synthetic source data, then drives the WHOLE pipeline through one
:func:`triage.adapters.run.run_experiment` call — experiment/run rows, source pinning,
timechop splits, one cohort + one labels over the union of split dates, per-split train+test
matrices, and a grid × split of models scored + evaluated. Asserts:

* the lineage rows — ``triage.experiments`` + ``triage.runs`` (run ``completed``) +
  ``triage.run_source_pins`` for the declared sources;
* the full artifact DAG via ``triage.artifacts`` + ``artifact_inputs`` — cohort (root) →
  labels; feature_group → cohort; matrix → {feature_group, cohort, labels}; test_matrix →
  train_matrix; model → train_matrix; every node ``status='built'``;
* ``triage.matrices`` (train + test per split, parquet, files on disk), ``triage.models`` +
  ``triage.model_groups``, append-only ``triage.predictions`` for the test split, and
  ``triage.evaluations`` with sane values;
* CACHE REUSE — a second identical ``run_experiment`` (same config + same pinned sources) is
  mostly cache hits: no duplicate cohort/labels/matrix/model artifact rows, counts unchanged.

The pinning step (register + bump the declared sources) is what makes the second run cacheable
— an unpinned source is volatile and would rebuild every run (ADR-0014).
"""

import os

import pytest
from sqlalchemy import text

from triage.adapters.run import experiment_hash_for, run_experiment

PROBLEM_TYPE = "classification"
LABEL_TIMESPAN = "6 months"
CLASS_PATH = "sklearn.tree.DecisionTreeClassifier"

# A small temporal config -> 3 splits over distinct as_of_dates
# {2013-01-01, 2013-07-01, 2014-01-01, 2014-07-01} (probed from timechop).
TEMPORAL_CONFIG = {
    "feature_start_time": "2013-01-01",
    "feature_end_time": "2015-01-01",
    "label_start_time": "2013-01-01",
    "label_end_time": "2015-01-01",
    "model_update_frequency": "6 months",
    "training_as_of_date_frequencies": "6 months",
    "test_as_of_date_frequencies": "6 months",
    "max_training_histories": "6 months",
    "test_durations": "0 days",
    "label_timespans": LABEL_TIMESPAN,
}

COHORT_QUERY = (
    "select customer_id as entity_id from customers where {as_of_date} is not null"
)
# Outcome read from a labels source in the [as_of, as_of + span) window.
LABEL_QUERY = (
    "select entity_id, outcome from label_src"
    " where knowledge_date >= date {as_of_date}"
    " and knowledge_date < date {as_of_date} + {label_timespan}"
)

# Six customers; the COUNT of (pre-as_of) orders separates the classes perfectly and is the
# SAME at every as_of_date because all orders are dated before the earliest split date — a
# clean, split-stable signal a DecisionTree learns identically across all splits.
_CUSTOMERS = [1, 2, 3, 4, 5, 6]
_ORDERS = [
    (101, 1, "2012-01-01", 50.0),
    (102, 1, "2012-02-01", 50.0),
    (103, 1, "2012-03-01", 50.0),  # customer 1: 3 orders -> positive
    (104, 2, "2012-01-01", 40.0),
    (105, 2, "2012-02-01", 40.0),  # customer 2: 2 orders -> positive
    (106, 3, "2012-01-01", 30.0),
    (107, 3, "2012-02-01", 30.0),  # customer 3: 2 orders -> positive
    (108, 4, "2012-01-01", 20.0),  # customer 4: 1 order  -> negative
    (109, 5, "2012-01-01", 10.0),  # customer 5: 1 order  -> negative
    # customer 6: 0 orders -> negative
]
_LABELS = {1: 1.0, 2: 1.0, 3: 1.0, 4: 0.0, 5: 0.0, 6: 0.0}
# The label windows start at each distinct as_of_date; a label row must land in every window.
_AS_OF_MONTHS = ["2013-01-01", "2013-07-01", "2014-01-01", "2014-07-01"]


def _featurizer_config() -> dict:
    """customers (target) ⋈ orders. COUNT/SUM/MEAN of orders.amount is the signal."""
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


def _experiment_config() -> dict:
    return {
        "problem_type": PROBLEM_TYPE,
        "temporal_config": TEMPORAL_CONFIG,
        "cohort_config": {"query": COHORT_QUERY},
        "label_config": {"query": LABEL_QUERY},
        "feature_config": _featurizer_config(),
        "imputation_config": {"all": {"type": "zero"}, "mean": {"type": "mean"}},
        "grid_config": {CLASS_PATH: {"max_depth": [3]}},
        "sources": [
            {"name": "customers", "relation": "customers", "version_label": "v1"},
            {
                "name": "orders",
                "relation": "orders",
                "knowledge_date_column": "order_date",
                "version_label": "v1",
            },
            {
                "name": "label_src",
                "relation": "label_src",
                "knowledge_date_column": "knowledge_date",
                "version_label": "v1",
            },
        ],
    }


def _seed_source(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "create table customers (customer_id bigint primary key, signup_date date, age int)"
            )
        )
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
        conn.execute(
            text(
                "create table label_src (entity_id bigint, knowledge_date date, outcome double precision)"
            )
        )
        # One label per customer per label window (each window starts at an as_of_date).
        for as_of in _AS_OF_MONTHS:
            year, month, _ = as_of.split("-")
            kd = f"{year}-{month}-15"  # mid-window: inside [as_of, as_of + 6 months)
            for customer_id, outcome in _LABELS.items():
                conn.execute(
                    text(
                        "insert into label_src (entity_id, knowledge_date, outcome)"
                        " values (:eid, cast(:kd as date), :out)"
                    ),
                    {"eid": customer_id, "kd": kd, "out": outcome},
                )


def test_run_experiment_end_to_end(db_engine_greenfield, tmp_path):
    engine = db_engine_greenfield
    _seed_source(engine)
    storage = str(tmp_path / "store")
    config = _experiment_config()

    result = run_experiment(engine, config, storage_dir=storage, random_seed=42)

    # ---- lineage: experiment + run rows, run completed
    assert result.experiment_hash == experiment_hash_for(config)
    with engine.connect() as conn:
        exp = (
            conn.execute(
                text(
                    "select problem_type from triage.experiments where experiment_hash = :h"
                ),
                {"h": result.experiment_hash},
            )
            .mappings()
            .one()
        )
        run = (
            conn.execute(
                text(
                    "select status, profile, random_seed, finished_at from triage.runs where run_id = :r"
                ),
                {"r": result.run_id},
            )
            .mappings()
            .one()
        )
    assert exp["problem_type"] == PROBLEM_TYPE
    assert run["status"] == "completed"
    assert run["profile"] == "local"
    assert run["random_seed"] == 42
    assert run["finished_at"] is not None

    # ---- run_source_pins: one per declared source, each pinned to v1
    with engine.connect() as conn:
        pins = {
            r["source_name"]: r["version_label"]
            for r in conn.execute(
                text(
                    "select source_name, version_label from triage.run_source_pins where run_id = :r"
                ),
                {"r": result.run_id},
            ).mappings()
        }
    assert pins == {"customers": "v1", "orders": "v1", "label_src": "v1"}
    # the frozen pins returned in the result match what was recorded
    assert result.source_pins == pins

    # ---- splits built (3 splits, each with a train + test matrix)
    assert len(result.splits) == 3
    for split in result.splits:
        assert split.train_matrix.matrix_artifact_id
        assert split.test_matrix.matrix_artifact_id
        assert os.path.exists(split.train_matrix.storage_uri)
        assert os.path.exists(split.test_matrix.storage_uri)

    # ---- the artifact DAG: cohort (root) -> labels; feature_group -> cohort;
    # matrix -> {feature_group, cohort, labels}; test_matrix -> train_matrix; model -> train_matrix
    def parents_of(artifact_id):
        with engine.connect() as conn:
            return set(
                conn.execute(
                    text(
                        "select parent_id from triage.artifact_inputs where artifact_id = :a"
                    ),
                    {"a": artifact_id},
                )
                .scalars()
                .all()
            )

    def status_of(artifact_id):
        with engine.connect() as conn:
            return conn.execute(
                text("select status from triage.artifacts where artifact_id = :a"),
                {"a": artifact_id},
            ).scalar_one()

    # cohort is a DAG root (no parents); labels' single parent is the cohort
    assert parents_of(result.cohort_artifact_id) == set()
    assert parents_of(result.labels_artifact_id) == {result.cohort_artifact_id}
    assert status_of(result.cohort_artifact_id) == "built"
    assert status_of(result.labels_artifact_id) == "built"

    first_split = result.splits[0]
    train_mx = first_split.train_matrix
    test_mx = first_split.test_matrix
    # feature_group's parent is the cohort
    assert parents_of(train_mx.feature_group_artifact_id) == {result.cohort_artifact_id}
    # train matrix parents: feature_group, cohort, labels
    assert parents_of(train_mx.matrix_artifact_id) == {
        train_mx.feature_group_artifact_id,
        result.cohort_artifact_id,
        result.labels_artifact_id,
    }
    # test matrix additionally carries the train matrix (the leakage-boundary edge)
    assert train_mx.matrix_artifact_id in parents_of(test_mx.matrix_artifact_id)
    # every model's single parent is its split's train matrix
    for split in result.splits:
        for model_artifact_id in split.model_artifact_ids:
            assert parents_of(model_artifact_id) == {
                split.train_matrix.matrix_artifact_id
            }
            assert status_of(model_artifact_id) == "built"

    # all artifacts built (no failed/building nodes)
    with engine.connect() as conn:
        not_built = conn.execute(
            text("select count(*) from triage.artifacts where status <> 'built'")
        ).scalar_one()
    assert not_built == 0

    # ---- triage.matrices: 2 per split (train + test), parquet, files on disk
    with engine.connect() as conn:
        mx_rows = (
            conn.execute(
                text(
                    "select matrix_kind, storage_format, storage_uri, num_entities from triage.matrices"
                )
            )
            .mappings()
            .all()
        )
    assert len(mx_rows) == 6  # 3 splits × (train + test)
    assert all(r["storage_format"] == "parquet" for r in mx_rows)
    assert all(os.path.exists(r["storage_uri"]) for r in mx_rows)
    assert all(r["num_entities"] > 0 for r in mx_rows)

    # ---- models + model_groups: one model per split (same family) -> one shared group
    with engine.connect() as conn:
        n_models = conn.execute(text("select count(*) from triage.models")).scalar_one()
        n_groups = conn.execute(
            text("select count(*) from triage.model_groups")
        ).scalar_one()
    assert n_models == 3  # one DecisionTree per split
    assert (
        n_groups == 1
    )  # same estimator + hyperparams + feature list -> one group across splits
    assert result.num_models == 3

    # ---- predictions: append-only test-split rows referencing the test matrix_uuid
    with engine.connect() as conn:
        pred_rows = (
            conn.execute(text("select split_kind from triage.predictions"))
            .mappings()
            .all()
        )
    # 6 entities per test matrix × 3 splits
    assert len(pred_rows) == 18
    assert result.num_predictions == 18
    assert all(r["split_kind"] == "test" for r in pred_rows)

    # ---- evaluations: rows with sane values (perfect separation -> AUC == 1.0)
    with engine.connect() as conn:
        n_evals = conn.execute(
            text("select count(*) from triage.evaluations")
        ).scalar_one()
        auc_values = (
            conn.execute(
                text("select value from triage.evaluations where metric = 'auc_roc'")
            )
            .scalars()
            .all()
        )
    assert n_evals > 0
    assert result.num_evaluations == n_evals
    assert auc_values  # at least one AUC computed
    assert all(v == pytest.approx(1.0) for v in auc_values)


def test_run_experiment_cache_reuse_on_rerun(db_engine_greenfield, tmp_path):
    """A second identical run reuses every artifact: no new artifact/matrix/model rows.

    Source pinning (register + bump in run_experiment) makes the derivations cacheable; the
    second run therefore cache-hits the cohort, labels, feature groups, matrices, and models
    instead of rebuilding them — the derivation cache works ACROSS runs (ADR-0014).
    """
    engine = db_engine_greenfield
    _seed_source(engine)
    storage = str(tmp_path / "store")
    config = _experiment_config()

    first = run_experiment(engine, config, storage_dir=storage, random_seed=42)

    def counts():
        with engine.connect() as conn:
            return {
                "artifacts": conn.execute(
                    text("select count(*) from triage.artifacts")
                ).scalar_one(),
                "matrices": conn.execute(
                    text("select count(*) from triage.matrices")
                ).scalar_one(),
                "models": conn.execute(
                    text("select count(*) from triage.models")
                ).scalar_one(),
                "model_groups": conn.execute(
                    text("select count(*) from triage.model_groups")
                ).scalar_one(),
            }

    after_first = counts()

    second = run_experiment(engine, config, storage_dir=storage, random_seed=42)
    after_second = counts()

    # same experiment hash (same config) — a re-run reuses the experiment row
    assert second.experiment_hash == first.experiment_hash
    # but a NEW run row each time
    assert second.run_id != first.run_id

    # the cache worked across runs: no duplicate artifact / matrix / model / group rows
    assert after_second == after_first

    # the SAME artifact ids were reused (cohort, labels, and every split's matrices/models)
    assert second.cohort_artifact_id == first.cohort_artifact_id
    assert second.labels_artifact_id == first.labels_artifact_id
    for s1, s2 in zip(first.splits, second.splits, strict=True):
        assert s2.train_matrix.matrix_artifact_id == s1.train_matrix.matrix_artifact_id
        assert s2.test_matrix.matrix_artifact_id == s1.test_matrix.matrix_artifact_id
        assert s2.model_artifact_ids == s1.model_artifact_ids
        # cache hits flagged on the reused matrices
        assert s2.train_matrix.cache_hit is True
        assert s2.test_matrix.cache_hit is True

    # the second run still recorded its own source pins + used the same artifacts
    with engine.connect() as conn:
        n_run_pins = conn.execute(
            text("select count(*) from triage.run_source_pins where run_id = :r"),
            {"r": second.run_id},
        ).scalar_one()
        # both runs are GC roots for the shared artifacts (run_artifacts usage edges)
        n_usage = conn.execute(
            text("select count(*) from triage.run_artifacts where run_id = :r"),
            {"r": second.run_id},
        ).scalar_one()
    assert n_run_pins == 3
    assert (
        n_usage > 0
    )  # the second run recorded usage edges even though everything cache-hit
