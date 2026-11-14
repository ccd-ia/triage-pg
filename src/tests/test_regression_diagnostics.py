"""Regression residuals, history segments and forecast structure (migration 0033).

Thirteen entities with hand-made monthly label histories, one regression model scoring three
test dates. Each history is one of the Syntetos–Boylan shapes, or an edge: no demand, a single
demand month, gaps (outside the cohort, not zeros) and a cold start with no prior label. The
last test date has Σy = 0, and one entity gets a negative forecast. Every number the SQL
returns is checked against a computation in Python over the same rows.
"""

from __future__ import annotations

import json
import re
import statistics
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from typing import Any

import numpy as np
import pytest
from dateutil.relativedelta import relativedelta
from typer.testing import CliRunner

import triage.cli as cli
from triage.diagnostics import readers

EXP = "exp-residuals"
TS = "1 month"
HISTORY_START = date(2014, 2, 1)  # 12 months, 2014-02 … 2015-01
TEST_DATES = (date(2015, 2, 1), date(2015, 3, 1), date(2015, 4, 1))
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

#: entity → its label in each of the 12 history months; None = outside the cohort that month.
HISTORY: dict[int, Sequence[float | None]] = {
    1: [5.0] * 12,  # smooth
    2: [8, 9, 10, 11] * 3,  # smooth
    3: [1, 40] * 6,  # erratic
    4: [2, 30, 5, 60, 1, 20] * 2,  # erratic
    5: [0, 0, 20] * 4,  # intermittent
    6: [0, 15] * 6,  # intermittent
    7: [0, 0, 1, 0, 0, 100] * 2,  # lumpy
    8: [0, 50, 0, 0, 2, 0] * 2,  # lumpy
    9: [0] * 12,  # no demand
    10: [0] * 11 + [7],  # one demand period
    11: [None, None, 3, None, 3, 3, None, 3, 3, 3, None, 3],  # gaps, all positive
    12: [None, 0, 6, None, 0, 6, None, 0, 6, None, 0, 6],  # gaps and zeros
    13: [None] * 12,  # cold start
}
ENTITIES = sorted(HISTORY)


def _outcome(e: int, d: date) -> float:
    """The label on a test date: Σy = 0 on the last one."""
    if d == TEST_DATES[2]:
        return 0.0
    return float(e) * (1 if d == TEST_DATES[0] else 2)


def _forecast(e: int, d: date) -> float:
    if e == 9:
        return -1.0  # a negative forecast
    return _outcome(e, d) + 0.5 * (e % 4) + 0.25 + (0.5 if d == TEST_DATES[1] else 0.0)


def _labels() -> dict[tuple[int, date], float]:
    """Every label of the fixture: the history months, then the test dates."""
    out: dict[tuple[int, date], float] = {}
    for e, values in HISTORY.items():
        for k, v in enumerate(values):
            if v is not None:
                out[(e, HISTORY_START + relativedelta(months=k))] = float(v)
    for e in ENTITIES:
        for d in TEST_DATES:
            out[(e, d)] = _outcome(e, d)
    return out


def _seed(pool, problem_type: str = "regression", exp: str = EXP) -> int:
    """One model of ``problem_type`` scoring the three test dates; returns its model_id."""
    tag = exp
    matrix_uuid = str(uuid.uuid4())
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values (%(h)s, '{}'::jsonb, %(t)s)",
            {"h": exp, "t": problem_type},
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile) values (%(h)s, 'local')"
            " returning run_id",
            {"h": exp},
        ).fetchone()["run_id"]
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) values"
            " (%(l)s, %(l)s, 'labels', '{}'::jsonb), (%(x)s, %(x)s, 'matrix', '{}'::jsonb),"
            " (%(m)s, %(m)s, 'model', '{}'::jsonb)",
            {"l": f"{tag}-labels", "x": f"{tag}-matrix", "m": f"{tag}-model"},
        )
        conn.execute(
            "insert into triage.artifact_inputs (artifact_id, parent_id)"
            " values (%(x)s, %(l)s)",
            {"l": f"{tag}-labels", "x": f"{tag}-matrix"},
        )
        conn.execute(
            "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind,"
            " storage_uri, label_timespan) values (%(u)s, %(x)s, 'test', 'mem://r',"
            " cast(%(ts)s as interval))",
            {"u": matrix_uuid, "x": f"{tag}-matrix", "ts": TS},
        )
        with conn.cursor() as cur:
            cur.executemany(
                "insert into triage.labels (label_hash, entity_id, as_of_date,"
                " label_timespan, outcome) values (%(l)s, %(e)s, %(d)s,"
                " cast(%(ts)s as interval), %(o)s)",
                [
                    {"l": f"{tag}-labels", "e": e, "d": d, "ts": TS, "o": y}
                    for (e, d), y in _labels().items()
                ],
            )
        gid = conn.execute(
            "insert into triage.model_groups"
            " (model_group_hash, model_type, hyperparameters, feature_list)"
            " values (%(h)s, 'sklearn.dummy.DummyRegressor', '{}'::jsonb, array['f'])"
            " returning model_group_id",
            {"h": f"{tag}-group"},
        ).fetchone()["model_group_id"]
        model_id = conn.execute(
            "insert into triage.models (model_group_id, model_hash, run_id,"
            " train_end_time, training_label_timespan)"
            " values (%(g)s, %(a)s, %(r)s, date '2015-01-01', cast(%(ts)s as interval))"
            " returning model_id",
            {"g": gid, "a": f"{tag}-model", "r": run_id, "ts": TS},
        ).fetchone()["model_id"]
        with conn.cursor() as cur:
            cur.executemany(
                "insert into triage.predictions (model_id, entity_id, as_of_date,"
                " split_kind, score, matrix_uuid)"
                " values (%(m)s, %(e)s, %(d)s, 'test', %(s)s, %(u)s)",
                [
                    {
                        "m": model_id,
                        "e": e,
                        "d": d,
                        "s": _forecast(e, d),
                        "u": matrix_uuid,
                    }
                    for e in ENTITIES
                    for d in TEST_DATES
                ],
            )
    return model_id


