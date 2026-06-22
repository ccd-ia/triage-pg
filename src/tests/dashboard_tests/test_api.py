"""Contract tests for the read-dashboard JSON API (read-dashboard-spec §5).

Each endpoint is a thin SELECT over a 0004 view/function; these assert the JSON SHAPE the SPA
consumes (spec §5) and that the empty-state envelope (spec §3.7) fires when a panel's source is
empty. The SSE test is kept light (asserts the content type + that the stream connects); live
streaming is covered at integration.
"""

from __future__ import annotations

import json

import pytest


def test_list_runs(client, seeded_run):
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    runs = resp.json()
    assert isinstance(runs, list)
    ids = {r["run_id"] for r in runs}
    assert seeded_run.run_id in ids
    row = next(r for r in runs if r["run_id"] == seeded_run.run_id)
    # rail fields (spec §1 rail)
    for key in ("status", "profile", "purpose", "started_at", "batch_job_id"):
        assert key in row
    assert row["status"] == "completed"


def test_run_summary(client, seeded_run):
    resp = client.get(f"/api/runs/{seeded_run.run_id}/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"summary", "cohort_profile", "label_base_rate"}
    summary = body["summary"]
    assert summary["status"] == "completed"
    assert summary["problem_type"] == "classification"
    assert summary["plan"]["n_models"] == 9
    # per-split profiles: one row per as_of_date
    assert len(body["cohort_profile"]) == 3
    assert all(r["n_entities"] == 4 for r in body["cohort_profile"])
    assert len(body["label_base_rate"]) == 3
    assert all("base_rate" in r for r in body["label_base_rate"])


def test_run_progress(client, seeded_run):
    resp = client.get(f"/api/runs/{seeded_run.run_id}/progress")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"progress", "plan"}
    kinds = {r["kind"] for r in body["progress"]}
    assert {"cohort", "labels", "model"} <= kinds
    assert all(r["status"] == "built" for r in body["progress"])
    # denominators come from runs.plan
    assert body["plan"]["n_splits"] == 3


def test_derivation(client, seeded_run):
    resp = client.get(f"/api/runs/{seeded_run.run_id}/derivation")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"nodes", "edges"}
    node_ids = {n["artifact_id"] for n in body["nodes"]}
    assert "art-cohort" in node_ids and "art-labels" in node_ids
    # the labels<-cohort edge is in the closure
    assert {"parent_id": "art-cohort", "artifact_id": "art-labels"} in body["edges"]
    # nodes carry the cache_hit flag (this run built them -> not a cache hit)
    assert all(n["cache_hit"] is False for n in body["nodes"])


def test_audition(client, seeded_run):
    resp = client.get(
        f"/api/runs/{seeded_run.run_id}/audition",
        params={"metric": "auc_roc", "rule": "best_average_value"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert not body.get("empty")
    assert {"ranking", "curves", "pick", "k", "n", "provisional"} <= set(body)
    assert len(body["ranking"]) == 3  # 3 model_groups
    # best_average_value picks mg2 (per the fixture's hand-computed averages)
    assert body["pick"] == seeded_run.group_ids["mg2"]
    assert body["k"] == 3 and body["n"] == 3
    assert body["provisional"] is False
    # curves: per (group, split) distance rows
    assert len(body["curves"]) == 3 * 3


def test_audition_empty_state(client, empty_run):
    resp = client.get(f"/api/runs/{empty_run}/audition")
    assert resp.status_code == 200
    body = resp.json()
    assert body["empty"] is True
    assert "reason" in body and "hint" in body


def test_bias(client, seeded_run):
    resp = client.get(f"/api/runs/{seeded_run.run_id}/bias")
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list) and rows
    row = rows[0]
    for key in ("attribute_name", "attribute_value", "metric", "value", "disparity"):
        assert key in row
    assert row["attribute_name"] == "race"


def test_bias_empty_state(client, empty_run):
    resp = client.get(f"/api/runs/{empty_run}/bias")
    assert resp.status_code == 200
    body = resp.json()
    assert body["empty"] is True
    assert "protected_groups" in body["reason"]


def test_leaderboard(client, seeded_run):
    resp = client.get(f"/api/runs/{seeded_run.run_id}/leaderboard")
    assert resp.status_code == 200
    rows = resp.json()
    assert rows  # matview was refreshed in the fixture
    row = rows[0]
    for key in ("run_id", "model_group_id", "metric", "value", "model_id"):
        assert key in row
    assert row["run_id"] == seeded_run.run_id


def test_evaluations(client, seeded_run):
    resp = client.get(
        f"/api/runs/{seeded_run.run_id}/evaluations", params={"metric": "auc_roc"}
    )
    assert resp.status_code == 200
    rows = resp.json()
    # 3 groups x 3 splits for auc_roc
    assert len(rows) == 9
    assert all(r["metric"] == "auc_roc" for r in rows)
    assert all("as_of_date" in r and "value" in r for r in rows)


def test_predictions(client, seeded_run):
    resp = client.get(f"/api/runs/{seeded_run.run_id}/predictions", params={"k": 2})
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list) and rows
    row = rows[0]
    for key in ("entity_id", "score", "rank_abs", "rank_pct"):
        assert key in row
    # top-k applied (one model, 4 entities, k=2)
    assert len(rows) == 2
    assert rows[0]["rank_abs"] == 1


