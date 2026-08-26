"""Pre-flight: what an experiment config will actually do, before it does it.

``triage analyze-config`` answers the questions worth asking before committing a grid to a
cluster — how many models will really be trained, how many feature columns exist, what the
feature-group fan-out expands into, how big the cohort is, and what the label base rate looks
like. This module computes those answers; the CLI only renders them.

Two halves, deliberately separated:

* :func:`plan_experiment` is **DB-free**. featurizer's planner runs inside
  ``Featurizer.__init__`` with no database, and triage-pg always declares its one-hot
  vocabularies (adapter-spec §4), so the feature manifest — and with it the fan-out and the
  model count — is fully determined by ``feature_config`` alone.
* :func:`estimate_data` **touches the database**: it renders the cohort and label templates
  exactly as :mod:`triage.adapters.cohort` and :mod:`triage.adapters.labels` do and counts what
  they would insert. Opt-in, because it runs one query per as_of_date.

Every number is produced by the *same* function the run uses — :func:`~triage.adapters.run.
_grid_specs` for the grid, :func:`~triage.adapters.run._feature_subsets` for the fan-out,
:func:`~triage.adapters.model._import_estimator` for the estimators. A diagnostic that computes
its own answer eventually computes a *different* one; that is the whole lesson of the
``feature_groups.definitions`` truncation bug, and it applies here identically.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, LiteralString, cast

from triage.logging import get_logger
from triage.util.db import DictRowPool, returned_row

logger = get_logger(__name__)

__all__ = [
    "BaselineIssue",
    "CohortCount",
    "DataEstimate",
    "ExperimentPlan",
    "LabelCount",
    "SubsetPlan",
    "estimate_data",
    "plan_experiment",
]


@dataclass(frozen=True)
class SubsetPlan:
    """One feature-column subset = one Run the experiment fans out into (ADR-0022/0023)."""

    label: str
    group_names: tuple[str, ...]
    n_columns: int


@dataclass(frozen=True)
class BaselineIssue:
    """A name-pinned estimator whose feature is absent from a subset it would train on.

    The DSSG baselines (rankers, thresholders) select their feature columns **by name** —
    ``consumes_named_features`` marks them. A ``leave-one-out`` fan-out is designed to *remove*
    feature groups, so the two are in direct tension: drop the group holding the pinned column
    and the estimator raises ``BaselineFeatureNotInMatrix`` partway through training, after the
    matrix is already built. Detecting it here costs nothing and reports it before any work.
    """

    class_path: str
    subset_label: str
    missing: tuple[str, ...]
    detail: str | None = None
    """Set instead of ``missing`` when the estimator could not even be constructed."""
    unknown_column: bool = False
    """True when the pinned column is in NO run — feature_config never produces it at all.

    A different defect with a different fix: not "narrow the strategies" but "the name is
    wrong". Reported once for the whole experiment rather than once per run.
    """

    def describe(self) -> str:
        """One line naming the estimator, the column, and which run loses it."""
        name = self.class_path.rsplit(".", 1)[-1]
        if self.detail:
            return f"{name}: {self.detail}"
        columns = ", ".join(self.missing)
        if self.unknown_column:
            return (
                f"{name} pins {columns}, which feature_config does not produce at all"
                " — no run would have it"
            )
        return f"{name} pins {columns} — absent from run {self.subset_label!r}"


@dataclass(frozen=True)
class ExperimentPlan:
    """The DB-free plan: everything derivable from the config alone."""

    n_splits: int
    n_test_matrices: int
    n_as_of_dates: int
    label_timespans: tuple[str, ...]
    grid_size: int
    subsets: tuple[SubsetPlan, ...] = ()
    n_feature_columns: int | None = None
    n_truncated_columns: int = 0
    baseline_issues: tuple[BaselineIssue, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    feature_error: str | None = None
    """Why the feature manifest could not be built, when it could not."""

    @property
    def n_runs(self) -> int:
        """Runs = feature subsets. One per subset, all under one experiment (ADR-0022)."""
        return len(self.subsets) or 1

    @property
    def n_models(self) -> int:
        """Models actually trained: grid size × splits × runs.

        The grid is trained once per train matrix, and there is one train matrix per split per
        subset — which is why a three-way fan-out over a 32-model experiment is 96 fits, not 32.
        """
        return self.grid_size * self.n_splits * self.n_runs

    @property
    def n_model_groups(self) -> int:
        """Model groups: grid size × runs — a group per (estimator, hyperparameters, features).

        The feature list enters the group hash (``model._model_group_hash``), so each feature
        subset gets its OWN groups rather than joining the full-feature run's. That is what makes
        a fan-out's leaderboard comparable: the same estimator on different features is a
        different group, tracked across splits. Verified on a live 4-run fan-out: 3 × 4 = 12.
        """
        return self.grid_size * self.n_runs

    @property
    def n_matrices(self) -> int:
        """Matrices actually built: 2 × splits — one train + one test each. NOT × runs.

        The fan-out is the one number here that does *not* multiply. featurizer runs once per
        split and every subset is a column projection of the same Parquet file (ADR-0023) — no
        re-featurizing, no projected copies. Verified on a live 4-run × 4-split fan-out: 8
        matrix artifacts over 8 distinct Parquet files, not 32.
        """
        return 2 * self.n_splits


@dataclass(frozen=True)
class CohortCount:
    as_of_date: date
    entities: int


@dataclass(frozen=True)
class LabelCount:
    as_of_date: date
    label_timespan: str
    entities: int
    labeled: int
    outcome_mean: float | None
    """Base rate for classification, event rate for survival, mean target for regression."""


@dataclass(frozen=True)
class DataEstimate:
    """What the cohort and label builders would insert, counted without inserting it."""

    cohort: tuple[CohortCount, ...]
    labels: tuple[LabelCount, ...]
    problem_type: str

    @property
    def cohort_total(self) -> int:
        return sum(c.entities for c in self.cohort)

    @property
    def label_total(self) -> int:
        return sum(label.entities for label in self.labels)


# --------------------------------------------------------------------------------------
# DB-free planning
# --------------------------------------------------------------------------------------


def plan_experiment(config: Mapping[str, Any]) -> ExperimentPlan:
    """Derive what ``config`` will do, touching no database.

    Raises ``ValueError`` only for the temporal config, which every other number depends on;
    a feature_config that cannot be planned degrades to ``feature_error`` with the split and
    grid numbers still reported, because a broken ER graph should not hide a broken grid.
    """
    from triage.adapters.run import (
        _generate_splits,
        _grid_specs,
        _union_as_of_dates,
    )
    from triage.adapters.temporal import TemporalConfig

    raw_temporal = config.get("temporal_config")
    if not raw_temporal:
        raise ValueError("temporal_config block is required")
    temporal = TemporalConfig.model_validate(raw_temporal)
    splits = _generate_splits(temporal)
    as_of_dates = _union_as_of_dates(splits)

    label_timespans = list(temporal.training_label_timespans) + list(
        temporal.test_label_timespans
    )
    label_timespans = list(dict.fromkeys(label_timespans))

    grid_config = config.get("grid_config") or {}
    grid_size = len(_grid_specs(grid_config)) if grid_config else 0

    warnings: list[str] = []
    subsets: tuple[SubsetPlan, ...] = ()
    n_columns: int | None = None
    n_truncated = 0
    feature_error: str | None = None
    baseline_issues: tuple[BaselineIssue, ...] = ()

    # The orchestrator assembles exactly ONE test matrix per split and raises otherwise
    # (``run._build_split``). Timechop will happily produce several from multiple
    # test_as_of_date_frequencies, so say it here rather than let the run discover it.
    crowded = [i for i, s in enumerate(splits) if len(s["test_matrices"]) != 1]
    if crowded:
        worst = max(len(splits[i]["test_matrices"]) for i in crowded)
        warnings.append(
            f"{len(crowded)} split(s) produce more than one test matrix (up to {worst});"
            " triage run supports exactly one per split — use a single"
            " test_as_of_date_frequency / test_duration"
        )

    feature_config = config.get("feature_config")
    if isinstance(feature_config, Mapping) and feature_config:
        try:
            columns, labels = _plan_features(feature_config)
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            feature_error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"pre-flight could not plan feature_config: {feature_error}")
        else:
            n_columns = len(columns)
            n_truncated = sum(1 for c in columns if labels.get(c, c) != c)
            subsets = tuple(
                SubsetPlan(
                    label=s.label,
                    group_names=tuple(s.group_names),
                    n_columns=len(s.columns),
                )
                for s in _plan_subsets(feature_config, columns)
            )
            baseline_issues = tuple(
                check_baseline_features(grid_config, feature_config, columns)
            )
    else:
        warnings.append(
            "no feature_config — the model count cannot account for a fan-out"
        )

    return ExperimentPlan(
        n_splits=len(splits),
        n_test_matrices=sum(len(s["test_matrices"]) for s in splits),
        n_as_of_dates=len(as_of_dates),
        label_timespans=tuple(label_timespans),
        grid_size=grid_size,
        subsets=subsets,
        n_feature_columns=n_columns,
        n_truncated_columns=n_truncated,
        baseline_issues=baseline_issues,
        warnings=tuple(warnings),
        feature_error=feature_error,
    )


def _plan_features(
    feature_config: Mapping[str, Any],
) -> tuple[list[str], Mapping[str, str]]:
    """The feature columns the run will build, plus the physical→label map, with no DB."""
    from triage.adapters.matrix import feature_labels
    from triage.adapters.run import _featurizer_only

    labels = feature_labels(_featurizer_only(feature_config))
    return list(labels), labels


def _plan_subsets(feature_config: Mapping[str, Any], columns: Sequence[str]):
    """The run fan-out, through the run's own subset builder (never a reimplementation)."""
    from triage.adapters.run import _feature_subsets, _featurizer_only

    return _feature_subsets(
        feature_config, columns, featurizer_config=_featurizer_only(feature_config)
    )


