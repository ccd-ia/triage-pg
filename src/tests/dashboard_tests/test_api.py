"""Contract tests for the read-dashboard JSON API (dashboard-api-contract.md).

Each endpoint is a thin SELECT over a 0004/0005 view/function; these assert the JSON SHAPE the
SPA consumes and that the empty-state envelope fires when a panel's source is empty. The
analysis layer is experiment-scoped (migration 0005): the CRITICAL regression here is that
``GET /experiments/{hash}/audition`` stays NON-EMPTY across a re-run that cache-shares models
(``models.run_id`` points at the FIRST run) — run-scoping would be empty (the Q1 bug).

The SSE test is kept light (asserts the content type + that the stream connects + forwards a
matching NOTIFY); live streaming is covered at integration.
"""

from __future__ import annotations

import json

import pytest

# =============================================================== run-scoped (rail + monitoring)


def test_list_runs(client, seeded):
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    runs = resp.json()
    assert isinstance(runs, list)
    ids = {r["run_id"] for r in runs}
    assert seeded.run_id in ids and seeded.rerun_id in ids
    row = next(r for r in runs if r["run_id"] == seeded.run_id)
    for key in ("status", "profile", "purpose", "started_at", "batch_job_id"):
        assert key in row
    assert row["status"] == "completed"


def test_run_summary(client, seeded):
    resp = client.get(f"/api/runs/{seeded.run_id}/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"summary", "cohort_profile", "label_base_rate"}
    summary = body["summary"]
    assert summary["status"] == "completed"
    assert summary["problem_type"] == "classification"
    assert summary["plan"]["n_models"] == 9
    assert len(body["cohort_profile"]) == 3
    assert all(r["n_entities"] == 4 for r in body["cohort_profile"])
    assert len(body["label_base_rate"]) == 3
    assert all("base_rate" in r for r in body["label_base_rate"])


def test_run_progress(client, seeded):
    resp = client.get(f"/api/runs/{seeded.run_id}/progress")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"progress", "plan"}
    kinds = {r["kind"] for r in body["progress"]}
    assert {"cohort", "labels", "model"} <= kinds
    assert all(r["status"] == "built" for r in body["progress"])
    assert body["plan"]["n_splits"] == 3


def test_run_derivation(client, seeded):
    resp = client.get(f"/api/runs/{seeded.run_id}/derivation")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"nodes", "edges"}
    node_ids = {n["artifact_id"] for n in body["nodes"]}
    assert "art-cohort" in node_ids and "art-labels" in node_ids
    assert {"parent_id": "art-cohort", "artifact_id": "art-labels"} in body["edges"]
    # run 1 built these -> not a cache hit from its own perspective
    assert all(n["cache_hit"] is False for n in body["nodes"])


def test_run_derivation_rerun_marks_cache_hits(client, seeded):
    # Run 2 cache-shared run 1's artifacts: from run 2's view every node is a cache hit.
    resp = client.get(f"/api/runs/{seeded.rerun_id}/derivation")
    assert resp.status_code == 200
    body = resp.json()
    assert body["nodes"]
    assert all(n["cache_hit"] is True for n in body["nodes"])


def test_source_pins(client, seeded):
    resp = client.get(f"/api/runs/{seeded.run_id}/source-pins")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"run_pins", "current"}
    assert body["run_pins"][0]["source_name"] == "customers"
    assert body["run_pins"][0]["version_label"] == "v1"
    assert body["current"][0]["source_name"] == "customers"


# ============================================================== experiment-scoped (analysis)


def test_list_experiments(client, seeded):
    resp = client.get("/api/experiments")
    assert resp.status_code == 200
    rows = resp.json()
    row = next(r for r in rows if r["experiment_hash"] == seeded.experiment_hash)
    for key in (
        "name",
        "description",
        "author",
        "problem_type",
        "created_at",
        "n_runs",
        "last_status",
        "last_plan",
    ):
        assert key in row
    assert row["name"] == "Churn baseline"
    assert row["author"] == "tester"
    assert row["n_runs"] == 2  # both runs counted
    # actuals (migration 0006) — derived from what was built, independent of runs.plan
    assert row["n_model_groups"] == 3
    assert row["n_models"] == 9  # 3 groups x 3 splits
    assert row["n_splits"] == 3  # distinct train_end_time
    assert row["n_features"] is None  # the fixture seeds no matrices
    assert row["base_rate"] == 0.5  # outcome = entity_id % 2 over entities 1..4
    assert row["cohort_size"] == 4


