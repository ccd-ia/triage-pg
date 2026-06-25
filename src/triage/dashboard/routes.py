"""Read-only dashboard endpoints (dashboard-api-contract.md).

Every handler is a thin ``SELECT`` over a ``triage.*`` view/function (migrations 0004/0005)
through the request pool (psycopg3, ``dict_row``). No selection/metric/business logic lives
here — it all lives in the views (ADR-0012). Parameters are ALWAYS bound with psycopg3
``%(name)s`` placeholders, never f-string-interpolated (SQL-injection + the global hard rule).

The dashboard hierarchy is **Experiment ▸ Model Group ▸ Model** (migration 0005): analysis
(audition / bias / leaderboard / model-groups / selected-model) is scoped to the
**experiment**, not a single run — a re-run cache-shares models, so run-scoped audition goes
empty (the Q1 bug). The run rail stays primary for live monitoring (runs / summary / progress /
derivation / source-pins / stream).

Empty-state contract: a panel whose source is empty returns ``200`` with
``{"empty": true, "reason": ..., "hint": ...}`` so the SPA can render the state rather than an
empty list it would have to special-case.

Live progress: ``GET /runs/{id}/stream`` holds its OWN ``LISTEN run_progress`` connection
(separate from the request pool) and streams ``text/event-stream`` events; it only LISTENs (the
telemetry emitters own the NOTIFY).
"""

from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from psycopg_pool import ConnectionPool

from triage.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

# The eight standard audition selection rules, each with its default params (the SPA renders a
# pick per rule; migration 0005's audition_pick implements all of them). best_average_two_metrics
# needs a second metric — supplied by the endpoint only when one is available, else skipped.
_AUDITION_RULES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("best_current_value", {}),
    ("best_average_value", {}),
    ("lowest_metric_variance", {}),
    ("most_frequent_best_dist", {"dist_window": 0.05}),
    ("best_avg_var_penalized", {"stdev_penalty": 1.0}),
    ("best_avg_recency_weight", {"curr_weight": 2.0, "decay_type": "linear"}),
    ("best_average_two_metrics", {}),  # metric2/metric1_weight filled in per-request
    ("random_model_group", {"seed": "0"}),
)


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
    """The empty-state envelope (contract §Notes)."""
    return {"empty": True, "reason": reason, "hint": hint}


# ================================================================ runs (rail + monitoring)
@router.get("/runs")
def list_runs(pool: ConnectionPool = Depends(_pool)) -> list[dict]:
    """Rail list of runs (newest first)."""
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
def run_derivation(run_id: UUID, pool: ConnectionPool = Depends(_pool)) -> dict:
    """Derivation graph for the run closure: {nodes, edges}.

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


# ============================================================== experiments (analysis scope)
@router.get("/experiments")
def list_experiments(pool: ConnectionPool = Depends(_pool)) -> list[dict]:
    """Experiment rail: one row per experiment (newest first)."""
    return _rows(
        pool,
        "select experiment_hash, name, description, author, problem_type, created_at,"
        "       n_runs, last_started_at, last_status, last_plan,"
        "       n_model_groups, n_models, n_splits, n_features, base_rate, cohort_size"
        " from triage.experiment_summary"
        " order by last_started_at desc nulls last, created_at desc",
    )


@router.get("/experiments/{experiment_hash}")
def experiment_detail(
    experiment_hash: str, pool: ConnectionPool = Depends(_pool)
) -> dict:
    """Experiment header: summary + raw config + this experiment's runs (newest first)."""
    params = {"hash": experiment_hash}
    summary = _one(
        pool,
        "select experiment_hash, name, description, author, problem_type, created_at,"
        "       n_runs, last_started_at, last_status, last_plan,"
        "       n_model_groups, n_models, n_splits, n_features, base_rate, cohort_size"
        " from triage.experiment_summary where experiment_hash = %(hash)s",
        params,
    )
    if summary is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    config = _one(
        pool,
        "select config from triage.experiments where experiment_hash = %(hash)s",
        params,
    )
    runs = _rows(
        pool,
        "select run_id, experiment_hash, profile, purpose, status, started_at,"
        "       finished_at, triage_version, git_hash, batch_job_id"
        " from triage.runs where experiment_hash = %(hash)s order by started_at desc",
        params,
    )
    return {
        "summary": summary,
        "config": (config or {}).get("config"),
        "runs": runs,
    }


