"""``triage check metrics`` and the regression metrics it audits (migration 0030).

Two fixtures. The classifier is ``test_readers``' model A: 200 scored rows on D1, about 140
labeled, evaluated at four top-k cuts over everyone and a Subset, plus the rank-free and
probability metrics added here. The regression fixture holds a DummyRegressor forecasting the
0.9 quantile and a point model, scored on one date with a varied target and one whose target
is all zeros (r2 and wape undefined).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date

import numpy as np
import pytest
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import GradientBoostingRegressor, HistGradientBoostingRegressor
from sklearn.linear_model import LinearRegression, QuantileRegressor
from typer.testing import CliRunner

import triage.cli as cli
from tests.test_readers import D1, LABEL_TIMESPAN, Group, group  # noqa: F401 — fixture
from triage.adapters.model import declared_quantile, quantile_of
from triage.adapters.run import _resolve_metric_config
from triage.component.catwalk.in_pg_evaluation import (
    DEFAULT_REGRESSION_CONFIG,
    DEFAULT_REGRESSION_RANKING_CONFIG,
    evaluate_in_db,
)
from triage.component.results_schema import downgrade_db, upgrade_db
from triage.diagnostics import metric_audit
from triage.diagnostics.readers import NotFoundError

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
R1, R0 = date(2015, 1, 1), date(2015, 2, 1)  # varied target; all-zero target
RNG = np.random.default_rng(7)
OUTCOMES = RNG.gamma(2.0, 50.0, 40).round(2)
POINT = (OUTCOMES + RNG.normal(0, 20, 40)).round(2)
UPPER = np.full(40, float(np.quantile(OUTCOMES, 0.9)))


def _plain(output: str) -> str:
    return " ".join(_ANSI.sub("", output).replace("│", " ").split())


# ------------------------------------------------------------------------------ seeding


def _seed_regression(pool) -> dict[str, int]:
    """Two models of a regression experiment, scored on R1 and R0 from one matrix."""
    matrix_uuid = str(uuid.uuid4())
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values ('exp-reg', '{}'::jsonb, 'regression')"
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile) values ('exp-reg', 'local')"
            " returning run_id"
        ).fetchone()["run_id"]
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) values"
            " ('reg-labels', 'reg-labels', 'labels', '{}'::jsonb),"
            " ('reg-matrix', 'reg-matrix', 'matrix', '{}'::jsonb),"
            " ('reg-q90', 'reg-q90', 'model', '{}'::jsonb),"
            " ('reg-point', 'reg-point', 'model', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.artifact_inputs (artifact_id, parent_id)"
            " values ('reg-matrix', 'reg-labels')"
        )
        conn.execute(
            "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind,"
            " storage_uri, label_timespan) values (%(u)s, 'reg-matrix', 'test', 'mem://r',"
            " cast(%(ts)s as interval))",
            {"u": matrix_uuid, "ts": LABEL_TIMESPAN},
        )
        ids = {}
        for name, model_type, hp, quantile in (
            ("q90", "sklearn.dummy.DummyRegressor",
             {"strategy": "quantile", "quantile": 0.9}, 0.9),
            ("point", "sklearn.linear_model.LinearRegression", {}, None),
        ):  # fmt: skip
            gid = conn.execute(
                "insert into triage.model_groups"
                " (model_group_hash, model_type, hyperparameters, feature_list)"
                " values (%(h)s, %(t)s, %(hp)s::jsonb, array['f']) returning model_group_id",
                {"h": f"mg-{name}", "t": model_type, "hp": json.dumps(hp)},
            ).fetchone()["model_group_id"]
            ids[name] = conn.execute(
                "insert into triage.models (model_group_id, model_hash, run_id,"
                " train_end_time, training_label_timespan, forecast_quantile)"
                " values (%(g)s, %(a)s, %(r)s, date '2014-12-01',"
                " cast(%(ts)s as interval), %(q)s) returning model_id",
                {"g": gid, "a": f"reg-{name}", "r": run_id, "ts": LABEL_TIMESPAN,
                 "q": quantile},
            ).fetchone()["model_id"]  # fmt: skip
        with conn.cursor() as cur:
            for as_of, outcomes in ((R1, OUTCOMES), (R0, np.zeros(40))):
                cur.executemany(
                    "insert into triage.labels (label_hash, entity_id, as_of_date,"
                    " label_timespan, outcome) values ('reg-labels', %(e)s, %(d)s,"
                    " cast(%(ts)s as interval), %(o)s)",
                    [
                        {"e": e, "d": as_of, "ts": LABEL_TIMESPAN, "o": float(o)}
                        for e, o in enumerate(outcomes, start=1)
                    ],
                )
                for name, scores in (("q90", UPPER), ("point", POINT)):
                    cur.executemany(
                        "insert into triage.predictions (model_id, entity_id, as_of_date,"
                        " split_kind, score, matrix_uuid)"
                        " values (%(m)s, %(e)s, %(d)s, 'test', %(s)s, %(u)s)",
                        [
                            {"m": ids[name], "e": e, "d": as_of, "s": float(s),
                             "u": matrix_uuid}
                            for e, s in enumerate(scores, start=1)
                        ],
                    )  # fmt: skip
    config = {
        "regression_metrics": [
            *DEFAULT_REGRESSION_CONFIG["regression_metrics"],
            "pinball@0.9",
        ]
    }
    for model_id in ids.values():
        for as_of in (R1, R0):
            evaluate_in_db(pool, model_id, as_of, LABEL_TIMESPAN, metric_config=config)
    return ids


def _value(pool, model_id: int, metric: str, as_of: date = R1):
    with pool.connection() as conn:
        row = conn.execute(
            "select value, num_labeled from triage.evaluations where model_id = %(m)s"
            " and metric = %(metric)s and as_of_date = %(d)s and subset_hash = ''",
            {"m": model_id, "metric": metric, "d": as_of},
        ).fetchone()
    assert row is not None, (model_id, metric, as_of)
    return row["value"]


def _evaluate_classifier_extras(pool, model_id: int) -> None:
    evaluate_in_db(
        pool,
        model_id,
        D1,
        LABEL_TIMESPAN,
        metric_config={
            "metrics": ["auc_roc", "average_precision", "brier", "log_loss", "ece@10"],
            "thresholds": [],
        },
    )


# ---------------------------------------------------------------------- the quantile record


@pytest.mark.parametrize(
    ("estimator", "expected"),
    [
        (DummyRegressor(strategy="quantile", quantile=0.9), 0.9),
        (DummyRegressor(strategy="median"), 0.5),
        (DummyRegressor(), None),
        (GradientBoostingRegressor(loss="quantile"), 0.9),
        (GradientBoostingRegressor(), None),
        (HistGradientBoostingRegressor(loss="quantile", quantile=0.3), 0.3),
        (QuantileRegressor(), 0.5),
        (LinearRegression(), None),
    ],
)
def test_declared_quantile_reads_the_estimator(estimator, expected):
    assert declared_quantile(estimator) == expected


def test_an_estimator_attribute_declares_its_quantile():
    class Croston:
        forecast_quantile = 0.8

    assert declared_quantile(Croston()) == 0.8
    assert quantile_of("LGBMRegressor", {"objective": "quantile"}) == 0.9
    assert quantile_of("LGBMRegressor", {"objective": "regression"}) is None


def test_the_backfill_reads_the_same_table(db_pool_greenfield, db_url):
    """Models that predate 0030 get forecast_quantile from their group's hyperparameters."""
    pool = db_pool_greenfield
    downgrade_db(dburl=db_url, revision="0029_typed_calibration")
    cases = [
        ("sklearn.dummy.DummyRegressor", {"strategy": "quantile", "quantile": 0.75}),
        ("sklearn.dummy.DummyRegressor", {"strategy": "median"}),
        ("sklearn.dummy.DummyRegressor", {"strategy": "mean"}),
        ("sklearn.ensemble.GradientBoostingRegressor", {"loss": "quantile"}),
        ("sklearn.ensemble.HistGradientBoostingRegressor",
         {"loss": "quantile", "quantile": 0.2}),
        ("sklearn.linear_model.QuantileRegressor", {}),
        ("lightgbm.LGBMRegressor", {"objective": "quantile", "alpha": 0.95}),
        ("sklearn.linear_model.LinearRegression", {}),
    ]  # fmt: skip
    with pool.connection() as conn:
        for i, (model_type, hp) in enumerate(cases):
            conn.execute(
                "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
                " values (%(a)s, %(a)s, 'model', '{}'::jsonb)",
                {"a": f"bf-{i}"},
            )
            conn.execute(
                "with g as (insert into triage.model_groups"
                " (model_group_hash, model_type, hyperparameters, feature_list)"
                " values (%(h)s, %(t)s, %(hp)s::jsonb, array['f']) returning model_group_id)"
                " insert into triage.models (model_group_id, model_hash, train_end_time)"
                " select model_group_id, %(a)s, date '2014-01-01' from g",
                {"h": f"bf-{i}", "t": model_type, "hp": json.dumps(hp), "a": f"bf-{i}"},
            )
    upgrade_db(dburl=db_url, revision="head")
    with pool.connection() as conn:
        got = {
            r["model_hash"]: r["forecast_quantile"]
            for r in conn.execute(
                "select model_hash, forecast_quantile from triage.models"
            ).fetchall()
        }
    for i, (model_type, hp) in enumerate(cases):
        assert got[f"bf-{i}"] == quantile_of(model_type.rsplit(".", 1)[-1], hp), cases[
            i
        ]


