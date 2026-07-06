"""Audition single-surface tests (ADR-0007, migration 0013, v1-release plan P1).

The invariant under test: the SQL audition surface carries everything the retired
``component/audition`` computed. ``dist_from_best_case_next_time`` follows DSSG
``distance_from_best.py`` semantics — the regret a group picked at time t realizes at the
NEXT evaluated time (NULL on each group's last split) — checked against a hand-computed
2-group × 3-split fixture. Subset-scoped evaluation rows (``subset_hash <> ''``) must
never contaminate the ranking. ``triage audition`` / ``triage leaderboard`` are golden
Rich-table reads over the same views (ADR-0012 headless parity).
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

EXP = "exp-aud"
D1, D2, D3 = "2026-01-01", "2026-02-01", "2026-03-01"

# auc_roc (higher is better). Best per date: 0.70, 0.90, 0.90.
#   g1 dists: 0.00, 0.10, 0.30  -> next-time: 0.10, 0.30, NULL (avg 0.20, max 0.30)
#   g2 dists: 0.10, 0.00, 0.00  -> next-time: 0.00, 0.00, NULL (avg 0.00, max 0.00)
G1_VALUES = {D1: 0.70, D2: 0.80, D3: 0.60}
G2_VALUES = {D1: 0.60, D2: 0.90, D3: 0.90}


# ------------------------------------------------------------------ seeding


def _seed_audition_fixture(pool):
    """Two model groups, one model each, evaluated (auc_roc) on three test splits."""
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values (%(h)s, '{}'::jsonb, 'classification')",
            {"h": EXP},
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile, status)"
            " values (%(h)s, 'local', 'completed') returning run_id",
            {"h": EXP},
        ).fetchone()["run_id"]
        model_ids = {}
        for tag, values in (("g1", G1_VALUES), ("g2", G2_VALUES)):
            conn.execute(
                "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
                " values (%(a)s, %(a)s, 'model', '{}'::jsonb)",
                {"a": f"model-art-{tag}"},
            )
            group_id = conn.execute(
                "insert into triage.model_groups"
                " (model_group_hash, model_type, hyperparameters, feature_list)"
                " values (%(gh)s, %(mt)s, '{}'::jsonb, ARRAY['f1'])"
                " returning model_group_id",
                {"gh": f"mg-aud-{tag}", "mt": f"sklearn.Fake{tag.upper()}"},
            ).fetchone()["model_group_id"]
            model_id = conn.execute(
                "insert into triage.models"
                " (model_group_id, model_hash, run_id, train_end_time)"
                " values (%(g)s, %(a)s, %(r)s, date '2025-12-01') returning model_id",
                {"g": group_id, "a": f"model-art-{tag}", "r": run_id},
            ).fetchone()["model_id"]
            model_ids[tag] = (group_id, model_id)
            for as_of, value in values.items():
                conn.execute(
                    "insert into triage.evaluations"
                    " (model_id, split_kind, as_of_date, metric, parameter, value)"
                    " values (%(m)s, 'test', %(d)s, 'auc_roc', '', %(v)s)",
                    {"m": model_id, "d": as_of, "v": value},
                )
    return model_ids


# ------------------------------------------------------------------ view math


def test_audition_distances_regret_next_time(db_pool_greenfield):
    ids = _seed_audition_fixture(db_pool_greenfield)
    g1, _ = ids["g1"]
    with db_pool_greenfield.connection() as conn:
        rows = conn.execute(
            "select as_of_date, dist_from_best_case, dist_from_best_case_next_time"
            " from triage.audition_distances"
            " where experiment_hash = %(h)s and model_group_id = %(g)s"
            " order by as_of_date",
            {"h": EXP, "g": g1},
        ).fetchall()
    assert [float(r["dist_from_best_case"]) for r in rows] == pytest.approx(
        [0.00, 0.10, 0.30], abs=1e-12
    )
    assert float(rows[0]["dist_from_best_case_next_time"]) == pytest.approx(0.10)
    assert float(rows[1]["dist_from_best_case_next_time"]) == pytest.approx(0.30)
    assert rows[2]["dist_from_best_case_next_time"] is None  # last split: no next time


def test_audition_aggregates_and_ranking_order(db_pool_greenfield):
    ids = _seed_audition_fixture(db_pool_greenfield)
    g1, _ = ids["g1"]
    g2, _ = ids["g2"]
    with db_pool_greenfield.connection() as conn:
        rows = conn.execute(
            "select model_group_id, n_splits_evaluated, avg_value,"
            "       avg_distance_from_best, max_regret,"
            "       avg_regret_next_time, max_regret_next_time"
            " from triage.audition where experiment_hash = %(h)s"
            " order by avg_distance_from_best asc, max_regret asc",
            {"h": EXP},
        ).fetchall()
    assert [r["model_group_id"] for r in rows] == [g2, g1]  # g2 dominates
    by_group = {r["model_group_id"]: r for r in rows}
    assert by_group[g1]["n_splits_evaluated"] == 3
    assert float(by_group[g1]["avg_value"]) == pytest.approx(0.70)
    assert float(by_group[g1]["avg_regret_next_time"]) == pytest.approx(0.20)
    assert float(by_group[g1]["max_regret_next_time"]) == pytest.approx(0.30)
    assert float(by_group[g2]["avg_regret_next_time"]) == pytest.approx(0.00)
    assert float(by_group[g2]["max_regret_next_time"]) == pytest.approx(0.00)


def test_subset_rows_do_not_contaminate_audition(db_pool_greenfield):
    """A subset-scoped evaluation (subset_hash <> '') must not enter the ranking base —
    otherwise migration 0015's subset rows would silently distort every best_value."""
    ids = _seed_audition_fixture(db_pool_greenfield)
    g1, m1 = ids["g1"]
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "insert into triage.subsets (subset_hash, config)"
            " values ('sub-x', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.evaluations"
            " (model_id, split_kind, as_of_date, subset_hash, metric, parameter, value)"
            " values (%(m)s, 'test', %(d)s, 'sub-x', 'auc_roc', '', 0.99)",
            {"m": m1, "d": D1},
        )
        rows = conn.execute(
            "select best_value, dist_from_best_case from triage.audition_distances"
            " where experiment_hash = %(h)s and model_group_id = %(g)s"
            "   and as_of_date = %(d)s",
            {"h": EXP, "g": g1, "d": D1},
        ).fetchall()
    assert len(rows) == 1  # the subset row itself is excluded
    assert float(rows[0]["best_value"]) == pytest.approx(0.70)  # 0.99 did not leak in
    assert float(rows[0]["dist_from_best_case"]) == pytest.approx(0.00)


