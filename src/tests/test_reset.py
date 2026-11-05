"""``triage reset``: delete results, keep configuration (src/triage/reset.py)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import triage.cli as cli
from triage.artifacts import begin_artifact, mark_built, record_use
from triage.component.results_schema import upgrade_db
from triage.derivation import derive
from triage.reset import (
    CONFIG_TABLES,
    RESULT_TABLES,
    SHARED_TABLES,
    ResetError,
    ResetResult,
    ResetScope,
    delete_external,
    reset_results,
)
from triage.sources import bump_source, register_source

PINS = {"events": "v1"}
CUT = datetime(2026, 3, 1)


@pytest.fixture
def triage_db(db_url, db_pool):
    upgrade_db(dburl=db_url)
    return db_pool


def _one(pool, sql: str, params: dict[str, Any] | None = None) -> Any:
    with pool.connection() as conn:
        row = conn.execute(sql, params or {}).fetchone()
    return next(iter(row.values()))


def _counts(pool) -> dict[str, int]:
    return {
        t: _one(pool, f"select count(*) from triage.{t}")
        for t in (*RESULT_TABLES, *SHARED_TABLES, *CONFIG_TABLES)
    }


def _config_snapshot(pool) -> dict[str, list[str]]:
    with pool.connection() as conn:
        return {
            t: sorted(
                json.dumps(dict(r), default=str, sort_keys=True)
                for r in conn.execute(f"select * from triage.{t}").fetchall()
            )
            for t in CONFIG_TABLES
        }


def _built(pool, run_id, kind, config, parents=(), output_ref=None):
    derivation = derive(kind, config, parents=list(parents), source_pins=PINS)
    begin_artifact(
        pool,
        derivation,
        kind,
        config,
        source_pins=PINS,
        run_id=run_id,
        parents=[p.id for p in parents],
    )
    mark_built(pool, derivation.id, output_ref=output_ref)
    return derivation


def _results_of(pool, run_id, tag: str, cohort, tmp_path: Path) -> dict[str, Any]:
    """One run's matrix, model, predictions and evaluations; files under tmp_path."""
    matrix_file = tmp_path / f"{tag}.parquet"
    model_file = tmp_path / f"{tag}.joblib"
    matrix_file.write_bytes(b"m")
    model_file.write_bytes(b"m")
    matrix = _built(
        pool, run_id, "matrix", {"m": tag}, [cohort], output_ref=str(matrix_file)
    )
    model = _built(
        pool, run_id, "model", {"c": tag}, [matrix], output_ref=str(model_file)
    )
    with pool.connection() as conn:
        matrix_uuid = conn.execute(
            "insert into triage.matrices"
            " (matrix_uuid, artifact_id, matrix_kind, storage_uri, built_by_run)"
            " values (gen_random_uuid(), %(a)s, 'train', %(u)s, %(r)s)"
            " returning matrix_uuid",
            {"a": matrix.id, "u": str(matrix_file), "r": run_id},
        ).fetchone()["matrix_uuid"]
        group_id = conn.execute(
            "insert into triage.model_groups"
            " (model_group_hash, model_type, hyperparameters, feature_list)"
            " values (%(h)s, 'DT', '{}', '{}') returning model_group_id",
            {"h": f"mg-{tag}"},
        ).fetchone()["model_group_id"]
        model_id = conn.execute(
            "insert into triage.models"
            " (model_group_id, model_hash, run_id, train_matrix_uuid)"
            " values (%(g)s, %(h)s, %(r)s, %(mx)s) returning model_id",
            {"g": group_id, "h": model.id, "r": run_id, "mx": matrix_uuid},
        ).fetchone()["model_id"]
        for entity in (1, 2):
            conn.execute(
                "insert into triage.predictions"
                " (model_id, entity_id, as_of_date, split_kind, score, matrix_uuid)"
                " values (%(m)s, %(e)s, '2026-01-01', 'test', 0.5, %(mx)s)",
                {"m": model_id, "e": entity, "mx": matrix_uuid},
            )
        conn.execute(
            "insert into triage.evaluations (model_id, split_kind, as_of_date, metric)"
            " values (%(m)s, 'test', '2026-01-01', 'auc')",
            {"m": model_id},
        )
        conn.execute(
            "insert into triage.feature_importances (model_id, feature)"
            " values (%(m)s, 'f1')",
            {"m": model_id},
        )
    record_use(pool, run_id, [cohort.id, matrix.id, model.id])
    return {"model_id": model_id, "files": [matrix_file, model_file]}