@router.get("/experiments/{experiment_hash}/audition")
def audition(
    experiment_hash: str,
    metric: str = "auc_roc",
    parameter: str = "",
    rule: str = "best_average_value",
    pool: ConnectionPool = Depends(_pool),
) -> dict:
    """Audition ranking + per-split curves + the pick + a pick per standard rule.

    Experiment-scoped (migration 0005): a re-run cache-shares models, so run-scoping is empty
    (the Q1 bug). Empty-state: < 2 model_groups AND < 2 evaluated splits.
    """
    params = {"hash": experiment_hash, "metric": metric, "parameter": parameter}
    ranking = _rows(
        pool,
        "select experiment_hash, metric, parameter, model_group_id, n_splits_evaluated,"
        "       avg_value, stddev_value, avg_distance_from_best, max_regret"
        " from triage.audition"
        " where experiment_hash = %(hash)s and metric = %(metric)s"
        "   and parameter = %(parameter)s"
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
        "select experiment_hash, model_group_id, metric, parameter, as_of_date,"
        "       raw_value, best_value, dist_from_best_case"
        " from triage.audition_distances"
        " where experiment_hash = %(hash)s and metric = %(metric)s"
        "   and parameter = %(parameter)s"
        " order by model_group_id, as_of_date",
        params,
    )
    pick = _one(
        pool,
        "select triage.audition_pick(%(hash)s, %(metric)s, %(parameter)s, %(rule)s,"
        " '{}'::jsonb) as model_group_id",
        {**params, "rule": rule},
    )

    # A pick per standard rule (the SPA shows the strategy panel). best_average_two_metrics
    # needs a SECOND metric; pick any other (metric, parameter) present for this experiment,
    # else skip that rule gracefully.
    second = _one(
        pool,
        "select metric, parameter from triage.metric_catalog"
        " where not (metric = %(metric)s and parameter = %(parameter)s)"
        " order by metric, parameter limit 1",
        params,
    )
    strategies: list[dict[str, Any]] = []
    for rule_name, rule_params in _AUDITION_RULES:
        if rule_name == "best_average_two_metrics":
            if second is None:
                continue
            rule_params = {
                "metric2": second["metric"],
                "parameter2": second["parameter"],
                "metric1_weight": 0.5,
            }
        gid = _one(
            pool,
            "select triage.audition_pick(%(hash)s, %(metric)s, %(parameter)s, %(rule)s,"
            " %(params)s::jsonb) as model_group_id",
            {**params, "rule": rule_name, "params": json.dumps(rule_params)},
        )
        strategies.append(
            {"rule": rule_name, "model_group_id": (gid or {}).get("model_group_id")}
        )

    # provisional until every planned split has evaluated (k/N); N from any run's plan.
    plan = (
        _one(
            pool,
            "select plan from triage.runs where experiment_hash = %(hash)s"
            " and plan is not null order by started_at desc limit 1",
            params,
        )
        or {}
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
        "strategies": strategies,
    }


