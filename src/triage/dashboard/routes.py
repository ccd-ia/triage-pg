"""Read-only dashboard endpoints (dashboard-api-contract.md).

Every handler is a thin ``SELECT`` over a ``triage.*`` view/function (migrations 0004/0005)
through the request pool (psycopg3, ``dict_row``). No selection/metric/business logic lives
here — it all lives in the views (ADR-0012). Parameters are ALWAYS bound with psycopg3
``%(name)s`` placeholders, never f-string-interpolated (SQL-injection + the global hard rule).

The diagnostic reads — the threshold curve, crosstabs, error rules, rule recurrence, list
overlap and a group's models — run through :mod:`triage.diagnostics.readers`, the query
layer ``triage plot`` shares; those handlers only add the HTTP contract (404s, 422s and the
empty-state envelope).

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
from typing import Annotated, Any, Literal, LiteralString, Optional
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from triage.component.catwalk.in_pg_evaluation import AUDITION_RULES as _AUDITION_RULES
from triage.diagnostics import readers
from triage.logging import get_logger
from triage.util.db import DictRowPool

logger = get_logger(__name__)

router = APIRouter()


def _pool(request: Request) -> DictRowPool:
    """Request-pool dependency — resolves the ACTIVE project (ADR-0025 project switcher).

    Reads the ``X-Triage-Project`` header and routes to that project's database pool when a
    registry is configured; falls back to the app's bound pool otherwise (single-project use is
    unchanged). Kept as a thin delegate so every read handler picks up project routing through this
    one ``Depends(_pool)`` seam.
    """
    from triage.dashboard.project_routing import resolve_active_pool

    return resolve_active_pool(request)


def _rows(
    pool: DictRowPool, sql: LiteralString, params: Optional[dict[str, Any]] = None
) -> list[dict[str, Any]]:
    # LiteralString: every read query in this module is a literal — typing it keeps
    # dynamically-built SQL (an injection surface) out of the read layer by construction.
    with pool.connection() as conn:
        return conn.execute(sql, params or {}).fetchall()


def _one(
    pool: DictRowPool, sql: LiteralString, params: Optional[dict[str, Any]] = None
) -> Optional[dict[str, Any]]:
    with pool.connection() as conn:
        return conn.execute(sql, params or {}).fetchone()


def _empty(reason: str, hint: str) -> dict[str, Any]:
    """The empty-state envelope (contract §Notes)."""
    return {"empty": True, "reason": reason, "hint": hint}


# ================================================================ runs (rail + monitoring)
@router.get("/runs")
def list_runs(pool: DictRowPool = Depends(_pool)) -> list[dict[str, Any]]:
    """Rail list of runs (newest first)."""
    return _rows(
        pool,
        "select run_id, experiment_hash, profile, purpose, status,"
        "       started_at, finished_at, triage_version, git_hash, batch_job_id"
        " from triage.runs order by started_at desc",
    )


@router.get("/runs/{run_id}/summary")
def run_summary(run_id: UUID, pool: DictRowPool = Depends(_pool)) -> dict[str, Any]:
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
def run_progress(run_id: UUID, pool: DictRowPool = Depends(_pool)) -> dict[str, Any]:
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


# Per-node temporal split for the derivation graph (swimlane grouping): a MODEL node carries its
# train_end_time; a MATRIX node carries the train_end of the model that trained on it (train
# matrix, via train_matrix_uuid) or scored with it (test matrix, via predictions.matrix_uuid).
# Other kinds (cohort/labels/feature_group/source) have no split. Requires _NODE_SPLIT_JOINS.
_NODE_SPLIT_SELECT = (
    " coalesce(mo.train_end_time::text,"
    "   (select m2.train_end_time::text from triage.models m2"
    "      where m2.train_matrix_uuid = mx.matrix_uuid order by m2.model_id limit 1),"
    "   (select m3.train_end_time::text from triage.predictions p"
    "      join triage.models m3 on m3.model_id = p.model_id"
    "      where p.matrix_uuid = mx.matrix_uuid order by m3.model_id limit 1)) as split,"
    " mx.matrix_kind::text as matrix_kind"
)
_NODE_SPLIT_JOINS = (
    " left join triage.models mo on mo.model_hash = a.artifact_id"
    " left join triage.matrices mx on mx.artifact_id = a.artifact_id"
)


@router.get("/runs/{run_id}/derivation")
def run_derivation(run_id: UUID, pool: DictRowPool = Depends(_pool)) -> dict[str, Any]:
    """Derivation graph for the run closure: {nodes, edges}.

    Nodes = artifacts the run touched (built OR cache-hit, via run_artifacts); edges =
    artifact_inputs scoped to that node set. A cache-hit node is one this run used but a
    *different* run built (``built_by_run`` != this run). Each node carries its temporal
    ``split`` (+ ``matrix_kind``) so the graph can lay matrices/models out in per-split lanes.
    """
    params = {"run": str(run_id)}
    nodes = _rows(
        pool,
        "select a.artifact_id, a.kind, a.status, a.built_by_run,"
        "       (a.built_by_run is distinct from %(run)s::uuid) as cache_hit,"
        + _NODE_SPLIT_SELECT
        + " from triage.artifacts a"
        " join triage.run_artifacts ra on ra.artifact_id = a.artifact_id"
        + _NODE_SPLIT_JOINS
        + " where ra.run_id = %(run)s",
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
def source_pins(run_id: UUID, pool: DictRowPool = Depends(_pool)) -> dict[str, Any]:
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
def list_experiments(pool: DictRowPool = Depends(_pool)) -> list[dict[str, Any]]:
    """Experiment rail: one row per experiment (newest first).

    Soft-archived experiments (``archived_at`` set — e.g. a stale pre-fix duplicate) are hidden
    here; they stay reachable by direct link via ``/experiments/{hash}``.
    """
    return _rows(
        pool,
        "select experiment_hash, name, description, author, problem_type, task_framing,"
        "       created_at, n_runs, last_started_at, last_status, last_plan,"
        "       n_model_groups, n_models, n_splits, n_features, base_rate, cohort_size"
        " from triage.experiment_summary"
        " where archived_at is null"
        " order by last_started_at desc nulls last, created_at desc",
    )


@router.get("/experiments/{experiment_hash}")
def experiment_detail(
    experiment_hash: str, pool: DictRowPool = Depends(_pool)
) -> dict[str, Any]:
    """Experiment header: summary + raw config + this experiment's runs (newest first)."""
    params = {"hash": experiment_hash}
    summary = _one(
        pool,
        "select experiment_hash, name, description, author, problem_type, task_framing,"
        "       created_at, n_runs, last_started_at, last_status, last_plan,"
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
    # Per-run built vs reused artifact counts: a run that BUILT 0 artifacts (everything was a
    # cache hit from another run) is a "replay" — it ran but produced nothing new. ``built_by_run``
    # on the artifact is the original builder, so a cache-hit node has a foreign builder.
    runs = _rows(
        pool,
        "select r.run_id, r.experiment_hash, r.profile, r.purpose, r.status, r.started_at,"
        "       r.finished_at, r.triage_version, r.git_hash, r.batch_job_id,"
        "       coalesce(ac.n_built, 0) as n_built, coalesce(ac.n_reused, 0) as n_reused"
        " from triage.runs r"
        " left join lateral ("
        "   select count(*) filter (where a.built_by_run = r.run_id) as n_built,"
        "          count(*) filter (where a.built_by_run is distinct from r.run_id) as n_reused"
        "   from triage.run_artifacts ra"
        "   join triage.artifacts a on a.artifact_id = ra.artifact_id"
        "   where ra.run_id = r.run_id) ac on true"
        " where r.experiment_hash = %(hash)s order by r.started_at desc",
        params,
    )
    # Cross-experiment artifact sharing: of the artifacts this experiment's runs touched, how many
    # were actually built by a run belonging to a DIFFERENT experiment. ~100% means this experiment
    # rebuilt nothing — it is effectively a duplicate of whatever it borrowed from (e.g. the same
    # config under a stale pre-fix hash). ``shared_with`` names the dominant lender.
    sharing = _one(
        pool,
        "with exp_runs as (select run_id from triage.runs where experiment_hash = %(hash)s),"
        " touched as ("
        "   select distinct a.artifact_id, a.built_by_run"
        "   from triage.run_artifacts ra"
        "   join triage.artifacts a on a.artifact_id = ra.artifact_id"
        "   where ra.run_id in (select run_id from exp_runs)),"
        " lender as ("
        "   select es.experiment_hash, es.name, count(*) as n"
        "   from touched t"
        "   join triage.runs r2 on r2.run_id = t.built_by_run"
        "   join triage.experiment_summary es on es.experiment_hash = r2.experiment_hash"
        "   where r2.experiment_hash <> %(hash)s"
        "   group by es.experiment_hash, es.name order by n desc limit 1),"
        " foreign_total as ("
        "   select count(*) as n from touched t"
        "   join triage.runs r3 on r3.run_id = t.built_by_run"
        "   where r3.experiment_hash <> %(hash)s)"
        " select (select count(*) from touched) as n_total,"
        "        (select n from foreign_total) as n_foreign,"
        "        (select n from lender) as n_shared,"
        "        (select experiment_hash from lender) as shared_with_hash,"
        "        (select name from lender) as shared_with_name",
        params,
    ) or {
        "n_total": 0,
        "n_foreign": 0,
        "n_shared": 0,
        "shared_with_hash": None,
        "shared_with_name": None,
    }
    # Built vs reused: of the models this experiment's runs touched, how many did those runs
    # actually BUILD vs REUSE from another run's cache (models keep their original builder
    # run_id on a cache-hit — the Q1 mechanism). So a re-run that cache-shares isn't undercounted.
    reuse = _one(
        pool,
        "with exp_runs as (select run_id from triage.runs where experiment_hash = %(hash)s),"
        " used as ("
        "   select distinct mo.model_id, mo.run_id as builder_run"
        "   from triage.run_artifacts ra"
        "   join triage.artifacts a on a.artifact_id = ra.artifact_id and a.kind = 'model'"
        "   join triage.models mo on mo.model_hash = ra.artifact_id"
        "   where ra.run_id in (select run_id from exp_runs))"
        " select count(*) filter (where builder_run in (select run_id from exp_runs)) as built,"
        "        count(*) filter (where builder_run is null"
        "                          or builder_run not in (select run_id from exp_runs)) as reused"
        " from used",
        params,
    ) or {"built": 0, "reused": 0}
    return {
        "summary": summary,
        "config": (config or {}).get("config"),
        "runs": runs,
        "model_reuse": reuse,
        "artifact_sharing": sharing,
    }


@router.get("/experiments/{experiment_hash}/audition")
def audition(
    experiment_hash: str,
    metric: str = "auc_roc",
    parameter: str = "",
    rule: str = "best_average_value",
    subset_hash: str = "",
    pool: DictRowPool = Depends(_pool),
) -> dict[str, Any]:
    """Audition ranking + per-split curves + the pick + a pick per standard rule.

    Experiment-scoped (migration 0005): a re-run cache-shares models, so run-scoping is empty
    (the Q1 bug). Empty-state: < 2 model_groups AND < 2 evaluated splits.

    ``subset_hash`` selects on one Subset's evaluations (migration 0031): the common-rows
    Subset that ``POST /experiments/{hash}/common-rows`` returns compares the groups on the
    rows every one of them scored. The default ``''`` is everyone, each group on its own
    rows. ``coverage`` is always the group's share of the labeled rows it scored.
    """
    params = {
        "hash": experiment_hash,
        "metric": metric,
        "parameter": parameter,
        "subset": subset_hash,
    }
    # audition_windowed with both bounds null is triage.audition exactly (migration 0022);
    # it is the form that takes a subset_hash.
    ranking = _rows(
        pool,
        "select a.experiment_hash, a.metric, a.parameter, a.model_group_id,"
        "       a.n_splits_evaluated, a.avg_value, a.stddev_value,"
        "       a.avg_distance_from_best, a.max_regret, c.coverage"
        " from triage.audition_windowed(%(hash)s, %(metric)s, %(parameter)s, null, null,"
        "                               %(subset)s) a"
        " left join triage.model_group_coverage(%(hash)s) c"
        "        on c.model_group_id = a.model_group_id"
        " order by a.avg_distance_from_best asc, a.max_regret asc, a.model_group_id asc",
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
        " from triage.audition_distances_by_subset"
        " where experiment_hash = %(hash)s and metric = %(metric)s"
        "   and parameter = %(parameter)s and subset_hash = %(subset)s"
        " order by model_group_id, as_of_date",
        params,
    )
    pick = _one(
        pool,
        "select triage.audition_pick(%(hash)s, %(metric)s, %(parameter)s, %(rule)s,"
        " '{}'::jsonb, null, null, %(subset)s) as model_group_id",
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
            " %(params)s::jsonb, null, null, %(subset)s) as model_group_id",
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
        "subset_hash": subset_hash,
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
    pool: DictRowPool = Depends(_pool),
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
        "       b.ref_group_value, b.disparity, b.tau, b.passes_fairness"
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
    experiment_hash: str, pool: DictRowPool = Depends(_pool)
) -> list[dict[str, Any]]:
    """The leaderboard matview, scoped to the experiment (migration 0005)."""
    return _rows(
        pool,
        "select experiment_hash, run_id, model_group_id, model_type, split_kind,"
        "       metric, parameter, as_of_date, value, value_expected, value_std,"
        "       model_id, train_end_time"
        " from triage.leaderboard where experiment_hash = %(hash)s"
        " order by metric, parameter, as_of_date,"
        " case when triage.higher_is_better(metric) then value else -value end"
        " desc nulls last",
        {"hash": experiment_hash},
    )


@router.get("/experiments/{experiment_hash}/subsets")
def experiment_subsets(
    experiment_hash: str, pool: DictRowPool = Depends(_pool)
) -> list[dict[str, Any]]:
    """The subsets this experiment's models have evaluations for (migration 0015) —
    feeds the heatmap's subset selector. Empty list = full-cohort only."""
    return _rows(
        pool,
        "select distinct s.subset_hash, s.config->>'name' as name"
        " from triage.subsets s"
        " join triage.evaluations e on e.subset_hash = s.subset_hash"
        " join triage.models m on m.model_id = e.model_id"
        " join triage.runs r on r.run_id = m.run_id"
        " where r.experiment_hash = %(hash)s"
        " order by name",
        {"hash": experiment_hash},
    )


@router.get("/experiments/{experiment_hash}/evaluations")
def evaluations(
    experiment_hash: str,
    metric: Optional[str] = None,
    subset_hash: str = "",
    pool: DictRowPool = Depends(_pool),
) -> list[dict[str, Any]]:
    """Metric-over-time card: raw evaluations (test split), scoped via evaluations ⋈ models ⋈ runs.

    ``subset_hash`` selects a cohort slice's evaluations (migration 0015); the default
    ``''`` is the full cohort — subset rows never leak into the default view.
    """
    sql = (
        "select r.experiment_hash, e.model_id, m.model_group_id, e.split_kind, e.as_of_date,"
        "       e.metric, e.parameter, e.value, e.num_labeled, e.num_positive"
        " from triage.evaluations e"
        " join triage.current_models m on m.model_id = e.model_id"
        " join triage.runs r on r.run_id = m.run_id"
        " where r.experiment_hash = %(hash)s and e.split_kind = 'test'"
        "   and e.subset_hash = %(subset)s"
    )
    params: dict[str, Any] = {"hash": experiment_hash, "subset": subset_hash}
    if metric is not None:
        sql += " and e.metric = %(metric)s"
        params["metric"] = metric
    sql += " order by e.metric, e.parameter, m.model_group_id, e.as_of_date"
    return _rows(pool, sql, params)


@router.get("/experiments/{experiment_hash}/model-groups")
def experiment_model_groups(
    experiment_hash: str, pool: DictRowPool = Depends(_pool)
) -> list[dict[str, Any]]:
    """Model-group cards for the experiment (the Experiment ▸ Model Group level), with each
    group's coverage: the share of the labeled rows it scored (migration 0031)."""
    return _rows(
        pool,
        "select s.experiment_hash, s.model_group_id, s.model_group_hash, s.model_type,"
        "       s.hyperparameters, s.feature_list, s.n_models, s.first_train_end,"
        "       s.last_train_end, c.coverage, c.min_coverage"
        " from triage.model_group_summary s"
        " left join triage.model_group_coverage(%(hash)s) c"
        "        on c.model_group_id = s.model_group_id"
        " where s.experiment_hash = %(hash)s"
        " order by s.model_group_id",
        {"hash": experiment_hash},
    )


@router.get("/experiments/{experiment_hash}/selected-model")
def selected_model(
    experiment_hash: str,
    metric: str = "auc_roc",
    parameter: str = "",
    rule: str = "best_average_value",
    pool: DictRowPool = Depends(_pool),
) -> dict[str, Any]:
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
    experiment_hash: str | None = None,
    pool: DictRowPool = Depends(_pool),
) -> dict[str, Any]:
    """Model-group drill-down: the card facts + its models + metric-over-time + per-split evals.

    A model_group can be SHARED across experiments (content-addressed: same algorithm +
    hyperparameters + feature_list ⇒ same model_group_id). Pass ``experiment_hash`` to scope
    the models/evals to one experiment's runs — otherwise the panel shows every experiment's
    models of this group (e.g. 8 models across two experiments for a 4-split group).

    ``problem_type`` is the type the group's models (in that scope) were trained for, the
    one the sheet's error-recurrence kinds follow; ``None`` when they span several types
    or none is in scope.
    """
    from triage import problem_types

    summary = _one(
        pool,
        "select experiment_hash, model_group_id, model_group_hash, model_type,"
        "       hyperparameters, feature_list, n_models, first_train_end, last_train_end"
        " from triage.model_group_summary where model_group_id = %(g)s limit 1",
        {"g": model_group_id},
    )
    if summary is None:
        raise HTTPException(status_code=404, detail="model group not found")
    # `(%(exp)s is null or r.experiment_hash = %(exp)s)` keeps the query single-shape whether or
    # not the caller scopes to an experiment.
    params = {
        "g": model_group_id,
        "metric": metric,
        "parameter": parameter,
        "exp": experiment_hash,
    }
    models = readers.group_models(pool, model_group_id, experiment_hash)
    metric_over_time = _rows(
        pool,
        "select e.model_id, m.model_group_id, e.as_of_date, e.metric, e.parameter,"
        "       e.value, e.num_labeled, e.num_positive"
        " from triage.evaluations e"
        " join triage.current_models m on m.model_id = e.model_id"
        " join triage.runs r on r.run_id = m.run_id"
        " where m.model_group_id = %(g)s and e.split_kind = 'test'"
        "   and e.subset_hash = ''"
        "   and e.metric = %(metric)s and e.parameter = %(parameter)s"
        "   and (%(exp)s::text is null or r.experiment_hash = %(exp)s)"
        " order by e.as_of_date",
        params,
    )
    per_split = _rows(
        pool,
        "select e.model_id, m.model_group_id, e.split_kind, e.as_of_date, e.metric,"
        "       e.parameter, e.value, e.value_expected, e.value_std, e.num_labeled,"
        "       e.num_positive"
        " from triage.evaluations e"
        " join triage.current_models m on m.model_id = e.model_id"
        " join triage.runs r on r.run_id = m.run_id"
        " where m.model_group_id = %(g)s and e.split_kind = 'test'"
        "   and e.subset_hash = ''"
        "   and (%(exp)s::text is null or r.experiment_hash = %(exp)s)"
        " order by e.metric, e.parameter, e.as_of_date",
        params,
    )
    # group-level aggregates (triage.audition, migration 0013): avg ± σ across the
    # group's splits + regret figures — the "how does this group behave" header chips
    # (plan P6). Experiment-scoped; empty when no experiment_hash was given.
    audition_rows = (
        _rows(
            pool,
            "select metric, parameter, n_splits_evaluated, avg_value, stddev_value,"
            "       avg_distance_from_best, max_regret,"
            "       avg_regret_next_time, max_regret_next_time"
            " from triage.audition"
            " where experiment_hash = %(exp)s and model_group_id = %(g)s"
            " order by metric, parameter",
            params,
        )
        if experiment_hash
        else []
    )
    types = problem_types.group_problem_types(pool, model_group_id, experiment_hash)
    return {
        "summary": summary,
        "problem_type": types[0] if len(types) == 1 else None,
        "models": models,
        "metric_over_time": metric_over_time,
        "per_split": per_split,
        "audition": audition_rows,
    }


