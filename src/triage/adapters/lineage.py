"""Greenfield model-lineage resolver — recover an experiment's config from the artifact DAG.

A model artifact carries only ``{class_path, hyperparameters, random_seed}``; its full
feature / cohort / label / temporal / imputation config lives one and two hops up the DAG —
on the *train matrix* it descends from, and on that matrix's cohort/labels parents. The
greenfield :mod:`triage.adapters.forward` and :mod:`triage.adapters.retrain` need that config
to rebuild a matrix at a *new* ``as_of_date``, so this module walks the DAG back from a
``model_id`` into the typed config slices plus a reconstructed :class:`MatrixResult`. Pure
``triage.*`` reads — no inherited (old-ORM) imports.

The model→train-matrix link is by UUID, not artifact_id: ``triage.models.train_matrix_uuid``
= ``as_uuid(train_matrix_artifact_id)`` = ``triage.matrices.matrix_uuid``. We resolve it back
to the artifact_id (``triage.matrices.artifact_id``) so we can read ``triage.artifacts.config``
and walk ``triage.artifact_inputs`` parent edges.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from triage.adapters.matrix import MatrixResult
from triage.artifacts import get_artifact
from triage.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "ModelLineage",
    "reconstruct_model_lineage",
    "matrix_result_from_uuid",
    "latest_model_in_group",
    "parents_of",
]

# Mirror triage.adapters.matrix._FIT_STATS_KEY (the matrices.metadata key the train-fitted
# imputation stats live under). Kept as a literal to avoid importing a private name.
_FIT_STATS_KEY = "fit_based_stats"


@dataclass(frozen=True)
class ModelLineage:
    """Everything a forward-score / retrain needs, recovered from a model's DAG closure."""

    model_id: int
    model_artifact_id: str  # == triage.models.model_hash
    model_group_id: int
    experiment_hash: str | None  # the experiment the model's run belonged to (if any)
    artifact_uri: str
    random_seed: int
    class_path: str
    hyperparameters: dict[str, Any]
    train_matrix: MatrixResult
    train_matrix_artifact_id: str
    featurizer_config: dict[str, Any]
    temporal_config: dict[str, Any]
    imputation_config: dict[str, Any]
    label_timespan: str | None
    cohort_config: dict[str, Any]  # carries 'query'
    label_config: dict[str, Any]  # carries 'query'
    problem_type: str
    source_pins: dict[str, str | None]


def matrix_result_from_uuid(db_engine: Engine, matrix_uuid: Any) -> MatrixResult:
    """Rebuild a :class:`MatrixResult` from its ``triage.matrices`` row (by ``matrix_uuid``).

    ``feature_group_artifact_id`` is left empty — the forward/retrain path does not re-derive
    the feature group from a reconstructed matrix; it passes the recovered featurizer config to
    :func:`triage.adapters.matrix.build_matrix`, which derives a fresh feature-group node.
    """
    with db_engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "select artifact_id, storage_uri, num_entities, num_features,"
                    + " feature_names, metadata from triage.matrices"
                    + " where matrix_uuid = :u"
                ),
                {"u": matrix_uuid},
            )
            .mappings()
            .first()
        )
    if row is None:
        raise ValueError(
            f"no triage.matrices row for matrix_uuid {matrix_uuid!r}"
            + " — cannot reconstruct the train matrix for this model"
        )
    return MatrixResult(
        matrix_artifact_id=row["artifact_id"],
        feature_group_artifact_id="",
        storage_uri=row["storage_uri"],
        num_entities=row["num_entities"],
        num_features=row["num_features"],
        feature_names=list(row["feature_names"] or []),
        fit_based_stats=(row["metadata"] or {}).get(_FIT_STATS_KEY, {}),
        cache_hit=True,
    )


def parents_of(db_engine: Engine, artifact_id: str) -> dict[str, str]:
    """The DAG parents of an artifact, as ``{kind: parent_artifact_id}``.

    A train matrix's parents are its feature_group, cohort, and labels nodes. Returns the
    last parent per kind (a matrix has exactly one parent of each kind that matters here).
    """
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "select a.artifact_id, a.kind from triage.artifact_inputs i"
                + " join triage.artifacts a on a.artifact_id = i.parent_id"
                + " where i.artifact_id = :child"
            ),
            {"child": artifact_id},
        ).all()
    return {str(kind): str(pid) for pid, kind in rows}


def latest_model_in_group(db_engine: Engine, model_group_id: int) -> int:
    """The most recently trained model in a group — the spec source for a retrain.

    Ordered by ``train_end_time`` (the data cut), then ``created_at`` / ``model_id`` to break
    ties deterministically. Raises if the group has no models.
    """
    with db_engine.connect() as conn:
        model_id = conn.execute(
            text(
                "select model_id from triage.models where model_group_id = :gid"
                + " order by train_end_time desc nulls last, created_at desc, model_id desc"
                + " limit 1"
            ),
            {"gid": model_group_id},
        ).scalar_one_or_none()
    if model_id is None:
        raise ValueError(
            f"model group {model_group_id} has no models — nothing to retrain from"
        )
    return int(model_id)


