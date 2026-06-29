"""Feature groups + mixing strategies (ADR-0023).

Original triage's ``FeatureGroupMixer`` re-expressed natively over **featurizer**'s output
columns. A *feature group* is a named partition of the feature columns; a *strategy* sweeps the
groups into feature-column *subsets*; each subset becomes a Run under one Experiment (ADR-0022).

This module is **pure** (no DB, no featurizer import): it takes the featurizer feature-column
names + the entity aliases and returns the subsets. The run orchestrator (``adapters/run.py``)
turns each subset into a column-projected matrix + Run.

Grouping:

* ``group_by='source_entity'`` (default) — partition by the *source* entity in each column
  name (``facilities.facility_type=…`` → ``facilities``;
  ``COUNT(inspections.result|interval=P3M)`` → ``inspections``). This reads the entity from the
  feature **name**, NOT featurizer's manifest ``entity`` field, which stamps aggregations with
  the *target* entity and would collapse everything into one group (ADR-0023).
* explicit ``definitions={group: [globs]}`` — each column is matched against the globs; every
  column must land in exactly one group (unmatched / ambiguous columns are a loud error).

Strategies (ported verbatim from triage's mixer): ``all``, ``leave-one-out``, ``leave-one-in``,
``all-combinations`` (2^N − 1, guarded by ``all_combinations_max_groups``).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

from triage.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "FeatureSubset",
    "STRATEGIES",
    "partition_features",
    "mix_strategies",
]

STRATEGIES = ("all", "leave-one-out", "leave-one-in", "all-combinations")

DEFAULT_ALL_COMBINATIONS_MAX_GROUPS = 6


@dataclass(frozen=True)
class FeatureSubset:
    """One feature-column subset produced by a strategy = one Run's feature attempt."""

    label: str
    """Stable human label, e.g. ``'all'``, ``'leave-one-in:inspections'``."""
    group_names: tuple[str, ...]
    """The groups included in this subset (sorted)."""
    columns: tuple[str, ...]
    """The feature columns included (sorted) — the projection applied to the matrix."""


def _source_entity(column: str, entity_aliases: Sequence[str]) -> str | None:
    """The source entity of a feature column = the earliest ``<alias>.`` token in its name.

    Direct features start with ``<alias>.``; aggregations embed it as ``AGG(<alias>.col…``.
    With several aliases present (deep graphs) the *earliest* (outermost source) wins — a
    predictable default; finer control is the explicit ``definitions`` path.
    """
    best_alias: str | None = None
    best_pos = len(column) + 1
    for alias in entity_aliases:
        pos = column.find(f"{alias}.")
        if pos != -1 and pos < best_pos:
            best_pos = pos
            best_alias = alias
    return best_alias


def partition_features(
    feature_names: Sequence[str],
    entity_aliases: Sequence[str],
    *,
    group_by: str = "source_entity",
    definitions: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, list[str]]:
    """Partition ``feature_names`` into ``{group_name: [columns]}``.

    Every feature column must land in exactly one group. A column matching no group (or, under
    explicit definitions, more than one) is a loud ``ValueError`` — a typo'd glob must not
    silently drop or double-count features.
    """
    if definitions is not None:
        return _partition_explicit(feature_names, definitions)
    if group_by != "source_entity":
        raise ValueError(
            f"feature_groups.group_by={group_by!r} is not supported"
            " (expected 'source_entity', or provide explicit 'definitions')"
        )
    groups: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for column in feature_names:
        alias = _source_entity(column, entity_aliases)
        if alias is None:
            unmatched.append(column)
            continue
        groups.setdefault(alias, []).append(column)
    if unmatched:
        raise ValueError(
            "feature_groups group_by='source_entity' could not map "
            f"{len(unmatched)} column(s) to any entity alias {list(entity_aliases)!r}: "
            f"{unmatched[:5]}{'…' if len(unmatched) > 5 else ''}. "
            "Declare explicit feature_groups.definitions for these columns."
        )
    return {name: sorted(cols) for name, cols in sorted(groups.items())}


