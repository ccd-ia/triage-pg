"""Greenfield run orchestration — the full cohort→labels→matrix→model→eval pipeline (ADR-0012, ADR-0014).

This is the capstone adapter: it wires the F1/F2/F3 builders into ONE pass that executes a
whole experiment with the derivation lifecycle, source pinning, and a complete run lineage.
It is the headless-complete core (ADR-0012) — the single entry point a CLI / Batch job calls;
it owns no business logic the builders don't, it only *sequences* them and records the
experiment + run rows the artifact DAG hangs off of.

It is **additive** (a new module). It deliberately does NOT touch the inherited
``experiments/base.py`` ``SingleThreadedExperiment`` flow (removed in a later infra task); the
inherited orchestration and this one do not share code.

Plan → build, in order
----------------------
1. **Experiment + run rows.** ``experiment_hash`` is a stable SHA-256 over the canonical
   experiment_config (``triage.derivation.canonical_json``); the experiment row is
   INSERT-or-get (a re-run of the same config reuses it). A ``triage.runs`` row is then
   created (``run_id`` from the DB ``gen_random_uuid()`` default, ``status='started'``).
2. **Source pinning at plan time (ADR-0014).** Every declared source is ``register_source``
   +d and ``bump_source``ed (so it carries a *pinned* version and is therefore cacheable),
   then ``resolve_pins`` freezes the current pin per source and ``record_run_pins`` persists
   the frozen set on the run. The *same* frozen pins thread into every ``build_*`` call — this
   is what lets a second run with the same config + same pinned sources be a cache hit
   (an unpinned source is volatile and would force a rebuild every run).
3. **Splits.** A :class:`TemporalConfig` is built from the config and fed to the inherited
   :class:`Timechop`. We take the UNION of every split's train+test ``as_of_dates`` for the
   ONE cohort + ONE labels build (per F2's note: the cohort/labels span all dates and each
   matrix inner-joins its own split's dates — per-split dates do NOT enter the cohort/labels
   derivation config, only the global window does).
4. **Cohort then labels** over the union of dates.
5. **Per split**: a train matrix (its split's train ``as_of_dates``) then a test matrix (its
   split's test ``as_of_dates``, with ``train_matrix_artifact_id`` set so the train-fitted
   imputation stats flow across the leakage boundary, ADR-0009).
6. **Grid × split**: for each model spec (``class_path`` + ``hyperparameters``) and each
   split's train matrix, ``build_model`` then ``score_and_evaluate`` over the test matrix.
7. **Terminal status.** On success the run is marked ``completed``; on any exception the run
   is marked ``failed`` and the exception re-raised (fail fast, CLAUDE.md error policy).

The cohort/labels/matrix/model builders each run their own derivation lifecycle (cache lookup
→ begin → build → mark_built → record_use); the orchestrator's job is the experiment/run rows,
the source pins, and the sequencing — not the artifact bookkeeping the builders already own.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from triage.adapters.bias import (
    INTERVENTION_PRIMARY_METRIC,
    ingest_protected_groups,
    validate_bias_config,
)
from triage.adapters.cohort import build_cohort
from triage.adapters.feature_groups import (
    DEFAULT_ALL_COMBINATIONS_MAX_GROUPS,
    FeatureSubset,
    mix_strategies,
    partition_features,
)
from triage.adapters.imputation import ImputationPolicy
from triage.adapters.labels import build_labels
from triage.adapters.matrix import MatrixResult, build_matrix
from triage.adapters.model import build_model, score_and_evaluate
from triage.adapters.subsets import register_subsets, validate_subsets_config
from triage.adapters.temporal import TemporalConfig
from triage.artifacts import _notify_run_progress, record_use
from triage.component.timechop import Timechop
from triage.derivation import canonical_json, engine_versions_for
from triage.logging import get_logger
from triage.profiles.protocols import StorageAdapter
from triage.sources import (
    bump_source,
    record_run_pins,
    register_source,
    resolve_pins,
)

logger = get_logger(__name__)

__all__ = [
    "run_experiment",
    "ExperimentResult",
    "RunResult",
    "SplitResult",
    "experiment_hash_for",
    "validate_experiment_config",
]


@dataclass(frozen=True)
class SplitResult:
    """One temporal split's built artifacts + the models scored on it."""

    train_as_of_dates: list[date]
    test_as_of_dates: list[date]
    train_matrix: MatrixResult
    test_matrix: MatrixResult
    model_ids: list[int] = field(default_factory=list)
    model_artifact_ids: list[str] = field(default_factory=list)
    num_predictions: int = 0
    num_evaluations: int = 0


@dataclass(frozen=True)
class RunResult:
    """What :func:`run_experiment` returns: the run lineage + per-split build outcomes."""

    run_id: str
    experiment_hash: str
    problem_type: str
    cohort_artifact_id: str
    labels_artifact_id: str
    source_pins: dict[str, str | None]
    splits: list[SplitResult]
    model_ids: list[int]
    num_models: int
    num_predictions: int
    num_evaluations: int
    feature_group: str = "all-features"
    """The feature-group subset this run attacked the problem with (ADR-0023). Default
    ``'all-features'`` when no feature_groups are configured (one implicit group)."""


@dataclass(frozen=True)
class ExperimentResult:
    """What :func:`run_experiment` returns: one Experiment (the problem) and its Runs.

    An Experiment is the prediction problem (cohort+label+temporal+problem_type, ADR-0022); each
    Run is one attempt at it. Without feature groups there is exactly one Run (``runs[0]``); with
    feature-group strategies (ADR-0023) there is one Run per feature subset, all sharing
    ``experiment_hash``, the cohort/labels, and the splits — so their leaderboards are directly
    comparable.
    """

    experiment_hash: str
    problem_type: str
    cohort_artifact_id: str
    labels_artifact_id: str
    source_pins: dict[str, str | None]
    runs: list[RunResult]

    @property
    def num_runs(self) -> int:
        return len(self.runs)

    @property
    def num_models(self) -> int:
        return sum(r.num_models for r in self.runs)

    @property
    def num_predictions(self) -> int:
        return sum(r.num_predictions for r in self.runs)

    @property
    def num_evaluations(self) -> int:
        return sum(r.num_evaluations for r in self.runs)