def test_experiment_detail(client, seeded):
    resp = client.get(f"/api/experiments/{seeded.experiment_hash}")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"summary", "config", "runs", "model_reuse", "artifact_sharing"}
    assert body["summary"]["experiment_hash"] == seeded.experiment_hash
    assert body["config"]["cohort_name"] == "active"
    # both runs returned, newest first
    assert {r["run_id"] for r in body["runs"]} == {seeded.run_id, seeded.rerun_id}
    # built vs reused: the fixture's models all belong to run 1 (the builder); both runs
    # USE them (run_artifacts), so from the experiment all 9 are "built", 0 reused.
    assert body["model_reuse"]["built"] == 9
    assert body["model_reuse"]["reused"] == 0
    # actuals populate the overview strip even though the fixture's plan has no n_splits source
    assert body["summary"]["n_models"] == 9
    assert body["summary"]["n_splits"] == 3


def test_experiment_detail_replay_run_built_nothing(client, seeded):
    """Per-run built/reused: the builder run built every artifact (cohort + labels + 9 models =
    11); the re-run cache-shared all of them, so it built 0 — it is a 'replay'."""
    body = client.get(f"/api/experiments/{seeded.experiment_hash}").json()
    by_id = {r["run_id"]: r for r in body["runs"]}
    builder = by_id[seeded.run_id]
    replay = by_id[seeded.rerun_id]
    assert builder["n_built"] == 11 and builder["n_reused"] == 0
    assert replay["n_built"] == 0 and replay["n_reused"] == 11


def test_experiment_detail_artifact_sharing(client, seeded):
    """The fixture's two runs both belong to exp-dash, so nothing is built by a *foreign*
    experiment: n_total counts the touched artifacts, n_foreign is 0, no lender named.
    """
    sharing = client.get(f"/api/experiments/{seeded.experiment_hash}").json()[
        "artifact_sharing"
    ]
    assert sharing["n_total"] == 11
    assert sharing["n_foreign"] == 0
    assert sharing["shared_with_name"] is None


def test_experiment_detail_404(client, seeded):
    resp = client.get("/api/experiments/does-not-exist")
    assert resp.status_code == 404


def test_list_experiments_hides_archived(client, seeded, db_pool_greenfield):
    """A soft-archived experiment (archived_at set) drops off the /experiments list but stays
    reachable by direct link via /experiments/{hash}."""
    assert any(
        e["experiment_hash"] == seeded.experiment_hash
        for e in client.get("/api/experiments").json()
    )
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "update triage.experiments set archived_at = now() where experiment_hash = %(h)s",
            {"h": seeded.experiment_hash},
        )
    assert all(
        e["experiment_hash"] != seeded.experiment_hash
        for e in client.get("/api/experiments").json()
    )
    # direct link still resolves
    assert client.get(f"/api/experiments/{seeded.experiment_hash}").status_code == 200


