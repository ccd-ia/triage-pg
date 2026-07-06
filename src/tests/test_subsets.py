"""Subset-filtered evaluation tests (migration 0015, plan P3).

The invariants under test, on a 10-entity fixture with known scores/labels:

* DSSG semantics — the subset IS the population: ranks are recomputed within the
  subset, so precision@2 on the subset differs from the full cohort by construction;
* isolation — a subset evaluation writes its own rows and leaves the full-cohort
  rows byte-identical;
* the default path (subset_hash='') is untouched (the broader guarantee is the whole
  pre-existing metric suite, which runs against the migrated head);
* materialization — register_subsets hashes, registers, and upserts idempotently,
  and every malformed config fails loud.
"""

from __future__ import annotations

import pytest

from triage.adapters.run import validate_experiment_config
from triage.adapters.subsets import (
    register_subsets,
    subset_hash_for,
    validate_subsets_config,
)
from triage.component.catwalk.in_pg_evaluation import evaluate_in_db

AS_OF = "2014-01-01"
TIMESPAN = "6 months"

# Scores rank e1..e10 in order; labels: positives at e1,e2,e4,e6,e9.
SCORES = [0.95, 0.90, 0.85, 0.80, 0.75, 0.60, 0.50, 0.40, 0.30, 0.20]
LABELS = [1, 1, 0, 1, 0, 1, 0, 0, 1, 0]
ENTITY_IDS = list(range(1, 11))

# The subset: entities {3, 4, 9, 10} — within-subset ranking by score:
#   e3 (0.85, label 0), e4 (0.80, label 1), e9 (0.30, label 1), e10 (0.20, label 0)
# precision@2 within the subset = 1/2 (e3, e4). Full-cohort precision@2 = 1.0 (e1, e2).
SUBSET_ENTITIES = [3, 4, 9, 10]
SUBSET_CONFIG = {
    "name": "the-four",
    "query": (
        "select entity_id from (values (3), (4), (9), (10)) as t(entity_id)"
        " where date '{as_of_date}' >= date '2014-01-01'"
    ),
}


def _seed(pool):
    """Model + predictions + labels — the same shape test_in_pg_metrics uses."""
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " values ('model-art-sub', 'model-log-sub', 'model', '{}'::jsonb),"
            "        ('lbl-sub', 'lbl-log-sub', 'labels', '{}'::jsonb)"
        )
        group_id = conn.execute(
            "insert into triage.model_groups"
            " (model_group_hash, model_type, hyperparameters, feature_list)"
            " values ('mg-sub', 'x.Y', '{}'::jsonb, ARRAY['f1'])"
            " returning model_group_id"
        ).fetchone()["model_group_id"]
        model_id = conn.execute(
            "insert into triage.models (model_group_id, model_hash, train_end_time)"
            " values (%(g)s, 'model-art-sub', date '2013-12-01') returning model_id",
            {"g": group_id},
        ).fetchone()["model_id"]
        for eid, score, label in zip(ENTITY_IDS, SCORES, LABELS):
            conn.execute(
                "insert into triage.predictions"
                " (model_id, entity_id, as_of_date, split_kind, score)"
                " values (%(m)s, %(e)s, %(d)s, 'test', %(s)s)",
                {"m": model_id, "e": eid, "d": AS_OF, "s": score},
            )
            conn.execute(
                "insert into triage.labels"
                " (label_hash, entity_id, as_of_date, label_timespan, outcome)"
                " values ('lbl-sub', %(e)s, %(d)s, %(t)s::interval, %(o)s)",
                {"e": eid, "d": AS_OF, "t": TIMESPAN, "o": float(label)},
            )
    return model_id


def _eval_rows(pool, model_id, subset_hash):
    with pool.connection() as conn:
        return {
            (r["metric"], r["parameter"]): r
            for r in conn.execute(
                "select metric, parameter, value, num_labeled, num_positive"
                " from triage.evaluations"
                " where model_id = %(m)s and subset_hash = %(s)s",
                {"m": model_id, "s": subset_hash},
            ).fetchall()
        }


CFG = {"metrics": ["precision@", "recall@", "auc_roc"], "thresholds": ["2_abs"]}


def test_subset_metrics_treat_subset_as_population(db_pool_greenfield):
    model_id = _seed(db_pool_greenfield)
    [entry] = register_subsets(db_pool_greenfield, [SUBSET_CONFIG], [AS_OF])
    sh = entry["subset_hash"]
    assert sh == subset_hash_for(SUBSET_CONFIG)

    evaluate_in_db(db_pool_greenfield, model_id, AS_OF, TIMESPAN, metric_config=CFG)
    evaluate_in_db(
        db_pool_greenfield, model_id, AS_OF, TIMESPAN, metric_config=CFG, subset_hash=sh
    )

    full = _eval_rows(db_pool_greenfield, model_id, "")
    sub = _eval_rows(db_pool_greenfield, model_id, sh)

    # full cohort: top-2 = e1,e2 (both positive) -> precision@2 = 1.0, N=10, P=5
    assert full[("precision@", "2_abs")]["value"] == pytest.approx(1.0)
    assert full[("precision@", "2_abs")]["num_labeled"] == 10
    # subset population: top-2 WITHIN {e3,e4,e9,e10} = e3(0),e4(1) -> 1/2, N=4, P=2
    assert sub[("precision@", "2_abs")]["value"] == pytest.approx(0.5)
    assert sub[("precision@", "2_abs")]["num_labeled"] == 4
    assert sub[("precision@", "2_abs")]["num_positive"] == 2
    # recall@2 within the subset: 1 of its 2 positives caught
    assert sub[("recall@", "2_abs")]["value"] == pytest.approx(0.5)
    # subset AUC: positives (e4 .80, e9 .30) vs negatives (e3 .85, e10 .20):
    # pairs (4,3)=0, (4,10)=1, (9,3)=0, (9,10)=1 -> 2/4
    assert sub[("auc_roc", "")]["value"] == pytest.approx(0.5)