# An Experiment IS the prediction problem (ADR-0022): the matrix rows (cohort), the target y
# (label, which carries problem_type), and the train/test splits (temporal). features/grid/
# imputation are how you ATTACK the problem — they belong to the Run, not the experiment, so
# they are deliberately NOT part of identity. Changing cohort/label/temporal is a new problem.
_PROBLEM_KEYS = ("cohort_config", "label_config", "temporal_config", "problem_type")

# task_framing names the OBSERVATION REGIME (who gets a label and why), orthogonal to
# problem_type (which drives the scoring machinery). Identity-neutral by construction —
# the hash covers only _PROBLEM_KEYS — so tagging an existing config never forks it
# (migration 0019, plan P12.4).
_TASK_FRAMINGS = ("early_warning", "resource_prioritization", "visit_level")


def _problem_identity(experiment_config: Mapping[str, Any]) -> dict[str, Any]:
    """The problem triple (+ problem_type) that identifies an Experiment (ADR-0022).

    Two configs differing only in features, grid, imputation, source pins, or name/description
    are the SAME experiment (one problem) attacked by different runs. Only the cohort, label,
    and temporal config — the matrix rows, target, and splits — define the problem.
    """
    return {k: experiment_config.get(k) for k in _PROBLEM_KEYS}


def experiment_hash_for(experiment_config: Mapping[str, Any]) -> str:
    """Stable SHA-256 identity for an experiment = its prediction PROBLEM (ADR-0022).

    Hashes only the canonical ``cohort_config + label_config + temporal_config + problem_type``
    (via :func:`triage.derivation.canonical_json`), so a re-run that merely adds features or
    models is the SAME experiment (a new run), while a different cohort/label/temporal is a new
    experiment. features/grid/imputation/sources are excluded — they are the run's attempt and
    are recorded on ``runs.plan`` instead.
    """
    return hashlib.sha256(
        canonical_json(_problem_identity(experiment_config)).encode("ascii")
    ).hexdigest()


# The triage.problem_type enum values (migration 0001, ADR-0010).
_PROBLEM_TYPES = ("classification", "regression_ranking", "regression", "survival")


def validate_experiment_config(experiment_config: Mapping[str, Any]) -> dict[str, Any]:
    """Dry-run config validation: structured errors + derived identity, nothing persisted.

    Mirrors the checks :func:`run_experiment` would fail on — required keys, the
    ``problem_type`` enum, query placeholders, the typed temporal/imputation configs, grid
    expansion — WITHOUT touching a database or building anything. The write webapp's
    ``POST /api/validate-config`` is a thin wrapper over this (ADR-0012: validation is core
    logic, not UI logic). Returns::

        {valid, experiment_hash, problem_type, n_splits, n_models, n_feature_groups,
         errors: [{path, message}], warnings: [str]}

    ``experiment_hash`` is the ADR-0022 problem identity — derivable whenever the four problem
    keys are present, even if deeper checks fail (presence fixes identity). ``n_models`` is the
    grid size per split; ``n_feature_groups`` is only known pre-run for explicit
    ``definitions`` (``group_by`` partitions are discovered from featurizer's columns at
    run time).
    """
    errors: list[dict[str, str]] = []
    warnings: list[str] = []

    def _err(path: str, message: str) -> None:
        errors.append({"path": path, "message": message})

    required = (
        "problem_type",
        "cohort_config",
        "label_config",
        "temporal_config",
        "feature_config",
        "grid_config",
    )
    for key in required:
        if key not in experiment_config:
            _err(key, "required key is missing")

    problem_type = experiment_config.get("problem_type")
    if problem_type is not None and problem_type not in _PROBLEM_TYPES:
        _err(
            "problem_type",
            f"unknown problem_type {problem_type!r} — expected one of {list(_PROBLEM_TYPES)}",
        )
    if problem_type == "survival":
        import importlib.util

        if importlib.util.find_spec("sksurv") is None:
            _err(
                "problem_type",
                "problem_type 'survival' requires the survival extra (scikit-survival) —"
                " install with `uv sync --extra survival` (ADR-0026)",
            )

    cohort_config = experiment_config.get("cohort_config")
    if cohort_config is not None:
        if not isinstance(cohort_config, Mapping) or not cohort_config.get("query"):
            _err("cohort_config.query", "cohort_config needs a 'query'")
        elif "{as_of_date}" not in cohort_config["query"]:
            _err(
                "cohort_config.query",
                "the cohort query must contain the {as_of_date} placeholder",
            )

    label_config = experiment_config.get("label_config")
    if label_config is not None:
        if not isinstance(label_config, Mapping) or not label_config.get("query"):
            _err("label_config.query", "label_config needs a 'query'")
        else:
            for placeholder in ("{as_of_date}", "{label_timespan}"):
                if placeholder not in label_config["query"]:
                    _err(
                        "label_config.query",
                        f"the label query must contain the {placeholder} placeholder",
                    )

    n_splits: int | None = None
    raw_temporal = experiment_config.get("temporal_config")
    if raw_temporal is not None:
        try:
            temporal = TemporalConfig.model_validate(raw_temporal)
            n_splits = len(_generate_splits(temporal))
        except ValidationError as exc:
            for e in exc.errors():
                loc = ".".join(str(part) for part in e["loc"])
                _err(f"temporal_config.{loc}" if loc else "temporal_config", e["msg"])
        except ValueError as exc:
            _err("temporal_config", str(exc))

    try:
        ImputationPolicy.model_validate(
            experiment_config.get("imputation_config", {"all": {"type": "zero"}})
        )
    except ValidationError as exc:
        for e in exc.errors():
            loc = ".".join(str(part) for part in e["loc"])
            _err(f"imputation_config.{loc}" if loc else "imputation_config", e["msg"])

    n_models: int | None = None
    grid_config = experiment_config.get("grid_config")
    if grid_config is not None:
        if not isinstance(grid_config, Mapping):
            _err(
                "grid_config",
                "grid_config must be a mapping {class_path: {hyperparam: [values]}}",
            )
        else:
            try:
                n_models = len(_grid_specs(grid_config))
            except ValueError as exc:
                _err("grid_config", str(exc))

    n_feature_groups: int | None = None
    feature_config = experiment_config.get("feature_config")
    if feature_config is not None:
        if not isinstance(feature_config, Mapping) or not feature_config:
            _err(
                "feature_config",
                "feature_config must be a non-empty mapping (the featurizer ER-graph config)",
            )
        else:
            groups = feature_config.get("feature_groups")
            if isinstance(groups, Mapping) and isinstance(
                groups.get("definitions"), Mapping
            ):
                n_feature_groups = len(groups["definitions"])

    task_framing = experiment_config.get("task_framing")
    if task_framing is not None and task_framing not in _TASK_FRAMINGS:
        _err(
            "task_framing",
            f"unknown task_framing {task_framing!r} — expected one of {list(_TASK_FRAMINGS)}",
        )

    bias_config = experiment_config.get("bias_config")
    if bias_config is not None:
        if not isinstance(bias_config, Mapping):
            _err("bias_config", "bias_config must be a mapping")
        else:
            # mirror validate_bias_config's fail-fast checks as path-addressed errors
            if not bias_config.get("query"):
                _err(
                    "bias_config.query",
                    "bias_config needs a 'query' returning entity_id + one column per"
                    " protected attribute",
                )
            elif "{as_of_date}" not in bias_config["query"]:
                _err(
                    "bias_config.query",
                    "the bias query must contain the {as_of_date} placeholder",
                )
            if not bias_config.get("parameter"):
                _err(
                    "bias_config.parameter",
                    "bias_config needs 'parameter' — the top-k cut the audit runs at"
                    " (e.g. '100_abs' or '10_pct')",
                )
            tau = bias_config.get("tau", 0.8)
            if (
                not isinstance(tau, (int, float))
                or isinstance(tau, bool)
                or not 0 < tau <= 1
            ):
                _err(
                    "bias_config.tau",
                    f"tau must be a number in (0, 1], got {tau!r}"
                    " (0.8 is the four-fifths rule)",
                )
            intervention = bias_config.get("intervention")
            if (
                intervention is not None
                and intervention not in INTERVENTION_PRIMARY_METRIC
            ):
                _err(
                    "bias_config.intervention",
                    f"unknown intervention {intervention!r} — expected one of"
                    f" {sorted(INTERVENTION_PRIMARY_METRIC)}",
                )
            ref_groups = bias_config.get("ref_groups")
            if ref_groups is not None and not isinstance(ref_groups, Mapping):
                _err(
                    "bias_config.ref_groups",
                    "ref_groups must be a mapping {attribute: reference_value}",
                )

    subsets = (experiment_config.get("evaluation") or {}).get("subsets")
    if subsets is not None:
        if not isinstance(subsets, (list, tuple)):
            _err(
                "evaluation.subsets",
                "subsets must be a list of {name, query} mappings",
            )
        else:
            seen_names: set[str] = set()
            for i, subset in enumerate(subsets):
                path = f"evaluation.subsets[{i}]"
                if not isinstance(subset, Mapping):
                    _err(path, "each subset must be a mapping with 'name' and 'query'")
                    continue
                name = subset.get("name")
                if not name:
                    _err(f"{path}.name", "subset needs a 'name'")
                elif name in seen_names:
                    _err(f"{path}.name", f"duplicate subset name {name!r}")
                else:
                    seen_names.add(name)
                query = subset.get("query")
                if not query:
                    _err(f"{path}.query", "subset needs a 'query' returning entity_id")
                elif "{as_of_date}" not in query:
                    _err(
                        f"{path}.query",
                        "the subset query must contain the {as_of_date} placeholder",
                    )

    if not experiment_config.get("sources"):
        warnings.append(
            "no sources declared — every derivation is volatile (never a cache hit)"
            " and inputs are unpinned (ADR-0014)"
        )

    experiment_hash = (
        experiment_hash_for(experiment_config)
        if all(k in experiment_config for k in _PROBLEM_KEYS)
        else None
    )
    return {
        "valid": not errors,
        "experiment_hash": experiment_hash,
        "problem_type": problem_type if problem_type in _PROBLEM_TYPES else None,
        "n_splits": n_splits,
        "n_models": n_models,
        "n_feature_groups": n_feature_groups,
        "errors": errors,
        "warnings": warnings,
    }