@router.get("/experiments/{experiment_hash}/bias")
def bias(
    experiment_hash: str,
    model_id: Optional[int] = None,
    pool: ConnectionPool = Depends(_pool),
) -> Any:
    """Bias / fairness group-bys for the experiment's models (optionally a single model).

    Scoped via ``models ⋈ runs WHERE experiment_hash``. Empty-state: no ``protected_groups``
    configured -> no bias_metrics.
    """
    has_bias = _one(
        pool,
        "select 1 as ok from triage.bias_metrics b"
        " join triage.models m on m.model_id = b.model_id"
        " join triage.runs r on r.run_id = m.run_id"
        " where r.experiment_hash = %(hash)s limit 1",
        {"hash": experiment_hash},
    )
    if not has_bias:
        return _empty(
            "no protected_groups configured for this experiment",
            "add a protected_groups config (attribute_name/value per entity, as_of_date) so"
            " in-PG bias metrics are computed (ADR-0007).",
        )

    sql = (
        "select b.model_id, b.split_kind, b.as_of_date, b.parameter,"
        "       b.attribute_name, b.attribute_value, b.metric, b.value,"
        "       b.ref_group_value, b.disparity"
        " from triage.bias_metrics b"
        " join triage.models m on m.model_id = b.model_id"
        " join triage.runs r on r.run_id = m.run_id"
        " where r.experiment_hash = %(hash)s"
    )
    params: dict[str, Any] = {"hash": experiment_hash}
    if model_id is not None:
        sql += " and b.model_id = %(model_id)s"
        params["model_id"] = model_id
    sql += " order by b.model_id, b.attribute_name, b.attribute_value, b.metric"
    return _rows(pool, sql, params)


@router.get("/experiments/{experiment_hash}/leaderboard")
def leaderboard(
    experiment_hash: str, pool: ConnectionPool = Depends(_pool)
) -> list[dict]:
    """The leaderboard matview, scoped to the experiment (migration 0005)."""
    return _rows(
        pool,
        "select experiment_hash, run_id, model_group_id, model_type, split_kind,"
        "       metric, parameter, as_of_date, value, value_expected, value_std,"
        "       model_id, train_end_time"
        " from triage.leaderboard where experiment_hash = %(hash)s"
        " order by metric, parameter, as_of_date, value desc",
        {"hash": experiment_hash},
    )


@router.get("/experiments/{experiment_hash}/evaluations")
def evaluations(
    experiment_hash: str,
    metric: Optional[str] = None,
    pool: ConnectionPool = Depends(_pool),
) -> list[dict]:
    """Metric-over-time card: raw evaluations (test split), scoped via evaluations ⋈ models ⋈ runs."""
    sql = (
        "select r.experiment_hash, e.model_id, m.model_group_id, e.split_kind, e.as_of_date,"
        "       e.metric, e.parameter, e.value, e.num_labeled, e.num_positive"
        " from triage.evaluations e"
        " join triage.models m on m.model_id = e.model_id"
        " join triage.runs r on r.run_id = m.run_id"
        " where r.experiment_hash = %(hash)s and e.split_kind = 'test'"
    )
    params: dict[str, Any] = {"hash": experiment_hash}
    if metric is not None:
        sql += " and e.metric = %(metric)s"
        params["metric"] = metric
    sql += " order by e.metric, e.parameter, m.model_group_id, e.as_of_date"
    return _rows(pool, sql, params)


@router.get("/experiments/{experiment_hash}/model-groups")
def experiment_model_groups(
    experiment_hash: str, pool: ConnectionPool = Depends(_pool)
) -> list[dict]:
    """Model-group cards for the experiment (the Experiment ▸ Model Group level)."""
    return _rows(
        pool,
        "select experiment_hash, model_group_id, model_group_hash, model_type,"
        "       hyperparameters, feature_list, n_models, first_train_end, last_train_end"
        " from triage.model_group_summary where experiment_hash = %(hash)s"
        " order by model_group_id",
        {"hash": experiment_hash},
    )


@router.get("/experiments/{experiment_hash}/selected-model")
def selected_model(
    experiment_hash: str,
    metric: str = "auc_roc",
    parameter: str = "",
    rule: str = "best_average_value",
    pool: ConnectionPool = Depends(_pool),
) -> dict:
    """Selected-model bar: audition pick vs leaderboard #1 + divergence flag.

    Wraps ``triage.selected_model(hash, metric, parameter, rule)`` (migration 0005); the
    columns are audition_group/audition_model, leaderboard_group/leaderboard_model, diverges.
    """
    row = _one(
        pool,
        "select * from triage.selected_model(%(hash)s, %(metric)s, %(parameter)s, %(rule)s)",
        {
            "hash": experiment_hash,
            "metric": metric,
            "parameter": parameter,
            "rule": rule,
        },
    )
    if row is None or row.get("audition_group") is None:
        return _empty(
            "no evaluated models for this experiment yet",
            "the selector needs at least one evaluated model on the metric.",
        )
    return {"metric": metric, "parameter": parameter, "rule": rule, **row}


