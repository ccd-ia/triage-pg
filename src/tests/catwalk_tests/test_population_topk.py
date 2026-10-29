"""Threshold metrics rank the full population (#21, migration 0024, ADR-0033).

DSSG triage ranked every scored test row, cut at k, and only then dropped the rows with no
label. These tests hold the in-Postgres functions to that definition with a reference copied
from DSSG's catwalk (``generate_binary_at_x`` + ``_filter_nan_labels``, ``dssg/master``), over
fixtures where a third of the rows have no label and score ties straddle the cut. The one
deliberate difference is ``N_pct`` rounding: triage-pg keeps ``ceil``, DSSG used ``int()``.

Also here: the random-ranking spread checked against an exact enumeration, the tie bounds
checked against every ordering of the tie block, and 0024's round trip.
"""

from __future__ import annotations

import itertools
import math
import uuid
from collections.abc import Sequence

import numpy as np
import pytest
from scipy.stats import hypergeom
from sklearn import metrics as skmetrics

from triage.component.catwalk.in_pg_evaluation import compute_bias_in_db, evaluate_in_db
from triage.component.results_schema import downgrade_db, upgrade_db

LABEL_TIMESPAN = "6 months"
AS_OF_DATE = "2014-01-01"
THRESHOLDS = [
    "1_abs",
    "3_abs",
    "5_abs",
    "7_abs",
    "10_pct",
    "35_pct",
    "50_pct",
    "100_pct",
]

# 15 entities: 5 unlabeled (None), score ties at 0.7 and 0.4 that straddle several cuts.
# entity: 1    2    3    4    5    6    7    8    9    10   11   12   13   14   15
SCORES = [0.9, 0.8, 0.8, 0.7, 0.7, 0.7, 0.7, 0.6, 0.5, 0.4, 0.4, 0.4, 0.3, 0.2, 0.1]
LABELS = [None, 1, 0, None, 1, 0, None, 1, 0, None, 1, 1, None, 0, 1]
ENTITY_IDS = list(range(1, 16))


def dssg_reference(
    scores: Sequence[float],
    labels: Sequence[int | None],
    entity_ids: Sequence[int],
    parameter: str,
) -> dict[str, float | int | None]:
    """DSSG catwalk's threshold evaluation, with triage-pg's deterministic tiebreak and ceil.

    Rank every row (score desc, entity_id asc), mark the top k, drop rows whose label is NaN,
    then score the remainder with sklearn — exactly ``_compute_evaluations``.
    """
    n = len(scores)
    raw, unit = parameter.split("_")
    k = math.ceil(float(raw) / 100 * n) if unit == "pct" else int(raw)
    k = max(0, min(k, n))
    order = sorted(range(n), key=lambda i: (-scores[i], entity_ids[i]))
    predicted = np.zeros(n, dtype=int)
    predicted[order[:k]] = 1
    y = np.array([np.nan if label is None else label for label in labels], dtype=float)
    labeled = np.isfinite(y)  # catwalk's _filter_nan_labels
    y_pred, y_true = predicted[labeled], y[labeled]
    m = int(y_pred.sum())
    return {
        "k": k,
        "num_labeled_above_threshold": m,
        "precision": None if m == 0 else skmetrics.precision_score(y_true, y_pred),
        "recall": skmetrics.recall_score(y_true, y_pred),
        "num_labeled": int(labeled.sum()),
        "num_positive": int((y_true > 0).sum()),
    }