def _as_dates(values: Sequence[Any]) -> list[date]:
    """Normalize timechop's ``as_of_times`` (``datetime``) to plain ``date`` objects.

    The builders take ``Sequence[date]`` and substitute bare quoted ``YYYY-MM-DD`` literals;
    timechop emits ``datetime.datetime`` at midnight, so we drop the time component.
    """
    out: list[date] = []
    for value in values:
        if isinstance(value, datetime):
            out.append(value.date())
        elif isinstance(value, date):
            out.append(value)
        else:
            raise TypeError(
                f"as_of_time {value!r} ({type(value).__name__}) is neither date nor datetime"
            )
    return out


def _generate_splits(temporal_config: TemporalConfig) -> list[dict[str, Any]]:
    """Run the inherited Timechop engine and return its matrix-set definitions.

    timechop stays the as_of_date/split generator (ADR-0010); :class:`TemporalConfig` only
    types/validates/canonicalizes the kwargs it feeds. Each returned dict has a
    ``train_matrix`` (with ``as_of_times`` + ``max_training_history`` +
    ``training_label_timespan``) and a list of ``test_matrices`` (each with ``as_of_times`` +
    ``test_label_timespan``).
    """
    timechop = Timechop(**temporal_config.to_timechop_kwargs())
    splits = timechop.chop_time()
    if not splits:
        raise ValueError(
            "Timechop produced no train/test splits for this temporal_config —"
            + " widen the feature/label windows or shorten the label_timespan"
        )
    return splits


def _union_as_of_dates(splits: Sequence[Mapping[str, Any]]) -> list[date]:
    """The sorted UNION of every split's train + test as_of_dates.

    One cohort + one labels are built over this union (each matrix later inner-joins its own
    split's dates), so per-split dates never enter the cohort/labels derivation config.
    """
    seen: set[date] = set()
    for split in splits:
        seen.update(_as_dates(split["train_matrix"]["as_of_times"]))
        for test_matrix in split["test_matrices"]:
            seen.update(_as_dates(test_matrix["as_of_times"]))
    return sorted(seen)


