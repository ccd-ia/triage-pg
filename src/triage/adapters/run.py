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

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from psycopg_pool import ConnectionPool

from triage.adapters.cohort import build_cohort
from triage.adapters.imputation import ImputationPolicy
from triage.adapters.labels import build_labels
from triage.adapters.matrix import MatrixResult, build_matrix
from triage.adapters.model import build_model, score_and_evaluate
from triage.adapters.temporal import TemporalConfig
from triage.artifacts import _notify_run_progress
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

__all__ = ["run_experiment", "RunResult", "SplitResult", "experiment_hash_for"]


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


def experiment_hash_for(experiment_config: Mapping[str, Any]) -> str:
    """Stable SHA-256 identity for an experiment from its canonical config.

    Reuses :func:`triage.derivation.canonical_json` (sorted keys, normalized types) so the
    same config — regardless of key order or surface form — maps to one ``experiment_hash``.
    A re-run of the same config therefore lands on the same experiment row.
    """
    return hashlib.sha256(canonical_json(experiment_config).encode("ascii")).hexdigest()


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
    """
    exp_hash = experiment_hash_for(experiment_config)
    with db_engine.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            + " values (%(h)s, cast(%(config)s as jsonb), cast(%(pt)s as triage.problem_type))"
            + " on conflict (experiment_hash) do nothing",
            {
                "h": exp_hash,
                "config": canonical_json(experiment_config),
                "pt": problem_type,
            },
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile, status, random_seed)"
            + " values (%(h)s, %(profile)s, 'started', %(seed)s) returning run_id",
            {"h": exp_hash, "profile": profile, "seed": random_seed},
        ).fetchone()["run_id"]
        # Live telemetry (read-dashboard-spec §4): the run has started. Emitted on
        # the same COMMIT as the runs INSERT.
        _notify_run_progress(conn, str(run_id), "run", "started")
    logger.info(
        f"Experiment {exp_hash[:12]}… ({problem_type}); started run {str(run_id)[:8]}…"
    )
    return exp_hash, str(run_id)


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
    except (
        Exception
    ) as exc:  # noqa: BLE001 - leaderboard is a read convenience, never fatal
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
) -> RunResult:
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
        A :class:`RunResult` with the run id, experiment hash, cohort/labels ids, the frozen
        source pins, per-split build outcomes, and the model/prediction/evaluation counts.

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

    exp_hash, run_id = _create_experiment_and_run(
        db_engine, experiment_config, problem_type, profile, random_seed
    )

    try:
        frozen_pins = _pin_sources(db_engine, run_id, declared_sources, source_pins)

        splits = _generate_splits(temporal_config)
        all_as_of_dates = _union_as_of_dates(splits)
        logger.info(
            f"Run {run_id[:8]}…: {len(splits)} split(s) over"
            + f" {len(all_as_of_dates)} distinct as_of_date(s)"
        )

        cohort_artifact_id = build_cohort(
            db_engine,
            run_id,
            cohort_query_template=cohort_config["query"],
            as_of_dates=all_as_of_dates,
            config=cohort_config,
            source_pins=frozen_pins,
            policy=cache_policy,
        )
        labels_artifact_id = build_labels(
            db_engine,
            run_id,
            cohort_artifact_id=cohort_artifact_id,
            label_query_template=label_config["query"],
            as_of_dates=all_as_of_dates,
            label_timespans=label_timespans,
            problem_type=problem_type,
            config=label_config,
            source_pins=frozen_pins,
            policy=cache_policy,
        )

        grid = _grid_specs(grid_config)

        # Record the planned shape now (after timechop + grid) so the read-dashboard's
        # pipeline-DAG denominators (matrices/models N/M) + experiment-summary are available
        # WHILE the run builds (ADR-0021, read-dashboard-spec §3.1). One train + one test
        # matrix per split; one model per (grid spec, split); grid specs are the model_groups.
        # n_features is unknown until the first matrix lands — filled in the split loop below.
        plan: dict[str, Any] = {
            "n_splits": len(splits),
            "n_matrices": 2 * len(splits),
            "n_model_groups": len(grid),
            "n_models": len(grid) * len(splits),
            "estimator_types": sorted({class_path for class_path, _ in grid}),
            "temporal": temporal_config.canonical(),
            "engine_versions": engine_versions_for("feature_group"),
            "n_feature_groups": 1,
            "n_features": None,
        }
        _record_run_plan(db_engine, run_id, plan)

        split_results: list[SplitResult] = []
        all_model_ids: list[int] = []
        total_predictions = 0
        total_evaluations = 0

        for split in splits:
            (
                train_matrix,
                test_matrix,
                train_dates,
                test_dates,
                train_timespan,
                test_timespan,
            ) = _build_split(
                db_engine,
                run_id,
                split,
                featurizer_config=feature_config,
                cohort_artifact_id=cohort_artifact_id,
                labels_artifact_id=labels_artifact_id,
                temporal_config=temporal_config,
                imputation_policy=imputation_policy,
                storage=storage,
                storage_root=storage_root,
                source_pins=frozen_pins,
            )

            # Fill the feature count once the first matrix is built (it's unknown at plan time).
            if plan["n_features"] is None and train_matrix.num_features is not None:
                plan["n_features"] = train_matrix.num_features
                _record_run_plan(db_engine, run_id, plan)

            split_model_ids: list[int] = []
            split_model_artifact_ids: list[str] = []
            split_predictions = 0
            split_evaluations = 0
            train_end_time = max(train_dates) if train_dates else None
            test_as_of = max(test_dates) if test_dates else None

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
                score = score_and_evaluate(
                    db_engine,
                    model.model_id,
                    model.estimator,
                    test_matrix_result=test_matrix,
                    as_of_date=test_as_of,
                    label_timespan=test_timespan,
                    metric_config=metric_config,
                )
                split_model_ids.append(model.model_id)
                split_model_artifact_ids.append(model.model_artifact_id)
                all_model_ids.append(model.model_id)
                split_predictions += score.num_predictions
                split_evaluations += score.num_evaluations

            total_predictions += split_predictions
            total_evaluations += split_evaluations
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
    except Exception as exc:
        _mark_run(db_engine, run_id, "failed", error=str(exc))
        logger.error(f"Run {run_id[:8]}… failed: {exc}")
        raise

    _refresh_leaderboard(db_engine)
    logger.info(
        f"Run {run_id[:8]}… completed: {len(all_model_ids)} model(s),"
        + f" {total_predictions} prediction(s), {total_evaluations} evaluation(s)"
    )
    return RunResult(
        run_id=run_id,
        experiment_hash=exp_hash,
        problem_type=problem_type,
        cohort_artifact_id=cohort_artifact_id,
        labels_artifact_id=labels_artifact_id,
        source_pins=dict(frozen_pins),
        splits=split_results,
        model_ids=all_model_ids,
        num_models=len(all_model_ids),
        num_predictions=total_predictions,
        num_evaluations=total_evaluations,
    )


def _require(config: Mapping[str, Any], key: str) -> Any:
    """Fetch a required config key, failing loudly with context if it is absent."""
    if key not in config:
        raise ValueError(
            f"experiment_config is missing required key {key!r}"
            + f" (have: {sorted(config)})"
        )
    return config[key]
