"""Read-only dashboard endpoints (read-dashboard-spec §5).

Every handler is a thin ``SELECT`` over a ``triage.*`` view/function (migration 0004) through
the request pool (psycopg3, ``dict_row``). No selection/metric/business logic lives here — it
all lives in the views (ADR-0012). Parameters are ALWAYS bound with psycopg3 ``%(name)s``
placeholders, never f-string-interpolated (SQL-injection + the global hard rule).

Empty-state contract (spec §3.7): a panel whose source is empty returns ``200`` with
``{"empty": true, "reason": ..., "hint": ...}`` so the SPA can render the state, rather than an
empty list the SPA would have to special-case.

Live progress (spec §4): ``GET /runs/{id}/stream`` holds its OWN ``LISTEN run_progress``
connection (separate from the request pool) and streams ``text/event-stream`` events; it only
LISTENs (the telemetry emitters own the NOTIFY).
"""

from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from psycopg_pool import ConnectionPool

from triage.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


def _pool(request: Request) -> ConnectionPool:
    """Request-pool dependency (avoids an import cycle with ``app.get_pool``)."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "dashboard request pool is not initialized — the app lifespan did not run."
        )
    return pool


def _rows(pool: ConnectionPool, sql: str, params: Optional[dict] = None) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(sql, params or {}).fetchall()


def _one(
    pool: ConnectionPool, sql: str, params: Optional[dict] = None
) -> Optional[dict]:
    with pool.connection() as conn:
        return conn.execute(sql, params or {}).fetchone()


def _empty(reason: str, hint: str) -> dict[str, Any]:
    """The spec §3.7 empty-state envelope."""
    return {"empty": True, "reason": reason, "hint": hint}


# ---------------------------------------------------------------- runs (rail + summary)
@router.get("/runs")
def list_runs(pool: ConnectionPool = Depends(_pool)) -> list[dict]:
    """Rail list of runs (newest first). The headline metric is panel-side off other reads."""
    return _rows(
        pool,
        "select run_id, experiment_hash, profile, purpose, status,"
        "       started_at, finished_at, triage_version, git_hash, batch_job_id"
        " from triage.runs order by started_at desc",
    )


@router.get("/runs/{run_id}/summary")
def run_summary(run_id: UUID, pool: ConnectionPool = Depends(_pool)) -> dict:
    """Summary strip + summary card: run_summary + per-split cohort_profile + label_base_rate."""
    params = {"run": str(run_id)}
    summary = _one(
        pool, "select * from triage.run_summary where run_id = %(run)s", params
    )
    cohort_profile = _rows(
        pool,
        "select run_id, as_of_date, n_entities from triage.cohort_profile"
        " where run_id = %(run)s order by as_of_date",
        params,
    )
    label_base_rate = _rows(
        pool,
        "select run_id, as_of_date, label_timespan, base_rate, n_labeled"
        " from triage.label_base_rate where run_id = %(run)s order by as_of_date",
        params,
    )
    return {
        "summary": summary,
        "cohort_profile": cohort_profile,
        "label_base_rate": label_base_rate,
    }


@router.get("/runs/{run_id}/progress")
def run_progress(run_id: UUID, pool: ConnectionPool = Depends(_pool)) -> dict:
    """Pipeline DAG state: per-(kind,status) counts + the runs.plan denominators (N/M)."""
    params = {"run": str(run_id)}
    progress = _rows(
        pool,
        "select run_id, kind, status, n from triage.run_progress"
        " where run_id = %(run)s order by kind, status",
        params,
    )
    plan = _one(pool, "select plan from triage.runs where run_id = %(run)s", params)
    return {"progress": progress, "plan": (plan or {}).get("plan")}


@router.get("/runs/{run_id}/derivation")
def derivation(run_id: UUID, pool: ConnectionPool = Depends(_pool)) -> dict:
    """Derivation graph for the run closure: {nodes, edges} (spec §3.6).

    Nodes = artifacts the run touched (built OR cache-hit, via run_artifacts); edges =
    artifact_inputs scoped to that node set. A cache-hit node is one this run used but a
    *different* run built (``built_by_run`` != this run).
    """
    params = {"run": str(run_id)}
    nodes = _rows(
        pool,
        "select a.artifact_id, a.kind, a.status, a.built_by_run,"
        "       (a.built_by_run is distinct from %(run)s::uuid) as cache_hit"
        " from triage.artifacts a"
        " join triage.run_artifacts ra on ra.artifact_id = a.artifact_id"
        " where ra.run_id = %(run)s",
        params,
    )
    edges = _rows(
        pool,
        "select ai.parent_id, ai.artifact_id"
        " from triage.artifact_inputs ai"
        " where ai.artifact_id in (select artifact_id from triage.run_artifacts"
        "                          where run_id = %(run)s)"
        "   and ai.parent_id   in (select artifact_id from triage.run_artifacts"
        "                          where run_id = %(run)s)",
        params,
    )
    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------- audition tab
@router.get("/runs/{run_id}/audition")
def audition(
    run_id: UUID,
    metric: str = "auc_roc",
    parameter: str = "",
    rule: str = "best_average_value",
    pool: ConnectionPool = Depends(_pool),
) -> dict:
    """Audition ranking + per-split curves + the pick (spec §3.4/§5).

    Empty-state (spec §3.7): < 2 model_groups across < 2 evaluated splits.
    """
    params = {"run": str(run_id), "metric": metric, "parameter": parameter}
    ranking = _rows(
        pool,
        "select run_id, metric, parameter, model_group_id, n_splits_evaluated,"
        "       avg_value, stddev_value, avg_distance_from_best, max_regret"
        " from triage.audition"
        " where run_id = %(run)s and metric = %(metric)s and parameter = %(parameter)s"
        " order by avg_distance_from_best asc, max_regret asc, model_group_id asc",
        params,
    )
    n_groups = len(ranking)
    n_splits = max((r["n_splits_evaluated"] for r in ranking), default=0)
    if n_groups < 2 and n_splits < 2:
        return _empty(
            "needs >=2 model_groups and >=2 evaluated splits to compare",
            "audition compares model_groups across test splits; let more of the grid x split"
            " finish evaluating.",
        )

    curves = _rows(
        pool,
        "select run_id, model_group_id, metric, parameter, as_of_date,"
        "       raw_value, best_value, dist_from_best_case"
        " from triage.audition_distances"
        " where run_id = %(run)s and metric = %(metric)s and parameter = %(parameter)s"
        " order by model_group_id, as_of_date",
        params,
    )
    pick = _one(
        pool,
        "select triage.audition_pick(%(run)s, %(metric)s, %(parameter)s, %(rule)s,"
        " '{}'::jsonb) as model_group_id",
        {**params, "rule": rule},
    )
    # provisional until every planned split has evaluated (k/N); N from runs.plan.
    plan = (
        _one(pool, "select plan from triage.runs where run_id = %(run)s", params) or {}
    ).get("plan")
    n_planned = (plan or {}).get("n_splits")
    return {
        "metric": metric,
        "parameter": parameter,
        "rule": rule,
        "ranking": ranking,
        "curves": curves,
        "pick": (pick or {}).get("model_group_id"),
        "k": n_splits,
        "n": n_planned,
        "provisional": (n_planned is None) or (n_splits < n_planned),
    }


# ---------------------------------------------------------------- bias tab
@router.get("/runs/{run_id}/bias")
def bias(
    run_id: UUID,
    model_id: Optional[int] = None,
    pool: ConnectionPool = Depends(_pool),
) -> Any:
    """Bias / fairness group-bys for the run's models (optionally a single model).

    Empty-state (spec §3.7): no ``protected_groups`` configured for the run -> no bias_metrics.
    """
    # bias is empty when the experiment has no protected_groups -> in-PG bias produced no rows.
    has_bias = _one(
        pool,
        "select 1 as ok from triage.bias_metrics b"
        " join triage.models m using (model_id)"
        " where m.run_id = %(run)s limit 1",
        {"run": str(run_id)},
    )
    if not has_bias:
        return _empty(
            "no protected_groups configured for this run",
            "add a protected_groups config (attribute_name/value per entity, as_of_date) so"
            " in-PG bias metrics are computed (ADR-0007).",
        )

    sql = (
        "select b.model_id, b.split_kind, b.as_of_date, b.parameter,"
        "       b.attribute_name, b.attribute_value, b.metric, b.value,"
        "       b.ref_group_value, b.disparity"
        " from triage.bias_metrics b join triage.models m using (model_id)"
        " where m.run_id = %(run)s"
    )
    params: dict[str, Any] = {"run": str(run_id)}
    if model_id is not None:
        sql += " and b.model_id = %(model_id)s"
        params["model_id"] = model_id
    sql += " order by b.model_id, b.attribute_name, b.attribute_value, b.metric"
    return _rows(pool, sql, params)


# ---------------------------------------------------------------- result cards
@router.get("/runs/{run_id}/leaderboard")
def leaderboard(run_id: UUID, pool: ConnectionPool = Depends(_pool)) -> list[dict]:
    """The leaderboard matview (now run-scoped, migration 0004)."""
    return _rows(
        pool,
        "select run_id, model_group_id, model_type, split_kind, metric, parameter,"
        "       as_of_date, value, value_expected, value_std, model_id, train_end_time"
        " from triage.leaderboard where run_id = %(run)s"
        " order by metric, parameter, as_of_date, value desc",
        {"run": str(run_id)},
    )


@router.get("/runs/{run_id}/evaluations")
def evaluations(
    run_id: UUID,
    metric: Optional[str] = None,
    pool: ConnectionPool = Depends(_pool),
) -> list[dict]:
    """Metric-over-time card: raw evaluations (test split), scoped to the run via models.run_id."""
    sql = (
        "select m.run_id, e.model_id, m.model_group_id, e.split_kind, e.as_of_date,"
        "       e.metric, e.parameter, e.value, e.num_labeled, e.num_positive"
        " from triage.evaluations e join triage.models m using (model_id)"
        " where m.run_id = %(run)s and e.split_kind = 'test'"
    )
    params: dict[str, Any] = {"run": str(run_id)}
    if metric is not None:
        sql += " and e.metric = %(metric)s"
        params["metric"] = metric
    sql += " order by e.metric, e.parameter, m.model_group_id, e.as_of_date"
    return _rows(pool, sql, params)


@router.get("/runs/{run_id}/predictions")
def predictions(
    run_id: UUID,
    model_id: Optional[int] = None,
    k: Optional[int] = None,
    pool: ConnectionPool = Depends(_pool),
) -> Any:
    """Top-predictions card: prediction_ranks (top-k), scoped to a run's models (spec §3.7).

    Empty-state: no completed scoring run -> no predictions for this run's models.
    """
    has_pred = _one(
        pool,
        "select 1 as ok from triage.predictions p join triage.models m using (model_id)"
        " where m.run_id = %(run)s limit 1",
        {"run": str(run_id)},
    )
    if not has_pred:
        return _empty(
            "no predictions yet",
            "predictions are written by a completed scoring run (append-only, ADR-0006).",
        )

    sql = (
        "select pr.model_id, pr.entity_id, pr.as_of_date, pr.split_kind,"
        "       pr.score, pr.scored_at, pr.rank_abs, pr.rank_pct"
        " from triage.prediction_ranks pr join triage.models m using (model_id)"
        " where m.run_id = %(run)s"
    )
    params: dict[str, Any] = {"run": str(run_id)}
    if model_id is not None:
        sql += " and pr.model_id = %(model_id)s"
        params["model_id"] = model_id
    sql += " order by pr.model_id, pr.as_of_date, pr.rank_abs"
    if k is not None:
        sql += " limit %(k)s"
        params["k"] = k
    return _rows(pool, sql, params)


@router.get("/runs/{run_id}/source-pins")
def source_pins(run_id: UUID, pool: ConnectionPool = Depends(_pool)) -> dict:
    """Source pins / drift card.

    ``run_source_pins`` are the pins frozen at this run's plan time (the per-run record);
    ``current_source_pins`` is the registry's current head per source (drift = the two differ).
    """
    run_pins = _rows(
        pool,
        "select run_id, source_name, version_label, fingerprint"
        " from triage.run_source_pins where run_id = %(run)s order by source_name",
        {"run": str(run_id)},
    )
    current = _rows(
        pool,
        "select source_name, version_label, registered_at, fingerprint"
        " from triage.current_source_pins order by source_name",
    )
    return {"run_pins": run_pins, "current": current}


@router.get("/runs/{run_id}/selected-model")
def selected_model(
    run_id: UUID,
    metric: str = "auc_roc",
    parameter: str = "",
    rule: str = "best_average_value",
    pool: ConnectionPool = Depends(_pool),
) -> dict:
    """Selected-model bar: audition pick vs leaderboard #1 + divergence flag (spec §3.5).

    Wraps ``triage.selected_model(run, metric, parameter, rule)`` (migration 0004); the
    columns are audition_group/audition_model, leaderboard_group/leaderboard_model, diverges.
    """
    row = _one(
        pool,
        "select * from triage.selected_model(%(run)s, %(metric)s, %(parameter)s, %(rule)s)",
        {"run": str(run_id), "metric": metric, "parameter": parameter, "rule": rule},
    )
    if row is None:
        return _empty(
            "no evaluated models for this run yet",
            "the selector needs at least one evaluated model on the metric.",
        )
    return {"metric": metric, "parameter": parameter, "rule": rule, **row}


# ---------------------------------------------------------------- model detail drill-down
@router.get("/models/{model_id}")
def model_detail(model_id: int, pool: ConnectionPool = Depends(_pool)) -> dict:
    """Model-detail drill-down: feature importances + this model's per-split evaluations."""
    importances = _rows(
        pool,
        "select model_id, feature, feature_importance, rank_abs, rank_pct"
        " from triage.feature_importances where model_id = %(model_id)s"
        " order by rank_abs nulls last, feature",
        {"model_id": model_id},
    )
    evals = _rows(
        pool,
        "select model_id, split_kind, as_of_date, metric, parameter, value,"
        "       value_expected, value_std, num_labeled, num_positive"
        " from triage.evaluations where model_id = %(model_id)s"
        " order by metric, parameter, as_of_date",
        {"model_id": model_id},
    )
    return {
        "model_id": model_id,
        "feature_importances": importances,
        "evaluations": evals,
    }


