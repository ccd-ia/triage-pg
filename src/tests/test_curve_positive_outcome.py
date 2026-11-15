"""The threshold curve of a regression_ranking model: a positive is ``outcome > 0``.

``model_threshold_curve`` took ``sum(outcome)`` as its count of positives, which is that
count only for a 0/1 label. On a continuous target the "true positives" in a cut exceeded
the labeled rows there, so FP and TN went negative. The curve now counts ``outcome > 0``,
as ``precision@``, ``recall@`` and the near-miss bands do, and a classifier's curve is
unchanged.

The fixture is ``test_allocation_metrics``' seed: four regression_ranking models over 50
entities, 40 of them labeled — a gamma target with zeros among it on R1, a signed one on
R2, an all-zero one on R0. Each test also stores ``precision@`` and ``recall@`` at every
cut, which the default regression_ranking metrics do not include.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pytest
from alembic import script
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tests.test_allocation_metrics import (
    LABEL_TIMESPAN,
    N,
    N_LABELED,
    OUTCOME,
    R0,
    R1,
    R2,
    SIGNED,
    _outcomes,
    _ranked,
    _scores,
    seed_allocation,
)
from triage.component import results_schema
from triage.component.catwalk.in_pg_evaluation import evaluate_in_db
from triage.component.results_schema import downgrade_db, upgrade_db
from triage.dashboard.app import create_app
from triage.diagnostics import readers

TARGETS = {R1: OUTCOME, R2: SIGNED, R0: np.zeros(N_LABELED)}
#: Every cut of the population, so each curve row has its stored metric.
EVERY_K = [f"{k}_abs" for k in range(1, N + 1)]
COUNTS = ("tp", "fp", "fn", "tn")


def _revisions(db_url) -> tuple[str, str]:
    """``(this revision, the one it revises)``, found by name, not by number."""
    directory = script.ScriptDirectory.from_config(
        results_schema.alembic_config(db_url)
    )
    for rev in directory.walk_revisions():
        if rev.revision.endswith("_curve_positive_outcome"):
            assert isinstance(rev.down_revision, str), rev.down_revision
            return rev.revision, rev.down_revision
    raise AssertionError("no *_curve_positive_outcome revision in the results schema")


@pytest.fixture
def models(db_pool_greenfield) -> dict[str, int]:
    """The four models of the seed, with precision@ and recall@ at every cut."""
    pool = db_pool_greenfield
    ids = seed_allocation(pool)
    config = {"metrics": ["precision@", "recall@"], "thresholds": EVERY_K}
    for model_id in ids.values():
        for as_of in TARGETS:
            evaluate_in_db(pool, model_id, as_of, LABEL_TIMESPAN, metric_config=config)
    return ids


def _curve(pool, model_id: int, as_of: date) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        return conn.execute(
            "select * from triage.model_threshold_curve(%(m)s, %(d)s::date) order by k",
            {"m": model_id, "d": as_of},
        ).fetchall()


def _metrics(pool, model_id: int, as_of: date) -> dict[tuple[str, str], dict[str, Any]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "select metric, parameter, value, value_expected, num_labeled,"
            "       num_positive, num_labeled_above_threshold"
            " from triage.evaluations"
            " where model_id = %(m)s and as_of_date = %(d)s and subset_hash = ''"
            "   and metric in ('precision@', 'recall@')",
            {"m": model_id, "d": as_of},
        ).fetchall()
    return {(r["metric"], r["parameter"]): r for r in rows}


def _assert_counts_are_a_confusion(rows: list[dict[str, Any]], where: str) -> None:
    """Non-negative counts that partition the labeled rows at every cut."""
    for r in rows:
        assert all(r[c] >= 0 for c in COUNTS), (where, r)
        assert sum(r[c] for c in COUNTS) == N_LABELED, (where, r)


# ----------------------------------------------------------- the function itself


@pytest.mark.parametrize("as_of", list(TARGETS), ids=["R1", "R2", "R0"])
def test_the_curve_counts_positives_as_precision_and_recall_do(
    db_pool_greenfield, models, as_of
):
    pool = db_pool_greenfield
    outcomes = _outcomes(TARGETS[as_of])
    n_positive = int((TARGETS[as_of] > 0).sum())
    for name, model_id in models.items():
        where = f"{name} at {as_of}"
        curve = _curve(pool, model_id, as_of)
        assert [r["k"] for r in curve] == list(range(1, N + 1)), where
        _assert_counts_are_a_confusion(curve, where)

        stored = _metrics(pool, model_id, as_of)
        ranked = _ranked(_scores(name), outcomes)
        for r in curve:
            precision = stored[("precision@", f"{r['k']}_abs")]
            recall = stored[("recall@", f"{r['k']}_abs")]
            # the numpy reference: positives (outcome > 0) among the top k
            top = ranked[: r["k"]]
            assert r["tp"] == int((top > 0).sum()), (where, r)
            assert r["fn"] == n_positive - r["tp"], (where, r)
            assert r["tp"] + r["fp"] == precision["num_labeled_above_threshold"], (
                where,
                r,
            )
            if precision["value"] is None:
                assert r["prec"] is None, (where, r)
            else:
                assert r["prec"] == pytest.approx(precision["value"], abs=1e-12)
            if recall["value"] is None:
                assert r["rec"] is None, (where, r)
            else:
                assert r["rec"] == pytest.approx(recall["value"], abs=1e-12)
            # the base rate is the random-ranking expectation precision@ stores
            assert r["base_rate"] == pytest.approx(
                precision["value_expected"], abs=1e-12
            )
        assert curve[-1]["base_rate"] == pytest.approx(n_positive / N_LABELED)


def test_a_classifier_curve_is_unchanged_row_for_row(db_pool_greenfield, db_url):
    """On a 0/1 label ``outcome > 0`` is the outcome: 0024's classification fixture, with
    its unlabeled rows and score ties, gives the same rows before and after."""
    from tests.catwalk_tests.test_population_topk import (
        AS_OF_DATE,
        AS_OF_DATE_2,
        ENTITY_IDS,
        LABELS,
        SCORES,
        _add_second_test_date,
        _assert_curve_is_dssg,
        _link_run,
        _seed,
    )

    pool = db_pool_greenfield
    this, previous = _revisions(db_url)
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)
    _add_second_test_date(pool, model_id)
    dates = (date.fromisoformat(AS_OF_DATE), date.fromisoformat(AS_OF_DATE_2))

    downgrade_db(dburl=db_url, revision=previous)
    before = {d: _curve(pool, model_id, d) for d in dates}
    upgrade_db(dburl=db_url, revision=this)
    for d in dates:
        assert before[d], f"the snapshot at {d} is empty — the fixture is broken"
        assert _curve(pool, model_id, d) == before[d], d
    _assert_curve_is_dssg(before[dates[0]], SCORES, LABELS, ENTITY_IDS)


# ------------------------------------------------------------- what reads the curve


def test_the_readers_the_plot_and_the_route_draw_the_fixed_counts(
    db_pool_greenfield, db_url, models, tmp_path, monkeypatch
):
    from triage import plots
    import triage.cli as cli

    pool = db_pool_greenfield
    model_id = models["noisy"]
    base_rate = float((OUTCOME > 0).sum()) / N_LABELED

    thinned = readers.curve(pool, model_id, R1, "", 11)
    _assert_counts_are_a_confusion(thinned, "readers.curve")
    assert [r["base_rate"] for r in thinned] == pytest.approx(
        [base_rate] * len(thinned)
    )
    full = {r["k"]: r for r in readers.curve(pool, model_id, R1)}
    for r in thinned:
        assert r == full[r["k"]]

    figure = plots.curve(pool, model_id, R1)
    assert figure.data == {"everyone": readers.curve(pool, model_id, R1, "", 101)}
    assert f"base rate {base_rate:.3f}" in figure.summary

    with TestClient(create_app(pool=pool)) as client:
        served = client.get(
            f"/api/models/{model_id}/curve", params={"as_of_date": str(R1)}
        )
    assert served.status_code == 200, served.text
    assert served.json() == readers.curve(pool, model_id, R1)

    monkeypatch.chdir(tmp_path)  # no database.yaml or .env here: DATABASE_URL wins
    drawn = CliRunner().invoke(
        cli.app,
        ["plot", "curve", str(model_id), "--date", str(R1), "-o", "c.svg"],
        env={"DATABASE_URL": db_url},
        catch_exceptions=False,
    )
    assert drawn.exit_code == 0, drawn.output
    assert (tmp_path / "c.svg").stat().st_size > 0


def test_the_error_tree_reference_rates_read_the_fixed_counts(
    db_pool_greenfield, models
):
    """A regression_ranking fp/fn tree's reference rate is read off the curve."""
    pool = db_pool_greenfield
    model_id = models["noisy"]
    rates = readers.error_reference_rates(pool, model_id, "10_pct", R1)
    precision = _metrics(pool, model_id, R1)[("precision@", "5_abs")]  # 10% of 50
    assert rates["fp"] == pytest.approx(1 - precision["value"], abs=1e-12)
    assert rates["fn"] is not None and 0 <= rates["fn"] <= 1
    assert rates["confusion"] is not None and 0 <= rates["confusion"] <= 1


