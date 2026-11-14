"""Prototypes — the most typical entity of each class at a top-k cut (migration 0037).

On each scored date every labeled row falls in one class at the cut: ``tp`` (selected and
positive), ``fp`` (selected, not positive), ``fn`` (passed over, positive) or ``tn``
(passed over, not positive). Selected means a population rank ≤ k, with k =
``resolve_k(parameter, n)`` over the n scored rows (ADR-0033); positive means
``outcome > 0``, which reads a binary label and a regression_ranking target alike.

The features are z-scored over the date's labeled rows. A class's centroid is the mean of
its rows, and its prototypes are the rows nearest that centroid (Euclidean distance, ties
broken by entity_id). Each prototype keeps its largest deviations from the centroid, so a
reader sees what makes it typical and where it is not.

This is descriptive only. The mean and standard deviation describe one date's rows and
transform no other data, so the train/test leakage boundary does not apply. A feature
that is constant over the date's labeled rows has no z-score and is left out of the
distance. A feature with a missing value refuses the date: the matrix should hold no
NULL, since every feature has an imputation rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from triage.diagnostics.matrixio import load_matrix_context, scored_dates
from triage.logging import get_logger
from triage.util.db import DictRowPool

logger = get_logger(__name__)

#: The classes of a labeled row at a cut, in the order they are stored and read.
CLASSES = ("tp", "fp", "fn", "tn")

#: How many of a prototype's deviations from its centroid are kept.
DEVIATIONS_KEPT = 10


@dataclass
class ComputedPrototypes:
    """What one :func:`compute_prototypes` call wrote.

    Attributes:
        rows_written: the prototype rows persisted.
        dates: the dates that produced at least one prototype.
        dropped_features: per date, the features left out because they are constant over
            the date's labeled rows.
    """

    rows_written: int = 0
    dates: list[str] = field(default_factory=list)
    dropped_features: dict[str, list[str]] = field(default_factory=dict)


def class_at_cut(rank_abs: int, outcome: float, k: int) -> str:
    """``tp``/``fp``/``fn``/``tn`` of one labeled row at a top-k cut."""
    selected, positive = rank_abs <= k, outcome > 0
    if selected:
        return "tp" if positive else "fp"
    return "fn" if positive else "tn"


def _labeled_ranks(
    pool: DictRowPool, model_id: int, split_kind: str, as_of_date: str, parameter: str
) -> tuple[list[dict[str, Any]], int]:
    """The date's labeled rows with their population ranks, and k over every scored row."""
    with pool.connection() as conn:
        rows = conn.execute(
            "with ls as ("
            "    select label_timespan"
            "    from triage.label_source(%(m)s, cast(%(s)s as triage.split_kind))"
            "    where as_of_date = cast(%(d)s as date)),"
            " pr as ("
            "    select p.entity_id, p.rank_abs, p.outcome"
            "    from ls cross join lateral triage.population_ranks("
            "        %(m)s, cast(%(s)s as triage.split_kind), cast(%(d)s as date),"
            "        ls.label_timespan, '') p)"
            " select entity_id, rank_abs, outcome,"
            "        triage.resolve_k(%(p)s, (select count(*)::int from pr)) as k"
            " from pr order by rank_abs",
            {"m": model_id, "s": split_kind, "d": as_of_date, "p": parameter},
        ).fetchall()
    if not rows:
        return [], 0
    k = int(rows[0]["k"])
    return [r for r in rows if r["outcome"] is not None], k


