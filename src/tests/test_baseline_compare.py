"""Where a model group beats its baseline (diagnostics/baseline_compare.py).

200 entities, six monthly test dates, one regression Experiment. Two baseline groups miss by
2 (seasonal naive) and by 5 (persistence) on every row, so the default baseline is the
seasonal naive. Three model groups whose answer is known by construction:

* ``dominant`` misses by 1 everywhere: it wins every segment and every entity;
* ``coin`` misses by 1 or by 3 at random, independently per row: its wins carry no entity
  memory, so the chance test must not reject;
* ``persistent`` misses by 1 on entities 1-100 and by 3 on the rest, every date: the same
  entities win every date, so the chance test must reject.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from dateutil.relativedelta import relativedelta
from typer.testing import CliRunner

import triage.cli as cli
from triage.diagnostics.baseline_compare import (
    ComparisonError,
    compare_to_baseline,
    entity_groups_from_query,
    merge_cells,
    poisson_binomial_pmf,
)

EXP = "exp-baseline"
TS = "1 month"
N = 200
TEST_DATES = tuple(date(2015, 1, 1) + relativedelta(months=k) for k in range(6))
HISTORY = tuple(date(2014, 1, 1) + relativedelta(months=k) for k in range(12))
SEASONAL = "triage.component.catwalk.baselines.timeseries.SeasonalNaive"
PERSISTENCE = "triage.component.catwalk.baselines.timeseries.Persistence"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _level(e: int) -> float:
    """The entity's history level: bands <= 0, (0, 10], (10, 100] and (100, 1000]."""
    return (0.0, 5.0, 50.0, 500.0)[e % 4]


def _misses(name: str, rng: np.random.Generator) -> dict[tuple[int, date], float]:
    """(entity, date) → the group's error, forecast − outcome."""
    out: dict[tuple[int, date], float] = {}
    for d in TEST_DATES:
        for e in range(1, N + 1):
            if name == "seasonal":
                out[(e, d)] = 2.0
            elif name == "persistence":
                out[(e, d)] = 5.0
            elif name == "dominant":
                out[(e, d)] = 1.0
            elif name == "coin":
                out[(e, d)] = 1.0 if rng.random() < 0.5 else 3.0
            else:  # persistent
                out[(e, d)] = 1.0 if e <= N // 2 else 3.0
    return out


#: group name → model class
GROUPS = {
    "seasonal": SEASONAL,
    "persistence": PERSISTENCE,
    "dominant": "sklearn.linear_model.Ridge",
    "coin": "sklearn.linear_model.Lasso",
    "persistent": "sklearn.tree.DecisionTreeRegressor",
}


def _seed(pool, seed: int = 7) -> dict[str, int]:
    """Group name → model_group_id; a Subset of the odd entities, evaluated."""
    rng = np.random.default_rng(seed)
    matrix_uuid = str(uuid.uuid4())
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
            " ('bc-labels', 'bc-labels', 'labels', '{}'::jsonb),"
            " ('bc-matrix', 'bc-matrix', 'matrix', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.artifact_inputs (artifact_id, parent_id)"
            " values ('bc-matrix', 'bc-labels')"
        )
        conn.execute(
            "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind,"
            " storage_uri, label_timespan) values (%(u)s, 'bc-matrix', 'test', 'mem://b',"
            " cast(%(ts)s as interval))",
            {"u": matrix_uuid, "ts": TS},
        )
        with conn.cursor() as cur:
            cur.executemany(
                "insert into triage.labels (label_hash, entity_id, as_of_date,"
                " label_timespan, outcome) values ('bc-labels', %(e)s, %(d)s,"
                " cast(%(ts)s as interval), %(o)s)",
                [
                    {"e": e, "d": d, "ts": TS, "o": _level(e)}
                    for d in HISTORY + TEST_DATES
                    for e in range(1, N + 1)
                ],
            )
        groups: dict[str, int] = {}
        for name, model_type in GROUPS.items():
            gid = conn.execute(
                "insert into triage.model_groups"
                " (model_group_hash, model_type, hyperparameters, feature_list)"
                " values (%(h)s, %(t)s, '{}'::jsonb, array['f'])"
                " returning model_group_id",
                {"h": f"bc-{name}", "t": model_type},
            ).fetchone()["model_group_id"]
            conn.execute(
                "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
                " values (%(a)s, %(a)s, 'model', '{}'::jsonb)",
                {"a": f"bc-model-{name}"},
            )
            model_id = conn.execute(
                "insert into triage.models (model_group_id, model_hash, run_id,"
                " train_end_time, training_label_timespan)"
                " values (%(g)s, %(a)s, %(r)s, date '2014-12-01', cast(%(ts)s as interval))"
                " returning model_id",
                {"g": gid, "a": f"bc-model-{name}", "r": run_id, "ts": TS},
            ).fetchone()["model_id"]
            misses = _misses(name, rng)
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
                            "s": _level(e) + miss,
                            "u": matrix_uuid,
                        }
                        for (e, d), miss in misses.items()
                    ],  # fmt: skip
                )
            groups[name] = gid
        conn.execute(
            "insert into triage.subsets (subset_hash, config)"
            " values ('bc-odd', '{\"name\": \"odd-ids\"}'::jsonb)"
        )
        conn.execute(
            "insert into triage.subset_members (subset_hash, entity_id, as_of_date)"
            " select 'bc-odd', e, d from generate_series(1, %(n)s, 2) e,"
            " unnest(%(d)s::date[]) d",
            {"n": N, "d": list(TEST_DATES)},
        )
        conn.execute(
            "insert into triage.evaluations (model_id, split_kind, as_of_date,"
            " subset_hash, metric, parameter, value)"
            " select model_id, 'test', %(d)s, 'bc-odd', 'mae', '', 1.0 from triage.models"
            " where model_hash = 'bc-model-dominant'",
            {"d": TEST_DATES[0]},
        )
    return groups