def _seed(pool, scores, labels, entity_ids, *, matrix=True) -> int:
    """A model with one test prediction per entity and a label for the labeled ones.

    With ``matrix``, the predictions carry a matrix whose labels parent is the labels artifact
    (the lineage evaluation pins on, and 0024's recompute reads label_timespan from).
    """
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) values"
            " ('model-art', 'model-logical', 'model', '{}'::jsonb),"
            " ('labels-art', 'labels-logical', 'labels', '{}'::jsonb),"
            " ('matrix-art', 'matrix-logical', 'matrix', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.artifact_inputs (artifact_id, parent_id)"
            " values ('matrix-art', 'labels-art')"
        )
        matrix_uuid = str(uuid.uuid4())
        conn.execute(
            "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind, storage_uri,"
            " label_timespan) values (%(u)s, 'matrix-art', 'test', 'mem://m',"
            " cast(%(ts)s as interval))",
            {"u": matrix_uuid, "ts": LABEL_TIMESPAN},
        )
        conn.execute(
            "insert into triage.model_groups"
            " (model_group_hash, model_type, hyperparameters, feature_list)"
            " values ('mg', 'sklearn.tree.DecisionTreeClassifier', '{}'::jsonb, ARRAY['f'])"
        )
        model_id = conn.execute(
            "insert into triage.models (model_group_id, model_hash, train_end_time)"
            " select model_group_id, 'model-art', date '2013-07-01'"
            " from triage.model_groups where model_group_hash = 'mg' returning model_id"
        ).fetchone()["model_id"]
        for eid, score, label in zip(entity_ids, scores, labels, strict=True):
            conn.execute(
                "insert into triage.predictions"
                " (model_id, entity_id, as_of_date, split_kind, score, matrix_uuid)"
                " values (%(m)s, %(e)s, %(d)s, 'test', %(s)s, %(u)s)",
                {
                    "m": model_id,
                    "e": eid,
                    "d": AS_OF_DATE,
                    "s": score,
                    "u": matrix_uuid if matrix else None,
                },
            )
            if label is not None:
                conn.execute(
                    "insert into triage.labels"
                    " (label_hash, entity_id, as_of_date, label_timespan, outcome)"
                    " values ('labels-art', %(e)s, %(d)s, cast(%(ts)s as interval), %(o)s)",
                    {
                        "e": eid,
                        "d": AS_OF_DATE,
                        "ts": LABEL_TIMESPAN,
                        "o": float(label),
                    },
                )
    return int(model_id)


def _evaluate(pool, model_id, thresholds=THRESHOLDS, subset_hash="") -> None:
    evaluate_in_db(
        pool,
        model_id,
        AS_OF_DATE,
        LABEL_TIMESPAN,
        metric_config={"metrics": ["precision@", "recall@"], "thresholds": thresholds},
        subset_hash=subset_hash,
    )


def _row(pool, model_id, metric, parameter, subset_hash=""):
    with pool.connection() as conn:
        return conn.execute(
            "select value, value_worst, value_best, value_expected, value_std,"
            " num_labeled, num_positive, num_labeled_above_threshold"
            " from triage.evaluations where model_id = %(m)s and metric = %(metric)s"
            " and parameter = %(p)s and subset_hash = %(s)s",
            {"m": model_id, "metric": metric, "p": parameter, "s": subset_hash},
        ).fetchone()


@pytest.mark.parametrize("parameter", THRESHOLDS)
def test_threshold_metrics_match_dssg(db_pool_greenfield, parameter):
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _evaluate(pool, model_id, [parameter])
    expected = dssg_reference(SCORES, LABELS, ENTITY_IDS, parameter)

    precision = _row(pool, model_id, "precision@", parameter)
    recall = _row(pool, model_id, "recall@", parameter)

    if expected["precision"] is None:
        assert precision["value"] is None
    else:
        assert precision["value"] == pytest.approx(expected["precision"], abs=1e-12)
    assert recall["value"] == pytest.approx(expected["recall"], abs=1e-12)
    for row in (precision, recall):
        assert (
            row["num_labeled_above_threshold"]
            == expected["num_labeled_above_threshold"]
        )
        assert row["num_labeled"] == expected["num_labeled"]
        assert row["num_positive"] == expected["num_positive"]


