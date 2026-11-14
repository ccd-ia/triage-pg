"""Time-series regression baselines (v1.0.1, Phase 4) — the metric floors for continuous
targets on the ADR-0010 score->rank->evaluate spine.

Each estimator forecasts each entity's next-window target from that entity's OWN prior target
history (ADR-0030), and emits a continuous prediction the spine ranks (regression_ranking) or
scores (regression, RMSE/MAE/pinball). Two families by which history shape they read:

* **Reserved-column family** (``history_kind = "lags"``) — persistence, promedio disponible,
  moving-average, drift. They read the windowed-label lags (the reserved ``_target_lag_*``
  columns). Work off the label; need no ``history_query``.
* **Raw-series family** (``history_kind = "series"``) — seasonal-naive, ETS, Holt-Winters,
  Croston/SBA. They forecast over the raw periodic sidecar and REQUIRE a ``history_query``
  (enforced by :func:`triage.adapters.target_history.require_history_query`). The
  exponential-smoothing pair pulls ``statsmodels`` from the optional ``baselines`` extra
  (decision D3); everything else is pure numpy.

The forecasting math is in :meth:`_forecast_one`, which takes one entity's history as a 1-D
**calendar** array (oldest -> newest, one slot per period, NaN = no value) so it is trivially
unit-testable independent of the matrix/estimator-seam delivery. ``predict`` maps it over rows;
cold-start (no usable history) yields ``NaN`` — the model abstains on that row — except
Croston, whose natural empty-demand forecast is ``0``. Each class declares how it treats an
interior gap (:class:`_TSBaseline`).
"""

# Fitted/lazy attributes follow the sklearn convention; forecasters are non-parametric (fit is
# a no-op — each entity is forecast from its own history at predict time).
# pyright: reportUninitializedInstanceVariable=false
from __future__ import annotations

from typing import Literal

import numpy as np
from sklearn.base import BaseEstimator

__all__ = [
    "Persistence",
    "PromedioDisponible",
    "MovingAverage",
    "Drift",
    "SeasonalNaive",
    "ETS",
    "HoltWinters",
    "Croston",
    "CrostonSBA",
]


def _iter_rows(H):
    """Iterate per-entity history vectors from either a 2-D lag matrix or a ragged list."""
    if isinstance(H, np.ndarray) and H.ndim == 2:
        return [H[i, :] for i in range(H.shape[0])]
    return list(H)


def _first_observed(h: np.ndarray) -> int | None:
    """Index of the first non-NaN value, or ``None`` when the row is all NaN."""
    idx = np.flatnonzero(~np.isnan(h))
    return int(idx[0]) if idx.size else None


class _TSBaseline(BaseEstimator):
    """Base for the target-history forecasters.

    ``consumes_target_history`` + ``history_kind`` are the delivery contract the estimator seam
    reads (ADR-0030): a ``"lags"`` estimator is fed the reserved ``_target_lag_*`` columns; a
    ``"series"`` estimator is fed the raw periodic sidecar. ``fit`` is a no-op — these are
    non-parametric and forecast per entity at predict time.

    Each history row is a **calendar array**: oldest -> newest, one slot per period (or per
    label date for the lags), NaN where no value exists. Leading NaNs mean the series starts
    later and are trimmed. NaNs after the first value are *interior gaps*. Two class attributes
    say how a forecaster treats them:

    * ``requires_complete_calendar`` — the matrix build fails on gaps (unless the config sets
      ``fill_missing_periods``) when this class is in the grid.
    * ``gap_policy`` — ``"abstain"`` (forecast NaN on a row with an interior gap; reachable only
      with ``fill_missing_periods: null``) or ``"calendar_positions"`` (compute on the calendar
      positions, skipping the gaps).
    """

    consumes_target_history = True
    history_kind = "lags"
    requires_complete_calendar: bool = False
    gap_policy: Literal["abstain", "calendar_positions"] = "calendar_positions"

    def fit(self, X, y=None):
        return self

    def predict(self, H):
        return np.array([self._forecast_one(row) for row in _iter_rows(H)], dtype=float)

    def _forecast_one(self, hist) -> float:
        raise NotImplementedError

    @staticmethod
    def _trim_leading(hist) -> np.ndarray:
        """The calendar array from its first value on (leading NaNs dropped)."""
        h = np.asarray(hist, dtype=float)
        first = _first_observed(h)
        return h[first:] if first is not None else h[:0]

    @staticmethod
    def _has_interior_gap(hist) -> bool:
        """True when a NaN follows the first value."""
        return bool(np.isnan(_TSBaseline._trim_leading(hist)).any())

    @staticmethod
    def _observed(hist) -> np.ndarray:
        """The values that exist, in calendar order (every NaN dropped)."""
        h = np.asarray(hist, dtype=float)
        return h[~np.isnan(h)]


