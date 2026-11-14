"""Error trees for regression: where |error| is large, and where the model beats its baseline.

A regression model has no top-k cut and no binary outcome, so the fp/fn/confusion trees of
:mod:`triage.diagnostics.error_tree` do not apply. Two kinds take their place (migration 0034):

* ``abs_error`` — per test date, a ``DecisionTreeRegressor`` on |ŷ − y| over every labeled row.
  A leaf's ``error_rate`` is its mean |e| over the date's population MAE, so 1 is an average
  row; ``n_errors`` counts its rows above that MAE.
* ``beats_baseline`` — a ``DecisionTreeClassifier`` on "the model is closer than the baseline"
  (strictly: a tie is not closer), fitted on the model group's test dates before the model's
  last one and applied to the last. The fit, its AUC on the last date and both base rates go
  to ``triage.error_tree_fits``; each leaf carries its training and test win shares and the
  gain (baseline |e| − model |e|) on the test rows it holds.

Leaves are persisted to ``triage.error_analysis`` with the same ``rule``, ``conditions`` and
``signature`` as the classification trees, so ``error-rules``, ``error-recurrence`` and the
plots read them unchanged; their own numbers are in ``leaf_stats``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from typing import Any, Optional

import numpy as np
import polars as pl

from triage.diagnostics.matrixio import MatrixContext, load_matrix_context
from triage.diagnostics.signatures import signature_of
from triage.logging import get_logger
from triage.util.db import DictRowPool

logger = get_logger(__name__)

#: The regression tree kinds, as ``triage.error_analysis.error_kind`` stores them.
REGRESSION_TREE_KINDS = ("abs_error", "beats_baseline")


def _leaf_paths(
    tree: Any, feature_names: Sequence[str]
) -> dict[int, list[dict[str, Any]]]:
    """Leaf node id → the conditions on its path from the root."""
    t = tree.tree_
    paths: dict[int, list[dict[str, Any]]] = {}

    def walk(node: int, conditions: list[dict[str, Any]]) -> None:
        if t.children_left[node] == -1:
            paths[node] = conditions
            return
        feature = feature_names[t.feature[node]]
        threshold = float(t.threshold[node])
        for child, op in ((t.children_left[node], "<="), (t.children_right[node], ">")):
            walk(
                child,
                [*conditions, {"feature": feature, "op": op, "threshold": threshold}],
            )

    walk(0, [])
    return paths


def _rule_text(conditions: Sequence[dict[str, Any]]) -> str:
    text = " AND ".join(
        f"{c['feature']} {c['op']} {c['threshold']:.4g}" for c in conditions
    )
    return text or "(all)"


def _persist(
    pool: DictRowPool,
    model_id: int,
    split_kind: str,
    as_of_date: date,
    parameter: str,
    error_kind: str,
    rules: Sequence[dict[str, Any]],
) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "delete from triage.error_analysis"
            " where model_id = %(m)s and split_kind = cast(%(s)s as triage.split_kind)"
            "   and as_of_date = %(d)s and parameter = %(p)s and error_kind = %(k)s",
            {
                "m": model_id,
                "s": split_kind,
                "d": as_of_date,
                "p": parameter,
                "k": error_kind,
            },
        )
        cur.executemany(
            "insert into triage.error_analysis"
            " (model_id, split_kind, as_of_date, parameter, error_kind, rule_id, rule,"
            "  n_matched, n_errors, error_rate, depth, class_counts, conditions, signature,"
            "  leaf_stats)"
            " values (%(m)s, cast(%(s)s as triage.split_kind), %(d)s, %(p)s, %(k)s,"
            "         %(rid)s, %(rule)s, %(n)s, %(ne)s, %(er)s, %(dep)s,"
            "         cast(%(cc)s as jsonb), cast(%(cond)s as jsonb), %(sig)s,"
            "         cast(%(ls)s as jsonb))",
            [
                {
                    "m": model_id,
                    "s": split_kind,
                    "d": as_of_date,
                    "p": parameter,
                    "k": error_kind,
                    "rid": i,
                    "rule": r["rule"],
                    "n": r["n_matched"],
                    "ne": r["n_errors"],
                    "er": r["error_rate"],
                    "dep": len(r["conditions"]),
                    "cc": json.dumps(r["class_counts"]) if r["class_counts"] else None,
                    "cond": json.dumps(r["conditions"]),
                    "sig": signature_of(r["conditions"]),
                    "ls": json.dumps(r["leaf_stats"]),
                }
                for i, r in enumerate(rules)
            ],
        )


def _labeled(pool: DictRowPool, model_id: int, split_kind: str) -> pl.DataFrame:
    """The model's labeled scored rows: ``entity_id, as_of_date, score, outcome``."""
    with pool.connection() as conn:
        rows = conn.execute(
            "select entity_id, as_of_date, score, outcome"
            " from triage.scored_labels(%(m)s, cast(%(s)s as triage.split_kind))"
            " where outcome is not null and score is not null",
            {"m": model_id, "s": split_kind},
        ).fetchall()
    return pl.DataFrame(
        rows,
        schema={
            "entity_id": pl.Int64,
            "as_of_date": pl.Date,
            "score": pl.Float64,
            "outcome": pl.Float64,
        },
        orient="row",
    )


