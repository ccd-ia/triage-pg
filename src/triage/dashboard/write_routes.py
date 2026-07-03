"""Write-surface endpoints: projects + experiment submissions (ADR-0002/0024).

The *write* half of the dashboard app — the read half (:mod:`triage.dashboard.routes`) is pure
``SELECT`` over ``triage.*`` views; this half creates control-plane rows (``registry.*``) and
submits experiments. It stays as thin as the read half: every handler is auth → a
:mod:`triage.registry` call and/or the injected experiment runner. No business logic beyond
authz + shape validation (ADR-0012 headless-complete core: submitting an experiment here calls the
SAME ``run_experiment`` the CLI does).

Two pools are in play: the **registry** pool (control plane — projects/users/submissions, via
:func:`auth._registry_pool`) and the **project** pool (the bound results DB the experiment runs
against, ``app.state.pool``). v1 binds ONE project DB per app instance; per-project-DB routing
across many databases (the full ADR-0002 multi-tenant cluster) is a documented later step — the
registry already records each project's ``database_name`` for it.

The experiment runner is injectable (``app.state.experiment_runner``) so tests exercise the route
without a real training run; the default runs in-process locally / submits a Batch job in cloud.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from triage import registry
from triage.dashboard.auth import (
    Principal,
    _registry_pool,
    current_principal,
    require_admin,
)
from triage.dashboard.project_routing import pool_for_slug
from triage.logging import get_logger
from triage.profiles.execution import RunHandle

logger = get_logger(__name__)

write_router = APIRouter()

# The four keys that identify the prediction problem (ADR-0022) plus the two that make a runnable
# attempt. A friendly 400 lists what's missing; deeper validation is run_experiment's job.
_REQUIRED_CONFIG_KEYS = (
    "problem_type",
    "cohort_config",
    "label_config",
    "temporal_config",
    "feature_config",
    "grid_config",
)


# --------------------------------------------------------------------------- request models


class ProjectCreate(BaseModel):
    slug: str = Field(
        ..., description="url-safe id; also names the per-project database (ADR-0002)"
    )
    display_name: str
    database_name: Optional[str] = Field(
        None, description="target DB in the cluster; defaults to the slug"
    )


class SubmissionCreate(BaseModel):
    project_slug: str
    config: dict[str, Any] = Field(..., description="the greenfield experiment_config")
    profile: str = Field(
        "local", description="'local' (in-process) or 'cloud' (AWS Batch)"
    )


# --------------------------------------------------------------------------- helpers


def default_experiment_runner(
    pool: ConnectionPool, config: dict[str, Any], *, profile: str = "local"
) -> RunHandle:
    """Run/submit an experiment via the profile seam (the same path as ``triage run``).

    Local: build local FS storage + in-process execution and run synchronously (this blocks the
    request — acceptable for v1; a background-job queue is a later enhancement). Cloud: build the
    full cloud profile from the environment (fails loud on a missing var) and submit one Batch job,
    returning immediately with its ``job_id``.
    """
    from triage.profiles import InProcessExecution, LocalStorage, load_profile

    if profile == "local":
        storage_root = os.environ.get("TRIAGE_PROJECT_PATH") or os.getcwd()
        return InProcessExecution().run(
            pool,
            config,
            storage=LocalStorage(),
            storage_root=storage_root,
            random_seed=0,
            profile="local",
            cache_policy="exact",
        )
    if profile == "cloud":
        prof = load_profile("cloud")
        return prof.execution.run(
            pool,
            config,
            storage=prof.storage,
            storage_root=prof.storage_root,
            random_seed=0,
            profile="cloud",
            cache_policy="exact",
        )
    raise HTTPException(
        status_code=400, detail=f"unknown profile {profile!r} (use local/cloud)"
    )


def _validate_config(config: dict[str, Any]) -> None:
    missing = [k for k in _REQUIRED_CONFIG_KEYS if k not in config]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"experiment_config is missing required key(s): {', '.join(missing)}",
        )


# --------------------------------------------------------------------------- identity


@write_router.get("/me")
def whoami(principal: Principal = Depends(current_principal)) -> dict:
    """The resolved caller identity (a sanity check on the auth seam)."""
    return {
        "user_id": principal.user_id,
        "email": principal.email,
        "display_name": principal.display_name,
        "is_admin": principal.is_admin,
    }


# --------------------------------------------------------------------------- projects


@write_router.get("/projects")
def get_projects(
    request: Request,
    include_archived: bool = False,
    principal: Principal = Depends(current_principal),
) -> list[dict]:
    return registry.list_projects(
        _registry_pool(request), include_archived=include_archived
    )


@write_router.post("/projects", status_code=201)
def post_project(
    body: ProjectCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> dict:
    """Create a project (admin only) and make the creator its owner."""
    reg = _registry_pool(request)
    try:
        project = registry.create_project(
            reg,
            slug=body.slug,
            display_name=body.display_name,
            database_name=body.database_name,
        )
    except ValueError as exc:  # bad slug — a client error, surfaced verbatim
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    registry.add_member(
        reg, project_id=project["project_id"], user_id=principal.user_id, role="owner"
    )
    return project


@write_router.get("/projects/{slug}/members")
def get_members(
    slug: str, request: Request, principal: Principal = Depends(current_principal)
) -> list[dict]:
    reg = _registry_pool(request)
    project = registry.get_project(reg, slug)
    if project is None:
        raise HTTPException(status_code=404, detail=f"no project {slug!r}")
    return registry.list_members(reg, project_id=project["project_id"])


# --------------------------------------------------------------------------- submissions


@write_router.get("/submissions")
def get_submissions(
    request: Request,
    project_slug: Optional[str] = None,
    limit: int = 100,
    principal: Principal = Depends(current_principal),
) -> list[dict]:
    reg = _registry_pool(request)
    project_id = None
    if project_slug is not None:
        project = registry.get_project(reg, project_slug)
        if project is None:
            raise HTTPException(status_code=404, detail=f"no project {project_slug!r}")
        project_id = project["project_id"]
    return registry.list_submissions(reg, project_id=project_id, limit=limit)


@write_router.post("/submissions", status_code=201)
def post_submission(
    body: SubmissionCreate,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> dict:
    """Submit an experiment: authz → run/submit via the profile seam → record the audit row.

    Runs against the app's bound project pool (v1: one project DB per instance). The registry
    submission row is the append-only audit trail (who submitted what, where it routed).
    """
    reg = _registry_pool(request)
    project = registry.get_project(reg, body.project_slug)
    if project is None:
        raise HTTPException(status_code=404, detail=f"no project {body.project_slug!r}")

    # authz: admins may submit anywhere; otherwise the caller must be an owner/contributor.
    role = registry.member_role(
        reg, project_id=project["project_id"], user_id=principal.user_id
    )
    if not principal.is_admin and role not in ("owner", "contributor"):
        raise HTTPException(
            status_code=403,
            detail=f"not authorized to submit to {body.project_slug!r} (role={role})",
        )

    _validate_config(body.config)

    # Run against the TARGET project's database (ADR-0025 routing), not just the bound pool — so a
    # submission lands in the project it names. Falls back to the bound pool for the same database.
    target_pool = pool_for_slug(request, project["slug"], project["database_name"])
    runner = getattr(request.app.state, "experiment_runner", default_experiment_runner)
    handle: RunHandle = runner(target_pool, body.config, profile=body.profile)

    result = handle.run_result
    experiment_hash = result.experiment_hash if result is not None else None
    submission = registry.record_submission(
        reg,
        project_id=project["project_id"],
        submitted_by=principal.user_id,
        experiment_hash=experiment_hash,
        profile=body.profile,
        batch_job_id=handle.batch_job_id,
    )

    # Summarize the run for the caller (cloud is async → only the Batch job id is known yet).
    run_summary: dict[str, Any]
    if result is not None:
        run_summary = {
            "experiment_hash": result.experiment_hash,
            "problem_type": result.problem_type,
            "num_runs": result.num_runs,
            "num_models": result.num_models,
            "num_predictions": result.num_predictions,
            "num_evaluations": result.num_evaluations,
        }
    else:
        run_summary = {
            "batch_job_id": handle.batch_job_id,
            "config_uri": handle.config_uri,
            "status": "submitted",
        }
    return {"submission": submission, "result": run_summary}
