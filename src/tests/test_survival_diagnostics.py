"""The censoring-aware survival set of migration 0036 (``diagnostics/survival.py``).

``test_survival.py`` seeds synthetic survival rows, not the DirtyDuck or Chicago 311
tutorial databases, so this module does the same: a Cox model fitted on a synthetic train
matrix, stored the way the model path stores it (joblib at ``models.artifact_uri``, both
matrices as Parquet rows of ``triage.matrices``, the test scores in ``triage.predictions``
with their matrix lineage). A third of the rows are censored early, before the horizon.

Every persisted value is checked against a direct scikit-survival call on the same arrays,
with the deciles rebuilt by a quadratic count rather than by the module's own sort.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import polars as pl
import pytest
from sksurv.compare import compare_survival
from sksurv.linear_model import CoxPHSurvivalAnalysis
from sksurv.metrics import brier_score, concordance_index_ipcw, cumulative_dynamic_auc
from sksurv.nonparametric import SurvivalFunctionEstimator
from sksurv.util import Surv
from typer.testing import CliRunner

import triage.cli as cli
from triage.diagnostics import readers
from triage.diagnostics.survival import (
    NEEDS_SURVIVAL_FUNCTION,
    compute_survival_diagnostics,
)

TEST_DATE = "2015-01-01"
TS = "60 days"
H = 30.0
TIMES = [10.0, 20.0, 30.0, 45.0]
FEATURES = ["x1", "x2"]
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _draw(
    rng: np.random.Generator, n: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Features, durations and events: risk rises with x1; a third censored early."""
    x = rng.normal(size=(n, 2))
    t_event = rng.exponential(scale=40 * np.exp(-x[:, 0]))
    early = rng.random(n) < 0.35
    censor = np.where(early, rng.uniform(1, 60, n), 60.0)
    duration = np.round(np.maximum(np.minimum(t_event, censor), 0.5), 1)
    return x, duration, t_event <= censor


