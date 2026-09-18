"""audition date window (#13, migration 0022).

``triage.audition`` aggregates every evaluated split, and ``audition_pick`` took no dates, so a
selection window — "the last two seasons", the regime the served model has to survive — could not
be expressed.

Two properties matter more than the feature itself. **Existing five-argument calls must still
resolve**, because `hockey.select_model()` and anything else downstream calls
``audition_pick(h, metric, parameter, rule, params)`` positionally. And **both bounds null must
reproduce the unwindowed answer exactly**, or every existing caller silently changes behaviour.
"""

from __future__ import annotations

from datetime import date

import pytest

from triage.component import results_schema

EXPERIMENT = "expwindow"
METRIC = "auc_roc"
PARAMETER = ""

# Two regimes. Group 1 wins early and collapses; group 2 is mediocre early and wins late.
# Selecting over all of history picks 1; selecting on the last two dates picks 2.
EARLY = [date(2013, 1, 1), date(2013, 7, 1)]
LATE = [date(2014, 1, 1), date(2014, 7, 1)]
VALUES = {
    (1, EARLY[0]): 0.90,
    (1, EARLY[1]): 0.90,
    (1, LATE[0]): 0.50,
    (1, LATE[1]): 0.50,
    (2, EARLY[0]): 0.60,
    (2, EARLY[1]): 0.60,
    (2, LATE[0]): 0.80,
    (2, LATE[1]): 0.80,
}


@pytest.fixture
def auditioned(db_pool_greenfield):
    """Two model groups evaluated over four dates spanning two regimes."""
    engine = db_pool_greenfield
    with engine.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, problem_type, config)"
            " values (%(e)s, 'classification', '{}'::jsonb)",
            {"e": EXPERIMENT},
        )
        run_id = conn.execute(
            "insert into triage.runs (profile, status, purpose, experiment_hash)"
            " values ('local', 'completed', 'experiment', %(e)s) returning run_id",
            {"e": EXPERIMENT},
        ).fetchone()["run_id"]
        for group in (1, 2):
            conn.execute(
                "insert into triage.model_groups (model_group_hash, model_type,"
                " hyperparameters, feature_list) values (%(h)s, 'x.Y', '{}'::jsonb,"
                " ARRAY['f1'])",
                {"h": f"mg-{group}"},
            )
            group_id = conn.execute(
                "select model_group_id from triage.model_groups"
                " where model_group_hash = %(h)s",
                {"h": f"mg-{group}"},
            ).fetchone()["model_group_id"]
            for as_of in EARLY + LATE:
                conn.execute(
                    "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
                    " values (%(a)s, %(l)s, 'model', '{}'::jsonb)",
                    {"a": f"m-{group}-{as_of}", "l": f"ml-{group}-{as_of}"},
                )
                model_id = conn.execute(
                    "insert into triage.models (model_group_id, model_hash, run_id,"
                    " train_end_time, artifact_uri, random_seed, feature_list)"
                    " values (%(g)s, %(h)s, %(r)s, %(t)s, 'file:///m', 0, ARRAY['f1'])"
                    " returning model_id",
                    {
                        "g": group_id,
                        "h": f"m-{group}-{as_of}",
                        "r": run_id,
                        "t": as_of,
                    },
                ).fetchone()["model_id"]
                conn.execute(
                    "insert into triage.evaluations (model_id, split_kind, as_of_date,"
                    " metric, parameter, value, subset_hash)"
                    " values (%(m)s, 'test', %(d)s, %(metric)s, %(p)s, %(v)s, '')",
                    {
                        "m": model_id,
                        "d": as_of,
                        "metric": METRIC,
                        "p": PARAMETER,
                        "v": VALUES[(group, as_of)],
                    },
                )
    return engine


def _pick(
    engine, rule: str, *, bounds: tuple[date | None, date | None] | None = None
) -> int | None:
    with engine.connection() as conn:
        if bounds is None:
            row = conn.execute(
                "select triage.audition_pick(%(e)s, %(m)s, %(p)s, %(r)s, '{}'::jsonb)"
                " as g",
                {"e": EXPERIMENT, "m": METRIC, "p": PARAMETER, "r": rule},
            ).fetchone()
        else:
            row = conn.execute(
                "select triage.audition_pick(%(e)s, %(m)s, %(p)s, %(r)s, '{}'::jsonb,"
                " %(f)s, %(t)s) as g",
                {
                    "e": EXPERIMENT,
                    "m": METRIC,
                    "p": PARAMETER,
                    "r": rule,
                    "f": bounds[0],
                    "t": bounds[1],
                },
            ).fetchone()
    return row["g"]


