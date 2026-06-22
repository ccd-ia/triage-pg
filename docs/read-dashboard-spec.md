# Read-dashboard spec (ADR-0012, first UI surface)

The first post-v1 UI surface: a **read-only** dashboard over the in-PG views (ADR-0007),
with no business logic of its own (ADR-0012 — anything it shows must already exist in a
view/function/CLI). The write webapp (config submission + project/user management) is a
**separate, later** surface with its own ADR, gated on the multi-project registry (ADR-0002),
and is out of scope here.

**Chosen design** (mockups: `ai-docs/design/read-dashboard-mockups.html` (v1) +
`read-dashboard-options.html` (resolved options)): a **run-centric master/detail +
card-grid hybrid** — a run-list rail, an experiment-summary strip, a **tabbed run monitor**
(Pipeline · Derivation · Audition · Bias), an explicit **selected-model** control, and a
grid of result cards, with a model-detail drill-down.

**Build decisions (2026-06-21):**
- **Stack** = FastAPI **JSON API** + a **SPA in React + Vite**. EventSource for SSE. Backend
  already settled (ADR-0012/0021); SPA chosen over server-rendered for the live DAG / tab
  state / selector interactivity; React over Svelte for ecosystem + maintainer familiarity
  (handover in a teaching/consulting context) — DAG viz is a wash (xyflow ships both).
