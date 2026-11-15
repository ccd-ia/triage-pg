"""The DirtyDuck regression example's raw history is in the label's units.

The raw-series baselines (seasonal naive, Holt-Winters, Croston) forecast one
``history_period`` ahead of the ``history_query`` series. That forecast is a floor for the
label only when one period of the series is the label's quantity over the label's window:
the same violations, summed over the same length of time. These tests run the example's own
``history_query`` and ``label_config.query`` on a small seeded ``ontology.events`` table.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from triage import cli
from triage.adapters.labels import render_label_query
from triage.adapters.target_history import (
    build_target_history_series,
    history_columns_in_order,
    pivot_series_to_history_columns,
)
from triage.component.catwalk.baselines.timeseries import SeasonalNaive
from triage.util.conf import convert_str_to_relativedelta

EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "example"
    / "dirtyduck"
    / "experiment-regression.yaml"
)
SEASONAL_NAIVE = "triage.component.catwalk.baselines.timeseries.SeasonalNaive"
AS_OF = date(2016, 1, 1)

# (entity_id, inspection date, number of violations; None is a pass with no violations list)
EVENTS: list[tuple[int, date, int | None]] = [
    (1, date(2014, 2, 10), 3),
    (1, date(2014, 3, 5), None),
    (1, date(2014, 9, 15), 2),
    (1, date(2015, 2, 20), 4),
    (1, date(2015, 4, 2), 1),
    (1, date(2015, 11, 30), 5),
    (1, date(2016, 1, 1), 7),  # on the as_of_date: in its label, never in its history
    (2, date(2014, 5, 1), 1),
    (2, date(2015, 8, 3), 2),
    (2, date(2015, 8, 24), 3),  # two inspections in one month
    (3, date(2015, 12, 31), 6),  # first seen the day before the as_of_date
    (3, date(2016, 3, 1), 2),
]


def _config() -> dict[str, Any]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture(name="events_pool")
def fixture_events_pool(db_pool):
    """``ontology.events`` with the columns the example's history and label SQL read."""
    with db_pool.connection() as conn:
        conn.execute("create schema ontology")
        conn.execute(
            "create table ontology.events (entity_id bigint, date date, violations jsonb)"
        )
        for entity_id, day, n_violations in EVENTS:
            violations = (
                None
                if n_violations is None
                else json.dumps([f"v{i}" for i in range(n_violations)])
            )
            conn.execute(
                "insert into ontology.events values (%(e)s, %(d)s, %(v)s::jsonb)",
                {"e": entity_id, "d": day, "v": violations},
            )
    return db_pool


def _labels_at(pool, config: dict[str, Any], as_of_date: date) -> dict[int, float]:
    """The example's label at one as_of_date; an entity with no inspection has no row."""
    timespan = config["temporal_config"]["test_label_timespans"][0]
    sql = render_label_query(config["label_config"]["query"], as_of_date, timespan)
    with pool.connection() as conn:
        rows = conn.execute(sql).fetchall()
    return {r["entity_id"]: r["outcome"] for r in rows}


def test_one_history_period_is_one_label_window():
    """A one-step forecast covers ``history_period``; the label covers its timespan."""
    config = _config()
    period = convert_str_to_relativedelta(config["history_period"])
    temporal = config["temporal_config"]
    timespans = temporal["training_label_timespans"] + temporal["test_label_timespans"]

    assert {convert_str_to_relativedelta(t) for t in timespans} == {period}


def test_each_history_value_is_the_label_at_its_period(events_pool):
    """The value for period ``p`` is the label the facility would have had at ``p``.

    A period with no inspection is 0 violations, where the label has no row.
    """
    config = _config()
    series = build_target_history_series(events_pool, config["history_query"], [AS_OF])

    assert series.height > 0
    for row in series.to_dicts():
        labels = _labels_at(events_pool, config, row["period"])
        expected = labels.get(row["entity_id"], 0.0)
        assert row["value"] == expected, row


def test_seasonal_naive_forecasts_the_label_one_year_earlier(events_pool):
    """With the example's ``season``, seasonal naive returns last year's label."""
    config = _config()
    (season,) = config["grid_config"][SEASONAL_NAIVE]["season"]
    series = build_target_history_series(events_pool, config["history_query"], [AS_OF])
    pivot = pivot_series_to_history_columns(
        series, config["history_series_width"], config["history_period"]
    )
    frame = pivot.frame.sort("entity_id")
    columns = history_columns_in_order(frame.columns, "series")

    forecasts = SeasonalNaive(season=season).predict(frame.select(columns).to_numpy())

    a_year_earlier = date(AS_OF.year - 1, AS_OF.month, AS_OF.day)
    labels = _labels_at(events_pool, config, a_year_earlier)
    expected = [labels.get(e, 0.0) for e in frame.get_column("entity_id").to_list()]
    assert forecasts.tolist() == expected
    assert labels[1] == 5.0  # entity 1: 4 + 1 violations in the first half of 2015


@pytest.mark.usefixtures("events_pool")
def test_check_history_finds_no_gap_in_the_example(db_url, tmp_path, monkeypatch):
    """Every period is returned, zeros included, so the complete-calendar baselines run."""
    monkeypatch.chdir(tmp_path)  # no database.yaml from the working directory
    report_path = tmp_path / "report.json"
    result = CliRunner().invoke(
        cli.app,
        [
            "check",
            "history",
            str(EXAMPLE),
            "--as-of",
            AS_OF.isoformat(),
            "--json",
            str(report_path),
        ],
        env={"DATABASE_URL": db_url},
    )

    assert result.exit_code == 0, result.output
    report = json.loads(report_path.read_text())
    assert report["n_series"] == 3
    assert report["n_series_with_gaps"] == 0