def test_migration_0013_roundtrip(db_url, db_pool_greenfield):
    from triage.component.results_schema import downgrade_db, upgrade_db

    def _has_column(conn, view, column):
        return conn.execute(
            "select exists (select 1 from information_schema.columns"
            " where table_schema = 'triage' and table_name = %(v)s"
            "   and column_name = %(c)s) as f",
            {"v": view, "c": column},
        ).fetchone()["f"]

    with db_pool_greenfield.connection() as conn:
        assert _has_column(conn, "audition", "max_regret_next_time")

    # explicit target (not "-1") so later migrations can't silently change what
    # "one step down" means for this test.
    downgrade_db(dburl=db_url, revision="0012_monitoring_views")
    with db_pool_greenfield.connection() as conn:
        assert not _has_column(conn, "audition", "max_regret_next_time")
        assert _has_column(conn, "audition", "max_regret")  # 0005 shape restored

    upgrade_db(dburl=db_url, revision="head")
    with db_pool_greenfield.connection() as conn:
        assert _has_column(conn, "audition", "max_regret_next_time")


# ------------------------------------------------------------------ CLI parity


def _invoke(db_url, tmp_path, monkeypatch, *args):
    import triage.cli as cli

    monkeypatch.chdir(tmp_path)  # never pick up a repo-root database.yaml / .env
    runner = CliRunner()
    return runner.invoke(cli.app, list(args), env={"DATABASE_URL": db_url})


