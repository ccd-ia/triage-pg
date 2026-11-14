"""Checks on a fitted model itself, not on its errors.

* :func:`leaf_support` — for scikit-learn trees and their ensembles, the training rows each
  leaf was fitted on. A leaf fitted on one row forecasts that row's outcome for every entity
  that lands in it. Read from the joblib file alone (``tree_.n_node_samples`` and
  ``tree_.value``); estimators that do not expose ``tree_`` (HistGradientBoosting, LightGBM)
  are reported as unsupported, and no private attribute is read.
* :func:`mean_shift` — for a linear forecast ``ŷ = b + Σ β_j x_j``, the change of the mean
  forecast from the train matrix to the test matrix splits exactly into
  ``Σ β_j (x̄_test,j − x̄_train,j)``. The reconstruction is checked against the stored test
  predictions.
* :func:`family_of` — the source column a featurizer feature is built from, so importances and
  contributions can be summed per column.
* :func:`importance_by_family` and :func:`sensitivity` — SQL reads over
  ``triage.feature_importances`` and ``triage.evaluations``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, LiteralString, Optional, cast

import numpy as np

from triage.util.db import DictRowPool

#: The tree estimators whose leaves :func:`leaf_support` reads, by class-name prefix.
TREE_PREFIXES = (
    "DecisionTree",
    "ExtraTree",
    "RandomForest",
    "ExtraTrees",
    "GradientBoosting",
)

# An aggregation or transform wrapper: an upper-case primitive, optionally behind the
# entity it aggregates to (``facilities.COUNT(``). A one-hot value may hold parentheses of
# its own (``facility_type=daycare (2 - 6 years)``), which this does not match.
_AGG = re.compile(r"^(?:\w+\.)*[A-Z][A-Z0-9_]*\((.*)\)$")


class StructureUnavailable(LookupError):
    """The estimator does not expose what the check reads."""


# ------------------------------------------------------------------------- families


def family_of(name: str, labels: Optional[Mapping[str, str]] = None) -> str:
    """The source column a featurizer feature is built from.

    ``ABS(facilities.COUNT(inspections.date|interval=P1M))`` → ``inspections.date``: the
    aggregations are peeled from the outside in and the ``|`` options dropped.
    ``facilities.zip_code=60622`` → ``facilities.zip_code``, and ``<feature>__missing`` is
    the family of ``<feature>``. A name truncated at PostgreSQL's 63 bytes is first resolved
    to its full label through ``labels`` (featurizer's manifest, :func:`feature_labels`).
    """
    full = labels.get(name, name) if labels else name
    if full.endswith("__missing"):
        full = full[: -len("__missing")]
        full = labels.get(full, full) if labels else full
    while (match := _AGG.match(full)) is not None:
        full = match.group(1)
    full = full.split("|", 1)[0]
    return full.split("=", 1)[0]


def feature_labels(experiment_config: Mapping[str, Any]) -> dict[str, str]:
    """Physical column → full label, from an Experiment's ``feature_config`` (no database)."""
    from triage.adapters.matrix import feature_labels as labels_of
    from triage.adapters.run import _featurizer_only

    feature_config = experiment_config.get("feature_config")
    if not feature_config:
        return {}
    return labels_of(_featurizer_only(feature_config))


def _experiment_labels(pool: DictRowPool, model_id: int) -> dict[str, str]:
    """The feature labels of the model's Experiment, or ``{}`` when it has no config."""
    with pool.connection() as conn:
        row = conn.execute(
            "select e.config from triage.models m join triage.runs r using (run_id)"
            " join triage.experiments e using (experiment_hash) where m.model_id = %(m)s",
            {"m": model_id},
        ).fetchone()
    if row is None or not row["config"]:
        return {}
    try:
        return feature_labels(row["config"])
    except (KeyError, TypeError, ValueError):
        return {}  # a hand-written or partial config: the physical names stand


# --------------------------------------------------------------------------- loading


def _model_row(pool: DictRowPool, model_id: int) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "select m.model_id, m.artifact_uri, m.feature_list,"
            "       m.train_matrix_uuid::text as train_matrix_uuid, mg.model_type"
            " from triage.models m"
            " join triage.model_groups mg on mg.model_group_id = m.model_group_id"
            " where m.model_id = %(m)s",
            {"m": model_id},
        ).fetchone()
    if row is None:
        raise ValueError(f"No model with id {model_id}")
    return row


def load_estimator(pool: DictRowPool, model_id: int) -> Any:
    """The fitted estimator of a model, from its joblib file.

    Raises:
        ValueError: the model has no artifact_uri.
    """
    from triage.adapters.model import _load_estimator

    row = _model_row(pool, model_id)
    if not row["artifact_uri"]:
        raise ValueError(
            f"model {model_id} has no artifact_uri — its estimator was never stored"
        )
    return _load_estimator(row["artifact_uri"])