# ---------------------------------------------------------------- reserved-column family (lags)


class Persistence(_TSBaseline):
    """Naive forecast: the most recent known target.

    Layout: ``_target_lag_*``, one slot per admissible label date (calendar-placed).
    Gaps: skipped — the newest slot with a value, however far back it is.
    """

    history_kind = "lags"
    requires_complete_calendar = False
    gap_policy = "calendar_positions"

    def _forecast_one(self, hist) -> float:
        known = self._observed(hist)
        return float(known[-1]) if known.size else float("nan")


class PromedioDisponible(_TSBaseline):
    """Running mean of all available history.

    Layout: ``_target_lag_*``, one slot per admissible label date (calendar-placed).
    Gaps: skipped — the mean of the slots that have a value.
    """

    history_kind = "lags"
    requires_complete_calendar = False
    gap_policy = "calendar_positions"

    def _forecast_one(self, hist) -> float:
        known = self._observed(hist)
        return float(known.mean()) if known.size else float("nan")


class MovingAverage(_TSBaseline):
    """Mean of the values in the last ``window`` calendar slots (moving-average 3/6/12).

    Layout: ``_target_lag_*``, one slot per admissible label date (calendar-placed).
    Gaps: skipped inside the window, which never reaches back past ``window`` slots; abstains
    when every slot in the window is empty.
    """

    history_kind = "lags"
    requires_complete_calendar = False
    gap_policy = "calendar_positions"

    def __init__(self, window=3):
        self.window = window

    def _forecast_one(self, hist) -> float:
        h = np.asarray(hist, dtype=float)
        recent = self._observed(h[-self.window :])
        return float(recent.mean()) if recent.size else float("nan")


class Drift(_TSBaseline):
    """Last value plus the average per-slot trend between the first and last values.

    Layout: ``_target_lag_*``, one slot per admissible label date (calendar-placed).
    Gaps: the slope divides by the calendar distance between the first and last values, and
    the forecast extends it from the last value to the slot after the array.
    """

    history_kind = "lags"
    requires_complete_calendar = False
    gap_policy = "calendar_positions"

    def _forecast_one(self, hist) -> float:
        h = np.asarray(hist, dtype=float)
        idx = np.flatnonzero(~np.isnan(h))
        if not idx.size:
            return float("nan")
        last = int(idx[-1])
        if idx.size == 1:
            return float(h[last])
        first = int(idx[0])
        slope = (h[last] - h[first]) / (last - first)
        return float(h[last] + slope * (h.size - last))


# ---------------------------------------------------------------- raw-series family (series)


class SeasonalNaive(_TSBaseline):
    """The value one season back (``y_{t-season}``).

    Layout: ``_hist_*`` placed by calendar offset (slot -1 = one ``history_period`` back).
    Gaps: abstains when the seasonal slot is an interior gap; a series shorter than one season
    falls back to its last value.
    """

    history_kind = "series"
    requires_complete_calendar = True
    gap_policy = "abstain"

    def __init__(self, season=12):
        self.season = season

    def _forecast_one(self, hist) -> float:
        h = np.asarray(hist, dtype=float)
        first = _first_observed(h)
        if first is None:
            return float("nan")
        if h.size >= self.season and h.size - self.season >= first:
            v = h[-self.season]
            return float(v) if not np.isnan(v) else float("nan")  # interior gap
        return float(self._observed(h)[-1])  # shorter than a season: last value


class ETS(_TSBaseline):
    """Simple exponential smoothing (level only) — statsmodels, ``baselines`` extra.

    Layout: ``_hist_*`` placed by calendar offset.
    Gaps: abstains on any interior gap; otherwise fits the series from its first value on.
    """

    history_kind = "series"
    requires_complete_calendar = True
    gap_policy = "abstain"

    def __init__(self, alpha=None):
        self.alpha = alpha

    def _forecast_one(self, hist) -> float:
        if self._has_interior_gap(hist):
            return float("nan")
        clean = self._trim_leading(hist)
        if clean.size < 2:
            return float(clean[-1]) if clean.size else float("nan")
        try:
            from statsmodels.tsa.holtwinters import SimpleExpSmoothing
        except (
            ImportError
        ) as exc:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                "ETS/HoltWinters need statsmodels — install triage[baselines]"
            ) from exc
        kw = (
            {}
            if self.alpha is None
            else {"smoothing_level": self.alpha, "optimized": False}
        )
        fit = SimpleExpSmoothing(clean, initialization_method="heuristic").fit(**kw)
        return float(np.asarray(fit.forecast(1))[0])