# --------------------------------------------------------------------- wape and pinball


def test_pinball_is_scored_only_for_the_quantile_it_forecasts(db_pool_greenfield):
    pool = db_pool_greenfield
    ids = _seed_regression(pool)
    assert _value(pool, ids["q90"], "pinball@0.9") is not None
    for tau in ("0.5", "0.8", "0.95"):
        assert _value(pool, ids["q90"], f"pinball@{tau}") is None
    for tau in ("0.5", "0.8", "0.9", "0.95"):
        assert _value(pool, ids["point"], f"pinball@{tau}") is None


def test_wape_matches_numpy_and_is_undefined_on_a_zero_target(db_pool_greenfield):
    pool = db_pool_greenfield
    ids = _seed_regression(pool)
    expected = np.abs(POINT - OUTCOMES).sum() / np.abs(OUTCOMES).sum()
    assert _value(pool, ids["point"], "wape") == pytest.approx(expected, abs=1e-12)
    assert _value(pool, ids["point"], "wape", R0) is None
    assert _value(pool, ids["point"], "r2", R0) is None  # a constant target
    with pool.connection() as conn:
        assert (
            conn.execute("select triage.higher_is_better('wape') as h").fetchone()["h"]
            is False
        )


def test_the_regression_defaults():
    assert "wape" in DEFAULT_REGRESSION_CONFIG["regression_metrics"]
    assert _resolve_metric_config({}, "regression", None) == DEFAULT_REGRESSION_CONFIG
    assert (
        _resolve_metric_config({}, "regression_ranking", None)
        == DEFAULT_REGRESSION_RANKING_CONFIG
    )