# --------------------------------------------------------------------- round trip


def _definition(pool) -> str:
    with pool.connection() as conn:
        rows = conn.execute(
            "select pg_get_functiondef(p.oid) as def"
            " from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
            " where n.nspname = 'triage' and p.proname = 'model_threshold_curve'"
        ).fetchall()
    assert len(rows) == 1, rows
    return rows[0]["def"]


def test_the_migration_round_trips_and_restores_0028s_function(
    db_pool_greenfield, db_url, models
):
    pool = db_pool_greenfield
    this, previous = _revisions(db_url)
    fixed = _definition(pool)
    noisy = models["noisy"]
    _assert_counts_are_a_confusion(_curve(pool, noisy, R1), "head")

    # down: the outcome is summed again, and a continuous target drives FP below zero
    downgrade_db(dburl=db_url, revision=previous)
    restored = _definition(pool)
    assert restored != fixed
    assert min(r["fp"] for r in _curve(pool, noisy, R1)) < 0

    # what the downgrade restores is the function as 0028 wrote it
    downgrade_db(dburl=db_url, revision="0027_error_confusion_recurrence")
    upgrade_db(dburl=db_url, revision="0028_subset_threshold_curve")
    assert _definition(pool) == restored

    upgrade_db(dburl=db_url, revision=this)
    assert _definition(pool) == fixed
