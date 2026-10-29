"""Error analysis — "predict on the errors to learn" (plan P5, the DSSG error tree).

A shallow, interpretable DecisionTree is fitted on the primary model's MISTAKES at the
top-k cut and its leaf paths become human-readable rules:

* ``fp`` — within the SELECTED population, target = (outcome = 0): where does the
  model over-select?
* ``fn`` — within the PASSED-OVER population, target = (outcome = 1): where does it
  miss need?
* ``confusion`` — over ALL labeled rows, target = the row's class at the cut (TP / FP /
  FN / TN). It is not conditioned on selection, so its first splits mostly redraw the
  selection boundary; the leaves below them are where it says something new.

Each leaf is persisted with its rule text, its class counts, its path as structured
``conditions`` and a threshold-free ``signature`` (migration 0027), so the same rule can be
recognised on another test date (``triage.error_rule_recurrence``) and the leaves that are
suspiciously right can be listed (``triage.error_rules_suspicious``).

This is a diagnostic, never a score modifier (stacking is deliberately out of scope —
see the plan's Questionables): the rules feed feature/cohort work, not predictions.
When the primary model is linear, per-entity β·x contributions for the selected
entities are persisted into ``individual_importances`` (method ``linear_contrib``) —
the "why this entity" surface.
"""

from __future__ import annotations

import json
from typing import Any

from triage.diagnostics.matrixio import load_matrix_context, top_k_entities
from triage.logging import get_logger

logger = get_logger(__name__)

_TOP_CONTRIBUTIONS = 10

#: The trees each ``kind`` argument fits; ``both`` keeps its pre-0027 meaning.
ERROR_TREE_KINDS: dict[str, tuple[str, ...]] = {
    "fp": ("fp",),
    "fn": ("fn",),
    "confusion": ("confusion",),
    "both": ("fp", "fn"),
    "all": ("fp", "fn", "confusion"),
}

#: The confusion tree's target classes, in the order its leaves report them.
CONFUSION_CLASSES = ("tp", "fp", "fn", "tn")

#: What the binary trees' two labels stand for: label 1 is the error class.
_BINARY_CLASSES = {"fp": {1: "fp", 0: "tp"}, "fn": {1: "fn", 0: "tn"}}


def _labeled_ranks(db_engine, model_id, split_kind, date, timespan):
    """The labeled rows, each with its rank among ALL scored rows (ADR-0033).

    ``top_k_entities``' k is a cut over the whole population, so the rank compared with it
    must be a population rank too — a rank among labeled rows only would shift the cut
    wherever unlabeled rows sit above it.
    """
    with db_engine.connection() as conn:
        return conn.execute(
            "select entity_id, rank_abs, outcome"
            " from triage.population_ranks(%(m)s, cast(%(s)s as triage.split_kind),"
            "        cast(%(d)s as date), cast(%(t)s as interval))"
            " where outcome is not null",
            {"m": model_id, "s": split_kind, "d": str(date), "t": timespan},
        ).fetchall()


def _leaf_counts(tree, node: int) -> dict[Any, int]:
    """Rows per class in one leaf, keyed by the class labels the tree was fitted on.

    sklearn >= 1.4 stores class PROPORTIONS in ``tree_.value`` (older: counts); taking n
    from ``n_node_samples`` and each class's share as a ratio is invariant to both, and
    rounds back to the exact integer counts, so they sum to n.
    """
    t = tree.tree_
    value_row = t.value[node][0]
    n = int(t.n_node_samples[node])
    total = float(value_row.sum())
    return {
        (cls.item() if hasattr(cls, "item") else cls): (
            int(round(float(v) / total * n)) if total > 0 else 0
        )
        for cls, v in zip(tree.classes_, value_row)
    }


def signature_of(conditions: list[dict[str, Any]]) -> str:
    """A rule's threshold-free identity: its distinct ``feature op`` pairs, sorted.

    Sorting makes it independent of the order the tree split on the features, and dropping
    the thresholds lets the same rule match on another date, where they move. A path that
    splits one feature twice the same way (``a <= 3 AND a <= 1``) has the signature of one
    split. The root-only tree has no conditions and the signature ``''``.
    """
    return "|".join(sorted({f"{c['feature']}{c['op']}" for c in conditions}))