def check_baseline_features(
    grid_config: Mapping[str, Any],
    feature_config: Mapping[str, Any],
    columns: Sequence[str],
) -> list[BaselineIssue]:
    """Cross-check every name-pinned estimator against the columns each subset will have.

    Estimators are constructed exactly as the training path constructs them and asked for their
    feature names through the same attributes it reads, so this cannot disagree with what
    ``fit`` would find. Estimators that consume the numpy design matrix (the overwhelming
    majority) are skipped — they never name a column.
    """
    if not grid_config:
        return []
    from triage.adapters.model import _import_estimator, _instantiate
    from triage.adapters.run import _grid_specs

    subsets = _plan_subsets(feature_config, columns)
    issues: list[BaselineIssue] = []
    for class_path, hyperparameters in _grid_specs(grid_config):
        try:
            estimator_cls = _import_estimator(class_path)
        except Exception as exc:  # noqa: BLE001 — a bad class_path is the run's error to raise
            logger.debug(f"pre-flight skipped un-importable {class_path}: {exc}")
            continue
        if not getattr(estimator_cls, "consumes_named_features", False):
            continue
        try:
            estimator = _instantiate(estimator_cls, hyperparameters, 0)
        except Exception as exc:  # noqa: BLE001 — surfaced as an issue, not swallowed
            issues.append(
                BaselineIssue(
                    class_path=class_path,
                    subset_label="—",
                    missing=(),
                    detail=f"cannot be constructed from its grid entry: {exc}",
                )
            )
            continue
        pinned = _pinned_feature_names(estimator)
        if not pinned:
            continue
        # A column no run produces is one defect ("the name is wrong"), not one per run —
        # reporting it three times for a three-way fan-out buries the actual fix.
        every_column = set(columns)
        unknown = tuple(f for f in pinned if f not in every_column)
        if unknown:
            issues.append(
                BaselineIssue(
                    class_path=class_path,
                    subset_label="every run",
                    missing=unknown,
                    unknown_column=True,
                )
            )
        for subset in subsets:
            missing = tuple(
                f for f in pinned if f in every_column and f not in set(subset.columns)
            )
            if missing:
                issues.append(
                    BaselineIssue(
                        class_path=class_path,
                        subset_label=subset.label,
                        missing=missing,
                    )
                )
    return issues


