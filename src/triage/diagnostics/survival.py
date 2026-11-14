"""Censoring-aware diagnostics of a survival model, computed once and persisted.

The in-PG Harrell C-index ranks survival models (migration 0011) and audition keeps reading
it. What a survival model needs beyond that ranking reads two things the database does not
hold: the training censoring distribution (Uno's C, AUC(t) and Brier(t) weight each test
row by it) and the estimator's survival function (Brier(t) and calibration). Both come from
the model's matrices and joblib file, so :func:`compute_survival_diagnostics` runs once from
``triage postmodel survival`` and persists its rows into ``triage.survival_diagnostics``
(migration 0036). The CLI, ``triage plot`` and the dashboard read them back through
:func:`triage.diagnostics.readers.survival_diagnostics`.

Every number is a scikit-survival call on the stored scores and labels:

* ``km_decile`` — risk deciles with the tie-aware rule of ``monitoring_calibration``
  (``ceil(c · bins / N)``, ``c`` the rows scoring at least as high, peers included), and the
  Kaplan-Meier event probability at the horizon per decile with its lift over the
  population's. A raw share of events would count a row censored before the horizon as an
  event-free one.
* ``km_group`` and ``logrank`` — the KM curve of each risk tertile and the log-rank test
  across them (``sksurv.compare.compare_survival``).
* ``uno_c`` and ``auc_t`` — ``concordance_index_ipcw`` truncated at the horizon and
  ``cumulative_dynamic_auc`` over the time grid; the censoring distribution is fitted on
  the train matrix's labels, never on the test rows.
* ``brier`` and ``calibration`` — for an estimator with ``predict_survival_function``:
  ``brier_score`` over the grid (beside the Brier of the training KM, a model that knows
  nothing of the features), and the predicted against the KM-observed survival at the
  horizon per decile of predicted survival, with ICI (the row-weighted mean gap) and E50
  (its median over the rows). Another estimator gets a NULL row that says
  ``needs predict_survival_function``.

A kind that is undefined on the rows (no event, a time outside the test follow-up) is
persisted with a NULL value and the reason in ``extra.undefined``. Horizons and times are
in the units of the label's ``duration``.

scikit-survival is the optional ``survival`` extra, so it is imported inside the functions.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from triage.logging import get_logger
from triage.util.db import DictRowPool

logger = get_logger(__name__)

#: The kinds of row ``triage.survival_diagnostics`` holds (migration 0036).
KINDS = ("km_decile", "km_group", "logrank", "calibration", "brier", "auc_t", "uno_c")

#: The kinds that read the estimator's survival function.
SURVIVAL_FUNCTION_KINDS = ("brier", "calibration")

#: Why a kind of :data:`SURVIVAL_FUNCTION_KINDS` is NULL for an estimator.
NEEDS_SURVIVAL_FUNCTION = "needs predict_survival_function"

#: Risk deciles and risk groups (tertiles).
DECILES = 10
GROUPS = 3

#: The time grid when none is given: this many evenly spaced points up to the horizon.
GRID_POINTS = 10


@dataclass
class SurvivalRows:
    """The labeled rows of one scored date, in entity order."""

    entity_id: np.ndarray
    score: np.ndarray
    duration: np.ndarray
    event: np.ndarray

    def __len__(self) -> int:
        return len(self.entity_id)

    def structured(self, mask: Optional[np.ndarray] = None) -> Any:
        from sksurv.util import Surv

        keep = slice(None) if mask is None else mask
        return Surv.from_arrays(event=self.event[keep], time=self.duration[keep])


@dataclass
class SurvivalResult:
    """What one ``compute_survival_diagnostics`` call wrote."""

    model_id: int
    estimator: str
    horizon: float
    times: list[float]
    dates: list[Any] = field(default_factory=list)
    rows_written: int = 0


# ------------------------------------------------------------------------- the numbers


def risk_bins(scores: np.ndarray, bins: int) -> np.ndarray:
    """The bin of each row, 1 for the highest scores, with tied scores in one bin.

    ``ceil(c · bins / N)``, where ``c`` counts the rows scoring at least as high as the row
    (its peers included): the rule of ``monitoring_calibration`` (migration 0029), computed
    in integers. A bin a block of ties covers is absent.
    """
    n = len(scores)
    c = n - np.searchsorted(np.sort(scores), scores, side="left")
    return np.minimum(bins, (c * bins + n - 1) // n).astype(int)


def km_at(event: np.ndarray, duration: np.ndarray, t: float) -> Optional[float]:
    """The Kaplan-Meier survival S(t), or None where the rows do not reach t.

    S is undefined past the last observed time while it is above 0: no row was followed
    that long, and nothing says whether its event came. It is 0 from an event that left no
    row at risk.
    """
    from sksurv.nonparametric import kaplan_meier_estimator

    if len(duration) == 0:
        return None
    # (time, survival); a third element only when a confidence interval is asked for
    km = kaplan_meier_estimator(event.astype(bool), duration.astype(float))
    times, surv = km[0], km[1]
    if t < times[0]:
        return 1.0
    if t > times[-1] and surv[-1] > 0:
        return None
    return float(surv[np.searchsorted(times, t, side="right") - 1])


def default_times(horizon: float) -> list[float]:
    """:data:`GRID_POINTS` evenly spaced times from horizon / 10 to the horizon."""
    return [float(t) for t in np.linspace(horizon / GRID_POINTS, horizon, GRID_POINTS)]


def _finite(value: Any) -> Optional[float]:
    value = float(value)
    return value if np.isfinite(value) else None


def _attempt(kind: str, call: Callable[[], Any]) -> tuple[Any, Optional[str]]:
    """Run one scikit-survival call; a refusal of the inputs becomes the row's reason.

    scikit-survival raises ``ValueError`` when its inputs do not define the metric (a time
    past the training follow-up, a censoring survival of 0). The other kinds stay computable,
    so the reason is persisted with a NULL value and logged, not raised.
    """
    try:
        return call(), None
    except ValueError as exc:
        logger.warning(f"survival diagnostics: {kind} is undefined here — {exc}")
        return None, str(exc)


def _row(
    kind: str,
    bin_: int,
    t: float,
    value: Optional[float],
    n: int,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "bin": int(bin_),
        "t": float(t),
        "value": None if value is None else float(value),
        "n": int(n),
        "extra": extra or {},
    }


def km_decile_rows(rows: SurvivalRows, horizon: float) -> list[dict[str, Any]]:
    """The KM event probability by the horizon per risk decile, and for the population."""
    bins = risk_bins(rows.score, DECILES)
    by_horizon = rows.event & (rows.duration <= horizon)
    s_pop = km_at(rows.event, rows.duration, horizon)
    p_pop = None if s_pop is None else 1.0 - s_pop
    out = []
    for b in range(DECILES + 1):
        mask = np.ones(len(rows), dtype=bool) if b == 0 else bins == b
        if not mask.any():
            continue
        s = km_at(rows.event[mask], rows.duration[mask], horizon)
        p = None if s is None else 1.0 - s
        extra: dict[str, Any] = {
            "survival": s,
            "lift": p / p_pop if p is not None and p_pop else None,
            "n_events": int(by_horizon[mask].sum()),
            "mean_score": float(rows.score[mask].mean()),
        }
        if s is None:
            extra["undefined"] = "no row of the bin is followed to the horizon"
        out.append(_row("km_decile", b, horizon, p, int(mask.sum()), extra))
    return out


def km_group_rows(
    rows: SurvivalRows, horizon: float
) -> tuple[list[dict[str, Any]], np.ndarray]:
    """The KM curve of each risk tertile and of the population, one row per step time."""
    from sksurv.nonparametric import kaplan_meier_estimator

    groups = risk_bins(rows.score, GROUPS)
    out = []
    for g in range(GROUPS + 1):
        mask = np.ones(len(rows), dtype=bool) if g == 0 else groups == g
        if not mask.any():
            continue
        duration, event = rows.duration[mask], rows.event[mask]
        km = kaplan_meier_estimator(event, duration)
        times, surv = km[0], km[1]
        ordered = np.sort(duration)
        if times[0] > 0:
            out.append(_row("km_group", g, 0.0, 1.0, int(mask.sum()), {"events": 0}))
        for t, s in zip(times, surv, strict=True):
            at_risk = len(ordered) - int(np.searchsorted(ordered, t, side="left"))
            events = int((event & (duration == t)).sum())
            out.append(_row("km_group", g, t, s, at_risk, {"events": events}))
    return out, groups


def logrank_row(
    rows: SurvivalRows, groups: np.ndarray, horizon: float
) -> dict[str, Any]:
    """The log-rank test across the risk tertiles."""
    from sksurv.compare import compare_survival

    present = np.unique(groups)
    if len(present) < 2:
        return _row(
            "logrank", 0, horizon, None, len(rows),
            {"undefined": "every row is in one risk group (one block of tied scores)"},
        )  # fmt: skip
    if not rows.event.any():
        return _row(
            "logrank", 0, horizon, None, len(rows), {"undefined": "no observed event"}
        )
    result, reason = _attempt(
        "logrank", lambda: compare_survival(rows.structured(), groups)
    )
    if result is None:
        return _row("logrank", 0, horizon, None, len(rows), {"undefined": reason})
    chisq, p = result
    return _row(
        "logrank", 0, horizon, _finite(p), len(rows),
        {"chisq": _finite(chisq), "df": len(present) - 1, "groups": len(present)},
    )  # fmt: skip


def uno_c_row(rows: SurvivalRows, train: Any, horizon: float) -> dict[str, Any]:
    """Uno's C over the comparable pairs before the horizon."""
    from sksurv.metrics import concordance_index_ipcw

    within = int((rows.duration < horizon).sum())
    if not (rows.event & (rows.duration < horizon)).any():
        return _row(
            "uno_c", 0, horizon, None, len(rows),
            {"undefined": "no observed event before the horizon", "n_before": within},
        )  # fmt: skip
    result, reason = _attempt(
        "uno_c",
        lambda: concordance_index_ipcw(
            train, rows.structured(), rows.score, tau=horizon
        ),
    )
    if result is None:
        return _row("uno_c", 0, horizon, None, len(rows), {"undefined": reason})
    cindex, concordant, discordant, tied_risk, tied_time = result
    return _row(
        "uno_c", 0, horizon, _finite(cindex), len(rows),
        {"concordant": int(concordant), "discordant": int(discordant),
         "tied_risk": int(tied_risk), "tied_time": int(tied_time), "n_before": within},
    )  # fmt: skip