# ---------------------------------------------------------------------- leaf support


def _trees(estimator: Any) -> list[Any]:
    name = type(estimator).__name__
    if not name.startswith(TREE_PREFIXES):
        raise StructureUnavailable(f"leaf support not available for {name}")
    if hasattr(estimator, "tree_"):
        return [estimator]
    trees = np.asarray(getattr(estimator, "estimators_", []), dtype=object).ravel()
    found = [t for t in trees if hasattr(t, "tree_")]
    if not found:
        raise StructureUnavailable(f"leaf support not available for {name}")
    return found


def _leaf_value(tree: Any, node: int) -> float:
    """A leaf's forecast: its value for a regressor, its positive share for a classifier."""
    value = np.asarray(tree.tree_.value[node])[0]
    if value.shape[0] == 1:
        return float(value[0])
    return float(value[-1] / value.sum()) if value.sum() else 0.0


def leaf_support(estimator: Any, min_rows: int = 2, limit: int = 20) -> dict[str, Any]:
    """The training rows behind each leaf of a tree or tree ensemble.

    Args:
        estimator: a fitted scikit-learn tree, forest, extra-trees or gradient-boosting
            estimator.
        min_rows: a leaf fitted on fewer rows is reported.
        limit: the most small leaves listed.

    Returns:
        ``estimator``, ``n_trees``, ``leaves`` (total), ``leaves_per_tree`` (min, mean,
        max), ``small_leaves`` (the count below ``min_rows``), ``trees_with_small_leaf``
        and ``small`` — rows ``tree, node, n_rows, value`` sorted by value, largest first.
        A gradient-boosting leaf value is the stage's raw output, not a forecast.

    Raises:
        StructureUnavailable: the estimator exposes no ``tree_``.
    """
    trees = _trees(estimator)
    per_tree: list[int] = []
    small: list[dict[str, Any]] = []
    with_small = 0
    for i, tree in enumerate(trees):
        t = tree.tree_
        leaves = np.flatnonzero(np.asarray(t.children_left) == -1)
        per_tree.append(len(leaves))
        counts = np.asarray(t.n_node_samples)[leaves]
        found = leaves[counts < min_rows]
        if len(found):
            with_small += 1
        small += [
            {
                "tree": i,
                "node": int(node),
                "n_rows": int(t.n_node_samples[node]),
                "value": _leaf_value(tree, int(node)),
            }
            for node in found
        ]
    small.sort(key=lambda r: (-r["value"], r["tree"], r["node"]))
    return {
        "estimator": type(estimator).__name__,
        "min_rows": min_rows,
        "n_trees": len(trees),
        "leaves": int(sum(per_tree)),
        "leaves_per_tree": {
            "min": int(min(per_tree)),
            "mean": float(np.mean(per_tree)),
            "max": int(max(per_tree)),
        },
        "small_leaves": len(small),
        "trees_with_small_leaf": with_small,
        "small": small[:limit],
    }


# ---------------------------------------------------------------------- linear shift


def _matrix_means(storage_uri: str, columns: Sequence[str]) -> tuple[np.ndarray, int]:
    from triage.profiles.storage import read_parquet, storage_for_root

    frame = read_parquet(storage_for_root(storage_uri), storage_uri)
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValueError(f"matrix {storage_uri} lacks feature column(s) {missing[:3]}")
    x = frame.select(list(columns)).to_numpy().astype(float)
    return x.mean(axis=0), x.shape[0]