def test_experiment_audition(client, seeded):
    resp = client.get(
        f"/api/experiments/{seeded.experiment_hash}/audition",
        params={"metric": "auc_roc", "rule": "best_average_value"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert not body.get("empty")
    assert {"ranking", "curves", "pick", "k", "n", "provisional", "strategies"} <= set(
        body
    )
    assert len(body["ranking"]) == 3  # 3 model_groups
    assert body["pick"] == seeded.group_ids["mg2"]
    assert body["k"] == 3 and body["n"] == 3
    assert body["provisional"] is False
    assert len(body["curves"]) == 3 * 3
    # strategies: a pick per standard rule, two-metrics resolvable (average_precision present)
    rules = {s["rule"] for s in body["strategies"]}
    assert {
        "best_current_value",
        "best_average_value",
        "lowest_metric_variance",
        "most_frequent_best_dist",
        "best_avg_var_penalized",
        "best_avg_recency_weight",
        "best_average_two_metrics",
        "random_model_group",
    } == rules
    by_rule = {s["rule"]: s["model_group_id"] for s in body["strategies"]}
    assert by_rule["best_current_value"] == seeded.group_ids["mg1"]
    assert by_rule["best_average_value"] == seeded.group_ids["mg2"]
    assert by_rule["lowest_metric_variance"] == seeded.group_ids["mg3"]


def test_experiment_audition_nonempty_after_rerun(client, seeded):
    """Q1 REGRESSION: audition is experiment-scoped, so a re-run that cache-shares the first
    run's models (models.run_id -> run 1, both runs share the experiment_hash) keeps audition
    NON-EMPTY. Run-scoping would return the empty-state envelope here."""
    # Both runs exist under the one experiment; the models all belong to run 1.
    resp = client.get(f"/api/experiments/{seeded.experiment_hash}/audition")
    assert resp.status_code == 200
    body = resp.json()
    assert not body.get("empty"), (
        "experiment-scoped audition must survive a cache-shared re-run"
    )
    assert len(body["ranking"]) == 3


def test_experiment_audition_empty_state(client, empty_experiment):
    exp_hash, _ = empty_experiment
    resp = client.get(f"/api/experiments/{exp_hash}/audition")
    assert resp.status_code == 200
    body = resp.json()
    assert body["empty"] is True
    assert "reason" in body and "hint" in body


def test_experiment_bias(client, seeded):
    resp = client.get(f"/api/experiments/{seeded.experiment_hash}/bias")
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list) and rows
    row = rows[0]
    for key in ("attribute_name", "attribute_value", "metric", "value", "disparity"):
        assert key in row
    assert row["attribute_name"] == "race"


def test_experiment_bias_empty_state(client, empty_experiment):
    exp_hash, _ = empty_experiment
    resp = client.get(f"/api/experiments/{exp_hash}/bias")
    assert resp.status_code == 200
    body = resp.json()
    assert body["empty"] is True
    assert "protected_groups" in body["reason"]


def test_experiment_leaderboard(client, seeded):
    resp = client.get(f"/api/experiments/{seeded.experiment_hash}/leaderboard")
    assert resp.status_code == 200
    rows = resp.json()
    assert rows  # matview was refreshed in the fixture
    row = rows[0]
    for key in ("experiment_hash", "model_group_id", "metric", "value", "model_id"):
        assert key in row
    assert row["experiment_hash"] == seeded.experiment_hash


def test_experiment_evaluations(client, seeded):
    resp = client.get(
        f"/api/experiments/{seeded.experiment_hash}/evaluations",
        params={"metric": "auc_roc"},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 9  # 3 groups x 3 splits for auc_roc
    assert all(r["metric"] == "auc_roc" for r in rows)
    assert all("as_of_date" in r and "value" in r for r in rows)
    assert all(r["experiment_hash"] == seeded.experiment_hash for r in rows)


def test_experiment_model_groups(client, seeded):
    resp = client.get(f"/api/experiments/{seeded.experiment_hash}/model-groups")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 3
    gids = {r["model_group_id"] for r in rows}
    assert gids == set(seeded.group_ids.values())
    row = rows[0]
    for key in (
        "model_group_hash",
        "model_type",
        "hyperparameters",
        "feature_list",
        "n_models",
        "first_train_end",
        "last_train_end",
    ):
        assert key in row
    assert all(r["n_models"] == 3 for r in rows)


def test_experiment_selected_model(client, seeded):
    resp = client.get(
        f"/api/experiments/{seeded.experiment_hash}/selected-model",
        params={"metric": "auc_roc", "rule": "best_average_value"},
    )
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "audition_group",
        "audition_model",
        "leaderboard_group",
        "leaderboard_model",
        "diverges",
    ):
        assert key in body
    assert body["audition_group"] == seeded.group_ids["mg2"]
    assert body["leaderboard_group"] == seeded.group_ids["mg1"]
    assert body["diverges"] is True
    assert body["audition_model"] == seeded.latest_model["mg2"]


# ============================================================== hierarchy detail


def test_model_group_detail(client, seeded):
    gid = seeded.group_ids["mg1"]
    resp = client.get(f"/api/model-groups/{gid}", params={"metric": "auc_roc"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "summary",
        "models",
        "metric_over_time",
        "per_split",
        "audition",
    }
    assert body["summary"]["model_group_id"] == gid
    assert {m["model_id"] for m in body["models"]} == set(seeded.all_models["mg1"])
    assert all("run_id" in m and "train_end_time" in m for m in body["models"])
    # audition aggregates only come with an experiment scope
    assert body["audition"] == []
    scoped = client.get(
        f"/api/model-groups/{gid}",
        params={"metric": "auc_roc", "experiment_hash": seeded.experiment_hash},
    ).json()
    assert any(a["metric"] == "auc_roc" for a in scoped["audition"])
    assert {"avg_value", "stddev_value", "max_regret"} <= set(scoped["audition"][0])
    # metric-over-time: one row per split for the chosen metric
    assert len(body["metric_over_time"]) == 3
    assert body["per_split"]


def test_model_group_detail_404(client, seeded):
    resp = client.get("/api/model-groups/999999")
    assert resp.status_code == 404


def test_model_group_detail_scoped_to_experiment(client, seeded):
    """A model_group can be shared across experiments; ?experiment_hash= scopes the models/evals
    to one experiment's runs. The seed's group belongs to exp-dash; a foreign hash yields none.
    """
    gid = seeded.group_ids["mg1"]
    scoped = client.get(
        f"/api/model-groups/{gid}",
        params={"metric": "auc_roc", "experiment_hash": seeded.experiment_hash},
    ).json()
    assert {m["model_id"] for m in scoped["models"]} == set(seeded.all_models["mg1"])
    # a different experiment owns none of this group's models
    foreign = client.get(
        f"/api/model-groups/{gid}", params={"experiment_hash": "does-not-exist"}
    ).json()
    assert foreign["models"] == []
    assert foreign["metric_over_time"] == []


def test_model_detail(client, seeded):
    model_id = seeded.all_models["mg1"][0]  # the first model (has importances)
    resp = client.get(f"/api/models/{model_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["model_id"] == model_id
    assert set(body) == {
        "model_id",
        "model_group_id",
        "feature_importances",
        "evaluations",
        "windowed",
    }
    assert body["model_group_id"] == seeded.group_ids["mg1"]
    assert {fi["feature"] for fi in body["feature_importances"]} == {"f1", "f2"}
    assert body["evaluations"]
    assert all(e["model_id"] == model_id for e in body["evaluations"])
    # evaluations_windowed (0010) finally has a consumer: one rollup per metric
    assert body["windowed"], "windowed rollup should carry the seeded metrics"
    w = body["windowed"][0]
    assert {"metric", "value_mean", "value_min", "value_max", "n_as_of_dates"} <= set(w)


def test_model_detail_404(client, seeded):
    resp = client.get("/api/models/999999")
    assert resp.status_code == 404


def test_model_curve(client, seeded):
    model_id = seeded.all_models["mg1"][0]  # has predictions at the last split
    resp = client.get(f"/api/models/{model_id}/curve")
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list) and rows
    row = rows[0]
    for key in ("k", "pct", "prec", "rec", "tp", "fp", "fn", "tn"):
        assert key in row
    # k increases monotonically from 1
    assert [r["k"] for r in rows] == sorted(r["k"] for r in rows)
    assert rows[0]["k"] == 1


def test_model_histogram(client, seeded):
    model_id = seeded.all_models["mg1"][0]
    resp = client.get(f"/api/models/{model_id}/histogram", params={"bins": 10})
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list) and rows
    for key in ("bin", "lo", "hi", "n", "n_pos"):
        assert key in rows[0]


def test_model_predictions(client, seeded):
    model_id = seeded.all_models["mg1"][0]
    resp = client.get(f"/api/models/{model_id}/predictions", params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    # paged contract (migration 0006): {rows, total}
    assert set(body) == {"rows", "total"}
    assert body["total"] == 4  # 4 entities predicted at the last split
    rows = body["rows"]
    row = rows[0]
    for key in ("entity_id", "as_of_date", "score", "rank_abs", "rank_pct", "outcome"):
        assert key in row
    assert len(rows) == 2  # limit applied
    assert rows[0]["rank_abs"] == 1


def test_model_predictions_paging(client, seeded):
    model_id = seeded.all_models["mg1"][0]
    page1 = client.get(
        f"/api/models/{model_id}/predictions", params={"limit": 2, "offset": 0}
    ).json()
    page2 = client.get(
        f"/api/models/{model_id}/predictions", params={"limit": 2, "offset": 2}
    ).json()
    assert page1["total"] == page2["total"] == 4
    # offset advances the rank window; no overlap between the two pages
    ranks1 = {r["rank_abs"] for r in page1["rows"]}
    ranks2 = {r["rank_abs"] for r in page2["rows"]}
    assert ranks1.isdisjoint(ranks2)
    assert ranks1 == {1, 2}


def test_model_predictions_empty_state(client, seeded):
    # a model with no predictions (mg2's first model) returns the empty-state envelope
    model_id = seeded.all_models["mg2"][0]
    resp = client.get(f"/api/models/{model_id}/predictions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["empty"] is True
    assert "no predictions" in body["reason"]


# ============================================================== project-level


def test_metrics(client, seeded):
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    rows = resp.json()
    metrics = {(r["metric"], r["parameter"]) for r in rows}
    assert ("auc_roc", "") in metrics and ("average_precision", "") in metrics
    assert all("higher_is_better" in r for r in rows)


def test_ontology(client, seeded):
    resp = client.get("/api/ontology")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"sources", "volumes", "volumes_by_type", "profile"}
    names = {s["source_name"] for s in body["sources"]}
    assert "customers" in names
    src = next(s for s in body["sources"] if s["source_name"] == "customers")
    for key in (
        "relation",
        "knowledge_date_column",
        "description",
        "role",
        "type_column",
    ):
        assert key in src
    # the seed's source has no type_column → no per-type series for it
    assert "customers" not in body["volumes_by_type"]
    # source_volume runs per source (customers has a knowledge_date_column)
    assert "customers" in body["volumes"]
    assert isinstance(body["volumes"]["customers"], list)
    # source_profile (migration 0006): total rows + knowledge-date range
    prof = body["profile"]["customers"]
    assert prof["total_rows"] == 2
    assert prof["first_date"] == "2014-01-15"
    assert prof["last_date"] == "2014-02-20"
    # customers has no entity_id column -> distinct entities not computed
    assert prof["n_distinct_entities"] is None


def test_status(client, seeded):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "sources",
        "engine_versions",
        "gc",
        "runs",
        "experiments",
        "artifact_paths",
        "db",
        "execution",
        "compute",
        "source_drift",
    }
    assert body["sources"][0]["source_name"] == "customers"
    assert body["engine_versions"] == {"featurizer": "0.4.1"}
    # run counts as a {status: count} map (the SPA reads Record<string,number>; a list here
    # would render an object as a React child and blank the page)
    assert body["runs"] == {"completed": 2}
    # experiments overview (not just runs): the seeded experiment is listed
    assert any(
        e["experiment_hash"] == seeded.experiment_hash for e in body["experiments"]
    )
    # gc tallies are grouped by (kind, status)
    assert any(g["kind"] == "model" and g["status"] == "built" for g in body["gc"])
    # DB health (proves reachability + headroom) from the live pg catalogs
    assert body["db"]["reachable"] is True
    assert body["db"]["server_version"]
    assert body["db"]["max_connections"] > 0
    # execution mode of the latest run (the fixture's run 1/2 are local, completed)
    assert body["execution"]["profile"] == "local"
    assert body["execution"]["status"] == "completed"
    # compute is from runs.plan->compute; the fixture's plan has none -> null (graceful)
    assert "compute" in body
    # source drift: the run's pin vs registry head (customers @ v1, no drift)
    assert any(d["source_name"] == "customers" for d in body["source_drift"])


def test_project_derivation(client, seeded):
    resp = client.get("/api/derivation")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"nodes", "edges"}
    node_ids = {n["artifact_id"] for n in body["nodes"]}
    assert "art-cohort" in node_ids and "art-labels" in node_ids
    # the cohort/labels are shared across both runs of the one experiment
    cohort = next(n for n in body["nodes"] if n["artifact_id"] == "art-cohort")
    assert cohort["n_runs"] == 2
    assert cohort["n_experiments"] == 1
    assert {"parent_id": "art-cohort", "artifact_id": "art-labels"} in body["edges"]


