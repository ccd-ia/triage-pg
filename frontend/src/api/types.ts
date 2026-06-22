/*
 * Typed API contract for the read dashboard.
 *
 * These types mirror the ACTUAL FastAPI responses in
 * src/triage/dashboard/routes.py (the §5 endpoints over the §3 SQL
 * views/functions of migration 0004). routes.py is the source of truth: the SPA
 * was first sketched against docs/read-dashboard-spec.md §5, then reconciled to
 * the shapes the API actually ships. Where the API returns a raw view row (bare
 * arrays, long-format metrics), the SPA reshapes client-side — those derived
 * shapes are marked `derived (client-side)`.
 *
 * Empty-state contract (routes.py `_empty`, spec §3.7): a panel whose source is
 * empty returns 200 with `{empty: true, reason, hint}` instead of an empty list,
 * so the SPA renders the state. Panels that can be empty are typed as
 * `<Rows> | EmptyState`.
 */

export type ProblemType = 'classification' | 'regression-as-ranking' | 'pure-regression'
export type RunStatus = 'started' | 'building' | 'completed' | 'failed'
export type ArtifactStatus = 'building' | 'built' | 'collected' | 'failed'

/** Pipeline stage kinds used by run_progress + the NOTIFY payload (spec §4). */
export type StageKind = 'cohort' | 'labels' | 'matrices' | 'models' | 'evaluate'

/** Selector provenance for the model driving the model-scoped panels (§3.5). */
export type SelectionSource = 'audition' | 'leaderboard' | 'manual'

/** Run-state of the selected-model bar (§3.5), derived client-side from run status. */
export type SelectionState = 'pending' | 'provisional' | 'final'

/** Empty-state envelope shared by panels whose source may be empty (routes.py `_empty`, §3.7). */
export interface EmptyState {
  empty: true
  reason: string
  hint: string
}

/** Narrowing helper for the empty-state envelope. */
export function isEmpty(x: unknown): x is EmptyState {
  return typeof x === 'object' && x !== null && (x as { empty?: unknown }).empty === true
}

/* -------------------------------------------------------------------------- */
/* GET /api/runs — rail list (triage.runs)                                    */
/* -------------------------------------------------------------------------- */

/** A row of triage.runs as returned by GET /runs (newest first). */
export interface RunListItem {
  run_id: string
  experiment_hash: string | null
  profile: string | null
  purpose: string | null
  status: RunStatus
  started_at: string // ISO-8601
  finished_at: string | null
  triage_version: string | null
  git_hash: string | null
  batch_job_id: string | null
}

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/summary — run_summary + per-split profiles (§3.1/§3.2)   */
/* -------------------------------------------------------------------------- */

/**
 * runs.plan denominators + temporal config (jsonb). Shape is open — routes.py
 * passes runs.plan through verbatim. The SPA reads `n_splits` (planned splits)
 * and the temporal window/frequency strings when present.
 */
export interface TemporalPlan {
  n_splits?: number | null
  label_timespan?: string | null
  history?: string | null
  [key: string]: unknown
}

/**
 * runs.experiment_config (jsonb) — the resolved experiment config. cohort/label
 * NAMES live here (NOT top-level on run_summary); the SPA reads
 * `cohort_name` / `label_name` from it. Open shape.
 */
export interface ExperimentConfig {
  cohort_name?: string | null
  label_name?: string | null
  [key: string]: unknown
}

/**
 * triage.run_summary — `select *`, so the column set follows migration 0004's
 * view. Typed permissively (everything optional/nullable) since routes.py does
 * not project a fixed list; the fields below are the ones the SPA reads.
 */
export interface RunSummary {
  run_id: string
  status: RunStatus
  profile?: string | null
  purpose?: string | null
  started_at: string
  finished_at?: string | null
  duration?: string | null // PG interval rendered as text, when the view exposes it
  problem_type?: ProblemType | null
  experiment_hash?: string | null
  /** Resolved config; cohort_name / label_name live in here (jsonb). */
  experiment_config?: ExperimentConfig | null
  /** runs.plan denominators + temporal config (jsonb). */
  plan?: TemporalPlan | null
  n_features?: number | null
  n_feature_groups?: number | null
  n_model_groups?: number | null
  n_models?: number | null
  estimator_types?: string[] | null
  random_seed?: number | null
  triage_version?: string | null
  git_hash?: string | null
  batch_job_id?: string | null
  /** runs.plan->engine_versions, incl. featurizer (e.g. {"featurizer":"v0.4.1"}). */
  engine_versions?: Record<string, string> | null
  // The view may expose more columns; the SPA only reads the ones above.
  [key: string]: unknown
}

