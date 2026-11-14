"""The time-series baselines read calendar arrays (ADR-0030, calendar placement).

Each history row is oldest -> newest with one slot per period and NaN where no value exists.
Leading NaNs are a late start; NaNs after the first value are interior gaps. These tests pin
what each class does with both, on hand-built arrays.
"""

import math

import numpy as np
import pytest

from triage.adapters.target_history import (
    LAG_BASELINE_CLASSES,
    RAW_SERIES_BASELINE_CLASSES,
    complete_calendar_baselines,
)
from triage.component.catwalk.baselines import timeseries
from triage.component.catwalk.baselines.timeseries import (
    Croston,
    CrostonSBA,
    Drift,
    MovingAverage,
    Persistence,
    SeasonalNaive,
)

NAN = float("nan")


def test_seasonal_naive_reads_twelve_calendar_months_back_past_a_gap():
    """13 months, month 5 missing: the value 12 slots back is month 1, not month 0."""
    hist = [float(m) for m in range(13)]
    hist[5] = NAN
    assert SeasonalNaive(season=12)._forecast_one(hist) == 1.0


def test_seasonal_naive_abstains_when_the_seasonal_slot_is_a_gap():
    hist = [float(m) for m in range(13)]
    hist[1] = NAN
    assert math.isnan(SeasonalNaive(season=12)._forecast_one(hist))


def test_seasonal_naive_short_series_after_leading_nans_uses_last_value():
    """A series that starts late (leading NaNs) and is shorter than a season."""
    hist = [NAN] * 10 + [4.0, 5.0, 6.0]
    assert SeasonalNaive(season=12)._forecast_one(hist) == 6.0


def test_croston_zero_filled_equals_complete_and_unfilled_abstains():
    complete = Croston(alpha=0.1)._forecast_one([5.0, 0.0, 0.0, 7.0])
    zero_filled = np.nan_to_num(np.array([5.0, NAN, NAN, 7.0]), nan=0.0)
    assert Croston(alpha=0.1)._forecast_one(zero_filled) == pytest.approx(
        complete, abs=1e-12
    )
    assert math.isnan(Croston(alpha=0.1)._forecast_one([5.0, NAN, NAN, 7.0]))
    assert math.isnan(CrostonSBA(alpha=0.1)._forecast_one([5.0, NAN, NAN, 7.0]))


def test_croston_ignores_leading_nans():
    late = Croston(alpha=0.1)._forecast_one([NAN, NAN, 5.0, 0.0, 0.0, 7.0])
    assert late == pytest.approx(Croston(alpha=0.1)._forecast_one([5.0, 0.0, 0.0, 7.0]))


def test_exponential_smoothing_abstains_on_an_interior_gap():
    pytest.importorskip("statsmodels")
    from triage.component.catwalk.baselines.timeseries import ETS, HoltWinters

    gapped = [1.0, 2.0, NAN, 4.0, 5.0, 6.0]
    assert math.isnan(ETS()._forecast_one(gapped))
    assert math.isnan(HoltWinters(trend="add")._forecast_one(gapped))
    assert math.isfinite(
        ETS()._forecast_one([NAN, NAN] + [float(i) for i in range(12)])
    )


def test_moving_average_skips_gaps_inside_its_window_only():
    """MovingAverage(3) over [1, 2, NaN, 4] is the mean of [2, 4], not of [1, 2, 4]."""
    assert MovingAverage(window=3)._forecast_one([1.0, 2.0, NAN, 4.0]) == 3.0
    assert math.isnan(MovingAverage(window=2)._forecast_one([1.0, 2.0, NAN, NAN]))


def test_drift_divides_by_calendar_distance():
    """Values 1 (slot 0) and 4 (slot 3) with slot 2 empty: slope 1 per slot, forecast 5."""
    assert Drift()._forecast_one([1.0, NAN, NAN, 4.0]) == 5.0
    # last value two slots before the end: extend the trend to the slot after the array
    assert Drift()._forecast_one([1.0, 2.0, NAN, NAN]) == 5.0


def test_persistence_is_the_most_recent_known_value():
    assert Persistence()._forecast_one([1.0, 2.0, NAN]) == 2.0


def test_every_class_declares_layout_and_gap_policy():
    for class_path in RAW_SERIES_BASELINE_CLASSES | LAG_BASELINE_CLASSES:
        cls = getattr(timeseries, class_path.rsplit(".", 1)[-1])
        assert "requires_complete_calendar" in vars(cls), class_path
        assert "gap_policy" in vars(cls), class_path
        assert cls.gap_policy in ("abstain", "calendar_positions")
        assert "Layout:" in (cls.__doc__ or "") and "Gaps:" in (cls.__doc__ or "")
    # the raw-series family is exactly the set that needs a complete calendar
    grid = {c: {} for c in RAW_SERIES_BASELINE_CLASSES | LAG_BASELINE_CLASSES}
    assert set(complete_calendar_baselines(grid)) == set(RAW_SERIES_BASELINE_CLASSES)
