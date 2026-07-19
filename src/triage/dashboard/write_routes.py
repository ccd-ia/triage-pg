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
import pathlib
from typing import Any, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from triage import project_lifecycle, registry
from triage.adapters.run import validate_experiment_config
from triage.dashboard.auth import (
    Principal,
    _registry_pool,
    current_principal,
    require_admin,
)
from triage.dashboard.project_routing import pool_for_slug, project_dburl
from triage.logging import get_logger
from triage.profiles.execution import RunHandle
from triage.util.db import DictRowPool

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
    config: Optional[dict[str, Any]] = Field(
        None, description="the greenfield experiment_config (JSON object)"
    )
    config_text: Optional[str] = Field(
        None,
        description="the config as raw YAML/JSON text — exactly what `triage run` consumes;"
        " provide this OR `config`, not both",
    )
    profile: str = Field(
        "local", description="'local' (in-process) or 'cloud' (AWS Batch)"
    )


class ConfigPayload(BaseModel):
    """A config either as a parsed object or as raw YAML/JSON text (exactly one)."""

    config: Optional[dict[str, Any]] = None
    config_text: Optional[str] = None


# --------------------------------------------------------------------------- helpers


def default_experiment_runner(
    pool: DictRowPool, config: dict[str, Any], *, profile: str = "local"
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


def _resolve_config(
    config: Optional[dict[str, Any]], config_text: Optional[str]
) -> dict[str, Any]:
    """Resolve the exactly-one-of (parsed object | raw YAML/JSON text) config payload.

    YAML is a superset of JSON, so one ``yaml.safe_load`` handles both text forms — users can
    paste/upload exactly the ``experiment.yaml`` that ``triage run`` consumes. Errors are 400s
    with the parser's message (a client mistake, not a server fault)."""
    if (config is None) == (config_text is None):
        raise HTTPException(
            status_code=400,
            detail="provide exactly one of 'config' (object) or 'config_text' (YAML/JSON text)",
        )
    if config is not None:
        return config
    try:
        parsed = yaml.safe_load(config_text or "")
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=400, detail=f"config_text is not valid YAML/JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail="config_text must parse to a mapping (the experiment config object)",
        )
    return parsed


_EXAMPLES_DIR_ENV = "TRIAGE_EXAMPLES_DIR"


def _examples_dir() -> Optional[pathlib.Path]:
    """The committed example-config directory (env override, else the repo checkout)."""
    override = os.environ.get(_EXAMPLES_DIR_ENV)
    if override:
        path = pathlib.Path(override)
        return path if path.is_dir() else None
    candidate = pathlib.Path(__file__).resolve().parents[3] / "example"
    return candidate if candidate.is_dir() else None


# --------------------------------------------------------------------------- identity