@pytest.fixture
def seeded(triage_db, tmp_path):
    """Two experiments; the old run (January) built a cohort the new run (June) reuses."""
    pool = triage_db
    with pool.connection() as conn:
        conn.execute("create table public.events (entity_id int)")
        conn.execute(
            "insert into triage.metric_directions (metric, higher_is_better)"
            " values ('lift@', true)"
        )
        conn.execute(
            "insert into triage.protected_groups"
            " (entity_id, as_of_date, attribute_name, attribute_value)"
            " values (1, '2026-01-01', 'sex', 'F')"
        )
        conn.execute(
            "insert into triage.subsets (subset_hash, config) values ('s1', '{}')"
        )
        conn.execute(
            "insert into triage.subset_members (subset_hash, entity_id, as_of_date)"
            " values ('s1', 1, '2026-01-01')"
        )
    register_source(pool, "events", "public.events")
    bump_source(pool, "events", "v1")

    runs: dict[str, str] = {}
    with pool.connection() as conn:
        for tag, started in (("old", "2026-01-15"), ("new", "2026-06-15")):
            conn.execute(
                "insert into triage.experiments"
                " (experiment_hash, config, problem_type, created_at)"
                " values (%(h)s, '{}', 'classification', %(t)s)",
                {"h": f"exp-{tag}", "t": started},
            )
            runs[tag] = str(
                conn.execute(
                    "insert into triage.runs (experiment_hash, profile, started_at)"
                    " values (%(h)s, 'local', %(t)s) returning run_id",
                    {"h": f"exp-{tag}", "t": started},
                ).fetchone()["run_id"]
            )
            conn.execute(
                "insert into triage.run_source_pins (run_id, source_name)"
                " values (%(r)s, 'events')",
                {"r": runs[tag]},
            )
    cohort = _built(pool, runs["old"], "cohort", {"q": 1}, output_ref="triage.cohorts")
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.cohorts (cohort_hash, entity_id, as_of_date)"
            " values (%(h)s, 1, '2026-01-01'), (%(h)s, 2, '2026-01-01')",
            {"h": cohort.id},
        )
    old = _results_of(pool, runs["old"], "old", cohort, tmp_path)
    new = _results_of(pool, runs["new"], "new", cohort, tmp_path)
    return {"pool": pool, "runs": runs, "cohort": cohort.id, "old": old, "new": new}


def test_every_triage_table_is_classified_once(triage_db):
    with triage_db.connection() as conn:
        tables = {
            r["relname"]
            for r in conn.execute(
                "select c.relname from pg_class c"
                " join pg_namespace n on n.oid = c.relnamespace"
                " where n.nspname = 'triage' and c.relkind in ('r', 'p')"
                " and not c.relispartition"
            ).fetchall()
        }
    groups = [set(RESULT_TABLES), set(SHARED_TABLES), set(CONFIG_TABLES)]
    assert sum(len(g) for g in groups) == len(set().union(*groups))  # disjoint
    assert set().union(*groups) == tables


def test_scope_needs_exactly_one_choice():
    with pytest.raises(ResetError):
        ResetScope()
    with pytest.raises(ResetError):
        ResetScope(all=True, before=CUT)


def test_dry_run_deletes_nothing(seeded):
    pool = seeded["pool"]
    before = _counts(pool)

    result = reset_results(pool, ResetScope(all=True))

    assert _counts(pool) == before
    assert result.dry_run
    assert result.tables["predictions"] == 4
    assert result.tables["protected_groups"] == 1
    assert len(result.external) == 4
    assert all(f.exists() for f in seeded["old"]["files"] + seeded["new"]["files"])


def test_before_keeps_the_newer_runs_and_what_they_reuse(seeded):
    pool = seeded["pool"]
    config = _config_snapshot(pool)

    result = reset_results(pool, ResetScope(before=CUT), dry_run=False)
    delete_external(result)

    assert _one(pool, "select array_agg(run_id::text) from triage.runs") == [
        seeded["runs"]["new"]
    ]
    assert _one(pool, "select array_agg(experiment_hash) from triage.experiments") == [
        "exp-new"
    ]
    assert _one(pool, "select array_agg(model_id) from triage.models") == [
        seeded["new"]["model_id"]
    ]
    assert _one(pool, "select count(*) from triage.predictions") == 2
    assert _one(pool, "select count(*) from triage.model_groups") == 1
    # the cohort was built by the old run but the new run still uses it
    assert _one(pool, "select count(*) from triage.cohorts") == 2
    assert (
        _one(
            pool,
            "select count(*) from triage.artifacts where artifact_id = %(c)s",
            {"c": seeded["cohort"]},
        )
        == 1
    )
    assert result.tables["runs"] == 1
    assert result.tables["predictions"] == 2
    assert result.tables["cohorts"] == 0
    assert not any(f.exists() for f in seeded["old"]["files"])
    assert all(f.exists() for f in seeded["new"]["files"])
    assert sorted(result.deleted) == sorted(str(f) for f in seeded["old"]["files"])
    # configuration and the shared run-time inputs are untouched by a scoped reset
    assert _config_snapshot(pool) == config
    assert _one(pool, "select count(*) from triage.protected_groups") == 1
    assert _one(pool, "select count(*) from triage.subset_members") == 1