def _pin_sources(
    db_engine: ConnectionPool,
    run_id: str,
    declared_sources: Sequence[Mapping[str, Any]],
    source_pins: Mapping[str, str | None] | None,
) -> dict[str, str | None]:
    """Register + pin the declared sources, then freeze + record the run's pins (ADR-0014).

    If ``source_pins`` is supplied it is taken as the already-frozen set (the caller pinned
    out of band); otherwise each declared source is registered and bumped so it carries a
    pinned version (making downstream derivations cacheable), then ``resolve_pins`` freezes
    the current pin per source. Either way the frozen set is persisted on the run via
    ``record_run_pins`` (the ``guix describe`` analog) and returned to thread into the builds.
    """
    names = [spec["name"] for spec in declared_sources]
    if source_pins is not None:
        frozen = dict(source_pins)
        record_run_pins(db_engine, run_id, frozen)
        return frozen

    for spec in declared_sources:
        register_source(
            db_engine,
            source_name=spec["name"],
            relation=spec["relation"],
            knowledge_date_column=spec.get("knowledge_date_column"),
            description=spec.get("description"),
            role=spec.get("role"),
            type_column=spec.get("type_column"),
        )
        # A pinned version is what makes the source cacheable (ADR-0014); without it every
        # derivation touching it is volatile and never a cache hit. An explicit per-source
        # version_label keeps the pin stable across runs of the same loaded data — but
        # ``source_versions`` has a (source_name, version_label) PK, so re-bumping an already
        # pinned label would collide. Only bump when this label is not already recorded; this
        # makes pinning idempotent across re-runs of the same loaded data.
        version_label = spec.get("version_label")
        if not _version_exists(db_engine, spec["name"], version_label):
            bump_source(db_engine, spec["name"], version_label=version_label)

    frozen = resolve_pins(db_engine, names)
    record_run_pins(db_engine, run_id, frozen)
    return frozen


def _version_exists(
    db_engine: ConnectionPool, source_name: str, version_label: str | None
) -> bool:
    """Whether ``(source_name, version_label)`` is already pinned (for idempotent bumps).

    A ``None`` ``version_label`` means "generate a fresh timestamped pin", which can never
    pre-exist — so we always bump in that case.
    """
    if version_label is None:
        return False
    with db_engine.connection() as conn:
        return (
            conn.execute(
                "select 1 from triage.source_versions"
                + " where source_name = %(name)s and version_label = %(label)s",
                {"name": source_name, "label": version_label},
            ).fetchone()
            is not None
        )


def _create_experiment_and_run(
    db_engine: ConnectionPool,
    experiment_config: Mapping[str, Any],
    problem_type: str,
    profile: str,
    random_seed: int,
) -> tuple[str, str]:
    """INSERT-or-get the experiment row, then create a fresh 'started' run row.

    Returns ``(experiment_hash, run_id)``. The experiment is keyed by
    :func:`experiment_hash_for` so a re-run reuses it; the run is always new (its ``run_id``
    comes from the DB ``gen_random_uuid()`` default).

    The cosmetic ``name``/``description`` (from the config) and ``author`` (the OS user) are
    stored on the experiment row but kept OUT of identity: the stored ``config`` is the
    cleaned config (without name/description) and the hash is over that same cleaned config.
    The upsert keeps the first writer's name/description/author for a re-run; the
    identity-neutral ``task_framing`` (migration 0019) instead updates when the config
    provides one and is never cleared by a config that omits it (coalesce).
    """
    exp_hash = experiment_hash_for(experiment_config)
    name = experiment_config.get("name")
    description = experiment_config.get("description")
    # Author = the OS user creating the experiment (getpass.getuser reads the env/passwd; the
    # USER env var is the documented fallback when neither is available).
    try:
        author = getpass.getuser()
    except Exception:  # noqa: BLE001 - getuser() can raise on an unconfigured passwd/env
        author = os.environ.get("USER")
    with db_engine.connection() as conn:
        conn.execute(
            "insert into triage.experiments"
            + " (experiment_hash, config, problem_type, name, description, author,"
            + " task_framing)"
            + " values (%(h)s, cast(%(config)s as jsonb),"
            + " cast(%(pt)s as triage.problem_type), %(name)s, %(description)s, %(author)s,"
            + " %(framing)s)"
            # name/description/author keep first-writer semantics; task_framing is
            # identity-neutral metadata (migration 0019) — a re-run that provides it
            # updates the tag, a re-run that omits it never clears an existing one.
            + " on conflict (experiment_hash) do update set task_framing ="
            + " coalesce(excluded.task_framing, triage.experiments.task_framing)",
            {
                "h": exp_hash,
                # The experiment stores its PROBLEM (cohort+label+temporal+problem_type, ADR-0022);
                # the per-run feature/grid/imputation config lives on runs.plan.attempt.
                "config": canonical_json(_problem_identity(experiment_config)),
                "pt": problem_type,
                "name": name,
                "description": description,
                "author": author,
                "framing": experiment_config.get("task_framing"),
            },
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile, status, random_seed,"
            + " batch_job_id)"
            + " values (%(h)s, %(profile)s, 'started', %(seed)s, %(job)s)"
            + " returning run_id",
            {
                "h": exp_hash,
                "profile": profile,
                "seed": random_seed,
                # Inside an AWS Batch container, Batch injects AWS_BATCH_JOB_ID — recording it
                # correlates this run with its job so `triage runs status` can backfill a
                # terminal Batch state (cloud-profile-spec §7). NULL locally.
                "job": os.environ.get("AWS_BATCH_JOB_ID"),
            },
        ).fetchone()["run_id"]
        # Live telemetry (read-dashboard-spec §4): the run has started. Emitted on
        # the same COMMIT as the runs INSERT.
        _notify_run_progress(conn, str(run_id), "run", "started")
    logger.info(
        f"Experiment {exp_hash[:12]}… ({problem_type}); started run {str(run_id)[:8]}…"
    )
    return exp_hash, str(run_id)