def seed_survival(
    pool,
    tmp_path: Path,
    tag: str = "sd",
    *,
    estimator: Any = None,
    labels: str = "early",
    seed: int = 11,
) -> dict[str, Any]:
    """A survival model with its estimator, both matrices, labels and test scores.

    ``labels``: ``early`` (a third censored before the horizon), ``quiet-decile`` (the
    lowest-scored tenth has no event: censored at the end of follow-up) or
    ``all-censored`` (every test row censored before the horizon).
    """
    from triage.adapters.model import as_uuid

    rng = np.random.default_rng(seed)
    x_train, d_train, e_train = _draw(rng, 400)
    x_test, d_test, e_test = _draw(rng, 300)
    model = estimator if estimator is not None else CoxPHSurvivalAnalysis()
    model.fit(x_train, Surv.from_arrays(e_train, d_train))
    scores = np.asarray(model.predict(x_test), dtype=float)
    if labels == "quiet-decile":
        lowest = np.argsort(scores)[:30]
        e_test, d_test = e_test.copy(), d_test.copy()
        e_test[lowest], d_test[lowest] = False, 60.0
    elif labels == "all-censored":
        e_test = np.zeros(len(d_test), dtype=bool)
        d_test = np.round(rng.uniform(1, H - 1, len(d_test)), 1)

    uris = {}
    for kind, x, d, e in (
        ("train", x_train, d_train, e_train),
        ("test", x_test, d_test, e_test),
    ):
        frame = pl.DataFrame(
            {
                "entity_id": list(range(1, len(x) + 1)),
                "as_of_date": [TEST_DATE] * len(x),
                "x1": x[:, 0],
                "x2": x[:, 1],
                "duration": d,
                "event_observed": e,
            }
        ).with_columns(pl.col("as_of_date").str.to_date())
        uris[kind] = str(tmp_path / f"{tag}-{kind}.parquet")
        frame.write_parquet(uris[kind])
    artifact = str(tmp_path / f"{tag}.joblib")
    joblib.dump(model, artifact)
    test_uuid = str(as_uuid(f"{tag}-mx-test"))
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values (%(h)s, '{}'::jsonb, 'survival')",
            {"h": f"exp-{tag}"},
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile) values (%(h)s, 'local')"
            " returning run_id",
            {"h": f"exp-{tag}"},
        ).fetchone()["run_id"]
        for artifact_id, kind in (
            (f"{tag}-labels", "labels"),
            (f"{tag}-mx-train", "matrix"),
            (f"{tag}-mx-test", "matrix"),
            (f"{tag}-model", "model"),
        ):
            conn.execute(
                "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
                " values (%(a)s, %(a)s, %(k)s, '{}'::jsonb)",
                {"a": artifact_id, "k": kind},
            )
        conn.execute(
            "insert into triage.artifact_inputs (artifact_id, parent_id)"
            " values (%(x)s, %(l)s)",
            {"x": f"{tag}-mx-test", "l": f"{tag}-labels"},
        )
        for kind in ("train", "test"):
            conn.execute(
                "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind,"
                " storage_uri, feature_names, label_timespan) values (%(u)s::uuid,"
                " %(a)s, %(k)s, %(uri)s, %(f)s, cast(%(ts)s as interval))",
                {"u": str(as_uuid(f"{tag}-mx-{kind}")), "a": f"{tag}-mx-{kind}",
                 "k": kind, "uri": uris[kind], "f": FEATURES, "ts": TS},
            )  # fmt: skip
        gid = conn.execute(
            "insert into triage.model_groups (model_group_hash, model_type,"
            " hyperparameters, feature_list) values (%(h)s, %(t)s, '{}'::jsonb, %(f)s)"
            " returning model_group_id",
            {"h": f"{tag}-group", "t": type(model).__name__, "f": FEATURES},
        ).fetchone()["model_group_id"]
        model_id = conn.execute(
            "insert into triage.models (model_group_id, model_hash, run_id,"
            " train_matrix_uuid, train_end_time, training_label_timespan, artifact_uri,"
            " feature_list) values (%(g)s, %(a)s, %(r)s, %(u)s::uuid, date '2014-11-01',"
            " cast(%(ts)s as interval), %(uri)s, %(f)s) returning model_id",
            {"g": gid, "a": f"{tag}-model", "r": run_id,
             "u": str(as_uuid(f"{tag}-mx-train")), "ts": TS, "uri": artifact,
             "f": FEATURES},
        ).fetchone()["model_id"]  # fmt: skip
        with conn.cursor() as cur:
            cur.executemany(
                "insert into triage.labels (label_hash, entity_id, as_of_date,"
                " label_timespan, duration, event_observed) values (%(l)s, %(e)s,"
                " %(d)s, cast(%(ts)s as interval), %(dur)s, %(ev)s)",
                [
                    {
                        "l": f"{tag}-labels",
                        "e": i,
                        "d": TEST_DATE,
                        "ts": TS,
                        "dur": float(dur),
                        "ev": bool(ev),
                    }
                    for i, (dur, ev) in enumerate(zip(d_test, e_test), start=1)
                ],  # fmt: skip
            )
            cur.executemany(
                "insert into triage.predictions (model_id, entity_id, as_of_date,"
                " split_kind, score, matrix_uuid) values (%(m)s, %(e)s, %(d)s, 'test',"
                " %(s)s, %(u)s::uuid)",
                [
                    {
                        "m": model_id,
                        "e": i,
                        "d": TEST_DATE,
                        "s": float(s),
                        "u": test_uuid,
                    }
                    for i, s in enumerate(scores, start=1)
                ],  # fmt: skip
            )
    return {
        "model_id": model_id,
        "group": gid,
        "estimator": model,
        "train": Surv.from_arrays(e_train, d_train),
        "test": Surv.from_arrays(e_test, d_test),
        "x_test": x_test,
        "scores": scores,
        "duration": d_test,
        "event": e_test,
    }