- **`triage.audition` ships as SQL now** — the audition tab + the selector's default
  "audition pick" need distance-from-best / regret as a view/function (today it's Python),
  so the UI stays logic-free (ADR-0012). New per-project migration (`0004_audition`).
- These are UI defaults / thin telemetry, not hard-to-reverse architecture, so **no new
  ADR** (only ADR-0021 for the live mechanism). The selector default (audition over
  leaderboard) is recorded here, not as an ADR.

---

## 1. Layout

Left **rail** → detail. Detail top-to-bottom: summary strip, tabbed monitor, selected-model
bar, result-card grid, model-detail drill-down.

- **Rail** — list of `triage.runs` (status badge started/building/completed/failed,
  started_at, headline metric when done). Select a run → detail.
- **Summary strip** (always visible) — `problem_type · status · cohort · base rate ·
  #splits · #features · #models · run_id`. A condensed read of §3.1.
- **Tabbed run monitor** — one panel, four tabs:
  1. **Pipeline progress** — linear DAG `cohort → labels → matrices → models → evaluate`,
     status dot (done/current/todo) + `N/M` counts. Live (§4).
  2. **Derivation graph** (Guix closure, full-width on its own tab) — nodes =
     `triage.artifacts` in the run closure, edges = `triage.artifact_inputs`, colored by
     status (built/building/collected) with cache-hits shaded. Provenance / reproducibility.
  3. **Audition** — distance-from-best / regret curves + model_group ranking for the active
     strategy; **live + `provisional · k/N splits`** until the run completes (the ranking can
     flip as later splits land). Empty-state until ≥2 model_groups across ≥2 evaluated splits.
  4. **Bias** — TPR/FPR/PPV group-bys for the selected model; **live** (fills per evaluated
     model/split). §3-B empty-state when the experiment has no `protected_groups` config.
- **Selected-model bar** — `Model-specific panels show: <model> ▾ from:[audition|leaderboard
  |manual]` + a **divergence flag** when leaderboard #1 ≠ audition pick. Default = audition
  pick; run-state fallback `pending → provisional → final`. Drives Top-predictions + the
  model-detail drill-down. (§3.5.)
- **Result-card grid** — Experiment summary (full card: §3.1 scalars + §3.2 per-split
  sparklines), Leaderboard, Metric-over-time, Top predictions, Source pins/drift.
- **Model-detail drill-down** — for the selected model: feature importances + per-split
  evaluations. (Individual-prediction importances deferred.)

## 2. Panel → source contract (ADR-0012 audit)

| Panel | Zone | Source | Status |
|-------|------|--------|--------|
| Run list + status | rail | `triage.runs` | ✓ table |
| Summary strip / Experiment summary card | strip + card | `triage.run_summary` (§3.1) | + view |
| cohort-size / base-rate per split | summary card | `triage.cohort_profile` / `label_base_rate` (§3.2) | + view |
| Pipeline DAG (per-node status) | tab | `triage.run_progress` (§3.3) | + view |
| Derivation graph | tab | `triage.artifacts` + `artifact_inputs` (§3.6) | ✓ tables |
| Audition (curves + ranking + pick) | tab | `triage.audition` (§3.4) | **+ view (build now)** |
| Bias / fairness | tab | `triage.bias_metrics` | ✓ table (empty-state §3.7) |
| Leaderboard | card | `triage.leaderboard` (matview) | ✓ |
| Metric-over-time | card | `triage.evaluations` | ✓ table |
| Top predictions | card | `triage.prediction_ranks` (by `model_id`) | ✓ view |
| Source pins / drift | card | `triage.current_source_pins` | ✓ view |
| Selected model + divergence | bar | `triage.selected_model` (§3.5) | + view |
| Model detail: feature importances | drill | `triage.feature_importances` (by `model_id`) | ✓ table |
| Model detail: per-split evaluations | drill | `triage.evaluations` (by `model_id`) | ✓ table |

**New read surfaces to add** (all thin wrappers / SQL math over existing tables — no new
business logic enters the system, ADR-0012 holds): `run_summary`, `cohort_profile`,
`label_base_rate`, `run_progress`, `audition`, `selected_model`, and a `runs.plan` jsonb
column for DAG denominators.

## 3. Read views & functions to add (the SQL contract)

**Run-scoping (grounded in the 0001 baseline schema).** Nothing is naively `run_id`-keyed:
- `evaluations` / `bias_metrics` / `feature_importances` / `predictions` are keyed by
  `model_id`; scope to a run via `triage.models.run_id` (`models` has `run_id`, `model_group_id`,
  `train_end_time`).
- `cohorts` / `labels` are keyed by `cohort_hash` / `label_hash` (= the artifact_id); scope to
  a run via `triage.run_artifacts` (run → artifact_id) filtered by `artifacts.kind`.
- `triage.runs` carries `random_seed`, `triage_version`, `git_hash`, `batch_job_id` directly
  (no `config` column); the experiment config jsonb is on `triage.experiments.config`.
- The `triage.leaderboard` **matview has no `run_id` and no `rank`** today (cols:
  model_group_id, model_type, metric, parameter, as_of_date, value, value_expected,
  value_std, model_id, train_end_time). Add `models.run_id` to it (small matview change) so it
  is run-scopable, or compute top-model in `selected_model` (§3.5).
- **Metric direction**: auc/precision/recall/AP are higher-is-better (`best = max`); RMSE/MAE
  are lower-is-better (`best = min`). `triage.audition` must branch on direction. (Note:
  `evaluations.value_best/value_worst` are a *single model's* tie bounds — NOT the
  cross-model_group best; don't reuse them for distance-from-best.)

### 3.1 `triage.run_summary`
One row per run for the strip + summary card. Join `runs` (+ `runs.plan`) × `experiments` ×
aggregates:
```sql
create view triage.run_summary as
select r.run_id, r.status, r.profile, r.started_at, r.finished_at,
       (r.finished_at - r.started_at)            as duration,
       e.problem_type, e.experiment_hash,
       e.config->>'cohort_name'                  as cohort_name,    -- experiments.config jsonb
       e.config->>'label_name'                   as label_name,
       r.plan->'temporal'                        as temporal,        -- #splits, windows, freqs
       (r.plan->>'n_features')::int              as n_features,
       (r.plan->>'n_feature_groups')::int        as n_feature_groups,
       (r.plan->>'n_model_groups')::int          as n_model_groups,
       (r.plan->>'n_models')::int                as n_models,
       r.plan->'estimator_types'                 as estimator_types,
       r.random_seed,                                                -- column on triage.runs
       r.triage_version, r.git_hash, r.batch_job_id,                 -- columns on triage.runs
       r.plan->'engine_versions'                 as engine_versions  -- incl. featurizer
from triage.runs r join triage.experiments e using (experiment_hash);
```
`runs.plan` (jsonb) is the ADR-0021 telemetry column — written once in `run_experiment`
after timechop (splits/windows, train+test matrix counts, grid×split model counts, feature
counts, estimator types, engine versions). Source pins read separately from
`current_source_pins`.

### 3.2 Per-split profiles (the point-in-time view)
Two small grouped reads (cohort size and base rate vary per `as_of_date` — base-rate-over-
time is the cardinal stability signal):
`cohorts`/`labels` are keyed by their artifact hash, so scope to a run through
`run_artifacts` + `artifacts.kind`:
```sql
create view triage.cohort_profile as            -- entities per as_of_date, per run
select ra.run_id, c.as_of_date, count(distinct c.entity_id) as n_entities
from triage.run_artifacts ra
join triage.artifacts a on a.artifact_id = ra.artifact_id and a.kind = 'cohort'
join triage.cohorts   c on c.cohort_hash = ra.artifact_id
group by ra.run_id, c.as_of_date;

create view triage.label_base_rate as           -- positive rate per as_of_date, per run
select ra.run_id, l.as_of_date, l.label_timespan,
       avg(l.outcome) filter (where l.outcome is not null) as base_rate,
       count(*)       filter (where l.outcome is not null) as n_labeled
from triage.run_artifacts ra
join triage.artifacts a on a.artifact_id = ra.artifact_id and a.kind = 'labels'
join triage.labels    l on l.label_hash  = ra.artifact_id
group by ra.run_id, l.as_of_date, l.label_timespan;
```

### 3.3 `triage.run_progress` (ADR-0021) + `runs.plan`
```sql
create view triage.run_progress as
select a.built_by_run as run_id, a.kind, a.status, count(*) as n
from triage.artifacts a where a.built_by_run is not null
group by a.built_by_run, a.kind, a.status;
```
`built_by_run` (set at `begin_artifact`, status `building`) surfaces in-flight nodes that
the post-build `run_artifacts` edges would miss. Denominators (`matrices N/M`) come from
`runs.plan`.

### 3.4 `triage.audition` (NEW — build now)
PL/pgSQL over `triage.evaluations`, mirroring the in-PG metric functions (ADR-0007,
migration 0002). For a run, per `metric` (test split), per `as_of_date`, the *best* value
across model_groups is `max(value)`; each model_group's **distance-from-best** at that split
is `best - value`; **regret** = its max distance over splits; the **strategy rank** is by
`avg(distance_from_best)` (or `max(distance_from_best)` = min-max-regret):
Run-scoped via `models.run_id`; `model_group_id` comes from `models` (evaluations don't carry
it). `best` branches on metric direction (a small `higher_is_better(metric)` helper, or a
direction column on a metrics catalog):
```sql
create view triage.audition as
with ev as (   -- run-scoped evals with their model_group, test split only
  select m.run_id, m.model_group_id, e.metric, e.as_of_date, e.value
  from triage.evaluations e join triage.models m using (model_id)
  where e.split_kind = 'test' and e.value is not null
), per_split as (
  select run_id, model_group_id, metric, as_of_date, value,
         case when triage.higher_is_better(metric)
              then max(value) over w else min(value) over w end as best
  from ev window w as (partition by run_id, metric, as_of_date)
)
select run_id, metric, model_group_id,
       avg(abs(best - value)) as avg_distance_from_best,
       max(abs(best - value)) as max_regret,
       count(*)               as n_splits_evaluated
from per_split group by run_id, metric, model_group_id;

-- the pick the selector reads (lowest avg distance-from-best for the strategy metric):
create function triage.audition_pick(p_run uuid, p_metric text)
  returns bigint language sql stable as $$
  select model_group_id from triage.audition
  where run_id = p_run and metric = p_metric
  order by avg_distance_from_best asc, max_regret asc, model_group_id asc limit 1
$$;
```
**Selection rules (port the full standard triage catalog** from
`src/triage/component/audition/selection_rules.py`). `audition_pick` generalizes to
`audition_pick(p_run, p_metric, p_rule, p_params jsonb)`; each rule is a deterministic
ranking over the run's `evaluations` (test split), tie-broken by `model_group_id`:

| rule | picks the model_group with… | params |
|------|------------------------------|--------|
| `best_current_value` | best metric on the most recent `train_end_time` | — |
| `best_average_value` | best mean metric over all train_end_times | — |
| `lowest_metric_variance` | lowest metric variance over time (most stable) | — |
| `most_frequent_best_dist` | most train_end_times within ε of the best | `dist_window` (ε) |
| `best_average_two_metrics` | best weighted avg of two metrics | `metric2, param2, metric1_weight` |
| `best_avg_var_penalized` | best `mean − λ·std` over time | `stdev_penalty` (λ) |
| `best_avg_recency_weight` | best recency-weighted mean | `curr_weight, decay_type` |
| `random_model_group` | a random group (baseline) | `seed` |

The `triage.audition` view supplies the shared building blocks per (run, metric,
model_group): current value, avg, variance, recency-weighted avg, and distance-from-best /
regret (the comparison surfaces — port `distance_from_best.py`, `regrets.py`). Direction
comes from the centralized `higher_is_better` helper (§8, ported from
`audition/metric_directionality.py`). The §3.5 selector default uses
`best_current_value` on the run's primary metric unless overridden.

**Validation**: as the 0002 metric functions were checked vs sklearn, validate each SQL rule
against `selection_rules.py` on a seeded fixture (the audition tests at
`src/tests/audition_tests/` are the oracle).

### 3.5 `triage.selected_model` (the §2-C default + divergence)
A **function** (a view can't take the strategy `metric`) giving the bar its defaults
logic-free; *manual* override is client state (the SPA passes a `model_id` to the
model-scoped endpoints). Leaderboard #1 is computed directly (the matview has no `run_id`/
`rank`) by ranking the latest-split evaluations within the run+metric:
```sql
create function triage.selected_model(p_run uuid, p_metric text)
  returns table (audition_group bigint, leaderboard_model bigint,
                 leaderboard_group bigint, diverges boolean)
  language sql stable as $$
  with lb as (   -- leaderboard #1 for this run+metric on the latest evaluated split
    select m.model_id, m.model_group_id
    from triage.evaluations e join triage.models m using (model_id)
    where m.run_id = p_run and e.metric = p_metric and e.split_kind = 'test'
      and e.as_of_date = (select max(as_of_date) from triage.evaluations e2
                          join triage.models m2 using (model_id)
                          where m2.run_id = p_run and e2.split_kind = 'test')
    order by case when triage.higher_is_better(p_metric) then e.value end desc nulls last,
             case when triage.higher_is_better(p_metric) then null else e.value end asc,
             m.model_id
    limit 1
  )
  select triage.audition_pick(p_run, p_metric), lb.model_id, lb.model_group_id,
         triage.audition_pick(p_run, p_metric) is distinct from lb.model_group_id
  from lb;
$$;
```
The bar resolves the audition pick's concrete `model_id` as its latest-split model. Run-state:
`pending` (no evaluated models) → `provisional` (audition over k<N splits) → `final` (k=N).

### 3.6 Derivation graph
Nodes = `select artifact_id, kind, status from triage.artifacts` in the run closure; edges =
`select parent_id, artifact_id from triage.artifact_inputs`, scoped to the closure (walk from
the run's artifacts). Cache-hit = an artifact `record_use`-d by this run but `built_by_run` a
*different* run. Returned as `{nodes, edges}` for the SPA's DAG renderer.

### 3.7 Empty-state triggers (the §3-B pattern, generalized)
Not views — documented response when a panel's source is empty:
- **Bias** — no `protected_groups` rows for the run → "No protected_groups configured" +
  how-to + docs link (ADR-0007).
- **Audition** — `< 2 model_groups` across `< 2` evaluated splits → "needs ≥2 model_groups /
  ≥2 evaluated splits."
- **Top predictions** — no completed scoring run → "no predictions yet."
Each endpoint returns `200` with `{empty: true, reason, hint}` so the SPA renders the state.

## 4. Live progress — `pg_notify → SSE` + REST poll

- **State (load / reconnect): REST.** The SPA fetches the relevant view on mount and on each
  delta. `run_progress` reconstructs the pipeline DAG; audition/bias/metric panels read their
  views directly (live, provisional during a run); the **leaderboard matview** refreshes at
  run end (final), so during a run the metric-over-time card (raw `evaluations`) is the live
  view and the leaderboard shows "finalizing."
- **Deltas (live): one `run_progress` NOTIFY channel.** Emit `pg_notify('run_progress',
  json_build_object('run_id',…, 'kind',…, 'status',…))` inside `begin_artifact` /
  `mark_built` / `mark_failed` (`artifacts.py`), the run status transitions
  (`run.py`), **and when an evaluations batch commits** (`in_pg_evaluation` →
  `kind:'evaluation'`) so the audition/bias/metric tabs re-fetch live. NOTIFY fires on
  COMMIT; each builder commits per `with pool.connection()`, so deltas land at node
  granularity. **Payload contract** (becomes stable): `{run_id: uuid, kind: text in
  (cohort|labels|feature_group|matrix|model|evaluation|run), status: text in
  (building|built|failed|completed)}`.
- **SSE endpoint** holds one psycopg3 `LISTEN run_progress` connection (`conn.notifies()`),
  separate from the request pool, and streams events to subscribed browsers. NOTIFY with no
  LISTENer is a no-op, so headless runs are unaffected; polling the views remains a complete
  fallback.

## 5. HTTP API (FastAPI, JSON, read-only)

```
GET /api/runs                              -> rail list (triage.runs)
GET /api/runs/{id}/summary                 -> run_summary + cohort_profile + label_base_rate
GET /api/runs/{id}/progress                -> run_progress (+ N/M from runs.plan)
GET /api/runs/{id}/derivation              -> {nodes, edges} (artifacts ⋈ artifact_inputs)
GET /api/runs/{id}/audition?metric=&strategy= -> ranking + curves + pick + {provisional,k,N}
GET /api/runs/{id}/bias?model_id=          -> bias_metrics | {empty,reason,hint}
GET /api/runs/{id}/leaderboard             -> triage.leaderboard
GET /api/runs/{id}/evaluations?metric=     -> metric-over-time
GET /api/runs/{id}/predictions?model_id=&k= -> prediction_ranks (top-k)
GET /api/runs/{id}/source-pins             -> current_source_pins
GET /api/runs/{id}/selected-model          -> selected_model (audition pick, lb #1, diverges)
GET /api/models/{model_id}                 -> feature_importances + per-split evaluations
GET /api/runs/{id}/stream                  -> text/event-stream (SSE, run_progress)
```
Each endpoint is a thin `SELECT` over a view (psycopg3 pool, `dict_row`). No endpoint
contains selection/metric logic — that lives in the views/functions (ADR-0012).

## 6. Frontend (React + Vite SPA)

- **Routes**: `/` (rail + most-recent run), `/runs/:id` (detail), model detail as a
  drill panel/modal driven by the selected `model_id` (React Router).
- **Components**: `RunRail`, `SummaryStrip`, `RunMonitor` (Tabs: Pipeline, Derivation,
  Audition, Bias), `SelectedModelBar`, `ResultCards` (ExperimentSummary, Leaderboard,
  MetricOverTime, TopPredictions, SourcePins), `ModelDetail`.
- **State**: current run; selected model `{source: audition|leaderboard|manual, model_id}`
  (default from `/selected-model`); a single `EventSource('/api/runs/:id/stream')` whose
  deltas trigger re-fetch of the affected panels (pipeline always; audition/bias/metric on
  `kind in (model, evaluation)`). Server state via a small fetch layer (TanStack Query or
  plain hooks) — the panels are independent reads, so per-panel fetch + SSE invalidation.
- **Graphs**: **`@xyflow/react`** (React Flow) for the pipeline DAG + the Guix derivation
  graph; **recharts** (or visx) for metric-over-time / audition curves / per-split sparklines.
- **Build/serve**: Vite build; **FastAPI serves the static bundle** (single artifact, single
  deploy) under `/`, the API under `/api`.

## 7. Deploy
Dockerized (the greenfield root `Dockerfile`), NAS via the existing `synology-deploy`
pattern: one container runs FastAPI (serving `/api` + the built SPA), project-scoped and
read-only. Real auth lands with the registry/webapp phase (ADR-0002). A Batch-submitted
cloud run (ADR-0005) writes the same `runs`/`artifacts`/`evaluations`, so the monitor is
profile-agnostic; surfacing `batch_job_id` is a thin add (cloud-profile-spec §7).

## 8. Build sequence
1. **SQL layer** — migration `0004_dashboard_reads`: a centralized
   `triage.higher_is_better(metric)` helper (port `audition/metric_directionality.py`),
   `triage.audition` + `audition_pick` (the full 8-rule catalog ported from
   `selection_rules.py`, validated vs `src/tests/audition_tests/`) + `selected_model`
   (function), the read views (`run_summary`, `cohort_profile`, `label_base_rate`,
   `run_progress`), add `run_id` to the `leaderboard` matview, and the `runs.plan` jsonb
   column; populate `plan` in `run_experiment` after timechop.
2. **NOTIFY telemetry** — emit `run_progress` in `artifacts.py`, `run.py`, and the
   evaluation commit (payload per §4).
3. **FastAPI** — the §5 endpoints (incl. the SSE `LISTEN` connection); contract tests over a
   seeded DB.
4. **SPA** — the §6 React app against the API; wire SSE + the selected-model bar.
5. **Dockerize + NAS deploy.**

## 9. Out of scope / deferred
- **Write webapp** — own ADR; gated on the ADR-0002 registry (auth, project/user mgmt,
  config submission).
- **Auth** — read dashboard is project-scoped/trusted for v1; real auth with the registry.
- **Individual-prediction importances** in model-detail (drill-down is feature importances +
  per-split evals only for v1).
- **Cross-experiment comparison** — the unit of view stays one run/experiment.

See ADR-0021 for the live-progress mechanism decision.