def _run_links(db_engine: Engine, run_id: Any) -> tuple[str | None, str | None]:
    """The ``(experiment_hash, problem_type)`` of the experiment a run belongs to.

    ``triage.models.run_id`` is nullable (FK on delete set null) and a run may have a NULL
    ``experiment_hash`` (forward/retrain runs), so either element can be None. The LEFT JOIN
    still returns the run's ``experiment_hash`` even if the experiment row is gone.
    """
    if run_id is None:
        return None, None
    with db_engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "select r.experiment_hash, e.problem_type::text as problem_type"
                    + " from triage.runs r"
                    + " left join triage.experiments e"
                    + "   on e.experiment_hash = r.experiment_hash"
                    + " where r.run_id = :rid"
                ),
                {"rid": run_id},
            )
            .mappings()
            .first()
        )
    if row is None:
        return None, None
    return row["experiment_hash"], row["problem_type"]


def reconstruct_model_lineage(
    db_engine: Engine,
    model_id: int,
    *,
    problem_type_override: str | None = None,
) -> ModelLineage:
    """Walk the artifact DAG back from a model into the config needed to rebuild a matrix.

    Args:
        db_engine: project-database engine (greenfield ``triage.*`` schema).
        model_id: the ``triage.models.model_id`` to resolve.
        problem_type_override: use this instead of the DB lookup. Required only when the
            model's run/experiment link is gone (run deleted) and the type cannot be
            recovered — otherwise the experiment's ``problem_type`` is used.

    Raises:
        ValueError: the model, its artifact, its train matrix, or its cohort/labels parents
            are missing, or the problem_type cannot be resolved and no override was given.
    """
    with db_engine.connect() as conn:
        model_row = (
            conn.execute(
                text(
                    "select model_id, model_hash, model_group_id, run_id,"
                    + " train_matrix_uuid, artifact_uri, random_seed"
                    + " from triage.models where model_id = :mid"
                ),
                {"mid": model_id},
            )
            .mappings()
            .first()
        )
    if model_row is None:
        raise ValueError(f"no triage.models row for model_id {model_id}")
    if model_row["train_matrix_uuid"] is None:
        raise ValueError(
            f"model {model_id} has no train_matrix_uuid — cannot recover its feature config"
        )

    model_artifact_id = model_row["model_hash"]
    model_artifact = get_artifact(db_engine, model_artifact_id)
    if model_artifact is None:
        raise ValueError(
            f"model {model_id} references artifact {model_artifact_id!r} which does not exist"
        )
    model_cfg = model_artifact["config"]

    train_matrix = matrix_result_from_uuid(db_engine, model_row["train_matrix_uuid"])
    train_matrix_artifact_id = train_matrix.matrix_artifact_id
    train_artifact = get_artifact(db_engine, train_matrix_artifact_id)
    if train_artifact is None:
        raise ValueError(
            f"train matrix artifact {train_matrix_artifact_id!r} for model {model_id}"
            + " does not exist"
        )
    matrix_cfg = train_artifact["config"]
    # The train matrix's pins are the closure's pins; the model's must match for cacheability
    # (build_model enforces this), so threading the matrix's pins keeps the forward closure
    # cacheable and consistent.
    source_pins = dict(train_artifact["source_pins"] or {})

    parents = parents_of(db_engine, train_matrix_artifact_id)
    cohort_id = parents.get("cohort")
    labels_id = parents.get("labels")
    if cohort_id is None or labels_id is None:
        raise ValueError(
            f"train matrix {train_matrix_artifact_id!r} is missing a"
            + f" {'cohort' if cohort_id is None else 'labels'} parent edge"
            + " — its config cannot be recovered"
        )
    cohort_artifact = get_artifact(db_engine, cohort_id)
    labels_artifact = get_artifact(db_engine, labels_id)
    if cohort_artifact is None or labels_artifact is None:
        raise ValueError(
            f"cohort/labels parent of train matrix {train_matrix_artifact_id!r} is missing"
        )

    experiment_hash, run_problem_type = _run_links(db_engine, model_row["run_id"])
    problem_type = problem_type_override or run_problem_type
    if problem_type is None:
        raise ValueError(
            f"cannot resolve problem_type for model {model_id} (its run/experiment link is"
            + " gone); pass problem_type_override"
        )

    return ModelLineage(
        model_id=int(model_row["model_id"]),
        model_artifact_id=model_artifact_id,
        model_group_id=int(model_row["model_group_id"]),
        experiment_hash=experiment_hash,
        artifact_uri=model_row["artifact_uri"],
        random_seed=int(model_row["random_seed"] or 0),
        class_path=model_cfg["class_path"],
        hyperparameters=dict(model_cfg.get("hyperparameters", {})),
        train_matrix=train_matrix,
        train_matrix_artifact_id=train_matrix_artifact_id,
        featurizer_config=dict(matrix_cfg["feature_group"]),
        temporal_config=dict(matrix_cfg["temporal_config"]),
        imputation_config=dict(matrix_cfg["imputation_policy"]),
        label_timespan=matrix_cfg.get("label_timespan"),
        cohort_config=dict(cohort_artifact["config"]),
        label_config=dict(labels_artifact["config"]),
        problem_type=problem_type,
        source_pins=source_pins,
    )
