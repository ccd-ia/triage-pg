"""Time-series regression baselines (v1.0.1, Phase 4).

The forecasting math is checked against hand-computed values on tiny chronological series
(oldest -> newest). The exponential-smoothing pair (ETS/HoltWinters) needs statsmodels from the
optional ``baselines`` extra, so those checks skip when it is absent.
"""

import numpy as np
import pytest

from triage.component.catwalk.baselines.timeseries import (
    Croston,
    CrostonSBA,
    Drift,
    MovingAverage,
    Persistence,
    PromedioDisponible,
    SeasonalNaive,
)


def test_persistence_is_last_observed():
    assert Persistence()._forecast_one([1.0, 2.0, 3.0]) == 3.0


def test_persistence_skips_trailing_missing():
    assert Persistence()._forecast_one([1.0, 2.0, np.nan]) == 2.0


def test_promedio_is_mean_of_available():
    assert PromedioDisponible()._forecast_one([1.0, 2.0, 3.0, np.nan]) == 2.0


def test_moving_average_uses_last_window():
    assert MovingAverage(window=2)._forecast_one([1.0, 2.0, 3.0, 4.0]) == 3.5
    assert MovingAverage(window=3)._forecast_one([1.0, 2.0, 3.0, 4.0]) == 3.0


def test_drift_extrapolates_average_trend():
    # slope = (4-1)/3 = 1 -> forecast = 4 + 1
    assert Drift()._forecast_one([1.0, 2.0, 3.0, 4.0]) == 5.0


def test_seasonal_naive_returns_one_season_back():
    assert SeasonalNaive(season=3)._forecast_one([10, 20, 30, 40, 50, 60]) == 40.0


def test_seasonal_naive_short_series_falls_back_to_last():
    assert SeasonalNaive(season=12)._forecast_one([10, 20]) == 20.0


def test_croston_constant_demand_equals_level():
    assert Croston(alpha=0.1)._forecast_one([5.0, 5.0, 5.0, 5.0]) == pytest.approx(5.0)


def test_croston_all_zero_history_is_zero():
    assert Croston()._forecast_one([0.0, 0.0, 0.0]) == 0.0


def test_croston_sba_debiases_below_croston():
    hist = [0.0, 4.0, 0.0, 4.0, 0.0, 4.0]
    c = Croston(alpha=0.1)._forecast_one(hist)
    sba = CrostonSBA(alpha=0.1)._forecast_one(hist)
    assert c == pytest.approx(2.0)
    assert sba == pytest.approx(c * (1 - 0.1 / 2))


def test_predict_over_2d_lag_matrix():
    H = np.array([[1.0, 2.0, 3.0], [10.0, 20.0, 30.0]])
    assert list(Persistence().predict(H)) == [3.0, 30.0]


def test_predict_over_ragged_series_list():
    out = Persistence().predict([np.array([1.0, 2.0]), np.array([9.0])])
    assert list(out) == [2.0, 9.0]


def test_cold_start_returns_nan():
    assert np.isnan(Persistence()._forecast_one([np.nan, np.nan]))
    assert np.isnan(PromedioDisponible()._forecast_one([]))


def test_ets_and_holtwinters_forecast_trend():
    pytest.importorskip("statsmodels")
    from triage.component.catwalk.baselines.timeseries import ETS, HoltWinters

    series = [float(i) for i in range(1, 13)]  # perfect +1 linear trend
    hw = HoltWinters(trend="add")._forecast_one(series)
    assert hw == pytest.approx(13.0, abs=1.5)  # continues the trend
    assert np.isfinite(ETS()._forecast_one(series))
