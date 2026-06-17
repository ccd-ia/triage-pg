"""Matrix-assembler lifecycle + leakage-boundary tests (ADR-0008, ADR-0009, ADR-0015).

Seeds tiny source tables, builds a cohort + labels via the F1 builders, then assembles a
TRAIN and a TEST matrix through :func:`triage.adapters.build_matrix`. Asserts the seam and
the two ADR boundaries:

* the artifact DAG (matrix -> [feature_group, cohort, labels]; test_matrix -> train_matrix);
* ``triage.matrices`` rows keyed by ``matrix_uuid == as_uuid(artifact_id)``;
* fit-based statistics persisted in the TRAIN matrix metadata;
* **the leakage property** — a fit-based fill in the TEST matrix equals the TRAIN-computed
  statistic, NOT the test split's own statistic (the fixture is built so they differ);
* **as_of_boundary exclusive** — a feature contribution knowable only ON the as_of_date is
  excluded (strict ``<``);
* a rerun is a cache hit (same matrix_uuid, no duplicate artifacts).
"""

from datetime import date

import polars as pl
import pytest
from sqlalchemy import text

from triage.adapters.cohort import build_cohort
from triage.adapters.imputation import ImputationPolicy
from triage.adapters.labels import build_labels
from triage.adapters.matrix import build_matrix
from triage.adapters.temporal import TemporalConfig
from triage.derivation import as_uuid

# One train as_of_date, one test as_of_date — distinct so train-only fitting is observable.
TRAIN_AS_OF = date(2014, 1, 1)
TEST_AS_OF = date(2014, 7, 1)
LABEL_TIMESPAN = "6 months"

# Cohort: the three customers exist on every as_of_date (a date-independent roster).
COHORT_QUERY = (
    "select customer_id as entity_id from customers where {as_of_date} is not null"
)

# Classification labels: outcome read from a labels source in the [as_of, as_of+span) window.
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
    """A two-entity ER graph: customers (target) with orders. P3650D = ~10y window.

    The wide P3650D interval makes the aggregation effectively all-history so the boundary
    is the only thing that excludes an order — letting us isolate the exclusive-``<`` test.
    """
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
                " values ('exp-matrix', '{}'::jsonb, 'classification')"
            )
        )
        run_id = conn.execute(
            text(
                "insert into triage.runs (experiment_hash, profile)"
                " values ('exp-matrix', 'local') returning run_id"
            )
        ).scalar_one()
    return str(run_id)


