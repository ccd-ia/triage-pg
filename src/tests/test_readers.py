"""The shared query layer (``triage.diagnostics.readers``): what the dashboard and the plots read.

The routes that moved onto the readers are held to their old contracts by the dashboard suite;
these tests cover what the readers add. A thinned curve keeps every configured cut's exact
row, so it still equals the stored ``precision@`` there. The reference error rate is the
population's rate at the cut (for fp leaves, 1 − the stored ``precision@``). The nonzero-rate
crosstabs rank by absolute difference, and a group's curves come one per (model, test date).
The fixture is one model group with two models, three test dates between them, a Subset and
stored threshold metrics.
"""

from __future__ import annotations

import ast
import json
import math
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from triage.component.catwalk.in_pg_evaluation import evaluate_in_db
from triage.diagnostics import readers

LABEL_TIMESPAN = "6 months"
# model A is scored on D1 and D2, model B on D3; D3 falls between them, so date order is
# not model order
D1, D3, D2 = date(2014, 1, 1), date(2014, 2, 1), date(2014, 4, 1)
NO_PREDICTIONS = date(2013, 6, 1)
POPULATION = {D1: 200, D2: 30, D3: 40}
THRESHOLDS = ["1_abs", "25_abs", "10_pct", "33_pct"]
SUBSET = "s1-thirds"
SUBSET_NAME = "every third"


@dataclass
class Group:
    model_group_id: int
    model_a: int
    model_b: int
    subset_size: int


def _population(n: int, seed: int) -> tuple[list[int], list[float], list[int | None]]:
    """``n`` scored entities: about 30% unlabeled, the rest positive with p = score."""
    rng = np.random.default_rng(seed)
    scores = [round(float(s), 4) for s in rng.random(n)]
    labels = [
        None if rng.random() < 0.3 else int(rng.random() < score) for score in scores
    ]
    return list(range(1, n + 1)), scores, labels


def _score(conn, model_id: int, as_of: date, matrix_uuid: str, seed: int) -> None:
    entity_ids, scores, labels = _population(POPULATION[as_of], seed)
    with conn.cursor() as cur:
        cur.executemany(
            "insert into triage.predictions"
            " (model_id, entity_id, as_of_date, split_kind, score, matrix_uuid)"
            " values (%(m)s, %(e)s, %(d)s, 'test', %(s)s, %(u)s)",
            [
                {"m": model_id, "e": e, "d": as_of, "s": s, "u": matrix_uuid}
                for e, s in zip(entity_ids, scores, strict=True)
            ],
        )
        cur.executemany(
            "insert into triage.labels"
            " (label_hash, entity_id, as_of_date, label_timespan, outcome)"
            " values ('labels-art', %(e)s, %(d)s, cast(%(ts)s as interval), %(o)s)"
            " on conflict do nothing",
            [
                {"e": e, "d": as_of, "ts": LABEL_TIMESPAN, "o": float(label)}
                for e, label in zip(entity_ids, labels, strict=True)
                if label is not None
            ],
        )