def _split_grid(
    rows: SurvivalRows, times: Sequence[float]
) -> tuple[list[float], list[float]]:
    """The grid times inside the test follow-up ``[min, max)``, and the others."""
    lo, hi = float(rows.duration.min()), float(rows.duration.max())
    inside = [t for t in times if lo <= t < hi]
    return inside, [t for t in times if t not in inside]


def _outside(
    kind: str, rows: SurvivalRows, times: Sequence[float]
) -> list[dict[str, Any]]:
    lo, hi = float(rows.duration.min()), float(rows.duration.max())
    reason = f"outside the test follow-up [{lo:g}, {hi:g})"
    return [_row(kind, 0, t, None, len(rows), {"undefined": reason}) for t in times]


def auc_t_rows(
    rows: SurvivalRows, train: Any, times: Sequence[float]
) -> list[dict[str, Any]]:
    """The cumulative/dynamic AUC at each grid time inside the test follow-up."""
    from sksurv.metrics import cumulative_dynamic_auc

    inside, outside = _split_grid(rows, times)
    out = _outside("auc_t", rows, outside)
    if not inside:
        return out
    if not rows.event.any():
        return out + [
            _row("auc_t", 0, t, None, len(rows), {"undefined": "no observed event"})
            for t in inside
        ]
    result, reason = _attempt(
        "auc_t",
        lambda: cumulative_dynamic_auc(train, rows.structured(), rows.score, inside),
    )
    if result is None:
        return out + [
            _row("auc_t", 0, t, None, len(rows), {"undefined": reason}) for t in inside
        ]
    aucs, mean_auc = result
    mean = _finite(mean_auc)
    for t, a in zip(inside, np.atleast_1d(aucs), strict=True):
        extra: dict[str, Any] = {"mean_auc": mean}
        if _finite(a) is None:
            extra["undefined"] = "no observed event by t"
        out.append(_row("auc_t", 0, t, _finite(a), len(rows), extra))
    return out