# ------------------------------------------------------------------ the statistics


def test_poisson_binomial_is_the_binomial_at_equal_p():
    from scipy.stats import binom

    assert poisson_binomial_pmf([0.3] * 6) == pytest.approx(binom.pmf(range(7), 6, 0.3))
    assert poisson_binomial_pmf([0.2, 0.9]).sum() == pytest.approx(1.0)


def test_merge_cells_reaches_the_floor_and_keeps_the_totals():
    observed = np.array([1.0, 2.0, 40.0, 50.0, 3.0, 4.0])
    expected = np.array([0.5, 3.0, 45.0, 45.0, 4.0, 2.5])
    obs, exp, edges = merge_cells(observed, expected)
    assert (exp >= 5).all()
    assert obs.sum() == observed.sum() and exp.sum() == pytest.approx(expected.sum())
    assert edges[0][0] == 0 and edges[-1][1] == 5  # the cells still cover 0…5


# ------------------------------------------------------------------ the comparisons


def test_the_default_baseline_has_the_lowest_shared_mae(db_pool_greenfield):
    groups = _seed(db_pool_greenfield)
    result = compare_to_baseline(db_pool_greenfield, groups["dominant"])
    assert result.baseline_group_id == groups["seasonal"] and result.baseline_chosen
    assert result.n_rows == N * len(TEST_DATES)
    assert result.dates == list(TEST_DATES)