# ------------------------------------------------------------------------ the references


def _deciles(scores: np.ndarray, bins: int) -> np.ndarray:
    """ceil(c · bins / N) with c counted pairwise, not by a sort."""
    c = (scores[None, :] >= scores[:, None]).sum(axis=1)
    return np.minimum(bins, np.ceil(c * bins / len(scores))).astype(int)


def _km_survival(event: np.ndarray, duration: np.ndarray, t: float) -> float:
    return float(
        SurvivalFunctionEstimator()
        .fit(Surv.from_arrays(event, duration))
        .predict_proba(np.array([t]))[0]
    )


def _by_bin(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {r["bin"]: r for r in rows}


@pytest.fixture
def computed(db_pool_greenfield, tmp_path):
    seeded = seed_survival(db_pool_greenfield, tmp_path)
    result = compute_survival_diagnostics(
        db_pool_greenfield, seeded["model_id"], H, TIMES
    )
    stored = readers.survival_diagnostics(db_pool_greenfield, seeded["model_id"])
    assert stored is not None
    return seeded, result, stored


def test_persisted_values_equal_direct_sksurv_calls(computed):
    seeded, result, stored = computed
    kinds = stored["kinds"]
    event, duration, scores = seeded["event"], seeded["duration"], seeded["scores"]
    assert result.dates and str(stored["as_of_date"]) == TEST_DATE
    assert stored["horizon"] == H and result.estimator == "CoxPHSurvivalAnalysis"

    deciles = _deciles(scores, 10)
    km = _by_bin(kinds["km_decile"])
    p_pop = 1 - _km_survival(event, duration, H)
    assert km[0]["value"] == pytest.approx(p_pop, abs=1e-12)
    assert sorted(b for b in km if b) == list(range(1, 11))
    for b in range(1, 11):
        m = deciles == b
        expected = 1 - _km_survival(event[m], duration[m], H)
        assert km[b]["value"] == pytest.approx(expected, abs=1e-12), b
        assert km[b]["n"] == int(m.sum())
        assert km[b]["extra"]["lift"] == pytest.approx(expected / p_pop, abs=1e-12)

    tertiles = _deciles(scores, 3)
    p = compare_survival(seeded["test"], tertiles)[1]
    assert kinds["logrank"][0]["value"] == pytest.approx(p, abs=1e-12)

    uno = concordance_index_ipcw(seeded["train"], seeded["test"], scores, tau=H)[0]
    assert kinds["uno_c"][0]["value"] == pytest.approx(uno, abs=1e-12)

    aucs, mean_auc = cumulative_dynamic_auc(
        seeded["train"], seeded["test"], scores, TIMES
    )
    assert [r["t"] for r in kinds["auc_t"]] == TIMES
    assert [r["value"] for r in kinds["auc_t"]] == pytest.approx(list(aucs), abs=1e-12)
    assert kinds["auc_t"][0]["extra"]["mean_auc"] == pytest.approx(mean_auc, abs=1e-12)

    functions = seeded["estimator"].predict_survival_function(seeded["x_test"])
    surv = np.vstack([fn(np.array(TIMES)) for fn in functions])
    _, briers = brier_score(seeded["train"], seeded["test"], surv, TIMES)
    assert [r["value"] for r in kinds["brier"]] == pytest.approx(
        list(briers), abs=1e-12
    )

    predicted = np.array([fn(H) for fn in functions])
    by_pred = _deciles(1 - predicted, 10)
    cal = _by_bin(kinds["calibration"])
    gaps, weights = [], []
    for b in range(1, 11):
        m = by_pred == b
        observed = _km_survival(event[m], duration[m], H)
        assert cal[b]["value"] == pytest.approx(observed, abs=1e-12), b
        assert cal[b]["extra"]["predicted"] == pytest.approx(predicted[m].mean())
        gaps.append(abs(predicted[m].mean() - observed))
        weights.append(int(m.sum()))
    assert cal[0]["extra"]["ici"] == pytest.approx(np.average(gaps, weights=weights))
    assert cal[0]["extra"]["e50"] == pytest.approx(np.median(np.repeat(gaps, weights)))


def test_km_by_decile_is_never_the_raw_event_fraction(computed):
    """A row censored before the horizon is not an event-free row: KM is above the share.

    The two agree only in a bin where no row is censored before one of its events (the
    KM product then telescopes to the share), so each bin is checked against that.
    """
    seeded, _, stored = computed
    event, duration = seeded["event"], seeded["duration"]
    deciles = _deciles(seeded["scores"], 10)
    km = _by_bin(stored["kinds"]["km_decile"])
    above = 0
    for b in range(0, 11):
        m = np.ones(len(event), dtype=bool) if b == 0 else deciles == b
        by_h = event & (duration <= H)
        raw = by_h[m].sum() / m.sum()
        assert km[b]["extra"]["n_events"] == int(by_h[m].sum())
        last_event = duration[m & by_h].max(initial=-1.0)
        censored_first = (m & ~event & (duration < last_event)).any()
        if censored_first:
            assert km[b]["value"] > raw + 1e-6, (b, km[b]["value"], raw)
            above += 1
        else:
            assert km[b]["value"] == pytest.approx(raw, abs=1e-12), b
    assert above >= 9  # the population and nearly every decile censor before an event


def test_km_group_curves_and_the_population(computed):
    seeded, _, stored = computed
    curves: dict[int, list[dict[str, Any]]] = {}
    for r in stored["kinds"]["km_group"]:
        curves.setdefault(r["bin"], []).append(r)
    assert sorted(curves) == [0, 1, 2, 3]
    assert curves[0][0]["t"] == 0.0 and curves[0][0]["value"] == 1.0
    assert curves[0][0]["n"] == 300
    tertiles = _deciles(seeded["scores"], 3)
    m = tertiles == 1
    last = curves[1][-1]
    assert last["value"] == pytest.approx(
        _km_survival(seeded["event"][m], seeded["duration"][m], last["t"])
    )
    # the riskiest tertile has the lowest survival by the horizon
    at_h = {g: min((r for r in rows if r["t"] <= H), key=lambda r: r["value"])
            for g, rows in curves.items()}  # fmt: skip
    assert at_h[1]["value"] < at_h[2]["value"] < at_h[3]["value"]


def test_rerunning_replaces_one_horizon_and_keeps_another(computed, db_pool_greenfield):
    seeded, result, _ = computed
    model_id = seeded["model_id"]
    compute_survival_diagnostics(db_pool_greenfield, model_id, H, TIMES)
    compute_survival_diagnostics(db_pool_greenfield, model_id, 45.0, [20.0, 40.0])
    with db_pool_greenfield.connection() as conn:
        counts = {
            r["horizon"]: r["n"]
            for r in conn.execute(
                "select horizon, count(*) as n from triage.survival_diagnostics"
                " where model_id = %(m)s group by horizon",
                {"m": model_id},
            ).fetchall()
        }
    assert counts[H] == result.rows_written and 45.0 in counts
    latest = readers.survival_diagnostics(db_pool_greenfield, model_id)
    assert latest is not None and latest["horizon"] == 45.0
    at_h = readers.survival_diagnostics(db_pool_greenfield, model_id, horizon=H)
    assert at_h is not None and [r["t"] for r in at_h["kinds"]["brier"]] == TIMES


def test_a_time_outside_the_follow_up_is_null_with_its_reason(
    db_pool_greenfield, tmp_path
):
    seeded = seed_survival(db_pool_greenfield, tmp_path)
    compute_survival_diagnostics(
        db_pool_greenfield, seeded["model_id"], H, [20.0, 90.0]
    )
    stored = readers.survival_diagnostics(db_pool_greenfield, seeded["model_id"])
    assert stored is not None
    for kind in ("auc_t", "brier"):
        rows = {r["t"]: r for r in stored["kinds"][kind]}
        assert rows[20.0]["value"] is not None
        assert rows[90.0]["value"] is None
        assert "outside the test follow-up" in rows[90.0]["extra"]["undefined"]


# ------------------------------------------------------------------------- edge cases


def test_a_decile_with_no_event_reads_zero_not_null(db_pool_greenfield, tmp_path):
    seeded = seed_survival(db_pool_greenfield, tmp_path, labels="quiet-decile")
    compute_survival_diagnostics(db_pool_greenfield, seeded["model_id"], H, TIMES)
    stored = readers.survival_diagnostics(db_pool_greenfield, seeded["model_id"])
    assert stored is not None
    quiet = _by_bin(stored["kinds"]["km_decile"])[10]
    assert quiet["n"] == 30 and quiet["extra"]["n_events"] == 0
    assert quiet["value"] == 0.0 and quiet["extra"]["lift"] == 0.0
    assert "undefined" not in quiet["extra"]


def test_every_row_censored_before_the_horizon(db_pool_greenfield, tmp_path):
    seeded = seed_survival(db_pool_greenfield, tmp_path, labels="all-censored")
    compute_survival_diagnostics(db_pool_greenfield, seeded["model_id"], H, TIMES)
    stored = readers.survival_diagnostics(db_pool_greenfield, seeded["model_id"])
    assert stored is not None
    kinds = stored["kinds"]
    assert all(r["value"] is None for r in kinds["km_decile"])
    assert "followed to the horizon" in kinds["km_decile"][0]["extra"]["undefined"]
    assert kinds["logrank"][0]["value"] is None
    assert kinds["logrank"][0]["extra"]["undefined"] == "no observed event"
    assert kinds["uno_c"][0]["value"] is None
    assert all(r["value"] is None for r in kinds["auc_t"] + kinds["brier"])
    cal = _by_bin(kinds["calibration"])
    assert cal[0]["value"] is None and cal[0]["extra"]["ici"] is None
    # the KM curves are still drawn: every row survives to its censoring
    assert {r["value"] for r in kinds["km_group"]} == {1.0}


def test_an_estimator_without_a_survival_function(db_pool_greenfield, tmp_path):
    from sksurv.svm import FastSurvivalSVM

    seeded = seed_survival(
        db_pool_greenfield,
        tmp_path,
        estimator=FastSurvivalSVM(max_iter=20, random_state=0),
    )
    result = compute_survival_diagnostics(
        db_pool_greenfield, seeded["model_id"], H, TIMES
    )
    stored = readers.survival_diagnostics(db_pool_greenfield, seeded["model_id"])
    assert stored is not None and result.estimator == "FastSurvivalSVM"
    for kind in ("brier", "calibration"):
        (row,) = stored["kinds"][kind]
        assert row["value"] is None
        assert row["extra"]["undefined"] == (
            f"{NEEDS_SURVIVAL_FUNCTION}: FastSurvivalSVM has none"
        )
    assert stored["kinds"]["uno_c"][0]["value"] is not None
    assert all(r["value"] is not None for r in stored["kinds"]["auc_t"])


def test_the_house_cox_wrapper_has_a_survival_function():
    from triage.component.catwalk.estimators.survival import (
        ScaledCoxPHSurvivalAnalysis,
    )

    rng = np.random.default_rng(2)
    x, duration, event = _draw(rng, 120)
    x[:, 1] *= 1000  # the scale the wrapper exists for
    wrapper = ScaledCoxPHSurvivalAnalysis().fit(x, Surv.from_arrays(event, duration))
    inner: list[Any] = list(
        wrapper.model.predict_survival_function(wrapper.minmax_scaler.transform(x))
    )
    outer: list[Any] = list(wrapper.predict_survival_function(x))
    assert [fn(20.0) for fn in outer] == [fn(20.0) for fn in inner]
    assert wrapper.predict_survival_function(x, return_array=True).shape[0] == 120


def test_compute_refuses_a_bad_horizon_and_a_missing_train_matrix(
    db_pool_greenfield, tmp_path
):
    seeded = seed_survival(db_pool_greenfield, tmp_path)
    with pytest.raises(ValueError, match="positive duration"):
        compute_survival_diagnostics(db_pool_greenfield, seeded["model_id"], 0.0)
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "update triage.models set train_matrix_uuid = null where model_id = %(m)s",
            {"m": seeded["model_id"]},
        )
    with pytest.raises(ValueError, match="has no train matrix"):
        compute_survival_diagnostics(db_pool_greenfield, seeded["model_id"], H)