def _unavailable(
    kind: str, rows: SurvivalRows, horizon: float, why: str
) -> dict[str, Any]:
    return _row(kind, 0, horizon, None, len(rows), {"undefined": why})


def _survival_at(functions: Sequence[Any], times: Sequence[float]) -> np.ndarray:
    """Each row's predicted S at each time: an (n_rows × n_times) array."""
    return np.vstack(
        [np.asarray(fn(np.asarray(times, dtype=float))) for fn in functions]
    )


def brier_rows(
    rows: SurvivalRows,
    train: Any,
    functions: Sequence[Any],
    times: Sequence[float],
) -> list[dict[str, Any]]:
    """Brier(t) at each grid time inside the test follow-up and the functions' domain.

    Each row carries ``km_reference``: the Brier of predicting the training Kaplan-Meier
    survival for every row, the score of a model that knows nothing of the features.
    """
    from sksurv.metrics import brier_score

    inside, outside = _split_grid(rows, times)
    out = _outside("brier", rows, outside)
    lo, hi = functions[0].domain
    defined = [t for t in inside if lo <= t <= hi]
    beyond = f"outside the survival function's domain [{lo:g}, {hi:g}]"
    out += [
        _row("brier", 0, t, None, len(rows), {"undefined": beyond})
        for t in inside
        if t not in defined
    ]
    if not defined:
        return out
    predicted = _survival_at(functions, defined)
    result, reason = _attempt(
        "brier", lambda: brier_score(train, rows.structured(), predicted, defined)
    )
    if result is None:
        return out + [
            _row("brier", 0, t, None, len(rows), {"undefined": reason}) for t in defined
        ]
    train_event, train_time = train["event"], train["time"]
    km_train = [km_at(train_event, train_time, t) for t in defined]
    reference: list[Optional[float]] = [None] * len(defined)
    if all(s is not None for s in km_train):
        flat = np.tile(np.asarray(km_train, dtype=float), (len(rows), 1))
        ref, _ = _attempt(
            "brier", lambda: brier_score(train, rows.structured(), flat, defined)
        )
        if ref is not None:
            reference = [_finite(v) for v in ref[1]]
    return out + [
        _row("brier", 0, t, _finite(b), len(rows), {"km_reference": r})
        for t, b, r in zip(defined, result[1], reference, strict=True)
    ]