def test_the_five_argument_call_still_resolves(auditioned) -> None:
    """Every existing caller passes five arguments positionally; the defaults must cover it."""
    assert _pick(auditioned, "best_average_value") is not None


def test_null_bounds_reproduce_the_unwindowed_answer(auditioned) -> None:
    """Both bounds null must not change what any rule returns."""
    for rule in (
        "best_current_value",
        "best_average_value",
        "lowest_metric_variance",
        "best_avg_var_penalized",
        "best_avg_recency_weight",
    ):
        unwindowed = _pick(auditioned, rule)
        explicit_nulls = _pick(auditioned, rule, bounds=(None, None))
        assert unwindowed == explicit_nulls, (
            f"{rule} changed under explicit null bounds"
        )


def test_a_window_changes_the_pick_when_the_ranking_differs_inside_it(
    auditioned,
) -> None:
    """The feature: selecting on the late regime picks the group that wins there."""
    over_history = _pick(auditioned, "best_average_value")
    over_late = _pick(auditioned, "best_average_value", bounds=(LATE[0], LATE[1]))

    assert over_history != over_late, (
        "group 1 averages 0.70 over all four dates and 0.50 over the last two; group 2"
        " averages 0.70 and 0.80 — so the window must change the pick"
    )
    with auditioned.connection() as conn:
        late_winner_type = conn.execute(
            "select model_group_hash from triage.model_groups"
            " where model_group_id = %(g)s",
            {"g": over_late},
        ).fetchone()["model_group_hash"]
    assert late_winner_type == "mg-2"


def test_a_window_containing_no_splits_returns_null_rather_than_raising(
    auditioned,
) -> None:
    assert (
        _pick(
            auditioned,
            "best_average_value",
            bounds=(date(2020, 1, 1), date(2020, 12, 31)),
        )
        is None
    )


def test_a_single_split_window_is_not_a_division_error(auditioned) -> None:
    """``stddev_samp`` over one row is null; the ordering must tolerate it."""
    assert (
        _pick(auditioned, "lowest_metric_variance", bounds=(LATE[0], LATE[0]))
        is not None
    )


def test_audition_windowed_matches_audition_when_unbounded(auditioned) -> None:
    """The windowed function is the view's twin, not a second implementation."""
    with auditioned.connection() as conn:
        view = conn.execute(
            "select model_group_id, n_splits_evaluated, avg_value"
            " from triage.audition where experiment_hash = %(e)s and metric = %(m)s"
            "   and parameter = %(p)s order by model_group_id",
            {"e": EXPERIMENT, "m": METRIC, "p": PARAMETER},
        ).fetchall()
        windowed = conn.execute(
            "select model_group_id, n_splits_evaluated, avg_value"
            " from triage.audition_windowed(%(e)s, %(m)s, %(p)s, null, null)"
            " order by model_group_id",
            {"e": EXPERIMENT, "m": METRIC, "p": PARAMETER},
        ).fetchall()
    assert [dict(r) for r in view] == [dict(r) for r in windowed]


def test_selected_model_takes_the_window(auditioned) -> None:
    with auditioned.connection() as conn:
        row = conn.execute(
            "select * from triage.selected_model(%(e)s, %(m)s, %(p)s,"
            " 'best_average_value', %(f)s, %(t)s)",
            {
                "e": EXPERIMENT,
                "m": METRIC,
                "p": PARAMETER,
                "f": LATE[0],
                "t": LATE[1],
            },
        ).fetchone()
    assert row["audition_group"] is not None


def test_0022_round_trips(db_url, db_pool_greenfield) -> None:
    """downgrade restores the pre-window signatures, upgrade restores the windowed ones."""

    def windowed_exists(engine) -> bool:
        with engine.connection() as conn:
            return (
                conn.execute(
                    "select count(*) as n from pg_proc p"
                    " join pg_namespace n on n.oid = p.pronamespace"
                    " where n.nspname = 'triage' and p.proname = 'audition_windowed'"
                ).fetchone()["n"]
                > 0
            )

    engine = db_pool_greenfield
    assert windowed_exists(engine)

    results_schema.downgrade_db(dburl=db_url, revision="0021_model_feature_geometry")
    assert not windowed_exists(engine), "downgrade must drop audition_windowed"
    with engine.connection() as conn:
        # the pre-window five-argument function is back and callable
        conn.execute(
            "select triage.audition_pick('nope', 'auc_roc', '', 'best_average_value',"
            " '{}'::jsonb)"
        )

    results_schema.upgrade_db(dburl=db_url, revision="0022_audition_window")
    assert windowed_exists(engine)