def _create_run(
    db_engine: ConnectionPool, exp_hash: str, profile: str, random_seed: int
) -> str:
    """Create an additional 'started' run under an EXISTING experiment (ADR-0023 fan-out).

    Used when feature-group strategies expand one experiment into several runs (one per feature
    subset). The experiment row already exists (created by :func:`_create_experiment_and_run`);
    this just mints another run under it.
    """
    with db_engine.connection() as conn:
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile, status, random_seed,"
            + " batch_job_id)"
            + " values (%(h)s, %(profile)s, 'started', %(seed)s, %(job)s)"
            + " returning run_id",
            {
                "h": exp_hash,
                "profile": profile,
                "seed": random_seed,
                "job": os.environ.get("AWS_BATCH_JOB_ID"),
            },
        ).fetchone()["run_id"]
        _notify_run_progress(conn, str(run_id), "run", "started")
    return str(run_id)


def _feature_subsets(
    feature_config: Mapping[str, Any], feature_names: Sequence[str]
) -> list[FeatureSubset]:
    """Resolve the run fan-out: the feature-column subsets one experiment expands into.

    Reads ``feature_config['feature_groups']`` (ADR-0023). Absent ⇒ a single implicit group =
    all features = one run (today's behaviour). Present ⇒ partition the columns into groups
    (by ``source_entity`` parsed from the feature names, or explicit globs) and sweep the
    declared strategies into subsets. ``feature_groups`` is a triage-pg adapter concern and is
    stripped from the config the featurizer sees (see :func:`_featurizer_only`), so featurizer
    stays group-agnostic (ADR-0008).
    """
    fg = (
        feature_config.get("feature_groups")
        if isinstance(feature_config, Mapping)
        else None
    )
    if not fg:
        return [
            FeatureSubset(
                label="all-features",
                group_names=("all",),
                columns=tuple(sorted(feature_names)),
            )
        ]
    entity_aliases = [
        e["alias"] for e in feature_config.get("entities", []) if "alias" in e
    ]
    groups = partition_features(
        feature_names,
        entity_aliases,
        group_by=fg.get("group_by", "source_entity"),
        definitions=fg.get("definitions"),
        target_alias=feature_config.get("target"),
    )
    return mix_strategies(
        groups,
        fg.get("strategies") or ["all"],
        all_combinations_max_groups=fg.get(
            "all_combinations_max_groups", DEFAULT_ALL_COMBINATIONS_MAX_GROUPS
        ),
    )


def _featurizer_only(feature_config: Mapping[str, Any]) -> dict[str, Any]:
    """The featurizer ER-graph config with the triage-pg-only ``feature_groups`` key removed.

    featurizer must never see ``feature_groups`` (ADR-0008/0023): it would reject the unknown
    key, and it would wrongly enter the feature_group derivation identity.
    """
    return {k: v for k, v in feature_config.items() if k != "feature_groups"}


def _refresh_leaderboard(db_engine: ConnectionPool) -> None:
    """Refresh the ``triage.leaderboard`` materialized view so reads see this run (ADR-0007).

    The matview is never auto-populated otherwise (querying it errors "has not been populated"),
    so we refresh it once a run lands its evaluations. Best-effort: a refresh failure must not
    undo an already-completed run, but it is logged with context — never silently swallowed
    (CLAUDE.md error policy). Plain (non-CONCURRENT) refresh: the matview has no unique index.
    """
    try:
        with db_engine.connection() as conn:
            conn.execute("refresh materialized view triage.leaderboard")
        logger.info("Refreshed triage.leaderboard")
    except Exception as exc:  # noqa: BLE001 - leaderboard is a read convenience, never fatal
        logger.warning(f"Could not refresh triage.leaderboard (non-fatal): {exc}")


def _mark_run(
    db_engine: ConnectionPool, run_id: str, status: str, error: str | None = None
) -> None:
    """Set a run's terminal status (``completed`` | ``failed``) + finish time."""
    with db_engine.connection() as conn:
        conn.execute(
            "update triage.runs set status = cast(%(status)s as triage.run_status),"
            + " finished_at = now(), error = coalesce(%(error)s, error)"
            + " where run_id = %(run_id)s",
            {"status": status, "error": error, "run_id": run_id},
        )
        # Live telemetry (read-dashboard-spec §4): the run reached its terminal
        # status ('completed' | 'failed'). Emitted on the same COMMIT as the UPDATE.
        _notify_run_progress(conn, run_id, "run", status)


def _record_run_plan(
    db_engine: ConnectionPool, run_id: str, plan: Mapping[str, Any]
) -> None:
    """Persist the run's planned shape on ``triage.runs.plan`` (ADR-0021 telemetry).

    The read-dashboard's pipeline-DAG denominators (``matrices N/M``, ``models N/M``) and the
    experiment-summary panel read this (``triage.run_summary`` exposes it). Written once after
    timechop + grid so the dashboard has the denominators *while the run is still building*,
    then updated once with ``n_features`` after the first matrix lands. A trivial UPDATE of a
    valid jsonb on an existing run — not wrapped: a failure here is a real error, not swallowed.
    """
    with db_engine.connection() as conn:
        conn.execute(
            "update triage.runs set plan = %(plan)s::jsonb where run_id = %(run_id)s",
            {"plan": json.dumps(plan), "run_id": run_id},
        )