# ------------------------------------------------------------------ the horizon tree


def test_the_binarized_tree_drops_early_censored_rows(db_pool_greenfield, tmp_path):
    from triage.diagnostics import compute_error_analysis

    pool = db_pool_greenfield
    seeded = seed_survival(pool, tmp_path)
    event, duration = seeded["event"], seeded["duration"]
    kept = event | (duration >= H)
    positive = event & (duration <= H)
    assert (~kept).sum() > 20  # the fixture censors rows before the horizon
    written = compute_error_analysis(
        pool, seeded["model_id"], parameter="30_pct", kind="all", max_depth=2,
        min_samples_leaf=10, horizon=H,
    )  # fmt: skip
    assert written > 0
    with pool.connection() as conn:
        rows = conn.execute(
            "select error_kind, n_matched, n_errors, class_counts, parameter"
            " from triage.error_analysis where model_id = %(m)s",
            {"m": seeded["model_id"]},
        ).fetchall()
    assert {r["parameter"] for r in rows} == {"30_pct|horizon=30"}
    confusion = [r for r in rows if r["error_kind"] == "confusion"]
    assert sum(r["n_matched"] for r in confusion) == int(kept.sum())
    counts = {
        c: sum(r["class_counts"][c] for r in confusion)
        for c in ("tp", "fp", "fn", "tn")
    }
    assert counts["tp"] + counts["fn"] == int(positive.sum())
    # the cut is over every scored row, as precision@ cuts it (ADR-0033)
    k = 90
    top = np.argsort(-seeded["scores"], kind="stable")[:k]
    in_top = np.zeros(len(event), dtype=bool)
    in_top[top] = True
    fp_rows = [r for r in rows if r["error_kind"] == "fp"]
    assert sum(r["n_matched"] for r in fp_rows) == int((in_top & kept).sum())
    assert sum(r["n_errors"] for r in fp_rows) == int((in_top & kept & ~positive).sum())

    leaves = readers.error_rules(pool, seeded["model_id"], error_kind="confusion")
    pooled = sum(r["n_errors"] for r in confusion) / sum(
        r["n_matched"] for r in confusion
    )
    assert leaves and all(
        r["reference_error_rate"] == pytest.approx(pooled) for r in leaves
    )


