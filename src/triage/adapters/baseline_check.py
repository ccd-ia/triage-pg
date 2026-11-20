"""Cross-check the grid's name-pinned estimators against the feature fan-out.

The check takes the grid specs and the feature subsets as arguments, so it imports neither
:mod:`triage.adapters.run` nor :mod:`triage.adapters.preflight`. Both call it: the run with
its own ``_grid_specs`` and ``_feature_subsets``, the pre-flight through the same two
functions. That keeps one implementation and no import cycle between the two modules.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from triage.adapters.feature_groups import FeatureSubset
from triage.logging import get_logger

logger = get_logger(__name__)

__all__ = ["BaselineIssue", "baseline_issues"]


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
    positional: str | None = None
    """Set when the estimator selects a column by POSITION and the runs disagree on it.

    Each run projects the matrix to its own sorted columns, so one ``feature_index`` is a
    different column wherever a dropped group sorted before it. Nothing raises: the model
    trains on the other column (#98).
    """

    def describe(self) -> str:
        """One line naming the estimator, the column, and which run loses it."""
        name = self.class_path.rsplit(".", 1)[-1]
        if self.detail:
            return f"{name}: {self.detail}"
        if self.positional:
            return f"{name}: {self.positional}"
        columns = ", ".join(self.missing)
        if self.unknown_column:
            return (
                f"{name} pins {columns}, which feature_config does not produce at all"
                " — no run would have it"
            )
        return f"{name} pins {columns} — absent from run {self.subset_label!r}"


def baseline_issues(
    specs: Sequence[tuple[str, Mapping[str, Any]]],
    subsets: Sequence[FeatureSubset],
    columns: Sequence[str],
) -> list[BaselineIssue]:
    """Cross-check every name-pinned estimator against the columns each subset will have.

    ``specs`` is the expanded grid, ``(class_path, hyperparameters)`` per entry, and
    ``subsets`` the run fan-out over ``columns``. Estimators are constructed exactly as the
    training path constructs them and asked for their feature names through the same
    attributes it reads, so this cannot disagree with what ``fit`` would find. Estimators
    that consume the numpy design matrix (the overwhelming majority) are skipped — they
    never name a column.

    An estimator that takes a ``feature_index`` and no name is checked by position: it is
    reported when the column at that index is not the same in every run.
    """
    from triage.adapters.model import _import_estimator, _instantiate

    issues: list[BaselineIssue] = []
    for class_path, hyperparameters in specs:
        try:
            estimator_cls = _import_estimator(class_path)
        except Exception as exc:  # noqa: BLE001 — a bad class_path is the run's error to raise
            logger.debug(f"pre-flight skipped un-importable {class_path}: {exc}")
            continue
        if not (
            getattr(estimator_cls, "consumes_named_features", False)
            or _takes_feature_index(estimator_cls)
        ):
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
            moved = _moved_column(getattr(estimator, "feature_index", None), subsets)
            if moved:
                issues.append(
                    BaselineIssue(
                        class_path=class_path,
                        subset_label="every run",
                        missing=(),
                        positional=moved,
                    )
                )
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


def _takes_feature_index(estimator_cls: Any) -> bool:
    """True when the constructor has a ``feature_index`` parameter."""
    try:
        return "feature_index" in inspect.signature(estimator_cls).parameters
    except (TypeError, ValueError):  # a builtin or C type has no readable signature
        return False


def _moved_column(index: Any, subsets: Sequence[FeatureSubset]) -> str | None:
    """Say which column ``index`` reads in each run, when the runs do not agree.

    ``subset.columns`` is the order the run projects the matrix to, so it is the order the
    estimator indexes. An index out of range in every run is left to ``fit``.
    """
    if not isinstance(index, int) or isinstance(index, bool):
        return None
    read = {
        s.label: s.columns[index] if -len(s.columns) <= index < len(s.columns) else None
        for s in subsets
    }
    if len(set(read.values())) <= 1:
        return None
    per_run = ", ".join(
        f"{column!r} in run {label!r}" if column else f"no column in run {label!r}"
        for label, column in read.items()
    )
    return f"feature_index={index} reads a different column in each run: {per_run}"


def _pinned_feature_names(estimator: Any) -> tuple[str, ...]:
    """The feature columns an estimator selects by name, read the way ``fit`` reads them.

    ``all_feature_names`` covers the rules-based baselines (BaselineRankMultiFeature,
    SimpleThresholder); ``features`` the weighted LinearRanker; ``feature`` the single-column
    PercentileRankOneFeature and SingleFeatureCox.
    """
    for attribute in ("all_feature_names", "features"):
        value = getattr(estimator, attribute, None)
        if isinstance(value, (list, tuple)):
            return tuple(str(v) for v in value)
    single = getattr(estimator, "feature", None)
    if isinstance(single, str):
        return (single,)
    return ()