def test_a_dominant_model_wins_every_segment_and_entity(db_pool_greenfield):
    groups = _seed(db_pool_greenfield)
    result = compare_to_baseline(db_pool_greenfield, groups["dominant"])
    assert result.segment_win_share == 1.0
    assert all(r["closer_share"] == 1.0 for r in result.segments)
    assert {r["cut"] for r in result.segments} == {
        "band", "sbc", "cold_start", "subset:odd-ids",
    }  # fmt: skip
    band = {r["segment"]: r for r in result.segments if r["cut"] == "band"}
    assert list(band) == ["<= 0", "(0, 10]", "(10, 100]", "(100, 1000]"]
    assert band["(10, 100]"]["mae_m"] == pytest.approx(1.0)
    assert band["(10, 100]"]["mae_b"] == pytest.approx(2.0)
    assert band["(10, 100]"]["excess_abs_error"] == pytest.approx(-N // 4 * 6)
    assert band["(10, 100]"]["excess_share"] is None  # a won segment adds no excess
    assert result.entities["won_share"] == 1.0
    assert result.entities["median_error_ratio"] == pytest.approx(0.5)
    oot = result.out_of_time
    assert (
        oot is not None and oot["n_leaders"] == N and oot["mae_m"] == pytest.approx(1)
    )


def test_coin_flip_wins_do_not_reject_chance(db_pool_greenfield):
    groups = _seed(db_pool_greenfield)
    chance = compare_to_baseline(db_pool_greenfield, groups["coin"]).chance
    assert chance is not None and chance["n_entities"] == N and chance["n_dates"] == 6
    assert sum(chance["observed"]) == N
    assert chance["p_value"] > 0.05, chance
    assert 0.35 < chance["p_win_last"] < 0.65


def test_persistent_winners_reject_chance(db_pool_greenfield):
    groups = _seed(db_pool_greenfield)
    result = compare_to_baseline(db_pool_greenfield, groups["persistent"])
    chance = result.chance
    assert chance is not None
    assert chance["observed"][0] == N // 2 and chance["observed"][6] == N // 2
    assert chance["p_value"] < 1e-6
    assert chance["p_win_last_given_all_earlier"] == 1.0
    assert result.entities["won_share"] == pytest.approx(0.5)
    oot = result.out_of_time
    assert oot is not None and oot["n_leaders"] == N // 2
    assert oot["mae_m"] == pytest.approx(1.0) and oot["mae_b"] == pytest.approx(2.0)


def test_a_subset_is_a_cut(db_pool_greenfield):
    groups = _seed(db_pool_greenfield)
    result = compare_to_baseline(
        db_pool_greenfield, groups["persistent"], cut="subset:odd-ids"
    )
    assert [(r["segment"], r["n"]) for r in result.segments] == [
        ("in subset", N // 2 * 6),
        ("outside subset", N // 2 * 6),
    ]
    with pytest.raises(ComparisonError, match="not evaluated on a Subset named"):
        compare_to_baseline(db_pool_greenfield, groups["persistent"], cut="subset:x")


def test_the_entity_group_query_pools_at_a_coarser_level(db_pool_greenfield):
    groups = _seed(db_pool_greenfield)
    pool = db_pool_greenfield
    # ten entities per group key: keys 0-9 hold entities 1-100 (all won), 10-19 the rest
    mapping = entity_groups_from_query(
        pool,
        "select e as entity_id, ((e - 1) / 10)::text as group_key"
        " from generate_series(1, 200) e",
    )
    result = compare_to_baseline(pool, groups["persistent"], entity_groups=mapping)
    coarse = result.entity_groups
    assert coarse is not None and coarse["n"] == 20 and coarse["level"] == "group_key"
    assert coarse["won_share"] == pytest.approx(0.5)
    assert coarse["n_unmapped_rows"] == 0
    with pytest.raises(ComparisonError, match="must return entity_id and group_key"):
        entity_groups_from_query(pool, "select 1 as entity_id")


def test_a_classification_experiment_is_refused(db_pool_greenfield):
    groups = _seed(db_pool_greenfield)
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "update triage.experiments set problem_type = 'classification'"
            " where experiment_hash = %(h)s",
            {"h": EXP},
        )
    with pytest.raises(ComparisonError, match="triage postmodel compare"):
        compare_to_baseline(db_pool_greenfield, groups["dominant"])


# ------------------------------------------------------------------ the CLI


@pytest.fixture
def run_cli(db_url, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def run(*args: str):
        result = CliRunner().invoke(
            cli.app,
            list(args),
            env={"DATABASE_URL": db_url, "COLUMNS": "220"},
            catch_exceptions=False,
        )
        return result.exit_code, " ".join(_ANSI.sub("", result.output).split())

    return run


def test_baseline_cli(run_cli, db_pool_greenfield, tmp_path: Path):
    groups = _seed(db_pool_greenfield)
    g, s = groups["persistent"], groups["seasonal"]
    code, text = run_cli("postmodel", "baseline", str(g))
    assert code == 0, text
    for fragment in (
        f"Group {g} against baseline group {s} (chosen",
        "Model against baseline, per segment",
        "Entities: 200 — model wins 50.0%",
        "Dates won per entity — 200 entities on all 6 dates",
        "P(win last | won all 5 earlier) 100.0% over 100 entities",
        "Out of time: the 100 entities",
    ):
        assert fragment in text, fragment
    query = tmp_path / "groups.sql"
    query.write_text(
        "select e as entity_id, (e % 2)::text as group_key from generate_series(1, 200) e"
    )
    code, text = run_cli(
        "postmodel", "baseline", str(g), "--baseline", str(groups["persistence"]),
        "--cut", "band", "--entity-group-query", str(query), "--json",
    )  # fmt: skip
    assert code == 0, text
    payload = json.loads(text[text.index("{") :])
    assert payload["baseline_chosen"] is False
    assert {r["cut"] for r in payload["segments"]} == {"band"}
    assert payload["entity_groups"]["n"] == 2
    code, text = run_cli("postmodel", "baseline", str(g), "--cut", "decile")
    assert code == 2 and "is not a cut" in text


def test_baseline_cli_with_one_shared_date_exits_1(run_cli, db_pool_greenfield):
    groups = _seed(db_pool_greenfield)
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "delete from triage.predictions where as_of_date > %(d)s",
            {"d": TEST_DATES[0]},
        )
    code, text = run_cli("postmodel", "baseline", str(groups["dominant"]))
    assert code == 1
    assert "the chance and out-of-time tables are skipped" in text
