"""Validation of the in-Postgres audition layer (read-dashboard-spec §3.4, 0004).

The ``0004_dashboard_reads`` migration ports the standard triage audition catalog
(``selection_rules.py``) + distance-from-best / regret (``distance_from_best.py`` /
``regrets.py``) to SQL: the ``triage.audition`` view and the
``triage.audition_pick`` / ``triage.selected_model`` functions. This test is the
ADR-0007/0012 proof that the SQL matches the Python oracle: it seeds a small KNOWN
fixture (3 model_groups × 3 splits) engineered so each selection rule picks a
*different* group, then asserts the SQL picks the hand-computed group.

Fixture (metric auc_roc, higher-is-better; average_precision mirrors it):

        split A   split B   split C      avg     stddev   current(C)
  mg1    0.50      0.60      0.90       0.667    0.208      0.90   <- best_current_value
  mg2    0.80      0.82      0.81       0.810    0.010      0.81   <- best_average_value, ...
  mg3    0.70      0.70      0.70       0.700    0.000      0.70   <- lowest_metric_variance

  best per split: A=0.80(mg2)  B=0.82(mg2)  C=0.90(mg1)
  distance-from-best:        mg1 .30/.22/0   mg2 0/0/.09   mg3 .10/.12/.20
  avg distance / max regret: mg1 .173/.30    mg2 .03/.09   mg3 .14/.20
"""

import json

import pytest

SPLITS = ["2014-01-01", "2014-07-01", "2015-01-01"]
GROUPS = {
    "mg1": [0.50, 0.60, 0.90],
    "mg2": [0.80, 0.82, 0.81],
    "mg3": [0.70, 0.70, 0.70],
}
METRICS = ["auc_roc", "average_precision"]  # ap mirrors auc so two-metrics == best avg


@pytest.fixture
def audition_fixture(db_pool_greenfield):
    """Seed experiment/run/model_groups/models/evaluations; return (pool, experiment_hash,
    group_ids{name->id}, latest_model{name->model_id at split C}).

    Audition is experiment-scoped (migration 0005): the scope key is the experiment_hash
    ('exp-aud'), not the run_id — a re-run cache-shares models, so run-scoping would go empty.
    """
    pool = db_pool_greenfield
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values ('exp-aud', '{}'::jsonb, 'classification')"
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile)"
            " values ('exp-aud', 'local') returning run_id"
        ).fetchone()["run_id"]

        group_ids: dict[str, int] = {}
        latest_model: dict[str, int] = {}
        for name, values in GROUPS.items():
            gid = conn.execute(
                "insert into triage.model_groups"
                " (model_group_hash, model_type, hyperparameters, feature_list)"
                " values (%(h)s, 'sklearn.tree.DecisionTreeClassifier', '{}'::jsonb,"
                " ARRAY['f1','f2']) returning model_group_id",
                {"h": f"hash-{name}"},
            ).fetchone()["model_group_id"]
            group_ids[name] = gid
            for split, value in zip(SPLITS, values):
                art = f"model-{name}-{split}"
                conn.execute(
                    "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
                    " values (%(a)s, %(a)s, 'model', '{}'::jsonb)",
                    {"a": art},
                )
                model_id = conn.execute(
                    "insert into triage.models"
                    " (model_group_id, model_hash, run_id, train_end_time)"
                    " values (%(g)s, %(a)s, %(r)s, %(t)s) returning model_id",
                    {"g": gid, "a": art, "r": run_id, "t": split},
                ).fetchone()["model_id"]
                latest_model[name] = model_id  # SPLITS ascending -> ends at C
                for metric in METRICS:
                    conn.execute(
                        "insert into triage.evaluations"
                        " (model_id, split_kind, as_of_date, metric, parameter, value)"
                        " values (%(m)s, 'test', %(d)s, %(metric)s, '', %(v)s)",
                        {"m": model_id, "d": split, "metric": metric, "v": value},
                    )
    return pool, "exp-aud", group_ids, latest_model