class HoltWinters(_TSBaseline):
    """Holt-Winters exponential smoothing with trend (+ optional seasonality) — statsmodels.

    Layout: ``_hist_*`` placed by calendar offset.
    Gaps: abstains on any interior gap; otherwise fits the series from its first value on.
    """

    history_kind = "series"
    requires_complete_calendar = True
    gap_policy = "abstain"

    def __init__(self, trend="add", seasonal=None, seasonal_periods=None):
        self.trend = trend
        self.seasonal = seasonal
        self.seasonal_periods = seasonal_periods

    def _forecast_one(self, hist) -> float:
        if self._has_interior_gap(hist):
            return float("nan")
        clean = self._trim_leading(hist)
        # Holt-Winters needs enough points to estimate trend (and a full season if seasonal).
        need = 2 if self.seasonal is None else 2 * (self.seasonal_periods or 1)
        if clean.size < max(need, 2):
            return float(clean[-1]) if clean.size else float("nan")
        try:
            from statsmodels.tsa.holtwinters import ExponentialSmoothing
        except (
            ImportError
        ) as exc:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                "HoltWinters needs statsmodels — install triage[baselines]"
            ) from exc
        model = ExponentialSmoothing(
            clean,
            trend=self.trend,
            seasonal=self.seasonal,
            seasonal_periods=self.seasonal_periods,
            initialization_method="estimated",
        )
        return float(np.asarray(model.fit().forecast(1))[0])


def _croston_forecast(hist, alpha: float, sba: bool) -> float:
    """Croston's method (SBA variant when ``sba``) for intermittent demand — pure numpy.

    Smooths nonzero demand sizes ``z`` and inter-demand intervals ``x`` separately; the
    forecast rate is ``z / x`` (SBA scales it by ``1 - alpha/2`` to debias). The calendar
    array is trimmed of its leading NaNs, so every remaining slot is a period and a zero is a
    no-demand period. An interior gap (NaN) abstains: the intervals would be wrong. An empty
    or all-zero history forecasts ``0``.
    """
    if _TSBaseline._has_interior_gap(hist):
        return float("nan")
    d = _TSBaseline._trim_leading(hist)
    nonzero = np.flatnonzero(d > 0)
    if nonzero.size == 0:
        return 0.0
    first = int(nonzero[0])
    z = float(d[first])
    x = float(first + 1)  # periods from series start to the first demand
    q = 1
    for t in range(first + 1, d.size):
        if d[t] > 0:
            z = alpha * d[t] + (1 - alpha) * z
            x = alpha * q + (1 - alpha) * x
            q = 1
        else:
            q += 1
    rate = z / x
    if sba:
        rate *= 1 - alpha / 2
    return float(rate)


class Croston(_TSBaseline):
    """Croston's method for intermittent / lumpy demand — pure numpy.

    Layout: ``_hist_*`` placed by calendar offset; zeros are no-demand periods.
    Gaps: abstains on any interior gap (``fill_missing_periods: zero`` turns them into zeros).
    """

    history_kind = "series"
    requires_complete_calendar = True
    gap_policy = "abstain"

    def __init__(self, alpha=0.1):
        self.alpha = alpha

    def _forecast_one(self, hist) -> float:
        return _croston_forecast(hist, self.alpha, sba=False)


class CrostonSBA(_TSBaseline):
    """Syntetos-Boylan Approximation — Croston debiased by ``1 - alpha/2``.

    Layout: ``_hist_*`` placed by calendar offset; zeros are no-demand periods.
    Gaps: abstains on any interior gap (``fill_missing_periods: zero`` turns them into zeros).
    """

    history_kind = "series"
    requires_complete_calendar = True
    gap_policy = "abstain"

    def __init__(self, alpha=0.1):
        self.alpha = alpha

    def _forecast_one(self, hist) -> float:
        return _croston_forecast(hist, self.alpha, sba=True)