# ================================================================ hierarchy detail
@router.get("/model-groups/{model_group_id}")
def model_group_detail(
    model_group_id: int,
    metric: str = "auc_roc",
    parameter: str = "",
    pool: ConnectionPool = Depends(_pool),
) -> dict:
    """Model-group drill-down: the card facts + its models + metric-over-time + per-split evals."""
    summary = _one(
        pool,
        "select experiment_hash, model_group_id, model_group_hash, model_type,"
        "       hyperparameters, feature_list, n_models, first_train_end, last_train_end"
        " from triage.model_group_summary where model_group_id = %(g)s limit 1",
        {"g": model_group_id},
    )
    if summary is None:
        raise HTTPException(status_code=404, detail="model group not found")
    models = _rows(
        pool,
        "select m.model_id, m.train_end_time, m.run_id,"
        "       m.training_label_timespan,"
        "       (select min(e.as_of_date) from triage.evaluations e"
        "          where e.model_id = m.model_id and e.split_kind = 'test') as test_as_of"
        " from triage.models m"
        " where m.model_group_id = %(g)s order by m.train_end_time, m.model_id",
        {"g": model_group_id},
    )
    params = {"g": model_group_id, "metric": metric, "parameter": parameter}
    metric_over_time = _rows(
        pool,
        "select e.model_id, m.model_group_id, e.as_of_date, e.metric, e.parameter,"
        "       e.value, e.num_labeled, e.num_positive"
        " from triage.evaluations e"
        " join triage.models m on m.model_id = e.model_id"
        " where m.model_group_id = %(g)s and e.split_kind = 'test'"
        "   and e.metric = %(metric)s and e.parameter = %(parameter)s"
        " order by e.as_of_date",
        params,
    )
    per_split = _rows(
        pool,
        "select e.model_id, m.model_group_id, e.split_kind, e.as_of_date, e.metric,"
        "       e.parameter, e.value, e.value_expected, e.value_std, e.num_labeled,"
        "       e.num_positive"
        " from triage.evaluations e"
        " join triage.models m on m.model_id = e.model_id"
        " where m.model_group_id = %(g)s and e.split_kind = 'test'"
        " order by e.metric, e.parameter, e.as_of_date",
        {"g": model_group_id},
    )
    return {
        "summary": summary,
        "models": models,
        "metric_over_time": metric_over_time,
        "per_split": per_split,
    }


@router.get("/models/{model_id}")
def model_detail(model_id: int, pool: ConnectionPool = Depends(_pool)) -> dict:
    """Model-detail drill-down: the model's group + feature importances + per-split evaluations."""
    model = _one(
        pool,
        "select model_id, model_group_id from triage.models where model_id = %(model_id)s",
        {"model_id": model_id},
    )
    if model is None:
        raise HTTPException(status_code=404, detail="model not found")
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
        "model_group_id": (model or {}).get("model_group_id"),
        "feature_importances": importances,
        "evaluations": evals,
    }


@router.get("/models/{model_id}/curve")
def model_curve(model_id: int, pool: ConnectionPool = Depends(_pool)) -> list[dict]:
    """The Rayid precision/recall + confusion curve over population cuts (client k-slider reads this)."""
    return _rows(
        pool,
        "select k, pct, prec, rec, tp, fp, fn, tn"
        " from triage.model_threshold_curve(%(model_id)s) order by k",
        {"model_id": model_id},
    )


@router.get("/models/{model_id}/histogram")
def model_histogram(
    model_id: int, bins: int = 20, pool: ConnectionPool = Depends(_pool)
) -> list[dict]:
    """Predicted-score histogram (by class) for the model card."""
    return _rows(
        pool,
        "select bin, lo, hi, n, n_pos"
        " from triage.model_score_histogram(%(model_id)s, %(bins)s) order by bin",
        {"model_id": model_id, "bins": bins},
    )


