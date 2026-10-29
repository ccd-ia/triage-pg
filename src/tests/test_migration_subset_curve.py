"""The Rayid curve of a subset: ``model_threshold_curve(p_model, p_as_of_date, p_subset_hash)``.

Two consistency properties, in the style of ADR-0033's curve-equals-metric test:

* ``p_subset_hash = ''`` returns, row for row, what the two-argument function returned on the
  same data, snapshot taken before the upgrade;
* within a subset, the curve's precision at k equals that subset's stored ``precision@{k}_abs``
  to 1e-12: both rank the subset's members among themselves (migration 0015's semantics).

Also here: the round trip, which must restore the two-argument function exactly as 0025 wrote
it, and ``triage model curve``, the CLI surface over the function. The revision is looked up
by its name, so renumbering it does not break these tests.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest
from alembic import script
from typer.testing import CliRunner

from triage.component import results_schema
from triage.component.results_schema import downgrade_db, upgrade_db
from tests.catwalk_tests.test_population_topk import (
    AS_OF_DATE,
    AS_OF_DATE_2,
    ENTITY_IDS,
    LABELS,
    LABELS_2,
    SCORES,
    _add_second_test_date,
    _evaluate,
    _link_run,
    _row,
    _seed,
    dssg_reference,
)

# The columns the two-argument function returned; the upgrade adds base_rate after them.
CURVE_COLUMNS = ("k", "pct", "prec", "rec", "tp", "fp", "fn", "tn")

# Subset "north": 8 of the 15 entities, with unlabeled rows and the 0.7 / 0.4 score ties.
SUBSET_HASH = "s1-north"
SUBSET_NAME = "north"
MEMBERS = [1, 3, 4, 6, 9, 10, 11, 14]


def _revisions(db_url) -> tuple[str, str]:
    """``(this revision, the one it revises)``, found by name, not by number."""
    directory = script.ScriptDirectory.from_config(
        results_schema.alembic_config(db_url)
    )
    for rev in directory.walk_revisions():
        if rev.revision.endswith("_subset_threshold_curve"):
            assert isinstance(rev.down_revision, str), rev.down_revision
            return rev.revision, rev.down_revision
    raise AssertionError("no *_subset_threshold_curve revision in the results schema")


def _seed_subset(pool) -> None:
    """Register subset "north" with its members at AS_OF_DATE only."""
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.subsets (subset_hash, config) values (%(h)s, %(c)s::jsonb)",
            {
                "h": SUBSET_HASH,
                "c": json.dumps(
                    {"name": SUBSET_NAME, "query": "select entity_id from t"}
                ),
            },
        )
        for eid in MEMBERS:
            conn.execute(
                "insert into triage.subset_members (subset_hash, entity_id, as_of_date)"
                " values (%(h)s, %(e)s, %(d)s)",
                {"h": SUBSET_HASH, "e": eid, "d": AS_OF_DATE},
            )


def _curve(pool, call: str, **params: object) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        return conn.execute(f"select * from {call} order by k", params).fetchall()


def _base_rate(labels) -> float:
    labeled = [label for label in labels if label is not None]
    return sum(labeled) / len(labeled)


def _signatures(pool) -> dict[str, str]:
    """``{identity arguments: pg_get_functiondef}`` of every model_threshold_curve."""
    with pool.connection() as conn:
        rows = conn.execute(
            "select pg_get_function_identity_arguments(p.oid) as args,"
            "       pg_get_functiondef(p.oid) as def"
            " from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
            " where n.nspname = 'triage' and p.proname = 'model_threshold_curve'"
        ).fetchall()
    return {r["args"]: r["def"] for r in rows}


# ------------------------------------------------------------- '' is the old curve


def test_an_empty_subset_hash_reproduces_the_previous_curve_row_for_row(
    db_pool_greenfield, db_url
):
    pool = db_pool_greenfield
    this, previous = _revisions(db_url)
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)
    _add_second_test_date(pool, model_id)
    _seed_subset(pool)  # a subset exists, and '' must not see it

    # the two-argument function as 0025 created it, not as this revision's downgrade does
    downgrade_db(dburl=db_url, revision="0024_population_topk")
    upgrade_db(dburl=db_url, revision=previous)
    dates = (AS_OF_DATE, AS_OF_DATE_2, None)
    before = {
        d: _curve(
            pool, "triage.model_threshold_curve(%(m)s, %(d)s::date)", m=model_id, d=d
        )
        for d in dates
    }
    before_one_arg = _curve(pool, "triage.model_threshold_curve(%(m)s)", m=model_id)

    upgrade_db(dburl=db_url, revision=this)
    expected_base_rate = {
        AS_OF_DATE: _base_rate(LABELS),
        AS_OF_DATE_2: _base_rate(LABELS_2),
        None: _base_rate(LABELS_2),  # no date: the model's latest test date
    }
    for d in dates:
        assert before[d], f"the snapshot at {d} is empty — the fixture is broken"
        for call in (
            "triage.model_threshold_curve(%(m)s, %(d)s::date)",
            "triage.model_threshold_curve(%(m)s, %(d)s::date, '')",
        ):
            after = _curve(pool, call, m=model_id, d=d)
            assert [{c: r[c] for c in CURVE_COLUMNS} for r in after] == before[d], (
                call,
                d,
            )
            assert [r["base_rate"] for r in after] == pytest.approx(
                [expected_base_rate[d]] * len(after), abs=1e-12
            )
    after_one_arg = _curve(pool, "triage.model_threshold_curve(%(m)s)", m=model_id)
    assert [{c: r[c] for c in CURVE_COLUMNS} for r in after_one_arg] == before_one_arg


def test_the_base_rate_is_the_curve_precision_at_the_whole_population(
    db_pool_greenfield,
):
    """At k = N every labeled row is inside the cut, so precision there is the base rate."""
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)
    _seed_subset(pool)
    for subset in ("", SUBSET_HASH):
        curve = _curve(
            pool,
            "triage.model_threshold_curve(%(m)s, %(d)s::date, %(s)s)",
            m=model_id,
            d=AS_OF_DATE,
            s=subset,
        )
        assert curve[-1]["prec"] == curve[-1]["base_rate"]


# -------------------------------------------------- a subset is its own population


def test_a_subset_curve_reads_what_subset_precision_at_k_stores(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)
    _seed_subset(pool)
    _evaluate(
        pool,
        model_id,
        [f"{k}_abs" for k in range(1, len(MEMBERS) + 1)],
        subset_hash=SUBSET_HASH,
    )

    curve = _curve(
        pool,
        "triage.model_threshold_curve(%(m)s, %(d)s::date, %(s)s)",
        m=model_id,
        d=AS_OF_DATE,
        s=SUBSET_HASH,
    )
    # one point per member: the subset is the population, not a filter on the full list
    assert [p["k"] for p in curve] == list(range(1, len(MEMBERS) + 1))

    idx = [ENTITY_IDS.index(e) for e in MEMBERS]
    sub_scores = [SCORES[i] for i in idx]
    sub_labels = [LABELS[i] for i in idx]
    for point in curve:
        parameter = f"{point['k']}_abs"
        precision = _row(pool, model_id, "precision@", parameter, SUBSET_HASH)
        recall = _row(pool, model_id, "recall@", parameter, SUBSET_HASH)
        if precision["value"] is None:
            assert point["prec"] is None, parameter
        else:
            assert point["prec"] == pytest.approx(precision["value"], abs=1e-12)
        assert point["rec"] == pytest.approx(recall["value"], abs=1e-12)
        assert point["tp"] + point["fp"] == precision["num_labeled_above_threshold"]
        assert point["pct"] == pytest.approx(point["k"] / len(MEMBERS))
        # the base rate is the random-ranking expectation precision@ stores
        assert point["base_rate"] == pytest.approx(
            precision["value_expected"], abs=1e-12
        )
        # and both agree with DSSG's evaluation over the subset alone
        expected = dssg_reference(sub_scores, sub_labels, MEMBERS, parameter)
        if expected["precision"] is None:
            assert point["prec"] is None
        else:
            assert point["prec"] == pytest.approx(expected["precision"], abs=1e-12)
    assert curve[0]["base_rate"] == pytest.approx(_base_rate(sub_labels), abs=1e-12)


def test_a_subset_without_members_at_the_date_draws_no_curve(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)
    _add_second_test_date(pool, model_id)
    _seed_subset(pool)  # members at AS_OF_DATE only
    assert (
        _curve(
            pool,
            "triage.model_threshold_curve(%(m)s, %(d)s::date, %(s)s)",
            m=model_id,
            d=AS_OF_DATE_2,
            s=SUBSET_HASH,
        )
        == []
    )


# --------------------------------------------------------------------- round trip


def test_the_migration_round_trips_and_restores_the_previous_function_verbatim(
    db_pool_greenfield, db_url
):
    pool = db_pool_greenfield
    this, previous = _revisions(db_url)

    downgrade_db(dburl=db_url, revision="0024_population_topk")
    upgrade_db(dburl=db_url, revision=previous)
    as_0025_wrote_it = _signatures(pool)
    assert set(as_0025_wrote_it) == {"p_model bigint, p_as_of_date date"}

    upgrade_db(dburl=db_url, revision=this)
    assert set(_signatures(pool)) == {
        "p_model bigint, p_as_of_date date, p_subset_hash text"
    }

    downgrade_db(dburl=db_url, revision=previous)
    assert _signatures(pool) == as_0025_wrote_it

    upgrade_db(dburl=db_url, revision=this)
    assert set(_signatures(pool)) == {
        "p_model bigint, p_as_of_date date, p_subset_hash text"
    }


# ---------------------------------------------------------- triage model curve


def _cli(db_url, tmp_path, monkeypatch, *args):
    import triage.cli as cli

    monkeypatch.chdir(tmp_path)  # never pick up a repo-root database.yaml / .env
    return CliRunner().invoke(cli.app, list(args), env={"DATABASE_URL": db_url})


def _payload(output: str):
    """The JSON body after the loguru 'Using database' line."""
    return json.loads(output[output.index("{") :])


def test_cli_curve_prints_the_cuts_at_ceil_of_i_n_over_points(
    db_url, db_pool_greenfield, tmp_path, monkeypatch
):
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)

    result = _cli(
        db_url, tmp_path, monkeypatch, "model", "curve", str(model_id), "--points", "3",
        "--json",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    payload = _payload(result.output)
    assert payload["as_of_date"] == AS_OF_DATE  # the latest (only) test date
    assert payload["subset_hash"] == ""
    assert payload["population"] == len(SCORES)
    assert payload["base_rate"] == pytest.approx(_base_rate(LABELS), abs=1e-12)
    n = len(SCORES)
    assert [p["k"] for p in payload["points"]] == [
        math.ceil(i * n / 3) for i in (1, 2, 3)
    ]
    full = {
        r["k"]: r
        for r in _curve(pool, "triage.model_threshold_curve(%(m)s)", m=model_id)
    }
    for p in payload["points"]:
        assert p["prec"] == full[p["k"]]["prec"]
        assert p["rec"] == full[p["k"]]["rec"]

    # more points than rows: every cut, once
    every = _payload(
        _cli(
            db_url,
            tmp_path,
            monkeypatch,
            "model",
            "curve",
            str(model_id),
            "--points",
            "40",
            "--json",
        ).output  # fmt: skip
    )
    assert [p["k"] for p in every["points"]] == list(range(1, n + 1))


def test_cli_curve_over_a_subset_by_name_or_hash_prefix(
    db_url, db_pool_greenfield, tmp_path, monkeypatch
):
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)
    _seed_subset(pool)
    subset_curve = _curve(
        pool,
        "triage.model_threshold_curve(%(m)s, %(d)s::date, %(s)s)",
        m=model_id,
        d=AS_OF_DATE,
        s=SUBSET_HASH,
    )

    for value in (SUBSET_NAME, SUBSET_HASH[:4]):
        result = _cli(
            db_url, tmp_path, monkeypatch, "model", "curve", str(model_id), "--subset",
            value, "--date", AS_OF_DATE, "--points", "4", "--json",
        )  # fmt: skip
        assert result.exit_code == 0, result.output
        payload = _payload(result.output)
        assert payload["subset_hash"] == SUBSET_HASH
        assert payload["subset_name"] == SUBSET_NAME
        assert payload["population"] == len(MEMBERS)
        assert [p["k"] for p in payload["points"]] == [2, 4, 6, 8]
        for p in payload["points"]:
            assert p["prec"] == subset_curve[p["k"] - 1]["prec"]
            assert p["base_rate"] == subset_curve[0]["base_rate"]

    table = _cli(
        db_url, tmp_path, monkeypatch, "model", "curve", str(model_id), "--subset",
        SUBSET_NAME,
    )  # fmt: skip
    assert table.exit_code == 0, table.output
    assert f"subset {SUBSET_NAME}" in table.output
    assert "Base rate" in table.output


def test_cli_curve_fails_loud_on_what_it_cannot_resolve(
    db_url, db_pool_greenfield, tmp_path, monkeypatch
):
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)
    _add_second_test_date(pool, model_id)
    _seed_subset(pool)  # members at AS_OF_DATE only

    def run(*args: str):
        return _cli(db_url, tmp_path, monkeypatch, "model", "curve", *args)

    unknown_subset = run(str(model_id), "--subset", "nowhere")
    assert unknown_subset.exit_code == 1
    assert "No subset matches" in unknown_subset.output

    no_model = run("999999")
    assert no_model.exit_code == 1
    assert "No model with id 999999" in no_model.output

    no_date = run(str(model_id), "--date", "2020-01-01")
    assert no_date.exit_code == 1
    assert "no test predictions" in no_date.output
    assert AS_OF_DATE in no_date.output and AS_OF_DATE_2 in no_date.output

    no_members = run(str(model_id), "--subset", SUBSET_NAME, "--date", AS_OF_DATE_2)
    assert no_members.exit_code == 1
    assert "has no members" in no_members.output

    bad_date = run(str(model_id), "--date", "January")
    assert bad_date.exit_code != 0
    assert "invalid date" in bad_date.output