def mean_shift(
    pool: DictRowPool, model_id: int, estimator: Any = None
) -> dict[str, Any]:
    """Split the change of a linear model's mean forecast, train to test, by feature.

    ``mean(ŷ_test) − mean(ŷ_train) = Σ β_j (x̄_test,j − x̄_train,j)``, exact for a forecast
    linear in its features. The means come from the train matrix and the scored test
    matrix; the reconstructed mean test forecast is compared with the mean of the stored test
    predictions.

    Returns:
        ``estimator``, ``intercept``, ``mean_forecast_train`` and ``mean_forecast_test``
        (both reconstructed), ``stored_mean_test`` and ``check_error`` (their gap),
        ``shift``, ``features`` (rows ``feature, family, beta, mean_train, mean_test,
        contribution``, the largest |contribution| first) and ``families`` (rows ``family,
        n_features, contribution, share``, with share of Σ|contribution|).

    Raises:
        StructureUnavailable: the estimator has no ``coef_`` and ``intercept_``, or forecasts
            through a link (``predict_proba``).
        ValueError: no train matrix or no test predictions.
    """
    from triage.diagnostics.matrixio import load_matrix_context

    estimator = estimator if estimator is not None else load_estimator(pool, model_id)
    name = type(estimator).__name__
    if not (hasattr(estimator, "coef_") and hasattr(estimator, "intercept_")):
        raise StructureUnavailable(f"{name} has no coef_ and intercept_: not linear")
    if hasattr(estimator, "predict_proba"):
        raise StructureUnavailable(
            f"{name} forecasts through a link (predict_proba): the mean shift splits"
            " exactly only for a forecast linear in its features"
        )
    coef = np.asarray(estimator.coef_, dtype=float).ravel()
    intercept = float(np.asarray(estimator.intercept_, dtype=float).ravel()[0])
    row = _model_row(pool, model_id)
    features = list(row["feature_list"] or [])
    if len(features) != coef.shape[0]:
        raise ValueError(
            f"model {model_id}: coef_ has {coef.shape[0]} terms for"
            f" {len(features)} features in models.feature_list"
        )
    with pool.connection() as conn:
        train = conn.execute(
            "select storage_uri from triage.matrices where matrix_uuid = %(u)s::uuid",
            {"u": row["train_matrix_uuid"]},
        ).fetchone()
        stored = conn.execute(
            "select avg(score) as mean, count(*) as n from triage.latest_predictions"
            " where model_id = %(m)s and split_kind = 'test'",
            {"m": model_id},
        ).fetchone()
    if train is None:
        raise ValueError(f"model {model_id} has no train matrix row")
    mean_train, _ = _matrix_means(train["storage_uri"], features)
    test_ctx = load_matrix_context(pool, model_id, "test")
    x_test = test_ctx.frame.select(features).to_numpy().astype(float)
    mean_test = x_test.mean(axis=0)
    contribution = coef * (mean_test - mean_train)
    labels = _experiment_labels(pool, model_id)
    rows: list[dict[str, Any]] = [
        {
            "feature": f,
            "family": family_of(f, labels),
            "beta": float(b),
            "mean_train": float(a),
            "mean_test": float(t),
            "contribution": float(c),
        }
        for f, b, a, t, c in zip(
            features, coef, mean_train, mean_test, contribution, strict=True
        )
    ]
    rows.sort(key=lambda r: (-abs(r["contribution"]), r["feature"]))
    families: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        families[r["family"]].append(r["contribution"])
    total = float(np.abs(contribution).sum())
    family_rows: list[dict[str, Any]] = sorted(
        (
            {
                "family": fam,
                "n_features": len(cs),
                "contribution": float(sum(cs)),
                "share": float(sum(abs(c) for c in cs)) / total if total else None,
            }
            for fam, cs in families.items()
        ),
        key=lambda r: (-abs(r["contribution"]), r["family"]),
    )
    forecast_test = intercept + float(coef @ mean_test)
    stored_mean = None if stored is None else stored["mean"]
    return {
        "estimator": name,
        "intercept": intercept,
        "mean_forecast_train": intercept + float(coef @ mean_train),
        "mean_forecast_test": forecast_test,
        "stored_mean_test": stored_mean,
        "check_error": None
        if stored_mean is None
        else abs(forecast_test - stored_mean),
        "shift": float(contribution.sum()),
        "features": rows,
        "families": family_rows,
    }


# ---------------------------------------------------------------- importance, sensitivity


def importance_by_family(
    pool: DictRowPool, model_group_id: int, top: int = 5
) -> dict[str, Any]:
    """Each feature family's share of a group's |importance|, and how stable the top is.

    Returns:
        ``models`` — rows ``model_id, train_end_time, families`` (family → share of the
        model's Σ|importance|) — in training order, and ``stability`` — rows
        ``model_id, previous_model_id, jaccard`` of the ``top`` features by |importance|
        between consecutive train ends.
    """
    with pool.connection() as conn:
        rows = conn.execute(
            "select m.model_id, m.train_end_time, fi.feature,"
            "       abs(fi.feature_importance) as importance"
            " from triage.models m"
            " join triage.feature_importances fi on fi.model_id = m.model_id"
            " where m.model_group_id = %(g)s and fi.feature_importance is not null"
            " order by m.train_end_time, m.model_id, fi.feature",
            {"g": model_group_id},
        ).fetchall()
    labels = _experiment_labels(pool, rows[0]["model_id"]) if rows else {}
    by_model: dict[int, dict[str, Any]] = {}
    for r in rows:
        entry = by_model.setdefault(
            r["model_id"],
            {"model_id": r["model_id"], "train_end_time": r["train_end_time"], "f": {}},
        )
        entry["f"][r["feature"]] = float(r["importance"])
    models = []
    tops: list[tuple[int, set[str]]] = []
    for entry in by_model.values():
        total = sum(entry["f"].values())
        families: dict[str, float] = defaultdict(float)
        for feature, value in entry["f"].items():
            families[family_of(feature, labels)] += value / total if total else 0.0
        models.append(
            {
                "model_id": entry["model_id"],
                "train_end_time": entry["train_end_time"],
                "families": dict(sorted(families.items(), key=lambda kv: -kv[1])),
            }
        )
        ranked = sorted(entry["f"].items(), key=lambda kv: (-kv[1], kv[0]))
        tops.append((entry["model_id"], {f for f, _ in ranked[:top]}))
    stability = [
        {
            "model_id": m,
            "previous_model_id": p,
            "jaccard": len(a & b) / len(a | b) if a | b else None,
        }
        for (p, b), (m, a) in zip(tops, tops[1:])
    ]
    return {"model_group_id": model_group_id, "top": top, "models": models,
            "stability": stability}  # fmt: skip


