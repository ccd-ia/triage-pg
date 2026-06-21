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
- **Detail, top — artifact derivation graph** (the *Guix-style* DAG, ADR-0013–0017): a
  visualization of the run's artifact closure — nodes = `triage.artifacts` used by the run,
  edges = `triage.artifact_inputs(parent_id → artifact_id)` — colored by `status`
  (built / building / collected). This is the "tracking" graph: it shows *what was derived
  from what* (cohort/labels → feature_group → matrices → models), including cache hits, and
  doubles as the reproducibility/provenance view. See §3.5.
- **Detail, bottom — result cards** (the ③ panel, populated once a run completes): the full
  card set in §2. Each card is an independent read of one view.
- **Drill-down** (scope: single run, but navigable into its models): a result row drills
  `run → model_group (triage.model_groups) → model (triage.models)`; a selected model opens
  a **model-detail** view — its feature importances, top individual predictions, and
  per-split evaluations. The unit of view stays one run/experiment (all its temporal splits
  + grid); cross-*experiment* comparison is deferred.

## 2. Panel → view contract (ADR-0012 audit)

| Panel | Zone | Source | Exists? |
|-------|------|--------|---------|
| Run list + status | rail | `triage.runs` | ✓ table |
| Pipeline DAG (per-node status) | ④ | `triage.artifacts` filtered by `built_by_run = :run_id`, grouped by `kind` + `status` | ✓ (query, §3) |
| Artifact derivation graph (Guix DAG) | ④ | `triage.artifacts` (nodes) + `triage.artifact_inputs` (edges), scoped to the run closure | ✓ (query, §3.5) |
| Leaderboard | ③ | `triage.leaderboard` (matview) | ✓ |
| Metric-over-time | ③ | `triage.evaluations` | ✓ table |
| Bias / fairness | ③ | `triage.bias_metrics` | ✓ table |
| Top predictions | ③ | `triage.prediction_ranks` / `latest_predictions` | ✓ view |
| Model selection / audition | ③ | audition modules (`distance_from_best`, `regrets`, `model_group_performance`) over `triage.evaluations` | ✓ (Python/SQL; may want a view) |
| Source pins / drift | ③ | `triage.current_source_pins` | ✓ view |
| **Model-group → model drill-down** | drill | `triage.model_groups` → `triage.models` | ✓ tables |
| **Model detail: feature importances** | drill | `triage.feature_importances` (by `model_id`) | ✓ table |
| **Model detail: individual predictions** | drill | `triage.prediction_ranks` / `triage.individual_importances` (by `model_id`) | ✓ |
| **Model detail: per-split evaluations** | drill | `triage.evaluations` (by `model_id`) | ✓ table |

**Gap:** none for results — every panel maps to an existing view/table (ADR-0012 holds).
New read-only convenience views (`triage.run_progress` §3, optionally a
`triage.audition` view to keep audition logic out of the UI) are thin wrappers, not new
business logic. The audition curves (regret / distance-from-best) should resolve to a SQL
view so the dashboard stays logic-free — if the audition math only exists in Python today,
porting it to a view is a small core task this spec flags.

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
