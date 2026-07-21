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
**chronological** array (oldest -> newest, NaN = missing) so it is trivially unit-testable
independent of the matrix/estimator-seam delivery. ``predict`` maps it over rows; cold-start
(no usable history) yields ``NaN`` (baseline-owned — the delivery layer imputes) except Croston,
whose natural empty-demand forecast is ``0``.
"""

# Fitted/lazy attributes follow the sklearn convention; forecasters are non-parametric (fit is
# a no-op — each entity is forecast from its own history at predict time).
# pyright: reportUninitializedInstanceVariable=false
from __future__ import annotations

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


class _TSBaseline(BaseEstimator):
    """Base for the target-history forecasters.

    ``consumes_target_history`` + ``history_kind`` are the delivery contract the estimator seam
    reads (ADR-0030): a ``"lags"`` estimator is fed the reserved ``_target_lag_*`` columns; a
    ``"series"`` estimator is fed the raw periodic sidecar. ``fit`` is a no-op — these are
    non-parametric and forecast per entity at predict time.
    """

    consumes_target_history = True
    history_kind = "lags"

    def fit(self, X, y=None):
        return self

    def predict(self, H):
        return np.array([self._forecast_one(row) for row in _iter_rows(H)], dtype=float)

    def _forecast_one(self, hist) -> float:
        raise NotImplementedError

    @staticmethod
    def _clean(hist) -> np.ndarray:
        """Chronological history with NaN (missing) dropped; zeros preserved."""
        h = np.asarray(hist, dtype=float)
        return h[~np.isnan(h)]


# ---------------------------------------------------------------- reserved-column family (lags)


class Persistence(_TSBaseline):
    """Naive forecast: the last observed target (``y_{t-1}``)."""

    history_kind = "lags"

    def _forecast_one(self, hist) -> float:
        clean = self._clean(hist)
        return float(clean[-1]) if clean.size else float("nan")


class PromedioDisponible(_TSBaseline):
    """Running mean of all available history."""

    history_kind = "lags"

    def _forecast_one(self, hist) -> float:
        clean = self._clean(hist)
        return float(clean.mean()) if clean.size else float("nan")


class MovingAverage(_TSBaseline):
    """Mean of the last ``window`` available observations (moving-average 3/6/12)."""

    history_kind = "lags"

    def __init__(self, window=3):
        self.window = window

    def _forecast_one(self, hist) -> float:
        clean = self._clean(hist)
        if not clean.size:
            return float("nan")
        return float(clean[-self.window :].mean())


class Drift(_TSBaseline):
    """Last value + the average per-step trend over the available history."""

    history_kind = "lags"

    def _forecast_one(self, hist) -> float:
        clean = self._clean(hist)
        if not clean.size:
            return float("nan")
        if clean.size == 1:
            return float(clean[0])
        slope = (clean[-1] - clean[0]) / (clean.size - 1)
        return float(clean[-1] + slope)


# ---------------------------------------------------------------- raw-series family (series)


class SeasonalNaive(_TSBaseline):
    """The value one season back (``y_{t-season}``); falls back to the last value if the series
    is shorter than one season."""

    history_kind = "series"

    def __init__(self, season=12):
        self.season = season

    def _forecast_one(self, hist) -> float:
        clean = self._clean(hist)
        if not clean.size:
            return float("nan")
        if clean.size >= self.season:
            return float(clean[-self.season])
        return float(clean[-1])


class ETS(_TSBaseline):
    """Simple exponential smoothing (level only) — statsmodels, ``baselines`` extra."""

    history_kind = "series"

    def __init__(self, alpha=None):
        self.alpha = alpha

    def _forecast_one(self, hist) -> float:
        clean = self._clean(hist)
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
    """Holt-Winters exponential smoothing with trend (+ optional seasonality) — statsmodels."""

    history_kind = "series"

    def __init__(self, trend="add", seasonal=None, seasonal_periods=None):
        self.trend = trend
        self.seasonal = seasonal
        self.seasonal_periods = seasonal_periods

    def _forecast_one(self, hist) -> float:
        clean = self._clean(hist)
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
    forecast rate is ``z / x`` (SBA scales it by ``1 - alpha/2`` to debias). Zeros are kept as
    no-demand periods; NaN is dropped. Empty / all-zero history forecasts ``0``.
    """
    d = _TSBaseline._clean(hist)  # keeps zeros, drops NaN
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
    """Croston's method for intermittent / lumpy demand — pure numpy."""

    history_kind = "series"

    def __init__(self, alpha=0.1):
        self.alpha = alpha

    def _forecast_one(self, hist) -> float:
        return _croston_forecast(hist, self.alpha, sba=False)


class CrostonSBA(_TSBaseline):
    """Syntetos-Boylan Approximation — Croston debiased by ``1 - alpha/2``."""

    history_kind = "series"

    def __init__(self, alpha=0.1):
        self.alpha = alpha

    def _forecast_one(self, hist) -> float:
        return _croston_forecast(hist, self.alpha, sba=True)
