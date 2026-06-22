/*
 * Dev fixture — sample data matching the §5 API contract, modeled on the real
 * DirtyDuck run 81a68920 used in the mockup (4 splits, deep-grid). Lets
 * `npm run dev` render the whole dashboard without a backend. The API client
 * (src/api/client.ts) serves this when import.meta.env.VITE_USE_FIXTURE is set
 * (the default in dev); real integration against /api happens later.
 */
import type {
  AuditionResponse,
  BiasResponse,
  DerivationResponse,
  EvaluationsResponse,
  LeaderboardResponse,
  ModelDetailResponse,
  PredictionsResponse,
  ProgressResponse,
  RunListItem,
  SelectedModelResponse,
  SourcePinsResponse,
  SummaryResponse,
} from '../api/types'

const RUN = '81a68920-0000-4000-8000-000000000001'
const SPLITS = ['2015-07-01', '2016-01-01', '2016-07-01', '2017-01-01']

export const FIXTURE_RUN_ID = RUN

export const runs: RunListItem[] = [
  {
    run_id: RUN,
    status: 'building',
    started_at: '2026-06-21T13:27:00Z',
    label: 'deep-grid',
    headline_metric: null,
    progress_line: 'matrices 3/8 · eval 3/8',
  },
  {
    run_id: '7e4d0000-0000-4000-8000-000000000004',
    status: 'completed',
    started_at: '2026-06-20T09:10:00Z',
    label: 'one-hot cats · v0.4',
    headline_metric: 'auc 0.574',
  },
  {
    run_id: 'a4ee0000-0000-4000-8000-000000000003',
    status: 'completed',
    started_at: '2026-06-19T16:42:00Z',
    label: 'ordinal',
    headline_metric: 'auc 0.575',
  },
  {
    run_id: '5a090000-0000-4000-8000-000000000002',
    status: 'failed',
    started_at: '2026-06-18T11:05:00Z',
    label: 'labels query error',
    headline_metric: null,
  },
]

export const summary: SummaryResponse = {
  summary: {
    run_id: RUN,
    status: 'building',
    profile: 'local (in-process)',
    started_at: '2026-06-21T13:27:00Z',
    finished_at: null,
    duration: '2m',
    problem_type: 'classification',
    experiment_hash: '81a68920c3f1',
    cohort_name: 'active_facilities',
    label_name: 'failed_inspections · 6mo',
    temporal: { n_splits: 4, label_timespan: '6mo', history: '5y hist' },
    n_features: 147,
    n_feature_groups: 6,
    n_model_groups: 3,
    n_models: 8,
    estimator_types: ['DT', 'RF', 'ET', 'GB'],
    random_seed: 42,
    triage_version: '0.1.0',
    git_hash: 'b15567f4',
    batch_job_id: null,
    engine_versions: { featurizer: 'v0.4.1 (3b60057f)', sklearn: '1.7.x' },
  },
  cohort_profile: [
    { as_of_date: SPLITS[0], n_entities: 1640 },
    { as_of_date: SPLITS[1], n_entities: 1810 },
    { as_of_date: SPLITS[2], n_entities: 1995 },
    { as_of_date: SPLITS[3], n_entities: 2140 },
  ],
  base_rate: [
    { as_of_date: SPLITS[0], label_timespan: '6mo', base_rate: 0.221, n_labeled: 1640 },
    { as_of_date: SPLITS[1], label_timespan: '6mo', base_rate: 0.236, n_labeled: 1810 },
    { as_of_date: SPLITS[2], label_timespan: '6mo', base_rate: 0.241, n_labeled: 1995 },
    { as_of_date: SPLITS[3], label_timespan: '6mo', base_rate: 0.238, n_labeled: 2140 },
  ],
}

export const progress: ProgressResponse = {
  run_id: RUN,
  stages: [
    { kind: 'cohort', status: 'done', n: 1, m: 1, detail: '2,140 rows' },
    { kind: 'labels', status: 'done', n: 1, m: 1, detail: '23.8% pos' },
    { kind: 'matrices', status: 'current', n: 3, m: 8 },
    { kind: 'models', status: 'current', n: 3, m: 8 },
    { kind: 'evaluate', status: 'todo', n: 3, m: 8 },
  ],
}