def _rules_from_tree(
    tree, feature_names: list[str], error_kind: str
) -> list[dict[str, Any]]:
    """Flatten a fitted DecisionTreeClassifier into one rule per leaf.

    The binary ``fp``/``fn`` trees are fitted with label 1 = error; the ``confusion`` tree
    with the labels of ``CONFUSION_CLASSES``. ``n_errors`` counts the error class of a
    binary tree, and FP + FN for the confusion tree (the rows the model gets wrong at the
    cut). Each rule also carries ``class_counts``, its path as ``conditions`` and its
    ``signature``. Rules come back ordered by error_rate descending (the highest-lift
    failure modes first).
    """
    t = tree.tree_
    rules: list[dict[str, Any]] = []

    def walk(node: int, conditions: list[dict[str, Any]], depth: int) -> None:
        if t.children_left[node] == -1:  # leaf
            n = int(t.n_node_samples[node])
            counts = _leaf_counts(tree, node)
            if error_kind == "confusion":
                class_counts = {c: counts.get(c, 0) for c in CONFUSION_CLASSES}
                n_err = class_counts["fp"] + class_counts["fn"]
            else:
                names = _BINARY_CLASSES[error_kind]
                class_counts = {names[1]: counts.get(1, 0), names[0]: counts.get(0, 0)}
                n_err = counts.get(1, 0)
            rule = " AND ".join(
                f"{c['feature']} {c['op']} {c['threshold']:.4g}" for c in conditions
            )
            rules.append(
                {
                    "rule": rule or "(all)",
                    "n_matched": n,
                    "n_errors": n_err,
                    "error_rate": (n_err / n) if n else 0.0,
                    "depth": depth,
                    "class_counts": class_counts,
                    "conditions": conditions,
                    "signature": signature_of(conditions),
                }
            )
            return
        feature = feature_names[t.feature[node]]
        threshold = float(t.threshold[node])
        for child, op in ((t.children_left[node], "<="), (t.children_right[node], ">")):
            step = {"feature": feature, "op": op, "threshold": threshold}
            walk(child, [*conditions, step], depth + 1)

    walk(0, [], 0)
    rules.sort(key=lambda r: (-r["error_rate"], -r["n_matched"]))
    return rules


def _tree_population(
    error_kind: str, labeled: list[Any], k: int
) -> tuple[list[Any], list[Any]]:
    """The rows one tree is fitted on, and each row's target label.

    fp: the selected rows (rank ≤ k), 1 where the outcome is 0. fn: the passed-over rows,
    1 where the outcome is positive. confusion: every labeled row, labeled with its class
    at the cut.
    """
    if error_kind == "fp":
        rows = [r for r in labeled if r["rank_abs"] <= k]
        return rows, [int(r["outcome"] == 0) for r in rows]
    if error_kind == "fn":
        rows = [r for r in labeled if r["rank_abs"] > k]
        return rows, [int(r["outcome"] > 0) for r in rows]
    targets = []
    for r in labeled:
        selected, positive = r["rank_abs"] <= k, r["outcome"] > 0
        if selected:
            targets.append("tp" if positive else "fp")
        else:
            targets.append("fn" if positive else "tn")
    return labeled, targets


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

    ``kind`` picks the trees: ``fp``, ``fn`` or ``confusion`` alone, ``both`` (fp + fn,
    its meaning before the confusion tree existed) or ``all`` (fp + fn + confusion).
    Re-running replaces a date's rows of each kind fitted, so rows written before
    migration 0027 gain their class counts, conditions and signature.

    Also persists linear per-entity contributions for the selected entities when the
    primary estimator exposes ``coef_`` (skipped otherwise — tree importances have no
    faithful per-entity decomposition).
    """
    from sklearn.tree import DecisionTreeClassifier

    kinds = ERROR_TREE_KINDS.get(kind)
    if kinds is None:
        raise ValueError(
            f"unknown error kind {kind!r} — expected one of"
            f" {' | '.join(ERROR_TREE_KINDS)}"
        )

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
            population, targets = _tree_population(error_kind, labeled, k)
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
                targets = [
                    y
                    for r, y in zip(population, targets)
                    if int(r["entity_id"]) in by_entity
                ]
            if len(idx) < 2 or len(set(targets)) < 2:
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
            tree.fit(x[idx], targets)
            rules = _rules_from_tree(tree, ctx.feature_names, error_kind)
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
                    "  rule_id, rule, n_matched, n_errors, error_rate, depth,"
                    "  class_counts, conditions, signature)"
                    " values (%(m)s, cast(%(s)s as triage.split_kind), %(d)s, %(p)s,"
                    "         %(ek)s, %(rid)s, %(rule)s, %(n)s, %(ne)s, %(er)s, %(dep)s,"
                    "         cast(%(cc)s as jsonb), cast(%(cond)s as jsonb), %(sig)s)",
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
                            "cc": json.dumps(r["class_counts"]),
                            "cond": json.dumps(r["conditions"]),
                            "sig": r["signature"],
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
