"""Coverage and comparing model groups on common rows (migration 0031).

Three groups of one regression experiment score two test dates of 100 labeled rows. Group A
scores all 100, group B abstains on rows 96-100 and group C on rows 81-100: the hard rows,
where every model misses by 40. On its own rows C looks best (it skipped them); on the 80
rows all three scored, A is best. Audition must pick C by default and A on common rows.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date

import numpy as np
import pytest
from typer.testing import CliRunner

import triage.cli as cli
from triage.adapters.common_rows import evaluate_on_common_rows
from triage.component.catwalk.in_pg_evaluation import AUDITION_RULES, evaluate_in_db
from triage.component.results_schema import downgrade_db, upgrade_db
from triage.diagnostics import readers

EXP = "exp-common"
TS = "1 month"
DATES = (date(2015, 1, 1), date(2015, 2, 1))
Y = np.array([10.0] * 80 + [100.0] * 20)
#: group → (rows it scores, its error on rows 1-80, its score on the hard rows)
GROUPS = {"A": (100, 0.5, 60.0), "B": (95, 1.0, 60.0), "C": (80, 1.5, 60.0)}
METRICS = {"regression_metrics": ["mae", "rmse", "wape"]}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _scores(name: str) -> np.ndarray:
    n, err, hard = GROUPS[name]
    s = np.concatenate([Y[:80] + err, np.full(20, hard)])
    return s[:n]


def _seed(pool) -> dict[str, int]:
    """Group name → model_group_id. One model per group, scoring both dates."""
    matrix_uuid = str(uuid.uuid4())
    groups: dict[str, int] = {}
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values (%(h)s, '{}'::jsonb, 'regression')",
            {"h": EXP},
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile) values (%(h)s, 'local')"
            " returning run_id",
            {"h": EXP},
        ).fetchone()["run_id"]
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) values"
            " ('cr-labels', 'cr-labels', 'labels', '{}'::jsonb),"
            " ('cr-matrix', 'cr-matrix', 'matrix', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.artifact_inputs (artifact_id, parent_id)"
            " values ('cr-matrix', 'cr-labels')"
        )
        conn.execute(
            "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind,"
            " storage_uri, label_timespan) values (%(u)s, 'cr-matrix', 'test', 'mem://c',"
            " cast(%(ts)s as interval))",
            {"u": matrix_uuid, "ts": TS},
        )
        with conn.cursor() as cur:
            for d in DATES:
                cur.executemany(
                    "insert into triage.labels (label_hash, entity_id, as_of_date,"
                    " label_timespan, outcome) values ('cr-labels', %(e)s, %(d)s,"
                    " cast(%(ts)s as interval), %(o)s)",
                    [
                        {"e": e, "d": d, "ts": TS, "o": float(y)}
                        for e, y in enumerate(Y, start=1)
                    ],
                )
        for name in GROUPS:
            conn.execute(
                "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
                " values (%(a)s, %(a)s, 'model', '{}'::jsonb)",
                {"a": f"cr-model-{name}"},
            )
            gid = conn.execute(
                "insert into triage.model_groups"
                " (model_group_hash, model_type, hyperparameters, feature_list)"
                " values (%(h)s, 'sklearn.dummy.DummyRegressor', %(hp)s::jsonb,"
                " array['f']) returning model_group_id",
                {"h": f"cr-{name}", "hp": json.dumps({"name": name})},
            ).fetchone()["model_group_id"]
            model_id = conn.execute(
                "insert into triage.models (model_group_id, model_hash, run_id,"
                " train_end_time, training_label_timespan)"
                " values (%(g)s, %(a)s, %(r)s, date '2014-12-01', cast(%(ts)s as interval))"
                " returning model_id",
                {"g": gid, "a": f"cr-model-{name}", "r": run_id, "ts": TS},
            ).fetchone()["model_id"]
            with conn.cursor() as cur:
                for d in DATES:
                    cur.executemany(
                        "insert into triage.predictions (model_id, entity_id, as_of_date,"
                        " split_kind, score, matrix_uuid)"
                        " values (%(m)s, %(e)s, %(d)s, 'test', %(s)s, %(u)s)",
                        [
                            {
                                "m": model_id,
                                "e": e,
                                "d": d,
                                "s": float(s),
                                "u": matrix_uuid,
                            }
                            for e, s in enumerate(_scores(name), start=1)
                        ],
                    )
            groups[name] = gid
    for name, gid in groups.items():
        model_id = _model(pool, gid)
        for d in DATES:
            evaluate_in_db(pool, model_id, d, TS, metric_config=METRICS)
    return groups


def _model(pool, group_id: int) -> int:
    with pool.connection() as conn:
        return conn.execute(
            "select model_id from triage.models where model_group_id = %(g)s"
            " order by model_id limit 1",
            {"g": group_id},
        ).fetchone()["model_id"]


def _pick(pool, rule: str = "best_average_value", subset: str = "") -> int:
    params = dict(dict(AUDITION_RULES)[rule])
    with pool.connection() as conn:
        return conn.execute(
            "select triage.audition_pick(%(h)s, 'mae', '', %(r)s, %(p)s::jsonb,"
            " null, null, %(s)s) as g",
            {"h": EXP, "r": rule, "p": json.dumps(params), "s": subset},
        ).fetchone()["g"]


# ---------------------------------------------------------------------- the SQL


def test_common_rows_are_the_rows_every_group_scored(db_pool_greenfield):
    pool = db_pool_greenfield
    groups = _seed(pool)
    rows = readers.common_rows(pool, groups.values())
    assert len(rows) == 6  # 3 groups x 2 dates
    by_group = {
        name: [r for r in rows if r["model_group_id"] == g]
        for name, g in groups.items()
    }
    for name, group_rows in by_group.items():
        scores = _scores(name)
        for r in group_rows:
            assert r["n_labeled"] == 100 and r["n_common"] == 80
            assert r["n_scored_own"] == GROUPS[name][0]
            assert r["mae_own"] == pytest.approx(
                np.abs(scores - Y[: len(scores)]).mean()
            )
            assert r["mae_common"] == pytest.approx(np.abs(scores[:80] - Y[:80]).mean())
            assert r["bias_common"] == pytest.approx((scores[:80] - Y[:80]).mean())
            assert r["wape_common"] == pytest.approx(
                np.abs(scores[:80] - Y[:80]).sum() / Y[:80].sum()
            )


def test_coverage_is_the_share_of_labeled_rows_scored(db_pool_greenfield):
    pool = db_pool_greenfield
    groups = _seed(pool)
    got = {r["model_group_id"]: r for r in readers.coverage(pool, EXP)}
    for name, g in groups.items():
        assert got[g]["coverage"] == pytest.approx(GROUPS[name][0] / 100)
        assert got[g]["n_dates"] == 2
    assert readers.coverage_differs(r["coverage"] for r in got.values())
    rows = readers.model_groups(pool, EXP, "mae", "")
    assert {r["model_group_id"]: r["coverage"] for r in rows} == pytest.approx(
        {g: GROUPS[n][0] / 100 for n, g in groups.items()}
    )


def test_the_common_rows_subset_is_idempotent(db_pool_greenfield):
    pool = db_pool_greenfield
    groups = _seed(pool)
    first = evaluate_on_common_rows(pool, EXP)
    second = evaluate_on_common_rows(pool, EXP, list(reversed(groups.values())))
    assert first.subset_hash == second.subset_hash
    assert first.subset_hash.startswith("common:")
    assert (first.n_rows, first.n_dates) == (160, 2)
    assert first.name == "rows scored by groups " + ", ".join(
        map(str, sorted(groups.values()))
    )
    with pool.connection() as conn:
        n = conn.execute(
            "select count(*) as n from triage.subset_members where subset_hash = %(h)s",
            {"h": first.subset_hash},
        ).fetchone()["n"]
    assert n == 160


def test_audition_on_common_rows_picks_another_group(db_pool_greenfield):
    pool = db_pool_greenfield
    groups = _seed(pool)
    assert _pick(pool) == groups["C"]  # it skipped the hard rows
    common = evaluate_on_common_rows(pool, EXP)
    assert _pick(pool, subset=common.subset_hash) == groups["A"]
    assert _pick(pool, "best_current_value", common.subset_hash) == groups["A"]
    assert _pick(pool) == groups["C"]  # the full-cohort pick is untouched


def _rerun(pool, group_id: int, *, status: str, started: str, offset: float) -> int:
    """A later Run of the Experiment refits the group on the same split; its model's id.

    The refit scores the group's rows ``offset`` above the original on both dates.
    """
    original = _model(pool, group_id)
    with pool.connection() as conn:
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile, status, started_at)"
            " values (%(h)s, 'local', %(s)s, %(t)s::timestamptz) returning run_id",
            {"h": EXP, "s": status, "t": started},
        ).fetchone()["run_id"]
        artifact = f"cr-rerun-{run_id}"
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " values (%(a)s, %(a)s, 'model', '{}'::jsonb)",
            {"a": artifact},
        )
        model_id = conn.execute(
            "insert into triage.models (model_group_id, model_hash, run_id,"
            " train_end_time, training_label_timespan)"
            " select model_group_id, %(a)s, %(r)s, train_end_time, training_label_timespan"
            " from triage.models where model_id = %(m)s returning model_id",
            {"a": artifact, "r": run_id, "m": original},
        ).fetchone()["model_id"]
        conn.execute(
            "insert into triage.predictions (model_id, entity_id, as_of_date,"
            " split_kind, score, matrix_uuid)"
            " select %(m)s, entity_id, as_of_date, split_kind, score + %(o)s, matrix_uuid"
            " from triage.predictions where model_id = %(orig)s",
            {"m": model_id, "o": offset, "orig": original},
        )
    for d in DATES:
        evaluate_in_db(pool, model_id, d, TS, metric_config=METRICS)
    return model_id


def _current(pool, group_id: int) -> list[int]:
    with pool.connection() as conn:
        return [
            r["model_id"]
            for r in conn.execute(
                "select model_id from triage.current_models"
                " where model_group_id = %(g)s order by model_id",
                {"g": group_id},
            ).fetchall()
        ]


def test_a_later_run_replaces_the_group_model_on_its_split(db_pool_greenfield):
    """Issue #69: two Runs on one split leave one model per group and date to compare."""
    pool = db_pool_greenfield
    groups = _seed(pool)
    original = _model(pool, groups["A"])
    with pool.connection() as conn:
        conn.execute(
            "update triage.runs set started_at = '2026-01-01', status = 'completed'"
        )
    refit = _rerun(
        pool, groups["A"], status="completed", started="2026-02-01", offset=2.0
    )

    assert _current(pool, groups["A"]) == [refit]
    rows = [
        r
        for r in readers.common_rows(pool, groups.values())
        if r["model_group_id"] == groups["A"]
    ]
    assert {r["model_id"] for r in rows} == {refit}
    assert rows[0]["mae_common"] == pytest.approx(0.5 + 2.0)
    with pool.connection() as conn:
        audition = conn.execute(
            "select n_splits_evaluated from triage.audition"
            " where experiment_hash = %(h)s and model_group_id = %(g)s"
            "   and metric = 'mae' and parameter = ''",
            {"h": EXP, "g": groups["A"]},
        ).fetchone()
    assert audition["n_splits_evaluated"] == len(DATES)
    assert original not in _current(pool, groups["A"])