def test_the_cut_counts_unlabeled_rows(db_pool_greenfield):
    """The case the old definition got wrong: 5_abs takes the 5 best-scored rows, of which
    two have no label — precision is over the 3 labeled ones, not over 5 labeled rows."""
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _evaluate(pool, model_id, ["5_abs"])
    row = _row(pool, model_id, "precision@", "5_abs")
    # top 5 by (score desc, id): 1(None) 2(1) 3(0) 4(None) 5(1)
    assert row["num_labeled_above_threshold"] == 3
    assert row["value"] == pytest.approx(2 / 3)


def test_precision_is_null_when_the_cut_holds_no_label(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _evaluate(pool, model_id, ["1_abs"])  # entity 1 has no label
    row = _row(pool, model_id, "precision@", "1_abs")
    assert row["value"] is None
    assert row["num_labeled_above_threshold"] == 0
    assert _row(pool, model_id, "recall@", "1_abs")["value"] == 0


def test_a_subset_ranks_its_own_members_unlabeled_included(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    members = [1, 3, 4, 6, 9, 10, 11, 14]
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.subsets (subset_hash, config) values ('s1', '{}'::jsonb)"
        )
        for eid in members:
            conn.execute(
                "insert into triage.subset_members (subset_hash, entity_id, as_of_date)"
                " values ('s1', %(e)s, %(d)s)",
                {"e": eid, "d": AS_OF_DATE},
            )
    _evaluate(pool, model_id, ["3_abs", "50_pct"], subset_hash="s1")
    idx = [ENTITY_IDS.index(e) for e in members]
    for parameter in ("3_abs", "50_pct"):
        expected = dssg_reference(
            [SCORES[i] for i in idx], [LABELS[i] for i in idx], members, parameter
        )
        row = _row(pool, model_id, "precision@", parameter, "s1")
        assert (
            row["num_labeled_above_threshold"]
            == expected["num_labeled_above_threshold"]
        )
        assert row["value"] == pytest.approx(expected["precision"], abs=1e-12)


# ------------------------------------------------------------ random-ranking spread


def _enumerated_precision_std(n: int, n_labeled: int, n_positive: int, k: int) -> float:
    """Exact std of TP/m over every equally likely top-k set, given m >= 1 — no formula."""
    kinds = [1] * n_positive + [0] * (n_labeled - n_positive) + [None] * (n - n_labeled)
    values = []
    for chosen in itertools.combinations(range(n), k):
        picked = [v for i in chosen if (v := kinds[i]) is not None]
        if picked:
            values.append(sum(picked) / len(picked))
    return float(np.std(values))


def _hypergeom_precision_std(n: int, n_labeled: int, n_positive: int, k: int) -> float:
    b = n_positive / n_labeled
    ms = np.arange(max(1, k - (n - n_labeled)), min(k, n_labeled) + 1)
    w = hypergeom.pmf(ms, n, n_labeled, k)
    f = b * (1 - b) * (n_labeled - ms) / ((n_labeled - 1) * ms)
    return float(math.sqrt((w * f).sum() / w.sum()))


@pytest.mark.parametrize(
    ("n", "n_labeled", "n_positive", "k"),
    [(10, 6, 3, 4), (12, 5, 2, 7), (9, 9, 4, 3), (8, 3, 1, 6), (11, 7, 5, 1)],
)
def test_precision_random_std_is_the_exact_spread(
    db_pool_greenfield, n, n_labeled, n_positive, k
):
    with db_pool_greenfield.connection() as conn:
        sql_std = conn.execute(
            "select triage.precision_random_std(%(n)s, %(l)s, %(p)s, %(k)s) as s",
            {"n": n, "l": n_labeled, "p": n_positive, "k": k},
        ).fetchone()["s"]
    assert sql_std == pytest.approx(
        _enumerated_precision_std(n, n_labeled, n_positive, k), abs=1e-12
    )


@pytest.mark.parametrize(
    ("n", "n_labeled", "n_positive", "k"),
    [(13_000, 5_600, 1_550, 1_300), (2_000, 1_999, 30, 1_000), (500, 40, 39, 450)],
)
def test_precision_random_std_at_scale_matches_scipy(
    db_pool_greenfield, n, n_labeled, n_positive, k
):
    with db_pool_greenfield.connection() as conn:
        sql_std = conn.execute(
            "select triage.precision_random_std(%(n)s, %(l)s, %(p)s, %(k)s) as s",
            {"n": n, "l": n_labeled, "p": n_positive, "k": k},
        ).fetchone()["s"]
    assert sql_std == pytest.approx(
        _hypergeom_precision_std(n, n_labeled, n_positive, k), rel=1e-9
    )


def test_precision_random_std_reduces_to_the_old_closed_form_when_all_labeled(
    db_pool_greenfield,
):
    n, n_positive, k = 10, 5, 5
    b = n_positive / n
    old = math.sqrt(k * b * (1 - b) * (n - k) / (n - 1)) / k
    with db_pool_greenfield.connection() as conn:
        sql_std = conn.execute(
            "select triage.precision_random_std(10, 10, 5, 5) as s"
        ).fetchone()["s"]
    assert sql_std == pytest.approx(old, abs=1e-12)


# ------------------------------------------------------------------ tie bounds


def _orderings_extremes(scores, labels, k):
    """Min and max precision/recall@k over every ordering of the rows tied at the cutoff."""
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    cutoff = scores[order[k - 1]]
    fixed = [i for i in order if scores[i] > cutoff]
    tie = [i for i in order if scores[i] == cutoff]
    n_positive = sum(1 for label in labels if label)
    precisions, recalls = [], []
    for perm in itertools.permutations(tie):
        top = fixed + list(perm[: k - len(fixed)])
        picked = [v for i in top if (v := labels[i]) is not None]
        if picked:
            precisions.append(sum(picked) / len(picked))
        recalls.append(sum(picked) / n_positive)
    return min(precisions), max(precisions), min(recalls), max(recalls)


@pytest.mark.parametrize("parameter", ["5_abs", "6_abs", "11_abs"])
def test_tie_bounds_are_the_extremes_over_every_ordering(db_pool_greenfield, parameter):
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _evaluate(pool, model_id, [parameter])
    k = int(parameter.split("_")[0])
    p_lo, p_hi, r_lo, r_hi = _orderings_extremes(SCORES, LABELS, k)
    precision = _row(pool, model_id, "precision@", parameter)
    recall = _row(pool, model_id, "recall@", parameter)
    assert precision["value_worst"] == pytest.approx(p_lo, abs=1e-12)
    assert precision["value_best"] == pytest.approx(p_hi, abs=1e-12)
    assert recall["value_worst"] == pytest.approx(r_lo, abs=1e-12)
    assert recall["value_best"] == pytest.approx(r_hi, abs=1e-12)


# ------------------------------------------------------------------ migration


def test_migration_0024_round_trips(db_pool_greenfield, db_url):
    """Down to 0023: labeled-only ranking again, no new column, pinball intact. Up again:
    population ranking."""
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)

    downgrade_db(dburl=db_url, revision="0023_probability_metrics")
    _evaluate(pool, model_id, ["5_abs"])
    with pool.connection() as conn:
        old = conn.execute(
            "select value from triage.evaluations where metric = 'precision@'"
        ).fetchone()["value"]
        columns = {
            r["column_name"]
            for r in conn.execute(
                "select column_name from information_schema.columns"
                " where table_schema = 'triage' and table_name = 'evaluations'"
            ).fetchall()
        }
        pinball = conn.execute(
            "select (triage.regression_metric(%(m)s, 'test', %(d)s,"
            " cast(%(ts)s as interval), 'pinball@0.5')).value as v",
            {"m": model_id, "d": AS_OF_DATE, "ts": LABEL_TIMESPAN},
        ).fetchone()["v"]
    labeled = [(s, label) for s, label in zip(SCORES, LABELS) if label is not None]
    assert old == pytest.approx(sum(label for _, label in labeled[:5]) / 5)
    assert "num_labeled_above_threshold" not in columns
    assert pinball is not None

    upgrade_db(dburl=db_url, revision="head")
    # 0024 recomputed the row written under 0023
    row = _row(pool, model_id, "precision@", "5_abs")
    assert row["value"] == pytest.approx(2 / 3)
    assert row["num_labeled_above_threshold"] == 3