@router.get("/models/{model_id}/predictions")
def model_predictions(
    model_id: int,
    limit: int = 20,
    offset: int = 0,
    pool: ConnectionPool = Depends(_pool),
) -> Any:
    """Ranked predictions (page) joined to outcome via the model's run's labels artifact.

    Returns ``{rows, total}`` — ``rows`` is the requested ``limit``/``offset`` page ordered by
    rank, ``total`` the full prediction count for the model (so the SPA can page the "View all"
    list without over-fetching). Empty-state when the model has no predictions yet.
    """
    total_row = _one(
        pool,
        "select count(*) as n from triage.prediction_ranks where model_id = %(model_id)s",
        {"model_id": model_id},
    )
    total = (total_row or {}).get("n", 0)
    if not total:
        return _empty(
            "no predictions yet",
            "predictions are written by a completed scoring run (append-only, ADR-0006).",
        )

    # rank_abs over the latest scores, LEFT-joined to the outcome from the model's run's single
    # labels artifact (greenfield: one labels per run) on (entity_id, as_of_date) at the model's
    # training_label_timespan. The labels artifact id is resolved as a *scalar* subselect (not a
    # run_artifacts join) so an unlabeled prediction stays one row instead of fanning out across
    # every artifact the run touched.
    rows = _rows(
        pool,
        "select pr.entity_id, pr.as_of_date, pr.score, pr.rank_abs, pr.rank_pct, l.outcome"
        " from triage.prediction_ranks pr"
        " join triage.models m on m.model_id = pr.model_id"
        " left join triage.labels l"
        "      on l.label_hash = (select ra.artifact_id from triage.run_artifacts ra"
        "           join triage.artifacts a on a.artifact_id = ra.artifact_id"
        "                and a.kind = 'labels'"
        "           where ra.run_id = m.run_id limit 1)"
        "      and l.entity_id = pr.entity_id and l.as_of_date = pr.as_of_date"
        "      and l.label_timespan = m.training_label_timespan"
        " where pr.model_id = %(model_id)s"
        " order by pr.as_of_date, pr.rank_abs"
        " limit %(limit)s offset %(offset)s",
        {"model_id": model_id, "limit": limit, "offset": offset},
    )
    return {"rows": rows, "total": total}


# ================================================================ project-level
@router.get("/metrics")
def metrics(pool: ConnectionPool = Depends(_pool)) -> list[dict]:
    """The (metric, parameter, higher_is_better) catalog present in this project (SPA selectors)."""
    return _rows(
        pool,
        "select metric, parameter, higher_is_better from triage.metric_catalog"
        " order by metric, parameter",
    )


@router.get("/ontology")
def ontology(pool: ConnectionPool = Depends(_pool)) -> dict:
    """Per-project data profile: the registered sources + each source's volume over time.

    Source names come from the trusted ``triage.sources`` table; they are still passed as
    bound params to ``source_volume`` (defense in depth — the function also regclass-validates).
    """
    sources = _rows(
        pool,
        "select source_name, relation, knowledge_date_column, description, role"
        " from triage.sources order by source_name",
    )
    volumes: dict[str, list[dict]] = {}
    profile: dict[str, dict] = {}
    for src in sources:
        name = src["source_name"]
        volumes[name] = _rows(
            pool,
            "select period, n from triage.source_volume(%(name)s, 'month') order by period",
            {"name": name},
        )
        profile[name] = (
            _one(
                pool,
                "select total_rows, first_date, last_date, n_distinct_entities"
                " from triage.source_profile(%(name)s)",
                {"name": name},
            )
            or {}
        )
    return {"sources": sources, "volumes": volumes, "profile": profile}