def test_experiment_scope_keeps_artifacts_another_run_built(seeded):
    pool = seeded["pool"]

    reset_results(pool, ResetScope(experiments=("exp-new",)), dry_run=False)

    assert _one(pool, "select array_agg(experiment_hash) from triage.experiments") == [
        "exp-old"
    ]
    assert _one(pool, "select array_agg(model_id) from triage.models") == [
        seeded["old"]["model_id"]
    ]
    assert _one(pool, "select count(*) from triage.cohorts") == 2


def test_unknown_experiment_is_an_error(seeded):
    with pytest.raises(ResetError, match="exp-nope"):
        reset_results(seeded["pool"], ResetScope(experiments=("exp-nope",)))


def test_all_empties_results_and_keeps_configuration(seeded):
    pool = seeded["pool"]
    config = _config_snapshot(pool)

    result = reset_results(pool, ResetScope(all=True), dry_run=False)
    delete_external(result)

    after = _counts(pool)
    assert all(after[t] == 0 for t in (*RESULT_TABLES, *SHARED_TABLES)), after
    assert _config_snapshot(pool) == config
    assert after["source_versions"] == 1 and after["metric_directions"] == 1
    assert result.tables["experiments"] == 2 and result.tables["predictions"] == 4
    assert len(result.deleted) == 4 and not result.failed
    assert not any(f.exists() for f in seeded["old"]["files"] + seeded["new"]["files"])

    again = reset_results(pool, ResetScope(all=True), dry_run=False)
    assert again.total_rows == 0 and again.external == []


def test_a_failed_file_delete_is_reported_and_the_rest_continue(tmp_path):
    present = tmp_path / "present.parquet"
    present.write_bytes(b"m")
    result = ResetResult(database="db", scope=ResetScope(all=True), dry_run=False)

    delete_external(
        result, ["gs://bucket/model.joblib", str(present), str(tmp_path / "gone")]
    )

    assert [f["ref"] for f in result.failed] == ["gs://bucket/model.joblib"]
    assert result.deleted == [str(present)] and not present.exists()
    assert result.absent == [str(tmp_path / "gone")]


@pytest.fixture
def reset_cli(db_url, seeded, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no database.yaml or .env here: DATABASE_URL wins
    runner = CliRunner()

    def run(*args: str):
        return runner.invoke(
            cli.app,
            ["reset", *args],
            env={"DATABASE_URL": db_url},
            catch_exceptions=False,
        )

    return run


def _database(pool) -> str:
    return _one(pool, "select current_database()")


def test_cli_refuses_without_confirm_when_stdin_is_not_a_terminal(reset_cli, seeded):
    pool = seeded["pool"]
    before = _counts(pool)

    result = reset_cli("--all", "--yes")

    assert result.exit_code == 2
    assert "stdin is not a terminal" in result.output
    assert _counts(pool) == before


def test_cli_refuses_a_wrong_database_name(reset_cli, seeded):
    pool = seeded["pool"]
    before = _counts(pool)

    result = reset_cli("--all", "--yes", "--confirm", "some_other_db")

    assert result.exit_code == 2
    assert _counts(pool) == before


def test_cli_dry_run_json(reset_cli, seeded):
    result = reset_cli("--before", "2026-03-01", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["tables"]["runs"] == 1
    assert len(payload["external"]["refs"]) == 2


def test_cli_all_with_confirm_and_bump(reset_cli, seeded):
    pool = seeded["pool"]

    result = reset_cli(
        "--all",
        "--yes",
        "--confirm",
        _database(pool),
        "--bump-sources",
        "reload-2026-11",
        "--json",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is False
    assert payload["bumped"] == {"events": "reload-2026-11"}
    assert len(payload["external"]["deleted"]) == 4
    assert _one(pool, "select count(*) from triage.runs") == 0
    assert _one(
        pool,
        "select array_agg(version_label order by registered_at) from triage.source_versions",
    ) == ["v1", "reload-2026-11"]