# ------------------------------------------------------------------ the histogram


def test_the_histogram_splits_events_from_censored_rows(db_pool_greenfield, tmp_path):
    from tests.test_regression_diagnostics import _seed

    pool = db_pool_greenfield
    seeded = seed_survival(pool, tmp_path)
    bins = readers.score_histogram(pool, seeded["model_id"], 10)
    assert sum(b["n"] for b in bins) == 300
    assert sum(b["n_event"] for b in bins) == int(seeded["event"].sum())
    assert sum(b["n_censored"] for b in bins) == int((~seeded["event"]).sum())
    assert all(b["n_pos"] is None and b["mean_outcome"] is None for b in bins)
    assert bins[0]["lo"] == pytest.approx(seeded["scores"].min())

    regression = _seed(pool, "regression", "exp-hist-reg")
    rows = readers.score_histogram(pool, regression, 5)
    assert rows and all(r["n_event"] is None and r["n_censored"] is None for r in rows)


def test_migration_0036_round_trips(db_url, db_pool_greenfield):
    from triage.component.results_schema import downgrade_db, upgrade_db

    def state(conn) -> tuple[bool, int]:
        table = conn.execute(
            "select to_regclass('triage.survival_diagnostics') is not null as t"
        ).fetchone()["t"]
        # the histogram's output columns: every argument type less the inputs
        n_out = conn.execute(
            "select array_length(p.proallargtypes, 1) - p.pronargs as n"
            " from pg_proc p join pg_namespace s on s.oid = p.pronamespace"
            " where s.nspname = 'triage' and p.proname = 'model_score_histogram'"
        ).fetchone()["n"]
        return table, n_out

    with db_pool_greenfield.connection() as conn:
        assert state(conn) == (True, 8)
    downgrade_db(dburl=db_url, revision="0035_allocation_metrics")
    with db_pool_greenfield.connection() as conn:
        assert state(conn) == (False, 6)
    upgrade_db(dburl=db_url, revision="head")
    with db_pool_greenfield.connection() as conn:
        assert state(conn) == (True, 8)