def test_a_run_that_did_not_complete_does_not_replace_a_completed_one(
    db_pool_greenfield,
):
    pool = db_pool_greenfield
    groups = _seed(pool)
    original = _model(pool, groups["A"])
    with pool.connection() as conn:
        conn.execute(
            "update triage.runs set started_at = '2026-01-01', status = 'completed'"
        )
    _rerun(pool, groups["A"], status="started", started="2026-02-01", offset=2.0)
    _rerun(pool, groups["A"], status="failed", started="2026-03-01", offset=3.0)

    assert _current(pool, groups["A"]) == [original]


def test_a_run_that_reuses_a_model_makes_it_current_again(db_pool_greenfield):
    """A cache hit is a use: the latest attempt's model is the one it reused."""
    pool = db_pool_greenfield
    groups = _seed(pool)
    original = _model(pool, groups["A"])
    with pool.connection() as conn:
        conn.execute(
            "update triage.runs set started_at = '2026-01-01', status = 'completed'"
        )
    _rerun(pool, groups["A"], status="completed", started="2026-02-01", offset=2.0)
    with pool.connection() as conn:
        reuse = conn.execute(
            "insert into triage.runs (experiment_hash, profile, status, started_at)"
            " values (%(h)s, 'local', 'completed', '2026-03-01') returning run_id",
            {"h": EXP},
        ).fetchone()["run_id"]
        conn.execute(
            "insert into triage.run_artifacts (run_id, artifact_id)"
            " values (%(r)s, 'cr-model-A')",
            {"r": reuse},
        )

    assert _current(pool, groups["A"]) == [original]