# ------------------------------------------------------- bias, curve, recompute

# Two groups over the 15 entities; group "b" holds three of the five unlabeled rows.
GROUPS = ["a", "a", "b", "b", "a", "b", "b", "a", "a", "b", "a", "b", "a", "b", "a"]


def _seed_groups(pool) -> None:
    with pool.connection() as conn:
        for eid, group in zip(ENTITY_IDS, GROUPS, strict=True):
            conn.execute(
                "insert into triage.protected_groups"
                " (entity_id, as_of_date, attribute_name, attribute_value)"
                " values (%(e)s, %(d)s, 'g', %(v)s)",
                {"e": eid, "d": AS_OF_DATE, "v": group},
            )


def _bias(pool, model_id) -> dict[tuple[str, str], float | None]:
    with pool.connection() as conn:
        rows = conn.execute(
            "select attribute_value, metric, value from triage.bias_metrics"
            " where model_id = %(m)s",
            {"m": model_id},
        ).fetchall()
    return {(r["attribute_value"], r["metric"]): r["value"] for r in rows}


def _expected_bias(group: str, k: int) -> dict[str, float]:
    order = sorted(range(len(SCORES)), key=lambda i: (-SCORES[i], ENTITY_IDS[i]))
    selected = set(order[:k])
    rows = [i for i in range(len(SCORES)) if GROUPS[i] == group]
    sel = [i for i in rows if i in selected]
    lab = [i for i in rows if LABELS[i] is not None]
    tp = sum(1 for i in sel if LABELS[i] == 1)
    fp = sum(1 for i in sel if LABELS[i] == 0)
    fn = sum(1 for i in lab if i not in selected and LABELS[i] == 1)
    tn = sum(1 for i in lab if i not in selected and LABELS[i] == 0)
    return {
        "group_size": len(rows),
        "num_selected": len(sel),
        "selection_rate": len(sel) / len(rows),
        "precision": tp / (tp + fp),
        "fdr": fp / (tp + fp),
        "for": fn / (fn + tn),
        "npv": tn / (fn + tn),
        "tpr": tp / (tp + fn),
        "fpr": fp / (fp + tn),
    }