def sensitivity(
    pool: DictRowPool,
    experiment_hash: str,
    param: str,
    metric: str = "mae",
    parameter: str = "",
    model_type: Optional[str] = None,
    common_rows: bool = False,
) -> dict[str, Any]:
    """The mean metric over test dates for each value of one hyperparameter.

    The groups compared are those of one ``model_type`` in the Experiment whose
    hyperparameters differ only in ``param``.

    Raises:
        ValueError: no group sets ``param``, several model types do (pass ``model_type``),
            or the groups also differ in another hyperparameter.
    """
    import json

    with pool.connection() as conn:
        groups = conn.execute(
            "select distinct mg.model_group_id, mg.model_type, mg.hyperparameters"
            " from triage.model_groups mg"
            " join triage.models m on m.model_group_id = mg.model_group_id"
            " join triage.runs r on r.run_id = m.run_id"
            " where r.experiment_hash = %(h)s and mg.hyperparameters ? %(p)s"
            "   and (%(t)s::text is null or mg.model_type = %(t)s)"
            " order by mg.model_group_id",
            {"h": experiment_hash, "p": param, "t": model_type},
        ).fetchall()
    if not groups:
        raise ValueError(
            f"no model group of experiment {experiment_hash[:12]} sets {param!r}"
            + (f" for {model_type}" if model_type else "")
        )
    types = sorted({g["model_type"] for g in groups})
    if len(types) > 1:
        raise ValueError(
            f"{param!r} is set by {len(types)} model types ({', '.join(types)}) — pass"
            " --model-type"
        )
    rest = {
        g["model_group_id"]: json.dumps(
            {k: v for k, v in g["hyperparameters"].items() if k != param},
            sort_keys=True,
        )
        for g in groups
    }
    if len(set(rest.values())) > 1:
        keys = sorted(
            {
                k
                for g in groups
                for k, v in g["hyperparameters"].items()
                if k != param
                and len({json.dumps(h["hyperparameters"].get(k)) for h in groups}) > 1
            }
        )
        raise ValueError(
            f"the {types[0]} groups differ in {', '.join(keys)} as well as {param!r} —"
            " sensitivity compares groups that differ in one hyperparameter only"
        )
    ids = [g["model_group_id"] for g in groups]
    subset_hash = ""
    if common_rows:
        from triage.adapters.common_rows import evaluate_on_common_rows

        subset_hash = evaluate_on_common_rows(pool, experiment_hash, ids).subset_hash
    with pool.connection() as conn:
        rows = conn.execute(
            cast(
                LiteralString,
                "select m.model_group_id, avg(e.value) as mean_value,"
                "       count(distinct e.as_of_date) as n_dates"
                " from triage.evaluations e"
                " join triage.models m on m.model_id = e.model_id"
                " join triage.runs r on r.run_id = m.run_id"
                " where r.experiment_hash = %(h)s and m.model_group_id = any(%(g)s)"
                "   and e.split_kind = 'test' and e.metric = %(metric)s"
                "   and e.parameter = %(parameter)s and e.subset_hash = %(s)s"
                " group by m.model_group_id",
            ),
            {
                "h": experiment_hash,
                "g": ids,
                "metric": metric,
                "parameter": parameter,
                "s": subset_hash,
            },  # fmt: skip
        ).fetchall()
    values = {g["model_group_id"]: g["hyperparameters"][param] for g in groups}
    found = {r["model_group_id"]: r for r in rows}
    out = sorted(
        (
            {
                "model_group_id": gid,
                "value": values[gid],
                "mean_metric": found[gid]["mean_value"] if gid in found else None,
                "n_dates": found[gid]["n_dates"] if gid in found else 0,
            }
            for gid in ids
        ),
        key=lambda r: (str(type(r["value"])), r["value"]),
    )
    return {
        "experiment_hash": experiment_hash,
        "model_type": types[0],
        "param": param,
        "metric": metric,
        "parameter": parameter,
        "subset_hash": subset_hash,
        "rows": out,
    }
