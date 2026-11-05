"""``triage.plots``: each figure draws exactly the rows the readers return, and writes the
same bytes twice.

Smoke and parity tests, not pixel comparisons. For each of the eight plot functions: the
rows in ``result.data`` equal the readers' rows for the same ids and options, and two
runs give byte-identical SVG. Then the errors: the series cap, nothing persisted, and an
unsupported extension. The fixture extends the readers' fixture (one model group with
two models, three test dates and a Subset) with error-tree leaves, crosstabs, and a
second model group whose one model scores the first date.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tests.test_readers import (  # noqa: F401 — group is a fixture
    D1,
    D2,
    D3,
    LABEL_TIMESPAN,
    SUBSET,
    SUBSET_NAME,
    Group,
    _leaf,
    _score,
    group,
)
from triage import plots
from triage.diagnostics import readers
from triage.plots import NothingToDraw, PlotResult

CUT = "10_pct"
ALL = 1_000_000
FEATURES = {  # feature → (list mean, rest mean, list nonzero, rest nonzero)
    "visits": (4.0, 1.0, 0.9, 0.3),
    "violations": (0.5, 2.0, 0.2, 0.6),
    "closures": (3.0, 2.0, 0.05, 0.04),
    "permits": (1.2, 1.0, 0.7, 0.6),
    "zip_60622": (0.3, 0.1, 0.3, 0.1),
}


@dataclass
class Diagnosed:
    group: Group
    other_group: int
    model_c: int  # the other group's model, scored on D1 like model A


def _crosstabs(pool, model_id: int, as_of: date, shift: float) -> None:
    """Mean and nonzero-rate crosstabs; ``shift`` moves the list's values per date."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.executemany(
            "insert into triage.crosstabs (model_id, split_kind, as_of_date, parameter,"
            " feature, stat, selected_value, rest_value, ratio)"
            " values (%(m)s, 'test', %(d)s, %(p)s, %(f)s, %(stat)s, %(s)s, %(r)s,"
            "         %(ratio)s)",
            [
                {
                    "m": model_id,
                    "d": as_of,
                    "p": CUT,
                    "f": feature,
                    "stat": stat,
                    "s": s,
                    "r": r,
                    "ratio": s / r,
                }
                for feature, (mean_s, mean_r, nz_s, nz_r) in FEATURES.items()
                for stat, s, r in (
                    ("mean", mean_s * shift, mean_r),
                    ("nonzero_rate", min(nz_s * shift, 1.0), nz_r),
                )
            ],
        )


@pytest.fixture
def diagnosed(db_pool_greenfield, group) -> Diagnosed:  # noqa: F811
    pool = db_pool_greenfield
    a, b = group.model_a, group.model_b
    # error-tree leaves at the cut: fp, fn and confusion on each test date; the fn rule
    # "f>" recurs on all three dates
    for model, as_of in ((a, D1), (b, D3), (a, D2)):
        _leaf(pool, model, as_of, "fp", 0, 100, 1, {"tp": 99, "fp": 1})
        _leaf(pool, model, as_of, "fp", 1, 40, 30, {"tp": 10, "fp": 30})
        _leaf(pool, model, as_of, "fn", 0, 60, 20, {"fn": 20, "tn": 40})
        _leaf(pool, model, as_of, "confusion", 0, 50, 10, {"tp": 40, "fp": 10})
    _crosstabs(pool, a, D1, 1.0)
    _crosstabs(pool, b, D3, 1.1)
    _crosstabs(pool, a, D2, 1.2)
    _crosstabs(pool, b, D1, 0.9)  # for the comparison with model A on D1

    with pool.connection() as conn:
        run_id = conn.execute(
            "select run_id from triage.runs where experiment_hash = 'exp-a'"
        ).fetchone()["run_id"]
        matrix_uuid = conn.execute(
            "select matrix_uuid from triage.matrices"
        ).fetchone()["matrix_uuid"]
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " values ('model-c', 'model-c', 'model', '{}'::jsonb)"
        )
        other = conn.execute(
            "insert into triage.model_groups"
            " (model_group_hash, model_type, hyperparameters, feature_list)"
            " values ('mg-other', 'sklearn.dummy.DummyClassifier', '{}'::jsonb,"
            " array['f']) returning model_group_id"
        ).fetchone()["model_group_id"]
        model_c = conn.execute(
            "insert into triage.models (model_group_id, model_hash, run_id,"
            " train_end_time, training_label_timespan)"
            " values (%(g)s, 'model-c', %(r)s, date '2013-07-01', cast(%(ts)s as interval))"
            " returning model_id",
            {"g": other, "r": run_id, "ts": LABEL_TIMESPAN},
        ).fetchone()["model_id"]
        _score(conn, model_c, D1, matrix_uuid, seed=4)
    return Diagnosed(group, other, model_c)


