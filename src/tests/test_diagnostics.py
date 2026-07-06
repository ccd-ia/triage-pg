"""Postmodeling diagnostics tests — migration 0016 part 1 (plan P4).

``list_overlap`` is checked against a fully hand-computed two-model fixture: known
top-k intersection (Jaccard over the union of the two lists) and a known Spearman
rank correlation (Pearson over rank pairs). Train-duration recording is asserted at
the ``_insert_model_row`` seam (the full fit path is covered by the model-builder
suite, which now records a positive duration on every built model).
"""

from __future__ import annotations

import pytest

AS_OF = "2014-01-01"

# Model A ranks e1..e5 in order; model B swaps e2/e3.
#   A top-2 = {e1, e2}; B top-2 = {e1, e3}  ->  |A∩B| = 1, union = 3, jaccard = 1/3
#   ranks A = (1,2,3,4,5) vs B = (1,3,2,4,5) -> Pearson/Spearman corr = 0.9
A_SCORES = {1: 0.95, 2: 0.90, 3: 0.85, 4: 0.80, 5: 0.75}
B_SCORES = {1: 0.95, 3: 0.90, 2: 0.85, 4: 0.80, 5: 0.75}


def _seed_two_models(pool):
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " values ('m-art-a', 'm-log-a', 'model', '{}'::jsonb),"
            "        ('m-art-b', 'm-log-b', 'model', '{}'::jsonb)"
        )
        group_id = conn.execute(
            "insert into triage.model_groups"
            " (model_group_hash, model_type, hyperparameters, feature_list)"
            " values ('mg-diag', 'x.Y', '{}'::jsonb, ARRAY['f1'])"
            " returning model_group_id"
        ).fetchone()["model_group_id"]
        ids = {}
        for tag, scores in (("a", A_SCORES), ("b", B_SCORES)):
            model_id = conn.execute(
                "insert into triage.models (model_group_id, model_hash, train_end_time)"
                " values (%(g)s, %(a)s, date '2013-12-01') returning model_id",
                {"g": group_id, "a": f"m-art-{tag}"},
            ).fetchone()["model_id"]
            ids[tag] = model_id
            for eid, score in scores.items():
                conn.execute(
                    "insert into triage.predictions"
                    " (model_id, entity_id, as_of_date, split_kind, score)"
                    " values (%(m)s, %(e)s, %(d)s, 'test', %(s)s)",
                    {"m": model_id, "e": eid, "d": AS_OF, "s": score},
                )
    return ids


def test_list_overlap_jaccard_and_rank_corr(db_pool_greenfield):
    ids = _seed_two_models(db_pool_greenfield)
    with db_pool_greenfield.connection() as conn:
        rows = conn.execute(
            "select * from triage.list_overlap(%(a)s, %(b)s, '2_abs', 'test', null)",
            {"a": ids["a"], "b": ids["b"]},
        ).fetchall()
    assert len(rows) == 1  # one shared prediction date
    r = rows[0]
    assert r["k_a"] == 2 and r["k_b"] == 2
    assert r["n_intersection"] == 1  # only e1 is in both top-2 lists
    assert float(r["jaccard"]) == pytest.approx(1 / 3)
    assert float(r["rank_corr"]) == pytest.approx(0.9)


def test_list_overlap_identical_model_is_perfect(db_pool_greenfield):
    ids = _seed_two_models(db_pool_greenfield)
    with db_pool_greenfield.connection() as conn:
        r = conn.execute(
            "select * from triage.list_overlap(%(a)s, %(a)s, '2_abs', 'test', null)",
            {"a": ids["a"]},
        ).fetchone()
    assert r["n_intersection"] == 2
    assert float(r["jaccard"]) == pytest.approx(1.0)
    assert float(r["rank_corr"]) == pytest.approx(1.0)