@router.get("/models/{model_id}")
def model_detail(model_id: int, pool: DictRowPool = Depends(_pool)) -> dict[str, Any]:
    """Model-detail drill-down: the model's group and problem type, its feature importances
    and its per-split evaluations."""
    model = _one(
        pool,
        "select model_id, model_group_id,"
        "       triage.model_problem_type(model_id)::text as problem_type"
        " from triage.models where model_id = %(model_id)s",
        {"model_id": model_id},
    )
    if model is None:
        raise HTTPException(status_code=404, detail="model not found")
    importances = _rows(
        pool,
        "select model_id, feature, feature_importance, rank_abs, rank_pct,"
        "       importance_kind, signed_value, odds_ratio"
        " from triage.feature_importances where model_id = %(model_id)s"
        " order by rank_abs nulls last, feature",
        {"model_id": model_id},
    )
    evals = _rows(
        pool,
        "select model_id, split_kind, as_of_date, metric, parameter, value,"
        "       value_expected, value_std, num_labeled, num_positive"
        " from triage.evaluations where model_id = %(model_id)s"
        "   and subset_hash = ''"
        " order by metric, parameter, as_of_date",
        {"model_id": model_id},
    )
    # evaluations_windowed (migration 0010): per-metric rollup over the model's test
    # window — mean/min/max/stddev chips on the model-sheet header.
    windowed = _rows(
        pool,
        "select metric, parameter, n_as_of_dates, window_start, window_end,"
        "       value_mean, value_min, value_max, value_stddev, num_labeled_total"
        " from triage.evaluations_windowed"
        " where model_id = %(model_id)s and split_kind = 'test' and subset_hash = ''"
        " order by metric, parameter",
        {"model_id": model_id},
    )
    return {
        "model_id": model_id,
        "model_group_id": model["model_group_id"],
        # the model card branches on it: which panels apply to this model
        "problem_type": model["problem_type"],
        "feature_importances": importances,
        "evaluations": evals,
        "windowed": windowed,
    }