def _with_features(rows: pl.DataFrame, ctx: MatrixContext) -> pl.DataFrame:
    """``rows`` joined to the matrix's feature values on ``(entity_id, as_of_date)``."""
    frame = ctx.frame.select(
        pl.col("entity_id").cast(pl.Int64),
        pl.col("as_of_date").cast(pl.Date),
        *ctx.feature_names,
    )
    joined = rows.join(frame, on=["entity_id", "as_of_date"], how="inner")
    if joined.height < rows.height:
        logger.warning(
            f"matrix {ctx.matrix_uuid}: {rows.height - joined.height} labeled rows have no"
            " matrix row — analyzing the intersection"
        )
    return joined


# --------------------------------------------------------------------------- abs_error


def abs_error_trees(
    pool: DictRowPool,
    model_id: int,
    split_kind: str = "test",
    max_depth: int = 3,
    min_samples_leaf: int = 30,
    random_seed: int = 0,
) -> int:
    """Fit one ``abs_error`` tree per date and persist its leaves. Returns leaves written."""
    from sklearn.tree import DecisionTreeRegressor

    ctx = load_matrix_context(pool, model_id, split_kind)
    rows = _with_features(_labeled(pool, model_id, split_kind), ctx)
    written = 0
    for (as_of_date,), day in rows.group_by("as_of_date", maintain_order=True):
        if day.height < 2:
            continue
        err = (day["score"] - day["outcome"]).to_numpy()
        abs_err = np.abs(err)
        x = day.select(ctx.feature_names).to_numpy()
        tree = DecisionTreeRegressor(
            max_depth=max_depth,
            min_samples_leaf=min_samples_leaf,
            random_state=random_seed,
        )
        tree.fit(x, abs_err)
        leaves = tree.apply(x)
        population_mae = float(abs_err.mean())
        total = float(abs_err.sum())
        outcome = day["outcome"].to_numpy()
        rules = []
        for node, conditions in _leaf_paths(tree, ctx.feature_names).items():
            mask = leaves == node
            n = int(mask.sum())
            if not n:
                continue
            leaf_mae = float(abs_err[mask].mean())
            rules.append(
                {
                    "rule": _rule_text(conditions),
                    "conditions": conditions,
                    "n_matched": n,
                    "n_errors": int((abs_err[mask] > population_mae).sum()),
                    "error_rate": leaf_mae / population_mae if population_mae else 0.0,
                    "class_counts": None,
                    "leaf_stats": {
                        "n": n,
                        "mean_abs_error": leaf_mae,
                        "bias": float(err[mask].mean()),
                        "share_of_abs_error": float(abs_err[mask].sum()) / total
                        if total
                        else None,
                        "mean_outcome": float(outcome[mask].mean()),
                        "population_mae": population_mae,
                    },
                }
            )
        rules.sort(key=lambda r: (-r["error_rate"], -r["n_matched"]))
        _persist(pool, model_id, split_kind, as_of_date, "", "abs_error", rules)
        written += len(rules)
        logger.info(
            f"error analysis: model {model_id} @ {as_of_date} [abs_error] — {len(rules)} leaves"
        )
    return written


