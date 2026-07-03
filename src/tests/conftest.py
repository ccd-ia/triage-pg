import pytest
from pytest_postgresql import factories

# Create postgresql process fixture (session-scoped, starts PostgreSQL once)
postgresql_proc = factories.postgresql_proc(port=None)

# Create postgresql client fixture (function-scoped, creates fresh db per test)
postgresql = factories.postgresql("postgresql_proc")


@pytest.fixture(name="db_url", scope="function")
def fixture_db_url(postgresql):
    """The ``postgresql+psycopg://`` SQLAlchemy URL for the throwaway test DB.

    For alembic-machinery tests (the migration layer kept on SQLAlchemy, ADR-0019) and to
    drive ``upgrade_db(dburl=...)``.
    """
    return f"postgresql+psycopg://{postgresql.info.user}@{postgresql.info.host}:{postgresql.info.port}/{postgresql.info.dbname}"


@pytest.fixture(name="db_pool", scope="function")
def fixture_db_pool(postgresql):
    """psycopg3 ``ConnectionPool`` over the throwaway test DB (ADR-0019).

    The greenfield application-side connection source; replaces the SQLAlchemy ``db_engine``
    fixture as adapters are converted. Shares the same ``postgresql`` (function-scoped) DB as
    ``db_url`` within a test.
    """
    from triage.util.db import connection_pool

    conninfo = f"postgresql://{postgresql.info.user}@{postgresql.info.host}:{postgresql.info.port}/{postgresql.info.dbname}"
    pool = connection_pool(conninfo)
    yield pool
    pool.close()


@pytest.fixture(scope="function")
def db_pool_greenfield(db_url, db_pool):
    """Fresh test DB with the greenfield ``triage`` schema applied, yielding a psycopg3 pool.

    The pool-based counterpart of ``db_engine_greenfield``: runs the per-project alembic
    migrations (0001 -> head) via ``upgrade_db(dburl=...)`` against the same throwaway DB the
    ``db_pool`` connects to, then yields that pool.
    """
    from triage.component.results_schema import upgrade_db

    upgrade_db(dburl=db_url, revision="head")
    yield db_pool


@pytest.fixture(scope="module")
def sample_timechop_splits():
    return [
        {
            "feature_start_time": "2010-01-01T00:00:00",
            "feature_end_time": "2014-01-01T00:00:00",
            "label_start_time": "2011-01-01T00:00:00",
            "label_end_time": "2014-01-01T00:00:00",
            "train_matrix": {
                "first_as_of_time": "2011-06-01T00:00:00",
                "last_as_of_time": "2011-12-01T00:00:00",
                "matrix_info_end_time": "2012-06-01T00:00:00",
                "as_of_times": [
                    "2011-06-01T00:00:00",
                    "2011-07-01T00:00:00",
                    "2011-08-01T00:00:00",
                    "2011-09-01T00:00:00",
                    "2011-10-01T00:00:00",
                    "2011-11-01T00:00:00",
                    "2011-12-01T00:00:00",
                ],
                "training_label_timespan": "6months",
                "training_as_of_date_frequency": "1month",
                "max_training_history": "6months",
            },
            "test_matrices": [
                {
                    "first_as_of_time": "2012-06-01T00:00:00",
                    "last_as_of_time": "2012-06-01T00:00:00",
                    "matrix_info_end_time": "2012-12-01T00:00:00",
                    "as_of_times": ["2012-06-01T00:00:00"],
                    "test_label_timespan": "6months",
                    "test_as_of_date_frequency": "3months",
                    "test_duration": "1months",
                }
            ],
            "train_uuid": "40de3a41a7b210c6a525adeb74fafb22",
            "test_uuids": ["6c41a75c5270ed036370ca2344371150"],
        },
        {
            "feature_start_time": "2010-01-01T00:00:00",
            "feature_end_time": "2014-01-01T00:00:00",
            "label_start_time": "2011-01-01T00:00:00",
            "label_end_time": "2014-01-01T00:00:00",
            "train_matrix": {
                "first_as_of_time": "2012-06-01T00:00:00",
                "last_as_of_time": "2012-12-01T00:00:00",
                "matrix_info_end_time": "2013-06-01T00:00:00",
                "as_of_times": [
                    "2012-06-01T00:00:00",
                    "2012-07-01T00:00:00",
                    "2012-08-01T00:00:00",
                    "2012-09-01T00:00:00",
                    "2012-10-01T00:00:00",
                    "2012-11-01T00:00:00",
                    "2012-12-01T00:00:00",
                ],
                "training_label_timespan": "6months",
                "training_as_of_date_frequency": "1month",
                "max_training_history": "6months",
            },
            "test_matrices": [
                {
                    "first_as_of_time": "2013-06-01T00:00:00",
                    "last_as_of_time": "2013-06-01T00:00:00",
                    "matrix_info_end_time": "2013-12-01T00:00:00",
                    "as_of_times": ["2013-06-01T00:00:00"],
                    "test_label_timespan": "6months",
                    "test_as_of_date_frequency": "3months",
                    "test_duration": "1months",
                }
            ],
            "train_uuid": "95f998f70d5be1cf3d2ec833cd9db079",
            "test_uuids": ["8fd8be5c0b8b2e5b06a233b960769ccf"],
        },
    ]


@pytest.fixture(scope="module")
def sample_grid_config():
    return {
        "sklearn.tree.DecisionTreeClassifier": {
            "max_depth": [2, 10],
            "min_samples_split": [2],
        },
        "sklearn.ensemble.ExtraTreesClassifier": {
            "n_jobs": [-1],
            "n_estimators": [10],
            "criterion": ["gini"],
            "max_depth": [1],
            "max_features": ["sqrt"],
            "min_samples_split": [2, 5],
        },
        "sklearn.ensemble.GradientBoostingClassifier": {
            "loss": ["deviance", "exponential"]
        },
        "triage.component.catwalk.estimators.classifiers.ScaledLogisticRegression": {
            "penalty": ["l1", "l2"],
            "C": [0.01, 1],
        },
        "triage.component.catwalk.baselines.rankers.PercentileRankOneFeature": {
            "feature": ["feature_one", "feature_two"],
            "low_value_high_score": [True],
        },
        "triage.component.catwalk.baselines.thresholders.SimpleThresholder": {
            "rules": [["feature_one > 3", "feature_two <= 5"]],
            "logical_operator": ["and"],
        },
    }