/** triage.cohort_profile — entities per as_of_date (§3.2). */
export interface CohortProfilePoint {
  run_id: string
  as_of_date: string
  n_entities: number
}

/** triage.label_base_rate — positive rate per as_of_date (§3.2). */
export interface BaseRatePoint {
  run_id: string
  as_of_date: string
  label_timespan: string
  base_rate: number | null
  n_labeled: number
}

/**
 * GET /summary — composite of run_summary + cohort_profile + label_base_rate
 * (routes.py `run_summary`). NOTE the key is `label_base_rate` (not `base_rate`).
 */
export interface SummaryResponse {
  summary: RunSummary
  cohort_profile: CohortProfilePoint[]
  label_base_rate: BaseRatePoint[]
}

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/progress — run_progress (§3.3)                          */
/* -------------------------------------------------------------------------- */

/** triage.run_progress — one row per (kind, status) with a count. */
export interface RunProgressRow {
  run_id: string
  kind: string
  status: string
  n: number
}

/**
 * GET /progress — raw per-(kind,status) counts + the runs.plan denominators
 * (routes.py `run_progress`). The SPA folds these into per-stage done/current/
 * todo + N/M client-side (see deriveStages).
 */
export interface ProgressResponse {
  progress: RunProgressRow[]
  plan: TemporalPlan | null
}

/** Derived (client-side): one entry per pipeline stage for PipelineGraph. */
export interface StageProgress {
  kind: StageKind
  status: 'done' | 'current' | 'todo'
  /** Built count and planned denominator (N/M from runs.plan). */
  n: number
  m: number
  /** Optional headline detail. */
  detail?: string | null
}

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/derivation — {nodes, edges} (§3.6)                      */
/* -------------------------------------------------------------------------- */

export interface DerivationNode {
  artifact_id: string
  kind: string // cohort | labels | feature_group | matrix | model | source | ...
  status: ArtifactStatus | 'collected' | string
  built_by_run: string | null
  /** True when used by this run but built by a different run (cache hit, §3.6). */
  cache_hit: boolean
}

export interface DerivationEdge {
  parent_id: string
  artifact_id: string
}

export interface DerivationResponse {
  nodes: DerivationNode[]
  edges: DerivationEdge[]
}

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/audition?metric=&parameter=&rule= — ranking+curves+pick */
/* -------------------------------------------------------------------------- */

/** triage.audition ranking row (one per model_group). */
export interface AuditionRankRow {
  run_id: string
  metric: string
  parameter: string
  model_group_id: number
  n_splits_evaluated: number
  avg_value: number | null
  stddev_value: number | null
  avg_distance_from_best: number
  max_regret: number
}

/** triage.audition_distances — a model_group's per-split distance row. */
export interface AuditionCurveRow {
  run_id: string
  model_group_id: number
  metric: string
  parameter: string
  as_of_date: string
  raw_value: number | null
  best_value: number | null
  dist_from_best_case: number | null
}

export interface AuditionData {
  empty?: false
  metric: string
  parameter: string
  rule: string
  ranking: AuditionRankRow[]
  curves: AuditionCurveRow[]
  /** model_group_id of the rule's pick (audition_pick), or null. */
  pick: number | null
  k: number // evaluated splits
  n: number | null // planned splits (runs.plan->n_splits), may be null
  provisional: boolean
}

export type AuditionResponse = AuditionData | EmptyState

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/bias?model_id= — bias_metrics (long format) | empty     */
/* -------------------------------------------------------------------------- */

/** triage.bias_metrics — long format: one row per (model, attribute, value, metric). */
export interface BiasMetricRow {
  model_id: number
  split_kind: string
  as_of_date: string
  parameter: string
  attribute_name: string
  attribute_value: string
  metric: string
  value: number | null
  ref_group_value: string | null
  disparity: number | null
}