@write_router.get("/me")
def whoami(
    request: Request, principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    """The resolved caller identity (a sanity check on the auth seam)."""
    return {
        "user_id": principal.user_id,
        "email": principal.email,
        "display_name": principal.display_name,
        "is_admin": principal.is_admin,
        # 'trusted' | 'oidc' — the SPA adapts (logout link only exists under oidc, ADR-0028).
        "auth_mode": getattr(request.app.state.auth_backend, "mode", "trusted"),
    }


# --------------------------------------------------------------------------- projects


@write_router.get("/projects")
def get_projects(
    request: Request,
    include_archived: bool = False,
    principal: Principal = Depends(current_principal),
) -> list[dict[str, Any]]:
    return registry.list_projects(
        _registry_pool(request), include_archived=include_archived
    )


@write_router.post("/projects", status_code=201)
def post_project(
    body: ProjectCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
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
    # The webapp creates the registry ROW only; database provisioning is CLI-only
    # ('triage project create' — least privilege, the app holds no CREATEDB credentials).
    # database_ready keeps the UI honest about that two-step reality.
    project = dict(project)
    base_url = getattr(request.app.state, "base_project_url", None)
    try:
        url = project_dburl(project["slug"], project["database_name"], base_url)
        project["database_ready"] = project_lifecycle.database_ready(url)
    except ValueError:
        project["database_ready"] = False
    return project


@write_router.get("/projects/{slug}/members")
def get_members(
    slug: str, request: Request, principal: Principal = Depends(current_principal)
) -> list[dict[str, Any]]:
    reg = _registry_pool(request)
    project = registry.get_project(reg, slug)
    if project is None:
        raise HTTPException(status_code=404, detail=f"no project {slug!r}")
    return registry.list_members(reg, project_id=project["project_id"])


# --------------------------------------------------------------------------- config tooling


@write_router.post("/validate-config")
def post_validate_config(
    body: ConfigPayload,
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Dry-run validation of an experiment config — nothing persisted, nothing run.

    Accepts the config as a JSON object (``config``) or raw YAML/JSON text (``config_text``,
    exactly what ``triage run`` consumes). Returns the core's structured verdict: the derived
    ADR-0022 ``experiment_hash``, split/grid counts, and path-addressed errors. YAML parse
    failures come back as a verdict too (``valid: false``) so the UI renders them inline
    rather than as a transport error.
    """
    if (body.config is None) == (body.config_text is None):
        raise HTTPException(
            status_code=400,
            detail="provide exactly one of 'config' (object) or 'config_text' (YAML/JSON text)",
        )
    if body.config is not None:
        config = body.config
    else:
        try:
            config = yaml.safe_load(body.config_text or "")
            if not isinstance(config, dict):
                raise ValueError("config_text must parse to a mapping")
        except (yaml.YAMLError, ValueError) as exc:
            return {
                "valid": False,
                "experiment_hash": None,
                "problem_type": None,
                "n_splits": None,
                "n_models": None,
                "n_feature_groups": None,
                "errors": [{"path": "$", "message": f"not valid YAML/JSON: {exc}"}],
                "warnings": [],
            }
    return validate_experiment_config(config)


def _temporal_blocks(temporal: dict[str, Any]) -> list[dict[str, Any]]:
    """Compute a temporal_config's cross-validation blocks as plain ISO dates for the SPA.

    Builds Timechop and returns, per split, the train/validation as-of-date spans, the
    per-as-of-date list, and the label-window horizon (last as-of + label timespan) — all as
    ISO date strings, so the frontend renders with no server-side plotting and no timespan math.
    """
    from triage.component.timechop import Timechop
    from triage.util.conf import convert_str_to_relativedelta

    def _matrix(m: dict[str, Any], span_key: str) -> dict[str, Any]:
        aost = m["as_of_times"]
        last = max(aost)
        span = m[span_key]
        return {
            "first_as_of": min(aost).date().isoformat(),
            "last_as_of": last.date().isoformat(),
            "label_end": (last + convert_str_to_relativedelta(span)).date().isoformat(),
            "as_of_dates": [d.date().isoformat() for d in aost],
            "label_timespan": span,
            "n_as_of": len(aost),
        }

    chopper = Timechop(**temporal)
    blocks: list[dict[str, Any]] = []
    for chop in chopper.chop_time():
        blocks.append(
            {
                "train": _matrix(chop["train_matrix"], "training_label_timespan"),
                "validation": _matrix(chop["test_matrices"][0], "test_label_timespan"),
                "feature_start": chop["feature_start_time"].date().isoformat(),
            }
        )
    return blocks


@write_router.post("/temporal-viz")
def post_temporal_viz(
    body: ConfigPayload,
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """A config's temporal cross-validation blocks, for the dashboard temporal-config viz.

    Same input contract as ``/validate-config`` (a ``config`` object or ``config_text``).
    Nothing persisted, nothing run — just Timechop over the ``temporal_config``.
    """
    if (body.config is None) == (body.config_text is None):
        raise HTTPException(
            status_code=400,
            detail="provide exactly one of 'config' (object) or 'config_text' (YAML/JSON text)",
        )
    if body.config is not None:
        config = body.config
    else:
        try:
            parsed = yaml.safe_load(body.config_text or "")
            if not isinstance(parsed, dict):
                raise ValueError("config_text must parse to a mapping")
            config = parsed
        except (yaml.YAMLError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"not valid YAML/JSON: {exc}")
    temporal = config.get("temporal_config")
    if not isinstance(temporal, dict) or not temporal:
        raise HTTPException(
            status_code=422, detail="config has no temporal_config block"
        )
    try:
        return {"splits": _temporal_blocks(temporal)}
    except (ValueError, KeyError, TypeError) as exc:
        # Timechop config errors (bad dates, impossible windows) -> a 422 the UI renders inline.
        logger.warning(f"temporal-viz: invalid temporal_config: {exc}")
        raise HTTPException(status_code=422, detail=f"invalid temporal_config: {exc}")


@write_router.get("/example-configs")
def get_example_configs(
    principal: Principal = Depends(current_principal),
) -> list[dict[str, Any]]:
    """The committed ``example/*/experiment*.yaml`` configs, for the submit-form picker.

    Served by the backend so the SPA needs no filesystem access; an instance without an
    example checkout (or ``TRIAGE_EXAMPLES_DIR``) just returns an empty list.
    """
    root = _examples_dir()
    if root is None:
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/experiment*.yaml")):
        content = path.read_text(encoding="utf-8")
        description = next(
            (
                line.lstrip("# ").strip()
                for line in content.splitlines()
                if line.startswith("#") and line.lstrip("# ").strip()
            ),
            "",
        )
        entries.append(
            {
                "name": f"{path.parent.name}/{path.name}",
                "dataset": path.parent.name,
                "filename": path.name,
                "description": description,
                "content": content,
            }
        )
    return entries


@write_router.get("/batch-status/{job_id}")
def get_batch_status(
    job_id: str, principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    """On-request AWS Batch job status for a cloud submission (cloud-profile-spec §7).

    Read-only and pull-based — no background polling thread; the CLI backfill
    (``triage runs status``) is the state-mutating path.
    """
    region = os.environ.get("AWS_REGION")
    if not region:
        raise HTTPException(
            status_code=503,
            detail="AWS_REGION is not configured on this instance (cloud profile only)",
        )
    from triage.profiles.execution import batch_job_status

    return batch_job_status(job_id, region=region)


# --------------------------------------------------------------------------- submissions


@write_router.get("/submissions")
def get_submissions(
    request: Request,
    project_slug: Optional[str] = None,
    limit: int = 100,
    principal: Principal = Depends(current_principal),
) -> list[dict[str, Any]]:
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
) -> dict[str, Any]:
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

    config = _resolve_config(body.config, body.config_text)
    _validate_config(config)

    # Run against the TARGET project's database (ADR-0025 routing), not just the bound pool — so a
    # submission lands in the project it names. Falls back to the bound pool for the same database.
    target_pool = pool_for_slug(request, project["slug"], project["database_name"])
    runner = getattr(request.app.state, "experiment_runner", default_experiment_runner)
    handle: RunHandle = runner(target_pool, config, profile=body.profile)

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
