"""Estimator-seam delivery of target history (ADR-0030 — Phase 3/4 close-out).

Proves a ``consumes_target_history`` baseline is fit AND scored from the reserved
``_target_lag_*`` / ``_hist_*`` columns of a matrix — not the feature columns — end to end
through the estimator seam (:func:`_fit_estimator` + :func:`score_matrix`). The matrix Parquet is
written directly, so this needs no featurizer and no database.
"""

from datetime import date

import polars as pl
import pytest

from triage.adapters.matrix import MatrixResult
from triage.adapters.model import _fit_estimator, score_matrix

LAGS = "triage.component.catwalk.baselines.timeseries"


def _matrix(tmp_path, frame, feature_names):
    uri = str(tmp_path / "m.parquet")
    frame.write_parquet(uri)
    return MatrixResult(
        matrix_artifact_id="m-1",
        feature_group_artifact_id="",
        storage_uri=uri,
        num_entities=frame.height,
        num_features=len(feature_names),
        feature_names=feature_names,
        fit_based_stats={},
        cache_hit=False,
    )


def test_persistence_scores_from_reserved_lags_not_features(tmp_path):
    """The seam feeds Persistence the lag columns (chronological); it ignores the real feature."""
    frame = pl.DataFrame(
        {
            "entity_id": [1, 2],
            "as_of_date": [date(2015, 1, 1), date(2015, 1, 1)],
            "outcome": [0.0, 1.0],
            "feat1": [7.0, 7.0],  # a real feature the baseline must NOT use
            "_target_lag_1": [20.0, 200.0],  # newest
            "_target_lag_2": [10.0, 100.0],
            "_target_lag_3": [None, 50.0],
        }
    )
    mr = _matrix(tmp_path, frame, feature_names=["feat1"])

    est, cols = _fit_estimator(mr, f"{LAGS}.Persistence", {}, 42, None)

    # fed the lags in chronological order (oldest -> newest), NOT the feature
    assert cols == ["_target_lag_3", "_target_lag_2", "_target_lag_1"]
    scores = {s["entity_id"]: s["score"] for s in score_matrix(est, mr)}
    assert scores[1] == 20.0  # newest lag, not feat1 (7.0)
    assert scores[2] == 200.0


def test_moving_average_scores_from_reserved_lags(tmp_path):
    frame = pl.DataFrame(
        {
            "entity_id": [1],
            "as_of_date": [date(2015, 1, 1)],
            "outcome": [0.0],
            "feat1": [
                99.0
            ],  # a real feature (matrices always have some); baseline ignores it
            "_target_lag_1": [30.0],
            "_target_lag_2": [20.0],
            "_target_lag_3": [10.0],
        }
    )
    mr = _matrix(tmp_path, frame, feature_names=["feat1"])

    est, _ = _fit_estimator(mr, f"{LAGS}.MovingAverage", {"window": 2}, 42, None)

    # window 2 over chronological [10, 20, 30] -> mean(20, 30) = 25
    assert score_matrix(est, mr)[0]["score"] == 25.0


def test_series_baseline_scores_from_hist_columns(tmp_path):
    frame = pl.DataFrame(
        {
            "entity_id": [1],
            "as_of_date": [date(2015, 1, 1)],
            "outcome": [0.0],
            "feat1": [99.0],  # a real feature; the series baseline ignores it
            "_hist_0": [1.0],
            "_hist_1": [2.0],
            "_hist_2": [3.0],
            "_hist_3": [4.0],
        }
    )
    mr = _matrix(tmp_path, frame, feature_names=["feat1"])

    est, cols = _fit_estimator(mr, f"{LAGS}.SeasonalNaive", {"season": 2}, 42, None)

    assert cols == ["_hist_0", "_hist_1", "_hist_2", "_hist_3"]  # chronological
    # season=2 over [1, 2, 3, 4] -> value 2 steps back = 3
    assert score_matrix(est, mr)[0]["score"] == 3.0


def test_missing_history_columns_raises(tmp_path):
    frame = pl.DataFrame(
        {
            "entity_id": [1],
            "as_of_date": [date(2015, 1, 1)],
            "outcome": [0.0],
            "feat1": [1.0],
        }
    )
    mr = _matrix(tmp_path, frame, feature_names=["feat1"])

    with pytest.raises(ValueError, match="reserved history columns"):
        _fit_estimator(mr, f"{LAGS}.Persistence", {}, 42, None)