def test_entity_profile(client, seeded):
    """Entity drill-down: label history + score/rank trajectory for a predicted entity.

    Entity 1 is in the cohort at all 3 splits (labels) and was predicted by mg1's first model
    at the last split. The seed's source relation has no entity_id, so attributes are null —
    but the entity is still known via labels + predictions, so this is a 200, not a 404.
    """
    resp = client.get("/api/entities/1")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"entity_id", "attributes", "label_history", "score_history"}
    assert body["entity_id"] == 1
    # 3 labels (one per split, 6-month timespan), outcome = 1 % 2 = 1
    assert len(body["label_history"]) == 3
    assert all(r["outcome"] == 1 for r in body["label_history"])
    # entity 1 was predicted by mg1's first model at the last split (one trajectory point)
    assert len(body["score_history"]) == 1
    pt = body["score_history"][0]
    for key in ("model_group_id", "as_of_date", "score", "rank_abs", "model_type"):
        assert key in pt


def test_entity_profile_attributes(client, seeded, db_pool_greenfield):
    """When a source is flagged role='entity' (and its relation has entity_id), the entity
    profile returns that row's attributes as jsonb."""
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "create table facilities (entity_id bigint, name text, kind text, as_of date)"
        )
        conn.execute(
            "insert into facilities values (1, 'east of edens', 'restaurant', date '2014-01-02')"
        )
        conn.execute(
            "insert into triage.sources (source_name, relation, knowledge_date_column, role)"
            " values ('facilities', 'facilities', 'as_of', 'entity')"
        )
    resp = client.get("/api/entities/1")
    assert resp.status_code == 200
    attrs = resp.json()["attributes"]
    assert attrs is not None
    assert attrs["name"] == "east of edens"
    assert attrs["kind"] == "restaurant"


