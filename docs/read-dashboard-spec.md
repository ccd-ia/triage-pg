# Read-dashboard spec (ADR-0012, first UI surface)

The first post-v1 UI surface: a **read-only** dashboard over the in-PG views (ADR-0007),
with no business logic of its own (ADR-0012 — anything it shows must already exist in a
view/CLI). The write webapp (config submission + project/user management) is a **separate,
later** surface with its own ADR, gated on the multi-project registry (ADR-0002), and is
out of scope here.

Chosen design (from `ai-docs/design/read-dashboard-mockups.html`): a **④ run-centric +
③ card-grid hybrid** — a master/detail **run monitor** (run-list rail + a live pipeline
DAG) for "the progress of the run online", and **result cards** (leaderboard, metric
trends, bias, predictions) for a completed run.

## 1. Layout

- **Left rail** — list of `triage.runs` (status badge: started/building/completed/failed,
  started_at, headline metric when done). Select a run → detail.
- **Detail, top — live pipeline DAG** (the ④ panel): nodes `cohort → labels → matrices →
  models → evaluate`, each with a status dot (done / current / todo) and a count
  (`matrices 3/5`). This is the live-progress view.
- **Detail, bottom — result cards** (the ③ panel, populated once a run completes):
  leaderboard, precision/AUC-over-time, bias group-bys, top predictions. Each card is an
  independent read of one view.

## 2. Panel → view contract (ADR-0012 audit)

| Panel | Source | Exists? |
|-------|--------|---------|
| Run list + status | `triage.runs` | ✓ table |
| Pipeline DAG (per-node status) | `triage.artifacts` filtered by `built_by_run = :run_id`, grouped by `kind` + `status` | ✓ (query, see §3) |
| Leaderboard | `triage.leaderboard` (matview) | ✓ |
| Metric-over-time | `triage.evaluations` | ✓ table |
| Bias / fairness | `triage.bias_metrics` | ✓ table |
| Top predictions | `triage.prediction_ranks` / `latest_predictions` | ✓ view |
| Source pins / drift (optional) | `triage.current_source_pins` | ✓ view |

**Gap:** none for results. The DAG panel needs only the two telemetry additions in §3 —
no new *results* view. A `triage.run_progress` view (§3) is a thin convenience wrapper, not
new business logic.

## 3. Live progress — `LISTEN/NOTIFY → SSE` push + REST poll for state

Two halves, per the grill:

- **State (initial load + reconnect): REST poll.** A `run_progress` read reconstructs the
  DAG from artifacts the run is building:
  ```sql
  create view triage.run_progress as
  select a.built_by_run as run_id, a.kind, a.status, count(*) as n
  from triage.artifacts a
  where a.built_by_run is not null
  group by a.built_by_run, a.kind, a.status;
  ```
  `built_by_run` (set at `begin_artifact`, status `building`) surfaces **in-flight** nodes,
  which `run_artifacts` (post-build usage edges) would miss. Node denominators
  (`matrices N/M`) come from the run's plan — see the telemetry additions.
- **Deltas (live): `pg_notify` → SSE.** Two small **core-telemetry** additions (not UI
  logic, so ADR-0012-clean):
  1. **Emit** `pg_notify('run_progress', json_build_object('run_id',…, 'kind',…,
     'status',…))` inside the existing `begin_artifact` / `mark_built` / `mark_failed`
     (`artifacts.py`) and the run status transitions (`run.py::_create_experiment_and_run`,
     `_mark_run`). NOTIFY fires on COMMIT — and each builder commits per
     `with pool.connection()` — so deltas land at node granularity.
  2. **Denominators:** record the planned node counts on the run (e.g. a `runs.plan` jsonb:
     `{splits, train+test matrices, grid×split models}`) computed once in
     `run_experiment` after timechop, so the DAG can show `N/M`. (Alternative: compute M in
     the dashboard from the experiment config — but recording it keeps the UI logic-free.)
- **Backend SSE endpoint** holds one psycopg3 `LISTEN run_progress` connection
  (`conn.notifies()` generator) and streams events to subscribed browsers; the browser
  re-fetches the `run_progress` view on connect and on each delta.

## 4. Stack

**FastAPI (read-only) + a thin frontend.** Rationale:
- Python-native (matches the stack); read-only endpoints are thin wrappers over the views.
- One natural home for the SSE endpoint (the `LISTEN` connection lives server-side).
- **Evolution path:** the write webapp (separate ADR) extends the *same* FastAPI backend
  with write endpoints over the registry — a Streamlit/Metabase read tool would force a
  second stack for the write phase. Chosen over BI tools (heavy external service, harder to
  keep the "views are the contract" discipline) and Streamlit/Dash (no clean write-phase
  path, weaker as a product).
- Deploy: Dockerized, NAS via the existing `synology-deploy` pattern (read-only, project-
  scoped); real auth arrives with the registry/webapp phase.
- Frontend: server-rendered + a small charting lib (or a light SPA); the mockup is the
  visual target. Endpoints: `GET /runs`, `GET /runs/{id}/progress`, `GET
  /runs/{id}/leaderboard|evaluations|bias|predictions`, `GET /runs/{id}/stream` (SSE).

## 5. Out of scope / deferred
- **Write webapp** — its own ADR; gated on ADR-0002 registry (auth, project/user mgmt,
  config submission). Not specified here.
- **Auth** — the read dashboard is project-scoped/trusted for v1; real auth lands with the
  registry.
- **Cloud runs** — a Batch-submitted run (`profile='cloud'`) writes the same `runs`/
  `artifacts` rows, so the monitor works unchanged; surfacing `batch_job_id` / Batch
  terminal state is a thin add (ties to `docs/cloud-profile-spec.md` §7).

See ADR-0021 for the live-progress mechanism decision.