def test_list_overlap_no_shared_dates_is_empty(db_pool_greenfield):
    ids = _seed_two_models(db_pool_greenfield)
    with db_pool_greenfield.connection() as conn:
        rows = conn.execute(
            "select * from triage.list_overlap(%(a)s, %(b)s, '2_abs', 'test',"
            " date '1999-01-01')",
            {"a": ids["a"], "b": ids["b"]},
        ).fetchall()
    assert rows == []


def test_insert_model_row_records_train_duration(db_pool_greenfield):
    from triage.adapters.model import _insert_model_row, as_uuid

    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " values ('m-art-dur', 'm-log-dur', 'model', '{}'::jsonb)"
        )
        group_id = conn.execute(
            "insert into triage.model_groups"
            " (model_group_hash, model_type, hyperparameters, feature_list)"
            " values ('mg-dur', 'x.Y', '{}'::jsonb, ARRAY['f1'])"
            " returning model_group_id"
        ).fetchone()["model_group_id"]
        run_id = conn.execute(
            "insert into triage.runs (profile, status) values ('local', 'started')"
            " returning run_id"
        ).fetchone()["run_id"]
        matrix_artifact_id = conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " values ('mx-art-dur', 'mx-log-dur', 'matrix', '{}'::jsonb)"
            " returning artifact_id"
        ).fetchone()["artifact_id"]
        conn.execute(
            # models.train_matrix_uuid FK: the adapter derives the uuid via as_uuid()
            "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind,"
            " storage_uri) values (%(u)s::uuid, %(a)s, 'train', 'file:///x')",
            {"u": str(as_uuid(matrix_artifact_id)), "a": matrix_artifact_id},
        )

    model_id = _insert_model_row(
        db_pool_greenfield,
        model_artifact_id="m-art-dur",
        model_group_id=group_id,
        run_id=str(run_id),
        train_matrix_artifact_id=matrix_artifact_id,
        train_end_time="2013-12-01",
        training_label_timespan="6 months",
        artifact_uri="file:///m",
        model_size_bytes=1,
        random_seed=0,
        train_duration_ms=1234,
    )
    with db_pool_greenfield.connection() as conn:
        row = conn.execute(
            "select train_duration_ms from triage.models where model_id = %(m)s",
            {"m": model_id},
        ).fetchone()
    assert row["train_duration_ms"] == 1234


# ------------------------------------------------------------- crosstabs / error tree
#
# Synthetic 20-entity matrix, one date, parameter '5_abs' (k=5 -> e1..e5 selected):
#   f_sep    = 1.0 exactly on the planted errors (FPs e3,e4,e5; FNs e6,e7), else 0.0
#            -> both error trees must recover the depth-1 rule "f_sep > 0.5" at rate 1.0
#   f_signal = 2.0 for selected, 1.0 for rest -> crosstab mean ratio == 2.0
ENTITIES = list(range(1, 21))
SELECTED = {1, 2, 3, 4, 5}
POSITIVE = {1, 2, 6, 7}  # e1,e2 caught; e6,e7 missed (FN); e3,e4,e5 wrong flags (FP)
ERRORY = {3, 4, 5, 6, 7}