@pytest.fixture
def group(db_pool_greenfield) -> Group:
    pool = db_pool_greenfield
    matrix_uuid = str(uuid.uuid4())
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) values"
            " ('model-a', 'model-a', 'model', '{}'::jsonb),"
            " ('model-b', 'model-b', 'model', '{}'::jsonb),"
            " ('labels-art', 'labels-art', 'labels', '{}'::jsonb),"
            " ('matrix-art', 'matrix-art', 'matrix', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.artifact_inputs (artifact_id, parent_id)"
            " values ('matrix-art', 'labels-art')"
        )
        conn.execute(
            "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind,"
            " storage_uri, label_timespan) values (%(u)s, 'matrix-art', 'test', 'mem://m',"
            " cast(%(ts)s as interval))",
            {"u": matrix_uuid, "ts": LABEL_TIMESPAN},
        )
        runs = {}
        for exp in ("exp-a", "exp-b"):
            conn.execute(
                "insert into triage.experiments (experiment_hash, config, problem_type)"
                " values (%(h)s, '{}'::jsonb, 'classification')",
                {"h": exp},
            )
            runs[exp] = conn.execute(
                "insert into triage.runs (experiment_hash, profile) values (%(h)s, 'local')"
                " returning run_id",
                {"h": exp},
            ).fetchone()["run_id"]
        gid = conn.execute(
            "insert into triage.model_groups"
            " (model_group_hash, model_type, hyperparameters, feature_list)"
            " values ('mg-readers', 'sklearn.tree.DecisionTreeClassifier', '{}'::jsonb,"
            " array['f']) returning model_group_id"
        ).fetchone()["model_group_id"]
        models = {}
        for art, exp in (("model-a", "exp-a"), ("model-b", "exp-b")):
            models[art] = conn.execute(
                "insert into triage.models (model_group_id, model_hash, run_id,"
                " train_end_time, training_label_timespan)"
                " values (%(g)s, %(a)s, %(r)s, date '2013-07-01', cast(%(ts)s as interval))"
                " returning model_id",
                {"g": gid, "a": art, "r": runs[exp], "ts": LABEL_TIMESPAN},
            ).fetchone()["model_id"]
        _score(conn, models["model-a"], D1, matrix_uuid, seed=1)
        _score(conn, models["model-a"], D2, matrix_uuid, seed=2)
        _score(conn, models["model-b"], D3, matrix_uuid, seed=3)
        # the Subset holds every third entity, at D1 only
        conn.execute(
            "insert into triage.subsets (subset_hash, config) values (%(h)s, %(c)s::jsonb)",
            {"h": SUBSET, "c": json.dumps({"name": SUBSET_NAME, "query": "select 1"})},
        )
        members = range(1, POPULATION[D1] + 1, 3)
        with conn.cursor() as cur:
            cur.executemany(
                "insert into triage.subset_members (subset_hash, entity_id, as_of_date)"
                " values (%(h)s, %(e)s, %(d)s)",
                [{"h": SUBSET, "e": e, "d": D1} for e in members],
            )
    for subset in ("", SUBSET):
        evaluate_in_db(
            pool,
            models["model-a"],
            D1,
            LABEL_TIMESPAN,
            metric_config={
                "metrics": ["precision@", "recall@"],
                "thresholds": THRESHOLDS,
            },
            subset_hash=subset,
        )
    return Group(gid, models["model-a"], models["model-b"], len(members))


def _stored(pool, model_id: int, metric: str, parameter: str, subset: str = ""):
    with pool.connection() as conn:
        row = conn.execute(
            "select value from triage.evaluations"
            " where model_id = %(m)s and split_kind = 'test' and as_of_date = %(d)s"
            "   and metric = %(metric)s and parameter = %(p)s and subset_hash = %(s)s",
            {"m": model_id, "d": D1, "metric": metric, "p": parameter, "s": subset},
        ).fetchone()
    assert row is not None, (metric, parameter, subset)
    return row["value"]


def _k(parameter: str, n: int) -> int:
    """``triage.resolve_k``: ``<n>_abs`` is a count, ``<n>_pct`` the ceil of a share."""
    raw, unit = parameter.split("_")
    k = int(raw) if unit == "abs" else math.ceil(float(raw) / 100 * n)
    return max(0, min(k, n))


def _leaf(
    pool,
    model_id: int,
    as_of: date,
    kind: str,
    rule_id: int,
    n_matched: int,
    n_errors: int,
    class_counts: dict[str, int],
    parameter: str = "10_pct",
    split_kind: str = "test",
) -> None:
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.error_analysis (model_id, split_kind, as_of_date,"
            " parameter, error_kind, rule_id, rule, n_matched, n_errors, error_rate,"
            " depth, class_counts, signature)"
            " values (%(m)s, %(s)s, %(d)s, %(p)s, %(k)s, %(r)s, %(rule)s, %(n)s, %(e)s,"
            "         %(er)s, 1, cast(%(cc)s as jsonb), %(sig)s)",
            {
                "m": model_id,
                "s": split_kind,
                "d": as_of,
                "p": parameter,
                "k": kind,
                "r": rule_id,
                "rule": f"f > {rule_id}",
                "n": n_matched,
                "e": n_errors,
                "er": n_errors / n_matched,
                "cc": json.dumps(class_counts),
                "sig": "f>",
            },
        )


# ------------------------------------------------------------------------ the module


