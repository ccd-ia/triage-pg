"""Error analysis — "predict on the errors to learn" (plan P5, the DSSG error tree).

A shallow, interpretable DecisionTree is fitted on the primary model's MISTAKES at the
top-k cut and its leaf paths become human-readable rules:

* ``fp`` — within the SELECTED population, target = (outcome = 0): where does the
  model over-select?
* ``fn`` — within the PASSED-OVER population, target = (outcome = 1): where does it
  miss need?

This is a diagnostic, never a score modifier (stacking is deliberately out of scope —
see the plan's Questionables): the rules feed feature/cohort work, not predictions.
When the primary model is linear, per-entity β·x contributions for the selected
entities are persisted into ``individual_importances`` (method ``linear_contrib``) —
the "why this entity" surface.
"""

from __future__ import annotations

from typing import Any

from triage.diagnostics.matrixio import load_matrix_context, top_k_entities
from triage.logging import get_logger

logger = get_logger(__name__)

_TOP_CONTRIBUTIONS = 10


def _labeled_ranks(db_engine, model_id, split_kind, date, timespan):
    with db_engine.connection() as conn:
        return conn.execute(
            "select entity_id, rank_abs, outcome"
            " from triage.labeled_ranks(%(m)s, cast(%(s)s as triage.split_kind),"
            "        cast(%(d)s as date), cast(%(t)s as interval))",
            {"m": model_id, "s": split_kind, "d": str(date), "t": timespan},
        ).fetchall()


def _rules_from_tree(tree, feature_names: list[str]) -> list[dict[str, Any]]:
    """Flatten a fitted DecisionTreeClassifier into one rule per leaf.

    ``n_errors`` counts the error class (class 1) in the leaf; rules come back ordered
    by error_rate descending (the highest-lift failure modes first).
    """
    t = tree.tree_
    rules: list[dict[str, Any]] = []

    def walk(node: int, path: list[str], depth: int) -> None:
        if t.children_left[node] == -1:  # leaf
            # sklearn >= 1.4 stores class PROPORTIONS in tree_.value (older: counts);
            # taking n from n_node_samples and the error share as a ratio is invariant.
            value_row = t.value[node][0]
            n = int(t.n_node_samples[node])
            classes = list(tree.classes_)
            share = (
                float(value_row[classes.index(1)]) / float(value_row.sum())
                if 1 in classes and value_row.sum() > 0
                else 0.0
            )
            n_err = int(round(share * n))
            rules.append(
                {
                    "rule": " AND ".join(path) if path else "(all)",
                    "n_matched": n,
                    "n_errors": n_err,
                    "error_rate": (n_err / n) if n else 0.0,
                    "depth": depth,
                }
            )
            return
        feature = feature_names[t.feature[node]]
        threshold = float(t.threshold[node])
        walk(t.children_left[node], [*path, f"{feature} <= {threshold:.4g}"], depth + 1)
        walk(t.children_right[node], [*path, f"{feature} > {threshold:.4g}"], depth + 1)

    walk(0, [], 0)
    rules.sort(key=lambda r: (-r["error_rate"], -r["n_matched"]))
    return rules