/** GET /bias returns a bare array of long-format rows, OR the empty envelope. */
export type BiasResponse = BiasMetricRow[] | EmptyState

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/leaderboard — triage.leaderboard (bare row array)       */
/* -------------------------------------------------------------------------- */

/** triage.leaderboard — one row per (model, metric, parameter, as_of_date). */
export interface LeaderboardRow {
  run_id: string
  model_group_id: number
  model_type: string | null
  split_kind: string
  metric: string
  parameter: string
  as_of_date: string
  value: number | null
  value_expected: number | null
  value_std: number | null
  model_id: number
  train_end_time: string | null
}

/** GET /leaderboard returns a bare array (may be empty until the matview is REFRESHed). */
export type LeaderboardResponse = LeaderboardRow[]

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/evaluations?metric= — raw test-split evaluations        */
/* -------------------------------------------------------------------------- */

/** triage.evaluations (test split, run-scoped) — one row per model/metric/split. */
export interface EvaluationRow {
  run_id: string
  model_id: number
  model_group_id: number
  split_kind: string
  as_of_date: string
  metric: string
  parameter: string
  value: number | null
  num_labeled: number | null
  num_positive: number | null
}

/** GET /evaluations returns a bare array; the SPA builds overlay series client-side. */
export type EvaluationsResponse = EvaluationRow[]

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/predictions?model_id=&k= — prediction_ranks | empty     */
/* -------------------------------------------------------------------------- */

/** triage.prediction_ranks — one scored entity. */
export interface PredictionRankRow {
  model_id: number
  entity_id: string | number
  as_of_date: string
  split_kind: string
  score: number
  scored_at: string
  rank_abs: number
  rank_pct: number | null
}

/** GET /predictions returns a bare array of ranked rows, OR the empty envelope. */
export type PredictionsResponse = PredictionRankRow[] | EmptyState

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/source-pins — run pins + registry head (drift)          */
/* -------------------------------------------------------------------------- */

/** triage.run_source_pins — a pin frozen at this run's plan time. */
export interface RunSourcePin {
  run_id: string
  source_name: string
  version_label: string | null
  fingerprint: string | null
}

/** triage.current_source_pins — the registry's current head per source. */
export interface CurrentSourcePin {
  source_name: string
  version_label: string | null
  registered_at: string | null
  fingerprint: string | null
}

/** GET /source-pins — the run's frozen pins + the registry's current head. */
export interface SourcePinsResponse {
  run_pins: RunSourcePin[]
  current: CurrentSourcePin[]
}

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/selected-model — selected_model (§3.5)                  */
/* -------------------------------------------------------------------------- */

export interface SelectedModelData {
  empty?: false
  metric: string
  parameter: string
  rule: string
  /** Audition pick's model_group + its concrete model (bigint ids; no labels). */
  audition_group: number | null
  audition_model: number | null
  /** Leaderboard #1 model + its group. */
  leaderboard_group: number | null
  leaderboard_model: number | null
  diverges: boolean
}

export type SelectedModelResponse = SelectedModelData | EmptyState

/* -------------------------------------------------------------------------- */
/* GET /api/models/{model_id} — feature_importances + per-split evals          */
/* -------------------------------------------------------------------------- */

/** triage.feature_importances — one feature's importance for a model. */
export interface FeatureImportanceRow {
  model_id: number
  feature: string
  feature_importance: number
  rank_abs: number | null
  rank_pct: number | null
}

/** triage.evaluations row for a single model (long format, all splits). */
export interface ModelEvaluationRow {
  model_id: number
  split_kind: string
  as_of_date: string
  metric: string
  parameter: string
  value: number | null
  value_expected: number | null
  value_std: number | null
  num_labeled: number | null
  num_positive: number | null
}

export interface ModelDetailResponse {
  model_id: number
  feature_importances: FeatureImportanceRow[]
  evaluations: ModelEvaluationRow[]
}

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/stream — SSE delta payload (spec §4)                    */
/* -------------------------------------------------------------------------- */

/** Stable NOTIFY/SSE payload contract (spec §4). */
export interface ProgressDelta {
  run_id: string
  kind: 'cohort' | 'labels' | 'feature_group' | 'matrix' | 'model' | 'evaluation' | 'run'
  status: 'building' | 'built' | 'failed' | 'completed'
}