# ---------------------------------------------------------------------- beats_baseline


def _group_models(
    pool: DictRowPool, model_id: int
) -> tuple[int, str, list[tuple[int, date]]]:
    """The model's group, its Experiment, and every (model, test date) of the group there."""
    with pool.connection() as conn:
        row = conn.execute(
            "select m.model_group_id, r.experiment_hash from triage.models m"
            " join triage.runs r on r.run_id = m.run_id where m.model_id = %(m)s",
            {"m": model_id},
        ).fetchone()
        if row is None:
            raise ValueError(
                f"model {model_id} has no run lineage — beats-baseline compares it with"
                " a baseline of its Experiment"
            )
        dates = conn.execute(
            "select distinct p.model_id, p.as_of_date from triage.predictions p"
            " join triage.models m on m.model_id = p.model_id"
            " join triage.runs r on r.run_id = m.run_id"
            " where m.model_group_id = %(g)s and r.experiment_hash = %(h)s"
            "   and p.split_kind = 'test' order by p.as_of_date, p.model_id",
            {"g": row["model_group_id"], "h": row["experiment_hash"]},
        ).fetchall()
    return (
        row["model_group_id"],
        row["experiment_hash"],
        [(r["model_id"], r["as_of_date"]) for r in dates],
    )


def beats_baseline_tree(
    pool: DictRowPool,
    model_id: int,
    baseline_group_id: Optional[int] = None,
    max_depth: int = 3,
    min_samples_leaf: int = 30,
    random_seed: int = 0,
) -> int:
    """Fit the ``beats_baseline`` tree for a model and persist its leaves and fit.

    Training rows are the group's test dates before the model's last one, each with the
    features of the group's model that scored it; the test rows are the model's last date.

    Raises:
        ValueError: no run lineage, no baseline, no earlier test date, or a single class.
    """
    from sklearn.metrics import roc_auc_score
    from sklearn.tree import DecisionTreeClassifier

    from triage.diagnostics.baseline_compare import default_baseline, paired_rows

    group_id, experiment, model_dates = _group_models(pool, model_id)
    own = [d for m, d in model_dates if m == model_id]
    if not own:
        raise ValueError(f"model {model_id} has no test predictions")
    eval_date = max(own)
    earlier = [(m, d) for m, d in model_dates if d < eval_date]
    by_date: dict[date, list[int]] = {}
    for m, d in earlier:
        by_date.setdefault(d, []).append(m)
    twins = {d: ms for d, ms in by_date.items() if len(ms) > 1}
    if twins:
        d, ms = next(iter(twins.items()))
        raise ValueError(
            f"model group {group_id} has {len(ms)} models scoring {d} — beats-baseline"
            " fits on one model per date"
        )
    if not by_date:
        raise ValueError(
            f"model group {group_id} has no test date before {eval_date}: beats-baseline"
            " fits on the earlier dates and tests on the last — score more test dates"
        )
    baseline = (
        default_baseline(pool, experiment, group_id)
        if baseline_group_id is None
        else baseline_group_id
    )
    paired = paired_rows(pool, experiment, group_id, baseline).with_columns(
        (pl.col("err_m") < pl.col("err_b")).cast(pl.Int8).alias("closer"),
        (pl.col("err_b") - pl.col("err_m")).alias("gain"),
    )
    contexts: dict[int, MatrixContext] = {}
    parts: list[pl.DataFrame] = []
    feature_names: Optional[list[str]] = None
    for d, (m,) in sorted(by_date.items()) + [(eval_date, [model_id])]:
        if m not in contexts:
            contexts[m] = load_matrix_context(pool, m, "test")
        ctx = contexts[m]
        if feature_names is None:
            feature_names = ctx.feature_names
        elif ctx.feature_names != feature_names:
            raise ValueError(
                f"models of group {group_id} carry different feature lists — beats-baseline"
                " needs one feature geometry across the dates"
            )
        parts.append(_with_features(paired.filter(pl.col("as_of_date") == d), ctx))
    assert feature_names is not None
    rows = pl.concat(parts)
    train = rows.filter(pl.col("as_of_date") < eval_date)
    test = rows.filter(pl.col("as_of_date") == eval_date)
    y_train = train["closer"].to_numpy()
    if train.height < 2 or len(set(y_train.tolist())) < 2:
        raise ValueError(
            f"model {model_id}: on the dates before {eval_date} the model is closer than"
            f" baseline group {baseline} on {'every' if y_train.all() else 'no'} row — no"
            " tree to fit"
        )
    tree = DecisionTreeClassifier(
        max_depth=max_depth, min_samples_leaf=min_samples_leaf, random_state=random_seed
    )
    x_train = train.select(feature_names).to_numpy()
    x_test = test.select(feature_names).to_numpy()
    tree.fit(x_train, y_train)
    y_test = test["closer"].to_numpy()
    auc = (
        float(roc_auc_score(y_test, np.asarray(tree.predict_proba(x_test))[:, 1]))
        if test.height and len(set(y_test.tolist())) == 2
        else None
    )
    leaves_train = tree.apply(x_train)
    leaves_test = tree.apply(x_test) if test.height else np.array([], dtype=int)
    gain = test["gain"].to_numpy()
    rules = []
    for node, conditions in _leaf_paths(tree, feature_names).items():
        tr = leaves_train == node
        te = leaves_test == node
        n_train, n_test = int(tr.sum()), int(te.sum())
        if not n_train:
            continue
        wins = int(y_train[tr].sum())
        rules.append(
            {
                "rule": _rule_text(conditions),
                "conditions": conditions,
                "n_matched": n_train,
                "n_errors": n_train - wins,
                "error_rate": (n_train - wins) / n_train,
                "class_counts": {"closer": wins, "not_closer": n_train - wins},
                "leaf_stats": {
                    "train_n": n_train,
                    "train_win_share": wins / n_train,
                    "test_n": n_test,
                    "test_win_share": float(y_test[te].mean()) if n_test else None,
                    "mean_gain": float(gain[te].mean()) if n_test else None,
                    "total_gain": float(gain[te].sum()) if n_test else 0.0,
                },
            }
        )
    rules.sort(key=lambda r: (-r["error_rate"], -r["n_matched"]))
    parameter = f"baseline:{baseline}"
    _persist(pool, model_id, "test", eval_date, parameter, "beats_baseline", rules)
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.error_tree_fits (model_id, split_kind, error_kind,"
            " parameter, fit_dates, eval_date, baseline_group_id, auc, base_rate_train,"
            " base_rate_test) values (%(m)s, 'test', 'beats_baseline', %(p)s, %(fd)s,"
            " %(ed)s, %(b)s, %(auc)s, %(brt)s, %(bre)s)"
            " on conflict (model_id, split_kind, error_kind, parameter) do update set"
            " fit_dates = excluded.fit_dates, eval_date = excluded.eval_date,"
            " baseline_group_id = excluded.baseline_group_id, auc = excluded.auc,"
            " base_rate_train = excluded.base_rate_train,"
            " base_rate_test = excluded.base_rate_test, computed_at = now()",
            {
                "m": model_id,
                "p": parameter,
                "fd": sorted(by_date),
                "ed": eval_date,
                "b": baseline,
                "auc": auc,
                "brt": float(y_train.mean()),
                "bre": float(y_test.mean()) if test.height else None,
            },
        )
    logger.info(
        f"error analysis: model {model_id} @ {eval_date} [beats_baseline vs {baseline}]"
        f" — {len(rules)} leaves, AUC {auc}"
    )
    return len(rules)
