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
  ``COUNT(inspections.result|interval=P3M)`` → ``inspections``; over a relationship with a
  ``name:``, the name is resolved to its entity from the config). This reads the entity from the
  feature **name**, NOT featurizer's manifest ``entity`` field, which stamps aggregations with
  the *target* entity and would collapse everything into one group (ADR-0023).
* explicit ``definitions={group: [globs]}`` — each column is matched against the globs; every
  column must land in exactly one group (unmatched / ambiguous columns are a loud error).
  Globs match the column's full featurizer **label** as well as its physical name (pass
  ``labels=``), because a name over PostgreSQL's 63-byte cap is hash-truncated from the tail
  and no glob can be written for the hash. The caller supplies the map as plain data —
  ``adapters/matrix.feature_labels`` builds it from the config — so this module stays pure.

Strategies (ported verbatim from triage's mixer): ``all``, ``leave-one-out``, ``leave-one-in``,
``all-combinations`` (2^N − 1, guarded by ``all_combinations_max_groups``).
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from triage.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "FeatureSubset",
    "STRATEGIES",
    "matches_globs",
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


def matches_globs(
    column: str,
    globs: Sequence[str],
    *,
    labels: Mapping[str, str] | None = None,
) -> bool:
    """True when any glob matches the column's *label* or its physical name.

    The single matching rule for explicit ``feature_groups.definitions``, shared by
    :func:`partition_features` and ``triage analyze-config --features`` so a diagnostic can
    never disagree with what partitioning actually does.

    Names over PostgreSQL's 63-byte cap are hash-truncated from the tail, so a glob aimed at
    an inner fragment (``*frecuencia_cardiaca*``) matches the label but misses the column.
    ``labels`` maps physical column → full label (a column absent from it is its own label),
    so for an untruncated column the two operands are the same string and this is exactly a
    plain match on the column name. Matching the physical name *as well* keeps a glob that
    was hand-written against a truncated name working.
    """
    label = labels.get(column, column) if labels else column
    return any(
        fnmatch.fnmatchcase(label, glob) or fnmatch.fnmatchcase(column, glob)
        for glob in globs
    )


#: A ``<token>.`` qualifier that starts at an identifier boundary, so ``team_games.`` is not
#: found inside ``home_team_games.`` (#9).
_QUALIFIER = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\.")


def qualifier_entities(
    entity_aliases: Sequence[str],
    relationships: Sequence[Mapping[str, Any]] = (),
    target_alias: str | None = None,
) -> dict[str, str]:
    """Map every token featurizer can put before a ``.`` in a column name to its entity.

    An entity alias names itself. A relationship with a ``name:`` replaces the alias in the
    columns it produces (featurizer requires one on parallel edges), so the name has to be
    resolved to an entity from the config (#9). Which end it stands for follows featurizer's
    own rule, the distance to the target: the end farther from the target is the one being
    read, aggregated when it is the child, transferred when it is the parent.
    """
    tokens = {alias: alias for alias in entity_aliases}
    ends: list[tuple[str, str, str]] = []
    for rel in relationships:
        parent, child = rel.get("parent"), rel.get("child")
        if not (isinstance(parent, Mapping) and isinstance(child, Mapping)):
            continue
        ends.append(
            (str(rel.get("name") or ""), str(parent["entity"]), str(child["entity"]))
        )
    distance = _distances(target_alias, [(p, c) for _, p, c in ends])
    for name, parent, child in ends:
        if not name or name in tokens:
            continue
        far = distance.get(parent, 0) > distance.get(child, 0)
        tokens[name] = parent if far else child
    return tokens


def _distances(target: str | None, edges: Sequence[tuple[str, str]]) -> dict[str, int]:
    """Breadth-first hop count from ``target`` over the undirected relationship graph."""
    if target is None:
        return {}
    distance = {target: 0}
    frontier = [target]
    while frontier:
        nxt: list[str] = []
        for node in frontier:
            for a, b in edges:
                for here, there in ((a, b), (b, a)):
                    if here == node and there not in distance:
                        distance[there] = distance[node] + 1
                        nxt.append(there)
        frontier = nxt
    return distance


def _source_entity(column: str, qualifiers: Mapping[str, str]) -> str | None:
    """The source entity of a feature column = the entity of its earliest known qualifier.

    Direct features start with ``<alias>.``; aggregations embed it as ``AGG(<alias>.col…``,
    or ``AGG(<relationship name>.col…`` over a named relationship. With several qualifiers
    present (deep graphs) the *earliest* (outermost source) wins — a predictable default;
    finer control is the explicit ``definitions`` path.
    """
    for match in _QUALIFIER.finditer(column):
        entity = qualifiers.get(match.group(1))
        if entity is not None:
            return entity
    return None


def _unknown_qualifier(column: str) -> str | None:
    """The token of a qualifier in featurizer's position (column start or after ``(``)."""
    for match in _QUALIFIER.finditer(column):
        if match.start() == 0 or column[match.start() - 1] == "(":
            return match.group(1)
    return None


def partition_features(
    feature_names: Sequence[str],
    entity_aliases: Sequence[str],
    *,
    group_by: str = "source_entity",
    definitions: Mapping[str, Sequence[str]] | None = None,
    target_alias: str | None = None,
    labels: Mapping[str, str] | None = None,
    relationships: Sequence[Mapping[str, Any]] = (),
) -> dict[str, list[str]]:
    """Partition ``feature_names`` into ``{group_name: [columns]}``.

    Every feature column must land in exactly one group. A column matching no group (or, under
    explicit definitions, more than one) is a loud ``ValueError`` — a typo'd glob must not
    silently drop or double-count features.

    Under ``group_by='source_entity'`` the source entity is read from the feature name
    (``COUNT(orders.amount…)`` → ``orders``; one-hot ``facilities.zip=…`` → ``facilities``). The
    target entity's *plain* direct variables carry no ``<alias>.`` prefix (featurizer names them
    bare, e.g. ``age``); these are attributed to ``target_alias``. A column that matches no alias
    and has no ``target_alias`` to fall back on is a loud error.

    ``relationships`` is the config's ``relationships:`` list. A relationship ``name:`` stands
    in for the entity alias in the columns it produces, so without it those columns would
    fall through to the target group (#9); :func:`qualifier_entities` resolves each name.

    ``labels`` (physical column → full featurizer label) is consulted **only** on the
    explicit-definitions path, where globs may target any part of a name that PostgreSQL's
    63-byte cap may have truncated. ``group_by='source_entity'`` ignores it: head-keeping
    truncation preserves the leading ``<alias>.`` token, so that path is immune by
    construction. Absent ⇒ every column is its own label (today's behaviour exactly).
    """
    if definitions is not None:
        return _partition_explicit(feature_names, definitions, labels=labels)
    if group_by != "source_entity":
        raise ValueError(
            f"feature_groups.group_by={group_by!r} is not supported"
            " (expected 'source_entity', or provide explicit 'definitions')"
        )
    qualifiers = qualifier_entities(entity_aliases, relationships, target_alias)
    groups: dict[str, list[str]] = {}
    unmatched: list[str] = []
    unresolved: dict[str, int] = {}
    for column in feature_names:
        alias = _source_entity(column, qualifiers)
        if alias is None:
            token = _unknown_qualifier(column)
            if token is not None and target_alias is not None:
                unresolved[token] = unresolved.get(token, 0) + 1
            alias = target_alias
        if alias is None:
            unmatched.append(column)
            continue
        groups.setdefault(alias, []).append(column)
    if unresolved:
        # A warning, not an error: spatial and graph relationships also name their columns,
        # and a config that ran before must not stop running. It must not stay silent (#9).
        logger.warning(
            "feature_groups group_by='source_entity' put columns qualified by "
            f"{sorted(unresolved)!r} ({sum(unresolved.values())} column(s)) in the target "
            f"group {target_alias!r}: no entity alias or relationship name matches those "
            "qualifiers. Declare explicit feature_groups.definitions if they belong elsewhere."
        )
    if unmatched:
        raise ValueError(
            "feature_groups group_by='source_entity' could not map "
            f"{len(unmatched)} column(s) to any entity alias {list(entity_aliases)!r}"
            f"{f' or target {target_alias!r}' if target_alias else ''}: "
            f"{unmatched[:5]}{'…' if len(unmatched) > 5 else ''}. "
            "Declare explicit feature_groups.definitions for these columns."
        )
    return {name: sorted(cols) for name, cols in sorted(groups.items())}


def _partition_explicit(
    feature_names: Sequence[str],
    definitions: Mapping[str, Sequence[str]],
    *,
    labels: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {name: [] for name in definitions}
    for column in feature_names:
        hits = [
            name
            for name, globs in definitions.items()
            if matches_globs(column, globs, labels=labels)
        ]
        if len(hits) == 0:
            label = labels.get(column, column) if labels else column
            shown = f" (label: {label!r})" if label != column else ""
            raise ValueError(
                f"feature column {column!r}{shown} matches no feature_groups.definitions"
                " glob; every column must belong to exactly one group (add a glob or widen"
                " one). Names over PostgreSQL's 63-byte cap are hash-truncated from the"
                " tail, so a glob aimed at an inner fragment misses the column — globs are"
                " matched against the full label as well as the physical name, so write the"
                " glob against the label. Run `triage analyze-config <config> --features"
                " '<glob>'` to see what a glob resolves to."
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
) -> list[tuple[str, tuple[str, ...]]]:
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
        out: list[tuple[str, tuple[str, ...]]] = []
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