@router.get("/entities/{entity_id}")
def entity_profile(
    entity_id: int,
    experiment_hash: Optional[str] = None,
    pool: ConnectionPool = Depends(_pool),
) -> dict:
    """Full entity profile: the entity-grain attributes + its label history + its score/rank
    trajectory across as_of_dates per model group (optionally scoped to one experiment).

    404 when the entity is unknown to the project (no attributes, no labels, no predictions).
    """
    attributes = (
        _one(
            pool,
            "select triage.entity_attributes(%(e)s) as attributes",
            {"e": entity_id},
        )
        or {}
    ).get("attributes")
    label_history = _rows(
        pool,
        "select as_of_date, label_timespan, outcome"
        " from triage.entity_label_history(%(e)s) order by as_of_date, label_timespan",
        {"e": entity_id},
    )
    score_history = _rows(
        pool,
        "select model_group_id, model_id, experiment_hash, as_of_date, score, rank_abs,"
        "       rank_pct, model_type, hyperparameters, train_end_time"
        " from triage.entity_score_history(%(e)s, %(hash)s)"
        " order by model_group_id, as_of_date",
        {"e": entity_id, "hash": experiment_hash},
    )
    if attributes is None and not label_history and not score_history:
        raise HTTPException(status_code=404, detail="entity not found")
    return {
        "entity_id": entity_id,
        "attributes": attributes,
        "label_history": label_history,
        "score_history": score_history,
    }


@router.get("/status")
def status(pool: ConnectionPool = Depends(_pool)) -> dict:
    """Project status: current source pins, latest engine versions, GC tallies, run counts."""
    sources = _rows(
        pool,
        "select source_name, version_label, registered_at, fingerprint"
        " from triage.current_source_pins order by source_name",
    )
    latest_plan = (
        _one(
            pool,
            "select plan from triage.runs where plan is not null"
            " order by started_at desc limit 1",
        )
        or {}
    ).get("plan")
    engine_versions = (latest_plan or {}).get("engine_versions")
    gc = _rows(
        pool,
        "select kind, status, count(*) as n from triage.artifacts"
        " group by kind, status order by kind, status",
    )
    run_rows = _rows(
        pool,
        "select status, count(*) as n from triage.runs group by status order by status",
    )
    # The SPA reads runs as a {status: count} map (StatusResponse.runs: Record<string,number>);
    # returning the raw rows would render an object as a React child and blank the page.
    runs = {r["status"]: r["n"] for r in run_rows}
    return {
        "sources": sources,
        "engine_versions": engine_versions,
        "gc": gc,
        "runs": runs,
    }


@router.get("/derivation")
def project_derivation(pool: ConnectionPool = Depends(_pool)) -> dict:
    """Project-wide derivation graph: every artifact + its inputs, with cross-experiment sharing.

    Nodes carry ``n_experiments``/``n_runs`` from ``triage.artifact_sharing``; a node touched
    by >1 experiment is shared (the graph highlights it).
    """
    nodes = _rows(
        pool,
        "select a.artifact_id, a.kind, a.status, a.built_by_run,"
        "       coalesce(s.n_experiments, 0) as n_experiments,"
        "       coalesce(s.n_runs, 0)        as n_runs"
        " from triage.artifacts a"
        " left join triage.artifact_sharing s on s.artifact_id = a.artifact_id",
    )
    edges = _rows(
        pool,
        "select parent_id, artifact_id from triage.artifact_inputs",
    )
    return {"nodes": nodes, "edges": edges}


# ================================================================ SSE live progress
# Poll cadence for the listen loop: short enough that a client disconnect is noticed promptly
# (the loop checks ``request.is_disconnected()`` each tick) and a keep-alive comment is emitted,
# long enough not to busy-spin.
_SSE_POLL_SECONDS = 1.0


async def _run_progress_events(request: Request, conninfo: str, run_id: str):
    """Yield ``text/event-stream`` frames for the ``run_progress`` channel.

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
    """SSE endpoint: a long-lived ``LISTEN run_progress`` connection."""
    # Borrow the pool's conninfo only to open a SEPARATE dedicated connection (the stream must
    # not tie up a request-pool connection for its lifetime).
    conninfo = pool.conninfo
    return StreamingResponse(
        _run_progress_events(request, conninfo, str(run_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