def _seed_source(engine) -> None:
    """Source tables, designed so the leakage + boundary properties are observable.

    customers: 1, 2, 3 (all in the cohort, all as_of_dates).
    orders, with the wide all-history interval:
      * entity 1 has orders BEFORE the train as_of (2013-12-01, amount 100) AND before the
        test as_of (those plus 2014-06-01, amount 300). So entity 1 always has a non-null
        mean -> it never needs fit-based imputation; it sets the train statistic.
      * entity 2 has NO orders -> its MEAN(amount) is NULL on BOTH train and test, so it is
        the row whose fit-based mean fill we inspect.
      * entity 3 has a *large* order (amount 1000) dated EXACTLY on the test as_of
        (2014-07-01). With the exclusive boundary that order is NOT knowable on 2014-07-01,
        so entity 3's MEAN(amount) is NULL on the test side (boundary assertion) — and 3 has
        no earlier orders, so it too needs the fit-based fill on test.

    The train mean of amount is computed from entity 1 only (100.0). On the test side the
    *non-null* amounts would be entity 1's {100, 300} (mean 200) — deliberately different
    from the train statistic. The leakage assertion checks entity 2/3's filled value equals
    the TRAIN mean (100.0), not any test-derived number.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "create table customers"
                " (customer_id bigint primary key, signup_date date, age int)"
            )
        )
        conn.execute(
            text(
                "insert into customers (customer_id, signup_date, age) values"
                " (1, date '2010-01-01', 30),"
                " (2, date '2010-01-01', 40),"
                " (3, date '2010-01-01', 50)"
            )
        )
        conn.execute(
            text(
                "create table orders"
                " (order_id bigint primary key, customer_id bigint,"
                "  order_date date, amount double precision)"
            )
        )
        conn.execute(
            text(
                "insert into orders (order_id, customer_id, order_date, amount) values"
                " (10, 1, date '2013-12-01', 100.0),"  # before train + test
                " (11, 1, date '2014-06-01', 300.0),"  # before test only
                " (30, 3, date '2014-07-01', 1000.0)"  # ON the test as_of (boundary)
            )
        )
        # Labels: entity 1, 2, 3 each get an outcome in both windows.
        conn.execute(
            text(
                "create table label_src"
                " (entity_id bigint, knowledge_date date, outcome double precision)"
            )
        )
        conn.execute(
            text(
                "insert into label_src (entity_id, knowledge_date, outcome) values"
                " (1, date '2014-02-01', 1.0), (2, date '2014-03-01', 0.0),"
                " (3, date '2014-04-01', 1.0),"
                " (1, date '2014-08-01', 0.0), (2, date '2014-09-01', 1.0),"
                " (3, date '2014-10-01', 0.0)"
            )
        )


def _build_cohort_and_labels(engine, run_id, as_of_dates) -> tuple[str, str]:
    """Build one cohort + labels spanning the given as_of_dates.

    Realistic pattern (adapter-spec §1.5): a single cohort/labels span every split's
    as_of_dates, and each matrix INNER-JOINs to select only its own split's dates. The
    config carries the dates so a train-only and an all-dates cohort hash distinctly.
    """
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


def _mean_amount_feature(feature_names) -> str:
    """The non-interval MEAN(orders.amount) feature column (the fit-based target)."""
    for name in feature_names:
        if name.startswith("MEAN(orders.amount)") and "interval" not in name:
            return name
    raise AssertionError(f"no MEAN(orders.amount) feature in {feature_names}")


def _read_parquet_value(storage_uri, feature, entity_id) -> float | None:
    frame = pl.read_parquet(storage_uri)
    row = frame.filter(pl.col("entity_id") == entity_id)
    assert row.height == 1, f"entity {entity_id} not unique in {storage_uri}"
    return row.get_column(feature)[0]


def test_train_then_test_matrix_full_lifecycle(db_engine_greenfield, tmp_path):
    engine = db_engine_greenfield
    run_id = _seed_lineage(engine)
    _seed_source(engine)
    temporal = _temporal_config()
    # mean fill for measures; counts/sums keep featurizer's fit-free zero (the `all` fallback
    # is fit-free zero, with an explicit fit-based mean for the `mean` metric).
    policy = ImputationPolicy.model_validate(
        {"all": {"type": "zero"}, "mean": {"type": "mean"}}
    )
    storage = str(tmp_path / "matrices")

    # One cohort + labels span both split dates (adapter-spec §1.5); each matrix selects
    # its own split date via the inner join.
    cohort, labels = _build_cohort_and_labels(engine, run_id, [TRAIN_AS_OF, TEST_AS_OF])

    # ---- TRAIN matrix
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
        source_pins={"customers": "v1", "orders": "v1", "label_src": "v1"},
    )

    # Parquet written; matrices row keyed by as_uuid(artifact_id)
    import os

    assert os.path.exists(train.storage_uri)
    with engine.connect() as conn:
        mrow = (
            conn.execute(
                text(
                    "select matrix_uuid, artifact_id, matrix_kind, storage_uri,"
                    " num_entities, num_features, metadata"
                    " from triage.matrices where artifact_id = :a"
                ),
                {"a": train.matrix_artifact_id},
            )
            .mappings()
            .one()
        )
    assert str(mrow["matrix_uuid"]) == str(as_uuid(train.matrix_artifact_id))
    assert mrow["matrix_kind"] == "train"
    assert mrow["num_entities"] == 3  # all three cohort entities on the one as_of_date

    # artifact DAG: matrix -> [feature_group, cohort, labels]
    with engine.connect() as conn:
        parents = set(
            conn.execute(
                text(
                    "select parent_id from triage.artifact_inputs where artifact_id = :a"
                ),
                {"a": train.matrix_artifact_id},
            )
            .scalars()
            .all()
        )
    assert parents == {train.feature_group_artifact_id, cohort, labels}

    # feature_group -> cohort edge
    with engine.connect() as conn:
        fg_parents = (
            conn.execute(
                text(
                    "select parent_id from triage.artifact_inputs where artifact_id = :a"
                ),
                {"a": train.feature_group_artifact_id},
            )
            .scalars()
            .all()
        )
    assert fg_parents == [cohort]

    # fit-based stat persisted in TRAIN metadata: the mean over the TRAIN split only.
    feature = _mean_amount_feature(train.feature_names)
    stats = mrow["metadata"]["fit_based_stats"]
    assert feature in stats
    assert stats[feature]["stat"] == "mean"
    # Only entity 1 has a non-null mean on the train side (its single order, amount 100).
    train_fitted_mean = stats[feature]["value"]
    assert train_fitted_mean == pytest.approx(100.0)

    # ---- TEST matrix (same cohort/labels; reuses the train-fitted stats — the leakage edge)
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
        source_pins={"customers": "v1", "orders": "v1", "label_src": "v1"},
    )

    # test_matrix -> train_matrix edge (the leakage-boundary dependency)
    with engine.connect() as conn:
        test_parents = set(
            conn.execute(
                text(
                    "select parent_id from triage.artifact_inputs where artifact_id = :a"
                ),
                {"a": test.matrix_artifact_id},
            )
            .scalars()
            .all()
        )
    assert train.matrix_artifact_id in test_parents
    assert {cohort, labels, test.feature_group_artifact_id} <= test_parents

    # ---- THE LEAKAGE PROPERTY ----------------------------------------------------------
    # Entity 2 has no orders -> its MEAN(amount) is NULL pre-fill on both sides. The fill it
    # receives in the TEST matrix MUST be the TRAIN statistic (100.0), NOT a test-derived
    # number. The test split's own non-null amounts are {100, 300} (mean 200), so a leak
    # would show up as ~200. We assert exactly the train value.
    test_fill_entity2 = _read_parquet_value(test.storage_uri, feature, 2)
    assert test_fill_entity2 == pytest.approx(train_fitted_mean)  # == 100.0
    assert test_fill_entity2 != pytest.approx(200.0)  # the leak would be ~200

    # The reused stats are exactly the train stats (no recompute on test).
    with engine.connect() as conn:
        test_meta = conn.execute(
            text("select metadata from triage.matrices where artifact_id = :a"),
            {"a": test.matrix_artifact_id},
        ).scalar_one()
    assert test_meta["fit_based_stats"][feature]["value"] == pytest.approx(100.0)

    # ---- as_of_boundary EXCLUSIVE -------------------------------------------------------
    # Entity 3's only order is dated EXACTLY on the test as_of (2014-07-01). With strict
    # ``<`` it is NOT knowable on that date, so entity 3's MEAN(amount) was NULL pre-fill and
    # therefore got the same train fill (100.0). If the boundary were inclusive (``<=``), the
    # 1000.0 order would have produced a non-null mean of 1000.0 for entity 3 -> no fill.
    test_fill_entity3 = _read_parquet_value(test.storage_uri, feature, 3)
    assert test_fill_entity3 == pytest.approx(
        100.0
    )  # imputed -> order excluded by ``<``
    assert test_fill_entity3 != pytest.approx(
        1000.0
    )  # the on-date order is NOT knowable

    # Entity 1's mean on test is a real (non-imputed) value from its pre-as_of orders.
    test_value_entity1 = _read_parquet_value(test.storage_uri, feature, 1)
    assert test_value_entity1 == pytest.approx(
        200.0
    )  # {100, 300} both before 2014-07-01


def test_matrix_cache_hit_on_rerun(db_engine_greenfield, tmp_path):
    engine = db_engine_greenfield
    run_id = _seed_lineage(engine)
    _seed_source(engine)
    temporal = _temporal_config()
    policy = ImputationPolicy.model_validate({"all": {"type": "zero"}})
    storage = str(tmp_path / "matrices")
    cohort_tr, labels_tr = _build_cohort_and_labels(engine, run_id, [TRAIN_AS_OF])

    kwargs = dict(
        featurizer_config=_featurizer_config(),
        cohort_artifact_id=cohort_tr,
        labels_artifact_id=labels_tr,
        temporal_config=temporal,
        imputation_policy=policy,
        matrix_kind="train",
        as_of_dates=[TRAIN_AS_OF],
        label_timespan=LABEL_TIMESPAN,
        storage_dir=storage,
        source_pins={"customers": "v1", "orders": "v1", "label_src": "v1"},
    )
    first = build_matrix(engine, run_id, **kwargs)
    assert first.cache_hit is False
    second = build_matrix(engine, run_id, **kwargs)
    assert second.cache_hit is True
    assert second.matrix_artifact_id == first.matrix_artifact_id

    # No duplicate matrix artifact / matrices row
    with engine.connect() as conn:
        n_matrices = conn.execute(
            text("select count(*) from triage.matrices")
        ).scalar_one()
        n_artifacts = conn.execute(
            text("select count(*) from triage.artifacts where kind = 'matrix'")
        ).scalar_one()
    assert n_matrices == 1
    assert n_artifacts == 1


def test_test_matrix_requires_train_parent(db_engine_greenfield, tmp_path):
    engine = db_engine_greenfield
    run_id = _seed_lineage(engine)
    _seed_source(engine)
    cohort_te, labels_te = _build_cohort_and_labels(engine, run_id, [TEST_AS_OF])
    with pytest.raises(ValueError, match="train_matrix_artifact_id"):
        build_matrix(
            engine,
            run_id,
            featurizer_config=_featurizer_config(),
            cohort_artifact_id=cohort_te,
            labels_artifact_id=labels_te,
            temporal_config=_temporal_config(),
            imputation_policy=ImputationPolicy.model_validate(
                {"all": {"type": "zero"}}
            ),
            matrix_kind="test",
            as_of_dates=[TEST_AS_OF],
            label_timespan=LABEL_TIMESPAN,
            storage_dir=str(tmp_path / "matrices"),
            train_matrix_artifact_id=None,  # the error
            source_pins={"customers": "v1", "orders": "v1", "label_src": "v1"},
        )