@router.get("/models/{model_id}/curve")
def model_curve(
    model_id: int,
    as_of_date: Optional[str] = None,
    subset: str = "",
    points: Annotated[Optional[int], Query(ge=2)] = None,
    pool: DictRowPool = Depends(_pool),
) -> list[dict[str, Any]]:
    """The Rayid precision/recall + confusion curve over population cuts (client k-slider reads this).

    One test date: ``as_of_date``, or the model's latest test date when omitted. ``subset``
    is a ``subset_hash`` (``/experiments/{hash}/subsets`` lists them); the subset is then the
    population, re-ranked inside it. ``base_rate`` is that population's labeled positives over
    its labeled rows — the reference line the panel draws. ``points`` thins the curve to the
    rows nearest that many evenly spaced shares of the population, keeping every configured
    cut's exact row (``readers.curve``); omitted, every k is returned. Classification and
    regression_ranking (positive means outcome > 0); another type answers 422.
    """
    _require_type(pool, "plot curve", model_id)
    try:
        return readers.curve(
            pool, model_id, as_of_date=as_of_date, subset=subset, points=points
        )
    except readers.NotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"subset {subset!r} not found — pass a subset_hash from"
            " /experiments/{hash}/subsets",
        ) from exc


@router.get("/models/{model_id}/histogram")
def model_histogram(
    model_id: int, bins: int = 20, pool: DictRowPool = Depends(_pool)
) -> list[dict[str, Any]]:
    """Predicted-score histogram for the model card (migration 0029): ``[0, 1]`` bins for a
    classifier, the model's own score range otherwise; ``n_pos`` is null unless the model
    is a classifier."""
    try:
        return readers.score_histogram(pool, model_id, bins)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/models/{model_id}/calibration")