# ------------------------------------------------------------------ the Python reference


def _history(e: int, as_of: date, n_history: int = 12) -> list[float]:
    """The entity's labels on the n most recent label dates whose window closed by as_of."""
    labels = _labels()
    grid = sorted(
        {d for (_, d) in labels if d + relativedelta(months=1) <= as_of}, reverse=True
    )[:n_history]
    return [labels[(e, g)] for g in grid if (e, g) in labels]


def meio_sbc_class(values: list[float]) -> str:
    """meio's ``sbc_class`` (demand_patterns.sql), with the plan's three edge classes."""
    if not values:
        return "no history"
    nonzero = [v for v in values if v > 0]
    if not nonzero:
        return "no demand"
    if len(nonzero) == 1:
        return "one demand period"
    adi = len(values) / len(nonzero)
    cv2 = (statistics.stdev(nonzero) / statistics.mean(nonzero)) ** 2
    if adi < 1.32 and cv2 < 0.49:
        return "smooth"
    if adi < 1.32:
        return "erratic"
    if cv2 < 0.49:
        return "intermittent"
    return "lumpy"


def _band(mean: float | None) -> str:
    if mean is None:
        return "no history"
    if mean <= 0:
        return "<= 0"
    for lo, hi in ((0, 10), (10, 100), (100, 1000)):
        if lo < mean <= hi:
            return f"({lo}, {hi}]"
    return "> 1000"


def _rows() -> list[dict[str, Any]]:
    """Every labeled scored row, with its history segments."""
    out = []
    for d in TEST_DATES:
        for e in ENTITIES:
            hist = _history(e, d)
            mean = statistics.mean(hist) if hist else None
            out.append(
                {
                    "e": e,
                    "d": d,
                    "y": _outcome(e, d),
                    "f": _forecast(e, d),
                    "band": _band(mean),
                    "sbc": meio_sbc_class(hist),
                    "cold": "cold start" if not hist else "with history",
                }
            )
    return out


# ------------------------------------------------------------------ the SQL


def test_prior_label_history_matches_the_python_reference(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool)
    with pool.connection() as conn:
        got = conn.execute(
            "select * from triage.prior_label_history(%(m)s)", {"m": model_id}
        ).fetchall()
    assert len(got) == len(ENTITIES) * len(TEST_DATES)
    for r in got:
        hist = _history(r["entity_id"], r["as_of_date"])
        assert r["n_grid"] == 12
        assert r["n_observed"] == len(hist)
        assert r["sbc_class"] == meio_sbc_class(hist), r
        assert r["cold_start"] is (not hist)
        if not hist:
            assert r["mean_value"] is None and r["band"] == "no history"
            assert r["band_ix"] is None
            continue
        nonzero = [v for v in hist if v > 0]
        assert r["mean_value"] == pytest.approx(np.mean(hist))
        assert r["zero_share"] == pytest.approx(hist.count(0.0) / len(hist))
        assert r["band"] == _band(float(np.mean(hist))), r
        if nonzero:
            assert r["adi"] == pytest.approx(len(hist) / len(nonzero))
        else:
            assert r["adi"] is None
        if len(nonzero) > 1:
            assert r["cv2"] == pytest.approx(
                (np.std(nonzero, ddof=1) / np.mean(nonzero)) ** 2
            )