# ---------------------------------------------------------------------------- the audit


def test_every_stored_regression_metric_matches(db_pool_greenfield):
    pool = db_pool_greenfield
    ids = _seed_regression(pool)
    report = metric_audit.audit(pool, list(ids.values()))
    assert report.passed, report.to_dict()
    names = {m.metric for m in report.metrics}
    assert {"rmse", "mae", "r2", "wape", "pinball@0.9", "pinball@0.5"} <= names
    assert all(m.max_gap <= 1e-9 for m in report.metrics)
    assert report.not_audited == []


def test_every_stored_classifier_metric_matches(db_pool_greenfield, group: Group):  # noqa: F811
    pool = db_pool_greenfield
    _evaluate_classifier_extras(pool, group.model_a)
    report = metric_audit.audit(pool, [group.model_a])
    assert report.passed, report.to_dict()
    by_name = {m.metric: m for m in report.metrics}
    assert set(by_name) == {
        "precision@",
        "recall@",
        "auc_roc",
        "average_precision",
        "brier",
        "log_loss",
        "ece@10",
    }
    # four cuts over everyone and the Subset
    assert by_name["precision@"].checked == 8


def test_a_corrupted_row_is_caught(db_pool_greenfield, group: Group):  # noqa: F811
    pool = db_pool_greenfield
    with pool.connection() as conn:
        conn.execute(
            "update triage.evaluations set value = value + 0.01"
            " where model_id = %(m)s and metric = 'precision@' and parameter = '10_pct'"
            "   and subset_hash = ''",
            {"m": group.model_a},
        )
        conn.execute(
            "insert into triage.evaluations (model_id, split_kind, as_of_date, metric,"
            " parameter, value) values (%(m)s, 'test', %(d)s, 'my_loss', '', 1.0)",
            {"m": group.model_a, "d": D1},
        )
    report = metric_audit.audit(pool, [group.model_a])
    assert not report.passed
    precision = next(m for m in report.metrics if m.metric == "precision@")
    assert precision.failed == 1
    assert precision.worst is not None and precision.worst.parameter == "10_pct"
    assert precision.max_gap == pytest.approx(0.01)
    assert [(n.metric, n.rows) for n in report.not_audited] == [("my_loss", 1)]