def calibration_rows(
    rows: SurvivalRows, functions: Sequence[Any], horizon: float
) -> list[dict[str, Any]]:
    """Predicted against KM-observed survival at the horizon, per decile of prediction.

    Decile 1 holds the lowest predicted survival (the highest predicted risk), with tied
    predictions in one decile. ``bin`` 0 is the population and carries ICI, the row-weighted
    mean of |predicted − observed| over the deciles, and E50, its median over the rows.
    """
    lo, hi = functions[0].domain
    if not lo <= horizon <= hi:
        return [
            _unavailable(
                "calibration", rows, horizon,
                f"the horizon is outside the survival function's domain"
                f" [{lo:g}, {hi:g}]",
            )
        ]  # fmt: skip
    predicted = _survival_at(functions, [horizon])[:, 0]
    bins = risk_bins(1.0 - predicted, DECILES)
    out = []
    gaps: list[float] = []
    weights: list[int] = []
    for b in range(1, DECILES + 1):
        mask = bins == b
        if not mask.any():
            continue
        observed = km_at(rows.event[mask], rows.duration[mask], horizon)
        mean_predicted = float(predicted[mask].mean())
        extra: dict[str, Any] = {"predicted": mean_predicted}
        if observed is None:
            extra["undefined"] = "no row of the bin is followed to the horizon"
        else:
            extra["gap"] = abs(mean_predicted - observed)
            gaps.append(extra["gap"])
            weights.append(int(mask.sum()))
        out.append(_row("calibration", b, horizon, observed, int(mask.sum()), extra))
    observed_pop = km_at(rows.event, rows.duration, horizon)
    summary: dict[str, Any] = {
        "predicted": float(predicted.mean()),
        "ici": float(np.average(gaps, weights=weights)) if gaps else None,
        "e50": float(np.median(np.repeat(gaps, weights))) if gaps else None,
        "n_deciles": len(gaps),
    }
    if observed_pop is None:
        summary["undefined"] = "no row is followed to the horizon"
    return [_row("calibration", 0, horizon, observed_pop, len(rows), summary), *out]


# ---------------------------------------------------------------------- the database


def _label_dates(
    pool: DictRowPool, model_id: int, split_kind: str
) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        return conn.execute(
            "select as_of_date, label_timespan::text as label_timespan"
            " from triage.label_source(%(m)s, cast(%(s)s as triage.split_kind))"
            " order by as_of_date",
            {"m": model_id, "s": split_kind},
        ).fetchall()