def test_a_group_with_two_models_on_a_date_is_refused(db_pool_greenfield):
    """Two current models of one group still may not score one date.

    Here they are two splits whose test dates overlap, so neither replaces the other.
    """
    pool = db_pool_greenfield
    groups = _seed(pool)
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " values ('cr-twin', 'cr-twin', 'model', '{}'::jsonb)"
        )
        twin = conn.execute(
            "insert into triage.models (model_group_id, model_hash, run_id,"
            " train_end_time, training_label_timespan)"
            " select model_group_id, 'cr-twin', run_id, date '2014-11-01',"
            " training_label_timespan from triage.models"
            " where model_id = %(m)s returning model_id",
            {"m": _model(pool, groups["A"])},
        ).fetchone()["model_id"]
        conn.execute(
            "insert into triage.predictions (model_id, entity_id, as_of_date, split_kind,"
            " score) values (%(m)s, 1, %(d)s, 'test', 1.0)",
            {"m": twin, "d": DATES[0]},
        )
    with pytest.raises(Exception, match="has 2 models scoring"):
        readers.common_rows(pool, groups.values())


def test_default_audition_is_unchanged_by_the_migration(db_pool_greenfield, db_url):
    """Every full-cohort audition result at 0030 equals the one at head, subset rows present."""
    pool = db_pool_greenfield
    _seed(pool)
    evaluate_on_common_rows(pool, EXP)

    def snapshot():
        with pool.connection() as conn:
            audition = conn.execute(
                "select * from triage.audition order by model_group_id, metric, parameter"
            ).fetchall()
            distances = conn.execute(
                "select * from triage.audition_distances"
                " order by model_group_id, metric, as_of_date"
            ).fetchall()
            windowed = conn.execute(
                "select * from triage.audition_windowed(%(h)s, 'mae', '')"
                " order by model_group_id",
                {"h": EXP},
            ).fetchall()
            selected = conn.execute(
                "select * from triage.selected_model(%(h)s, 'mae', '')", {"h": EXP}
            ).fetchall()
            picks = [
                conn.execute(
                    "select triage.audition_pick(%(h)s, 'mae', '', %(r)s, %(p)s::jsonb)"
                    " as g",
                    {"h": EXP, "r": rule, "p": json.dumps(
                        {**params, "metric2": "rmse", "metric1_weight": 0.5}
                        if rule == "best_average_two_metrics" else params
                    )},
                ).fetchone()["g"]
                for rule, params in AUDITION_RULES
            ]  # fmt: skip
        return audition, distances, windowed, selected, picks

    at_head = snapshot()
    downgrade_db(dburl=db_url, revision="0030_wape_and_forecast_quantile")
    assert snapshot() == at_head
    upgrade_db(dburl=db_url, revision="head")
    assert snapshot() == at_head