# ------------------------------------------------------------------------ the CLI


@pytest.fixture
def run_cli(db_url, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def run(*args: str) -> tuple[int, str]:
        result = CliRunner().invoke(
            cli.app, list(args), env={"DATABASE_URL": db_url, "COLUMNS": "200"}
        )
        return result.exit_code, " ".join(_ANSI.sub("", result.output).split())

    return run


def test_postmodel_survival_prints_and_persists(run_cli, db_pool_greenfield, tmp_path):
    from tests.test_regression_diagnostics import _seed

    seeded = seed_survival(db_pool_greenfield, tmp_path)
    m = str(seeded["model_id"])
    code, text = run_cli("postmodel", "survival", m, "--horizon", "30", "--times",
                         "10,20,30,45")  # fmt: skip
    assert code == 0, text
    assert "Risk deciles at horizon 30" in text and "Uno's C up to the horizon" in text
    assert "Over time" in text and "ICI" in text and "Log-rank" in text
    code, text = run_cli("postmodel", "survival", m, "--horizon", "30", "--json")
    assert code == 0, text
    payload = json.loads(text[text.index("{") :])
    (day,) = payload["dates"]
    assert payload["estimator"] == "CoxPHSurvivalAnalysis" and day["horizon"] == 30.0
    assert len(day["kinds"]["auc_t"]) == 10  # the default grid: ten times up to H

    regression = _seed(db_pool_greenfield, "regression", "exp-cli-reg")
    code, text = run_cli("postmodel", "survival", str(regression), "--horizon", "30")
    assert code == 2 and "reads survival models" in text, text
    assert f"triage postmodel residuals {regression}" in text
    code, text = run_cli("postmodel", "survival", m, "--horizon", "0")
    assert code == 2 and "not a positive duration" in text, text
    code, text = run_cli("postmodel", "survival", m, "--horizon", "30", "--times", "a")
    assert code == 2 and "comma-separated list of durations" in text, text


def test_error_tree_on_a_survival_model_needs_a_horizon(
    run_cli, db_pool_greenfield, tmp_path
):
    from tests.test_regression_diagnostics import _seed

    seeded = seed_survival(db_pool_greenfield, tmp_path)
    m = str(seeded["model_id"])
    code, text = run_cli("postmodel", "error-tree", m)
    assert code == 2 and "Add --horizon H" in text, text
    code, text = run_cli("postmodel", "error-tree", m, "--horizon", "30", "--kind",
                         "confusion", "--min-leaf", "10")  # fmt: skip
    assert code == 0, text
    assert "Error rules — model" in text and "100_abs|horizon=30" in text
    classifier = _seed(db_pool_greenfield, "classification", "exp-cli-class")
    code, text = run_cli("postmodel", "error-tree", str(classifier), "--horizon", "30")
    assert code == 2 and "reads survival models only" in text, text