def test_subset_evaluation_leaves_full_cohort_rows_untouched(db_pool_greenfield):
    model_id = _seed(db_pool_greenfield)
    evaluate_in_db(db_pool_greenfield, model_id, AS_OF, TIMESPAN, metric_config=CFG)
    before = _eval_rows(db_pool_greenfield, model_id, "")

    [entry] = register_subsets(db_pool_greenfield, [SUBSET_CONFIG], [AS_OF])
    evaluate_in_db(
        db_pool_greenfield,
        model_id,
        AS_OF,
        TIMESPAN,
        metric_config=CFG,
        subset_hash=entry["subset_hash"],
    )
    after = _eval_rows(db_pool_greenfield, model_id, "")
    assert after == before  # byte-identical full-cohort rows


def test_register_subsets_idempotent_and_validating(db_pool_greenfield):
    register_subsets(db_pool_greenfield, [SUBSET_CONFIG], [AS_OF])
    register_subsets(db_pool_greenfield, [SUBSET_CONFIG], [AS_OF])  # no dupes
    with db_pool_greenfield.connection() as conn:
        n = conn.execute("select count(*) as n from triage.subset_members").fetchone()[
            "n"
        ]
    assert n == len(SUBSET_ENTITIES)

    with pytest.raises(ValueError, match="placeholder"):
        validate_subsets_config([{"name": "x", "query": "select 1 as entity_id"}])
    with pytest.raises(ValueError, match="duplicate"):
        validate_subsets_config([SUBSET_CONFIG, SUBSET_CONFIG])
    with pytest.raises(ValueError, match="name"):
        validate_subsets_config([{"query": "q {as_of_date}"}])
    with pytest.raises(ValueError, match="entity_id"):
        register_subsets(
            db_pool_greenfield,
            [
                {
                    "name": "bad",
                    "query": "select 1 as eid where date '{as_of_date}' is not null",
                }
            ],
            [AS_OF],
        )


def test_unknown_subset_hash_evaluates_empty_population(db_pool_greenfield):
    model_id = _seed(db_pool_greenfield)
    evaluate_in_db(
        db_pool_greenfield,
        model_id,
        AS_OF,
        TIMESPAN,
        metric_config={"metrics": ["precision@"], "thresholds": ["2_abs"]},
        subset_hash="no-such-subset",
    )
    # honest empty: counts land, value is NULL (n_labeled = 0 short-circuit)
    with db_pool_greenfield.connection() as conn:
        rows = conn.execute(
            "select value, num_labeled from triage.evaluations"
            " where model_id = %(m)s and subset_hash = 'no-such-subset'",
            {"m": model_id},
        ).fetchall()
    # subsets FK: unknown hash has no triage.subsets row -> the insert into
    # evaluations still carries the stamp; num_labeled is 0 and value NULL
    assert rows and rows[0]["num_labeled"] == 0 and rows[0]["value"] is None


def test_validate_experiment_config_reports_subset_errors():
    result = validate_experiment_config(
        {
            "evaluation": {
                "metrics": ["precision@"],
                "thresholds": ["2_abs"],
                "subsets": [
                    {"name": "a", "query": "select entity_id from t"},  # no placeholder
                    {"name": "a", "query": "q '{as_of_date}'"},  # duplicate name
                    {"query": "q '{as_of_date}'"},  # no name
                ],
            }
        }
    )
    paths = {e["path"] for e in result["errors"]}
    assert "evaluation.subsets[0].query" in paths
    assert "evaluation.subsets[1].name" in paths
    assert "evaluation.subsets[2].name" in paths


def test_migration_0015_roundtrip(db_url, db_pool_greenfield):
    from triage.component.results_schema import downgrade_db, upgrade_db

    def _has_table(conn):
        return conn.execute(
            "select to_regclass('triage.subset_members') is not null as f"
        ).fetchone()["f"]

    def _ranks_arity(conn):
        return conn.execute(
            "select max(pronargs) as n from pg_proc p"
            " join pg_namespace ns on ns.oid = p.pronamespace"
            " where ns.nspname = 'triage' and p.proname = 'labeled_ranks'"
        ).fetchone()["n"]

    with db_pool_greenfield.connection() as conn:
        assert _has_table(conn)
        assert _ranks_arity(conn) == 5

    # explicit target, not "-1" (head keeps moving)
    downgrade_db(dburl=db_url, revision="0014_bias_completeness")
    with db_pool_greenfield.connection() as conn:
        assert not _has_table(conn)
        assert _ranks_arity(conn) == 4  # the 0011 shape is restored

    upgrade_db(dburl=db_url, revision="head")
    with db_pool_greenfield.connection() as conn:
        assert _has_table(conn)
        assert _ranks_arity(conn) == 5
