"""The model-group commands refuse a problem type the way the model commands do.

``triage postmodel error-recurrence`` and ``triage plot recurrence`` read the kinds of
leaf the group's problem type has (``problem_types.leaf_kinds``), the gate the
dashboard's recurrence route applies: a kind the type lacks exits 2 naming the kind to
read. ``triage plot curve-dates`` draws precision at a top-k cut, which a plain
regression or survival group lacks, so it exits 2 naming the figure to draw instead.
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

import triage.cli as cli
from tests.test_diagnostics import _seed_regression_trees
from tests.test_near_miss_prototypes import seed_model as seed_classifier
from tests.test_survival_diagnostics import seed_survival

RECURRENCE = (("postmodel", "error-recurrence"), ("plot", "recurrence"))


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


def _group_of(pool, model_id: int) -> int:
    with pool.connection() as conn:
        return conn.execute(
            "select model_group_id from triage.models where model_id = %(m)s",
            {"m": model_id},
        ).fetchone()["model_group_id"]


def _seed(pool, tmp_path, problem_type: str) -> int:
    """A model group of ``problem_type``."""
    if problem_type == "classification":
        return _group_of(pool, seed_classifier(pool, tmp_path, tag="gate"))
    if problem_type == "survival":
        return _group_of(pool, seed_survival(pool, tmp_path)["model_id"])
    return _seed_regression_trees(pool, tmp_path, problem_type)["group"]


@pytest.mark.parametrize("command", RECURRENCE, ids=" ".join)
@pytest.mark.parametrize(
    ("problem_type", "kind", "args", "named"),
    [
        # with a cut and without: the type is refused before the missing cut is
        ("regression", "fp", ["-p", "10_pct"], "--kind abs-error"),
        ("regression", "fp", [], "--kind abs-error"),
        ("regression", "confusion", [], "--kind abs-error"),
        ("classification", "abs-error", [], "--kind fp"),
        ("classification", "beats-baseline", [], "--kind fp"),
        ("survival", "abs-error", [], "--kind fp -p 'CUT|horizon=H'"),
    ],
)
def test_a_kind_the_groups_type_lacks_exits_2(
    run, db_pool_greenfield, tmp_path, command, problem_type, kind, args, named
):
    g = _seed(db_pool_greenfield, tmp_path, problem_type)
    code, text = run(*command, str(g), "--kind", kind, *args)
    assert code == 2, text
    assert f"model group {g} is {problem_type}: its error trees have" in text, text
    assert f"Use triage {' '.join(command)} {g} {named}" in text, text
    assert not list(tmp_path.glob("*.svg"))


def test_the_kinds_a_regression_group_has_still_read(run, db_pool_greenfield, tmp_path):
    from triage.diagnostics import compute_error_analysis

    seeded = _seed_regression_trees(db_pool_greenfield, tmp_path)
    compute_error_analysis(
        db_pool_greenfield,
        seeded["model"],
        kind="abs_error",
        max_depth=2,
        min_samples_leaf=20,
    )
    g = str(seeded["group"])
    code, text = run("postmodel", "error-recurrence", g, "--kind", "abs-error")
    assert code == 0 and "Rule recurrence" in text and "3/3" in text, text
    code, text = run("plot", "recurrence", g, "--kind", "abs-error")
    assert code == 0 and f"wrote recurrence-{g}.svg" in text, text
    # without --kind, the first kind the group's type has, as the dashboard reads it
    code, text = run("postmodel", "error-recurrence", g)
    assert code == 0 and "abs_error leaves" in text, text
    code, text = run("plot", "recurrence", g, "-o", "default.svg")
    assert code == 0 and "wrote default.svg" in text, text


@pytest.mark.parametrize(
    "kind", ["fp", "fn", "confusion", "abs-error", "beats-baseline"]
)
def test_a_regression_ranking_group_reads_both_families(
    run, db_pool_greenfield, tmp_path, kind
):
    g = str(_seed(db_pool_greenfield, tmp_path, "regression_ranking"))
    code, text = run("postmodel", "error-recurrence", g, "--kind", kind)
    assert code == 0 and "no " in text, text  # nothing persisted, but not refused


def test_a_classifier_group_reads_its_cut_kinds(run, db_pool_greenfield, tmp_path):
    g = str(_seed(db_pool_greenfield, tmp_path, "classification"))
    code, text = run("postmodel", "error-recurrence", g, "--kind", "fn")
    assert code == 0 and "no fn error rules" in text, text
    # the classification kinds of the plot still need their cut
    code, text = run("plot", "recurrence", g, "--kind", "fn")
    assert code == 2 and "--kind fn needs the cut its trees were fitted at" in text


@pytest.mark.parametrize(
    ("problem_type", "named"),
    [
        ("regression", "triage plot bias MODEL"),
        ("survival", "triage plot km MODEL"),
    ],
)
def test_curve_dates_refuses_a_group_without_a_cut(
    run, db_pool_greenfield, tmp_path, problem_type, named
):
    g = _seed(db_pool_greenfield, tmp_path, problem_type)
    code, text = run("plot", "curve-dates", str(g))
    assert code == 2, text
    assert (
        "triage plot curve-dates reads classification and regression_ranking model"
        f" groups; model group {g} is {problem_type}." in text
    ), text
    assert named in text and f"--group {g}" in text, text
    assert not list(tmp_path.glob("*.svg"))


def test_curve_dates_draws_a_regression_ranking_group(
    run, db_pool_greenfield, tmp_path
):
    g = _seed(db_pool_greenfield, tmp_path, "regression_ranking")
    code, text = run("plot", "curve-dates", str(g))
    assert code == 0 and f"wrote curve-dates-{g}.svg" in text, text