def _build_split(
    db_engine: ConnectionPool,
    run_id: str,
    split: Mapping[str, Any],
    *,
    featurizer_config: Mapping[str, Any],
    cohort_artifact_id: str,
    labels_artifact_id: str,
    temporal_config: TemporalConfig,
    imputation_policy: ImputationPolicy,
    storage: StorageAdapter,
    storage_root: str,
    source_pins: Mapping[str, str | None],
) -> tuple[MatrixResult, MatrixResult, list[date], list[date], str, str]:
    """Build a split's train + test matrices; return both + their dates + label timespans.

    The train matrix uses the split's train ``as_of_times`` and ``max_training_history`` as
    the lookback; the test matrix uses the (single, by convention) test matrix definition's
    ``as_of_times`` and is given the train matrix as its parent so the train-fitted imputation
    statistics flow to it without refitting (ADR-0009).
    """
    train_def = split["train_matrix"]
    train_dates = _as_dates(train_def["as_of_times"])
    train_timespan = train_def["training_label_timespan"]
    lookback = train_def.get("max_training_history")

    test_defs = split["test_matrices"]
    if len(test_defs) != 1:
        # The greenfield orchestrator assembles one test matrix per split (the common case:
        # a single test_as_of_date_frequency + test_duration). Multiple test matrices per
        # split would need a per-test loop; surface it rather than silently using the first.
        raise ValueError(
            f"split produced {len(test_defs)} test matrices; the greenfield orchestrator"
            + " supports exactly one test matrix per split (one test_as_of_date_frequency)"
        )
    test_def = test_defs[0]
    test_dates = _as_dates(test_def["as_of_times"])
    test_timespan = test_def["test_label_timespan"]

    train_matrix = build_matrix(
        db_engine,
        run_id,
        featurizer_config=featurizer_config,
        cohort_artifact_id=cohort_artifact_id,
        labels_artifact_id=labels_artifact_id,
        temporal_config=temporal_config,
        imputation_policy=imputation_policy,
        matrix_kind="train",
        as_of_dates=train_dates,
        label_timespan=train_timespan,
        storage=storage,
        storage_root=storage_root,
        lookback=lookback,
        source_pins=source_pins,
    )
    test_matrix = build_matrix(
        db_engine,
        run_id,
        featurizer_config=featurizer_config,
        cohort_artifact_id=cohort_artifact_id,
        labels_artifact_id=labels_artifact_id,
        temporal_config=temporal_config,
        imputation_policy=imputation_policy,
        matrix_kind="test",
        as_of_dates=test_dates,
        label_timespan=test_timespan,
        storage=storage,
        storage_root=storage_root,
        train_matrix_artifact_id=train_matrix.matrix_artifact_id,
        source_pins=source_pins,
    )
    return (
        train_matrix,
        test_matrix,
        train_dates,
        test_dates,
        train_timespan,
        test_timespan,
    )


