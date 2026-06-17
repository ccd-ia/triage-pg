"""Greenfield model builder — train → predict → evaluate on the artifact DAG (ADR-0011, ADR-0016).

This is the last adapter in the cohort → labels → matrix → model chain. It turns a built
*train matrix* into a fitted estimator registered in the artifact DAG + ``triage.models``,
then scores a *test matrix* (append-only, ADR-0006) and evaluates it in-Postgres (ADR-0007).
It mirrors :mod:`triage.adapters.matrix` (F2) lifecycle and style; it does **not** touch the
inherited ``catwalk/model_trainers.py`` path (slated for removal under the ADRs).

The model node and its identity (ADR-0016)
------------------------------------------
A model artifact's identity hashes over ``{class_path, hyperparameters, random_seed}`` plus
its single parent (the train matrix) plus the *estimator library's version*. The estimator
version enters through ``engine_versions_for('model', class_path)``, which resolves the
class path's top-level module to its installed distribution and pins that version — so a
scikit-learn bump rebuilds models even though triage-pg's own code did not change. The train
matrix is the only DAG parent; cohort/labels/feature-group lineage flows transitively through
it. ``models.model_hash`` is the model artifact_id; ``models.train_matrix_uuid`` is
``as_uuid(train_matrix_artifact_id)``.

Model groups (the comparison axis)
----------------------------------
A *model group* is the comparable family across temporal splits: same estimator class, same
hyperparameters, same feature list. ``model_group_hash`` is a stable hash over exactly those
three; SELECT-or-INSERT means a second model of the same family (e.g. the next split) reuses
the existing ``model_group_id`` rather than minting a new one. ``random_seed`` and the train
matrix are NOT part of the group identity (they vary within a group); they are part of the
*model* identity.

Lifecycle (mirrors :mod:`triage.adapters.matrix`)
-------------------------------------------------
``derive`` → ``cache_hit`` (reuse the existing model + its ``model_id`` on a hit) →
``begin_artifact`` → load train matrix Parquet → fit → serialize (joblib) → SELECT-or-INSERT
model group → INSERT ``triage.models`` → persist feature importances → ``mark_built`` →
``record_use``, with ``mark_failed`` + re-raise on any error (fail fast).

Predict + evaluate (a sibling entry point)
------------------------------------------
:func:`score_and_evaluate` loads a *test* matrix Parquet, calls the fitted estimator
(``predict_proba`` for classification, else ``predict``/``decision_function``), builds
``scores=[{entity_id, as_of_date, score}]`` from the matrix keys, and appends them through
:func:`triage.component.catwalk.prediction_ranking.record_predictions` (append-only,
ADR-0006). It then drives in-PG evaluation
(:func:`triage.component.catwalk.in_pg_evaluation.evaluate_in_db`) and, optionally, bias
metrics. Predictions are never overwritten — re-scoring appends rows with a later
``scored_at``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from triage.adapters.matrix import MatrixResult
from triage.artifacts import (
    begin_artifact,
    cache_hit,
    get_artifact,
    mark_built,
    mark_failed,
    record_use,
)
from triage.component.catwalk.in_pg_evaluation import (
    DEFAULT_CLASSIFICATION_CONFIG,
    compute_bias_in_db,
    evaluate_in_db,
)
from triage.component.catwalk.prediction_ranking import record_predictions
from triage.derivation import Derivation, as_uuid, derive, engine_versions_for
from triage.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "build_model",
    "score_and_evaluate",
    "ModelResult",
    "ScoreEvaluateResult",
    "MODEL_KIND",
]

MODEL_KIND = "model"

# The non-feature columns the F2 design matrix carries alongside the feature columns:
# the two keys plus the label triple (ADR-0010). MatrixResult.feature_names is the
# authoritative feature list, so we select X by that list rather than by exclusion — but we
# keep this set documented for the label/key reads below.
_KEY_COLS = ("entity_id", "as_of_date")
_LABEL_COLS = ("outcome", "duration", "event_observed")


@dataclass(frozen=True)
class ModelResult:
    """What :func:`build_model` returns: the model node + its DB ids and the fitted estimator."""

    model_artifact_id: str
    model_id: int
    model_group_id: int
    model_group_hash: str
    artifact_uri: str
    feature_names: list[str]
    estimator: Any
    cache_hit: bool


@dataclass(frozen=True)
class ScoreEvaluateResult:
    """What :func:`score_and_evaluate` returns: prediction + evaluation row counts."""

    model_id: int
    num_predictions: int
    num_evaluations: int
    num_bias_metrics: int = 0
    metric_config: Mapping[str, Any] = field(default_factory=dict)


def _import_estimator(class_path: str):
    """Import an estimator class from its dotted ``module.ClassName`` path."""
    module_path, _, class_name = class_path.rpartition(".")
    if not module_path:
        raise ValueError(f"class_path {class_path!r} must be a dotted 'module.ClassName' path")
    import importlib

    module = importlib.import_module(module_path)
    try:
        return getattr(module, class_name)
    except AttributeError as exc:
        raise ValueError(
            f"estimator class {class_name!r} not found in module {module_path!r}" + f" (from class_path {class_path!r})"
        ) from exc


def _canonical_hyperparameters(hyperparameters: Mapping[str, Any]) -> dict[str, Any]:
    """Hyperparameters with sorted keys for stable identity (the derivation hasher
    sorts keys itself, but we materialize a plain dict so the same object enters both
    the model derivation and the model_group hash)."""
    return {key: hyperparameters[key] for key in sorted(hyperparameters)}


def _model_group_hash(
    class_path: str,
    hyperparameters: Mapping[str, Any],
    feature_list: Sequence[str],
) -> str:
    """A stable hash over the model-group identity: estimator + hyperparameters + features.

    Reuses :func:`triage.derivation.canonical_json` semantics via a small local hash so the
    group hash is independent of the model node's full identity (which also folds in the
    train matrix, the random seed, and engine versions — none of which belong to the group).
    """
    import hashlib

    from triage.derivation import canonical_json

    envelope = {
        "model_type": class_path,
        "hyperparameters": _canonical_hyperparameters(hyperparameters),
        "feature_list": sorted(feature_list),
    }
    return hashlib.sha256(canonical_json(envelope).encode("ascii")).hexdigest()


def _reconstruct_derivation(engine: Engine, artifact_id: str, what: str) -> Derivation:
    """Re-read an upstream artifact and rebuild its Derivation so it can chain.

    Mirrors :func:`triage.adapters.matrix._reconstruct_derivation`.
    """
    row = get_artifact(engine, artifact_id)
    if row is None:
        raise ValueError(
            f"{what} artifact {artifact_id!r} does not exist — build it before the"
            + " model (the model->parent edge requires the parent row)"
        )
    return Derivation(
        id=row["artifact_id"],
        logical_id=row["logical_id"],
        cacheable=row["cacheable"],
    )


def build_model(
    db_engine: Engine,
    run_id: str,
    train_matrix_result: MatrixResult,
    class_path: str,
    hyperparameters: Mapping[str, Any],
    *,
    random_seed: int,
    storage_dir: str,
    train_end_time: Any | None = None,
    training_label_timespan: str | None = None,
    source_pins: Mapping[str, str | None] | None = None,
    policy: str = "exact",
) -> ModelResult:
    """Fit (or reuse) a model from a built train matrix and register it in the artifact DAG.

    Runs the derivation lifecycle: identity over ``{class_path, hyperparameters, random_seed}``
    with the train matrix as the single parent and the estimator library's version folded in
    (ADR-0016). On a cache hit the existing model is reused (its ``model_id`` returned, the
    serialized estimator reloaded from disk); otherwise the estimator is fitted, serialized to
    ``storage_dir`` via joblib, the model group SELECT-or-INSERTed, the ``triage.models`` row
    written, and feature importances persisted (ADR-0011).

    Args:
        db_engine: project-database engine (greenfield ``triage.*`` schema).
        run_id: the owning run; must already exist (FK).
        train_matrix_result: the F2 :class:`MatrixResult` for the *train* matrix — supplies
            the parent artifact id, the Parquet ``storage_uri``, and ``feature_names`` (X).
        class_path: dotted ``module.ClassName`` of the estimator (e.g.
            ``'sklearn.tree.DecisionTreeClassifier'``).
        hyperparameters: estimator constructor kwargs (excluding the seed).
        random_seed: deterministic seed; passed to the estimator as ``random_state`` when it
            accepts one, and stored on ``triage.models.random_seed``. Part of model identity.
        storage_dir: directory the joblib model artifact is written under.
        train_end_time: optional ``triage.models.train_end_time`` (the split's train cut).
        training_label_timespan: optional label horizon the model trained against
            (``triage.models.training_label_timespan``).
        source_pins: declared-source → version pins (volatile if unpinned, ADR-0014). Must
            match the train matrix's pins for the model to be cacheable.
        policy: cache lookup policy ('exact' default).

    Returns:
        A :class:`ModelResult` (model artifact id, ``model_id``, group id/hash, artifact uri,
        feature names, the fitted estimator, and whether this was a cache hit).
    """
    train_matrix_artifact_id = train_matrix_result.matrix_artifact_id
    feature_list = list(train_matrix_result.feature_names)
    canonical_hp = _canonical_hyperparameters(hyperparameters)

    train_deriv = _reconstruct_derivation(db_engine, train_matrix_artifact_id, "train matrix")

    model_config = {
        "class_path": class_path,
        "hyperparameters": canonical_hp,
        "random_seed": random_seed,
    }
    engine_versions = engine_versions_for(MODEL_KIND, class_path)
    model_derivation = derive(
        kind=MODEL_KIND,
        config=model_config,
        parents=[train_deriv],
        source_pins=source_pins,
        engine_versions=engine_versions,
    )

    group_hash = _model_group_hash(class_path, canonical_hp, feature_list)

    # ---- cache: an already-built model is reused wholesale (estimator reloaded from disk).
    hit = cache_hit(db_engine, model_derivation, policy=policy)
    if hit is not None:
        logger.info(f"Model {model_derivation.id[:12]}… already built — reusing (cache hit)")
        record_use(db_engine, run_id, [model_derivation.id])
        existing = _existing_model_row(db_engine, model_derivation.id)
        return ModelResult(
            model_artifact_id=model_derivation.id,
            model_id=existing["model_id"],
            model_group_id=existing["model_group_id"],
            model_group_hash=group_hash,
            artifact_uri=existing["artifact_uri"],
            feature_names=feature_list,
            estimator=_load_estimator(existing["artifact_uri"]),
            cache_hit=True,
        )

    begin_artifact(
        db_engine,
        model_derivation,
        kind=MODEL_KIND,
        config=model_config,
        source_pins=source_pins,
        engine_versions=engine_versions,
        run_id=run_id,
        parents=[train_matrix_artifact_id],
    )

    try:
        estimator, x_columns = _fit_estimator(train_matrix_result, class_path, hyperparameters, random_seed)
        artifact_uri = _serialize_estimator(estimator, storage_dir, model_derivation.id)
        model_size_bytes = Path(artifact_uri).stat().st_size

        model_group_id = _select_or_insert_model_group(
            db_engine,
            group_hash=group_hash,
            class_path=class_path,
            hyperparameters=canonical_hp,
            feature_list=feature_list,
        )
        model_id = _insert_model_row(
            db_engine,
            model_artifact_id=model_derivation.id,
            model_group_id=model_group_id,
            run_id=run_id,
            train_matrix_artifact_id=train_matrix_artifact_id,
            train_end_time=train_end_time,
            training_label_timespan=training_label_timespan,
            artifact_uri=artifact_uri,
            model_size_bytes=model_size_bytes,
            random_seed=random_seed,
        )
        _persist_feature_importances(db_engine, model_id, estimator, x_columns)
        mark_built(db_engine, model_derivation.id, output_ref=artifact_uri)
    except Exception:
        mark_failed(db_engine, model_derivation.id)
        raise

    record_use(db_engine, run_id, [model_derivation.id])
    logger.info(
        f"Built model {model_derivation.id[:12]}… ({class_path}) -> model_id={model_id},"
        + f" group={model_group_id} -> {artifact_uri}"
    )
    return ModelResult(
        model_artifact_id=model_derivation.id,
        model_id=model_id,
        model_group_id=model_group_id,
        model_group_hash=group_hash,
        artifact_uri=artifact_uri,
        feature_names=feature_list,
        estimator=estimator,
        cache_hit=False,
    )


def _design_X(matrix_result: MatrixResult):
    """Load a matrix Parquet and return (X numpy array, feature columns, frame).

    The design matrix X is exactly the ``feature_names`` columns (which already exclude the
    keys, the ``__missing`` flags, and the label triple — F2's ``_feature_columns``). We pull
    them in a stable, recorded order so train and score see identical column geometry, and
    hand the estimator a numpy array (the Phase E seam: Polars/numpy, not pandas).
    """
    import polars as pl

    frame = pl.read_parquet(matrix_result.storage_uri)
    feature_columns = list(matrix_result.feature_names)
    missing = [c for c in feature_columns if c not in frame.columns]
    if missing:
        raise ValueError(
            f"matrix {matrix_result.storage_uri} is missing feature column(s) {missing!r}"
            + " declared in MatrixResult.feature_names — train/score geometry mismatch"
        )
    x = frame.select(feature_columns).to_numpy()
    return x, feature_columns, frame


def _fit_estimator(
    train_matrix_result: MatrixResult,
    class_path: str,
    hyperparameters: Mapping[str, Any],
    random_seed: int,
):
    """Instantiate and fit the estimator on the train matrix; return (estimator, x_columns).

    The label is ``outcome`` (ADR-0010 classification/regression-ranking/regression). Rows
    with a NULL outcome (unlabeled cohort members) are dropped before fitting — an estimator
    cannot learn from an absent target, and the F2 matrix keeps them as a left-join NULL.
    """
    import numpy as np
    import polars as pl

    estimator_cls = _import_estimator(class_path)
    estimator = _instantiate(estimator_cls, hyperparameters, random_seed)

    x, feature_columns, frame = _design_X(train_matrix_result)
    if "outcome" not in frame.columns:
        raise ValueError(
            f"train matrix {train_matrix_result.storage_uri} has no 'outcome' column —"
            + " classification/regression training requires a label (ADR-0010)"
        )
    y = frame.get_column("outcome").to_numpy()

    labeled = ~pl.Series(y).is_null().to_numpy()
    n_labeled = int(labeled.sum())
    if n_labeled == 0:
        raise ValueError(
            f"train matrix {train_matrix_result.storage_uri} has no labeled rows"
            + " (every 'outcome' is NULL) — cannot fit an estimator"
        )
    x_fit = x[labeled]
    y_fit = np.asarray(y)[labeled]

    estimator.fit(x_fit, y_fit)
    logger.debug(
        f"Fitted {class_path} on {n_labeled}/{frame.height} labeled rows ×" + f" {len(feature_columns)} features"
    )
    return estimator, feature_columns


def _instantiate(estimator_cls, hyperparameters: Mapping[str, Any], random_seed: int):
    """Construct the estimator, passing ``random_state`` only if the constructor accepts it.

    Reproducibility (ADR-0016): a deterministic seed enters identity, so it must reach the
    estimator. sklearn estimators take ``random_state``; ones that don't (e.g. a fixed-rule
    baseline) are constructed without it rather than erroring.
    """
    import inspect

    kwargs = dict(hyperparameters)
    try:
        params = inspect.signature(estimator_cls).parameters
        accepts_random_state = "random_state" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):
        accepts_random_state = True
    if accepts_random_state and "random_state" not in kwargs:
        kwargs["random_state"] = random_seed
    return estimator_cls(**kwargs)


def _serialize_estimator(estimator, storage_dir: str, model_artifact_id: str) -> str:
    """joblib.dump the fitted estimator under ``storage_dir``; return its uri.

    Named by ``as_uuid(model_artifact_id)`` to mirror the matrix naming convention
    (ADR-0015) and keep one file per identity.
    """
    import joblib

    Path(storage_dir).mkdir(parents=True, exist_ok=True)
    artifact_uri = str(Path(storage_dir) / f"{as_uuid(model_artifact_id)}.joblib")
    joblib.dump(estimator, artifact_uri)
    return artifact_uri


def _load_estimator(artifact_uri: str):
    import joblib

    return joblib.load(artifact_uri)


def _select_or_insert_model_group(
    db_engine: Engine,
    group_hash: str,
    class_path: str,
    hyperparameters: Mapping[str, Any],
    feature_list: Sequence[str],
) -> int:
    """SELECT the model group by hash, INSERTing it if absent; return ``model_group_id``.

    A second model of the same family (same estimator + hyperparameters + features) reuses the
    existing row — that's how a model group spans temporal splits.
    """
    with db_engine.begin() as conn:
        existing = conn.execute(
            text("select model_group_id from triage.model_groups" + " where model_group_hash = :h"),
            {"h": group_hash},
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        model_group_id = conn.execute(
            text(
                "insert into triage.model_groups"
                + " (model_group_hash, model_type, hyperparameters, feature_list)"
                + " values (:h, :model_type, cast(:hp as jsonb), :feature_list)"
                + " on conflict (model_group_hash) do nothing"
                + " returning model_group_id"
            ),
            {
                "h": group_hash,
                "model_type": class_path,
                "hp": json.dumps(hyperparameters),
                "feature_list": list(feature_list),
            },
        ).scalar_one_or_none()
        if model_group_id is None:
            # A concurrent insert won the race; re-read the now-present row.
            model_group_id = conn.execute(
                text("select model_group_id from triage.model_groups" + " where model_group_hash = :h"),
                {"h": group_hash},
            ).scalar_one()
    return model_group_id


def _insert_model_row(
    db_engine: Engine,
    model_artifact_id: str,
    model_group_id: int,
    run_id: str,
    train_matrix_artifact_id: str,
    train_end_time: Any | None,
    training_label_timespan: str | None,
    artifact_uri: str,
    model_size_bytes: int,
    random_seed: int,
) -> int:
    """INSERT the ``triage.models`` row; return the generated ``model_id``.

    ``model_hash`` is the model artifact_id (FK → artifacts); ``train_matrix_uuid`` is
    ``as_uuid(train_matrix_artifact_id)`` (FK → matrices, ADR-0015).
    """
    with db_engine.begin() as conn:
        model_id = conn.execute(
            text(
                "insert into triage.models"
                + " (model_group_id, model_hash, run_id, train_matrix_uuid,"
                + "  train_end_time, training_label_timespan, artifact_uri,"
                + "  artifact_format, model_size_bytes, random_seed)"
                + " values (:model_group_id, :model_hash, :run_id, :train_matrix_uuid,"
                + "  cast(:train_end_time as date),"
                + "  cast(:training_label_timespan as interval), :artifact_uri,"
                + "  'joblib', :model_size_bytes, :random_seed)"
                + " returning model_id"
            ),
            {
                "model_group_id": model_group_id,
                "model_hash": model_artifact_id,
                "run_id": run_id,
                "train_matrix_uuid": as_uuid(train_matrix_artifact_id),
                "train_end_time": str(train_end_time) if train_end_time else None,
                "training_label_timespan": training_label_timespan,
                "artifact_uri": artifact_uri,
                "model_size_bytes": model_size_bytes,
                "random_seed": random_seed,
            },
        ).scalar_one()
    return model_id


def _feature_importance_values(estimator, n_features: int):
    """Extract per-feature importances from the estimator, or None if it exposes none.

    Tree/ensemble estimators expose ``feature_importances_``; linear models expose ``coef_``
    (we take the magnitude of the first/only row for ranking). Anything else -> None (no rows
    persisted, which is fine — ADR-0011 persists what the estimator offers).
    """
    import numpy as np

    if hasattr(estimator, "feature_importances_"):
        values = np.asarray(estimator.feature_importances_, dtype=float).ravel()
    elif hasattr(estimator, "coef_"):
        coef = np.asarray(estimator.coef_, dtype=float)
        values = np.abs(coef[0] if coef.ndim > 1 else coef).ravel()
    else:
        return None
    if values.shape[0] != n_features:
        logger.warning(
            f"estimator exposed {values.shape[0]} importances for {n_features} features"
            + " — skipping feature-importance persistence (geometry mismatch)"
        )
        return None
    return values


def _persist_feature_importances(db_engine: Engine, model_id: int, estimator, feature_columns: Sequence[str]) -> None:
    """INSERT ``triage.feature_importances`` rows with absolute + percentile ranks (ADR-0011).

    ``rank_abs`` is the 1-based rank by ``abs(importance)`` descending (deterministic
    tie-break by feature name); ``rank_pct`` is the percentile in ``[0, 1]``.
    """
    import numpy as np

    values = _feature_importance_values(estimator, len(feature_columns))
    if values is None:
        logger.debug(f"model_id={model_id} estimator exposes no feature importances — none persisted")
        return

    pairs = list(zip(feature_columns, values, strict=True))
    # Sort by |importance| desc, then feature name asc for a deterministic ranking.
    order = sorted(pairs, key=lambda p: (-abs(p[1]), p[0]))
    n = len(order)
    rows = []
    for rank0, (feature, importance) in enumerate(order):
        rank_abs = rank0 + 1
        rank_pct = (n - rank_abs) / (n - 1) if n > 1 else 1.0
        rows.append(
            {
                "model_id": model_id,
                "feature": feature,
                "feature_importance": float(importance),
                "rank_abs": rank_abs,
                "rank_pct": float(rank_pct),
            }
        )
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "insert into triage.feature_importances"
                + " (model_id, feature, feature_importance, rank_abs, rank_pct)"
                + " values (:model_id, :feature, :feature_importance, :rank_abs, :rank_pct)"
                + " on conflict (model_id, feature) do update set"
                + "  feature_importance = excluded.feature_importance,"
                + "  rank_abs = excluded.rank_abs,"
                + "  rank_pct = excluded.rank_pct"
            ),
            rows,
        )
    logger.debug(f"Persisted {len(rows)} feature importance(s) for model_id={model_id}")


def _existing_model_row(db_engine: Engine, model_artifact_id: str) -> dict[str, Any]:
    with db_engine.connect() as conn:
        row = (
            conn.execute(
                text("select model_id, model_group_id, artifact_uri" + " from triage.models where model_hash = :h"),
                {"h": model_artifact_id},
            )
            .mappings()
            .first()
        )
    if row is None:
        raise ValueError(
            f"model artifact {model_artifact_id!r} has no triage.models row —"
            + " the cache hit found a built artifact without its model row"
        )
    return dict(row)


def _score_column(estimator, x):
    """Produce one score per row: P(positive) for classifiers, else the raw prediction.

    Classification (``predict_proba``): the positive-class probability — the second column
    when ``classes_`` is binary ``[neg, pos]``, else the column for the largest class label
    (the conventional positive). Regression and rankers fall back to ``decision_function``
    then ``predict``. This is the score that feeds the append-only predictions + the in-PG
    ranking spine (ADR-0010).
    """
    import numpy as np

    if hasattr(estimator, "predict_proba"):
        proba = np.asarray(estimator.predict_proba(x))
        if proba.ndim == 2 and proba.shape[1] >= 2:
            classes = getattr(estimator, "classes_", None)
            if classes is not None and len(classes) == proba.shape[1]:
                # positive class = the max class label (1 in a 0/1 problem).
                pos_idx = int(np.argmax(np.asarray(classes)))
            else:
                pos_idx = 1
            return proba[:, pos_idx]
        return proba.ravel()
    if hasattr(estimator, "decision_function"):
        return np.asarray(estimator.decision_function(x)).ravel()
    return np.asarray(estimator.predict(x)).ravel()


def score_and_evaluate(
    db_engine: Engine,
    model_id: int,
    estimator,
    test_matrix_result: MatrixResult,
    as_of_date: Any,
    label_timespan: str,
    *,
    split_kind: str = "test",
    metric_config: Mapping[str, Any] | None = None,
    subset_hash: str = "",
    compute_bias: bool = False,
    bias_parameter: str | None = None,
    bias_ref_groups: Mapping[str, str] | None = None,
) -> ScoreEvaluateResult:
    """Score a test matrix (append-only) and evaluate the model in-Postgres.

    Loads the test matrix Parquet, scores every row with ``estimator`` (see
    :func:`_score_column`), builds ``scores=[{entity_id, as_of_date, score}]`` from the matrix
    keys, and appends them via
    :func:`triage.component.catwalk.prediction_ranking.record_predictions` (ADR-0006: never
    overwritten; re-scoring appends with a later ``scored_at``). Then drives
    :func:`triage.component.catwalk.in_pg_evaluation.evaluate_in_db` and, optionally,
    :func:`~.compute_bias_in_db`.

    Args:
        db_engine: project-database engine.
        model_id: the model these scores belong to (``triage.models.model_id``).
        estimator: the fitted estimator (from :class:`ModelResult`).
        test_matrix_result: the F2 :class:`MatrixResult` for the *test* matrix.
        as_of_date: the scoring date evaluation runs at (must match the test matrix's date).
        label_timespan: the label horizon the test labels were built with (selects the
            matching ``triage.labels`` rows for evaluation).
        split_kind: ``triage.split_kind`` for the prediction rows (default ``'test'``).
        metric_config: ``triage.evaluate_model`` metric config; defaults to the classification
            set (precision@/recall@ + auc_roc + average_precision).
        subset_hash: subset discriminator recorded on the evaluation rows (filtering deferred).
        compute_bias: if True, also call :func:`~.compute_bias_in_db` (needs
            ``bias_parameter``).
        bias_parameter: top-k threshold for the bias group-by (e.g. ``'10_pct'``).
        bias_ref_groups: optional ``{attribute: value}`` reference-group pins for bias.

    Returns:
        A :class:`ScoreEvaluateResult` with the prediction / evaluation / bias row counts.

    Raises:
        ValueError: on a marked-failed-style misconfiguration (missing keys, bad bias args).
    """
    cfg = dict(metric_config) if metric_config is not None else dict(DEFAULT_CLASSIFICATION_CONFIG)
    test_matrix_uuid = str(as_uuid(test_matrix_result.matrix_artifact_id))

    try:
        scores = _build_scores(estimator, test_matrix_result)
        num_predictions = record_predictions(
            db_engine,
            model_id,
            split_kind,
            scores,
            matrix_uuid=test_matrix_uuid,
        )
        num_evaluations = evaluate_in_db(
            db_engine,
            model_id,
            as_of_date,
            label_timespan,
            split_kind=split_kind,
            metric_config=cfg,
            subset_hash=subset_hash,
        )
        num_bias = 0
        if compute_bias:
            if not bias_parameter:
                raise ValueError("compute_bias=True requires bias_parameter (a top-k threshold," + " e.g. '10_pct')")
            num_bias = compute_bias_in_db(
                db_engine,
                model_id,
                as_of_date,
                label_timespan,
                bias_parameter,
                split_kind=split_kind,
                ref_groups=dict(bias_ref_groups or {}),
            )
    except Exception:
        logger.error(
            f"score_and_evaluate failed for model_id={model_id} at {as_of_date}"
            + f" (test matrix {test_matrix_result.matrix_artifact_id[:12]}…)"
        )
        raise

    logger.info(
        f"model_id={model_id}: appended {num_predictions} prediction(s), wrote"
        + f" {num_evaluations} evaluation row(s)"
        + (f", {num_bias} bias row(s)" if compute_bias else "")
    )
    return ScoreEvaluateResult(
        model_id=model_id,
        num_predictions=num_predictions,
        num_evaluations=num_evaluations,
        num_bias_metrics=num_bias,
        metric_config=cfg,
    )


def _build_scores(estimator, matrix_result: MatrixResult) -> list[dict[str, Any]]:
    """Build the append-only score rows from a test matrix's keys + the estimator's scores.

    One row per matrix row: ``{entity_id, as_of_date, score}``. The estimator scores X (the
    ``feature_names`` columns); the keys come from the matrix's ``entity_id`` / ``as_of_date``
    columns in the same row order, so scores align with the entities that produced them.
    """
    x, _feature_columns, frame = _design_X(matrix_result)
    for key in _KEY_COLS:
        if key not in frame.columns:
            raise ValueError(f"test matrix {matrix_result.storage_uri} is missing key column {key!r}")
    scores = _score_column(estimator, x)
    entity_ids = frame.get_column("entity_id").to_list()
    as_of_dates = frame.get_column("as_of_date").to_list()
    return [
        {
            "entity_id": int(entity_id),
            "as_of_date": as_of_date,
            "score": float(score),
        }
        for entity_id, as_of_date, score in zip(entity_ids, as_of_dates, scores, strict=True)
    ]
