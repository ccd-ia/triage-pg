from unittest.mock import patch

import numpy as np

from triage.component.audition.model_group_performance import (
    ModelGroupPerformancePlotter,
)

from .utils import create_sample_distance_table


def test_ModelGroupPerformancePlotter_generate_plot_data(db_engine_greenfield):
    distance_table, model_groups = create_sample_distance_table(db_engine_greenfield)
    plotter = ModelGroupPerformancePlotter(distance_table)
    df = plotter.generate_plot_data(
        metric="precision@",
        parameter="100_abs",
        model_group_ids=[model_groups["stable"], model_groups["spiky"]],
        train_end_times=["2014-01-01", "2015-01-01"],
    )
    assert sorted(df["model_type"].unique()) == [
        "best case",
        "mySpikeClassifier",
        "myStableClassifier",
    ]
    for value in df[df["model_group_id"] == model_groups["stable"]]["raw_value"].values:
        assert np.isclose(value, 0.5)


def test_ModelGroupPerformancePlotter_plot_all(db_engine_greenfield):
    with patch(
        "triage.component.audition.model_group_performance.plot_cats"
    ) as plot_patch:
        distance_table, model_groups = create_sample_distance_table(
            db_engine_greenfield
        )
        plotter = ModelGroupPerformancePlotter(distance_table)
        plotter.plot_all(
            [{"metric": "precision@", "parameter": "100_abs"}],
            model_group_ids=[model_groups["stable"], model_groups["spiky"]],
            train_end_times=["2014-01-01", "2015-01-01"],
        )
    assert plot_patch.called
    args, kwargs = plot_patch.call_args
    assert "raw_value" in kwargs["frame"]
    assert "train_end_time" in kwargs["frame"]
    assert kwargs["x_col"] == "train_end_time"
    assert kwargs["y_col"] == "raw_value"
