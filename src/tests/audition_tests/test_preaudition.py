import uuid
from datetime import datetime

import pytest

from triage.component.audition.pre_audition import PreAudition

from .utils import insert_evaluation, insert_model, insert_model_group


def _insert_experiment_and_run(conn):
    """Create a ``triage.experiments`` + ``triage.runs`` pair; return (hash, run_id).

    ``get_model_groups_from_experiment`` joins ``triage.models`` to
    ``triage.runs`` on ``run_id`` and filters by ``runs.experiment_hash``. One
    experiment/run per model group makes each experiment_hash map to exactly one
    model group (matching the original "== 1" assertions).
    """
    experiment_hash = uuid.uuid4().hex
    conn.execute(
        "insert into triage.experiments (experiment_hash, config, problem_type)"
        " values (%(h)s, '{}'::jsonb, 'classification')",
        {"h": experiment_hash},
    )
    run_id = conn.execute(
        "insert into triage.runs (experiment_hash, profile) values (%(h)s, 'local') returning run_id",
        {"h": experiment_hash},
    ).fetchone()["run_id"]
    return experiment_hash, run_id


def test_PreAudition(db_pool_greenfield):
    db_engine = db_pool_greenfield

    num_model_groups = 10
    model_types = ["classifier type {}".format(i) for i in range(0, num_model_groups)]
    train_end_times = [
        datetime(2013, 1, 1),
        datetime(2013, 7, 1),
        datetime(2014, 1, 1),
        datetime(2014, 7, 1),
        datetime(2015, 1, 1),
        datetime(2015, 7, 1),
        datetime(2016, 7, 1),
        datetime(2016, 1, 1),
    ]
    metrics = [
        ("precision@", "100_abs"),
        ("recall@", "100_abs"),
        ("precision@", "50_abs"),
        ("recall@", "50_abs"),
        ("fpr@", "10_pct"),
    ]

    model_group_ids = []
    experiment_hashes = []
    with db_engine.connection() as conn:
        for model_type in model_types:
            mgid = insert_model_group(conn, model_type)
            model_group_ids.append(mgid)
            # one experiment/run per model group -> experiment_hash maps 1:1 to group
            experiment_hash, run_id = _insert_experiment_and_run(conn)
            experiment_hashes.append(experiment_hash)
            for train_end_time in train_end_times:
                tet = train_end_time.strftime("%Y-%m-%d")
                model_id = insert_model(conn, mgid, tet, run_id=run_id)
                for metric, parameter in metrics:
                    insert_evaluation(conn, model_id, metric, parameter, 0.5, as_of_date=tet)

    pre_aud = PreAudition(db_engine)

    # Label-based selection is not available on the greenfield schema
    # (triage.model_groups.config carries no 'label_definition').
    with pytest.raises(NotImplementedError):
        pre_aud.get_model_groups_from_label("label_1")

    # Expect exactly one model group for a given experiment_hash
    with db_engine.connection() as conn:
        experiment_hash = conn.execute("""SELECT r.experiment_hash
            FROM triage.models m
            JOIN triage.runs r ON m.run_id = r.run_id
            limit 1""").fetchone()["experiment_hash"]
    assert len(pre_aud.get_model_groups_from_experiment(experiment_hash)["model_groups"]) == 1

    # Expect the number of model groups for customs SQL
    query = f"""
        SELECT DISTINCT(model_group_id)
        FROM triage.models m
        JOIN triage.runs r ON m.run_id = r.run_id
        WHERE m.train_end_time >= '2013-01-01'
        AND r.experiment_hash = '{experiment_hash}'
    """

    assert len(pre_aud.get_model_groups(query)) == 1
    # Expect the number of train_end_times after 2014-01-01
    assert len(pre_aud.get_train_end_times(after="2014-01-01")) == 6

    query = """
        SELECT DISTINCT train_end_time
        FROM triage.models
        WHERE model_group_id IN ({})
            AND train_end_time >= '2014-01-01'
        ORDER BY train_end_time
        """.format(", ".join(map(str, pre_aud.model_groups)))

    assert len(pre_aud.get_train_end_times(query=query)) == 6