def _partition_explicit(
    feature_names: Sequence[str],
    definitions: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {name: [] for name in definitions}
    for column in feature_names:
        hits = [
            name
            for name, globs in definitions.items()
            if any(fnmatch.fnmatchcase(column, g) for g in globs)
        ]
        if len(hits) == 0:
            raise ValueError(
                f"feature column {column!r} matches no feature_groups.definitions glob; "
                "every column must belong to exactly one group (add a glob or widen one)."
            )
        if len(hits) > 1:
            raise ValueError(
                f"feature column {column!r} matches multiple groups {hits!r}; "
                "feature_groups.definitions globs must be mutually exclusive."
            )
        groups[hits[0]].append(column)
    empty = [name for name, cols in groups.items() if not cols]
    if empty:
        raise ValueError(
            f"feature_groups.definitions group(s) {empty!r} matched no columns "
            "(a group with no features can't form a subset)."
        )
    return {name: sorted(cols) for name, cols in groups.items()}


def _strategy_combos(
    group_names: Sequence[str],
    strategy: str,
    *,
    all_combinations_max_groups: int,
) -> list[tuple[str, str]]:
    """Return ``[(label_suffix, group_name_tuple)]`` for one strategy over the group names."""
    names = sorted(group_names)
    n = len(names)
    if strategy == "all":
        return [("all", tuple(names))]
    if strategy == "leave-one-in":
        return [(f"leave-one-in:{g}", (g,)) for g in names]
    if strategy == "leave-one-out":
        # all groups except one; with a single group this is the empty set → dropped upstream.
        return [
            (f"leave-one-out:{g}", tuple(x for x in names if x != g)) for g in names
        ]
    if strategy == "all-combinations":
        if n > all_combinations_max_groups:
            raise ValueError(
                f"feature_groups strategy 'all-combinations' over {n} groups would build "
                f"2^{n}-1={2**n - 1} subsets, above the cap "
                f"all_combinations_max_groups={all_combinations_max_groups}. Raise the cap "
                "deliberately or use leave-one-out/leave-one-in."
            )
        out: list[tuple[str, str]] = []
        for k in range(1, n + 1):
            for combo in combinations(names, k):
                out.append((f"all-combinations:{'+'.join(combo)}", combo))
        return out
    raise ValueError(
        f"unknown feature-group strategy {strategy!r} (expected one of {STRATEGIES})"
    )


def mix_strategies(
    groups: Mapping[str, Sequence[str]],
    strategies: Sequence[str],
    *,
    all_combinations_max_groups: int = DEFAULT_ALL_COMBINATIONS_MAX_GROUPS,
) -> list[FeatureSubset]:
    """Expand ``strategies`` over ``groups`` into the deduped list of :class:`FeatureSubset`.

    Empty subsets (``leave-one-out`` of a single group) are dropped with a warning; subsets that
    repeat across strategies (same set of groups) are deduped, keeping the first label seen, so
    e.g. ``all`` and a single-group ``leave-one-in`` don't build the same matrix twice.
    """
    if not groups:
        raise ValueError(
            "feature_groups: no groups to mix (partition produced nothing)"
        )
    seen: dict[frozenset[str], FeatureSubset] = {}
    ordered: list[FeatureSubset] = []
    for strategy in strategies:
        for label, names in _strategy_combos(
            list(groups),
            strategy,
            all_combinations_max_groups=all_combinations_max_groups,
        ):
            if not names:
                logger.warning(
                    f"feature_groups strategy {strategy!r} produced an empty subset "
                    "(leave-one-out needs ≥2 groups) — skipped."
                )
                continue
            key = frozenset(names)
            if key in seen:
                continue
            columns = tuple(sorted(c for g in names for c in groups[g]))
            subset = FeatureSubset(
                label=label, group_names=tuple(sorted(names)), columns=columns
            )
            seen[key] = subset
            ordered.append(subset)
    if not ordered:
        raise ValueError(
            f"feature_groups: strategies {list(strategies)!r} over groups {list(groups)!r} "
            "produced no usable subsets."
        )
    return ordered
