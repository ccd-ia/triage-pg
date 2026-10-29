"""Postmodeling diagnostics tests — migration 0016 part 1 (plan P4).

``list_overlap`` is checked against a fully hand-computed two-model fixture: known
top-k intersection (Jaccard over the union of the two lists) and a known Spearman
rank correlation (Pearson over rank pairs). Train-duration recording is asserted at
the ``_insert_model_row`` seam (the full fit path is covered by the model-builder
suite, which now records a positive duration on every built model).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any

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
        feature_list=["f1"],
        train_duration_ms=1234,
    )
    with db_pool_greenfield.connection() as conn:
        row = conn.execute(
            "select train_duration_ms, feature_list from triage.models"
            " where model_id = %(m)s",
            {"m": model_id},
        ).fetchone()
    assert row["train_duration_ms"] == 1234
    # The fit geometry is recorded on the row, not re-derived at score time (#14, 0021).
    assert list(row["feature_list"]) == ["f1"]


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


def _new_group(pool, tag: str, features: list[str]) -> int:
    with pool.connection() as conn:
        return conn.execute(
            "insert into triage.model_groups"
            " (model_group_hash, model_type, hyperparameters, feature_list)"
            " values (%(h)s, 'x.Y', '{}'::jsonb, %(f)s)"
            " returning model_group_id",
            {"h": f"mg-{tag}", "f": features},
        ).fetchone()["model_group_id"]


def _seed_scored_model(
    pool,
    tmp_path,
    *,
    tag: str,
    columns: dict[str, list[float]],
    scores: list[float],
    outcomes: Mapping[int, float | None],
    as_of: str = AS_OF,
    group_id: int | None = None,
) -> int:
    """One scored test matrix: entities 1..N with ``columns`` as features, one date.

    ``outcomes[e] = None`` leaves entity ``e`` unlabeled. Artifact ids derive from ``tag``,
    so several models can be seeded side by side (in one group when ``group_id`` is given).
    """
    import polars as pl

    from triage.adapters.model import as_uuid

    entities = list(range(1, len(scores) + 1))
    features = list(columns)
    frame = pl.DataFrame(
        {"entity_id": entities, "as_of_date": [as_of] * len(entities), **columns}
    ).with_columns(pl.col("as_of_date").str.to_date())
    uri = str(tmp_path / f"diag-matrix-{tag}.parquet")
    frame.write_parquet(uri)

    if group_id is None:
        group_id = _new_group(pool, tag, features)
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " values (%(m)s, %(m)s || '-log', 'model', '{}'::jsonb),"
            "        (%(mx)s, %(mx)s || '-log', 'matrix', '{}'::jsonb),"
            "        (%(l)s, %(l)s || '-log', 'labels', '{}'::jsonb)",
            {"m": f"m-art-{tag}", "mx": f"mx-art-{tag}", "l": f"lbl-{tag}"},
        )
        matrix_uuid = str(as_uuid(f"mx-art-{tag}"))
        conn.execute(
            "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind,"
            " storage_uri, feature_names, label_timespan)"
            " values (%(u)s::uuid, %(a)s, 'test', %(uri)s, %(f)s, interval '6 months')",
            {"u": matrix_uuid, "a": f"mx-art-{tag}", "uri": uri, "f": features},
        )
        model_id = conn.execute(
            "insert into triage.models (model_group_id, model_hash, train_end_time,"
            " training_label_timespan)"
            " values (%(g)s, %(h)s, cast(%(d)s as date) - 31, interval '6 months')"
            " returning model_id",
            {"g": group_id, "h": f"m-art-{tag}", "d": as_of},
        ).fetchone()["model_id"]
        for eid, score in zip(entities, scores):
            conn.execute(
                "insert into triage.predictions"
                " (model_id, entity_id, as_of_date, split_kind, score, matrix_uuid)"
                " values (%(m)s, %(e)s, %(d)s, 'test', %(s)s, %(u)s::uuid)",
                {"m": model_id, "e": eid, "d": as_of, "s": score, "u": matrix_uuid},
            )
            if outcomes.get(eid) is None:
                continue
            conn.execute(
                "insert into triage.labels"
                " (label_hash, entity_id, as_of_date, label_timespan, outcome)"
                " values (%(l)s, %(e)s, %(d)s, interval '6 months', %(o)s)",
                {"l": f"lbl-{tag}", "e": eid, "d": as_of, "o": outcomes[eid]},
            )
    return model_id


def _seed_matrix_model(
    pool,
    tmp_path,
    unlabeled: Collection[int] = (),
    *,
    tag: str = "x",
    as_of: str = AS_OF,
    group_id: int | None = None,
) -> int:
    return _seed_scored_model(
        pool,
        tmp_path,
        tag=tag,
        as_of=as_of,
        group_id=group_id,
        columns={
            "f_sep": [1.0 if e in ERRORY else 0.0 for e in ENTITIES],
            "f_signal": [2.0 if e in SELECTED else 1.0 for e in ENTITIES],
        },
        scores=[1.0 - i * 0.01 for i in range(len(ENTITIES))],
        outcomes={
            e: None if e in unlabeled else (1.0 if e in POSITIVE else 0.0)
            for e in ENTITIES
        },
    )


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


def test_error_tree_cuts_at_the_population_rank(db_pool_greenfield, tmp_path):
    """#21: e1 has no label but is still the best-scored row, so the 5_abs cut is e1..e5 and
    the labeled rows inside it are e2..e5. A rank among labeled rows only would pull e6 — a
    missed positive — into the selection, diluting the fp rule and hiding one fn."""
    from triage.diagnostics import compute_error_analysis

    model_id = _seed_matrix_model(db_pool_greenfield, tmp_path, unlabeled={1})
    compute_error_analysis(
        db_pool_greenfield, model_id, parameter="5_abs", max_depth=2, min_samples_leaf=1
    )
    with db_pool_greenfield.connection() as conn:
        rows = conn.execute(
            "select error_kind, n_matched, n_errors, error_rate"
            " from triage.error_analysis where model_id = %(m)s"
            " order by error_kind, error_rate desc",
            {"m": model_id},
        ).fetchall()
    fp = [r for r in rows if r["error_kind"] == "fp"]
    fn = [r for r in rows if r["error_kind"] == "fn"]
    assert sum(r["n_matched"] for r in fp) == 4  # e2..e5
    assert (fp[0]["n_matched"], fp[0]["n_errors"]) == (3, 3)
    assert (fn[0]["n_matched"], fn[0]["n_errors"]) == (2, 2)  # e6 and e7


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


# ------------------------------------------- confusion tree / suspicious leaves / recurrence
#
# The planted perfect segment: 200 entities, one date, parameter '100_abs'.
#   group G = e1..e100 (f_group = 1); inside G, f_leak equals the outcome (the leak); f_leak
#   is 0 everywhere outside G.
#   ranks 1..50   G positives      (selected, TP)
#   ranks 51..100 e101..e150       (selected; e101..e125 positive, e126..e150 negative)
#   ranks 101..150 G negatives     (passed over, TN)
#   ranks 151..200 e151..e200      (passed over; e151..e160 positive, rest negative)
# precision@100_abs = 75/100. Inside the selection the 50 G positives form a leaf with no
# error, and the confusion tree isolates them as a pure-TP leaf: both are suspiciously right.
PLANTED_N = 200
PLANTED_PRECISION = 0.75


def _seed_planted_segment(pool, tmp_path) -> int:
    in_group = [1.0 if e <= 100 else 0.0 for e in range(1, PLANTED_N + 1)]
    outcome = {
        e: 1.0 if e <= 50 or 101 <= e <= 125 or 151 <= e <= 160 else 0.0
        for e in range(1, PLANTED_N + 1)
    }
    rank_order = [
        *range(1, 51),
        *range(101, 151),
        *range(51, 101),
        *range(151, 201),
    ]
    rank_of = {e: r for r, e in enumerate(rank_order, start=1)}
    model_id = _seed_scored_model(
        pool,
        tmp_path,
        tag="planted",
        columns={
            "f_group": in_group,
            "f_leak": [
                outcome[e] if in_group[e - 1] else 0.0 for e in range(1, PLANTED_N + 1)
            ],
        },
        scores=[1.0 - rank_of[e] * 0.001 for e in range(1, PLANTED_N + 1)],
        outcomes=outcome,
    )
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.evaluations (model_id, split_kind, as_of_date, metric,"
            " parameter, value) values (%(m)s, 'test', %(d)s, 'precision@', '100_abs',"
            " %(v)s)",
            {"m": model_id, "d": AS_OF, "v": PLANTED_PRECISION},
        )
    return model_id


def _error_rows(pool, model_id: int) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        return conn.execute(
            "select error_kind, rule, n_matched, n_errors, error_rate, class_counts,"
            "       conditions, signature"
            " from triage.error_analysis where model_id = %(m)s"
            " order by error_kind, rule_id",
            {"m": model_id},
        ).fetchall()


def test_confusion_tree_class_counts_sum_to_support(db_pool_greenfield, tmp_path):
    from triage.diagnostics import compute_error_analysis
    from triage.diagnostics.error_tree import signature_of

    model_id = _seed_matrix_model(db_pool_greenfield, tmp_path)
    compute_error_analysis(
        db_pool_greenfield,
        model_id,
        parameter="5_abs",
        kind="all",
        max_depth=2,
        min_samples_leaf=1,
    )
    rows = _error_rows(db_pool_greenfield, model_id)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_kind.setdefault(r["error_kind"], []).append(r)
    assert set(by_kind) == {"fp", "fn", "confusion"}

    keys = {
        "fp": {"tp", "fp"},
        "fn": {"fn", "tn"},
        "confusion": {"tp", "fp", "fn", "tn"},
    }
    for r in rows:
        counts = r["class_counts"]
        assert set(counts) == keys[r["error_kind"]]
        assert sum(counts.values()) == r["n_matched"]
        assert r["signature"] == signature_of(r["conditions"])
        assert r["rule"] == " AND ".join(
            f"{c['feature']} {c['op']} {c['threshold']:.4g}" for c in r["conditions"]
        )
    for r in by_kind["confusion"]:
        assert r["n_errors"] == r["class_counts"]["fp"] + r["class_counts"]["fn"]

    def totals(kind: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in by_kind[kind]:
            for c, n in r["class_counts"].items():
                out[c] = out.get(c, 0) + n
        return out

    # e1,e2 TP; e3..e5 FP; e6,e7 FN; the other 13 TN — every labeled row, once
    assert totals("confusion") == {"tp": 2, "fp": 3, "fn": 2, "tn": 13}
    assert totals("fp") == {"tp": 2, "fp": 3}
    assert totals("fn") == {"fn": 2, "tn": 13}


def test_error_tree_kind_both_keeps_its_meaning(db_pool_greenfield, tmp_path):
    from triage.diagnostics import compute_error_analysis

    model_id = _seed_matrix_model(db_pool_greenfield, tmp_path)
    compute_error_analysis(
        db_pool_greenfield, model_id, parameter="5_abs", min_samples_leaf=1
    )
    kinds = {r["error_kind"] for r in _error_rows(db_pool_greenfield, model_id)}
    assert kinds == {"fp", "fn"}
    with pytest.raises(ValueError, match=re.escape("fp | fn | confusion | both | all")):
        compute_error_analysis(db_pool_greenfield, model_id, kind="tp")


def test_signature_does_not_depend_on_path_order():
    """Two trees that split the same two features in opposite order give the conjunction
    the same signature; thresholds and a repeated split do not enter it."""
    import numpy as np
    from sklearn.tree import DecisionTreeClassifier

    from triage.diagnostics.error_tree import _rules_from_tree, signature_of

    def data(first: int) -> tuple[np.ndarray, list[int]]:
        # target = a AND b; the feature at index ``first`` is the better first split
        rows = [(0, 0, 0)] * 10 + [(0, 1, 0)] * 10 + [(1, 0, 0)] * 2 + [(1, 1, 1)] * 8
        x = np.array([(r[0], r[1]) for r in rows], dtype=float)
        if first == 1:
            x = x[:, ::-1]
        return x, [r[2] for r in rows]

    signatures, orders = [], []
    for first in (0, 1):
        x, y = data(first)
        tree = DecisionTreeClassifier(max_depth=2, random_state=0).fit(x, y)
        leaf = next(
            r for r in _rules_from_tree(tree, ["a", "b"], "fn") if r["n_errors"] == 8
        )
        signatures.append(leaf["signature"])
        orders.append([c["feature"] for c in leaf["conditions"]])
    assert orders == [["a", "b"], ["b", "a"]], "the two trees split in opposite order"
    assert signatures == ["a>|b>", "a>|b>"]

    path = [
        {"feature": "inspections_1y", "op": "<=", "threshold": 2.5},
        {"feature": "facility_age", "op": ">", "threshold": 10.0},
    ]
    moved = [{**c, "threshold": c["threshold"] * 3} for c in path]
    assert signature_of(path) == signature_of(moved) == "facility_age>|inspections_1y<="
    twice = [*path, {"feature": "inspections_1y", "op": "<=", "threshold": 1.0}]
    assert signature_of(twice) == signature_of(path)
    assert signature_of([]) == ""


def test_planted_perfect_segment_is_suspiciously_right(db_pool_greenfield, tmp_path):
    from triage.diagnostics import compute_error_analysis

    model_id = _seed_planted_segment(db_pool_greenfield, tmp_path)
    compute_error_analysis(
        db_pool_greenfield, model_id, parameter="100_abs", kind="all"
    )
    with db_pool_greenfield.connection() as conn:
        rows = conn.execute(
            "select * from triage.error_rules_suspicious(%(m)s)", {"m": model_id}
        ).fetchall()
    by_kind = {r["error_kind"]: r for r in rows}
    assert set(by_kind) == {"fp", "confusion"}, rows

    fp = by_kind["fp"]
    assert (fp["n_matched"], fp["n_errors"]) == (50, 0)
    assert fp["signature"] in {"f_leak>", "f_group>"}  # identical inside the selection
    assert fp["precision_at_k"] == pytest.approx(PLANTED_PRECISION)
    assert fp["lift"] == pytest.approx(1 / PLANTED_PRECISION, abs=1e-12)

    confusion = by_kind["confusion"]
    assert confusion["n_matched"] == 50
    assert confusion["class_counts"] == {"tp": 50, "fp": 0, "fn": 0, "tn": 0}
    assert confusion["tp_share"] == pytest.approx(1.0)
    assert "f_leak>" in confusion["signature"]


def test_a_rule_on_every_date_recurs_on_every_date(db_pool_greenfield, tmp_path):
    from triage.diagnostics import compute_error_analysis

    group_id = _new_group(db_pool_greenfield, "recur", ["f_sep", "f_signal"])
    for i, as_of in enumerate(("2014-01-01", "2014-07-01", "2015-01-01")):
        model_id = _seed_matrix_model(
            db_pool_greenfield, tmp_path, tag=f"r{i}", as_of=as_of, group_id=group_id
        )
        compute_error_analysis(
            db_pool_greenfield,
            model_id,
            parameter="5_abs",
            kind="fp",
            max_depth=2,
            min_samples_leaf=1,
        )
    with db_pool_greenfield.connection() as conn:
        rows = conn.execute(
            "select * from triage.error_rule_recurrence(%(g)s, '5_abs', 'fp')",
            {"g": group_id},
        ).fetchall()
    top = rows[0]
    assert top["signature"] == "f_sep>"  # the planted wrong flags, on every date
    assert (top["n_dates"], top["n_dates_total"]) == (3, 3)
    assert top["mean_error_rate"] == pytest.approx(1.0)
    assert top["mean_support"] == pytest.approx(3.0)
    assert top["example_rule"].startswith("f_sep > ")


def test_cli_error_rules_and_recurrence(
    db_url, db_pool_greenfield, tmp_path, monkeypatch
):
    from typer.testing import CliRunner

    import triage.cli as cli

    model_id = _seed_planted_segment(db_pool_greenfield, tmp_path)
    with db_pool_greenfield.connection() as conn:
        group_id = conn.execute(
            "select model_group_id from triage.models where model_id = %(m)s",
            {"m": model_id},
        ).fetchone()["model_group_id"]
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    env = {"DATABASE_URL": db_url}

    result = runner.invoke(
        cli.app, ["postmodel", "error-rules", str(model_id), "--suspicious"], env=env
    )
    assert result.exit_code == 0, result.output
    assert "no error rules persisted" in result.output

    result = runner.invoke(
        cli.app,
        ["postmodel", "error-tree", str(model_id), "--kind", "all"],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "confusion" in result.output

    result = runner.invoke(
        cli.app, ["postmodel", "error-rules", str(model_id), "--suspicious"], env=env
    )
    assert result.exit_code == 0, result.output
    assert "Suspiciously right" in result.output
    assert "1.33" in result.output  # lift = 1 / precision@100_abs = 1 / 0.75

    # the thresholds are options: no leaf has 60 rows, so nothing qualifies
    result = runner.invoke(
        cli.app,
        [
            "postmodel",
            "error-rules",
            str(model_id),
            "--suspicious",
            "--min-support",
            "60",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "No suspiciously-right leaves" in result.output

    result = runner.invoke(
        cli.app,
        ["postmodel", "error-rules", str(model_id), "--kind", "confusion"],
        env=env,
    )
    assert result.exit_code == 0, result.output
    body = result.output.split("Kind", 1)[1]  # the table rows, below the header
    assert " confusion " in body
    assert " fp " not in body and " fn " not in body

    result = runner.invoke(
        cli.app, ["postmodel", "error-recurrence", str(group_id)], env=env
    )
    assert result.exit_code == 0, result.output
    assert "fp leaves" in result.output and "1/1" in result.output

    result = runner.invoke(
        cli.app,
        ["postmodel", "error-tree", str(model_id), "--kind", "tp"],
        env=env,
    )
    assert result.exit_code == 2
    # rich wraps the error box, and colours it on CI (it forces a terminal under
    # GITHUB_ACTIONS), so drop the ANSI codes and the box before matching.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output).replace("│", " ")
    assert "fp | fn | confusion | both | all" in " ".join(plain.split())


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