def test_the_twelve_series_cover_every_class(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool)
    with pool.connection() as conn:
        got = {
            r["entity_id"]: r["sbc_class"]
            for r in conn.execute(
                "select entity_id, sbc_class from triage.prior_label_history(%(m)s)"
                " where as_of_date = %(d)s",
                {"m": model_id, "d": TEST_DATES[0]},
            ).fetchall()
        }
    assert got == {
        1: "smooth",
        2: "smooth",
        3: "erratic",
        4: "erratic",
        5: "intermittent",
        6: "intermittent",
        7: "lumpy",
        8: "lumpy",
        9: "no demand",
        10: "one demand period",
        11: "smooth",  # a gap is not a zero: ADI stays 1
        12: "intermittent",
        13: "no history",
    }


def test_the_residual_summary_matches_numpy(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool)
    got = readers.residual_summary(pool, model_id)
    assert [r["as_of_date"] for r in got] == list(TEST_DATES)
    for r in got:
        y = np.array([_outcome(e, r["as_of_date"]) for e in ENTITIES])
        f = np.array([_forecast(e, r["as_of_date"]) for e in ENTITIES])
        err = np.abs(f - y)
        assert r["n"] == len(ENTITIES)
        assert r["sum_outcome"] == pytest.approx(y.sum())
        assert r["mae"] == pytest.approx(err.mean())
        assert r["rmse"] == pytest.approx(np.sqrt((err**2).mean()))
        assert r["bias"] == pytest.approx((f - y).mean())
        if y.sum() == 0:
            assert r["wape"] is None  # Σy = 0
        else:
            assert r["wape"] == pytest.approx(err.sum() / np.abs(y).sum())
        assert r["top100_error_share"] == pytest.approx(1.0)  # 13 rows ≤ 100
        top1 = int(np.ceil(0.01 * len(err)))
        assert r["top1pct_error_share"] == pytest.approx(
            np.sort(err)[::-1][:top1].sum() / err.sum()
        )
        assert r["negative_share"] == pytest.approx((f < 0).mean())
        values, counts = np.unique(f, return_counts=True)
        assert r["n_distinct_scores"] == len(values)
        assert r["mode_score"] == pytest.approx(values[np.argmax(counts)])
        assert r["mode_share"] == pytest.approx(counts.max() / len(f))
        assert (r["min_score"], r["max_score"]) == pytest.approx((f.min(), f.max()))
        assert r["coverage_point"] == pytest.approx((y <= f).mean())


@pytest.mark.parametrize(
    "cut,key", [("band", "band"), ("sbc", "sbc"), ("cold-start", "cold")]
)
def test_residuals_by_segment_match_numpy(db_pool_greenfield, cut, key):
    pool = db_pool_greenfield
    model_id = _seed(pool)
    rows = _rows()
    pooled = {
        r["segment"]: r
        for r in readers.residuals_by_segment(pool, model_id, cut, pooled=True)
    }
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[r[key]].append(r)
    assert set(pooled) == set(groups)
    total = sum(abs(r["f"] - r["y"]) for r in rows)
    for segment, members in groups.items():
        got = pooled[segment]
        err = np.array([abs(r["f"] - r["y"]) for r in members])
        y = np.array([r["y"] for r in members])
        assert got["as_of_date"] is None
        assert got["n"] == len(members)
        assert got["entities"] == len({r["e"] for r in members})
        assert got["mae"] == pytest.approx(err.mean())
        assert got["bias"] == pytest.approx(np.mean([r["f"] - r["y"] for r in members]))
        assert got["sum_forecast"] == pytest.approx(sum(r["f"] for r in members))
        assert got["share_of_abs_error"] == pytest.approx(err.sum() / total)
        if y.sum():
            assert got["wape"] == pytest.approx(err.sum() / np.abs(y).sum())
    per_date = readers.residuals_by_segment(pool, model_id, cut, pooled=False)
    for d in TEST_DATES:
        shares = [r["share_of_abs_error"] for r in per_date if r["as_of_date"] == d]
        assert sum(shares) == pytest.approx(1.0)


def test_cold_start_rows_are_reported_apart(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool)
    rows = readers.residuals_by_segment(pool, model_id, "cold-start", pooled=True)
    assert [r["segment"] for r in rows] == ["with history", "cold start"]
    # entity 13 has no label before the first test date; that date's label is history
    # for the next two, so it is a cold start once
    assert rows[1]["n"] == 1 and rows[1]["entities"] == 1