def _svg(result: PlotResult, path: Path) -> bytes:
    result.save(path)
    return path.read_bytes()


# ------------------------------------------------------------------------ the package


def test_the_plots_run_no_sql_and_need_no_dashboard():
    package = Path(plots.__file__).parent
    for source in package.glob("*.py"):
        tree = ast.parse(source.read_text())
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not {"psycopg", "fastapi", "triage.dashboard"} & imported, source
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not {"execute", "connection", "cursor"} & calls, source
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, triage.plots;"
            " print('fastapi' in sys.modules, 'matplotlib.pyplot' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert loaded.stdout.split() == ["False", "False"], loaded.stdout + loaded.stderr


def test_every_format_is_written_and_others_are_refused(
    db_pool_greenfield, group, tmp_path
):  # noqa: F811
    result = plots.curve(db_pool_greenfield, group.model_a, D1)
    assert ET.parse(result.save(tmp_path / "c.svg")).getroot().tag.endswith("svg")
    assert result.save(tmp_path / "c.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert result.save(tmp_path / "nested" / "c.pdf").read_bytes()[:5] == b"%PDF-"
    with pytest.raises(ValueError, match=r"\.svg, \.png, \.pdf"):
        result.save(tmp_path / "c.jpg")
    assert not (tmp_path / "c.jpg").exists()


# ------------------------------------------------------------------ the same bytes twice

#: Each plot function, as its CLI command draws it on the fixture.
DRAWS = {
    "curve": lambda pool, d: plots.curve(pool, d.group.model_a, D1, [SUBSET_NAME]),
    "curve-dates": lambda pool, d: plots.curve_dates(pool, d.group.model_group_id),
    "error-rules": lambda pool, d: plots.error_rules(pool, d.group.model_a),
    "leaves": lambda pool, d: plots.leaves(pool, d.group.model_group_id, CUT),
    "recurrence": lambda pool, d: plots.recurrence(pool, d.group.model_group_id, CUT),
    "crosstabs": lambda pool, d: plots.crosstabs(
        pool,
        d.group.model_a,
        stat="nonzero_rate",
        compare=d.group.model_b,
        as_of_date=D1,
    ),
    "crosstabs-dates": lambda pool, d: plots.crosstabs_dates(
        pool, d.group.model_group_id
    ),
    "overlap": lambda pool, d: plots.overlap(
        pool, d.group.model_group_id, d.other_group, CUT
    ),
}


@pytest.mark.parametrize("kind", list(DRAWS))
def test_two_runs_write_the_same_svg(db_pool_greenfield, diagnosed, tmp_path, kind):
    draw = DRAWS[kind]
    one = _svg(draw(db_pool_greenfield, diagnosed), tmp_path / "one.svg")
    two = _svg(draw(db_pool_greenfield, diagnosed), tmp_path / "two.svg")
    assert one == two
    assert ET.fromstring(one).tag.endswith("svg")  # it parses
    assert b"<dc:date>" not in one and b"Matplotlib v" not in one


# ----------------------------------------------------------------------------- curves


def test_curve_draws_the_readers_rows_per_population(
    db_pool_greenfield,
    group,  # noqa: F811
    tmp_path,
):
    pool = db_pool_greenfield
    result = plots.curve(pool, group.model_a, D1, subsets=[SUBSET_NAME])
    assert result.data == {
        "everyone": readers.curve(pool, group.model_a, D1, "", 101),
        SUBSET_NAME: readers.curve(pool, group.model_a, D1, SUBSET, 101),
    }
    assert result.summary.startswith(f"model {group.model_a} at {D1}")
    assert f"{SUBSET_NAME}: {group.subset_size} scored" in result.summary


def test_curve_compares_two_models(db_pool_greenfield, diagnosed, tmp_path):
    pool = db_pool_greenfield
    a, c = diagnosed.group.model_a, diagnosed.model_c
    result = plots.curve(pool, a, D1, compare=c, points=None)
    assert result.data == {
        f"model {a}": readers.curve(pool, a, D1),
        f"model {c}": readers.curve(pool, c, D1),
    }
    # no date: each model's latest
    latest = plots.curve(pool, a, compare=diagnosed.group.model_b)
    assert latest.data == {
        f"model {a}": readers.curve(pool, a, D2, "", 101),
        f"model {diagnosed.group.model_b}": readers.curve(
            pool, diagnosed.group.model_b, D3, "", 101
        ),
    }
    with pytest.raises(ValueError, match="itself"):
        plots.curve(pool, a, D1, compare=a)


def test_curve_dates_draws_one_series_per_model_and_date(
    db_pool_greenfield,
    group,  # noqa: F811
    tmp_path,
):
    pool = db_pool_greenfield
    result = plots.curve_dates(pool, group.model_group_id)
    assert list(result.data.values()) == readers.group_curves(
        pool, group.model_group_id
    )
    assert list(result.data) == [
        f"{D1} · model {group.model_a}",
        f"{D3} · model {group.model_b}",
        f"{D2} · model {group.model_a}",
    ]
    subset = plots.curve_dates(pool, group.model_group_id, SUBSET[:4], points=None)
    assert list(subset.data.values()) == readers.group_curves(
        pool, group.model_group_id, SUBSET, None
    )


# ------------------------------------------------------------------------ error trees


def test_error_rules_draws_the_leaves_with_the_most_errors(
    db_pool_greenfield, diagnosed, tmp_path
):
    pool = db_pool_greenfield
    a = diagnosed.group.model_a
    result = plots.error_rules(pool, a, top=1)
    every = readers.error_rules(pool, a, limit=ALL)

    def expected(kind: str, day: date, top: int) -> list[dict[str, Any]]:
        rows = [
            r
            for r in every
            if r["error_kind"] == kind
            and r["as_of_date"] == day
            and r["parameter"] == CUT
        ]
        return sorted(rows, key=lambda r: (-r["n_errors"], r["rule"]))[:top]

    # the latest date with leaves, the fp and fn trees, the leaf with the most errors
    assert result.data == {"fp": expected("fp", D2, 1), "fn": expected("fn", D2, 1)}
    assert result.data["fp"][0]["n_errors"] == 30
    fp_rate = readers.error_reference_rates(pool, a, CUT, D2)["fp"]
    assert result.data["fp"][0]["reference_error_rate"] == fp_rate
    assert f"model {a} at {D2}, {CUT}: 1 fp leaves against" in result.summary

    confusion = plots.error_rules(pool, a, D1, CUT, "confusion")
    assert confusion.data == {"confusion": expected("confusion", D1, 12)}


def test_leaves_counts_the_suspicious_zone(db_pool_greenfield, diagnosed, tmp_path):
    pool = db_pool_greenfield
    gid = diagnosed.group.model_group_id
    result = plots.leaves(pool, gid, CUT)
    rows = readers.group_leaves(pool, gid, CUT, ("fp", "confusion"))
    assert result.data == {
        "fp": [r for r in rows if r["error_kind"] == "fp"],
        "confusion": [r for r in rows if r["error_kind"] == "confusion"],
    }
    # the fp leaf with 1 error in 100 rows, on each of the three dates
    assert result.summary == "9 leaves, 3 in the suspicious zone"
    assert plots.leaves(pool, gid, CUT, min_support=101).summary == (
        "9 leaves, 0 in the suspicious zone"
    )


def test_recurrence_draws_each_signature_on_its_dates(
    db_pool_greenfield, diagnosed, tmp_path
):
    pool = db_pool_greenfield
    gid = diagnosed.group.model_group_id
    result = plots.recurrence(pool, gid, CUT, "fn")
    found = readers.error_recurrence(pool, gid, CUT, "fn", 12)
    assert result.data["rules"] == found["rules"]
    assert result.data["leaves"] == readers.group_leaves(pool, gid, CUT, ("fn",))
    assert found["rules"][0]["n_dates"] == 3
    assert result.summary.endswith("the most recurrent on 3 of 3")


# -------------------------------------------------------------------------- crosstabs


def test_crosstabs_draws_the_readers_ranking(db_pool_greenfield, diagnosed, tmp_path):
    pool = db_pool_greenfield
    a, b = diagnosed.group.model_a, diagnosed.group.model_b
    result = plots.crosstabs(pool, a)
    # the only cut, the latest date
    assert result.data == {
        f"model {a}": readers.crosstabs(pool, a, CUT, "mean", 15, D2)
    }

    nonzero = plots.crosstabs(pool, a, CUT, D1, "nonzero_rate", top=3)
    assert nonzero.data == {
        f"model {a}": readers.crosstabs(pool, a, CUT, "nonzero_rate", 3, D1)
    }
    assert [r["feature"] for r in nonzero.data[f"model {a}"]] == [
        "visits",
        "violations",
        "zip_60622",
    ]

    pair = plots.crosstabs(pool, a, CUT, D1, "mean", compare=b, top=3)
    first = readers.crosstabs(pool, a, CUT, "mean", 3, D1)
    other = {r["feature"]: r for r in readers.crosstabs(pool, b, CUT, "mean", ALL, D1)}
    assert pair.data == {
        f"model {a}": first,
        f"model {b}": [other[r["feature"]] for r in first],
    }
    plots.crosstabs(pool, a, CUT, D1, "nonzero_rate", compare=b)


def test_crosstabs_draws_only_the_named_features(db_pool_greenfield, diagnosed):
    """features= picks rows out of the whole ranking, in its order, and ignores top."""
    pool = db_pool_greenfield
    a = diagnosed.group.model_a
    ranking = readers.crosstabs(pool, a, CUT, "nonzero_rate", ALL, D1)
    named = [ranking[-1]["feature"], ranking[0]["feature"]]  # given out of order
    result = plots.crosstabs(pool, a, CUT, D1, "nonzero_rate", top=1, features=named)
    assert result.data == {f"model {a}": [ranking[0], ranking[-1]]}
    assert "2 features by nonzero rate" in result.summary
    with pytest.raises(ValueError, match="Not in model .*'s crosstabs .*: nowhere"):
        plots.crosstabs(pool, a, CUT, D1, features=[ranking[0]["feature"], "nowhere"])


def test_crosstabs_dates_reads_each_model_and_date(
    db_pool_greenfield, diagnosed, tmp_path
):
    pool = db_pool_greenfield
    g = diagnosed.group
    result = plots.crosstabs_dates(pool, g.model_group_id)
    pairs = [(g.model_a, D1), (g.model_b, D1), (g.model_b, D3), (g.model_a, D2)]
    pairs.sort(key=lambda p: (p[1], p[0]))
    by_pair = {
        (m, d): {
            r["feature"]: r for r in readers.crosstabs(pool, m, CUT, "mean", ALL, d)
        }
        for m, d in pairs
    }
    top = [r["feature"] for r in readers.crosstabs(pool, g.model_a, CUT, "mean", 4, D2)]
    assert list(result.data) == top
    for feature in top:
        assert result.data[feature] == [
            {"model_id": m, "as_of_date": d, "row": by_pair[(m, d)].get(feature)}
            for m, d in pairs
        ]
    nonzero = plots.crosstabs_dates(pool, g.model_group_id, CUT, "nonzero_rate", 2)
    assert list(nonzero.data) == [
        r["feature"]
        for r in readers.crosstabs(pool, g.model_a, CUT, "nonzero_rate", 2, D2)
    ]


# ---------------------------------------------------------------------------- overlap


def test_overlap_pairs_the_groups_by_training_window(
    db_pool_greenfield, diagnosed, tmp_path
):
    pool = db_pool_greenfield
    g = diagnosed.group
    result = plots.overlap(pool, g.model_group_id, diagnosed.other_group, CUT)
    # models A and B share model C's training window; only A scored C's date
    assert result.data == {
        f"model {g.model_a} vs model {diagnosed.model_c}": readers.list_overlap(
            pool, g.model_a, diagnosed.model_c, CUT, "test"
        )
    }
    assert result.summary.startswith(f"1 shared test date(s) at {CUT}: Jaccard")


# ----------------------------------------------------------------------------- errors


def test_more_than_eight_series_is_refused(db_pool_greenfield, group):  # noqa: F811
    pool = db_pool_greenfield
    with pool.connection() as conn:
        for i in range(8):
            conn.execute(
                "insert into triage.subsets (subset_hash, config) values (%(h)s, %(c)s)",
                {"h": f"extra-{i}", "c": json.dumps({"name": f"extra {i}"})},
            )
    with pytest.raises(ValueError, match="cap is 8 series.*fewer --subset"):
        plots.curve(pool, group.model_a, D1, [f"extra {i}" for i in range(8)])
    with pytest.raises(ValueError, match="9 features.*cap is 8"):
        plots.crosstabs_dates(pool, group.model_group_id, top=9)


def test_nothing_persisted_names_the_command_that_creates_it(
    db_pool_greenfield,
    group,  # noqa: F811
):
    pool = db_pool_greenfield
    m, gid = group.model_a, group.model_group_id
    for draw, command in (
        (lambda: plots.error_rules(pool, m), "triage postmodel error-tree"),
        (lambda: plots.leaves(pool, gid, CUT), "triage postmodel error-tree"),
        (lambda: plots.recurrence(pool, gid, CUT), "triage postmodel error-tree"),
        (lambda: plots.crosstabs(pool, m), "triage postmodel crosstabs"),
        (lambda: plots.crosstabs_dates(pool, gid), "triage postmodel crosstabs"),
        (lambda: plots.curve(pool, group.model_b, D1), "triage run"),
    ):
        with pytest.raises(NothingToDraw, match=command):
            draw()
    assert issubclass(NothingToDraw, LookupError)


def test_unknown_ids_are_bad_input(db_pool_greenfield, group):  # noqa: F811
    pool = db_pool_greenfield
    for draw in (
        lambda: plots.curve_dates(pool, 999999),
        lambda: plots.leaves(pool, 999999, CUT),
        lambda: plots.recurrence(pool, 999999, CUT),
        lambda: plots.crosstabs_dates(pool, 999999),
        lambda: plots.overlap(pool, group.model_group_id, 999999, CUT),
        lambda: plots.curve(pool, group.model_a, D1, ["nowhere"]),
    ):
        with pytest.raises(readers.NotFoundError):
            draw()


def test_a_subset_given_twice_is_refused(db_pool_greenfield, group):  # noqa: F811
    """One series per population: a repeat would overwrite its own series."""
    with pytest.raises(ValueError, match="given twice"):
        plots.curve(
            db_pool_greenfield, group.model_a, D1, subsets=[SUBSET_NAME, SUBSET[:6]]
        )


def test_crosstabs_compare_names_the_features_it_leaves_out(
    db_pool_greenfield, diagnosed
):
    """A feature the compared model has no row for cannot be paired: it is left out,
    and the summary says which."""
    pool = db_pool_greenfield
    a, b = diagnosed.group.model_a, diagnosed.group.model_b
    first = readers.crosstabs(pool, a, CUT, "mean", 3, D1)
    dropped = first[0]["feature"]
    with pool.connection() as conn:
        conn.execute(
            "delete from triage.crosstabs where model_id = %(b)s and feature = %(f)s",
            {"b": b, "f": dropped},
        )
    pair = plots.crosstabs(pool, a, CUT, D1, "mean", compare=b, top=3)
    assert [r["feature"] for r in pair.data[f"model {a}"]] == [
        r["feature"] for r in first[1:]
    ]
    assert (
        f"1 of model {a}'s top 3 left out, not in model {b}'s crosstabs: {dropped}"
        in pair.summary
    )
