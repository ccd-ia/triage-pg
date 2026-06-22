/*
 * Typed API contract for the read dashboard.
 *
 * These types mirror the FIXED JSON contract in docs/read-dashboard-spec.md §5
 * (the HTTP endpoints) over the §3 SQL views/functions. The backend is not yet
 * running; the dev fixture (src/fixtures) produces values of exactly these
 * shapes so the SPA renders standalone. Where a §5 endpoint's JSON shape was
 * not pinned down to the field level by the spec, the chosen shape is noted with
 * an `AMBIGUOUS` comment so it can be reconciled at integration.
 */

export type ProblemType = 'classification' | 'regression-as-ranking' | 'pure-regression'
export type RunStatus = 'started' | 'building' | 'completed' | 'failed'
export type ArtifactStatus = 'building' | 'built' | 'collected' | 'failed'

/** Pipeline stage kinds used by run_progress + the NOTIFY payload (spec §4). */
export type StageKind = 'cohort' | 'labels' | 'matrices' | 'models' | 'evaluate'

/** Selector provenance for the model driving the model-scoped panels (§3.5). */
export type SelectionSource = 'audition' | 'leaderboard' | 'manual'

/** Run-state of the selected-model bar (§3.5). */
export type SelectionState = 'pending' | 'provisional' | 'final'

/* -------------------------------------------------------------------------- */
/* GET /api/runs — rail list (triage.runs)                                    */
/* -------------------------------------------------------------------------- */

export interface RunListItem {
  run_id: string
  status: RunStatus
  started_at: string // ISO-8601
  /** Short human label for the run (e.g. "deep-grid", "one-hot cats"). */
  label: string
  /** Headline metric once done, e.g. "auc 0.574"; null while building/failed. */
  headline_metric: string | null
  /** Sub-line for in-flight runs, e.g. "matrices 3/8 · eval 3/8"; optional. */
  progress_line?: string | null
}

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/summary — run_summary + per-split profiles (§3.1/§3.2)   */
/* -------------------------------------------------------------------------- */

export interface TemporalPlan {
  n_splits: number
  /** e.g. "6mo"; window/frequency human strings from runs.plan->temporal. */
  label_timespan?: string
  history?: string
}

export interface SourcePinSummary {
  source: string
  pin: string
}

export interface RunSummary {
  run_id: string
  status: RunStatus
  profile: string
  started_at: string
  finished_at: string | null
  duration: string | null // PG interval rendered as text
  problem_type: ProblemType
  experiment_hash: string
  cohort_name: string | null
  label_name: string | null
  temporal: TemporalPlan | null
  n_features: number | null
  n_feature_groups: number | null
  n_model_groups: number | null
  n_models: number | null
  estimator_types: string[] | null
  random_seed: number | null
  triage_version: string | null
  git_hash: string | null
  batch_job_id: string | null
  /** runs.plan->engine_versions, incl. featurizer (e.g. {"featurizer":"v0.4.1"}). */
  engine_versions: Record<string, string> | null
}

/** triage.cohort_profile — entities per as_of_date (§3.2). */
export interface CohortProfilePoint {
  as_of_date: string
  n_entities: number
}

/** triage.label_base_rate — positive rate per as_of_date (§3.2). */
export interface BaseRatePoint {
  as_of_date: string
  label_timespan: string
  base_rate: number | null
  n_labeled: number
}

/**
 * AMBIGUOUS (§5): GET /summary maps to "run_summary + cohort_profile +
 * label_base_rate". The composite envelope (this shape) is chosen so the strip
 * + summary card get one fetch. Reconcile field nesting at integration.
 */
export interface SummaryResponse {
  summary: RunSummary
  cohort_profile: CohortProfilePoint[]
  base_rate: BaseRatePoint[]
}

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/progress — run_progress (§3.3)                          */
/* -------------------------------------------------------------------------- */

export interface StageProgress {
  kind: StageKind
  status: 'done' | 'current' | 'todo'
  /** Built count and planned denominator (N/M from runs.plan). */
  n: number
  m: number
  /** Optional headline detail, e.g. "2,140 rows", "23.8% pos". */
  detail?: string | null
}

export interface ProgressResponse {
  run_id: string
  stages: StageProgress[]
}

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/derivation — {nodes, edges} (§3.6)                      */
/* -------------------------------------------------------------------------- */