def nearest_to_centroid(
    z: np.ndarray, entity_ids: np.ndarray, n: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The ``n`` rows of ``z`` nearest their centroid.

    Args:
        z: the class's rows in z-scored feature space, one row per entity.
        entity_ids: the entity of each row, which breaks a tie in distance.
        n: how many rows to keep.

    Returns:
        The kept rows' positions in ``z`` (nearest first), their distances, and the
        centroid.
    """
    centroid = z.mean(axis=0)
    distance = np.sqrt(((z - centroid) ** 2).sum(axis=1))
    order = np.lexsort((entity_ids, distance))[:n]
    return order, distance[order], centroid


def deviations(
    raw: np.ndarray,
    z: np.ndarray,
    centroid: np.ndarray,
    features: list[str],
    kept: int = DEVIATIONS_KEPT,
) -> list[dict[str, Any]]:
    """One entity's largest departures from its class centroid, largest first.

    Args:
        raw: the entity's feature values.
        z: the same values, z-scored.
        centroid: the class centroid, z-scored.
        features: the feature names, in column order.
        kept: how many to keep.

    Returns:
        ``feature, value, z, centroid_z, deviation`` per feature, by |deviation|
        descending, then by feature name.
    """
    gap = z - centroid
    order = sorted(range(len(features)), key=lambda j: (-abs(gap[j]), features[j]))
    return [
        {
            "feature": features[j],
            "value": float(raw[j]),
            "z": float(z[j]),
            "centroid_z": float(centroid[j]),
            "deviation": float(gap[j]),
        }
        for j in order[:kept]
    ]


def compute_prototypes(
    pool: DictRowPool,
    model_id: int,
    parameter: str = "100_abs",
    n: int = 3,
    split_kind: str = "test",
    as_of_date: Any = None,
) -> ComputedPrototypes:
    """Compute and persist each class's prototypes at the cut, on every scored date (or one).

    Re-running replaces a date's rows at the cut, so a smaller ``n`` leaves no stale rank.

    Args:
        pool: the project database.
        model_id: the model.
        parameter: the top-k cut, e.g. ``'100_abs'``.
        n: the prototypes kept per class.
        split_kind: the prediction split.
        as_of_date: one scored date; ``None`` computes every one.

    Returns:
        The rows written, the dates with a prototype and the features left out per date.

    Raises:
        ValueError: ``n`` is below 1; the model has no scored matrix
            (:func:`~triage.diagnostics.matrixio.load_matrix_context`); or a feature has a
            missing value among a date's labeled rows.
    """
    if n < 1:
        raise ValueError(f"n={n} keeps no prototype — pass 1 or more")
    ctx = load_matrix_context(pool, model_id, split_kind)
    dates = (
        [str(as_of_date)]
        if as_of_date is not None
        else [str(d) for d in scored_dates(pool, model_id, split_kind)]
    )
    result = ComputedPrototypes()
    for date in dates:
        labeled, k = _labeled_ranks(pool, model_id, split_kind, date, parameter)
        if not labeled:
            logger.warning(f"model {model_id}: no labeled row at {date} — skipping")
            continue
        day = ctx.frame.filter(ctx.frame["as_of_date"].cast(str) == date)
        position = {int(e): i for i, e in enumerate(day["entity_id"].to_list())}
        present = [r for r in labeled if int(r["entity_id"]) in position]
        if len(present) != len(labeled):
            logger.warning(
                f"model {model_id} @ {date}: {len(labeled) - len(present)} labeled"
                " entities missing from the matrix — using the intersection"
            )
        if len(present) < 2:
            logger.warning(
                f"model {model_id} @ {date}: {len(present)} labeled row(s) in the"
                " matrix — no z-score, skipping"
            )
            continue
        raw = (
            day.select(ctx.feature_names)
            .to_numpy()
            .astype(float)[[position[int(r["entity_id"])] for r in present]]
        )
        missing = [
            f for j, f in enumerate(ctx.feature_names) if np.isnan(raw[:, j]).any()
        ]
        if missing:
            raise ValueError(
                f"model {model_id} @ {date}: feature(s) {', '.join(missing[:5])} have"
                " missing values among the labeled rows — prototypes need a complete"
                " matrix; give each feature an imputation rule"
            )
        std = raw.std(axis=0)
        varies = std > 0
        features = [f for f, v in zip(ctx.feature_names, varies, strict=True) if v]
        dropped = [f for f, v in zip(ctx.feature_names, varies, strict=True) if not v]
        if dropped:
            result.dropped_features[date] = dropped
            logger.info(
                f"model {model_id} @ {date}: {len(dropped)} constant feature(s) left"
                " out of the distance"
            )
        if not features:
            logger.warning(
                f"model {model_id} @ {date}: every feature is constant over the"
                " labeled rows — no prototype"
            )
            continue
        values = raw[:, varies]
        z = (values - values.mean(axis=0)) / std[varies]
        entity_ids = np.array([int(r["entity_id"]) for r in present])
        classes = np.array(
            [class_at_cut(int(r["rank_abs"]), float(r["outcome"]), k) for r in present]
        )
        params: list[dict[str, Any]] = []
        for cls in CLASSES:
            rows = np.flatnonzero(classes == cls)
            if rows.size == 0:
                continue
            order, distance, centroid = nearest_to_centroid(
                z[rows], entity_ids[rows], n
            )
            for rank, (at, dist) in enumerate(zip(order, distance, strict=True), 1):
                i = rows[at]
                params.append(
                    {
                        "m": model_id,
                        "s": split_kind,
                        "d": date,
                        "p": parameter,
                        "c": cls,
                        "size": int(rows.size),
                        "rank": rank,
                        "e": int(entity_ids[i]),
                        "dist": float(dist),
                        "dev": json.dumps(
                            deviations(values[i], z[i], centroid, features)
                        ),
                    }
                )
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "delete from triage.prototypes"
                " where model_id = %(m)s"
                "   and split_kind = cast(%(s)s as triage.split_kind)"
                "   and as_of_date = cast(%(d)s as date) and parameter = %(p)s",
                {"m": model_id, "s": split_kind, "d": date, "p": parameter},
            )
            cur.executemany(
                "insert into triage.prototypes"
                " (model_id, split_kind, as_of_date, parameter, class, class_size,"
                "  rank, entity_id, distance, deviations)"
                " values (%(m)s, cast(%(s)s as triage.split_kind), %(d)s, %(p)s, %(c)s,"
                "         %(size)s, %(rank)s, %(e)s, %(dist)s, cast(%(dev)s as jsonb))",
                params,
            )
        result.rows_written += len(params)
        result.dates.append(date)
        logger.info(
            f"prototypes: model {model_id} @ {date} (k={k}) — {len(params)} row(s)"
        )
    return result