# ---------------------------------------------------------------------- the CLI


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


def test_audition_cli_with_common_rows(run_cli, db_pool_greenfield):
    groups = _seed(db_pool_greenfield)
    code, text = run_cli("audition", EXP, "-m", "mae", "-p", "")
    assert code == 0, text
    assert "Coverage" in text and "80.0%" in text
    assert "with --common-rows" in text
    assert f"group {groups['C']}" in text

    code, text = run_cli("audition", EXP, "-m", "mae", "-p", "", "--common-rows")
    assert code == 0, text
    assert "Subset 'rows scored by groups" in text and "160 rows on 2 date(s)" in text
    assert f"group {groups['A']}" in text
    assert "with --common-rows" not in text

    code, text = run_cli(
        "audition", EXP, "-m", "mae", "-p", "", "--common-rows", "--groups", "999999"
    )
    assert code == 2 and "have no model in experiment" in text
    code, text = run_cli("audition", EXP, "--groups", "1,2")
    assert code == 2 and "--groups selects the groups of --common-rows" in text


def test_models_cli_shows_coverage(run_cli, db_pool_greenfield):
    _seed(db_pool_greenfield)
    code, text = run_cli("models", EXP, "-m", "mae", "-p", "")
    assert code == 0, text
    assert "Coverage" in text and "95.0%" in text and "with --common-rows" in text


def test_compare_rows_cli(run_cli, db_pool_greenfield):
    groups = _seed(db_pool_greenfield)
    a, c = groups["A"], groups["C"]
    code, text = run_cli("postmodel", "compare-rows", EXP)
    assert code == 0, text
    assert "Own rows and common rows" in text and "MAE common" in text
    code, text = run_cli(
        "postmodel", "compare-rows", EXP, "--groups", f"{a},{c}", "--json"
    )
    assert code == 0, text
    payload = json.loads(text[text.index("[") :])
    assert {r["n_common"] for r in payload} == {80}
    assert {r["model_group_id"] for r in payload} == {a, c}
    code, text = run_cli("postmodel", "compare-rows", EXP, "--groups", f"{a}")
    assert code == 2 and "two or more model groups" in text