def test_bias_selects_over_the_population_and_scores_the_labeled(db_pool_greenfield):
    """group_size / selection count every row; precision, FDR, FOR, NPV count labeled rows."""
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _seed_groups(pool)
    compute_bias_in_db(pool, model_id, AS_OF_DATE, LABEL_TIMESPAN, "6_abs")
    got = _bias(pool, model_id)
    for group in ("a", "b"):
        for metric, value in _expected_bias(group, 6).items():
            assert got[(group, metric)] == pytest.approx(value, abs=1e-12), (
                group,
                metric,
            )


def _link_run(pool, model_id) -> None:
    """The run → labels-artifact edge model_threshold_curve joins through."""
    with pool.connection() as conn:
        run_id = conn.execute(
            "insert into triage.runs (profile) values ('local') returning run_id"
        ).fetchone()["run_id"]
        conn.execute(
            "insert into triage.run_artifacts (run_id, artifact_id) values (%(r)s, 'labels-art')",
            {"r": run_id},
        )
        conn.execute(
            "update triage.models set run_id = %(r)s,"
            " training_label_timespan = cast(%(ts)s as interval) where model_id = %(m)s",
            {"r": run_id, "ts": LABEL_TIMESPAN, "m": model_id},
        )


def test_the_dashboard_curve_reads_what_precision_at_k_stores(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)
    with pool.connection() as conn:
        curve = conn.execute(
            "select * from triage.model_threshold_curve(%(m)s) order by k",
            {"m": model_id},
        ).fetchall()
    assert len(curve) == len(SCORES)  # one point per scored row, labeled or not
    for point in curve:
        expected = dssg_reference(SCORES, LABELS, ENTITY_IDS, f"{point['k']}_abs")
        assert point["pct"] == pytest.approx(point["k"] / len(SCORES))
        if expected["precision"] is None:
            assert point["prec"] is None
        else:
            assert point["prec"] == pytest.approx(expected["precision"], abs=1e-12)
        assert point["rec"] == pytest.approx(expected["recall"], abs=1e-12)
        assert point["tp"] + point["fp"] == expected["num_labeled_above_threshold"]


