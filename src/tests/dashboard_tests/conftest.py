"""Shared fixtures for the read-dashboard API contract tests (dashboard-api-contract.md).

Seeds a small but fully-populated EXPERIMENT against the ``db_pool_greenfield`` DB (reusing the
seeding shape of ``catwalk_tests/test_audition_sql.py`` and ``adapter_tests/
test_run_orchestration.py``), then yields a FastAPI ``TestClient`` pointed at that same pool.
The app is built with ``create_app(pool=...)`` so it shares the throwaway test DB rather than
resolving a real project DB from the environment.

The dashboard analysis is **experiment-scoped** (migration 0005). The seed deliberately uses
TWO runs that SHARE one experiment_hash, where the second run cache-shares the first run's
models (``models.run_id`` points at run 1). This is the Q1 regression fixture: run-scoped
audition would go empty on the re-run; experiment-scoped audition must still be non-empty.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from triage.dashboard.app import create_app

# A 3-model_group x 3-split fixture (mirrors test_audition_sql): mg1 wins current, mg2 wins
# average, mg3 is most stable — enough to make audition non-empty and divergence observable.
SPLITS = ["2014-01-01", "2014-07-01", "2015-01-01"]
GROUPS = {
    "mg1": [0.50, 0.60, 0.90],
    "mg2": [0.80, 0.82, 0.81],
    "mg3": [0.70, 0.70, 0.70],
}
METRICS = ["auc_roc", "average_precision"]
EXPERIMENT_HASH = "exp-dash"
LABEL_TIMESPAN = "6 months"


@dataclass
class SeededExperiment:
    experiment_hash: str
    run_id: str  # the FIRST run (the builder run; owns cohort/labels + models)
    rerun_id: str  # the SECOND run, cache-sharing run 1's models (Q1 regression)
    group_ids: dict[str, int]
    latest_model: dict[str, int]  # name -> model_id at the latest split
    all_models: dict[str, list[int]] = field(default_factory=dict)


def _seed_full_experiment(pool) -> SeededExperiment:
    """Seed one experiment with TWO runs (the 2nd cache-shares the 1st's models),
    model_groups/models/evaluations, cohort/labels artifacts, bias_metrics, predictions,
    feature_importances, and source pins."""
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type,"
            " name, description, author) values (%(h)s, %(cfg)s::jsonb, 'classification',"
            " 'Churn baseline', 'first churn experiment', 'tester')",
            {
                "h": EXPERIMENT_HASH,
                "cfg": json.dumps({"cohort_name": "active", "label_name": "churn"}),
            },
        )
        plan = json.dumps(
            {
                "n_splits": 3,
                "n_features": 12,
                "n_models": 9,
                "estimator_types": ["sklearn.tree.DecisionTreeClassifier"],
                "engine_versions": {"featurizer": "0.4.1"},
            }
        )
        # Run 1 is the builder run; run 2 re-runs the same experiment and cache-shares its
        # models (models.run_id stays run 1). Both share EXPERIMENT_HASH.
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile, status, plan,"
            " triage_version, git_hash, batch_job_id)"
            " values (%(h)s, 'local', 'completed', %(plan)s::jsonb,"
            " '5.5.6', 'abc1234', 'batch-1') returning run_id",
            {"h": EXPERIMENT_HASH, "plan": plan},
        ).fetchone()["run_id"]
        rerun_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile, status, plan,"
            " triage_version, git_hash, batch_job_id)"
            " values (%(h)s, 'local', 'completed', %(plan)s::jsonb,"
            " '5.5.6', 'def5678', 'batch-2') returning run_id",
            {"h": EXPERIMENT_HASH, "plan": plan},
        ).fetchone()["run_id"]

        # ---- cohort + labels artifacts (run_artifacts edges scope cohort_profile/base_rate)
        cohort_hash = "art-cohort"
        labels_hash = "art-labels"
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config, status,"
            " built_by_run) values (%(a)s, %(a)s, 'cohort', '{}'::jsonb, 'built', %(r)s)",
            {"a": cohort_hash, "r": run_id},
        )
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config, status,"
            " built_by_run) values (%(a)s, %(a)s, 'labels', '{}'::jsonb, 'built', %(r)s)",
            {"a": labels_hash, "r": run_id},
        )
        conn.execute(
            "insert into triage.artifact_inputs (artifact_id, parent_id)"
            " values (%(c)s, %(p)s)",
            {"c": labels_hash, "p": cohort_hash},
        )
        # both runs USED the cohort + labels (the cache-shared usage edges)
        for art in (cohort_hash, labels_hash):
            for r in (run_id, rerun_id):
                conn.execute(
                    "insert into triage.run_artifacts (run_id, artifact_id)"
                    " values (%(r)s, %(a)s)",
                    {"r": r, "a": art},
                )
        for as_of in SPLITS:
            for entity_id in (1, 2, 3, 4):
                conn.execute(
                    "insert into triage.cohorts (cohort_hash, entity_id, as_of_date)"
                    " values (%(h)s, %(e)s, %(d)s)",
                    {"h": cohort_hash, "e": entity_id, "d": as_of},
                )
                conn.execute(
                    "insert into triage.labels (label_hash, entity_id, as_of_date,"
                    " label_timespan, outcome) values (%(h)s, %(e)s, %(d)s,"
                    " %(ts)s::interval, %(o)s)",
                    {
                        "h": labels_hash,
                        "e": entity_id,
                        "d": as_of,
                        "ts": LABEL_TIMESPAN,
                        "o": entity_id % 2,
                    },
                )

        # ---- source registry + pins (both runs pin the same source). The relation must be a
        # real table: /ontology profiles each source via triage.source_volume, which regclass-
        # resolves the relation and groups its knowledge_date_column over time.
        conn.execute("create table customers (customer_id bigint, signup_date date)")
        conn.execute(
            "insert into customers (customer_id, signup_date)"
            " values (1, date '2014-01-15'), (2, date '2014-02-20')"
        )
        conn.execute(
            "insert into triage.sources (source_name, relation, knowledge_date_column,"
            " description) values ('customers', 'customers', 'signup_date', 'customer master')"
        )
        conn.execute(
            "insert into triage.source_versions (source_name, version_label)"
            " values ('customers', 'v1')"
        )
        for r in (run_id, rerun_id):
            conn.execute(
                "insert into triage.run_source_pins (run_id, source_name, version_label)"
                " values (%(r)s, 'customers', 'v1')",
                {"r": r},
            )

        # ---- model_groups + models + evaluations (+ a model artifact each). Models belong
        # to run 1 (the builder run); run 2 cache-shares them.
        group_ids: dict[str, int] = {}
        latest_model: dict[str, int] = {}
        all_models: dict[str, list[int]] = {}
        first_model_id = None
        for name, values in GROUPS.items():
            gid = conn.execute(
                "insert into triage.model_groups (model_group_hash, model_type,"
                " hyperparameters, feature_list) values (%(h)s,"
                " 'sklearn.tree.DecisionTreeClassifier', '{}'::jsonb, ARRAY['f1','f2'])"
                " returning model_group_id",
                {"h": f"hash-{name}"},
            ).fetchone()["model_group_id"]
            group_ids[name] = gid
            all_models[name] = []
            for split, value in zip(SPLITS, values):
                art = f"model-{name}-{split}"
                conn.execute(
                    "insert into triage.artifacts (artifact_id, logical_id, kind, config,"
                    " status, built_by_run) values (%(a)s, %(a)s, 'model', '{}'::jsonb,"
                    " 'built', %(r)s)",
                    {"a": art, "r": run_id},
                )
                # both runs USED each model artifact (cache-shared usage edges)
                for r in (run_id, rerun_id):
                    conn.execute(
                        "insert into triage.run_artifacts (run_id, artifact_id)"
                        " values (%(r)s, %(a)s)",
                        {"r": r, "a": art},
                    )
                model_id = conn.execute(
                    "insert into triage.models (model_group_id, model_hash, run_id,"
                    " train_end_time, training_label_timespan)"
                    " values (%(g)s, %(a)s, %(r)s, %(t)s, %(ts)s::interval)"
                    " returning model_id",
                    {
                        "g": gid,
                        "a": art,
                        "r": run_id,
                        "t": split,
                        "ts": LABEL_TIMESPAN,
                    },
                ).fetchone()["model_id"]
                latest_model[name] = model_id
                all_models[name].append(model_id)
                if first_model_id is None:
                    first_model_id = model_id
                for metric in METRICS:
                    conn.execute(
                        "insert into triage.evaluations (model_id, split_kind, as_of_date,"
                        " metric, parameter, value, num_labeled, num_positive)"
                        " values (%(m)s, 'test', %(d)s, %(metric)s, '', %(v)s, 4, 2)",
                        {"m": model_id, "d": split, "metric": metric, "v": value},
                    )

        # ---- feature importances + bias + predictions for the first model (at the last split)
        for feat, imp, rk in (("f1", 0.7, 1), ("f2", 0.3, 2)):
            conn.execute(
                "insert into triage.feature_importances (model_id, feature,"
                " feature_importance, rank_abs, rank_pct) values (%(m)s, %(f)s, %(i)s, %(r)s,"
                " %(p)s)",
                {"m": first_model_id, "f": feat, "i": imp, "r": rk, "p": rk / 2.0},
            )
        conn.execute(
            "insert into triage.bias_metrics (model_id, split_kind, as_of_date, parameter,"
            " attribute_name, attribute_value, metric, value, disparity)"
            " values (%(m)s, 'test', %(d)s, '', 'race', 'A', 'tpr', 0.8, 1.0)",
            {"m": first_model_id, "d": SPLITS[-1]},
        )
        # predictions at the last split (where the labels artifact has matching as_of_dates)
        for entity_id, score in ((1, 0.9), (2, 0.7), (3, 0.4), (4, 0.1)):
            conn.execute(
                "insert into triage.predictions (model_id, entity_id, as_of_date,"
                " split_kind, score) values (%(m)s, %(e)s, %(d)s, 'test', %(s)s)",
                {"m": first_model_id, "e": entity_id, "d": SPLITS[-1], "s": score},
            )

        # ---- refresh the leaderboard matview (created `with no data`)
        conn.execute("refresh materialized view triage.leaderboard")

    return SeededExperiment(
        experiment_hash=EXPERIMENT_HASH,
        run_id=str(run_id),
        rerun_id=str(rerun_id),
        group_ids=group_ids,
        latest_model=latest_model,
        all_models=all_models,
    )


@pytest.fixture
def seeded(db_pool_greenfield) -> SeededExperiment:
    return _seed_full_experiment(db_pool_greenfield)


@pytest.fixture
def empty_experiment(db_pool_greenfield) -> tuple[str, str]:
    """An experiment + run with NO evaluations/bias/predictions, for the empty-state contract.

    Returns ``(experiment_hash, run_id)``.
    """
    pool = db_pool_greenfield
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values ('exp-empty', '{}'::jsonb, 'classification')"
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile, status)"
            " values ('exp-empty', 'local', 'started') returning run_id"
        ).fetchone()["run_id"]
    return "exp-empty", str(run_id)


@pytest.fixture
def client(db_pool_greenfield) -> Iterator[TestClient]:
    """A TestClient whose app shares the throwaway greenfield test DB pool."""
    app = create_app(pool=db_pool_greenfield)
    with TestClient(app) as c:
        yield c
