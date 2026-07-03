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
  /* built vs reused artifact counts (present on /experiments/{hash} runs). A run with
   * n_built === 0 (and n_reused > 0) is a "replay" — it ran but rebuilt nothing. */
  n_built?: number
  n_reused?: number
}

/* -------------------------------------------------------------------------- */
/* GET /api/runs/{id}/summary — run_summary + per-split profiles (§3.1/§3.2)   */
/* -------------------------------------------------------------------------- */

/**
 * runs.plan denominators + temporal config (jsonb). Shape is open — routes.py
 * passes runs.plan through verbatim. The SPA reads `n_splits` (planned splits)
 * and the temporal window/frequency strings when present.
 */
/** The run's ATTEMPT at the problem (ADR-0022): the per-run feature/grid/imputation config
 *  recorded on runs.plan. The experiment row itself only carries the problem. */
export interface ExperimentAttempt {
  feature_config?: unknown
  grid_config?: Record<string, unknown> | null
  imputation_config?: unknown
}

export interface TemporalPlan {
  n_splits?: number | null
  label_timespan?: string | null
  history?: string | null
  /** per-run feature/grid/imputation config (ADR-0022). */
  attempt?: ExperimentAttempt | null
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
  /** Temporal split (train_end date) for matrix/model nodes; null for others. */
  split: string | null
  /** 'train' | 'test' for matrix nodes; null otherwise. */
  matrix_kind: string | null
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

/** A source fingerprint (capture_fingerprint): an advisory {row_count, max_knowledge_date}
 *  jsonb object — NOT a string. Rendered via fmtFingerprint, never as a raw React child. */
export type Fingerprint =
  | { row_count?: number; max_knowledge_date?: string | null }
  | Record<string, unknown>
  | string
  | null

/** triage.run_source_pins — a pin frozen at this run's plan time. */
export interface RunSourcePin {
  run_id: string
  source_name: string
  version_label: string | null
  fingerprint: Fingerprint
}

/** triage.current_source_pins — the registry's current head per source. */
export interface CurrentSourcePin {
  source_name: string
  version_label: string | null
  registered_at: string | null
  fingerprint: Fingerprint
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
  /** 'gini' (trees) | 'coef'/'abs_coef' (linear). null for pre-0009 rows. */
  importance_kind?: string | null
  /** signed coefficient β (linear models only). */
  signed_value?: number | null
  /** odds-ratio exp(β) (linear models only). */
  odds_ratio?: number | null
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

/* ========================================================================== */
/* EXPERIMENT-SCOPED contract (rework — dashboard-api-contract.md §"Experiment-*) */
/* ========================================================================== */

/* -------------------------------------------------------------------------- */
/* GET /api/experiments — experiment_summary rows                             */
/* GET /api/experiments/{hash} — summary + config + runs                      */
/* -------------------------------------------------------------------------- */

/** triage.experiment_summary — one row per experiment (the rail/list shape). */
export interface ExperimentSummary {
  experiment_hash: string
  name: string | null
  description: string | null
  author: string | null
  problem_type: ProblemType | null
  created_at: string | null
  n_runs: number
  last_started_at: string | null
  last_status: RunStatus | null
  /** runs.plan rollup of the latest run (n_splits, label_timespan, …). */
  last_plan: TemporalPlan | null
  /* actuals (migration 0006) — the *built* shape, independent of runs.plan. */
  n_model_groups: number
  n_models: number
  n_splits: number
  n_features: number | null
  base_rate: number | null
  cohort_size: number | null
}

/** Models the experiment's runs built vs reused from another run's cache (the Q1 mechanism). */
export interface ModelReuse {
  built: number
  reused: number
}

/**
 * Cross-experiment artifact sharing. Of the artifacts this experiment's runs touched,
 * how many were actually built by a DIFFERENT experiment's run. n_foreign === n_total
 * means this experiment rebuilt nothing — it duplicates whatever it borrowed from
 * (`shared_with_name`, the dominant lender, e.g. the same config under a stale hash).
 */
export interface ArtifactSharing {
  n_total: number
  n_foreign: number
  n_shared: number
  shared_with_hash: string | null
  shared_with_name: string | null
}

/** GET /experiments/{hash} — the experiment header detail. */
export interface ExperimentDetailResponse {
  summary: ExperimentSummary
  /** experiments.config jsonb (open shape; name/description stripped from hash). */
  config: ExperimentConfig | null
  /** Sibling runs for this experiment, newest first. */
  runs: RunListItem[]
  model_reuse: ModelReuse
  artifact_sharing: ArtifactSharing
}

/* -------------------------------------------------------------------------- */
/* GET /api/experiments/{hash}/audition — ranking + curves + 8 strategies      */
/* -------------------------------------------------------------------------- */

/** experiment_audition ranking row (one per model_group, experiment-scoped). */
export interface ExpAuditionRankRow {
  experiment_hash: string
  metric: string
  parameter: string
  model_group_id: number
  n_splits_evaluated: number
  avg_value: number | null
  stddev_value: number | null
  avg_distance_from_best: number
  max_regret: number
}

/** experiment_audition_distances — a model_group's per-split distance row. */
export interface ExpAuditionCurveRow {
  experiment_hash: string
  model_group_id: number
  metric: string
  parameter: string
  as_of_date: string
  raw_value: number | null
  best_value: number | null
  dist_from_best_case: number | null
}

/** One (rule → picked model_group) entry; all 8 rules are returned. */
export interface AuditionStrategy {
  rule: string
  model_group_id: number | null
}

export interface ExpAuditionData {
  empty?: false
  metric: string
  parameter: string
  rule: string
  ranking: ExpAuditionRankRow[]
  curves: ExpAuditionCurveRow[]
  /** model_group_id of the active rule's pick (audition_pick_exp), or null. */
  pick: number | null
  k: number // evaluated splits
  n: number | null // planned splits (any run's plan->n_splits), may be null
  provisional: boolean
  /** The 8 selection rules, each with its picked model_group_id. */
  strategies: AuditionStrategy[]
}

export type ExpAuditionResponse = ExpAuditionData | EmptyState

/* -------------------------------------------------------------------------- */
/* GET /api/experiments/{hash}/bias?model_id= — bias_metrics (long format)      */
/* -------------------------------------------------------------------------- */

/** GET /experiments/{hash}/bias — bare array of long-format rows, OR empty. */
export type ExpBiasResponse = BiasMetricRow[] | EmptyState

/* -------------------------------------------------------------------------- */
/* GET /api/experiments/{hash}/leaderboard — triage.leaderboard rows           */
/* -------------------------------------------------------------------------- */

/** triage.leaderboard row scoped to an experiment_hash (same columns as 0004). */
export interface ExpLeaderboardRow {
  experiment_hash: string
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

export type ExpLeaderboardResponse = ExpLeaderboardRow[]

/* -------------------------------------------------------------------------- */
/* GET /api/experiments/{hash}/evaluations?metric= — test-split evals          */
/* -------------------------------------------------------------------------- */

/** Experiment-scoped evaluation row (test split, via evaluations ⋈ models ⋈ runs). */
export interface ExpEvaluationRow {
  experiment_hash: string
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

export type ExpEvaluationsResponse = ExpEvaluationRow[]

/* -------------------------------------------------------------------------- */
/* GET /api/experiments/{hash}/model-groups — model_group_summary rows          */
/* -------------------------------------------------------------------------- */

/** triage.model_group_summary — one row per (experiment_hash, model_group_id). */
export interface ModelGroupSummaryRow {
  experiment_hash: string
  model_group_id: number
  model_group_hash: string | null
  model_type: string | null
  hyperparameters: Record<string, unknown> | null
  feature_list: string[] | null
  n_models: number
  first_train_end: string | null
  last_train_end: string | null
}

export type ModelGroupsResponse = ModelGroupSummaryRow[]

/* -------------------------------------------------------------------------- */
/* GET /api/experiments/{hash}/selected-model — selected_model_exp              */
/* -------------------------------------------------------------------------- */

export interface ExpSelectedModelData {
  empty?: false
  metric: string
  parameter: string
  rule: string
  audition_group: number | null
  audition_model: number | null
  leaderboard_group: number | null
  leaderboard_model: number | null
  diverges: boolean
}

export type ExpSelectedModelResponse = ExpSelectedModelData | EmptyState

/* -------------------------------------------------------------------------- */
/* GET /api/model-groups/{id} — group detail (summary + models + over-time)     */
/* -------------------------------------------------------------------------- */

export interface ModelGroupModelRow {
  model_id: number
  train_end_time: string | null
  run_id: string
  training_label_timespan: string | null
  /** test/evaluation period (min test as_of_date) — the period the model is scored on. */
  test_as_of: string | null
}

export interface ModelGroupDetailResponse {
  summary: ModelGroupSummaryRow
  models: ModelGroupModelRow[]
  /** evals for this group's models (long format, for metric-over-time). */
  metric_over_time: ExpEvaluationRow[]
  /** same shape as the experiment evaluations table (test split). */
  per_split: ExpEvaluationRow[]
}

/* -------------------------------------------------------------------------- */
/* GET /api/models/{id} — feature importances + evals (+ model_group_id)        */
/* -------------------------------------------------------------------------- */

export interface ModelCardResponse {
  model_id: number
  model_group_id: number | null
  feature_importances: FeatureImportanceRow[]
  evaluations: ModelEvaluationRow[]
}

/* -------------------------------------------------------------------------- */
/* GET /api/models/{id}/curve — the Rayid threshold curve                       */
/* -------------------------------------------------------------------------- */

/** One point on triage.model_threshold_curve (cumulative TP/FP by rank). */
export interface ThresholdCurvePoint {
  k: number
  pct: number
  prec: number | null
  rec: number | null
  tp: number
  fp: number
  fn: number
  tn: number
}

export type ModelCurveResponse = ThresholdCurvePoint[]

/* -------------------------------------------------------------------------- */
/* GET /api/models/{id}/histogram — score histogram                             */
/* -------------------------------------------------------------------------- */

/** One score-histogram bin (width_bucket over prediction_ranks.score). */
export interface HistogramBin {
  bin: number
  lo: number
  hi: number
  n: number
  n_pos: number
}

export type ModelHistogramResponse = HistogramBin[]

/* -------------------------------------------------------------------------- */
/* GET /api/models/{id}/predictions?k= — top-k predictions ⋈ labels             */
/* -------------------------------------------------------------------------- */

/** triage.prediction_ranks ⋈ labels — one scored entity with its outcome. */
export interface PredictionRow {
  entity_id: string | number
  as_of_date: string
  score: number
  rank_abs: number
  rank_pct: number | null
  outcome: number | null
}

/** A page of ranked predictions + the full count (migration 0006: limit/offset paging). */
export interface PredictionsPage {
  rows: PredictionRow[]
  total: number
}

export type ModelPredictionsResponse = PredictionsPage | EmptyState

/* -------------------------------------------------------------------------- */
/* GET /api/metrics — the metric catalog (for SPA selectors)                    */
/* -------------------------------------------------------------------------- */

export interface MetricCatalogRow {
  metric: string
  parameter: string
  higher_is_better: boolean
}

export type MetricsResponse = MetricCatalogRow[]

/* -------------------------------------------------------------------------- */
/* GET /api/ontology — per-project data profile (sources + volumes)             */
/* -------------------------------------------------------------------------- */

export interface OntologySourceRow {
  source_name: string
  relation: string
  knowledge_date_column: string | null
  description: string | null
  /** 'entity' | 'event' | null (migration 0006): which source is the entity grain. */
  role: string | null
  /** the categorical column volume is broken out by, e.g. facility_type (migration 0009). */
  type_column?: string | null
}

/** One bucket of a source's volume-over-time series. */
export interface VolumePoint {
  period: string
  n: number
}

/** triage.source_profile — total rows + knowledge-date range + distinct entities. */
export interface SourceProfile {
  total_rows: number
  first_date: string | null
  last_date: string | null
  n_distinct_entities: number | null
}

/** One (period, type, count) point for the per-type volume series. */
export interface TypeVolumePoint {
  period: string | null
  type_value: string | null
  n: number
}

export interface OntologyResponse {
  sources: OntologySourceRow[]
  /** source_name → its volume-over-time series. */
  volumes: Record<string, VolumePoint[]>
  /** source_name → volume broken out by the source's type_column (entities/events). */
  volumes_by_type?: Record<string, TypeVolumePoint[]>
  /** source_name → its profile stats (migration 0006). */
  profile: Record<string, SourceProfile>
}

/* -------------------------------------------------------------------------- */
/* GET /api/entities/{id} — full entity profile (attributes + histories)        */
/* -------------------------------------------------------------------------- */

/** triage.entity_score_history — one (model_group, as_of_date) trajectory point. */
export interface EntityScorePoint {
  model_group_id: number
  model_id: number
  experiment_hash: string
  as_of_date: string
  score: number
  rank_abs: number
  rank_pct: number | null
  model_type: string | null
  hyperparameters: Record<string, unknown> | null
  train_end_time: string | null
}

/** triage.entity_label_history — outcome over time for the entity. */
export interface EntityLabelPoint {
  as_of_date: string
  label_timespan: string
  outcome: number | null
}

/** GET /entities/{id} — the entity-grain attributes + label + score histories. */
export interface EntityProfileResponse {
  entity_id: number
  /** The entity-grain source row as jsonb (role='entity'); null when none resolves. */
  attributes: Record<string, unknown> | null
  label_history: EntityLabelPoint[]
  score_history: EntityScorePoint[]
}

/* -------------------------------------------------------------------------- */
/* GET /api/status — triage status (pins, engine versions, GC, run counts)      */
/* -------------------------------------------------------------------------- */

/** One GC/artifact-status rollup row (artifacts grouped by kind × status). */
export interface ArtifactStatusRow {
  kind: string
  status: string
  n: number
}

/** Live DB health from the pg catalogs (migration-free; proves reachability + headroom). */
export interface DbHealth {
  server_version: string
  db_size: string
  connections: number
  max_connections: number
  max_parallel_workers: number
  uptime: string
  reachable: boolean
}

/** How/where the latest run executed (local in-process vs cloud AWS Batch). */
export interface ExecutionInfo {
  profile: string | null
  purpose: string | null
  status: RunStatus | null
  started_at: string | null
  finished_at: string | null
  duration_s: number | null
  triage_version: string | null
  git_hash: string | null
  batch_job_id: string | null
}

/** Compute telemetry stamped into runs.plan (null for runs predating the telemetry). */
export interface ComputeInfo {
  cpu_count: number | null
  profile: string | null
}

/** A source's frozen run-pin vs the registry's current head (drift = they differ). */
export interface SourceDriftRow {
  source_name: string
  run_version: string | null
  head_version: string | null
  drift: boolean
}

/** A distinct artifact storage directory (parent of the matrices/.parquet, models/.joblib). */
export interface ArtifactPathRow {
  kind: string
  dir: string
  n: number
}

/** One experiment row for the status overview. */
export interface StatusExperimentRow {
  experiment_hash: string
  name: string | null
  n_runs: number
  n_models: number | null
  last_status: RunStatus | null
  last_started_at: string | null
}

export interface StatusResponse {
  sources: CurrentSourcePin[]
  engine_versions: Record<string, string> | null
  gc: ArtifactStatusRow[]
  /** run counts by status (e.g. {completed: 3, failed: 1}). */
  runs: Record<string, number>
  /** experiments overview (not just runs). */
  experiments?: StatusExperimentRow[]
  /** where artifacts land on disk/S3 (parent dirs of matrices/models). */
  artifact_paths?: ArtifactPathRow[]
  db: DbHealth
  execution: ExecutionInfo
  compute: ComputeInfo | null
  source_drift: SourceDriftRow[]
}

/* -------------------------------------------------------------------------- */
/* GET /api/derivation — project-wide derivation graph (shared nodes)           */
/* -------------------------------------------------------------------------- */

export interface ProjectDerivationNode {
  artifact_id: string
  kind: string
  status: string
  built_by_run: string | null
  n_experiments: number
  n_runs: number
  /** Temporal split (train_end date) for matrix/model nodes; null for others. */
  split: string | null
  /** 'train' | 'test' for matrix nodes; null otherwise. */
  matrix_kind: string | null
}

export interface ProjectDerivationEdge {
  parent_id: string
  artifact_id: string
}

export interface ProjectDerivationResponse {
  nodes: ProjectDerivationNode[]
  edges: ProjectDerivationEdge[]
}

/* -------------------------- write surface (ADR-0024) ---------------------- */
// Shapes mirror src/triage/dashboard/write_routes.py + triage/registry.py. The write routes
// return raw registry rows; UUID/timestamp fields arrive as strings over JSON.

export type MemberRole = 'owner' | 'contributor' | 'viewer'
export type Profile = 'local' | 'cloud'

/** GET /api/me — the resolved caller identity (the auth seam). `auth_mode` tells the SPA
 *  which backend resolved it: logout only exists under 'oidc' (ADR-0028). */
export interface Principal {
  user_id: string
  email: string
  display_name: string | null
  is_admin: boolean
  auth_mode?: 'trusted' | 'oidc'
}

/** A registry.projects row. `database_ready` is present on POST /api/projects responses only:
 *  the webapp creates the registry ROW; database provisioning is `triage project create` (CLI). */
export interface Project {
  project_id: string
  slug: string
  display_name: string
  database_name: string
  status: 'active' | 'archived' | 'dropped'
  created_at: string
  archived_at: string | null
  database_ready?: boolean
}

/** A registry.project_members row joined to the user. */
export interface Member {
  project_id: string
  user_id: string
  role: MemberRole
  added_at: string
  email: string
  display_name: string | null
}

/** A registry.submissions row (the append-only audit trail), joined to project + user. */
export interface Submission {
  submission_id: string
  project_id: string
  project_slug: string
  submitted_by: string | null
  submitted_by_email: string | null
  experiment_hash: string | null
  profile: Profile
  batch_job_id: string | null
  submitted_at: string
}

/* ------------------------------ monitoring (ADR-0027) ---------------------- */

/** A triage.monitoring_volume row — the scoring heartbeat. */
export interface MonitoringVolumeRow {
  model_group_id: number
  model_id: number
  split_kind: string
  scored_on: string
  n_predictions: number
  n_entities: number
  first_as_of_date: string
  last_as_of_date: string
}

/** triage.monitoring_score_drift(...) — PSI + KS between two scored_at windows. */
export interface MonitoringDrift {
  psi: number | null
  ks: number | null
  n_reference: number
  n_window: number
}

/** A triage.monitoring_outcome_tracking row — realized metrics over time. */
export interface MonitoringOutcomeRow {
  model_group_id: number
  model_id: number
  purpose: string | null
  split_kind: string
  as_of_date: string
  metric: string
  parameter: string
  value: number | null
  num_labeled: number | null
  computed_at: string
}

/** POST /api/validate-config — the core's dry-run verdict (nothing persisted, nothing run). */
export interface ValidateConfigResult {
  valid: boolean
  experiment_hash: string | null
  problem_type: string | null
  n_splits: number | null
  n_models: number | null
  n_feature_groups: number | null
  errors: { path: string; message: string }[]
  warnings: string[]
}

/** GET /api/example-configs — a committed example greenfield config, for the picker. */
export interface ExampleConfig {
  name: string
  dataset: string
  filename: string
  description: string
  content: string
}

/** POST /api/submissions response: the audit row + a run/Batch summary. */
export interface SubmissionResult {
  submission: Submission
  result: {
    experiment_hash?: string
    problem_type?: string
    num_runs?: number
    num_models?: number
    num_predictions?: number
    num_evaluations?: number
    batch_job_id?: string | null
    config_uri?: string | null
    status?: string
  }
}
