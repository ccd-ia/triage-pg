"""``triage model show``: the calibration table follows the problem type and names ties.

The ``group`` fixture's model A is a classifier evaluated on D1: 200 scored rows, about 140
of them labeled. Messages are matched without ANSI codes and with whitespace collapsed.
"""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

import triage.cli as cli
from tests.test_readers import D1, Group, group  # noqa: F401 — group is a fixture

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    return " ".join(_ANSI.sub("", output).split())


@pytest.fixture
def show(db_url, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no database.yaml or .env here: DATABASE_URL wins

    def run(*args: str):
        result = CliRunner().invoke(
            cli.app,
            ["model", "show", *args],
            env={"DATABASE_URL": db_url, "COLUMNS": "120"},
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        return result.output

    return run


def _json(output: str):
    return json.loads(output[output.index("{") :])


def test_a_classifier_shows_the_realized_rate_and_the_new_keys(show, group):  # noqa: F811
    payload = _json(show(str(group.model_a), "--json"))
    assert payload["card"]["problem_type"] == "classification"
    assert set(payload["calibration"][0]) == {
        "decile",
        "n",
        "avg_score",
        "mean_outcome",
        "lift",
        "realized_rate",
        "n_distinct_scores",
    }
    assert payload["calibration_ties"]["n"] == 200

    text = _plain(show(str(group.model_a)))
    assert f"Calibration @ {D1}" in text
    assert "Decile n Mean score Mean outcome Lift Realized" in text
    # about 140 labeled rows over 10 buckets is under 20 a bucket
    assert "try --bins 5" in text
    assert "try --bins 5" not in _plain(show(str(group.model_a), "--bins", "5"))
    assert "tied rows are kept in one decile" not in text


def test_a_regression_model_drops_the_realized_rate_and_names_the_tie(
    show,
    group: Group,  # noqa: F811
    db_pool_greenfield,
):
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "update triage.experiments set problem_type = 'regression'"
            " where experiment_hash = 'exp-a'"
        )
        # 170 of D1's 200 rows get one forecast
        conn.execute(
            "update triage.predictions set score = 15.9"
            " where model_id = %(m)s and as_of_date = %(d)s and entity_id <= 170",
            {"m": group.model_a, "d": D1},
        )

    payload = _json(show(str(group.model_a), "--json"))
    assert all(r["realized_rate"] is None for r in payload["calibration"])
    assert payload["calibration_ties"]["largest_tie_share"] == pytest.approx(0.85)

    text = _plain(show(str(group.model_a)))
    assert "Decile n Mean score Mean outcome Lift" in text
    assert "Realized" not in text
    assert "85% of rows share score 15.9 — tied rows are kept in one decile" in text


def test_a_ranking_model_shows_its_deciles_with_lift_above_the_evaluations(
    show,
    db_pool_greenfield,
):
    from tests.test_allocation_metrics import R1, seed_allocation

    model_id = seed_allocation(db_pool_greenfield)["noisy"]
    # read R1: the latest evaluated date (R0) is all zeros
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "delete from triage.evaluations where model_id = %(m)s and as_of_date > %(d)s",
            {"m": model_id, "d": R1},
        )
    text = _plain(show(str(model_id)))
    assert f"Deciles with lift @ {R1}" in text
    assert text.index("Deciles with lift") < text.index("Evaluations (test window)")
    for metric in ("lift@", "captured@", "aucc", "spearman"):
        assert metric in text, metric