def _grid_specs(grid_config: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Expand the grid config into ``(class_path, hyperparameters)`` pairs.

    The grid is ``{class_path: {hyperparam: [values...]}}`` (the inherited triage shape); each
    estimator's hyperparameter lists are Cartesian-producted into concrete hyperparameter
    dicts. A class with no hyperparameters yields a single empty-dict spec.
    """
    import itertools

    specs: list[tuple[str, dict[str, Any]]] = []
    for class_path, param_grid in grid_config.items():
        if not param_grid:
            specs.append((class_path, {}))
            continue
        keys = sorted(param_grid)
        value_lists = [
            (
                param_grid[key]
                if isinstance(param_grid[key], (list, tuple))
                else [param_grid[key]]
            )
            for key in keys
        ]
        for combo in itertools.product(*value_lists):
            specs.append((class_path, dict(zip(keys, combo, strict=True))))
    if not specs:
        raise ValueError(
            "grid is empty — at least one estimator class_path is required"
        )
    return specs


def run_experiment(
    db_engine: ConnectionPool,
    experiment_config: Mapping[str, Any],
    *,
    storage: StorageAdapter,
    storage_root: str,
    source_pins: Mapping[str, str | None] | None = None,
    profile: str = "local",
    random_seed: int = 0,
    metric_config: Mapping[str, Any] | None = None,
    cache_policy: str = "exact",
) -> ExperimentResult:
    """Execute a whole experiment end-to-end and return its run lineage (ADR-0012).

    Sequences the F1/F2/F3 builders into one pass: experiment + run rows → source pinning →
    timechop splits → one cohort + one labels over the union of split dates → per-split train
    + test matrices → grid × split models → score + evaluate. The run is marked ``completed``
    on success, ``failed`` (with the error recorded) on any exception, which is re-raised.

    Args:
        db_engine: project-database engine (greenfield ``triage.*`` schema).
        experiment_config: the full experiment config. Required keys:

            * ``problem_type`` — ADR-0010 discriminator
              (``classification`` | ``regression_ranking`` | ``regression`` | ``survival``).
            * ``temporal_config`` — :class:`TemporalConfig` kwargs.
            * ``cohort_config`` — ``{query, ...}`` with the ``{as_of_date}`` placeholder.
            * ``label_config`` — ``{query, ...}`` with ``{as_of_date}`` + ``{label_timespan}``.
            * ``feature_config`` — the featurizer ER-graph config (dict).
            * ``grid_config`` — ``{class_path: {hyperparam: [values]}}``.
            * ``sources`` — the declared input tables:
              ``[{name, relation, knowledge_date_column?, version_label?, description?}]``.
            * ``imputation_config`` (optional) — :class:`ImputationPolicy` mapping; defaults
              to ``{"all": {"type": "zero"}}`` (fit-free).
        storage: the :class:`~triage.profiles.protocols.StorageAdapter` the Parquet matrices +
            joblib models are written/read through (local FS or S3, by ``storage_root`` scheme).
        storage_root: the artifact root URI (``./matrices`` locally, ``s3://…/<scope>`` cloud);
            matrices/models land at ``<storage_root>/<uuid>.parquet|.joblib``.
        source_pins: pre-frozen pins to use verbatim (skips register/bump). When ``None``
            (the default) the declared ``sources`` are registered + bumped + resolved here.
        profile: ``'local'`` | ``'cloud'`` (``triage.runs.profile``).
        random_seed: deterministic seed stored on the run and passed to every model.
        metric_config: ``triage.evaluate_model`` config; defaults to the classification set.
        cache_policy: cache lookup policy threaded to the builders ('exact' default).

    Returns:
        An :class:`ExperimentResult` with the experiment hash, cohort/labels ids, the frozen
        source pins, and ``runs`` — one :class:`RunResult` per feature-group subset (ADR-0023),
        or a single run when no feature groups are configured. Each RunResult carries that run's
        per-split build outcomes + model/prediction/evaluation counts.

    Raises:
        ValueError: on a malformed config (missing keys, empty grid, no splits, multi-test).
        Exception: any builder failure — the run is marked ``failed`` first, then re-raised.
    """
    problem_type = _require(experiment_config, "problem_type")
    cohort_config = _require(experiment_config, "cohort_config")
    label_config = _require(experiment_config, "label_config")
    feature_config = _require(experiment_config, "feature_config")
    grid_config = _require(experiment_config, "grid_config")
    declared_sources = experiment_config.get("sources", [])
    _require_survival_extra(problem_type)
    metric_config = _resolve_metric_config(
        experiment_config, problem_type, metric_config
    )
    # bias_config is identity-neutral (observes the problem, does not define it — it is
    # NOT in _PROBLEM_KEYS); validate it before anything is built (fail fast).
    bias_config = experiment_config.get("bias_config")
    if bias_config is not None:
        validate_bias_config(bias_config)
    # evaluation.subsets: identity-neutral cohort slices, evaluated alongside the full
    # cohort (migration 0015 re-ranks within each subset). Validate before building.
    subsets_config = (experiment_config.get("evaluation") or {}).get("subsets") or []
    if subsets_config:
        validate_subsets_config(subsets_config)
    # task_framing: identity-neutral observation-regime tag (migration 0019); fail fast
    # on an unknown value before anything is built.
    task_framing = experiment_config.get("task_framing")
    if task_framing is not None and task_framing not in _TASK_FRAMINGS:
        raise ValueError(
            f"unknown task_framing {task_framing!r} — expected one of {list(_TASK_FRAMINGS)}"
        )

    temporal_config = TemporalConfig.model_validate(
        _require(experiment_config, "temporal_config")
    )
    imputation_policy = ImputationPolicy.model_validate(
        experiment_config.get("imputation_config", {"all": {"type": "zero"}})
    )
    label_timespans = list(temporal_config.training_label_timespans) + list(
        temporal_config.test_label_timespans
    )
    # de-dupe while keeping order; labels are built for every timespan a split will join.
    label_timespans = list(dict.fromkeys(label_timespans))

    featurizer_config = _featurizer_only(feature_config)

    # The experiment (the PROBLEM) + the FIRST run. Feature-group strategies (ADR-0023) expand
    # into more runs once the feature columns are known (below); without them this is the only run.
    exp_hash, first_run_id = _create_experiment_and_run(
        db_engine, experiment_config, problem_type, profile, random_seed
    )
    current_run_id = first_run_id

    try:
        frozen_pins = _pin_sources(
            db_engine, first_run_id, declared_sources, source_pins
        )

        splits = _generate_splits(temporal_config)
        all_as_of_dates = _union_as_of_dates(splits)
        logger.info(
            f"Experiment {exp_hash[:8]}…: {len(splits)} split(s) over"
            + f" {len(all_as_of_dates)} distinct as_of_date(s)"
        )

        cohort_artifact_id = build_cohort(
            db_engine,
            first_run_id,
            cohort_query_template=cohort_config["query"],
            as_of_dates=all_as_of_dates,
            config=cohort_config,
            source_pins=frozen_pins,
            policy=cache_policy,
        )
        if bias_config is not None:
            # Populate protected_groups from the config's templated query so the SQL
            # bias audit runs end-to-end (ADR-0007) — idempotent upsert per date.
            ingest_protected_groups(db_engine, bias_config["query"], all_as_of_dates)
        labels_artifact_id = build_labels(
            db_engine,
            first_run_id,
            cohort_artifact_id=cohort_artifact_id,
            label_query_template=label_config["query"],
            as_of_dates=all_as_of_dates,
            label_timespans=label_timespans,
            problem_type=problem_type,
            config=label_config,
            source_pins=frozen_pins,
            policy=cache_policy,
        )
        # evaluation.subsets → subset_members over the union grid; every model then
        # evaluates once per (date × subset) alongside the full cohort (0015).
        subset_hashes: list[str] = [
            s["subset_hash"]
            for s in (
                register_subsets(db_engine, subsets_config, all_as_of_dates)
                if subsets_config
                else []
            )
        ]

        grid = _grid_specs(grid_config)

        # Build the FULL (all-features) matrices once per split, under the first run. featurizer
        # runs here; every feature-group subset is then a column PROJECTION of these same Parquet
        # files (ADR-0023) — no featurizer re-run, no projected copies. feature_names come from
        # the full train matrix, so groups are partitioned over real output columns.
        full_splits: list[tuple[Any, ...]] = [
            _build_split(
                db_engine,
                first_run_id,
                split,
                featurizer_config=featurizer_config,
                cohort_artifact_id=cohort_artifact_id,
                labels_artifact_id=labels_artifact_id,
                temporal_config=temporal_config,
                imputation_policy=imputation_policy,
                storage=storage,
                storage_root=storage_root,
                source_pins=frozen_pins,
            )
            for split in splits
        ]
        feature_names = list(full_splits[0][0].feature_names)
        shared_matrix_ids = [
            mid
            for ft, fs, *_ in full_splits
            for mid in (ft.matrix_artifact_id, fs.matrix_artifact_id)
        ]

        subsets = _feature_subsets(feature_config, feature_names)
        # First subset reuses the run that built the shared artifacts; the rest get new runs.
        run_ids = [first_run_id] + [
            _create_run(db_engine, exp_hash, profile, random_seed) for _ in subsets[1:]
        ]
        logger.info(
            f"Experiment {exp_hash[:8]}…: {len(subsets)} feature-group run(s)"
            + f" [{', '.join(s.label for s in subsets)}]"
        )

        run_results: list[RunResult] = []
        for subset, run_id in zip(subsets, run_ids, strict=True):
            current_run_id = run_id
            if run_id != first_run_id:
                # New runs reuse the shared cohort/labels/full-matrices via usage edges
                # (the builders already recorded them for the first run).
                record_use(
                    db_engine,
                    run_id,
                    [cohort_artifact_id, labels_artifact_id, *shared_matrix_ids],
                )

            plan: dict[str, Any] = {
                "n_splits": len(splits),
                "n_matrices": 2 * len(splits),
                "n_model_groups": len(grid),
                "n_models": len(grid) * len(splits),
                "estimator_types": sorted({class_path for class_path, _ in grid}),
                "temporal": temporal_config.canonical(),
                "engine_versions": engine_versions_for("feature_group"),
                "n_feature_groups": len(subsets),
                "n_features": len(subset.columns),
                # The run's ATTEMPT at the problem (ADR-0022): feature/grid/imputation. With
                # feature groups (ADR-0023) the attempt also records WHICH subset this run used.
                "attempt": {
                    "feature_config": featurizer_config,
                    "grid_config": experiment_config.get("grid_config"),
                    "imputation_config": experiment_config.get("imputation_config"),
                    "feature_group": subset.label,
                    "feature_group_members": list(subset.group_names),
                },
                "compute": {"cpu_count": os.cpu_count(), "profile": profile},
            }
            _record_run_plan(db_engine, run_id, plan)

            split_results: list[SplitResult] = []
            run_model_ids: list[int] = []
            run_predictions = 0
            run_evaluations = 0

            for (
                full_train,
                full_test,
                train_dates,
                test_dates,
                train_timespan,
                test_timespan,
            ) in full_splits:
                # Project the shared full matrices to this subset's columns: same Parquet, fewer
                # feature_names. fit-based imputation is per-column, so the subset's values are
                # unchanged; the subset enters the model's feature_list (and model_group) identity.
                train_matrix = replace(full_train, feature_names=list(subset.columns))
                test_matrix = replace(full_test, feature_names=list(subset.columns))

                split_model_ids: list[int] = []
                split_model_artifact_ids: list[str] = []
                split_predictions = 0
                split_evaluations = 0
                train_end_time = max(train_dates) if train_dates else None

                for class_path, hyperparameters in grid:
                    model = build_model(
                        db_engine,
                        run_id,
                        train_matrix_result=train_matrix,
                        class_path=class_path,
                        hyperparameters=hyperparameters,
                        random_seed=random_seed,
                        storage=storage,
                        storage_root=storage_root,
                        train_end_time=train_end_time,
                        training_label_timespan=train_timespan,
                        source_pins=frozen_pins,
                        policy=cache_policy,
                    )
                    # as_of_date=None → evaluate at EVERY test as_of_date (WS1).
                    score = score_and_evaluate(
                        db_engine,
                        model.model_id,
                        model.estimator,
                        test_matrix_result=test_matrix,
                        as_of_date=None,
                        label_timespan=test_timespan,
                        metric_config=metric_config,
                        subset_hashes=subset_hashes,
                        compute_bias=bias_config is not None,
                        bias_parameter=(bias_config or {}).get("parameter"),
                        bias_ref_groups=(bias_config or {}).get("ref_groups"),
                        bias_tau=(bias_config or {}).get("tau", 0.8),
                    )
                    split_model_ids.append(model.model_id)
                    split_model_artifact_ids.append(model.model_artifact_id)
                    run_model_ids.append(model.model_id)
                    split_predictions += score.num_predictions
                    split_evaluations += score.num_evaluations

                run_predictions += split_predictions
                run_evaluations += split_evaluations
                split_results.append(
                    SplitResult(
                        train_as_of_dates=train_dates,
                        test_as_of_dates=test_dates,
                        train_matrix=train_matrix,
                        test_matrix=test_matrix,
                        model_ids=split_model_ids,
                        model_artifact_ids=split_model_artifact_ids,
                        num_predictions=split_predictions,
                        num_evaluations=split_evaluations,
                    )
                )

            _mark_run(db_engine, run_id, "completed")
            logger.info(
                f"Run {run_id[:8]}… ({subset.label}) completed:"
                + f" {len(run_model_ids)} model(s), {run_predictions} prediction(s),"
                + f" {run_evaluations} evaluation(s)"
            )
            run_results.append(
                RunResult(
                    run_id=run_id,
                    experiment_hash=exp_hash,
                    problem_type=problem_type,
                    cohort_artifact_id=cohort_artifact_id,
                    labels_artifact_id=labels_artifact_id,
                    source_pins=dict(frozen_pins),
                    splits=split_results,
                    model_ids=run_model_ids,
                    num_models=len(run_model_ids),
                    num_predictions=run_predictions,
                    num_evaluations=run_evaluations,
                    feature_group=subset.label,
                )
            )

    except Exception as exc:
        _mark_run(db_engine, current_run_id, "failed", error=str(exc))
        logger.error(f"Run {current_run_id[:8]}… failed: {exc}")
        raise

    _refresh_leaderboard(db_engine)
    return ExperimentResult(
        experiment_hash=exp_hash,
        problem_type=problem_type,
        cohort_artifact_id=cohort_artifact_id,
        labels_artifact_id=labels_artifact_id,
        source_pins=dict(frozen_pins),
        runs=run_results,
    )


def _require(config: Mapping[str, Any], key: str) -> Any:
    """Fetch a required config key, failing loudly with context if it is absent."""
    if key not in config:
        raise ValueError(
            f"experiment_config is missing required key {key!r}"
            + f" (have: {sorted(config)})"
        )
    return config[key]


def _require_survival_extra(problem_type: str) -> None:
    """Fail fast (before any build) when survival is requested without the extra installed."""
    if problem_type != "survival":
        return
    import importlib.util

    if importlib.util.find_spec("sksurv") is None:
        raise ValueError(
            "problem_type 'survival' requires the survival extra (scikit-survival) —"
            " install it with `uv sync --extra survival` (ADR-0026)"
        )


def _resolve_metric_config(
    experiment_config: Mapping[str, Any],
    problem_type: str,
    override: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The evaluation metric set for this experiment, resolved by precedence.

    Explicit ``metric_config`` argument (CLI/tests) > the config's ``evaluation`` block
    (the ``triage.evaluate_model`` jsonb shape: ``metrics``/``thresholds``/
    ``regression_metrics``/``survival_metrics``) > the problem-type default. Defaults:
    classification keeps the inherited set; the regression family gets rmse/mae/r2
    (``regression_ranking`` users who want ranking metrics too must declare them —
    precision@k assumes a binary outcome, which a continuous target does not have by
    default); survival gets the C-index (ADR-0010/0026).
    """
    from triage.component.catwalk.in_pg_evaluation import (
        DEFAULT_CLASSIFICATION_CONFIG,
        DEFAULT_REGRESSION_CONFIG,
        DEFAULT_SURVIVAL_CONFIG,
    )

    if override is not None:
        return dict(override)
    block = experiment_config.get("evaluation")
    if block:
        resolved = dict(block)
        # subsets ride the evaluation block for config ergonomics but are NOT part of
        # the jsonb metric config evaluate_model consumes (they are materialized +
        # looped by the orchestrator — see register_subsets / score_and_evaluate).
        resolved.pop("subsets", None)
        if resolved:
            return resolved
        # a subsets-only evaluation block still gets the problem-type metric default
    if problem_type in ("regression", "regression_ranking"):
        return dict(DEFAULT_REGRESSION_CONFIG)
    if problem_type == "survival":
        return dict(DEFAULT_SURVIVAL_CONFIG)
    return dict(DEFAULT_CLASSIFICATION_CONFIG)