def test_0024_recomputes_stored_bias_and_leaves_rows_without_lineage(
    db_pool_greenfield, db_url
):
    """Rows written under 0023 are rewritten by the upgrade; a key whose predictions carry no
    matrix has no recoverable label_timespan and is left exactly as it was."""
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _seed_groups(pool)
    downgrade_db(dburl=db_url, revision="0023_probability_metrics")
    compute_bias_in_db(pool, model_id, AS_OF_DATE, LABEL_TIMESPAN, "6_abs")
    old = _bias(pool, model_id)

    upgrade_db(dburl=db_url, revision="head")
    new = _bias(pool, model_id)
    assert new != old
    for group in ("a", "b"):
        assert new[(group, "group_size")] == _expected_bias(group, 6)["group_size"]
        assert new[(group, "precision")] == pytest.approx(
            _expected_bias(group, 6)["precision"]
        )


def test_0024_leaves_a_key_without_matrix_lineage_as_it_was(db_pool_greenfield, db_url):
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS, matrix=False)
    downgrade_db(dburl=db_url, revision="0023_probability_metrics")
    _evaluate(pool, model_id, ["5_abs"])
    with pool.connection() as conn:
        before = conn.execute(
            "select value, computed_at from triage.evaluations where metric = 'precision@'"
        ).fetchone()

    upgrade_db(dburl=db_url, revision="head")
    row = _row(pool, model_id, "precision@", "5_abs")
    with pool.connection() as conn:
        computed_at = conn.execute(
            "select computed_at from triage.evaluations where metric = 'precision@'"
        ).fetchone()["computed_at"]
    assert row["value"] == before["value"]
    assert row["num_labeled_above_threshold"] is None
    assert computed_at == before["computed_at"]


# A second test date in the same matrix: different scores, every row labeled.
AS_OF_DATE_2 = "2014-02-01"
SCORES_2 = [0.1, 0.9, 0.5, 0.3]
LABELS_2 = [1, 0, 1, 0]
ENTITY_IDS_2 = [1, 2, 3, 4]


def _add_second_test_date(pool, model_id) -> None:
    with pool.connection() as conn:
        matrix_uuid = conn.execute(
            "select distinct matrix_uuid from triage.predictions where model_id = %(m)s",
            {"m": model_id},
        ).fetchone()["matrix_uuid"]
        for eid, score, label in zip(ENTITY_IDS_2, SCORES_2, LABELS_2, strict=True):
            conn.execute(
                "insert into triage.predictions"
                " (model_id, entity_id, as_of_date, split_kind, score, matrix_uuid)"
                " values (%(m)s, %(e)s, %(d)s, 'test', %(s)s, %(u)s)",
                {
                    "m": model_id,
                    "e": eid,
                    "d": AS_OF_DATE_2,
                    "s": score,
                    "u": matrix_uuid,
                },
            )
            conn.execute(
                "insert into triage.labels"
                " (label_hash, entity_id, as_of_date, label_timespan, outcome)"
                " values ('labels-art', %(e)s, %(d)s, cast(%(ts)s as interval), %(o)s)",
                {"e": eid, "d": AS_OF_DATE_2, "ts": LABEL_TIMESPAN, "o": float(label)},
            )