def test_the_readers_import_no_fastapi():
    tree = ast.parse(Path(readers.__file__).read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imported & {"fastapi", "starlette"}, imported
    # nor does anything it imports pull FastAPI in: the core install must be enough
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, triage.diagnostics.readers; print('fastapi' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert loaded.stdout.strip() == "False", loaded.stdout + loaded.stderr


# ---------------------------------------------------------------------------- curve


def test_without_points_the_curve_is_the_sql_function_row_for_row(
    db_pool_greenfield, group
):
    pool = db_pool_greenfield
    for as_of, subset in ((D1, ""), (D2, ""), (None, ""), (D1, SUBSET)):
        with pool.connection() as conn:
            expected = conn.execute(
                "select * from triage.model_threshold_curve(%(m)s, %(d)s::date, %(s)s)"
                " order by k",
                {"m": group.model_a, "d": as_of, "s": subset},
            ).fetchall()
        assert expected, (as_of, subset)
        assert readers.curve(pool, group.model_a, as_of, subset) == expected


def test_a_thinned_curve_keeps_each_cut_and_meets_the_stored_metric(
    db_pool_greenfield, group
):
    pool = db_pool_greenfield
    full = {r["k"]: r for r in readers.curve(pool, group.model_a, D1)}
    n = POPULATION[D1]
    assert len(full) == n

    thin = readers.curve(pool, group.model_a, D1, points=11)
    cuts = {p: _k(p, n) for p in THRESHOLDS}
    shares = {max(1, i * n // 10) for i in range(11)}  # 0, 10%, …, 100% of 200 rows
    assert [r["k"] for r in thin] == sorted(shares | set(cuts.values()) | {n})
    assert len(thin) <= 11 + len(THRESHOLDS)
    for row in thin:
        assert row == full[row["k"]]  # thinning drops rows; it changes none

    at_cut = {r["k"]: r for r in thin}
    for parameter, k in cuts.items():
        assert at_cut[k]["prec"] == pytest.approx(
            _stored(pool, group.model_a, "precision@", parameter), abs=1e-12
        )
        assert at_cut[k]["rec"] == pytest.approx(
            _stored(pool, group.model_a, "recall@", parameter), abs=1e-12
        )
    assert cuts["10_pct"] == 20 and at_cut[20]["prec"] is not None


def test_a_thinned_subset_curve_cuts_the_subset_population(db_pool_greenfield, group):
    pool = db_pool_greenfield
    n = group.subset_size
    thin = readers.curve(pool, group.model_a, D1, subset=SUBSET, points=5)
    assert thin[-1]["k"] == n  # the last row: the whole subset
    at = {r["k"]: r for r in thin}
    for parameter in THRESHOLDS:
        assert at[_k(parameter, n)]["prec"] == pytest.approx(
            _stored(pool, group.model_a, "precision@", parameter, SUBSET), abs=1e-12
        )


def test_the_curve_refuses_what_it_cannot_draw(db_pool_greenfield, group):
    pool = db_pool_greenfield
    with pytest.raises(ValueError, match="points=1"):
        readers.curve(pool, group.model_a, D1, points=1)
    with pytest.raises(readers.NotFoundError, match="nowhere"):
        readers.curve(pool, group.model_a, D1, subset="nowhere")
    # no predictions at the date is no curve, not an error
    assert readers.curve(pool, group.model_a, NO_PREDICTIONS, points=11) == []


# -------------------------------------------------------------------- group curves


def test_group_curves_returns_one_series_per_model_and_date(db_pool_greenfield, group):
    pool = db_pool_greenfield
    series = readers.group_curves(pool, group.model_group_id)
    assert [(s["model_id"], s["as_of_date"]) for s in series] == [
        (group.model_a, D1),
        (group.model_b, D3),
        (group.model_a, D2),
    ]
    for s in series:
        points = readers.curve(pool, s["model_id"], s["as_of_date"], points=101)
        assert s["points"] == points
        assert s["population"] == POPULATION[s["as_of_date"]] == points[-1]["k"]
        assert s["base_rate"] == points[-1]["base_rate"] == points[0]["base_rate"]

    # one experiment's models of the shared group
    scoped = readers.group_curves(pool, group.model_group_id, experiment_hash="exp-b")
    assert [(s["model_id"], s["as_of_date"]) for s in scoped] == [(group.model_b, D3)]
    # a Subset with members at D1 only draws D1 alone, over its own population
    subset = readers.group_curves(
        pool, group.model_group_id, subset=SUBSET, points=None
    )
    assert [(s["model_id"], s["as_of_date"]) for s in subset] == [(group.model_a, D1)]
    assert subset[0]["population"] == group.subset_size


def test_group_curves_refuses_unknown_ids_and_other_splits(db_pool_greenfield, group):
    pool = db_pool_greenfield
    with pytest.raises(readers.NotFoundError, match="model group 999999"):
        readers.group_curves(pool, 999999)
    with pytest.raises(readers.NotFoundError, match="nowhere"):
        readers.group_curves(pool, group.model_group_id, subset="nowhere")
    with pytest.raises(ValueError, match="test predictions only"):
        readers.group_curves(pool, group.model_group_id, split="train")


# ---------------------------------------------------------------------- error trees


def test_the_fp_reference_rate_is_one_minus_stored_precision(db_pool_greenfield, group):
    pool = db_pool_greenfield
    n = POPULATION[D1]
    full = {r["k"]: r for r in readers.curve(pool, group.model_a, D1)}
    n_compared = 0
    for parameter in THRESHOLDS:
        rates = readers.error_reference_rates(pool, group.model_a, parameter, D1)
        precision = _stored(pool, group.model_a, "precision@", parameter)
        if precision is None:  # no labeled row inside the cut: no rate either way
            assert rates["fp"] is None, parameter
        else:
            assert rates["fp"] == pytest.approx(1 - precision, abs=1e-12), parameter
            n_compared += 1
        row = full[_k(parameter, n)]
        assert rates["fn"] == pytest.approx(row["fn"] / (row["fn"] + row["tn"]))
        labeled = row["tp"] + row["fp"] + row["fn"] + row["tn"]
        assert rates["confusion"] == pytest.approx((row["fp"] + row["fn"]) / labeled)
    assert (
        n_compared == len(THRESHOLDS) - 1
    )  # all but 1_abs, whose one row is unlabeled
    # no test predictions at the date: no reference
    assert readers.error_reference_rates(
        pool, group.model_a, "10_pct", NO_PREDICTIONS
    ) == {"fp": None, "fn": None, "confusion": None}


def test_each_leaf_carries_the_reference_rate_of_its_kind(db_pool_greenfield, group):
    pool = db_pool_greenfield
    _leaf(pool, group.model_a, D1, "fp", 0, 100, 1, {"tp": 99, "fp": 1})
    _leaf(pool, group.model_a, D1, "fn", 0, 60, 20, {"fn": 20, "tn": 40})
    _leaf(pool, group.model_a, D1, "confusion", 0, 50, 10, {"tp": 40, "fp": 10})
    _leaf(pool, group.model_a, NO_PREDICTIONS, "fp", 0, 40, 30, {"tp": 10, "fp": 30})
    rates = readers.error_reference_rates(pool, group.model_a, "10_pct", D1)

    rows = readers.error_rules(pool, group.model_a, limit=100)
    assert len(rows) == 4
    for row in rows:
        expected = (
            None if row["as_of_date"] == NO_PREDICTIONS else rates[row["error_kind"]]
        )
        assert row["reference_error_rate"] == expected, row
    # the suspicious leaves carry it too
    suspicious = readers.suspicious_leaves(pool, group.model_a)
    assert [(r["error_kind"], r["reference_error_rate"]) for r in suspicious] == [
        ("fp", rates["fp"])
    ]
    with pytest.raises(ValueError, match="'tp' is not an error-tree leaf kind"):
        readers.error_rules(pool, group.model_a, error_kind="tp")


def test_a_train_leaf_has_no_reference_rate(db_pool_greenfield, group):
    """The reference is the test population's rate: a leaf fitted on another split gets
    none, and the row keeps the route's fields (no split_kind)."""
    pool = db_pool_greenfield
    _leaf(pool, group.model_a, D1, "fp", 0, 100, 1, {"tp": 99, "fp": 1})
    _leaf(
        pool, group.model_a, D1, "fp", 1, 80, 8, {"tp": 72, "fp": 8}, split_kind="train"
    )
    rate = readers.error_reference_rates(pool, group.model_a, "10_pct", D1)["fp"]
    assert rate is not None

    rows = readers.error_rules(pool, group.model_a, limit=100)
    by_rule = {r["rule"]: r["reference_error_rate"] for r in rows}
    assert by_rule == {"f > 0": rate, "f > 1": None}
    assert all("split_kind" not in r for r in rows)


def test_group_leaves_returns_every_leaf_of_the_kinds_asked(db_pool_greenfield, group):
    pool = db_pool_greenfield
    _leaf(pool, group.model_a, D1, "fp", 0, 100, 1, {"tp": 99, "fp": 1})
    _leaf(pool, group.model_a, D1, "fn", 0, 60, 20, {"fn": 20, "tn": 40})
    _leaf(pool, group.model_b, D3, "confusion", 0, 50, 10, {"tp": 45, "fn": 5})
    _leaf(pool, group.model_b, D3, "fp", 1, 30, 3, {"tp": 27, "fp": 3}, "25_abs")

    leaves = readers.group_leaves(pool, group.model_group_id, "10_pct")
    assert [(r["model_id"], r["as_of_date"], r["error_kind"]) for r in leaves] == [
        (group.model_a, D1, "fp"),
        (group.model_b, D3, "confusion"),
    ]
    fp, confusion = leaves
    assert fp["wrong_share"] == pytest.approx(1 / 100)
    assert fp["tp_share"] == pytest.approx(99 / 100)
    assert confusion["wrong_share"] == pytest.approx(1 - 45 / 50)

    fn = readers.group_leaves(pool, group.model_group_id, "10_pct", kinds=("fn",))
    assert [(r["error_kind"], r["tp_share"]) for r in fn] == [("fn", None)]
    with pytest.raises(readers.NotFoundError):
        readers.group_leaves(pool, 999999, "10_pct")
    with pytest.raises(ValueError, match="'both' is not an error-tree leaf kind"):
        readers.group_leaves(pool, group.model_group_id, "10_pct", kinds=("both",))


def test_error_recurrence_names_an_unknown_group(db_pool_greenfield, group):
    pool = db_pool_greenfield
    empty = readers.error_recurrence(pool, group.model_group_id)
    assert empty["parameters"] == [] and empty["rules"] == []
    with pytest.raises(readers.NotFoundError, match="model group 999999"):
        readers.error_recurrence(pool, 999999)


# ------------------------------------------------------------------------ crosstabs


def _crosstab(pool, model_id: int, feature: str, stat: str, s, r, parameter="10_pct"):
    ratio = s / r if s is not None and r not in (None, 0) else None
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.crosstabs (model_id, split_kind, as_of_date, parameter,"
            " feature, stat, selected_value, rest_value, ratio)"
            " values (%(m)s, 'test', %(d)s, %(p)s, %(f)s, %(stat)s, %(s)s, %(r)s, %(ratio)s)",
            {
                "m": model_id,
                "d": D1,
                "p": parameter,
                "f": feature,
                "stat": stat,
                "s": s,
                "r": r,
                "ratio": ratio,
            },
        )


def test_nonzero_rate_crosstabs_rank_by_absolute_difference(db_pool_greenfield, group):
    pool = db_pool_greenfield
    m = group.model_a
    # nonzero rates: by |ln ratio| the order would be tiny, drop, broad
    for feature, s, r in (
        ("broad", 0.9, 0.3),  # |diff| 0.6, ratio 3
        ("drop", 0.1, 0.5),  # |diff| 0.4, ratio 0.2
        ("tiny", 0.02, 0.001),  # |diff| 0.019, ratio 20
        ("unknown", None, 0.4),  # no rate among the selected: not ranked
    ):
        _crosstab(pool, m, feature, "nonzero_rate", s, r)
    # means: ranked by |ln ratio|, a zero or missing ratio left out, as before
    for feature, s, r in (
        ("double", 2.0, 1.0),
        ("quarter", 1.0, 4.0),
        ("zero", 0.0, 3.0),
        ("no_rest", 1.0, 0.0),
    ):
        _crosstab(pool, m, feature, "mean", s, r)
    _crosstab(pool, m, "other_cut", "nonzero_rate", 1.0, 0.0, parameter="25_abs")

    nonzero = readers.crosstabs(pool, m, parameter="10_pct", stat="nonzero_rate")
    assert [r["feature"] for r in nonzero] == ["broad", "drop", "tiny"]
    assert {"selected_value", "rest_value", "ratio"} <= set(nonzero[0])
    every_cut = readers.crosstabs(pool, m, stat="nonzero_rate")
    assert [r["feature"] for r in every_cut] == ["other_cut", "broad", "drop", "tiny"]

    mean = readers.crosstabs(pool, m)
    assert [r["feature"] for r in mean] == ["quarter", "double"]
    assert readers.crosstabs(pool, m, as_of_date=D2) == []
    with pytest.raises(ValueError, match="'std' is not a crosstab ranking"):
        readers.crosstabs(pool, m, stat="std")


# --------------------------------------------------------------------------- subsets


def test_resolve_subset_by_name_or_hash_prefix(db_pool_greenfield, group):
    pool = db_pool_greenfield
    for value in (SUBSET_NAME, SUBSET, SUBSET[:4]):
        assert readers.resolve_subset(pool, value) == (SUBSET, SUBSET_NAME)

    with pytest.raises(readers.NotFoundError, match="No subset matches 'nowhere'"):
        readers.resolve_subset(pool, "nowhere")
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.subsets (subset_hash, config)"
            " values ('s1-other', '{\"name\": \"other\"}'::jsonb)"
        )
    with pytest.raises(ValueError, match="Ambiguous subset 's1-'") as ambiguous:
        readers.resolve_subset(pool, "s1-")
    assert not isinstance(ambiguous.value, readers.NotFoundError)
