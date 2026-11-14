"""``triage check history``: gaps and off-grid periods in the target-history series.

Exit codes are the contract: 0 clean, 1 gaps or off-grid rows found, 2 the check could not
run. The series lives in a small events table; the as_of_date is given with ``--as-of`` so
no temporal_config has to be planned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from triage import cli

runner = CliRunner()

COMPLETE = """
with months as (
  select e.entity_id, m::date as period
  from (select distinct entity_id from hist_events) e
  cross join generate_series(date '2014-07-01', {as_of_date}::date - interval '1 month',
                             interval '1 month') m
)
select months.entity_id, months.period, count(h.entity_id) as value
from months
left join hist_events h
  on h.entity_id = months.entity_id and date_trunc('month', h.event_date)::date = months.period
 and h.event_date < {as_of_date}
group by 1, 2
"""

SPARSE = (
    "select entity_id, date_trunc('month', event_date)::date as period, count(*) as value"
    " from hist_events where event_date < {as_of_date} group by 1, 2"
)

# one day after each month start: every period is off the monthly grid
MISALIGNED = (
    "select entity_id, (date_trunc('month', event_date) + interval '1 day')::date as period,"
    " count(*) as value from hist_events where event_date < {as_of_date} group by 1, 2"
)


@pytest.fixture(name="seeded")
def fixture_seeded(db_url, db_pool):
    with db_pool.connection() as conn:
        conn.execute("create table hist_events (entity_id bigint, event_date date)")
        # entity 1: July, August and October 2014 — September is missing
        # entity 2: every month July to December
        conn.execute(
            "insert into hist_events values"
            " (1, date '2014-07-10'), (1, date '2014-08-10'), (1, date '2014-10-10'),"
            " (1, date '2014-12-10'),"
            " (2, date '2014-07-05'), (2, date '2014-08-05'), (2, date '2014-09-05'),"
            " (2, date '2014-10-05'), (2, date '2014-11-05'), (2, date '2014-12-05')"
        )
    return db_url


def _run(tmp_path: Path, monkeypatch, db_url: str, config: dict[str, Any]):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "experiment.yaml"
    path.write_text(yaml.safe_dump(config))
    report_path = tmp_path / "report.json"
    result = runner.invoke(
        cli.app,
        [
            "check",
            "history",
            str(path),
            "--as-of",
            "2015-01-01",
            "--json",
            str(report_path),
        ],
        env={"DATABASE_URL": db_url},
    )
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    return result, report


def _config(query: str, **extra: Any) -> dict[str, Any]:
    return {
        "history_query": query,
        "history_period": "1 month",
        "history_series_width": 6,
        **extra,
    }


def test_complete_series_exits_0(seeded, tmp_path, monkeypatch):
    result, report = _run(tmp_path, monkeypatch, seeded, _config(COMPLETE))
    assert result.exit_code == 0, result.output
    assert report["n_series"] == 2
    assert report["n_series_with_gaps"] == 0


def test_sparse_series_exits_1_and_names_the_entity(seeded, tmp_path, monkeypatch):
    result, report = _run(tmp_path, monkeypatch, seeded, _config(SPARSE))
    assert result.exit_code == 1, result.output
    assert report["n_series_with_gaps"] == 1
    assert report["n_gaps"] == 2  # September and November for entity 1
    assert report["worst_entities"][0]["entity_id"] == 1
    assert "fill_missing_periods: zero" in " ".join(result.output.split())


def test_misaligned_periods_exit_1(seeded, tmp_path, monkeypatch):
    result, report = _run(tmp_path, monkeypatch, seeded, _config(MISALIGNED))
    assert result.exit_code == 1, result.output
    assert report["n_misaligned"] > 0
    assert report["misaligned_examples"][0]["problem"] == "off the grid"


def test_missing_history_period_exits_2(seeded, tmp_path, monkeypatch):
    config = _config(COMPLETE)
    del config["history_period"]
    result, _ = _run(tmp_path, monkeypatch, seeded, config)
    assert result.exit_code == 2, result.output
    assert "history_period" in result.output


def test_no_history_query_exits_2(seeded, tmp_path, monkeypatch):
    result, _ = _run(tmp_path, monkeypatch, seeded, {"history_period": "1 month"})
    assert result.exit_code == 2, result.output