def _seed_matrix_model(pool, tmp_path):
    import polars as pl

    from triage.adapters.model import as_uuid

    frame = pl.DataFrame(
        {
            "entity_id": ENTITIES,
            "as_of_date": [AS_OF] * len(ENTITIES),
            "f_sep": [1.0 if e in ERRORY else 0.0 for e in ENTITIES],
            "f_signal": [2.0 if e in SELECTED else 1.0 for e in ENTITIES],
        }
    ).with_columns(pl.col("as_of_date").str.to_date())
    uri = str(tmp_path / "diag-matrix.parquet")
    frame.write_parquet(uri)

    with pool.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " values ('m-art-x', 'm-log-x', 'model', '{}'::jsonb),"
            "        ('mx-art-x', 'mx-log-x', 'matrix', '{}'::jsonb),"
            "        ('lbl-x', 'lbl-log-x', 'labels', '{}'::jsonb)"
        )
        matrix_uuid = str(as_uuid("mx-art-x"))
        conn.execute(
            "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind,"
            " storage_uri, feature_names, label_timespan)"
            " values (%(u)s::uuid, 'mx-art-x', 'test', %(uri)s,"
            "         ARRAY['f_sep', 'f_signal'], interval '6 months')",
            {"u": matrix_uuid, "uri": uri},
        )
        group_id = conn.execute(
            "insert into triage.model_groups"
            " (model_group_hash, model_type, hyperparameters, feature_list)"
            " values ('mg-x', 'x.Y', '{}'::jsonb, ARRAY['f_sep', 'f_signal'])"
            " returning model_group_id"
        ).fetchone()["model_group_id"]
        model_id = conn.execute(
            "insert into triage.models (model_group_id, model_hash, train_end_time,"
            " training_label_timespan)"
            " values (%(g)s, 'm-art-x', date '2013-12-01', interval '6 months')"
            " returning model_id",
            {"g": group_id},
        ).fetchone()["model_id"]
        for i, eid in enumerate(ENTITIES):
            conn.execute(
                "insert into triage.predictions"
                " (model_id, entity_id, as_of_date, split_kind, score, matrix_uuid)"
                " values (%(m)s, %(e)s, %(d)s, 'test', %(s)s, %(u)s::uuid)",
                {
                    "m": model_id,
                    "e": eid,
                    "d": AS_OF,
                    "s": 1.0 - i * 0.01,
                    "u": matrix_uuid,
                },
            )
            conn.execute(
                "insert into triage.labels"
                " (label_hash, entity_id, as_of_date, label_timespan, outcome)"
                " values ('lbl-x', %(e)s, %(d)s, interval '6 months', %(o)s)",
                {"e": eid, "d": AS_OF, "o": 1.0 if eid in POSITIVE else 0.0},
            )
    return model_id


def test_crosstabs_stats_match_hand_computation(db_pool_greenfield, tmp_path):
    from triage.diagnostics import compute_crosstabs

    model_id = _seed_matrix_model(db_pool_greenfield, tmp_path)
    written = compute_crosstabs(db_pool_greenfield, model_id, parameter="5_abs")
    assert written == 2 * 3  # 2 features x 3 stats

    with db_pool_greenfield.connection() as conn:
        rows = conn.execute(
            "select feature, stat, selected_value, rest_value, ratio"
            " from triage.crosstabs where model_id = %(m)s",
            {"m": model_id},
        ).fetchall()
    by_key = {(r["feature"], r["stat"]): r for r in rows}
    sig = by_key[("f_signal", "mean")]
    assert sig["selected_value"] == pytest.approx(2.0)
    assert sig["rest_value"] == pytest.approx(1.0)
    assert sig["ratio"] == pytest.approx(2.0)
    sep = by_key[("f_sep", "mean")]
    assert sep["selected_value"] == pytest.approx(3 / 5)  # FPs e3,e4,e5 of 5 selected
    assert sep["rest_value"] == pytest.approx(2 / 15)  # FNs e6,e7 of 15 rest
    # idempotent re-run: same PK, refreshed values
    assert compute_crosstabs(db_pool_greenfield, model_id, parameter="5_abs") == 6


