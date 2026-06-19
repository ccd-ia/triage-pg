"""Tests for GC roots, liveness, collect, and purge (ADR-0017)."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from triage.artifacts import (
    archive_experiment,
    begin_artifact,
    cache_hit,
    collect,
    delete_outputs,
    gc_candidates,
    get_artifact,
    mark_built,
    purge,
    record_use,
)
from triage.component.results_schema import upgrade_db
from triage.derivation import derive

PINS = {"events": "v1"}


@pytest.fixture
def triage_db(db_engine):
    upgrade_db(db_engine=db_engine)
    return db_engine


def make_experiment(engine, experiment_hash="exp1"):
    with engine.begin() as conn:
        conn.execute(
            text("""
                insert into triage.experiments (experiment_hash, config, problem_type)
                values (:hash, '{}', 'classification')
                """),
            {"hash": experiment_hash},
        )
    return experiment_hash


def make_run(engine, experiment_hash=None):
    with engine.begin() as conn:
        return str(
            conn.execute(
                text("""
                    insert into triage.runs (experiment_hash, profile)
                    values (:hash, 'local') returning run_id
                    """),
                {"hash": experiment_hash},
            ).scalar_one()
        )


def build(engine, derivation, kind, config, parents=()):
    begin_artifact(engine, derivation, kind, config, source_pins=PINS, parents=parents)
    mark_built(engine, derivation.id)
    return derivation


def make_model_row(engine, model_artifact_id):
    with engine.begin() as conn:
        model_group_id = conn.execute(
            text("""
                insert into triage.model_groups
                    (model_group_hash, model_type, hyperparameters, feature_list)
                values (:hash, 'DT', '{}', '{}') returning model_group_id
                """),
            {"hash": f"mg-{model_artifact_id[:8]}"},
        ).scalar_one()
        return conn.execute(
            text("""
                insert into triage.models (model_group_id, model_hash)
                values (:group_id, :hash) returning model_id
                """),
            {"group_id": model_group_id, "hash": model_artifact_id},
        ).scalar_one()


def add_prediction(engine, model_id):
    with engine.begin() as conn:
        conn.execute(
            text("""
                insert into triage.predictions
                    (model_id, entity_id, as_of_date, split_kind, score)
                values (:model_id, 1, '2026-01-01', 'test', 0.5)
                """),
            {"model_id": model_id},
        )


def test_usage_by_active_experiment_keeps_artifact_live(triage_db):
    experiment = make_experiment(triage_db)
    run_id = make_run(triage_db, experiment)
    cohort = build(
        triage_db, derive("cohort", {"q": 1}, source_pins=PINS), "cohort", {"q": 1}
    )
    record_use(triage_db, run_id, [cohort.id])

    assert gc_candidates(triage_db) == []

    archive_experiment(triage_db, experiment)
    assert [row["artifact_id"] for row in gc_candidates(triage_db)] == [cohort.id]


def test_liveness_includes_the_upstream_closure(triage_db):
    experiment = make_experiment(triage_db)
    run_id = make_run(triage_db, experiment)
    cohort = build(
        triage_db, derive("cohort", {"q": 1}, source_pins=PINS), "cohort", {"q": 1}
    )
    matrix_derivation = derive("matrix", {"m": 1}, parents=[cohort], source_pins=PINS)
    build(triage_db, matrix_derivation, "matrix", {"m": 1}, parents=[cohort.id])
    # only the matrix is recorded as used; the cohort must stay live via closure
    record_use(triage_db, run_id, [matrix_derivation.id])

    assert gc_candidates(triage_db) == []

    archive_experiment(triage_db, experiment)
    dead = {row["artifact_id"] for row in gc_candidates(triage_db)}
    assert dead == {cohort.id, matrix_derivation.id}


def test_predictions_pin_models_beyond_experiment_lifecycle(triage_db):
    experiment = make_experiment(triage_db)
    run_id = make_run(triage_db, experiment)
    model = build(
        triage_db, derive("model", {"c": "DT"}, source_pins=PINS), "model", {"c": "DT"}
    )
    record_use(triage_db, run_id, [model.id])
    model_id = make_model_row(triage_db, model.id)
    add_prediction(triage_db, model_id)

    archive_experiment(triage_db, experiment)
    assert gc_candidates(triage_db) == []  # pinned by its predictions


def test_runs_without_experiment_are_conservatively_live(triage_db):
    run_id = make_run(triage_db, experiment_hash=None)
    cohort = build(
        triage_db, derive("cohort", {"q": 1}, source_pins=PINS), "cohort", {"q": 1}
    )
    record_use(triage_db, run_id, [cohort.id])
    assert gc_candidates(triage_db) == []


def test_min_age_guard_protects_recent_builds(triage_db):
    build(triage_db, derive("cohort", {"q": 1}, source_pins=PINS), "cohort", {"q": 1})
    assert gc_candidates(triage_db, min_age_days=1) == []
    assert len(gc_candidates(triage_db, min_age_days=0)) == 1


def test_collect_deletes_slices_keeps_provenance_and_rebuilds(triage_db):
    cohort = build(
        triage_db, derive("cohort", {"q": 1}, source_pins=PINS), "cohort", {"q": 1}
    )
    with triage_db.begin() as conn:
        conn.execute(
            text(
                "insert into triage.cohorts (cohort_hash, entity_id, as_of_date)"
                " values (:hash, 1, '2026-01-01'), (:hash, 2, '2026-01-01')"
            ),
            {"hash": cohort.id},
        )

    external = collect(triage_db, [cohort.id])
    assert external == []  # cohort outputs are in-PG, nothing for the storage layer

    with triage_db.connect() as conn:
        remaining = conn.execute(
            text("select count(*) from triage.cohorts where cohort_hash = :hash"),
            {"hash": cohort.id},
        ).scalar_one()
    assert remaining == 0

    artifact = get_artifact(triage_db, cohort.id)
    assert artifact is not None  # provenance survives collection
    assert artifact["status"] == "collected"
    assert cache_hit(triage_db, cohort) is None  # rebuilds on next demand


def test_collect_returns_file_backed_outputs_for_the_storage_layer(triage_db):
    matrix = derive("matrix", {"m": 1}, source_pins=PINS)
    begin_artifact(triage_db, matrix, "matrix", {"m": 1}, source_pins=PINS)
    mark_built(triage_db, matrix.id, output_ref="file:///tmp/m.parquet")

    external = collect(triage_db, [matrix.id])
    assert external == [
        {
            "artifact_id": matrix.id,
            "kind": "matrix",
            "output_ref": "file:///tmp/m.parquet",
        }
    ]


def test_delete_outputs_removes_files_through_the_storage_layer(triage_db, tmp_path):
    matrix_file = tmp_path / "m.parquet"
    matrix_file.write_bytes(b"parquet-bytes")
    model_file = tmp_path / "model.joblib"
    model_file.write_bytes(b"joblib-bytes")

    matrix = build(
        triage_db, derive("matrix", {"m": 1}, source_pins=PINS), "matrix", {"m": 1}
    )
    model = build(
        triage_db, derive("model", {"k": 1}, source_pins=PINS), "model", {"k": 1}
    )
    # mark_built records the on-disk path as the output_ref (bare path, the greenfield form)
    mark_built(triage_db, matrix.id, output_ref=str(matrix_file))
    mark_built(triage_db, model.id, output_ref=str(model_file))

    external = collect(triage_db, [matrix.id, model.id])
    result = delete_outputs(external)

    assert not matrix_file.exists()
    assert not model_file.exists()
    assert set(result["deleted"]) == {str(matrix_file), str(model_file)}
    assert result["absent"] == []


def test_delete_outputs_tolerates_already_absent_files(triage_db, tmp_path):
    gone = tmp_path / "gone.parquet"  # never created
    matrix = build(
        triage_db, derive("matrix", {"m": 2}, source_pins=PINS), "matrix", {"m": 2}
    )
    mark_built(triage_db, matrix.id, output_ref=str(gone))

    result = delete_outputs(collect(triage_db, [matrix.id]))

    assert result["deleted"] == []
    assert result["absent"] == [str(gone)]


def test_collect_refuses_non_built_artifacts(triage_db):
    failed = derive("matrix", {"m": 1}, source_pins=PINS)
    begin_artifact(triage_db, failed, "matrix", {"m": 1}, source_pins=PINS)
    with pytest.raises(ValueError, match="only 'built'"):
        collect(triage_db, [failed.id])
    with pytest.raises(ValueError, match="no such artifact"):
        collect(triage_db, ["deadbeef"])


def test_purge_deletes_dead_rows_and_cascades(triage_db):
    cohort = build(
        triage_db, derive("cohort", {"q": 1}, source_pins=PINS), "cohort", {"q": 1}
    )
    matrix_derivation = derive("matrix", {"m": 1}, parents=[cohort], source_pins=PINS)
    build(triage_db, matrix_derivation, "matrix", {"m": 1}, parents=[cohort.id])

    collect(triage_db, [cohort.id, matrix_derivation.id])
    purged = purge(triage_db)
    assert set(purged) == {cohort.id, matrix_derivation.id}
    assert get_artifact(triage_db, cohort.id) is None

    with triage_db.connect() as conn:
        edges = conn.execute(
            text("select count(*) from triage.artifact_inputs")
        ).scalar_one()
    assert edges == 0


def test_purge_retains_parents_with_surviving_children(triage_db):
    cohort = build(
        triage_db, derive("cohort", {"q": 1}, source_pins=PINS), "cohort", {"q": 1}
    )
    matrix_derivation = derive("matrix", {"m": 1}, parents=[cohort], source_pins=PINS)
    build(triage_db, matrix_derivation, "matrix", {"m": 1}, parents=[cohort.id])

    # only the parent is collected; the dead child is still 'built'
    collect(triage_db, [cohort.id])
    assert purge(triage_db) == []  # parent retained: child's edge still needs it
    assert get_artifact(triage_db, cohort.id) is not None

    collect(triage_db, [matrix_derivation.id])
    purged = purge(triage_db)
    assert set(purged) == {cohort.id, matrix_derivation.id}


def test_purge_spares_live_and_built_artifacts(triage_db):
    experiment = make_experiment(triage_db)
    run_id = make_run(triage_db, experiment)
    live = build(
        triage_db, derive("cohort", {"q": 1}, source_pins=PINS), "cohort", {"q": 1}
    )
    record_use(triage_db, run_id, [live.id])
    dead_but_built = build(
        triage_db, derive("cohort", {"q": 2}, source_pins=PINS), "cohort", {"q": 2}
    )

    assert purge(triage_db) == []  # nothing collected/failed and dead yet
    assert get_artifact(triage_db, live.id) is not None
    assert get_artifact(triage_db, dead_but_built.id) is not None


def test_predictions_fk_restricts_model_deletion(triage_db):
    model = build(
        triage_db, derive("model", {"c": "DT"}, source_pins=PINS), "model", {"c": "DT"}
    )
    model_id = make_model_row(triage_db, model.id)
    add_prediction(triage_db, model_id)

    with pytest.raises(IntegrityError):
        with triage_db.begin() as conn:
            conn.execute(
                text("delete from triage.models where model_id = :id"),
                {"id": model_id},
            )


def test_archive_is_idempotent_and_fails_on_unknown(triage_db):
    experiment = make_experiment(triage_db)
    archive_experiment(triage_db, experiment)
    with triage_db.connect() as conn:
        first = conn.execute(
            text(
                "select archived_at from triage.experiments where experiment_hash = :h"
            ),
            {"h": experiment},
        ).scalar_one()
    archive_experiment(triage_db, experiment)  # idempotent, keeps timestamp
    with triage_db.connect() as conn:
        second = conn.execute(
            text(
                "select archived_at from triage.experiments where experiment_hash = :h"
            ),
            {"h": experiment},
        ).scalar_one()
    assert first == second

    with pytest.raises(ValueError, match="no such experiment"):
        archive_experiment(triage_db, "ghost")