def compute_error_analysis(
    db_engine,
    model_id: int,
    parameter: str = "100_abs",
    kind: str = "both",
    split_kind: str = "test",
    max_depth: int = 3,
    min_samples_leaf: int = 30,
    random_seed: int = 0,
) -> int:
    """Fit per-date error trees and persist their rules. Returns rules written.

    Also persists linear per-entity contributions for the selected entities when the
    primary estimator exposes ``coef_`` (skipped otherwise — tree importances have no
    faithful per-entity decomposition).
    """
    from sklearn.tree import DecisionTreeClassifier

    kinds = ("fp", "fn") if kind == "both" else (kind,)
    if any(k not in ("fp", "fn") for k in kinds):
        raise ValueError(f"unknown error kind {kind!r} — expected fp | fn | both")

    ctx = load_matrix_context(db_engine, model_id, split_kind)
    if ctx.label_timespan is None:
        raise ValueError(
            f"matrix {ctx.matrix_uuid} carries no label_timespan — error analysis"
            " needs labeled predictions"
        )
    written = 0
    dates = sorted({str(d) for d in ctx.frame["as_of_date"].cast(str).to_list()})
    for date in dates:
        labeled = _labeled_ranks(
            db_engine, model_id, split_kind, date, ctx.label_timespan
        )
        if not labeled:
            logger.warning(f"model {model_id}: no labeled ranks at {date} — skipping")
            continue
        _, k = top_k_entities(db_engine, model_id, split_kind, date, parameter)
        day = ctx.frame.filter(ctx.frame["as_of_date"].cast(str) == str(date))
        by_entity = {int(e): i for i, e in enumerate(day["entity_id"].to_list())}
        x = day.select(ctx.feature_names).to_numpy()

        for error_kind in kinds:
            if error_kind == "fp":
                population = [r for r in labeled if r["rank_abs"] <= k]
                is_error = [r["outcome"] == 0 for r in population]
            else:
                population = [r for r in labeled if r["rank_abs"] > k]
                is_error = [r["outcome"] > 0 for r in population]
            idx = [
                by_entity[int(r["entity_id"])]
                for r in population
                if int(r["entity_id"]) in by_entity
            ]
            if len(idx) != len(population):
                logger.warning(
                    f"model {model_id} @ {date}: {len(population) - len(idx)} labeled"
                    " entities missing from the matrix — analyzing the intersection"
                )
                is_error = [
                    e
                    for r, e in zip(population, is_error)
                    if int(r["entity_id"]) in by_entity
                ]
            if len(idx) < 2 or len(set(is_error)) < 2:
                logger.info(
                    f"model {model_id} @ {date}: {error_kind} population too small or"
                    " single-class — no tree"
                )
                continue
            tree = DecisionTreeClassifier(
                max_depth=max_depth,
                min_samples_leaf=min_samples_leaf,
                random_state=random_seed,
            )
            tree.fit(x[idx], [int(e) for e in is_error])
            rules = _rules_from_tree(tree, ctx.feature_names)
            with db_engine.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "delete from triage.error_analysis"
                    " where model_id = %(m)s"
                    "   and split_kind = cast(%(s)s as triage.split_kind)"
                    "   and as_of_date = %(d)s and parameter = %(p)s"
                    "   and error_kind = %(ek)s",
                    {
                        "m": model_id,
                        "s": split_kind,
                        "d": date,
                        "p": parameter,
                        "ek": error_kind,
                    },
                )
                cur.executemany(
                    "insert into triage.error_analysis"
                    " (model_id, split_kind, as_of_date, parameter, error_kind,"
                    "  rule_id, rule, n_matched, n_errors, error_rate, depth)"
                    " values (%(m)s, cast(%(s)s as triage.split_kind), %(d)s, %(p)s,"
                    "         %(ek)s, %(rid)s, %(rule)s, %(n)s, %(ne)s, %(er)s, %(dep)s)",
                    [
                        {
                            "m": model_id,
                            "s": split_kind,
                            "d": date,
                            "p": parameter,
                            "ek": error_kind,
                            "rid": i,
                            "rule": r["rule"],
                            "n": r["n_matched"],
                            "ne": r["n_errors"],
                            "er": r["error_rate"],
                            "dep": r["depth"],
                        }
                        for i, r in enumerate(rules)
                    ],
                )
            written += len(rules)
            logger.info(
                f"error analysis: model {model_id} @ {date} [{error_kind}] —"
                f" {len(rules)} rule(s)"
            )

    written_contribs = _persist_linear_contributions(
        db_engine, model_id, split_kind, parameter, ctx
    )
    if written_contribs:
        logger.info(
            f"individual importances: model {model_id} — {written_contribs} row(s)"
            " (linear_contrib)"
        )
    return written


def _persist_linear_contributions(
    db_engine, model_id, split_kind, parameter, ctx
) -> int:
    """β·x per selected entity for linear estimators → individual_importances."""
    import numpy as np

    with db_engine.connection() as conn:
        row = conn.execute(
            "select artifact_uri from triage.models where model_id = %(m)s",
            {"m": model_id},
        ).fetchone()
    if row is None or not row["artifact_uri"]:
        return 0
    from triage.adapters.model import _load_estimator

    try:
        estimator = _load_estimator(row["artifact_uri"])
    except (
        Exception
    ) as exc:  # artifact GC'd / unreadable — diagnostic degrades, run goes on
        logger.warning(
            f"model {model_id}: cannot load estimator ({exc}) — no contributions"
        )
        return 0
    coef = getattr(estimator, "coef_", None)
    if coef is None:
        return 0
    coef = np.asarray(coef).ravel()
    if coef.shape[0] != len(ctx.feature_names):
        logger.warning(
            f"model {model_id}: coef_ has {coef.shape[0]} terms vs"
            f" {len(ctx.feature_names)} features — no contributions"
        )
        return 0

    written = 0
    dates = sorted({str(d) for d in ctx.frame["as_of_date"].cast(str).to_list()})
    for date in dates:
        selected, _ = top_k_entities(db_engine, model_id, split_kind, date, parameter)
        if not selected:
            continue
        day = ctx.frame.filter(ctx.frame["as_of_date"].cast(str) == str(date))
        sel = day.filter(day["entity_id"].is_in(list(selected)))
        if sel.height == 0:
            continue
        x = sel.select(ctx.feature_names).to_numpy().astype(float)
        entities = sel["entity_id"].to_list()
        contribs = x * coef  # (n_entities, n_features)
        params: list[dict[str, Any]] = []
        for i, entity in enumerate(entities):
            order = np.argsort(-np.abs(contribs[i]))[:_TOP_CONTRIBUTIONS]
            for j in order:
                params.append(
                    {
                        "m": model_id,
                        "e": int(entity),
                        "d": date,
                        "f": ctx.feature_names[int(j)],
                        "fv": float(x[i, int(j)]),
                        "sc": float(contribs[i, int(j)]),
                    }
                )
        with db_engine.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "insert into triage.individual_importances"
                " (model_id, entity_id, as_of_date, feature, method,"
                "  feature_value, importance_score)"
                " values (%(m)s, %(e)s, %(d)s, %(f)s, 'linear_contrib', %(fv)s, %(sc)s)"
                " on conflict (model_id, entity_id, as_of_date, feature, method)"
                " do update set feature_value = excluded.feature_value,"
                "               importance_score = excluded.importance_score",
                params,
            )
        written += len(params)
    return written