# ---------------------------------------------------------------- SSE live progress
# Poll cadence for the listen loop: short enough that a client disconnect is noticed
# promptly (the loop checks ``request.is_disconnected()`` each tick) and a keep-alive
# comment is emitted, long enough not to busy-spin.
_SSE_POLL_SECONDS = 1.0


async def _run_progress_events(request: Request, conninfo: str, run_id: str):
    """Yield ``text/event-stream`` frames for the ``run_progress`` channel (spec §4).

    A dedicated *async* psycopg3 connection (separate from the request pool) in autocommit
    mode does ``LISTEN run_progress`` and forwards each NOTIFY whose ``run_id`` matches (or
    that omits run_id) as an SSE ``data:`` frame. This handler ONLY LISTENs; the NOTIFY is
    emitted by the telemetry side, so headless runs are a no-op.

    The loop bounds each ``notifies()`` wait with a short timeout so it can (a) emit a
    keep-alive comment during quiet periods and (b) check ``request.is_disconnected()`` and
    exit promptly when the client goes away — the ``finally`` then closes the dedicated
    connection. Bounding the wait is what makes the stream cancellable under a TestClient.
    """
    # An initial comment so the client's EventSource opens immediately.
    yield ": connected\n\n"
    conn = await psycopg.AsyncConnection.connect(conninfo, autocommit=True)
    try:
        await conn.execute("listen run_progress")
        while True:
            if await request.is_disconnected():
                break
            saw_notify = False
            async for notify in conn.notifies(timeout=_SSE_POLL_SECONDS):
                saw_notify = True
                try:
                    payload = json.loads(notify.payload)
                except (ValueError, TypeError):
                    payload = {"raw": notify.payload}
                if payload.get("run_id") not in (None, run_id):
                    continue
                yield f"event: run_progress\ndata: {json.dumps(payload)}\n\n"
            if not saw_notify:
                yield ": keep-alive\n\n"  # the timeout elapsed with nothing to forward
    finally:
        await conn.close()


@router.get("/runs/{run_id}/stream")
def stream(
    request: Request, run_id: UUID, pool: ConnectionPool = Depends(_pool)
) -> StreamingResponse:
    """SSE endpoint: a long-lived ``LISTEN run_progress`` connection (spec §4)."""
    # Borrow the pool's conninfo only to open a SEPARATE dedicated connection (the stream must
    # not tie up a request-pool connection for its lifetime).
    conninfo = pool.conninfo
    return StreamingResponse(
        _run_progress_events(request, conninfo, str(run_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
