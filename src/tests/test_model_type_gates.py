"""The last model and group commands that read a diagnostic only some types define.

``triage plot curve`` and ``triage model curve`` draw precision at each cut, which needs a
positive class at a top-k cut: a plain regression model's labeled rows are all positive
and a survival label has no outcome to count. ``triage plot calibration`` reads a score
against the outcome a bucket realized, which a survival label lacks. ``postmodel
crosstabs --against near-miss`` reads the near-miss band of ``postmodel near-miss``. The
error-rules readers take a ``--kind`` the model's type may have no leaves of, and ``plot
leaves`` reads the abs-error leaves without a cut and the fp and confusion ones with one.
Each exits 2 on a type it does not read, naming what to run instead, and still draws or
prints for a type it reads.
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

import triage.cli as cli
from tests.test_diagnostics import _seed_regression_trees
from tests.test_near_miss_prototypes import seed_model
from tests.test_regression_diagnostics import TEST_DATES
from tests.test_regression_diagnostics import _seed as seed_typed
from tests.test_survival_diagnostics import seed_survival


def _plain(output: str) -> str:
    # rich draws an error in a box: drop its side bars as well as the colour
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).replace("│", " ").split())


@pytest.fixture
def run(db_url, tmp_path, monkeypatch):
    """Run ``triage …`` against the test database, from ``tmp_path``."""
    monkeypatch.chdir(tmp_path)  # no database.yaml or .env here: DATABASE_URL wins

    def invoke(*args: str) -> tuple[int, str]:
        result = CliRunner().invoke(
            cli.app, list(args), env={"DATABASE_URL": db_url, "COLUMNS": "200"}
        )
        return result.exit_code, _plain(result.output)

    return invoke


def _evaluated(pool, model_id: int) -> int:
    """The model, with an evaluation on its latest test date (what calibration reads)."""
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.evaluations (model_id, split_kind, as_of_date, metric,"
            " parameter, value) values (%(m)s, 'test', %(d)s, 'mae', '', 1.0)",
            {"m": model_id, "d": TEST_DATES[1]},
        )
    return model_id


def _leaves_of(pool, model_id: int, kind: str) -> None:
    from triage.diagnostics import compute_error_analysis

    compute_error_analysis(pool, model_id, kind=kind, max_depth=2, min_samples_leaf=20)


# ------------------------------------------------------------------ threshold curve


@pytest.mark.parametrize(
    ("command", "problem_type", "named"),
    [
        (("plot", "curve"), "regression", "triage plot gains"),
        (("plot", "curve"), "survival", "triage plot km"),
        (("model", "curve"), "regression", "triage plot gains"),
        (("model", "curve"), "survival", "triage plot km"),
    ],
    ids=lambda v: " ".join(v) if isinstance(v, tuple) else v,
)
def test_the_curve_refuses_a_model_without_a_cut(
    run, db_pool_greenfield, tmp_path, command, problem_type, named
):
    m = seed_typed(db_pool_greenfield, problem_type, f"exp-curve-{problem_type}")
    code, text = run(*command, str(m))
    assert code == 2, text
    assert (
        f"triage {' '.join(command)} reads classification and regression_ranking models;"
        f" model {m} is {problem_type}." in text
    ), text
    assert f"use {named} {m}" in text, text
    assert not list(tmp_path.glob("*.svg"))


def test_the_curve_reads_a_ranking_model_and_refuses_a_compared_one(
    run, db_pool_greenfield, tmp_path
):
    ranking = seed_typed(db_pool_greenfield, "regression_ranking", "exp-curve-rr")
    day = str(TEST_DATES[0])
    code, text = run("plot", "curve", str(ranking), "--date", day)
    assert code == 0 and f"wrote curve-{ranking}.svg" in text, text
    code, text = run("model", "curve", str(ranking), "--date", day)
    assert code == 0 and "base rate" in text.lower(), text
    regression = seed_typed(db_pool_greenfield, "regression", "exp-curve-cmp")
    code, text = run("plot", "curve", str(ranking), "--compare", str(regression))
    assert code == 2 and f"model {regression} is regression" in text, text


# ------------------------------------------------------------------------ calibration


def test_calibration_refuses_a_survival_model(run, db_pool_greenfield, tmp_path):
    m = seed_survival(db_pool_greenfield, tmp_path)["model_id"]
    code, text = run("plot", "calibration", str(m))
    assert code == 2, text
    assert f"model {m} is survival." in text, text
    assert f"use triage plot survival-calibration {m}" in text, text
    assert not list(tmp_path.glob("*.svg"))


def test_calibration_still_draws_a_regression_model(run, db_pool_greenfield):
    m = _evaluated(db_pool_greenfield, seed_typed(db_pool_greenfield, "regression"))
    code, text = run("plot", "calibration", str(m))
    assert code == 0 and f"wrote calibration-{m}.svg" in text, text
    assert "(regression): mean outcome" in text, text


# --------------------------------------------------------- crosstabs against near miss


@pytest.mark.parametrize(
    ("problem_type", "named"),
    [("regression", "--by abs-error"), ("survival", "-p 10_pct")],
)
def test_the_near_miss_crosstab_refuses_a_model_without_a_cut(
    run, db_pool_greenfield, tmp_path, problem_type, named
):
    if problem_type == "survival":
        m = seed_survival(db_pool_greenfield, tmp_path)["model_id"]
    else:
        m = seed_model(
            db_pool_greenfield, tmp_path, tag="nm", problem_type=problem_type
        )
    code, text = run("postmodel", "crosstabs", str(m), "--against", "near-miss")
    assert code == 2, text
    assert (
        "triage postmodel crosstabs --against near-miss reads classification and"
        f" regression_ranking models; model {m} is {problem_type}." in text
    ), text
    assert f"triage postmodel crosstabs {m} {named}" in text, text
    with db_pool_greenfield.connection() as conn:
        written = conn.execute(
            "select count(*) as n from triage.crosstabs where model_id = %(m)s",
            {"m": m},
        ).fetchone()["n"]
    assert written == 0
    # against the rest, the same model's list still reads
    code, text = run("postmodel", "crosstabs", str(m), "-p", "10_pct")
    assert code == 0 and "selected vs rest" in text, text


# ------------------------------------------------------------------- error rules, leaf


@pytest.mark.parametrize(
    "command", [("postmodel", "error-rules"), ("plot", "error-rules")]
)
def test_error_rules_refuse_a_kind_the_models_type_lacks(
    run, db_pool_greenfield, tmp_path, command
):
    seeded = _seed_regression_trees(db_pool_greenfield, tmp_path)
    m = seeded["model"]
    _leaves_of(db_pool_greenfield, m, "abs_error")
    code, text = run(*command, str(m), "--kind", "fp")
    assert code == 2, text
    assert (
        f"model {m} is regression: its error trees have abs-error, beats-baseline"
        " leaves, not fp." in text
    ), text
    assert f"Use triage {' '.join(command)} {m} --kind abs-error." in text, text
    # the kind it has, and every persisted kind without --kind, still read
    for args in (["--kind", "abs-error"], []):
        code, text = run(*command, str(m), *args)
        assert code == 0, (args, text)


def test_error_rules_refuse_a_regression_kind_of_a_classifier(
    run, db_pool_greenfield, tmp_path
):
    m = seed_model(db_pool_greenfield, tmp_path, tag="rules")
    for command in (("postmodel", "error-rules"), ("plot", "error-rules")):
        code, text = run(*command, str(m), "--kind", "beats-baseline")
        assert code == 2, (command, text)
        assert f"model {m} is classification: its error trees have fp, fn," in text
        assert f"Use triage {' '.join(command)} {m} --kind fp." in text, text


@pytest.mark.parametrize(
    ("problem_type", "args", "named"),
    [
        ("regression", ["-p", "10_pct"], "with no -p"),
        ("classification", [], "-p CUT."),
        ("survival", [], "-p 'CUT|horizon=H'."),
    ],
)
def test_leaves_refuse_a_cut_the_groups_type_lacks(
    run, db_pool_greenfield, tmp_path, problem_type, args, named
):
    from tests.test_group_type_gates import _seed as seed_group

    g = seed_group(db_pool_greenfield, tmp_path, problem_type)
    code, text = run("plot", "leaves", str(g), *args)
    assert code == 2, text
    assert f"model group {g} is {problem_type}: its error trees have" in text, text
    assert f"Use triage plot leaves {g} {named}" in text, text
    assert not list(tmp_path.glob("*.svg"))


def test_leaves_draw_a_regression_groups_abs_error_leaves(
    run, db_pool_greenfield, tmp_path
):
    seeded = _seed_regression_trees(db_pool_greenfield, tmp_path)
    _leaves_of(db_pool_greenfield, seeded["model"], "abs_error")
    code, text = run("plot", "leaves", str(seeded["group"]))
    assert code == 0 and f"wrote leaves-{seeded['group']}.svg" in text, text