def _json_payload(output: str):
    """Parse the JSON body out of CLI output (skips the loguru banner line)."""
    start = min(i for i in (output.find("{"), output.find("[")) if i >= 0)
    return json.loads(output[start:])


def test_cli_audition_ranking_and_rules(
    db_url, db_pool_greenfield, tmp_path, monkeypatch
):
    ids = _seed_audition_fixture(db_pool_greenfield)
    g2, _ = ids["g2"]
    result = _invoke(db_url, tmp_path, monkeypatch, "audition", EXP)
    assert result.exit_code == 0, result.output
    assert "auc_roc" in result.output  # defaulted metric is reported
    assert "best_average_value" in result.output
    assert "Regret" in result.output  # header may wrap at the runner's 80-col width

    as_json = _invoke(db_url, tmp_path, monkeypatch, "audition", EXP, "--json")
    assert as_json.exit_code == 0, as_json.output
    payload = _json_payload(as_json.output)
    assert payload["metric"] == "auc_roc"
    assert [r["model_group_id"] for r in payload["ranking"]][0] == g2
    rules = {s["rule"] for s in payload["strategies"]}
    assert {"best_current_value", "best_average_value", "random_model_group"} <= rules
    # only one (metric, parameter) pair exists -> two-metrics rule is skipped
    assert "best_average_two_metrics" not in rules
    assert payload["selected"]["audition_group"] == g2


def test_cli_audition_rejects_unknown_rule(db_url, tmp_path, monkeypatch):
    result = _invoke(db_url, tmp_path, monkeypatch, "audition", EXP, "--rule", "nope")
    assert result.exit_code != 0
    assert "unknown audition rule" in result.output


def test_cli_models_groups_and_members(
    db_url, db_pool_greenfield, tmp_path, monkeypatch
):
    ids = _seed_audition_fixture(db_pool_greenfield)
    g1, m1 = ids["g1"]
    result = _invoke(db_url, tmp_path, monkeypatch, "models", EXP)
    assert result.exit_code == 0, result.output
    assert "Model groups" in result.output
    assert "FakeG1" in result.output and "FakeG2" in result.output

    members = _invoke(
        db_url, tmp_path, monkeypatch, "models", EXP, "--group", str(g1), "--json"
    )
    assert members.exit_code == 0, members.output
    payload = _json_payload(members.output)
    # one member model; its window mean == the group avg (single-model group) -> Δ = 0
    assert payload["group_avg"] == pytest.approx(0.70)
    assert [m["model_id"] for m in payload["models"]] == [m1]
    assert payload["models"][0]["value_mean"] == pytest.approx(0.70)


def test_cli_model_show(db_url, db_pool_greenfield, tmp_path, monkeypatch):
    ids = _seed_audition_fixture(db_pool_greenfield)
    _, m1 = ids["g1"]
    result = _invoke(db_url, tmp_path, monkeypatch, "model", "show", str(m1))
    assert result.exit_code == 0, result.output
    assert f"model {m1}" in result.output
    assert "auc_roc" in result.output  # the windowed evaluations table
    assert "postmodel crosstabs" in result.output  # empty-state hint


def test_cli_leaderboard_refreshes_unpopulated_matview(
    db_url, db_pool_greenfield, tmp_path, monkeypatch
):
    _seed_audition_fixture(db_pool_greenfield)
    # the matview is WITH NO DATA and no run has refreshed it -> the command must
    result = _invoke(db_url, tmp_path, monkeypatch, "leaderboard", EXP)
    assert result.exit_code == 0, result.output
    assert "Leaderboard" in result.output
    assert "auc_roc" in result.output

    as_json = _invoke(
        db_url, tmp_path, monkeypatch, "leaderboard", EXP, "--json", "--limit", "3"
    )
    assert as_json.exit_code == 0, as_json.output
    rows = _json_payload(as_json.output)
    assert 0 < len(rows) <= 3
    assert {"model_group_id", "metric", "value"} <= set(rows[0])