def test_a_date_without_matrix_lineage_is_not_audited(db_pool_greenfield, group: Group):  # noqa: F811
    pool = db_pool_greenfield
    with pool.connection() as conn:
        conn.execute(
            "update triage.predictions set matrix_uuid = null where model_id = %(m)s",
            {"m": group.model_a},
        )
    report = metric_audit.audit(pool, [group.model_a])
    assert report.checked == 0 and not report.passed
    assert all("no matrix lineage" in n.reason for n in report.not_audited)


def test_the_scope(db_pool_greenfield, group: Group):  # noqa: F811
    pool = db_pool_greenfield
    assert metric_audit.models_in_scope(pool, model=group.model_a) == [group.model_a]
    assert metric_audit.models_in_scope(pool, group=group.model_group_id) == sorted(
        [group.model_a, group.model_b]
    )
    assert metric_audit.models_in_scope(pool, experiment="exp-a") == [group.model_a]
    with pytest.raises(ValueError, match="matches 2 experiments"):
        metric_audit.models_in_scope(pool, experiment="exp-")
    with pytest.raises(NotFoundError):
        metric_audit.models_in_scope(pool, model=999999)
    with pytest.raises(ValueError, match="exactly one"):
        metric_audit.models_in_scope(pool, model=group.model_a, group=1)


# ------------------------------------------------------------------------------ the CLI


@pytest.fixture
def check(db_url, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no database.yaml or .env here: DATABASE_URL wins

    def run(*args: str):
        result = CliRunner().invoke(
            cli.app,
            ["check", "metrics", *args],
            env={"DATABASE_URL": db_url, "COLUMNS": "160"},
            catch_exceptions=False,
        )
        return result.exit_code, _plain(result.output)

    return run


def test_the_cli_exit_codes(check, db_pool_greenfield, group: Group, tmp_path):  # noqa: F811
    code, text = check("--model", str(group.model_a), "--json", "audit.json")
    assert code == 0, text
    assert "match" in text and "precision@" in text
    payload = json.loads((tmp_path / "audit.json").read_text())
    assert payload["passed"] is True and payload["checked"] == payload["metrics"][0][
        "checked"
    ] + sum(m["checked"] for m in payload["metrics"][1:])

    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "update triage.evaluations set value = 0.5 where model_id = %(m)s"
            " and metric = 'recall@' and parameter = '1_abs' and subset_hash = ''",
            {"m": group.model_a},
        )
    code, text = check("--model", str(group.model_a))
    assert code == 1, text
    assert "differ by more than 1e-09" in text and "1_abs" in text

    for args, message in (
        ((), "exactly one of --model, --group, --experiment"),
        (("--model", "999999"), "model 999999 not found"),
        (("--model", "1", "--group", "1"), "exactly one"),
        (("--experiment", "nowhere"), "experiment nowhere not found"),
        (("--model", "1", "--tolerance", "-1"), "--tolerance must be 0 or more"),
    ):
        code, text = check(*args)
        assert code == 2, (args, text)
        assert message in text, (args, text)


def test_average_precision_on_tied_scores_matches_the_audit(
    db_pool_greenfield,
    group: Group,  # noqa: F811
):
    """Migration 0032: a tie block is one threshold in SQL as in sklearn."""
    pool = db_pool_greenfield
    with pool.connection() as conn:
        conn.execute("update triage.predictions set score = round(score::numeric, 1)")
    evaluate_in_db(
        pool,
        group.model_a,
        D1,
        LABEL_TIMESPAN,
        metric_config={"metrics": ["average_precision", "auc_roc"], "thresholds": []},
    )
    report = metric_audit.audit(pool, [group.model_a])
    by_name = {m.metric: m for m in report.metrics}
    assert by_name["average_precision"].failed == 0
    assert by_name["average_precision"].max_gap <= 1e-9