def test_entity_label_history_includes_null_grid(client, seeded, db_pool_greenfield):
    """The label history is the full cohort as_of grid (migration 0008): an as_of date where the
    entity is in the cohort but has no matured label surfaces with outcome=None, not hidden.
    """
    extra = "2015-07-01"
    with db_pool_greenfield.connection() as conn:
        # entity 1 joins the cohort at a 4th as_of, but no label row is written for it there.
        conn.execute(
            "insert into triage.cohorts (cohort_hash, entity_id, as_of_date)"
            " values ('art-cohort', 1, %(d)s)",
            {"d": extra},
        )
    rows = client.get("/api/entities/1").json()["label_history"]
    by_date = {r["as_of_date"]: r["outcome"] for r in rows}
    assert extra in by_date and by_date[extra] is None  # shown, with a NULL outcome
    assert by_date["2014-01-01"] == 1  # the matured ones still carry their outcome


def test_entity_attributes_geo_decoded(client, seeded, db_pool_greenfield):
    """A PostGIS geography/geometry attribute renders as {lon,lat,geojson,kind} instead of WKB
    hex (migration 0008). Skipped where PostGIS is not installed in the test server."""
    with db_pool_greenfield.connection() as conn:
        try:
            conn.execute("create extension if not exists postgis")
        except Exception:  # noqa: BLE001 - environment without PostGIS: nothing to assert
            pytest.skip("PostGIS not available in the test server")
        conn.execute(
            "create table geo_facilities (entity_id bigint, name text, as_of date,"
            " location geography)"
        )
        conn.execute(
            "insert into geo_facilities values (1, 'east', date '2014-01-02',"
            " ST_SetSRID(ST_MakePoint(-87.65, 41.95), 4326)::geography)"
        )
        conn.execute(
            "insert into triage.sources (source_name, relation, knowledge_date_column, role)"
            " values ('geo_facilities', 'geo_facilities', 'as_of', 'entity')"
        )
    loc = client.get("/api/entities/1").json()["attributes"]["location"]
    assert loc["kind"] == "geo"
    assert abs(loc["lon"] - (-87.65)) < 1e-6 and abs(loc["lat"] - 41.95) < 1e-6
    assert loc["geojson"]["type"] == "Point"