def _pinned_feature_names(estimator: Any) -> tuple[str, ...]:
    """The feature columns an estimator selects by name, read the way ``fit`` reads them.

    ``all_feature_names`` covers the rules-based baselines (BaselineRankMultiFeature,
    SimpleThresholder); ``features`` the weighted LinearRanker; ``feature`` the single-column
    PercentileRankOneFeature.
    """
    for attribute in ("all_feature_names", "features"):
        value = getattr(estimator, attribute, None)
        if isinstance(value, (list, tuple)):
            return tuple(str(v) for v in value)
    single = getattr(estimator, "feature", None)
    if isinstance(single, str):
        return (single,)
    return ()


# --------------------------------------------------------------------------------------
# DB-touching estimates
# --------------------------------------------------------------------------------------


def estimate_data(
    pool: DictRowPool,
    config: Mapping[str, Any],
    *,
    max_dates: int | None = None,
) -> DataEstimate:
    """Count what the cohort and label builders would insert, without inserting anything.

    The templates are rendered exactly as :func:`~triage.adapters.cohort.build_cohort` and
    :func:`~triage.adapters.labels.build_labels` render them — the same ``{as_of_date}`` /
    ``{label_timespan}`` substitution — and wrapped in a counting projection. Entities are
    counted ``distinct`` because both builders insert ``on conflict do nothing``, so a template
    returning an entity twice still lands one row.

    ``max_dates`` samples the first N as_of_dates instead of all of them; the caller is
    responsible for saying so, since a sampled total is not a total.
    """
    from triage.adapters.run import _generate_splits, _union_as_of_dates
    from triage.adapters.temporal import TemporalConfig

    problem_type = config.get("problem_type")
    if not isinstance(problem_type, str):
        raise ValueError("problem_type is required to estimate labels (ADR-0010)")

    cohort_query = _query_of(config.get("cohort_config"), "cohort_config")
    label_query = _query_of(config.get("label_config"), "label_config")

    temporal = TemporalConfig.model_validate(config["temporal_config"])
    as_of_dates = _union_as_of_dates(_generate_splits(temporal))
    if max_dates is not None:
        as_of_dates = as_of_dates[:max_dates]
    label_timespans = list(
        dict.fromkeys(
            list(temporal.training_label_timespans)
            + list(temporal.test_label_timespans)
        )
    )

    cohort_counts: list[CohortCount] = []
    label_counts: list[LabelCount] = []
    with pool.connection() as conn:
        for as_of_date in as_of_dates:
            rendered = cohort_query.format(as_of_date=f"'{as_of_date}'")
            row = returned_row(
                conn.execute(
                    cast(
                        LiteralString,
                        f"select count(distinct sub.entity_id) as entities from ({rendered}) sub",
                    )
                ).fetchone()
            )
            cohort_counts.append(
                CohortCount(as_of_date=as_of_date, entities=int(row["entities"]))
            )

        projection = _count_projection(problem_type)
        for as_of_date in as_of_dates:
            for label_timespan in label_timespans:
                rendered = label_query.format(
                    as_of_date=f"'{as_of_date}'",
                    label_timespan=f"interval '{label_timespan}'",
                )
                row = returned_row(
                    conn.execute(
                        cast(
                            LiteralString,
                            f"select {projection} from ({rendered}) sub",
                        )
                    ).fetchone()
                )
                label_counts.append(
                    LabelCount(
                        as_of_date=as_of_date,
                        label_timespan=label_timespan,
                        entities=int(row["entities"]),
                        labeled=int(row["labeled"]),
                        outcome_mean=(
                            None
                            if row["outcome_mean"] is None
                            else float(row["outcome_mean"])
                        ),
                    )
                )

    return DataEstimate(
        cohort=tuple(cohort_counts),
        labels=tuple(label_counts),
        problem_type=problem_type,
    )