export interface DerivationNode {
  artifact_id: string
  kind: string // cohort | labels | feature_group | matrix | model | source | ...
  status: ArtifactStatus | 'cachehit' | 'todo'
  label: string
  /** True when used by this run but built by a different run (cache hit, §3.6). */
  cache_hit?: boolean
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
/* GET /api/runs/{id}/audition — ranking + curves + pick (§3.4)               */
/* -------------------------------------------------------------------------- */

export interface AuditionRankRow {
  model_group_id: number
  label: string // human model_group label, e.g. "RF·d3"
  avg_distance_from_best: number
  max_regret: number
  n_splits_evaluated: number
  is_pick: boolean
}

/** A single model_group's distance-from-best curve across evaluated splits. */
export interface AuditionCurve {
  model_group_id: number
  label: string
  points: { as_of_date: string; distance_from_best: number }[]
}

/** Empty-state envelope shared by panels whose source may be empty (§3.7). */
export interface EmptyState {
  empty: true
  reason: string
  hint: string
}

export interface AuditionData {
  empty?: false
  run_id: string
  metric: string
  strategy: string
  provisional: boolean
  k: number // evaluated splits
  n: number // planned splits
  ranking: AuditionRankRow[]
  curves: AuditionCurve[]
}

export type AuditionResponse = AuditionData | EmptyState

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/bias — bias_metrics | empty (§3.7)                      */
/* -------------------------------------------------------------------------- */

export interface BiasRow {
  group_attribute: string // e.g. "facility_type"
  group_value: string // e.g. "restaurant"
  tpr: number
  fpr: number
  ppv: number
  n: number
  /** Optional flagged disparity vs reference group, e.g. 0.12. */
  disparity?: number | null
}

export interface BiasData {
  empty?: false
  model_id: number
  rows: BiasRow[]
}

export type BiasResponse = BiasData | EmptyState

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/leaderboard — triage.leaderboard                        */
/* -------------------------------------------------------------------------- */

export interface LeaderboardRow {
  model_id: number
  model_group_id: number
  label: string // human model label, e.g. "RF·d3·n10"
  /** Metric -> value; UI reads p@10%, p@100, auc, ap when present. */
  metrics: Record<string, number>
  /** True for the audition pick; "← #1 by X" rendered from rank_metric. */
  is_audition_pick?: boolean
  /** When this row is leaderboard #1 by a metric, the metric name. */
  rank_metric?: string | null
}

export interface LeaderboardResponse {
  run_id: string
  rows: LeaderboardRow[]
}

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/evaluations?metric= — metric-over-time                  */
/* -------------------------------------------------------------------------- */

export interface MetricSeriesPoint {
  as_of_date: string
  value: number | null
}

export interface MetricSeries {
  metric: string
  points: MetricSeriesPoint[]
}

/**
 * AMBIGUOUS (§5): GET /evaluations?metric= returns "metric-over-time". The
 * card overlays two series (e.g. p@10% + auc), so the response carries a list
 * of series. If the backend keys by a single metric per call, the SPA will
 * fan out one call per series. Reconcile at integration.
 */
export interface EvaluationsResponse {
  run_id: string
  series: MetricSeries[]
}

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/predictions?model_id=&k= — top-k prediction_ranks       */
/* -------------------------------------------------------------------------- */

export interface PredictionRow {
  rank: number
  entity_id: string
  /** Optional display attribute, e.g. facility_type. */
  attribute?: string | null
  score: number
}

export interface PredictionsData {
  empty?: false
  model_id: number
  rows: PredictionRow[]
}

export type PredictionsResponse = PredictionsData | EmptyState

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/source-pins — current_source_pins                       */
/* -------------------------------------------------------------------------- */

export interface SourcePinRow {
  source: string
  pin: string
  rows: number | null
  drift: string // "stable" | "pinned" | ...
}

export interface SourcePinsResponse {
  run_id: string
  pins: SourcePinRow[]
}

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/selected-model — selected_model (§3.5)                  */
/* -------------------------------------------------------------------------- */

export interface SelectedModelResponse {
  run_id: string
  metric: string
  state: SelectionState
  /** Audition pick's model_group + its concrete latest-split model_id. */
  audition_group: number | null
  audition_model_id: number | null
  audition_label: string | null
  /** Leaderboard #1 model + its group. */
  leaderboard_model: number | null
  leaderboard_group: number | null
  leaderboard_label: string | null
  /** Metric leaderboard #1 ranks by, for the divergence message. */
  leaderboard_metric: string | null
  diverges: boolean
}

/* -------------------------------------------------------------------------- */
/* GET /api/models/{model_id} — feature_importances + per-split evals          */
/* -------------------------------------------------------------------------- */

export interface FeatureImportanceRow {
  feature: string
  importance: number
}

export interface SplitEvalRow {
  as_of_date: string
  /** Metric -> value; UI reads p@10% + auc + "n test". null while building. */
  metrics: Record<string, number | null>
  n_test: number | null
  building?: boolean
}

export interface ModelDetailResponse {
  model_id: number
  model_group_id: number
  label: string
  feature_importances: FeatureImportanceRow[]
  per_split: SplitEvalRow[]
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