def _audition_row(pool, exp, group_id, metric="auc_roc"):
    with pool.connection() as conn:
        return conn.execute(
            "select * from triage.audition where experiment_hash = %(r)s and metric = %(m)s"
            " and parameter = '' and model_group_id = %(g)s",
            {"r": exp, "m": metric, "g": group_id},
        ).fetchone()


def test_audition_view_distance_and_regret(audition_fixture):
    pool, exp, gids, _ = audition_fixture
    mg1, mg2, mg3 = gids["mg1"], gids["mg2"], gids["mg3"]

    r1, r2, r3 = (_audition_row(pool, exp, g) for g in (mg1, mg2, mg3))
    assert r1["n_splits_evaluated"] == 3
    # avg distance-from-best and max regret (hand-computed above)
    assert r1["avg_distance_from_best"] == pytest.approx((0.30 + 0.22 + 0.0) / 3)
    assert r1["max_regret"] == pytest.approx(0.30)
    assert r2["avg_distance_from_best"] == pytest.approx(0.09 / 3)
    assert r2["max_regret"] == pytest.approx(0.09)
    assert r3["avg_distance_from_best"] == pytest.approx((0.10 + 0.12 + 0.20) / 3)
    assert r3["max_regret"] == pytest.approx(0.20)
    # avg value
    assert r2["avg_value"] == pytest.approx(0.81)
    assert r3["stddev_value"] == pytest.approx(0.0)


def _pick(pool, exp, rule, params=None):
    with pool.connection() as conn:
        return conn.execute(
            "select triage.audition_pick(%(r)s, 'auc_roc', '', %(rule)s, %(p)s::jsonb)"
            " as g",
            {"r": exp, "rule": rule, "p": json.dumps(params or {})},
        ).fetchone()["g"]


def test_selection_rules_each_pick_their_group(audition_fixture):
    pool, exp, gids, _ = audition_fixture
    mg1, mg2, mg3 = gids["mg1"], gids["mg2"], gids["mg3"]

    # the three discriminating rules pick three different groups
    assert _pick(pool, exp, "best_current_value") == mg1
    assert _pick(pool, exp, "best_average_value") == mg2
    assert _pick(pool, exp, "lowest_metric_variance") == mg3

    # the remaining standard rules (all resolve to the consistent group mg2 here)
    assert _pick(pool, exp, "most_frequent_best_dist", {"dist_window": 0.1}) == mg2
    assert _pick(pool, exp, "best_avg_var_penalized", {"stdev_penalty": 1.0}) == mg2
    assert (
        _pick(
            pool,
            exp,
            "best_avg_recency_weight",
            {"curr_weight": 5.0, "decay_type": "linear"},
        )
        == mg2
    )
    assert (
        _pick(
            pool,
            exp,
            "best_average_two_metrics",
            {"metric2": "average_precision", "parameter2": "", "metric1_weight": 0.5},
        )
        == mg2
    )
    # baseline rule returns *some* valid group deterministically
    assert _pick(pool, exp, "random_model_group", {"seed": "7"}) in gids.values()

    with pytest.raises(Exception, match="unknown audition rule"):
        _pick(pool, exp, "no_such_rule")


def test_selected_model_divergence(audition_fixture):
    pool, exp, gids, latest = audition_fixture
    with pool.connection() as conn:
        row = conn.execute(
            "select * from triage.selected_model(%(r)s, 'auc_roc', '',"
            " 'best_average_value')",
            {"r": exp},
        ).fetchone()
    # default rule (best_average_value) picks mg2; leaderboard #1 (best_current_value)
    # picks mg1 -> they diverge (the §2-C flag).
    assert row["audition_group"] == gids["mg2"]
    assert row["leaderboard_group"] == gids["mg1"]
    assert row["diverges"] is True
    # each group resolves to its latest-split (C) model
    assert row["audition_model"] == latest["mg2"]
    assert row["leaderboard_model"] == latest["mg1"]
