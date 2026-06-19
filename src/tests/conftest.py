import tempfile

import pytest
from pytest_postgresql import factories

from triage import create_engine
from triage.component.catwalk.storage import ProjectStorage

# Create postgresql process fixture (session-scoped, starts PostgreSQL once)
postgresql_proc = factories.postgresql_proc(port=None)

# Create postgresql client fixture (function-scoped, creates fresh db per test)
postgresql = factories.postgresql("postgresql_proc")


@pytest.fixture(name="db_engine", scope="function")
def fixture_db_engine(postgresql):
    """pytest fixture provider to set up and teardown a "test" database
    and provide the test function a connection engine with which to
    query that database.

    """
    # Build connection URL from pytest-postgresql fixture
    connection_url = f"postgresql+psycopg://{postgresql.info.user}@{postgresql.info.host}:{postgresql.info.port}/{postgresql.info.dbname}"
    engine = create_engine(connection_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="function")
def db_engine_greenfield(db_engine):
    """Fresh pytest-postgresql DB with the greenfield ``triage`` schema applied.

    Runs the per-project alembic migrations (results_schema/alembic.ini, 0001 ->
    head) against the throwaway ``db_engine`` database, creating the greenfield
    ``triage.*`` schema (artifacts/runs/cohorts/labels/... + the 0002 metric
    functions). This is the schema the greenfield builders (cohort, labels,
    matrix, model) write to — distinct from ``db_engine_with_results_schema``,
    which builds the *inherited* ORM schema via ``Base.metadata.create_all``.
    """
    from triage.component.results_schema import upgrade_db

    upgrade_db(db_engine=db_engine, revision="head")
    yield db_engine


@pytest.fixture(scope="function")
def project_path():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture(scope="function")
def project_storage(project_path):
    """Set up a temporary project storage engine on the filesystem

    Yields (catwalk.storage.ProjectStorage)
    """
    yield ProjectStorage(project_path)


@pytest.fixture(scope="module")
def shared_db_engine(postgresql_proc):
    """pytest fixture provider to set up and teardown a "test" database
    and provide a test module a connection engine with which to
    query that database.

    Uses pytest-postgresql's DatabaseJanitor for module-scoped database management.
    """
    import uuid

    from pytest_postgresql.janitor import DatabaseJanitor

    # Create a unique database name for this module
    db_name = f"test_module_{uuid.uuid4().hex[:8]}"

    with DatabaseJanitor(
        user=postgresql_proc.user,
        host=postgresql_proc.host,
        port=postgresql_proc.port,
        dbname=db_name,
        version=postgresql_proc.version,
        password=postgresql_proc.password or "",
    ):
        connection_url = f"postgresql+psycopg://{postgresql_proc.user}@{postgresql_proc.host}:{postgresql_proc.port}/{db_name}"
        engine = create_engine(connection_url)
        yield engine
        engine.dispose()


@pytest.fixture(scope="module")
def shared_project_storage():
    """Set up a temporary project storage engine on the filesystem at module scope

    Yields (catwalk.storage.ProjectStorage)
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        project_storage = ProjectStorage(temp_dir)
        yield project_storage


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
