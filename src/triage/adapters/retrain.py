"""Greenfield retrain — retrain a model group's spec on fresh data, then forward-score.

The greenfield replacement for the inherited ``predictlist.Retrainer``. Rather than route a
synthetic single-split config through :func:`triage.adapters.run.run_experiment` (built for the
full timechop grid), it reuses the *builders* directly — exactly as ``run_experiment`` does —
for the one split a retrain needs:

1. Recover the group's estimator spec + feature/cohort/label/imputation config from the latest
   model in the group (:func:`triage.adapters.lineage.reconstruct_model_lineage`).
2. Compute the retrain train cut ``as_of = prediction_date - label_timespan`` so the label
   window closes by ``prediction_date`` (the inherited ``test_duration='0day'`` semantics).
3. Build a fresh cohort + labels + ``train`` matrix at ``as_of``, then ``build_model``.

The retrained model **rejoins its original group iff its feature_list is unchanged** — the
greenfield model-group identity is ``(estimator, hyperparameters, feature_list)`` (ADR-0015 /
model.py), so a feature_list shift mints a new group. This is the documented greenfield
semantics (G5), differing from inherited triage's forced same-group; no override is provided.

``retrain`` and the subsequent forward-score are two runs (``purpose='retrain'`` then
``'forward_score'``, ADR-0018), each a self-contained operation with its own ``prediction_date``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from triage.util.db import DictRowPool

from triage.adapters.cohort import build_cohort
from triage.adapters.forward import (
    ForwardResult,
    close_run,
    dated_config,
    open_run,
    predict_forward,
)
from triage.adapters.imputation import ImputationPolicy
from triage.adapters.labels import build_labels
from triage.adapters.lineage import latest_model_in_group, reconstruct_model_lineage
from triage.adapters.matrix import build_matrix
from triage.adapters.model import build_model
from triage.adapters.temporal import TemporalConfig
from triage.logging import get_logger
from triage.profiles.storage import parent_root, storage_for_root
from triage.util.conf import convert_str_to_relativedelta

logger = get_logger(__name__)

__all__ = ["retrain", "retrain_and_predict", "RetrainResult"]


@dataclass(frozen=True)
class RetrainResult:
    """What :func:`retrain` returns."""

    retrain_model_id: int
    retrain_model_artifact_id: str
    model_group_id: int
    train_matrix_artifact_id: str
    train_as_of_date: date
    run_id: str
    cache_hit: bool


def retrain(
    db_engine: DictRowPool,
    model_group_id: int,
    prediction_date: date,
    *,
    storage_dir: str | None = None,
    source_pins: Mapping[str, str | None] | None = None,
    random_seed: int | None = None,
    profile: str = "local",
    cache_policy: str = "exact",
    problem_type_override: str | None = None,
) -> RetrainResult:
    """Retrain a model group's spec on data cut at ``prediction_date - label_timespan``.

    Args:
        db_engine: project-database engine (greenfield ``triage.*`` schema).
        model_group_id: the group whose estimator spec + feature config to retrain.
        prediction_date: the date the retrain targets; the train cut is this minus the
            group's label_timespan (so labels are realized by ``prediction_date``).
        storage_dir: directory the matrix Parquet + joblib model are written under.
            ``None`` (the default) uses the latest group model's artifact root (the parent
            of its recorded ``artifact_uri``) — new artifacts land beside the old ones.
        source_pins: pins to use; defaults to the latest model's recovered pins.
        random_seed: seed override; defaults to the latest model's seed (reproducible retrain).
        profile: ``'local'`` | ``'cloud'`` for the run row.
        cache_policy: cache lookup policy threaded to the builders.
        problem_type_override: passed to lineage recovery when the run/experiment link is gone.

    Returns:
        A :class:`RetrainResult` for the newly trained (or cache-hit) model.
    """
    last_model_id = latest_model_in_group(db_engine, model_group_id)
    lineage = reconstruct_model_lineage(
        db_engine, last_model_id, problem_type_override=problem_type_override
    )
    if lineage.label_timespan is None:
        raise ValueError(
            f"model group {model_group_id}'s latest model has no label_timespan —"
            + " cannot compute the retrain train cut"
        )
    pins = dict(source_pins) if source_pins is not None else lineage.source_pins
    seed = random_seed if random_seed is not None else lineage.random_seed
    label_timespan = lineage.label_timespan
    if storage_dir is None:
        storage_dir = parent_root(lineage.artifact_uri)
        logger.info(
            "no storage_dir given — defaulting to the group's artifact root {}",
            storage_dir,
        )
    train_as_of = prediction_date - convert_str_to_relativedelta(label_timespan)

    run_id = open_run(
        db_engine,
        purpose="retrain",
        prediction_date=prediction_date,
        profile=profile,
        random_seed=seed,
        experiment_hash=lineage.experiment_hash,
    )
    try:
        cohort_artifact_id = build_cohort(
            db_engine,
            run_id,
            cohort_query_template=lineage.cohort_config["query"],
            as_of_dates=[train_as_of],
            config=dated_config(lineage.cohort_config, train_as_of),
            source_pins=pins,
            policy=cache_policy,
        )
        labels_artifact_id = build_labels(
            db_engine,
            run_id,
            cohort_artifact_id=cohort_artifact_id,
            label_query_template=lineage.label_config["query"],
            as_of_dates=[train_as_of],
            label_timespans=[label_timespan],
            problem_type=lineage.problem_type,
            config=dated_config(lineage.label_config, train_as_of),
            source_pins=pins,
            policy=cache_policy,
        )
        train_matrix = build_matrix(
            db_engine,
            run_id,
            featurizer_config=lineage.featurizer_config,
            cohort_artifact_id=cohort_artifact_id,
            labels_artifact_id=labels_artifact_id,
            temporal_config=TemporalConfig.model_validate(lineage.temporal_config),
            imputation_policy=ImputationPolicy.model_validate(
                lineage.imputation_config
            ),
            matrix_kind="train",
            as_of_dates=[train_as_of],
            label_timespan=label_timespan,
            storage=storage_for_root(storage_dir),
            storage_root=storage_dir,
            source_pins=pins,
            policy=cache_policy,
        )
        model_result = build_model(
            db_engine,
            run_id,
            train_matrix,
            class_path=lineage.class_path,
            hyperparameters=lineage.hyperparameters,
            random_seed=seed,
            storage=storage_for_root(storage_dir),
            storage_root=storage_dir,
            train_end_time=train_as_of,
            training_label_timespan=label_timespan,
            source_pins=pins,
            policy=cache_policy,
        )
    except Exception as exc:
        close_run(db_engine, run_id, "failed", error=str(exc))
        raise

    close_run(db_engine, run_id, "completed")
    logger.info(
        f"Retrained group {model_group_id} for prediction_date={prediction_date}"
        + f" (train cut {train_as_of}) -> model_id={model_result.model_id},"
        + f" group={model_result.model_group_id}"
    )
    return RetrainResult(
        retrain_model_id=model_result.model_id,
        retrain_model_artifact_id=model_result.model_artifact_id,
        model_group_id=model_result.model_group_id,
        train_matrix_artifact_id=train_matrix.matrix_artifact_id,
        train_as_of_date=train_as_of,
        run_id=run_id,
        cache_hit=model_result.cache_hit,
    )


def retrain_and_predict(
    db_engine: DictRowPool,
    model_group_id: int,
    prediction_date: date,
    *,
    storage_dir: str | None = None,
    source_pins: Mapping[str, str | None] | None = None,
    random_seed: int | None = None,
    profile: str = "local",
    cache_policy: str = "exact",
    problem_type_override: str | None = None,
) -> tuple[RetrainResult, ForwardResult]:
    """Retrain the group then forward-score the fresh model at ``prediction_date``.

    ``storage_dir=None`` defaults each leg to the relevant model's own artifact root
    (the retrain writes beside the group's latest model; the forward-score beside the
    freshly retrained one — the same root by construction).

    Returns the ``(RetrainResult, ForwardResult)`` pair. The forward-score is its own run
    (``purpose='forward_score'``); see :func:`triage.adapters.forward.predict_forward`.
    """
    retrain_result = retrain(
        db_engine,
        model_group_id,
        prediction_date,
        storage_dir=storage_dir,
        source_pins=source_pins,
        random_seed=random_seed,
        profile=profile,
        cache_policy=cache_policy,
        problem_type_override=problem_type_override,
    )
    forward_result = predict_forward(
        db_engine,
        retrain_result.retrain_model_id,
        prediction_date,
        storage_dir=storage_dir,
        source_pins=source_pins,
        profile=profile,
        cache_policy=cache_policy,
        problem_type_override=problem_type_override,
    )
    return retrain_result, forward_result