def test_entity_profile_scoped_to_experiment(client, seeded):
    """The optional experiment_hash filter scopes the trajectory to one experiment."""
    resp = client.get(
        "/api/entities/1", params={"experiment_hash": seeded.experiment_hash}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert all(
        p["experiment_hash"] == seeded.experiment_hash for p in body["score_history"]
    )


def test_entity_profile_404(client, seeded):
    resp = client.get("/api/entities/999999")
    assert resp.status_code == 404


# ============================================================== SPA deep-link fallback


def test_spa_fallback_serves_index_for_client_routes(client):
    """A hard navigation / refresh to a React Router client route (no real file at that path)
    is served index.html (200) so the SPA bootstraps client-side — not a 404. The packaged
    static/ placeholder has an index.html, so the fallback is exercised here."""
    for client_route in ("/experiments/deadbeef", "/ontology", "/runs/abc/whatever"):
        resp = client.get(client_route)
        assert resp.status_code == 200, client_route
        assert "<title>" in resp.text and "</html>" in resp.text
        assert resp.headers["content-type"].startswith("text/html")


def test_spa_fallback_does_not_shadow_api_404(client):
    """An UNKNOWN /api/* path must keep the JSON 404 ({"detail": "Not Found"}); the SPA
    fallback only ever covers non-/api routes."""
    resp = client.get("/api/definitely-not-a-real-endpoint")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


def test_spa_fallback_still_serves_real_api_and_assets(client, seeded):
    """The fallback doesn't break the real surface: /api/* still returns JSON and the static
    root's real index.html is served at '/'."""
    api = client.get("/api/experiments")
    assert api.status_code == 200 and isinstance(api.json(), list)
    root = client.get("/")
    assert root.status_code == 200
    assert "<title>" in root.text


# ============================================================== SSE live progress


def test_stream_route_registered_event_stream(db_pool_greenfield):
    # The SSE endpoint is registered as a GET under /runs/{run_id}/stream and returns a
    # text/event-stream StreamingResponse. We don't consume the stream through the sync
    # TestClient (an infinite SSE body deadlocks its portal teardown); the framing is
    # exercised by test_stream_forwards_notify below.
    from uuid import uuid4

    from starlette.requests import Request

    from triage.dashboard.routes import router, stream

    route = next(r for r in router.routes if getattr(r, "name", "") == "stream")
    assert "GET" in route.methods
    assert route.path.endswith("/stream")

    req = Request({"type": "http", "method": "GET", "headers": []})
    resp = stream(req, uuid4(), pool=db_pool_greenfield)
    assert resp.media_type == "text/event-stream"


@pytest.mark.timeout(30)
def test_stream_forwards_notify(seeded, db_pool_greenfield):
    # Drive the SSE async generator directly (no TestClient): assert it (1) opens with the
    # ": connected" comment, (2) forwards a matching run_progress NOTIFY as a data frame, and
    # (3) filters out a notify for a different run. aclose() then unwinds the generator's
    # finally (closing its dedicated LISTEN connection) — the cancellable path in production.
    import asyncio

    import psycopg

    from triage.dashboard.routes import _run_progress_events

    conninfo = db_pool_greenfield.conninfo
    run_id = seeded.run_id

    async def _notify(payload: dict):
        async with await psycopg.AsyncConnection.connect(
            conninfo, autocommit=True
        ) as nconn:
            await nconn.execute(
                "select pg_notify('run_progress', %(p)s)",
                {"p": json.dumps(payload)},
            )

    async def drive():
        class _AlwaysConnected:
            async def is_disconnected(self):
                return False

        gen = _run_progress_events(_AlwaysConnected(), conninfo, run_id)
        frames: list[str] = []
        frames.append(await gen.__anext__())
        frames.append(await gen.__anext__())  # first keep-alive (no notify yet)

        await _notify(
            {"run_id": "00000000-0000-0000-0000-000000000000", "kind": "model"}
        )
        await _notify({"run_id": run_id, "kind": "model", "status": "built"})

        for _ in range(20):
            frame = await gen.__anext__()
            frames.append(frame)
            if frame.startswith("event: run_progress"):
                break
        await gen.aclose()
        return frames

    frames = asyncio.run(asyncio.wait_for(drive(), timeout=25))
    assert frames[0].startswith(": connected")
    data_frames = [f for f in frames if f.startswith("event: run_progress")]
    assert data_frames, f"no run_progress frame forwarded; got {frames!r}"
    payload = json.loads(data_frames[-1].split("data: ", 1)[1].strip())
    assert payload["run_id"] == run_id
    assert payload["kind"] == "model"
