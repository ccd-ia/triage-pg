"""In-Postgres evaluation bridge (ADR-0007).

Thin Python entry point that drives the PL/pgSQL metric functions created by the
``0002_metric_functions`` migration. The functions read
``triage.prediction_ranks ⋈ triage.labels`` and write long-format rows into
``triage.evaluations`` / ``triage.bias_metrics`` — all the math lives in SQL
(ADR-0007: metrics need only ``(entity_id, score, label)``, which is in
PostgreSQL regardless of where matrices live).

This is intentionally minimal for Phase C: it is the callable bridge over the
SQL, not a rewrite of the experiment flow. Wiring it into the orchestration (and
retiring the inherited sklearn ``evaluation.py`` path) is Phase F's adapter pass.
"""

import json

from triage.artifacts import _notify_run_progress
from triage.logging import get_logger

logger = get_logger(__name__)

# Default metric set, using the inherited metric-name + parameter conventions
# (precision@/recall@ with "<n>_abs"/"<n>_pct" thresholds, auc_roc) so downstream
# audition / dashboards keep working.
DEFAULT_CLASSIFICATION_CONFIG = {
    "metrics": ["precision@", "recall@", "auc_roc", "average_precision"],
    "thresholds": ["100_abs", "10_pct"],
}
DEFAULT_REGRESSION_CONFIG = {
    "regression_metrics": ["rmse", "mae", "r2"],
}
DEFAULT_SURVIVAL_CONFIG = {
    "survival_metrics": ["c_index"],
}


def evaluate_in_db(
    db_engine,
    model_id,
    as_of_date,
    label_timespan,
    split_kind="test",
    metric_config=None,
    subset_hash="",
):
    """Compute evaluation metrics for one model in PostgreSQL.

    Calls ``triage.evaluate_model``, which upserts into ``triage.evaluations``.

    Args:
        db_engine (psycopg_pool.ConnectionPool): pool for the project DB.
        model_id (int): the model to evaluate.
        as_of_date (str | datetime.date): the prediction date to evaluate at.
        label_timespan (str): label timespan interval the labels were built with
            (e.g. ``'6 months'``); selects the matching ``triage.labels`` rows.
        split_kind (str): one of the ``triage.split_kind`` enum values
            (``'train' | 'test' | 'validation' | 'production'``).
        metric_config (dict | None): ``{"metrics": [...], "thresholds": [...],
            "regression_metrics": [...]}``. Defaults to the classification set.
        subset_hash (str): subset discriminator recorded on the rows (filtering
            deferred, schema-design §8.6).

    Returns:
        int: number of evaluation rows written.
    """
    if metric_config is None:
        metric_config = DEFAULT_CLASSIFICATION_CONFIG

    with db_engine.connection() as conn:
        # The owning run for live telemetry (read-dashboard-spec §4): run_id is not a
        # parameter here, only model_id, so resolve it from the model row. Nullable
        # (a model can be seeded without a run), in which case the NOTIFY is skipped.
        model_row = conn.execute(
            "select run_id from triage.models where model_id = %(m)s",
            {"m": model_id},
        ).fetchone()
        run_id = model_row["run_id"] if model_row is not None else None

        result = conn.execute(
            "select triage.evaluate_model("
            "%(model_id)s, cast(%(split_kind)s as triage.split_kind), "
            "cast(%(as_of_date)s as date), cast(%(label_timespan)s as interval), "
            "cast(%(metric_config)s as jsonb), %(subset_hash)s) as written",
            {
                "model_id": model_id,
                "split_kind": split_kind,
                "as_of_date": str(as_of_date),
                "label_timespan": label_timespan,
                "metric_config": json.dumps(metric_config),
                "subset_hash": subset_hash,
            },
        )
        written = result.fetchone()["written"]
        # Emitted after evaluate_model ran, on the same COMMIT, so the dashboard sees
        # the evaluation only once its rows are durable. No-op if run_id is None.
        _notify_run_progress(
            conn, str(run_id) if run_id is not None else None, "evaluation", "completed"
        )
    logger.debug(
        "in-PG evaluation wrote %s rows for model_id=%s as_of_date=%s",
        written,
        model_id,
        as_of_date,
    )
    return written


def compute_bias_in_db(
    db_engine,
    model_id,
    as_of_date,
    label_timespan,
    parameter,
    split_kind="test",
    ref_groups=None,
):
    """Compute SQL bias/disparity metrics for one model at a top-k threshold.

    Calls ``triage.compute_bias_metrics``, which upserts long-format rows into
    ``triage.bias_metrics`` (group_size, selection_rate, precision, tpr, fpr,
    fdr and their disparity vs the reference group). Replaces the Aequitas dump
    (ADR-0007).

    Args:
        db_engine (psycopg_pool.ConnectionPool): pool for the project DB.
        model_id (int): the model to audit.
        as_of_date (str | datetime.date): the prediction date.
        label_timespan (str): label timespan interval (e.g. ``'6 months'``).
        parameter (str): top-k threshold, e.g. ``'100_abs'`` or ``'10_pct'``.
        split_kind (str): ``triage.split_kind`` enum value.
        ref_groups (dict | None): ``{"race": "White"}`` to pin the reference
            group per attribute; otherwise the largest group is used.

    Returns:
        int: number of bias-metric rows written.
    """
    if ref_groups is None:
        ref_groups = {}

    with db_engine.connection() as conn:
        result = conn.execute(
            "select triage.compute_bias_metrics("
            "%(model_id)s, cast(%(split_kind)s as triage.split_kind), "
            "cast(%(as_of_date)s as date), cast(%(label_timespan)s as interval), "
            "%(parameter)s, cast(%(ref_groups)s as jsonb)) as written",
            {
                "model_id": model_id,
                "split_kind": split_kind,
                "as_of_date": str(as_of_date),
                "label_timespan": label_timespan,
                "parameter": parameter,
                "ref_groups": json.dumps(ref_groups),
            },
        )
        written = result.fetchone()["written"]
    logger.debug(
        "in-PG bias metrics wrote %s rows for model_id=%s parameter=%s",
        written,
        model_id,
        parameter,
    )
    return written