def load_rows(
    pool: DictRowPool,
    model_id: int,
    split_kind: str,
    as_of_date: Any,
    label_timespan: str,
) -> SurvivalRows:
    """The scored rows with a survival label on one date (``triage.survival_ranks``)."""
    with pool.connection() as conn:
        found = conn.execute(
            "select entity_id, score, duration, event_observed"
            " from triage.survival_ranks(%(m)s, cast(%(s)s as triage.split_kind),"
            "        cast(%(d)s as date), cast(%(ts)s as interval))"
            " where score is not null order by entity_id",
            {
                "m": model_id,
                "s": split_kind,
                "d": str(as_of_date),
                "ts": label_timespan,
            },
        ).fetchall()
    return SurvivalRows(
        entity_id=np.array([r["entity_id"] for r in found], dtype=np.int64),
        score=np.array([r["score"] for r in found], dtype=float),
        duration=np.array([r["duration"] for r in found], dtype=float),
        event=np.array([r["event_observed"] for r in found], dtype=bool),
    )


def _model(pool: DictRowPool, model_id: int) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "select m.artifact_uri, m.feature_list, mx.storage_uri as train_uri"
            " from triage.models m"
            " left join triage.matrices mx on mx.matrix_uuid = m.train_matrix_uuid"
            " where m.model_id = %(m)s",
            {"m": model_id},
        ).fetchone()
    if row is None:
        raise ValueError(f"No model with id {model_id}")
    return row


def train_survival(model_id: int, train_uri: Optional[str]) -> Any:
    """The (event, time) pairs of the model's train matrix: the censoring distribution.

    Raises:
        ValueError: the model has no train matrix, the file is gone, or it carries no
            labeled survival row.
    """
    from sksurv.util import Surv

    from triage.profiles.storage import read_parquet, storage_for_root

    if train_uri is None:
        raise ValueError(
            f"model {model_id} has no train matrix — Uno's C, AUC(t) and Brier(t) weight"
            " the test rows by the training censoring distribution"
        )
    storage = storage_for_root(train_uri)
    if not storage.exists(train_uri):
        raise ValueError(
            f"model {model_id}'s train matrix is gone from storage ({train_uri}) —"
            " re-run the experiment to rebuild it"
        )
    frame = read_parquet(storage, train_uri)
    missing = [c for c in ("duration", "event_observed") if c not in frame.columns]
    if missing:
        raise ValueError(
            f"train matrix {train_uri} has no {missing} column — it was not built from"
            " a survival label"
        )
    labeled = frame.filter(
        frame["duration"].is_not_null() & frame["event_observed"].is_not_null()
    )
    if labeled.height == 0:
        raise ValueError(f"train matrix {train_uri} has no labeled survival row")
    return Surv.from_arrays(
        event=labeled["event_observed"].to_numpy().astype(bool),
        time=labeled["duration"].to_numpy().astype(float),
    )


def survival_functions(
    pool: DictRowPool,
    model_id: int,
    split_kind: str,
    as_of_date: Any,
    rows: SurvivalRows,
    estimator: Any,
    feature_list: Sequence[str],
) -> Sequence[Any]:
    """The estimator's survival function for each labeled row, from the scored matrix.

    Raises:
        ValueError: the scored matrix lacks a feature or a row the model scored.
    """
    from triage.diagnostics.matrixio import load_matrix_context

    ctx = load_matrix_context(pool, model_id, split_kind)
    features = list(feature_list) or ctx.feature_names
    missing = [f for f in features if f not in ctx.frame.columns]
    if missing:
        raise ValueError(
            f"matrix {ctx.matrix_uuid} lacks the model's feature column(s) {missing[:3]}"
        )
    day = ctx.frame.filter(ctx.frame["as_of_date"].cast(str) == str(as_of_date))
    position = {int(e): i for i, e in enumerate(day["entity_id"].to_list())}
    absent = [int(e) for e in rows.entity_id if int(e) not in position]
    if absent:
        raise ValueError(
            f"model {model_id} scored {len(absent)} labeled row(s) on {as_of_date} that"
            f" matrix {ctx.matrix_uuid} does not hold (entity {absent[0]} first)"
        )
    x = day.select(features).to_numpy().astype(float)
    return estimator.predict_survival_function(
        x[[position[int(e)] for e in rows.entity_id]]
    )