def test_error_tree_recovers_the_planted_rule(db_pool_greenfield, tmp_path):
    from triage.diagnostics import compute_error_analysis

    model_id = _seed_matrix_model(db_pool_greenfield, tmp_path)
    written = compute_error_analysis(
        db_pool_greenfield, model_id, parameter="5_abs", max_depth=2, min_samples_leaf=1
    )
    assert written > 0

    with db_pool_greenfield.connection() as conn:
        rows = conn.execute(
            "select error_kind, rule, n_matched, n_errors, error_rate"
            " from triage.error_analysis where model_id = %(m)s"
            " order by error_kind, error_rate desc",
            {"m": model_id},
        ).fetchall()
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["error_kind"], []).append(r)
    # fp tree: within the 5 selected, the 3 wrong flags all sit behind f_sep > 0.5
    top_fp = by_kind["fp"][0]
    assert "f_sep >" in top_fp["rule"]
    assert top_fp["n_matched"] == 3 and top_fp["n_errors"] == 3
    assert top_fp["error_rate"] == pytest.approx(1.0)
    # fn tree: among the 15 passed-over, the 2 missed positives sit behind the same split
    top_fn = by_kind["fn"][0]
    assert "f_sep >" in top_fn["rule"]
    assert top_fn["n_matched"] == 2 and top_fn["n_errors"] == 2
    assert top_fn["error_rate"] == pytest.approx(1.0)


def test_linear_contributions_persisted_for_selected(db_pool_greenfield, tmp_path):
    import joblib
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    from triage.diagnostics import compute_error_analysis

    model_id = _seed_matrix_model(db_pool_greenfield, tmp_path)
    est = LogisticRegression().fit(
        np.array([[0.0, 1.0], [1.0, 2.0], [0.0, 2.0], [1.0, 1.0]]), [0, 1, 1, 0]
    )
    uri = str(tmp_path / "est.joblib")
    joblib.dump(est, uri)
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "update triage.models set artifact_uri = %(u)s where model_id = %(m)s",
            {"u": uri, "m": model_id},
        )

    compute_error_analysis(
        db_pool_greenfield, model_id, parameter="5_abs", max_depth=2, min_samples_leaf=1
    )
    with db_pool_greenfield.connection() as conn:
        rows = conn.execute(
            "select entity_id, feature, feature_value, importance_score, method"
            " from triage.individual_importances where model_id = %(m)s",
            {"m": model_id},
        ).fetchall()
    # 5 selected entities x 2 features, method linear_contrib, score = beta * x
    assert len(rows) == 10
    assert {r["method"] for r in rows} == {"linear_contrib"}
    coef = {name: float(b) for name, b in zip(["f_sep", "f_signal"], est.coef_.ravel())}
    for r in rows:
        assert r["importance_score"] == pytest.approx(
            coef[r["feature"]] * r["feature_value"]
        )


def test_cli_postmodel_commands(db_url, db_pool_greenfield, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import triage.cli as cli

    model_id = _seed_matrix_model(db_pool_greenfield, tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    env = {"DATABASE_URL": db_url}

    result = runner.invoke(
        cli.app, ["postmodel", "crosstabs", str(model_id), "-p", "5_abs"], env=env
    )
    assert result.exit_code == 0, result.output
    assert "crosstab row(s) persisted" in result.output

    result = runner.invoke(
        cli.app,
        ["postmodel", "error-tree", str(model_id), "-p", "5_abs", "--min-leaf", "1"],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "error rule(s) persisted" in result.output
    assert "f_sep" in result.output

    result = runner.invoke(
        cli.app,
        ["postmodel", "compare", str(model_id), str(model_id), "-p", "5_abs"],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "1.000" in result.output  # self-comparison: jaccard 1.0


def test_migration_0016_roundtrip(db_url, db_pool_greenfield):
    from triage.component.results_schema import downgrade_db, upgrade_db

    def _has(conn):
        return conn.execute(
            "select to_regprocedure("
            "'triage.list_overlap(bigint, bigint, text, triage.split_kind, date)')"
            " is not null as f"
        ).fetchone()["f"]

    with db_pool_greenfield.connection() as conn:
        assert _has(conn)
    downgrade_db(dburl=db_url, revision="0015_subset_evaluation")
    with db_pool_greenfield.connection() as conn:
        assert not _has(conn)
    upgrade_db(dburl=db_url, revision="head")
    with db_pool_greenfield.connection() as conn:
        assert _has(conn)