def _query_of(block: object, path: str) -> str:
    """The block's non-empty ``query``, or a loud error naming the block."""
    if isinstance(block, Mapping):
        query = block.get("query")
        if isinstance(query, str) and query:
            return query
    raise ValueError(f"{path} needs a 'query' to estimate against the database")


def _count_projection(problem_type: str) -> str:
    """The counting SELECT list for ``problem_type``'s label columns (ADR-0010).

    Mirrors :func:`~triage.adapters.labels._label_projection`: the outcome types supply
    ``outcome``, survival supplies ``duration``/``event_observed``. An unknown problem_type is
    the same loud error the label builder raises, one step earlier.
    """
    from triage.adapters.labels import _OUTCOME_TYPES, _SURVIVAL_TYPE

    if problem_type in _OUTCOME_TYPES:
        return (
            "count(distinct sub.entity_id) as entities,"
            " count(sub.outcome) as labeled,"
            " avg(sub.outcome::double precision) as outcome_mean"
        )
    if problem_type == _SURVIVAL_TYPE:
        return (
            "count(distinct sub.entity_id) as entities,"
            " count(sub.duration) as labeled,"
            " avg(sub.event_observed::int::double precision) as outcome_mean"
        )
    raise ValueError(
        f"unknown problem_type {problem_type!r}; expected one of"
        f" {sorted(_OUTCOME_TYPES) + [_SURVIVAL_TYPE]} (ADR-0010)"
    )
