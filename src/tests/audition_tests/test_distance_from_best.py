from unittest.mock import patch

import numpy as np
import pytest

from triage.component.audition.distance_from_best import (
    BestDistancePlotter,
    DistanceFromBestTable,
)

from .utils import (
    create_sample_distance_table,
    insert_evaluation,
    insert_model,
    insert_model_group,
)


def test_DistanceFromBestTable(db_pool_greenfield):
    db_engine = db_pool_greenfield

    # Three model groups across three train end times each. In the greenfield
    # schema evaluations carry only an as_of_date (no start/end window), so there
    # is nothing to de-dup — we seed exactly one test-split evaluation per
    # (model, metric) at the model's train_end_time, with the metric's value.
    with db_engine.connection() as conn:
        model_groups = {
            "stable": insert_model_group(conn, "myStableClassifier"),
            "bad": insert_model_group(conn, "myBadClassifier"),
            "spiky": insert_model_group(conn, "mySpikeClassifier"),
        }

        train_end_times = ["2014-01-01", "2015-01-01", "2016-01-01"]

        # value tables keyed by (group, train_end_time) mirroring the original
        # immediate-eval fixture (the month-out rows were dropped by the old
        # de-dup CTE, so they never affected the asserted result).
        precision_values = {
            ("stable", "2014-01-01"): 0.6,
            ("stable", "2015-01-01"): 0.57,
            ("stable", "2016-01-01"): 0.59,
            ("bad", "2014-01-01"): 0.4,
            ("bad", "2015-01-01"): 0.39,
            ("bad", "2016-01-01"): 0.43,
            ("spiky", "2014-01-01"): 0.8,
            ("spiky", "2015-01-01"): 0.4,
            ("spiky", "2016-01-01"): 0.4,
        }
        recall_values = {
            ("stable", "2014-01-01"): 0.55,
            ("stable", "2015-01-01"): 0.56,
            ("stable", "2016-01-01"): 0.55,
            ("bad", "2014-01-01"): 0.35,
            ("bad", "2015-01-01"): 0.34,
            ("bad", "2016-01-01"): 0.36,
            ("spiky", "2014-01-01"): 0.35,
            ("spiky", "2015-01-01"): 0.8,
            ("spiky", "2016-01-01"): 0.36,
        }

        for grp_name, model_group_id in model_groups.items():
            for tet in train_end_times:
                model_id = insert_model(conn, model_group_id, tet)
                insert_evaluation(
                    conn,
                    model_id,
                    "precision@",
                    "100_abs",
                    precision_values[(grp_name, tet)],
                    as_of_date=tet,
                )
                insert_evaluation(
                    conn,
                    model_id,
                    "recall@",
                    "100_abs",
                    recall_values[(grp_name, tet)],
                    as_of_date=tet,
                )

    distance_table = DistanceFromBestTable(
        db_engine=db_engine,
        models_table="models",
        distance_table="dist_table",
        agg_type="worst",
    )
    metrics = [
        {"metric": "precision@", "parameter": "100_abs"},
        {"metric": "recall@", "parameter": "100_abs"},
    ]
    model_group_ids = list(model_groups.values())
    distance_table.create_and_populate(model_group_ids, ["2014-01-01", "2015-01-01", "2016-01-01"], metrics)

    # get an ordered list of the model groups for a particular metric/time
    query = """
        select
            model_group_id,
            raw_value,
            dist_from_best_case,
            dist_from_best_case_next_time
        from dist_table
        where metric = %(metric)s
        and parameter = %(threshold)s
        and train_end_time = %(train_end_time)s
        order by dist_from_best_case
        """

    # greenfield evaluations.value is double precision (the old ORM
    # stochastic_value was numeric/Decimal), so compare the float columns under
    # tolerance while keeping the model_group_id / ordering exact. The pool uses
    # dict_row, so flatten each row to a tuple in column-select order first.
    def as_tuple(row):
        return (
            row["model_group_id"],
            row["raw_value"],
            row["dist_from_best_case"],
            row["dist_from_best_case_next_time"],
        )

    def assert_rows(actual, expected):
        assert len(actual) == len(expected)
        for got_row, want in zip(actual, expected):
            got = as_tuple(got_row)
            assert got[0] == want[0]
            assert got[1:] == pytest.approx(want[1:])

    with db_engine.connection() as conn:
        prec_3y_ago = conn.execute(
            query,
            {
                "metric": "precision@",
                "threshold": "100_abs",
                "train_end_time": "2014-01-01",
            },
        ).fetchall()
        assert_rows(
            prec_3y_ago,
            [
                (model_groups["spiky"], 0.8, 0, 0.17),
                (model_groups["stable"], 0.6, 0.2, 0),
                (model_groups["bad"], 0.4, 0.4, 0.18),
            ],
        )

        recall_2y_ago = conn.execute(
            query,
            {
                "metric": "recall@",
                "threshold": "100_abs",
                "train_end_time": "2015-01-01",
            },
        ).fetchall()
        assert_rows(
            recall_2y_ago,
            [
                (model_groups["spiky"], 0.8, 0, 0.19),
                (model_groups["stable"], 0.56, 0.24, 0),
                (model_groups["bad"], 0.34, 0.46, 0.19),
            ],
        )

        bounds = distance_table.observed_bounds
        assert set(bounds.keys()) == {
            ("precision@", "100_abs"),
            ("recall@", "100_abs"),
        }
        assert bounds[("precision@", "100_abs")] == pytest.approx((0.39, 0.8))
        assert bounds[("recall@", "100_abs")] == pytest.approx((0.34, 0.8))