export const derivation: DerivationResponse = {
  nodes: [
    { artifact_id: 'src-db', kind: 'source', status: 'built', label: 'source:db' },
    { artifact_id: 'src-fz', kind: 'source', status: 'built', label: 'source:fz v0.4.1' },
    { artifact_id: 'src-tc', kind: 'source', status: 'built', label: 'timechop' },
    { artifact_id: 'cohort', kind: 'cohort', status: 'cachehit', label: 'cohort ⟲ cache hit', cache_hit: true },
    { artifact_id: 'labels', kind: 'labels', status: 'cachehit', label: 'labels ⟲ cache hit', cache_hit: true },
    { artifact_id: 'fg', kind: 'feature_group', status: 'built', label: 'feature_group (147)' },
    { artifact_id: 'mx-123', kind: 'matrix', status: 'built', label: 'matrix s1..s3 ✓' },
    { artifact_id: 'mx-4', kind: 'matrix', status: 'building', label: 'matrix s4 ◐' },
    { artifact_id: 'models', kind: 'model', status: 'todo', label: 'models ⋯' },
  ],
  edges: [
    { parent_id: 'src-db', artifact_id: 'cohort' },
    { parent_id: 'src-db', artifact_id: 'labels' },
    { parent_id: 'src-tc', artifact_id: 'cohort' },
    { parent_id: 'src-fz', artifact_id: 'fg' },
    { parent_id: 'cohort', artifact_id: 'mx-123' },
    { parent_id: 'labels', artifact_id: 'mx-123' },
    { parent_id: 'fg', artifact_id: 'mx-123' },
    { parent_id: 'fg', artifact_id: 'mx-4' },
    { parent_id: 'mx-123', artifact_id: 'models' },
  ],
}

export const audition: AuditionResponse = {
  run_id: RUN,
  metric: 'precision@10_pct',
  strategy: 'best_average_value',
  provisional: true,
  k: 3,
  n: 4,
  ranking: [
    { model_group_id: 4, label: 'RF·d3', avg_distance_from_best: 0.018, max_regret: 0.03, n_splits_evaluated: 3, is_pick: true },
    { model_group_id: 7, label: 'DT·d5', avg_distance_from_best: 0.041, max_regret: 0.07, n_splits_evaluated: 3, is_pick: false },
    { model_group_id: 2, label: 'DT·d3', avg_distance_from_best: 0.063, max_regret: 0.09, n_splits_evaluated: 3, is_pick: false },
  ],
  curves: [
    {
      model_group_id: 4,
      label: 'RF·d3',
      points: [
        { as_of_date: SPLITS[0], distance_from_best: 0.024 },
        { as_of_date: SPLITS[1], distance_from_best: 0.028 },
        { as_of_date: SPLITS[2], distance_from_best: 0.02 },
      ],
    },
    {
      model_group_id: 7,
      label: 'DT·d5',
      points: [
        { as_of_date: SPLITS[0], distance_from_best: 0.05 },
        { as_of_date: SPLITS[1], distance_from_best: 0.038 },
        { as_of_date: SPLITS[2], distance_from_best: 0.064 },
      ],
    },
    {
      model_group_id: 2,
      label: 'DT·d3',
      points: [
        { as_of_date: SPLITS[0], distance_from_best: 0.088 },
        { as_of_date: SPLITS[1], distance_from_best: 0.076 },
        { as_of_date: SPLITS[2], distance_from_best: 0.082 },
      ],
    },
  ],
}

export const bias: BiasResponse = {
  model_id: 27,
  rows: [
    { group_attribute: 'facility_type', group_value: 'restaurant', tpr: 0.41, fpr: 0.18, ppv: 0.34, n: 1210 },
    { group_attribute: 'facility_type', group_value: 'grocery store', tpr: 0.33, fpr: 0.15, ppv: 0.3, n: 486 },
    { group_attribute: 'facility_type', group_value: 'school', tpr: 0.29, fpr: 0.11, ppv: 0.27, n: 221, disparity: 0.12 },
  ],
}

export const leaderboard: LeaderboardResponse = {
  run_id: RUN,
  rows: [
    {
      model_id: 27,
      model_group_id: 4,
      label: 'RF·d3·n10',
      metrics: { 'precision@10_pct': 0.343, 'precision@100_abs': 0.41, auc: 0.574, ap: 0.31 },
      is_audition_pick: true,
    },
    {
      model_id: 31,
      model_group_id: 7,
      label: 'DT·d5',
      metrics: { 'precision@10_pct': 0.331, 'precision@100_abs': 0.39, auc: 0.571, ap: 0.33 },
      rank_metric: 'ap',
    },
    {
      model_id: 19,
      model_group_id: 2,
      label: 'DT·d3',
      metrics: { 'precision@10_pct': 0.318, 'precision@100_abs': 0.37, auc: 0.567, ap: 0.29 },
    },
  ],
}

export const evaluations: EvaluationsResponse = {
  run_id: RUN,
  series: [
    {
      metric: 'precision@10_pct',
      points: [
        { as_of_date: SPLITS[0], value: 0.31 },
        { as_of_date: SPLITS[1], value: 0.33 },
        { as_of_date: SPLITS[2], value: 0.35 },
        { as_of_date: SPLITS[3], value: null },
      ],
    },
    {
      metric: 'auc',
      points: [
        { as_of_date: SPLITS[0], value: 0.561 },
        { as_of_date: SPLITS[1], value: 0.569 },
        { as_of_date: SPLITS[2], value: 0.578 },
        { as_of_date: SPLITS[3], value: null },
      ],
    },
  ],
}