def _curve(pool, model_id, as_of_date=None):
    with pool.connection() as conn:
        return conn.execute(
            "select * from triage.model_threshold_curve(%(m)s, %(d)s::date) order by k",
            {"m": model_id, "d": as_of_date},
        ).fetchall()


def _assert_curve_is_dssg(curve, scores, labels, entity_ids) -> None:
    assert [p["k"] for p in curve] == list(range(1, len(scores) + 1))
    for point in curve:
        expected = dssg_reference(scores, labels, entity_ids, f"{point['k']}_abs")
        assert point["pct"] == pytest.approx(point["k"] / len(scores))
        if expected["precision"] is None:
            assert point["prec"] is None
        else:
            assert point["prec"] == pytest.approx(expected["precision"], abs=1e-12)
        assert point["rec"] == pytest.approx(expected["recall"], abs=1e-12)


def test_the_curve_is_one_test_date_not_every_date_pooled(db_pool_greenfield):
    """Evaluations are stored per as_of_date, so the curve that reads them must be too.

    Before 0025 the curve took every test row of the model and ordered them by their per-date
    rank, interleaving the dates: k = 2 held the two rank-1 rows of two different lists.
    """
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)
    _add_second_test_date(pool, model_id)

    _assert_curve_is_dssg(
        _curve(pool, model_id, AS_OF_DATE), SCORES, LABELS, ENTITY_IDS
    )
    _assert_curve_is_dssg(
        _curve(pool, model_id, AS_OF_DATE_2), SCORES_2, LABELS_2, ENTITY_IDS_2
    )
    # no date given: the model's latest test date
    assert _curve(pool, model_id) == _curve(pool, model_id, AS_OF_DATE_2)


def test_the_curve_reads_labels_at_the_scored_matrix_horizon(db_pool_greenfield):
    """Test labels carry the test matrix's label_timespan, which need not be the training one.

    Before 0025 the curve joined on ``models.training_label_timespan``: with a 1-year training
    horizon and 6-month test labels it found no label at all and drew precision as NULL.
    """
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)
    with pool.connection() as conn:
        conn.execute(
            "update triage.models set training_label_timespan = interval '1 year'"
            " where model_id = %(m)s",
            {"m": model_id},
        )

    _assert_curve_is_dssg(_curve(pool, model_id), SCORES, LABELS, ENTITY_IDS)


def test_the_curve_of_a_model_without_test_predictions_is_empty(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    with pool.connection() as conn:
        conn.execute(
            "update triage.predictions set split_kind = 'production' where model_id = %(m)s",
            {"m": model_id},
        )
    assert _curve(pool, model_id) == []


def test_migration_0025_round_trips(db_pool_greenfield, db_url):
    """Down to 0024: the one-argument curve and the split-blind views again. Up: both fixed."""
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)
    _add_second_test_date(pool, model_id)

    def signatures():
        with pool.connection() as conn:
            return {
                r["args"]
                for r in conn.execute(
                    "select pg_get_function_identity_arguments(p.oid) as args"
                    " from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
                    " where n.nspname = 'triage' and p.proname = 'model_threshold_curve'"
                ).fetchall()
            }

    downgrade_db(dburl=db_url, revision="0024_population_topk")
    assert signatures() == {"p_model bigint"}
    with pool.connection() as conn:
        pooled = conn.execute(
            "select count(*) as n from triage.model_threshold_curve(%(m)s)",
            {"m": model_id},
        ).fetchone()["n"]
    assert pooled == len(SCORES) + len(SCORES_2)

    # to 0025 itself, not head: a later revision may widen the signature again
    upgrade_db(dburl=db_url, revision="0025_split_ranks_and_curve")
    assert signatures() == {"p_model bigint, p_as_of_date date"}
    _assert_curve_is_dssg(
        _curve(pool, model_id, AS_OF_DATE_2), SCORES_2, LABELS_2, ENTITY_IDS_2
    )