def test_BestDistancePlotter(db_pool_greenfield):
    distance_table, model_groups = create_sample_distance_table(db_pool_greenfield)
    plotter = BestDistancePlotter(distance_table)
    df_dist = plotter.generate_plot_data(
        metric="precision@",
        parameter="100_abs",
        model_group_ids=[model_groups["stable"], model_groups["spiky"]],
        train_end_times=["2014-01-01", "2015-01-01"],
    )
    # assert that we have the right # of columns and a row for each % diff value
    # 202 row because 101 percentiles (0-100 inclusive), 2 model groups
    assert df_dist.shape == (101 * 2, 5)

    # all of the model groups are within .34 of the best, so pick
    # a number higher than that and all should qualify
    for value in df_dist[df_dist["distance"] == 0.35]["pct_of_time"].values:
        assert np.isclose(value, 1.0)

    # the stable model group should be within 0.11 1/2 of the time
    # if we included 2016 in the train_end_times, this would be 1/3!
    for value in df_dist[(df_dist["distance"] == 0.11) & (df_dist["model_group_id"] == model_groups["stable"])][
        "pct_of_time"
    ].values:
        assert np.isclose(value, 0.5)


def test_BestDistancePlotter_plot(db_pool_greenfield):
    with patch("triage.component.audition.distance_from_best.plot_cats") as plot_patch:
        distance_table, model_groups = create_sample_distance_table(db_pool_greenfield)
        plotter = BestDistancePlotter(distance_table)
        plotter.plot_all_best_dist(
            [{"metric": "precision@", "parameter": "100_abs"}],
            model_group_ids=[model_groups["stable"], model_groups["spiky"]],
            train_end_times=["2014-01-01", "2015-01-01"],
        )
    assert plot_patch.called
    args, kwargs = plot_patch.call_args
    assert "distance" in kwargs["frame"]
    assert "pct_of_time" in kwargs["frame"]
    assert kwargs["x_col"] == "distance"
    assert kwargs["y_col"] == "pct_of_time"


def test_BestDistancePlotter_plot_bounds():
    class FakeDistanceTable:
        @property
        def observed_bounds(self):
            return {
                ("precision@", "100_abs"): (0.02, 0.87),
                ("recall@", "100_abs"): (0.0, 1.0),
                ("false positives@", "300_abs"): (2, 162),
            }

    plotter = BestDistancePlotter(FakeDistanceTable())
    assert plotter.plot_bounds("precision@", "100_abs") == (0.0, 1.0)
    assert plotter.plot_bounds("recall@", "100_abs") == (0.0, 1.0)
    assert plotter.plot_bounds("false positives@", "300_abs") == (2, 178)