def test_a_subset_is_a_cut(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool)
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.subsets (subset_hash, config)"
            " values ('s-low', '{\"name\": \"low\"}'::jsonb)"
        )
        conn.execute(
            "insert into triage.subset_members (subset_hash, entity_id, as_of_date)"
            " select 's-low', e, d from unnest(array[1, 2, 3]) e,"
            " unnest(%(d)s::date[]) d",
            {"d": list(TEST_DATES)},
        )
    rows = readers.residuals_by_segment(pool, model_id, "subset:low", pooled=True)
    assert {r["segment"]: r["n"] for r in rows} == {
        "in subset": 9,
        "outside subset": 30,
    }
    with pytest.raises(readers.NotFoundError):
        readers.residuals_by_segment(pool, model_id, "subset:nope")
    with pytest.raises(ValueError, match="not a segment cut"):
        readers.residuals_by_segment(pool, model_id, "decile")


def test_largest_errors_are_ordered(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool)
    got = readers.largest_errors(pool, model_id, n=5)
    expected = sorted(_rows(), key=lambda r: (-abs(r["f"] - r["y"]), r["d"], r["e"]))[
        :5
    ]
    assert [(r["entity_id"], r["as_of_date"]) for r in got] == [
        (r["e"], r["d"]) for r in expected
    ]
    assert all(r["error"] == pytest.approx(r["forecast"] - r["outcome"]) for r in got)
    assert all(r["label"] is None for r in got)  # no entity source registered


def test_forecast_actual_is_capped_and_sampled_by_entity_hash(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool)
    full = readers.forecast_actual(pool, model_id)
    assert full["n_total"] == 39 and not full["sampled"] and len(full["pairs"]) == 39
    first = readers.forecast_actual(pool, model_id, cap=9)
    second = readers.forecast_actual(pool, model_id, cap=9)
    assert first == second and first["sampled"] and len(first["pairs"]) == 9
    with pool.connection() as conn:
        order = [
            r["e"]
            for r in conn.execute(
                "select e from unnest(%(e)s::bigint[]) e order by hashint8(e), e",
                {"e": ENTITIES},
            ).fetchall()
        ]
    # three dates per entity: a cap of 9 keeps the first three entities by hash, whole
    assert {r["entity_id"] for r in first["pairs"]} == set(order[:3])


# ------------------------------------------------------------------ the CLI


@pytest.fixture
def run_cli(db_url, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no database.yaml or .env here: DATABASE_URL wins

    def run(*args: str):
        result = CliRunner().invoke(
            cli.app,
            list(args),
            env={"DATABASE_URL": db_url, "COLUMNS": "200"},
            catch_exceptions=False,
        )
        return result.exit_code, " ".join(_ANSI.sub("", result.output).split())

    return run


def test_residuals_cli_default_view(run_cli, db_pool_greenfield):
    model_id = _seed(db_pool_greenfield)
    code, text = run_cli("postmodel", "residuals", str(model_id))
    assert code == 0, text
    for fragment in (
        "Errors per test date",
        "Errors by band",
        "Errors by cold-start",
        "Forecast structure on 2015-04-01:",
        "7.7% negative",
        "Largest errors",
    ):
        assert fragment in text, fragment


def test_residuals_cli_by_and_json(run_cli, db_pool_greenfield):
    model_id = _seed(db_pool_greenfield)
    code, text = run_cli("postmodel", "residuals", str(model_id), "--by", "sbc")
    assert code == 0, text
    assert "Errors by sbc" in text and "lumpy" in text and "Largest errors" not in text
    code, text = run_cli(
        "postmodel", "residuals", str(model_id), "--json", "--quantiles"
    )
    assert code == 0, text
    payload = json.loads(text[text.index("{") :])
    assert payload["forecast_quantile"] is None
    assert len(payload["dates"]) == 3 and len(payload["largest_errors"]) == 20
    assert set(payload["segments"]) == {"band", "cold-start"}
    code, text = run_cli("postmodel", "residuals", str(model_id), "--by", "decile")
    assert code == 2 and "is not a cut" in text


def test_residuals_cli_refuses_a_classifier(run_cli, db_pool_greenfield):
    model_id = _seed(db_pool_greenfield, "classification", "exp-classifier")
    code, text = run_cli("postmodel", "residuals", str(model_id))
    assert code == 2
    assert "reads regression and regression_ranking models" in text
    assert f"triage plot calibration {model_id}" in text


def test_migration_0033_round_trips(db_pool_greenfield, db_url):
    from triage.component.results_schema import downgrade_db, upgrade_db

    model_id = _seed(db_pool_greenfield)
    before = readers.residual_summary(db_pool_greenfield, model_id)
    downgrade_db(dburl=db_url, revision="0032_average_precision_ties")
    with db_pool_greenfield.connection() as conn:
        assert (
            conn.execute(
                "select count(*) as n from pg_proc where proname in ('prior_label_history',"
                " 'regression_residual_summary', 'regression_residuals_by_segment',"
                " 'largest_errors')"
            ).fetchone()["n"]
            == 0
        )
    upgrade_db(dburl=db_url, revision="head")
    assert readers.residual_summary(db_pool_greenfield, model_id) == before
