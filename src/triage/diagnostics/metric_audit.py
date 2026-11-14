"""``triage check metrics``: every stored evaluation, recomputed by a second implementation.

The metrics in ``triage.evaluations`` are PL/pgSQL (ADR-0007). This module reads the same
rows those functions read — ``labeled_ranks`` for the rank-free metrics, ``population_ranks``
for the top-k cuts, ``survival_ranks`` for the C-index, at the label horizon of the scored
matrix — and recomputes each value with scikit-learn, numpy or scikit-survival. The row
selection is shared; the arithmetic is not. A stored value and its recomputation agree when
both are undefined, or when they differ by no more than the tolerance.

A metric with no second implementation here (a custom metric, a confusion count) is listed as
not audited, with the reason, and is never counted as passed.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, LiteralString, Optional

import numpy as np

from triage.diagnostics.readers import NotFoundError
from triage.util.db import DictRowPool

#: The default largest absolute gap between a stored value and its recomputation.
DEFAULT_TOLERANCE = 1e-9

#: Two quantiles closer than this are the same (migration 0030's pinball gate).
_QUANTILE_TOL = 1e-9

Rows = dict[str, np.ndarray]


@dataclass(frozen=True)
class Gap:
    """One stored evaluation and its recomputation."""

    model_id: int
    split_kind: str
    as_of_date: date
    subset_hash: str
    metric: str
    parameter: str
    stored: Optional[float]
    recomputed: Optional[float]
    gap: float


@dataclass
class MetricAudit:
    """Every audited row of one metric name."""

    metric: str
    checked: int = 0
    failed: int = 0
    max_gap: float = 0.0
    worst: Optional[Gap] = None


@dataclass(frozen=True)
class NotAudited:
    """Rows of one metric this module could not recompute, and why."""

    metric: str
    rows: int
    reason: str


@dataclass
class AuditReport:
    """What :func:`audit` found."""

    model_ids: list[int]
    tolerance: float
    metrics: list[MetricAudit] = field(default_factory=list)
    not_audited: list[NotAudited] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return sum(m.checked for m in self.metrics)

    @property
    def passed(self) -> bool:
        """At least one row audited, and every audited row within the tolerance."""
        return self.checked > 0 and all(m.failed == 0 for m in self.metrics)

    def to_dict(self) -> dict[str, Any]:
        def gap(g: Optional[Gap]) -> Optional[dict[str, Any]]:
            if g is None:
                return None
            return {**g.__dict__, "as_of_date": g.as_of_date.isoformat()}

        return {
            "model_ids": self.model_ids,
            "tolerance": self.tolerance,
            "passed": self.passed,
            "checked": self.checked,
            "metrics": [
                {
                    "metric": m.metric,
                    "checked": m.checked,
                    "failed": m.failed,
                    "max_gap": m.max_gap,
                    "worst": gap(m.worst),
                }
                for m in self.metrics
            ],
            "not_audited": [n.__dict__ for n in self.not_audited],
        }


# ------------------------------------------------------------------------------ the scope


def models_in_scope(
    pool: DictRowPool,
    *,
    model: Optional[int] = None,
    group: Optional[int] = None,
    experiment: Optional[str] = None,
) -> list[int]:
    """The model ids of exactly one of a model, a model group or an Experiment.

    ``experiment`` is an experiment_hash or a unique prefix of one.

    Raises:
        ValueError: none or more than one of the three is given, or a prefix is ambiguous.
        NotFoundError: the id or hash matches nothing.
    """
    given = [v for v in (model, group, experiment) if v is not None]
    if len(given) != 1:
        raise ValueError("pass exactly one of --model, --group, --experiment")
    with pool.connection() as conn:
        if model is not None:
            rows = conn.execute(
                "select model_id from triage.models where model_id = %(m)s",
                {"m": model},
            ).fetchall()
            what = f"model {model}"
        elif group is not None:
            rows = conn.execute(
                "select model_id from triage.models where model_group_id = %(g)s"
                " order by model_id",
                {"g": group},
            ).fetchall()
            what = f"model group {group}"
        else:
            hashes = conn.execute(
                "select experiment_hash from triage.experiments"
                " where starts_with(experiment_hash, %(p)s)",
                {"p": experiment},
            ).fetchall()
            if len(hashes) > 1:
                raise ValueError(
                    f"experiment prefix {experiment!r} matches {len(hashes)} experiments"
                    " — pass more characters"
                )
            rows = conn.execute(
                "select distinct m.model_id from triage.models m"
                " join triage.runs r on r.run_id = m.run_id"
                " where r.experiment_hash = %(h)s order by m.model_id",
                {"h": hashes[0]["experiment_hash"] if hashes else None},
            ).fetchall()
            what = f"experiment {experiment}"
    if not rows:
        raise NotFoundError(f"{what} not found, or it has no models")
    return [r["model_id"] for r in rows]


# ------------------------------------------------------------------ the recomputations


def _undefined(value: Optional[float]) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _gap(stored: Optional[float], recomputed: Optional[float]) -> float:
    """0 when both are undefined, infinite when one is, else the absolute difference."""
    if (
        stored is None
        or recomputed is None
        or _undefined(stored)
        or _undefined(recomputed)
    ):
        return 0.0 if _undefined(stored) and _undefined(recomputed) else math.inf
    return abs(stored - recomputed)


def _resolve_k(parameter: str, n: int) -> int:
    """``<n>_abs`` is a count, ``<n>_pct`` the ceiling of a share of ``n``; clamped to [0, n]."""
    raw, _, unit = parameter.partition("_")
    if unit == "abs":
        k = math.floor(float(raw))
    elif unit == "pct":
        k = math.ceil(float(raw) / 100 * n)
    else:
        raise ValueError(f"malformed threshold parameter {parameter!r}")
    return max(0, min(k, n))


def _counts(pop: Rows) -> tuple[int, int]:
    """(labeled rows, labeled positives) of a population."""
    labeled = pop["outcome"][~np.isnan(pop["outcome"])]
    return len(labeled), int((labeled > 0).sum())


def _top_k(pop: Rows, parameter: str) -> np.ndarray:
    """The labeled outcomes inside the cut: rows ranked by score descending, entity_id
    ascending breaking ties, the first ``resolve_k(parameter, N)`` kept."""
    k = _resolve_k(parameter, len(pop["score"]))
    order = np.lexsort((pop["entity_id"], -pop["score"]))
    top = pop["outcome"][order[:k]]
    return top[~np.isnan(top)]


def _precision_at(pop: Rows, parameter: str) -> Optional[float]:
    n_labeled, _ = _counts(pop)
    top = _top_k(pop, parameter)
    if n_labeled == 0 or len(top) == 0:  # no labeled row, or none inside the cut
        return None
    return float((top > 0).sum() / len(top))


def _recall_at(pop: Rows, parameter: str) -> Optional[float]:
    n_labeled, n_positive = _counts(pop)
    if n_labeled == 0 or n_positive == 0:
        return None
    return float((_top_k(pop, parameter) > 0).sum() / n_positive)


def _is_probability(s: np.ndarray) -> bool:
    return bool(len(s)) and bool(((s >= 0) & (s <= 1)).all())


def _labeled_metric(
    metric: str, rows: Rows, quantile: Optional[float]
) -> Optional[float]:
    """A rank-free metric over the labeled rows, or None where the SQL leaves it undefined."""
    from sklearn import metrics as sk

    from triage.component.catwalk.metrics import expected_calibration_error

    y, s = rows["outcome"], rows["score"]
    if len(y) == 0:
        return None
    positive = (y > 0).astype(int)
    if metric == "rmse":
        return math.sqrt(sk.mean_squared_error(y, s))
    if metric == "mae":
        return float(sk.mean_absolute_error(y, s))
    if metric == "r2":
        return None if np.all(y == y[0]) else float(sk.r2_score(y, s))
    if metric == "wape":
        volume = float(np.abs(y).sum())
        return None if volume == 0 else float(np.abs(s - y).sum() / volume)
    if metric.startswith("pinball@"):
        tau = float(metric.split("@", 1)[1])
        if quantile is None or abs(quantile - tau) >= _QUANTILE_TOL:
            return None
        return float(sk.mean_pinball_loss(y, s, alpha=tau))
    if metric == "auc_roc":
        if positive.min() == positive.max():
            return None
        return float(sk.roc_auc_score(positive, s))
    if metric == "average_precision":
        return (
            None
            if positive.sum() == 0
            else float(sk.average_precision_score(positive, s))
        )
    if not _is_probability(s):
        return None  # brier, log_loss and ece@ score a probability only
    if metric == "brier":
        return float(sk.brier_score_loss(positive, s))
    if metric == "log_loss":
        return float(sk.log_loss(positive, s, labels=[0, 1]))
    if metric.startswith("ece@"):
        return float(
            expected_calibration_error(positive, s, int(metric.split("@", 1)[1]))
        )
    raise KeyError(metric)


def _c_index(rows: Rows) -> Optional[float]:
    from sksurv.metrics import concordance_index_censored

    if len(rows["score"]) == 0 or not rows["event_observed"].any():
        return None
    try:
        return float(
            concordance_index_censored(
                rows["event_observed"].astype(bool), rows["duration"], rows["score"]
            )[0]
        )
    except ZeroDivisionError:  # no comparable pair
        return None


#: The metrics this module recomputes, by the row set each one reads.
_LABELED = (
    "rmse",
    "mae",
    "r2",
    "wape",
    "auc_roc",
    "average_precision",
    "brier",
    "log_loss",
)
_LABELED_FAMILIES = ("pinball@", "ece@")
_TOP_K = ("precision@", "recall@")


def _kind(metric: str) -> Optional[str]:
    if metric in _LABELED or metric.startswith(_LABELED_FAMILIES):
        return "labeled"
    if metric in _TOP_K:
        return "population"
    if metric == "c_index":
        return "survival"
    return None


# --------------------------------------------------------------------------- the reads


def _arrays(rows: Sequence[dict[str, Any]], columns: Iterable[str]) -> Rows:
    return {
        c: (
            np.array([r[c] for r in rows], dtype=bool)
            if c == "event_observed"
            else np.array([np.nan if r[c] is None else r[c] for r in rows], dtype=float)
        )
        for c in columns
    }


#: Per row set: the ranks function it reads and the columns it returns.
_READS: dict[str, tuple[LiteralString, tuple[str, ...]]] = {
    "labeled": (
        "select entity_id, score, outcome from triage.labeled_ranks(",
        ("entity_id", "score", "outcome"),
    ),
    "population": (
        "select entity_id, score, outcome from triage.population_ranks(",
        ("entity_id", "score", "outcome"),
    ),
    "survival": (
        "select entity_id, score, duration, event_observed from triage.survival_ranks(",
        ("entity_id", "score", "duration", "event_observed"),
    ),
}


def _read(pool: DictRowPool, kind: str, key: dict[str, Any]) -> Rows:
    head, columns = _READS[kind]
    with pool.connection() as conn:
        rows = conn.execute(
            head + "%(m)s, cast(%(split)s as triage.split_kind), %(d)s,"
            " cast(%(ts)s as interval), %(subset)s)",
            key,
        ).fetchall()
    return _arrays(rows, columns)


def _label_timespan(
    pool: DictRowPool, model_id: int, split_kind: str, as_of_date: date
) -> tuple[Optional[str], str]:
    """The label horizon of the matrix these predictions were scored from, or a reason."""
    with pool.connection() as conn:
        row = conn.execute(
            "select count(distinct mx.label_timespan) as n,"
            "       min(mx.label_timespan)::text as ts"
            " from triage.predictions p"
            " join triage.matrices mx on mx.matrix_uuid = p.matrix_uuid"
            " where p.model_id = %(m)s and p.split_kind = cast(%(s)s as triage.split_kind)"
            "   and p.as_of_date = %(d)s",
            {"m": model_id, "s": split_kind, "d": as_of_date},
        ).fetchone()
    if row is None or row["n"] == 0:
        return (
            None,
            "the predictions carry no matrix lineage, so the label horizon is unknown",
        )
    if row["n"] > 1:
        return None, "the predictions come from matrices with different label horizons"
    return row["ts"], ""


# --------------------------------------------------------------------------- the audit


def audit(
    pool: DictRowPool,
    model_ids: Sequence[int],
    tolerance: float = DEFAULT_TOLERANCE,
) -> AuditReport:
    """Recompute every ``triage.evaluations`` row of ``model_ids`` and compare.

    Args:
        pool: the project database.
        model_ids: the models (see :func:`models_in_scope`).
        tolerance: the largest absolute gap that passes.

    Returns:
        Per metric name: the rows checked, the rows outside the tolerance, the largest gap
        and its row. Rows that could not be recomputed are grouped by metric and reason.
    """
    with pool.connection() as conn:
        stored = conn.execute(
            "select e.model_id, e.split_kind::text as split_kind, e.as_of_date,"
            "       e.subset_hash, e.metric, e.parameter, e.value, m.forecast_quantile"
            " from triage.evaluations e"
            " join triage.models m on m.model_id = e.model_id"
            " where e.model_id = any(%(ids)s)"
            " order by e.model_id, e.split_kind, e.as_of_date, e.subset_hash, e.metric,"
            "          e.parameter",
            {"ids": list(model_ids)},
        ).fetchall()

    audits: dict[str, MetricAudit] = {}
    skipped: dict[tuple[str, str], int] = defaultdict(int)
    timespans: dict[tuple[int, str, date], tuple[Optional[str], str]] = {}
    cache: dict[tuple[Any, ...], Rows] = {}
    try:
        import sksurv  # noqa: F401

        has_sksurv = True
    except ImportError:
        has_sksurv = False

    for row in stored:
        metric = row["metric"]
        kind = _kind(metric)
        if kind is None:
            skipped[(metric, "no second implementation of this metric")] += 1
            continue
        if kind == "survival" and not has_sksurv:
            skipped[
                (metric, "scikit-survival is not installed (the survival extra)")
            ] += 1
            continue
        when = (row["model_id"], row["split_kind"], row["as_of_date"])
        if when not in timespans:
            timespans[when] = _label_timespan(pool, *when)
        ts, reason = timespans[when]
        if ts is None:
            skipped[(metric, reason)] += 1
            continue
        key = {
            "m": row["model_id"],
            "split": row["split_kind"],
            "d": row["as_of_date"],
            "ts": ts,
            "subset": row["subset_hash"],
        }
        cache_key = (kind, *key.values())
        if cache_key not in cache:
            cache[cache_key] = _read(pool, kind, key)
        rows = cache[cache_key]
        if kind == "population":
            top_k = _precision_at if metric == "precision@" else _recall_at
            value = top_k(rows, row["parameter"])
        elif kind == "survival":
            value = _c_index(rows)
        else:
            value = _labeled_metric(metric, rows, row["forecast_quantile"])

        gap = _gap(row["value"], value)
        entry = audits.setdefault(metric, MetricAudit(metric))
        entry.checked += 1
        if gap > tolerance:
            entry.failed += 1
        if entry.worst is None or gap > entry.max_gap:
            entry.max_gap = gap
            entry.worst = Gap(
                row["model_id"],
                row["split_kind"],
                row["as_of_date"],
                row["subset_hash"],
                metric,
                row["parameter"],
                row["value"],
                value,
                gap,
            )

    return AuditReport(
        model_ids=list(model_ids),
        tolerance=tolerance,
        metrics=[audits[m] for m in sorted(audits)],
        not_audited=[
            NotAudited(metric, n, reason)
            for (metric, reason), n in sorted(skipped.items())
        ],
    )