export const predictions: PredictionsResponse = {
  model_id: 27,
  rows: [
    { rank: 1, entity_id: 'e·9921', attribute: 'restaurant', score: 0.94 },
    { rank: 2, entity_id: 'e·4410', attribute: 'restaurant', score: 0.91 },
    { rank: 3, entity_id: 'e·7732', attribute: 'grocery store', score: 0.88 },
  ],
}

export const sourcePins: SourcePinsResponse = {
  run_id: RUN,
  pins: [
    { source: 'clean.inspections', pin: 'b15567f4', rows: 221134, drift: 'stable' },
    { source: 'featurizer', pin: 'v0.4.1', rows: null, drift: 'pinned' },
    { source: 'sklearn', pin: '1.7.x', rows: null, drift: 'pinned' },
  ],
}

export const selectedModel: SelectedModelResponse = {
  run_id: RUN,
  metric: 'precision@10_pct',
  state: 'provisional',
  audition_group: 4,
  audition_model_id: 27,
  audition_label: 'RF·d3',
  leaderboard_model: 31,
  leaderboard_group: 7,
  leaderboard_label: 'DT·d5',
  leaderboard_metric: 'ap',
  diverges: true,
}

const MODEL_DETAILS: Record<number, ModelDetailResponse> = {
  27: {
    model_id: 27,
    model_group_id: 4,
    label: 'RF·d3·n10',
    feature_importances: [
      { feature: 'inspections.count_180d', importance: 0.284 },
      { feature: 'inspections.fail_rate_1y', importance: 0.197 },
      { feature: 'inspections.risk_max_180d', importance: 0.121 },
      { feature: 'facilities.facility_type=school', importance: 0.018 },
      { feature: 'facilities.zip_code=60647', importance: 0.011 },
    ],
    per_split: [
      { as_of_date: SPLITS[0], metrics: { 'precision@10_pct': 0.31, auc: 0.561 }, n_test: 38402 },
      { as_of_date: SPLITS[1], metrics: { 'precision@10_pct': 0.33, auc: 0.569 }, n_test: 40118 },
      { as_of_date: SPLITS[2], metrics: { 'precision@10_pct': 0.35, auc: 0.578 }, n_test: 41290 },
      { as_of_date: SPLITS[3], metrics: { 'precision@10_pct': null, auc: null }, n_test: null, building: true },
    ],
  },
  31: {
    model_id: 31,
    model_group_id: 7,
    label: 'DT·d5',
    feature_importances: [
      { feature: 'inspections.fail_rate_1y', importance: 0.312 },
      { feature: 'inspections.count_180d', importance: 0.244 },
      { feature: 'inspections.risk_max_180d', importance: 0.103 },
      { feature: 'facilities.facility_type=restaurant', importance: 0.014 },
    ],
    per_split: [
      { as_of_date: SPLITS[0], metrics: { 'precision@10_pct': 0.3, auc: 0.558 }, n_test: 38402 },
      { as_of_date: SPLITS[1], metrics: { 'precision@10_pct': 0.32, auc: 0.566 }, n_test: 40118 },
      { as_of_date: SPLITS[2], metrics: { 'precision@10_pct': 0.34, auc: 0.571 }, n_test: 41290 },
      { as_of_date: SPLITS[3], metrics: { 'precision@10_pct': null, auc: null }, n_test: null, building: true },
    ],
  },
  19: {
    model_id: 19,
    model_group_id: 2,
    label: 'DT·d3',
    feature_importances: [
      { feature: 'inspections.count_180d', importance: 0.301 },
      { feature: 'inspections.fail_rate_1y', importance: 0.176 },
      { feature: 'inspections.risk_max_180d', importance: 0.092 },
    ],
    per_split: [
      { as_of_date: SPLITS[0], metrics: { 'precision@10_pct': 0.29, auc: 0.55 }, n_test: 38402 },
      { as_of_date: SPLITS[1], metrics: { 'precision@10_pct': 0.31, auc: 0.56 }, n_test: 40118 },
      { as_of_date: SPLITS[2], metrics: { 'precision@10_pct': 0.32, auc: 0.567 }, n_test: 41290 },
      { as_of_date: SPLITS[3], metrics: { 'precision@10_pct': null, auc: null }, n_test: null, building: true },
    ],
  },
}

export function modelDetail(modelId: number): ModelDetailResponse {
  return MODEL_DETAILS[modelId] ?? MODEL_DETAILS[27]
}

/** Top predictions keyed by model_id (selected-model drives this panel). */
export function predictionsFor(modelId: number): PredictionsResponse {
  if ('empty' in predictions && predictions.empty) return predictions
  return { ...predictions, model_id: modelId }
}

/** Bias rows keyed by model_id. */
export function biasFor(modelId: number): BiasResponse {
  if ('rows' in bias) return { ...bias, model_id: modelId }
  return bias
}