def model_calibration(
    model_id: int,
    split_kind: str = "test",
    as_of_date: Optional[str] = None,
    bins: int = 10,
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """Reliability deciles for the model card (migration 0029): per decile the mean score,
    the mean outcome and its lift, the realized rate for a classifier, and the largest
    block of tied scores, which shares one decile.

    Defaults: the model's LATEST evaluated test date and its training label timespan —
    exactly the slice the per-split evaluations describe. A survival model answers 422: its
    calibration is ``/survival``'s, predicted S(H) against Kaplan-Meier.
    """
    _require_type(pool, "plot calibration", model_id)
    try:
        result = readers.calibration(pool, model_id, split_kind, as_of_date, bins)
    except readers.NotFoundError as exc:
        raise HTTPException(status_code=404, detail="model not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        return _empty(
            "no evaluated date / label timespan for this model",
            "calibration needs labeled predictions — run an evaluation first.",
        )
    return result


@router.get("/models/{model_id}/overlap/{other_model_id}")
def model_overlap(
    model_id: int,
    other_model_id: int,
    parameter: str = "100_abs",
    split_kind: str = "test",
    pool: DictRowPool = Depends(_pool),
) -> list[dict[str, Any]]:
    """Top-k list overlap between two models (migration 0016): per shared prediction
    date — intersection, Jaccard over the union of the two lists, Spearman rank corr."""
    return readers.list_overlap(
        pool, model_id, other_model_id, parameter=parameter, split_kind=split_kind
    )


@router.get("/models/{model_id}/entities/{entity_id}/importances")
def model_entity_importances(
    model_id: int,
    entity_id: int,
    limit: int = 5,
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """Per-entity feature contributions ('why this entity'), when a diagnostics pass has
    written ``individual_importances`` (``triage postmodel``, plan P5)."""
    rows = _rows(
        pool,
        "select feature, feature_value, importance_score, method"
        " from triage.individual_importances"
        " where model_id = %(model_id)s and entity_id = %(entity_id)s"
        " order by abs(importance_score) desc nulls last limit %(limit)s",
        {"model_id": model_id, "entity_id": entity_id, "limit": limit},
    )
    if not rows:
        return _empty(
            "no per-entity importances for this model",
            "run `triage postmodel error-tree <model_id>` — linear models get per-entity"
            " β·x contributions persisted alongside the diagnostics.",
        )
    return rows


def _problem_type(pool: DictRowPool, model_id: int) -> str:
    """The model's problem type, for an empty state's hint (classification when unknown)."""
    row = _one(
        pool,
        "select triage.model_problem_type(%(m)s)::text as problem_type",
        {"m": model_id},
    )
    return "classification" if row is None else row["problem_type"]


@router.get("/models/{model_id}/crosstabs")
def model_crosstabs(
    model_id: int,
    parameter: Optional[str] = None,
    limit: int = 20,
    stat: Literal["mean", "nonzero_rate"] = "mean",
    comparison: Literal["rest", "near_miss"] = "rest",
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """Top distinguishing features among the selected vs the rest — thin read over
    ``triage.crosstabs`` (persisted by ``triage postmodel crosstabs``).

    ``stat=mean`` (the default) ranks the ratio of means by |log ratio|; ``stat=nonzero_rate``
    ranks the nonzero rates by |selected − rest|, where a ratio of two rates near zero would
    mislead. ``comparison=near_miss`` compares the selected rows with the next k rows below
    the cut instead of every row below it (migration 0037); ``rest_value`` then holds the
    near-miss value. The near miss is a classification or regression_ranking model's; another
    type answers 422.
    """
    if comparison == "near_miss":
        _require_type(pool, "postmodel crosstabs --against near-miss", model_id)
    rows = readers.crosstabs(
        pool,
        model_id,
        parameter=parameter,
        stat=stat,
        limit=limit,
        comparison=comparison,
    )
    if not rows:
        if comparison == "near_miss":
            hint = (
                f"run `triage postmodel crosstabs {model_id} --against near-miss` — it"
                " compares the top k with the next k rows and persists here."
            )
        elif _problem_type(pool, model_id) == "regression":
            hint = (
                f"run `triage postmodel crosstabs {model_id} --by abs-error` — the rows"
                " with the largest errors against the rest, computed from the matrix."
            )
        else:
            hint = (
                f"run `triage postmodel crosstabs {model_id}` — the CLI computes from the"
                " matrix and persists here."
            )
        return _empty(
            "no crosstabs persisted for this model"
            + (" against the near miss" if comparison == "near_miss" else ""),
            hint,
        )
    return rows


#: Every kind of error-tree leaf: the classification kinds (also a survival model's at a
#: horizon) and the regression kinds (migration 0034).
ErrorKind = Literal["fp", "fn", "confusion", "abs_error", "beats_baseline"]


def _error_tree_option(problem_type: str, kind: Optional[str]) -> str:
    """The ``triage postmodel error-tree`` options that fit ``kind`` leaves for a type."""
    if kind == "beats_baseline":
        return "--kind beats-baseline"
    if kind == "abs_error" or problem_type == "regression":
        return "--kind abs-error"
    if problem_type == "survival":
        return "--kind all --horizon H"
    return "--kind all"


def _error_tree_hint(pool: DictRowPool, model_id: int, kind: Optional[str]) -> str:
    """The ``triage postmodel error-tree`` command that fills an empty error-rules panel."""
    option = _error_tree_option(_problem_type(pool, model_id), kind)
    return (
        f"run `triage postmodel error-tree {model_id} {option}` — the CLI fits the error"
        " trees from the matrix and persists the rules here."
    )


@router.get("/models/{model_id}/error-rules")
def model_error_rules(
    model_id: int,
    error_kind: Optional[ErrorKind] = None,
    suspicious: bool = False,
    max_error_rate: float = Query(0.02, ge=0.0, le=1.0),
    min_support: int = Query(30, ge=1),
    limit: int = 20,
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """The error-tree rules (where does this model fail?) — thin read over
    ``triage.error_analysis`` (persisted by ``triage postmodel error-tree``).

    ``error_kind`` keeps one kind of leaf: fp | fn | confusion (a survival model's trees at
    a horizon are these kinds, at parameter ``<cut>|horizon=<H>``), or the regression kinds
    abs_error | beats_baseline. ``suspicious=true`` reads ``triage.error_rules_suspicious``
    instead (migration 0027): the high-support leaves with almost no error, each with its
    TP share and its lift over the model's precision@ (an abs_error leaf: the population
    MAE over its own). That list is a plain array — empty when nothing qualifies — since
    "nothing suspicious" is an answer, not a missing diagnostic.

    Every leaf carries ``reference_error_rate``: the population error rate of its kind at its
    date and cut (``readers.error_reference_rates``), the mark its own rate is read against.
    A kind the model's problem type has no leaves of (``problem_types.leaf_kinds``) answers
    422 naming the kind to read instead.
    """
    if error_kind is not None:
        from triage import problem_types

        try:
            problem_types.require_leaf_kind(pool, model_id, error_kind)
        except problem_types.WrongProblemType as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:  # no model has this id
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    if suspicious:
        return readers.suspicious_leaves(
            pool,
            model_id,
            error_kind=error_kind,
            max_error_rate=max_error_rate,
            min_support=min_support,
            limit=limit,
        )
    rows = readers.error_rules(pool, model_id, error_kind=error_kind, limit=limit)
    if not rows:
        return _empty(
            "no error analysis persisted for this model"
            + ("" if error_kind is None else f" ({error_kind} leaves)"),
            _error_tree_hint(pool, model_id, error_kind),
        )
    return rows


@router.get("/model-groups/{model_group_id}/error-recurrence")
def model_group_error_recurrence(
    model_group_id: int,
    parameter: Optional[str] = None,
    error_kind: Optional[ErrorKind] = None,
    limit: int = 20,
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """On how many of the group's test dates each error rule recurs — thin read over
    ``triage.error_rule_recurrence`` (migration 0027).

    ``error_kind`` defaults to the first kind of leaf the group's problem type has
    (``problem_types.leaf_kinds``): fp, or abs_error for a regression group. A kind the
    type has no leaves of answers 422 naming the kind to read instead. ``parameters``
    lists the cuts the group's error trees of that kind were fitted at (``''`` for
    abs_error, ``baseline:<group>`` for beats_baseline); without ``parameter`` the most
    recently analysed one is read.
    """
    from triage import problem_types

    types = problem_types.group_problem_types(pool, model_group_id)
    kind = error_kind or (problem_types.leaf_kinds(types[0])[0] if types else "fp")
    refused = problem_types.recurrence_refusal(model_group_id, types, kind)
    if refused is not None:
        raise HTTPException(status_code=422, detail=refused)
    try:
        recurrence = readers.error_recurrence(
            pool,
            model_group_id,
            parameter=parameter,
            error_kind=kind,
            limit=limit,
        )
    except readers.NotFoundError as exc:
        raise HTTPException(status_code=404, detail="model group not found") from exc
    if not recurrence["parameters"]:
        reads = next(
            (t for t in types if kind in problem_types.leaf_kinds(t)), "classification"
        )
        return _empty(
            f"no error analysis persisted for this model group ({kind} leaves)",
            f"run `triage postmodel error-tree <model_id> {_error_tree_option(reads, kind)}`"
            " on the group's models — recurrence counts each rule across their test"
            " dates.",
        )
    return recurrence


@router.get("/model-groups/{model_group_id}/curves")
def model_group_curves(
    model_group_id: int,
    subset: str = "",
    points: Annotated[int, Query(ge=2)] = 101,
    experiment_hash: Optional[str] = None,
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """The curve across a model group's test dates: one thinned curve per (model, date).

    Each series is ``{model_id, as_of_date, base_rate, population, points}``, in date order;
    ``points`` holds the curve rows ``readers.curve`` returns, thinned to that many evenly
    spaced shares of the population plus every configured cut. ``subset`` is a subset_hash
    (each model's curve is then drawn over the subset's members); ``experiment_hash`` keeps
    one experiment's models of a shared group. A group whose models (in that scope) are all
    regression or survival answers 422: the curve needs a top-k cut.
    """
    from triage import problem_types

    types = problem_types.group_problem_types(pool, model_group_id, experiment_hash)
    refused = problem_types.group_refusal("plot curve-dates", model_group_id, types)
    if refused is not None:
        raise HTTPException(status_code=422, detail=refused)
    try:
        series = readers.group_curves(
            pool,
            model_group_id,
            subset=subset,
            points=points,
            experiment_hash=experiment_hash,
        )
    except readers.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not series:
        return _empty(
            "no test curves for this model group"
            + (f" in subset {subset!r}" if subset else ""),
            "a curve is drawn over a model's scored test date — score the group's"
            " models (a run of the experiment does), or pick a subset with members"
            " on those dates.",
        )
    return series


@router.get("/models/{model_id}/predictions")
def model_predictions(
    model_id: int,
    limit: int = 20,
    offset: int = 0,
    split_kind: str = "test",
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """Ranked predictions (page) of one split, each with the outcome its evaluation read.

    Returns ``{rows, total}`` — ``rows`` is the requested ``limit``/``offset`` page ordered by
    rank, ``total`` the full prediction count for the model and split (so the SPA can page the
    "View all" list without over-fetching). Empty-state when there are no predictions yet.
    """
    total_row = _one(
        pool,
        "select count(*) as n from triage.prediction_ranks"
        " where model_id = %(model_id)s"
        "   and split_kind = cast(%(split)s as triage.split_kind)",
        {"model_id": model_id, "split": split_kind},
    )
    total = (total_row or {}).get("n", 0)
    if not total:
        return _empty(
            "no predictions yet",
            "predictions are written by a completed scoring run (append-only, ADR-0006).",
        )

    # scored_labels (migration 0026) labels each row from its scored matrix's lineage, at the
    # horizon precision@ was computed with — not the training horizon.
    rows = _rows(
        pool,
        "select entity_id, as_of_date, score, rank_abs, rank_pct, outcome"
        " from triage.scored_labels(%(model_id)s, cast(%(split)s as triage.split_kind))"
        " order by as_of_date, rank_abs"
        " limit %(limit)s offset %(offset)s",
        {"model_id": model_id, "split": split_kind, "limit": limit, "offset": offset},
    )
    return {"rows": rows, "total": total}


# ======================================================== postmodel panels per problem type
# Each route wraps one reader of triage.diagnostics (or a diagnostics module) and adds only
# the HTTP contract. A route whose diagnostic does not apply to the model's problem type
# answers 422 with problem_types' refusal, which names the command that does apply. An
# empty source answers the empty-state envelope, whose hint names the CLI command that
# fills it or the run that scores the model.


def _require_type(pool: DictRowPool, command: str, model_id: int) -> str:
    """The model's problem type when ``command`` reads it; else 422 (unknown model: 404)."""
    from triage import problem_types

    try:
        return problem_types.require(pool, command, model_id)
    except problem_types.WrongProblemType as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:  # no model has this id
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _unscored(model_id: int, command: str, options: str = "") -> dict[str, Any]:
    """The empty state of a diagnostic computed on read: the model scored no labeled row."""
    return _empty(
        f"model {model_id} has no labeled test rows",
        f"score and evaluate it first (a `triage run` of its experiment does); then"
        f" `triage {command} {model_id}{options}` reads the same rows.",
    )


@router.get("/models/{model_id}/residuals")
def model_residuals(model_id: int, pool: DictRowPool = Depends(_pool)) -> Any:
    """Per test date: a regression model's MAE, RMSE, WAPE, bias, error concentration,
    point coverage and forecast shape (``readers.residual_summary``, migration 0033).

    ``mode_share`` is the share of rows forecast the most frequent value: a forecast that
    gives one value to most rows ranks nothing. Regression and regression_ranking only.
    """
    _require_type(pool, "postmodel residuals", model_id)
    rows = readers.residual_summary(pool, model_id)
    return rows or _unscored(model_id, "postmodel residuals")


@router.get("/models/{model_id}/residuals/segments")
def model_residual_segments(
    model_id: int,
    cut: str = "band",
    pooled: Optional[bool] = None,
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """A regression model's errors per segment of its rows (``readers.residuals_by_segment``).

    ``cut`` is ``band`` (the band of the entity's mean prior label), ``sbc`` (its
    Syntetos–Boylan class), ``cold-start`` or ``subset:NAME``. ``pooled=true`` keeps the rows
    pooled over the test dates, ``false`` the per-date rows; omitted, both (a pooled row has a
    null ``as_of_date``).
    """
    _require_type(pool, "postmodel residuals", model_id)
    try:
        rows = readers.residuals_by_segment(pool, model_id, cut, pooled=pooled)
    except readers.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return rows or _unscored(model_id, "postmodel residuals", f" --by {cut}")


@router.get("/models/{model_id}/forecast-actual")
def model_forecast_actual(
    model_id: int,
    cap: Annotated[int, Query(ge=1, le=readers.FORECAST_ACTUAL_CAP)] = 20_000,
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """Forecast and outcome pairs of a regression model's labeled test rows, for the
    forecast-against-actual panel (``readers.forecast_actual``).

    Over ``cap`` pairs, the entities kept are the first in ``hashint8(entity_id)`` order, the
    same on every call; ``sampled`` says so.
    """
    _require_type(pool, "plot forecast-actual", model_id)
    result = readers.forecast_actual(pool, model_id, cap=cap)
    if not result["n_total"]:
        return _unscored(model_id, "plot forecast-actual")
    return result


@router.get("/models/{model_id}/baseline")
def model_baseline(
    model_id: int,
    baseline: Annotated[Optional[int], Query(ge=1)] = None,
    cut: str = "all",
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """The model's group against a baseline group, on the rows both scored, over the
    group's test dates in the model's experiment (``baseline_compare.compare_to_baseline``).

    ``baseline`` is a model group; omitted, the experiment's baseline group with the lowest
    MAE on the shared rows. ``cut`` is ``band``, ``sbc``, ``cold-start``, ``subset:NAME`` or
    ``all``. A non-regression model, or groups that share no row, answer 422; an experiment
    with no baseline group answers the empty state.
    """
    from triage.diagnostics.baseline_compare import (
        ComparisonError,
        NoBaseline,
        compare_to_baseline,
    )

    model = _one(
        pool,
        "select m.model_group_id, r.experiment_hash from triage.models m"
        " left join triage.runs r on r.run_id = m.run_id where m.model_id = %(m)s",
        {"m": model_id},
    )
    if model is None:
        raise HTTPException(status_code=404, detail="model not found")
    group = model["model_group_id"]
    try:
        result = compare_to_baseline(
            pool,
            group,
            baseline,
            experiment_hash=model["experiment_hash"],
            cut=cut,
        )
    except NoBaseline as exc:
        return _empty(
            "no baseline group shares a scored row with this model's group",
            f"{exc}: `triage postmodel baseline {group} --baseline GROUP` compares it"
            " with any other group of the experiment.",
        )
    except ComparisonError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result.to_dict()


@router.get("/models/{model_id}/structure")
def model_structure(
    model_id: int,
    min_rows: Annotated[int, Query(ge=1)] = 2,
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """Checks on the fitted estimator (``structure.model_structure``, what ``triage postmodel
    structure`` prints): the forecast structure of a regression model, the leaves of a tree
    fitted on fewer than ``min_rows`` training rows, and a linear forecast's mean shift from
    train to test by feature family. ``unavailable`` says why a check does not apply.

    Reads the model's joblib file and matrices. A model with no stored estimator answers the
    empty state.
    """
    from triage import problem_types
    from triage.diagnostics.structure import model_structure as structure_of

    try:
        problem_types.model_problem_type(pool, model_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return structure_of(pool, model_id, min_rows)
    except (ValueError, OSError) as exc:
        return _empty(
            f"the fitted model cannot be read: {exc}",
            f"`triage postmodel structure {model_id}` reads the estimator the run stored"
            " (models.artifact_uri) and its train and test matrices.",
        )


@router.get("/models/{model_id}/gains")
def model_gains(
    model_id: int,
    as_of_date: Optional[str] = None,
    points: Annotated[int, Query(ge=1)] = 100,
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """The cumulative-gains curve of one evaluated date (``readers.gains_curve``, migration
    0035): the share of the outcome total the top k hold, its lift over a random list, and
    the stored ``aucc``. Defaults to the model's latest evaluated test date.
    """
    _require_type(pool, "plot gains", model_id)
    try:
        result = readers.gains_curve(
            pool, model_id, as_of_date=as_of_date, points=points
        )
    except readers.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        return _empty(
            f"model {model_id} has no evaluated test date",
            f"score and evaluate it first (a `triage run` of its experiment does); then"
            f" `triage plot gains {model_id}` draws the same curve.",
        )
    return result


@router.get("/models/{model_id}/survival")
def model_survival(
    model_id: int,
    horizon: Optional[float] = None,
    as_of_date: Optional[str] = None,
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """The persisted survival set of one date and horizon (``readers.survival_diagnostics``,
    migration 0036): KM by risk decile and group, log-rank, calibration at the horizon,
    Brier(t), AUC(t) and Uno's C. Defaults to the horizon computed most recently and its
    latest date. Survival models only.
    """
    _require_type(pool, "postmodel survival", model_id)
    try:
        result = readers.survival_diagnostics(
            pool, model_id, horizon=horizon, as_of_date=as_of_date
        )
    except readers.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        return _empty(
            "no survival diagnostics persisted for this model"
            + ("" if horizon is None else f" at horizon {horizon:g}"),
            f"run `triage postmodel survival {model_id} --horizon H` — it reads the"
            " model's survival function and its train and test matrices, and persists"
            " the set here.",
        )
    return result


@router.get("/models/{model_id}/near-miss")
def model_near_miss(
    model_id: int,
    parameter: str = "100_abs",
    width: Optional[str] = None,
    as_of_date: Optional[str] = None,
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """The selected, near-miss and rest bands of each scored date (``readers.near_miss``,
    migration 0037): k = the cut over the date's scored rows, and the near-miss band holds
    the next ``width`` rows (the cut itself when omitted). Classification and
    regression_ranking (positive means outcome > 0).
    """
    _require_type(pool, "postmodel near-miss", model_id)
    rows = readers.near_miss(
        pool, model_id, parameter, width=width, as_of_date=as_of_date
    )
    if not rows:
        return _empty(
            f"model {model_id} scored no test row",
            f"score it first (a `triage run` of its experiment does); then `triage"
            f" postmodel near-miss {model_id} -p {parameter}` reads the same bands.",
        )
    return rows


@router.get("/models/{model_id}/prototypes")
def model_prototypes(
    model_id: int,
    parameter: Optional[str] = None,
    as_of_date: Optional[str] = None,
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """The most typical entities of each class (TP, FP, FN, TN) at a cut, as persisted by
    ``triage postmodel prototypes`` (``readers.prototypes``). ``parameter`` keeps one cut;
    omitted, every cut persisted. Classification and regression_ranking.
    """
    _require_type(pool, "postmodel prototypes", model_id)
    rows = readers.prototypes(
        pool, model_id, parameter=parameter, as_of_date=as_of_date
    )
    if not rows:
        return _empty(
            "no prototypes persisted for this model",
            f"run `triage postmodel prototypes {model_id} -p {parameter or '100_abs'}` —"
            " it z-scores the matrix's features and persists each class's nearest"
            " entities here.",
        )
    return rows


@router.get("/experiments/{experiment_hash}/common-rows")
def experiment_common_rows(
    experiment_hash: str,
    groups: Annotated[Optional[list[int]], Query()] = None,
    pool: DictRowPool = Depends(_pool),
) -> Any:
    """Each model group's errors on its own rows and on the rows all of them scored, per test
    date (``readers.common_rows``, migration 0031). ``groups`` (repeatable) picks the groups;
    omitted, every group of the experiment. Nothing is written: ``POST`` on the same path
    registers the common rows as a Subset and evaluates the groups on it.
    """
    from triage.adapters.common_rows import compared_groups

    if (
        _one(
            pool,
            "select 1 as found from triage.experiments where experiment_hash = %(h)s",
            {"h": experiment_hash},
        )
        is None
    ):
        raise HTTPException(status_code=404, detail="experiment not found")
    try:
        ids = compared_groups(pool, experiment_hash, groups)
        rows = readers.common_rows(pool, ids)
    except psycopg.errors.RaiseException as exc:  # a group with two models on one date
        raise HTTPException(
            status_code=422, detail=exc.diag.message_primary or str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not rows:
        return _empty(
            "these model groups have no scored test date",
            f"score them first; then `triage postmodel compare-rows {experiment_hash[:12]}`"
            " reads the same rows.",
        )
    return rows


# ================================================================ project-level
@router.get("/metrics")
def metrics(pool: DictRowPool = Depends(_pool)) -> list[dict[str, Any]]:
    """The (metric, parameter, higher_is_better) catalog present in this project (SPA selectors)."""
    return _rows(
        pool,
        "select metric, parameter, higher_is_better from triage.metric_catalog"
        " order by metric, parameter",
    )


@router.get("/ontology")
def ontology(pool: DictRowPool = Depends(_pool)) -> dict[str, Any]:
    """Per-project data profile: the registered sources + each source's volume over time.

    Source names come from the trusted ``triage.sources`` table; they are still passed as
    bound params to ``source_volume`` (defense in depth — the function also regclass-validates).
    """
    sources = _rows(
        pool,
        "select source_name, relation, knowledge_date_column, description, role, type_column"
        " from triage.sources order by source_name",
    )
    volumes: dict[str, list[dict[str, Any]]] = {}
    volumes_by_type: dict[str, list[dict[str, Any]]] = {}
    profile: dict[str, dict[str, Any]] = {}
    for src in sources:
        name = src["source_name"]
        volumes[name] = _rows(
            pool,
            "select period, n from triage.source_volume(%(name)s, 'month') order by period",
            {"name": name},
        )
        # Per-type series only when the source declares a type_column (e.g. facility_type for
        # entities, inspection type for events); empty otherwise.
        if src.get("type_column"):
            volumes_by_type[name] = _rows(
                pool,
                "select period, type_value, n from triage.source_volume_by_type(%(name)s, 'month')"
                " order by period, type_value",
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
    return {
        "sources": sources,
        "volumes": volumes,
        "volumes_by_type": volumes_by_type,
        "profile": profile,
    }


@router.get("/entities/{entity_id}")
def entity_profile(
    entity_id: int,
    experiment_hash: Optional[str] = None,
    pool: DictRowPool = Depends(_pool),
) -> dict[str, Any]:
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
        "select as_of_date, label_timespan, outcome, duration, event_observed"
        " from triage.entity_label_history(%(e)s, %(hash)s)"
        " order by as_of_date, label_timespan",
        {"e": entity_id, "hash": experiment_hash},
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
def status(pool: DictRowPool = Depends(_pool)) -> dict[str, Any]:
    """Project control panel: DB health, execution mode + compute, source pins/drift, engine
    versions, GC tallies, run counts. (If this endpoint answers at all the project DB is
    reachable — the pool could not connect otherwise.)"""
    sources = _rows(
        pool,
        "select source_name, version_label, registered_at, fingerprint"
        " from triage.current_source_pins order by source_name",
    )
    latest = (
        _one(
            pool,
            "select run_id, plan, profile, purpose, status, started_at, finished_at,"
            "       extract(epoch from (coalesce(finished_at, now()) - started_at))::int"
            "         as duration_s,"
            "       triage_version, git_hash, batch_job_id"
            " from triage.runs where plan is not null order by started_at desc limit 1",
        )
        or {}
    )
    latest_plan = latest.get("plan") or {}
    engine_versions = latest_plan.get("engine_versions")
    # DB health from the live server (pg catalogs) — proves reachability + shows headroom.
    db = (
        _one(
            pool,
            "select current_setting('server_version') as server_version,"
            "       pg_size_pretty(pg_database_size(current_database())) as db_size,"
            "       (select count(*) from pg_stat_activity"
            "          where datname = current_database()) as connections,"
            "       (select setting::int from pg_settings"
            "          where name = 'max_connections') as max_connections,"
            "       (select setting::int from pg_settings"
            "          where name = 'max_parallel_workers') as max_parallel_workers,"
            "       date_trunc('second', now() - pg_postmaster_start_time())::text as uptime",
        )
        or {}
    )
    db["reachable"] = True
    # Execution: how/where the latest run ran (local in-process vs cloud AWS Batch).
    execution = {
        "profile": latest.get("profile"),
        "purpose": latest.get("purpose"),
        "status": latest.get("status"),
        "started_at": latest.get("started_at"),
        "finished_at": latest.get("finished_at"),
        "duration_s": latest.get("duration_s"),
        "triage_version": latest.get("triage_version"),
        "git_hash": latest.get("git_hash"),
        "batch_job_id": latest.get("batch_job_id"),
    }
    compute = latest_plan.get(
        "compute"
    )  # {cpu_count, profile} (run.py telemetry); null on old runs
    # Source drift: the latest run's frozen pins vs the registry's current head per source.
    source_drift = (
        _rows(
            pool,
            "select rsp.source_name, rsp.version_label as run_version,"
            "       csp.version_label as head_version,"
            "       (rsp.version_label is distinct from csp.version_label) as drift"
            " from triage.run_source_pins rsp"
            " left join triage.current_source_pins csp on csp.source_name = rsp.source_name"
            " where rsp.run_id = %(run_id)s order by rsp.source_name",
            {"run_id": latest.get("run_id")},
        )
        if latest.get("run_id")
        else []
    )
    gc = _rows(
        pool,
        "select kind, status, count(*) as n from triage.artifacts"
        " group by kind, status order by kind, status",
    )
    # Where artifacts land on disk/S3 — the parent directory of the matrices (.parquet) and
    # models (.joblib). storage_uri is relative to the project storage root (set at run time).
    artifact_paths = _rows(
        pool,
        "select 'matrix' as kind,"
        "       regexp_replace(storage_uri, '/[^/]+$', '') as dir, count(*) as n"
        " from triage.matrices where storage_uri is not null"
        " group by 2"
        " union all"
        " select 'model', regexp_replace(artifact_uri, '/[^/]+$', ''), count(*)"
        " from triage.models where artifact_uri is not null"
        " group by 2 order by kind, dir",
    )
    # Experiments overview (not just runs): one row per non-archived experiment.
    experiments = _rows(
        pool,
        "select experiment_hash, name, n_runs, n_models, last_status, last_started_at"
        " from triage.experiment_summary where archived_at is null"
        " order by last_started_at desc nulls last",
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
        "experiments": experiments,
        "artifact_paths": artifact_paths,
        "db": db,
        "execution": execution,
        "compute": compute,
        "source_drift": source_drift,
    }


@router.get("/derivation")
def project_derivation(pool: DictRowPool = Depends(_pool)) -> dict[str, Any]:
    """Project-wide derivation graph: every artifact + its inputs, with cross-experiment sharing.

    Nodes carry ``n_experiments``/``n_runs`` from ``triage.artifact_sharing``; a node touched
    by >1 experiment is shared (the graph highlights it).
    """
    nodes = _rows(
        pool,
        "select a.artifact_id, a.kind, a.status, a.built_by_run,"
        "       coalesce(s.n_experiments, 0) as n_experiments,"
        "       coalesce(s.n_runs, 0)        as n_runs,"
        + _NODE_SPLIT_SELECT
        + " from triage.artifacts a"
        " left join triage.artifact_sharing s on s.artifact_id = a.artifact_id"
        + _NODE_SPLIT_JOINS,
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
    request: Request, run_id: UUID, pool: DictRowPool = Depends(_pool)
) -> StreamingResponse:
    """SSE endpoint: a long-lived ``LISTEN run_progress`` connection."""
    # Borrow the pool's conninfo only to open a SEPARATE dedicated connection (the stream must
    # not tie up a request-pool connection for its lifetime).
    conninfo = str(pool.conninfo)
    return StreamingResponse(
        _run_progress_events(request, conninfo, str(run_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------- monitoring (ADR-0027)
# Thin reads over migration 0012 — everything the Monitoring view shows IS these
# views/functions (ADR-0012: no UI-only logic). Windows are parameters (the pinned-reference
# policy is the operator's convention, docs/monitoring.md).


@router.get("/monitoring/volume")
def monitoring_volume(
    model_group_id: Optional[int] = None, pool: DictRowPool = Depends(_pool)
) -> list[dict[str, Any]]:
    """The scoring heartbeat: predictions + distinct entities per (group, model, split, day)."""
    sql = "select * from triage.monitoring_volume"
    params: dict[str, Any] = {}
    if model_group_id is not None:
        sql += " where model_group_id = %(g)s"
        params["g"] = model_group_id
    sql += " order by scored_on, model_id"
    return _rows(pool, sql, params)


@router.get("/monitoring/drift")
def monitoring_drift(
    model_group_id: int,
    reference_from: str,
    reference_to: str,
    window_from: str,
    window_to: str,
    pool: DictRowPool = Depends(_pool),
) -> dict[str, Any]:
    """PSI + KS between a pinned reference window and a scoring window (scored_at bounds)."""
    row = _one(
        pool,
        "select * from triage.monitoring_score_drift(%(g)s, cast(%(rf)s as timestamptz),"
        " cast(%(rt)s as timestamptz), cast(%(wf)s as timestamptz),"
        " cast(%(wt)s as timestamptz))",
        {
            "g": model_group_id,
            "rf": reference_from,
            "rt": reference_to,
            "wf": window_from,
            "wt": window_to,
        },
    )
    return row or {"psi": None, "ks": None, "n_reference": 0, "n_window": 0}


@router.get("/monitoring/outcomes")
def monitoring_outcomes(
    model_group_id: int,
    metric: Optional[str] = None,
    pool: DictRowPool = Depends(_pool),
) -> list[dict[str, Any]]:
    """Realized metrics over time (evaluations upserts sequenced per model group).

    Full-cohort rows only (``subset_hash = ''``, migration 0018): monitoring is the
    cohort-level ops surface — subset-filtered evaluations would silently mix scales.
    """
    sql = (
        "select * from triage.monitoring_outcome_tracking"
        " where model_group_id = %(g)s and subset_hash = ''"
    )
    params: dict[str, Any] = {"g": model_group_id}
    if metric:
        sql += " and metric = %(m)s"
        params["m"] = metric
    sql += " order by as_of_date, computed_at"
    return _rows(pool, sql, params)