def test_predictions_empty_state(client, empty_run):
    resp = client.get(f"/api/runs/{empty_run}/predictions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["empty"] is True
    assert "no predictions" in body["reason"]


def test_source_pins(client, seeded_run):
    resp = client.get(f"/api/runs/{seeded_run.run_id}/source-pins")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"run_pins", "current"}
    assert body["run_pins"][0]["source_name"] == "customers"
    assert body["run_pins"][0]["version_label"] == "v1"
    assert body["current"][0]["source_name"] == "customers"


def test_selected_model(client, seeded_run):
    resp = client.get(
        f"/api/runs/{seeded_run.run_id}/selected-model",
        params={"metric": "auc_roc", "rule": "best_average_value"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # the 0004 selected_model function columns (spec §3.5)
    for key in (
        "audition_group",
        "audition_model",
        "leaderboard_group",
        "leaderboard_model",
        "diverges",
    ):
        assert key in body
    # default rule (best_average_value) -> mg2; leaderboard #1 (best_current_value) -> mg1
    assert body["audition_group"] == seeded_run.group_ids["mg2"]
    assert body["leaderboard_group"] == seeded_run.group_ids["mg1"]
    assert body["diverges"] is True
    assert body["audition_model"] == seeded_run.latest_model["mg2"]


def test_model_detail(client, seeded_run):
    model_id = seeded_run.all_models["mg1"][0]  # the first model (has importances)
    resp = client.get(f"/api/models/{model_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["model_id"] == model_id
    assert set(body) == {"model_id", "feature_importances", "evaluations"}
    assert {fi["feature"] for fi in body["feature_importances"]} == {"f1", "f2"}
    assert body["evaluations"]  # this model's per-split evals
    assert all(e["model_id"] == model_id for e in body["evaluations"])


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

    # Build the StreamingResponse directly to assert its media type (no body consumed).
    req = Request({"type": "http", "method": "GET", "headers": []})
    resp = stream(req, uuid4(), pool=db_pool_greenfield)
    assert resp.media_type == "text/event-stream"


@pytest.mark.timeout(30)
def test_stream_forwards_notify(seeded_run, db_pool_greenfield):
    # Drive the SSE async generator directly (no TestClient): assert it (1) opens with the
    # ": connected" comment, (2) forwards a matching run_progress NOTIFY as a data frame, and
    # (3) filters out a notify for a different run. aclose() then unwinds the generator's
    # finally (closing its dedicated LISTEN connection) — the cancellable path in production.
    import asyncio

    import psycopg

    from triage.dashboard.routes import _run_progress_events

    conninfo = db_pool_greenfield.conninfo
    run_id = seeded_run.run_id

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
        # (1) initial comment, then the first keep-alive — pulling the keep-alive guarantees
        # the generator has already run LISTEN and entered its poll loop, so notifies sent now
        # won't race ahead of the subscription.
        frames.append(await gen.__anext__())
        frames.append(await gen.__anext__())  # first keep-alive (no notify yet)

        # (3) a notify for a DIFFERENT run is filtered out. (2) the matching one is forwarded.
        await _notify(
            {"run_id": "00000000-0000-0000-0000-000000000000", "kind": "model"}
        )
        await _notify({"run_id": run_id, "kind": "model", "status": "built"})

        # Pull frames until we see the forwarded run_progress event (skip keep-alives).
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
