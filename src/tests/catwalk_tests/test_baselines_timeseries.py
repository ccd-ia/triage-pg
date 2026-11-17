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


# ------------------------------------------------------------ ETS on short series (#94)


@pytest.mark.parametrize("n", [2, 5, 9])
def test_ets_fixed_alpha_forecasts_a_short_series(n):
    """A fixed alpha is the closed form started at the first value: no 10-point minimum."""
    from triage.component.catwalk.baselines.timeseries import ETS

    series = np.array([np.nan] * 4 + [5.0, 0.0, 7.0, 3.0, 4.0, 6.0, 2.0, 8.0, 1.0][:n])
    level = series[4]
    for value in series[5:]:
        level = 0.5 * value + 0.5 * level
    assert ETS(alpha=0.5)._forecast_one(series) == pytest.approx(level)


@pytest.mark.parametrize("n", [2, 5, 9])
def test_ets_fitted_alpha_forecasts_a_short_series(n):
    pytest.importorskip("statsmodels")
    from triage.component.catwalk.baselines.timeseries import ETS

    series = np.array([5.0, 0.0, 7.0, 3.0, 4.0, 6.0, 2.0, 8.0, 1.0][:n])
    assert np.isfinite(ETS()._forecast_one(series))


def test_ets_issue_94_reproduction_no_longer_raises():
    from triage.component.catwalk.baselines.timeseries import ETS

    hist = np.array([np.nan] * 19 + [5, 0, 7, 3, 4], dtype=float)
    assert np.isfinite(ETS(alpha=0.5)._forecast_one(hist))


def test_ets_abstains_on_an_interior_gap():
    from triage.component.catwalk.baselines.timeseries import ETS

    hist = np.array([np.nan, 5.0, np.nan, 7.0, 3.0])
    assert np.isnan(ETS(alpha=0.5)._forecast_one(hist))
    assert np.isnan(ETS()._forecast_one(hist))


def test_ets_predict_scores_every_short_row():
    """One short series no longer fails the whole scoring step."""
    from triage.component.catwalk.baselines.timeseries import ETS

    x = np.array(
        [
            [np.nan, np.nan, 3.0, 4.0],
            [1.0, 2.0, 3.0, 4.0],
            [np.nan, np.nan, np.nan, 6.0],
            [np.nan, 1.0, np.nan, 2.0],
        ]
    )
    scores = ETS(alpha=0.3).predict(x)
    assert np.isfinite(scores[:3]).all()
    assert np.isnan(scores[3])