def date_rows(
    rows: SurvivalRows,
    train: Any,
    horizon: float,
    times: Sequence[float],
    functions: Optional[Sequence[Any]],
    no_function: str,
) -> list[dict[str, Any]]:
    """Every persisted row of one date, from arrays alone (no database)."""
    out = km_decile_rows(rows, horizon)
    curves, groups = km_group_rows(rows, horizon)
    out += curves
    out.append(logrank_row(rows, groups, horizon))
    out.append(uno_c_row(rows, train, horizon))
    out += auc_t_rows(rows, train, times)
    if functions is None:
        out += [
            _unavailable(k, rows, horizon, no_function) for k in SURVIVAL_FUNCTION_KINDS
        ]
    else:
        out += brier_rows(rows, train, functions, times)
        out += calibration_rows(rows, functions, horizon)
    return out


def _persist(
    pool: DictRowPool,
    model_id: int,
    split_kind: str,
    as_of_date: Any,
    horizon: float,
    rows: list[dict[str, Any]],
) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "delete from triage.survival_diagnostics"
            " where model_id = %(m)s and split_kind = cast(%(s)s as triage.split_kind)"
            "   and as_of_date = cast(%(d)s as date) and horizon = %(h)s",
            {"m": model_id, "s": split_kind, "d": str(as_of_date), "h": horizon},
        )
        cur.executemany(
            "insert into triage.survival_diagnostics"
            " (model_id, split_kind, as_of_date, kind, horizon, bin, t, value, n, extra)"
            " values (%(m)s, cast(%(s)s as triage.split_kind), cast(%(d)s as date),"
            "         %(kind)s, %(h)s, %(bin)s, %(t)s, %(value)s, %(n)s,"
            "         cast(%(extra)s as jsonb))",
            [
                {
                    "m": model_id,
                    "s": split_kind,
                    "d": str(as_of_date),
                    "h": horizon,
                    **r,
                    "extra": json.dumps(r["extra"]),
                }
                for r in rows
            ],
        )


def compute_survival_diagnostics(
    pool: DictRowPool,
    model_id: int,
    horizon: float,
    times: Optional[Sequence[float]] = None,
    split_kind: str = "test",
) -> SurvivalResult:
    """Compute and persist the survival set of every scored date of one model.

    Re-running replaces the rows of each date at this horizon; rows at another horizon stay.

    Args:
        pool: the project database.
        model_id: a survival model.
        horizon: the time the deciles, Uno's C and calibration are read at, in the units
            of the label's ``duration``.
        times: the grid of AUC(t) and Brier(t); default :func:`default_times`.
        split_kind: the prediction split.

    Returns:
        The estimator's class name, the grid, the dates computed and the rows written.

    Raises:
        ValueError: the horizon is not positive, the model has no train matrix or no
            stored estimator file it can read, or a scored matrix lacks a scored row.
    """
    from triage.adapters.model import _load_estimator

    if not horizon > 0:
        raise ValueError(f"horizon={horizon} — pass a positive duration")
    grid = sorted({float(t) for t in (times or default_times(horizon))})
    if grid[0] <= 0:
        raise ValueError(f"times must be positive durations, got {grid[0]:g}")
    model = _model(pool, model_id)
    train = train_survival(model_id, model["train_uri"])
    estimator = (
        _load_estimator(model["artifact_uri"]) if model["artifact_uri"] else None
    )
    name = type(estimator).__name__ if estimator is not None else "no stored estimator"
    if estimator is None:
        no_function = f"{NEEDS_SURVIVAL_FUNCTION}: model {model_id} stores no estimator"
    elif not hasattr(estimator, "predict_survival_function"):
        no_function = f"{NEEDS_SURVIVAL_FUNCTION}: {name} has none"
    else:
        no_function = ""
    result = SurvivalResult(model_id, name, float(horizon), grid)
    for d in _label_dates(pool, model_id, split_kind):
        rows = load_rows(
            pool, model_id, split_kind, d["as_of_date"], d["label_timespan"]
        )
        if len(rows) == 0:
            logger.warning(
                f"model {model_id} @ {d['as_of_date']}: no scored row with a survival"
                " label — skipping"
            )
            continue
        functions = None
        if not no_function:
            functions = survival_functions(
                pool, model_id, split_kind, d["as_of_date"], rows, estimator,
                model["feature_list"] or [],
            )  # fmt: skip
        persisted = date_rows(rows, train, float(horizon), grid, functions, no_function)
        _persist(pool, model_id, split_kind, d["as_of_date"], float(horizon), persisted)
        result.dates.append(d["as_of_date"])
        result.rows_written += len(persisted)
        logger.info(
            f"survival diagnostics: model {model_id} @ {d['as_of_date']} —"
            f" {len(persisted)} row(s) at horizon {horizon:g}"
        )
    return result
