"""Greenfield forward scoring — score new entities at a new ``as_of_date`` with an EXISTING model.

The greenfield replacement for the inherited ``predictlist.predict_forward_with_existed_model``
(~190 lines of collate/architect orchestration). Given a trained ``model_id`` and a new
``as_of_date``, it recovers the model's feature/cohort/label/imputation config from the artifact
DAG (:func:`triage.adapters.lineage.reconstruct_model_lineage`), builds a fresh cohort + labels
+ ``production`` matrix at that date through the same builders ``run_experiment`` uses, loads the
fitted estimator, and appends scores (append-only, ADR-0006) tagged ``split_kind='production'``.

Two design points worth stating:

* **No labels yet (G1).** A future ``as_of_date`` has no realized outcomes. We still build the
  labels node (its query may legitimately return no rows / NULL outcomes); the matrix left-joins
  labels, and :func:`triage.adapters.model.score_matrix` reads only X + keys, never ``outcome`` —
  so an unlabeled production matrix scores fine. No evaluation is run (there is nothing to score
  against); evaluation happens later, once outcomes land, via the normal in-PG path.
* **Train feature geometry (G2 + skew).** The ``production`` matrix carries the train matrix as a
  parent so the train-fitted imputation stats flow forward (ADR-0009, the matrix.py G2 widening).
  The estimator expects the *train* matrix's exact feature columns; if the new data yields
  data-dependent columns (e.g. one-hot categories) that differ, we score against the train
  geometry by overriding ``feature_names`` — a train column absent from the production matrix is
  then a loud error rather than a silent wrong prediction.

The forward run carries ``purpose='forward_score'`` + ``prediction_date`` (ADR-0018) and a NULL
experiment_hash, so its rebuilt artifacts are conservatively live for GC (ADR-0017).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from psycopg_pool import ConnectionPool

from triage.adapters.cohort import build_cohort
from triage.adapters.imputation import ImputationPolicy
from triage.adapters.labels import build_labels
from triage.adapters.lineage import reconstruct_model_lineage
from triage.adapters.matrix import build_matrix
from triage.adapters.model import _load_estimator, score_matrix
from triage.adapters.temporal import TemporalConfig
from triage.component.catwalk.prediction_ranking import record_predictions
from triage.derivation import as_uuid
from triage.logging import get_logger
from triage.profiles.storage import storage_for_root

logger = get_logger(__name__)

__all__ = [
    "predict_forward",
    "ForwardResult",
    "open_run",
    "close_run",
    "dated_config",
]


def dated_config(config: Mapping[str, Any], as_of_date: date) -> dict[str, Any]:
    """Fold a single scoring date into a cohort/label config so the artifact is date-scoped.

    The experiment path builds ONE cohort/labels spanning all split dates and deliberately
    excludes per-split dates from identity (the dates are pinned upstream by the temporal
    config). A forward/retrain build is a single NEW date that is *not* derivable from the
    config — so it must enter identity, else ``build_cohort``/``build_labels`` cache-hit a
    cohort/labels populated at the training dates and the matrix's inner join is empty.
    """
    return {**dict(config), "as_of_dates": [as_of_date.isoformat()]}


@dataclass(frozen=True)
class ForwardResult:
    """What :func:`predict_forward` returns."""

    model_id: int
    as_of_date: date
    num_predictions: int
    production_matrix_artifact_id: str
    cohort_artifact_id: str
    labels_artifact_id: str
    run_id: str


def open_run(
    db_engine: ConnectionPool,
    *,
    purpose: str,
    prediction_date: date,
    profile: str = "local",
    random_seed: int = 0,
    experiment_hash: str | None = None,
) -> str:
    """Open a 'started' run row for a forward-score / retrain operation (ADR-0018).

    ``experiment_hash`` is set for retrain runs (the source model's experiment) so a model
    built under this run keeps a run→experiment→problem_type chain that lineage recovery can
    follow; forward-score runs (which build no models) pass None. A NULL experiment_hash makes
    the run conservatively live for GC (ADR-0017) — its rebuilt artifacts are not collected out
    from under an in-use model.
    """
    with db_engine.connection() as conn:
        run_id = conn.execute(
            "insert into triage.runs (profile, status, random_seed, purpose,"
            + " prediction_date, experiment_hash) values (%(profile)s, 'started', %(seed)s,"
            + " %(purpose)s, %(pred)s, %(exp)s) returning run_id",
            {
                "profile": profile,
                "seed": random_seed,
                "purpose": purpose,
                "pred": prediction_date,
                "exp": experiment_hash,
            },
        ).fetchone()["run_id"]
    return str(run_id)


def close_run(
    db_engine: ConnectionPool,
    run_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """Set a run's terminal status ('completed' | 'failed') + finish time."""
    with db_engine.connection() as conn:
        conn.execute(
            "update triage.runs set status = cast(%(status)s as triage.run_status),"
            + " finished_at = now(), error = coalesce(%(error)s, error)"
            + " where run_id = %(run_id)s",
            {"status": status, "error": error, "run_id": run_id},
        )


def predict_forward(
    db_engine: ConnectionPool,
    model_id: int,
    as_of_date: date,
    *,
    storage_dir: str,
    source_pins: Mapping[str, str | None] | None = None,
    profile: str = "local",
    cache_policy: str = "exact",
    split_kind: str = "production",
    problem_type_override: str | None = None,
) -> ForwardResult:
    """Forward-score an existing model at a new ``as_of_date`` (append-only, ADR-0006).

    Args:
        db_engine: project-database engine (greenfield ``triage.*`` schema).
        model_id: the trained model to score with (``triage.models.model_id``).
        as_of_date: the new date to score at.
        storage_dir: directory the production matrix Parquet is written under.
        source_pins: pins to use; defaults to the model's train-matrix pins (recovered from
            the DAG) so the forward closure is cacheable and consistent.
        profile: ``'local'`` | ``'cloud'`` for the run row.
        cache_policy: cache lookup policy threaded to the builders.
        split_kind: prediction ``split_kind`` (default ``'production'``).
        problem_type_override: passed to lineage recovery when the model's run/experiment link
            is gone.

    Returns:
        A :class:`ForwardResult` with the appended-prediction count and the artifact ids.
    """
    lineage = reconstruct_model_lineage(
        db_engine, model_id, problem_type_override=problem_type_override
    )
    if lineage.label_timespan is None:
        raise ValueError(
            f"model {model_id}'s train matrix has no label_timespan — cannot build a"
            + " forward-scoring matrix at the same horizon"
        )
    pins = dict(source_pins) if source_pins is not None else lineage.source_pins
    label_timespan = lineage.label_timespan

    run_id = open_run(
        db_engine,
        purpose="forward_score",
        prediction_date=as_of_date,
        profile=profile,
        random_seed=lineage.random_seed,
    )
    try:
        cohort_artifact_id = build_cohort(
            db_engine,
            run_id,
            cohort_query_template=lineage.cohort_config["query"],
            as_of_dates=[as_of_date],
            config=dated_config(lineage.cohort_config, as_of_date),
            source_pins=pins,
            policy=cache_policy,
        )
        labels_artifact_id = build_labels(
            db_engine,
            run_id,
            cohort_artifact_id=cohort_artifact_id,
            label_query_template=lineage.label_config["query"],
            as_of_dates=[as_of_date],
            label_timespans=[label_timespan],
            problem_type=lineage.problem_type,
            config=dated_config(lineage.label_config, as_of_date),
            source_pins=pins,
            policy=cache_policy,
        )
        production_matrix = build_matrix(
            db_engine,
            run_id,
            featurizer_config=lineage.featurizer_config,
            cohort_artifact_id=cohort_artifact_id,
            labels_artifact_id=labels_artifact_id,
            temporal_config=TemporalConfig.model_validate(lineage.temporal_config),
            imputation_policy=ImputationPolicy.model_validate(
                lineage.imputation_config
            ),
            matrix_kind="production",
            as_of_dates=[as_of_date],
            label_timespan=label_timespan,
            storage=storage_for_root(storage_dir),
            storage_root=storage_dir,
            train_matrix_artifact_id=lineage.train_matrix_artifact_id,
            source_pins=pins,
            policy=cache_policy,
        )

        estimator = _load_estimator(lineage.artifact_uri)
        # Score against the TRAIN feature geometry the estimator was fit on (skew guard).
        scoring_view = replace(
            production_matrix, feature_names=list(lineage.train_matrix.feature_names)
        )
        scores = score_matrix(estimator, scoring_view)
        num_predictions = record_predictions(
            db_engine,
            lineage.model_id,
            split_kind,
            scores,
            matrix_uuid=str(as_uuid(production_matrix.matrix_artifact_id)),
        )
    except Exception as exc:
        close_run(db_engine, run_id, "failed", error=str(exc))
        raise

    close_run(db_engine, run_id, "completed")
    logger.info(
        f"Forward-scored model_id={lineage.model_id} at {as_of_date}:"
        + f" {num_predictions} prediction(s) appended (split_kind={split_kind})"
    )
    return ForwardResult(
        model_id=lineage.model_id,
        as_of_date=as_of_date,
        num_predictions=num_predictions,
        production_matrix_artifact_id=production_matrix.matrix_artifact_id,
        cohort_artifact_id=cohort_artifact_id,
        labels_artifact_id=labels_artifact_id,
        run_id=run_id,
    )
