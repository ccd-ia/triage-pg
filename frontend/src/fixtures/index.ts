/*
 * Dev fixture — sample data matching the REAL API shapes in
 * src/triage/dashboard/routes.py (raw view rows: long-format metrics, bare
 * leaderboard/evaluations/predictions arrays, per-(kind,status) progress). Lets
 * `npm run dev` render the whole dashboard without a backend. The API client
 * (src/api/client.ts) serves this when import.meta.env.VITE_USE_FIXTURE is set
 * (the default in dev); real integration against /api happens later.
 *
 * Modeled on the DirtyDuck run 81a68920 (4 splits, deep-grid).
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
    experiment_hash: '81a68920c3f1',
    profile: 'local (in-process)',
    purpose: 'deep-grid',
    status: 'building',
    started_at: '2026-06-21T13:27:00Z',
    finished_at: null,
    triage_version: '0.1.0',
    git_hash: 'b15567f4',
    batch_job_id: null,
  },
  {
    run_id: '7e4d0000-0000-4000-8000-000000000004',
    experiment_hash: '7e4d0000aa11',
    profile: 'local (in-process)',
    purpose: 'one-hot cats · v0.4',
    status: 'completed',
    started_at: '2026-06-20T09:10:00Z',
    finished_at: '2026-06-20T09:42:00Z',
    triage_version: '0.1.0',
    git_hash: '31c63d3a',
    batch_job_id: null,
  },
  {
    run_id: 'a4ee0000-0000-4000-8000-000000000003',
    experiment_hash: 'a4ee0000bb22',
    profile: 'local (in-process)',
    purpose: 'ordinal',
    status: 'completed',
    started_at: '2026-06-19T16:42:00Z',
    finished_at: '2026-06-19T17:08:00Z',
    triage_version: '0.1.0',
    git_hash: '033113f2',
    batch_job_id: null,
  },
  {
    run_id: '5a090000-0000-4000-8000-000000000002',
    experiment_hash: '5a090000cc33',
    profile: 'local (in-process)',
    purpose: 'labels query error',
    status: 'failed',
    started_at: '2026-06-18T11:05:00Z',
    finished_at: '2026-06-18T11:06:00Z',
    triage_version: '0.1.0',
    git_hash: 'b15567f4',
    batch_job_id: null,
  },
]

export const summary: SummaryResponse = {
  summary: {
    run_id: RUN,
    status: 'building',
    profile: 'local (in-process)',
    purpose: 'deep-grid',
    started_at: '2026-06-21T13:27:00Z',
    finished_at: null,
    duration: '2m',
    problem_type: 'classification',
    experiment_hash: '81a68920c3f1',
    experiment_config: {
      cohort_name: 'active_facilities',
      label_name: 'failed_inspections · 6mo',
    },
    plan: {
      n_splits: 4,
      label_timespan: '6mo',
      history: '5y hist',
      n_models: 8,
      n_matrices: 8,
      n_evaluations: 8,
    },
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
    { run_id: RUN, as_of_date: SPLITS[0], n_entities: 1640 },
    { run_id: RUN, as_of_date: SPLITS[1], n_entities: 1810 },
    { run_id: RUN, as_of_date: SPLITS[2], n_entities: 1995 },
    { run_id: RUN, as_of_date: SPLITS[3], n_entities: 2140 },
  ],
  label_base_rate: [
    { run_id: RUN, as_of_date: SPLITS[0], label_timespan: '6mo', base_rate: 0.221, n_labeled: 1640 },
    { run_id: RUN, as_of_date: SPLITS[1], label_timespan: '6mo', base_rate: 0.236, n_labeled: 1810 },
    { run_id: RUN, as_of_date: SPLITS[2], label_timespan: '6mo', base_rate: 0.241, n_labeled: 1995 },
    { run_id: RUN, as_of_date: SPLITS[3], label_timespan: '6mo', base_rate: 0.238, n_labeled: 2140 },
  ],
}

export const progress: ProgressResponse = {
  progress: [
    { run_id: RUN, kind: 'cohort', status: 'built', n: 4 },
    { run_id: RUN, kind: 'labels', status: 'built', n: 4 },
    { run_id: RUN, kind: 'matrices', status: 'built', n: 3 },
    { run_id: RUN, kind: 'matrices', status: 'building', n: 1 },
    { run_id: RUN, kind: 'models', status: 'built', n: 3 },
    { run_id: RUN, kind: 'models', status: 'building', n: 5 },
    { run_id: RUN, kind: 'evaluate', status: 'built', n: 3 },
  ],
  plan: {
    n_splits: 4,
    n_matrices: 8,
    n_models: 8,
    n_evaluations: 8,
    n_as_of_dates: 4,
  },
}

export const derivation: DerivationResponse = {
  nodes: [
    { artifact_id: 'src-db', kind: 'source', status: 'built', built_by_run: RUN, cache_hit: false },
    { artifact_id: 'src-fz', kind: 'source', status: 'built', built_by_run: RUN, cache_hit: false },
    { artifact_id: 'src-tc', kind: 'source', status: 'built', built_by_run: RUN, cache_hit: false },
    { artifact_id: 'cohort', kind: 'cohort', status: 'built', built_by_run: 'prev-run', cache_hit: true },
    { artifact_id: 'labels', kind: 'labels', status: 'built', built_by_run: 'prev-run', cache_hit: true },
    { artifact_id: 'fg', kind: 'feature_group', status: 'built', built_by_run: RUN, cache_hit: false },
    { artifact_id: 'mx-123', kind: 'matrix', status: 'built', built_by_run: RUN, cache_hit: false },
    { artifact_id: 'mx-4', kind: 'matrix', status: 'building', built_by_run: RUN, cache_hit: false },
    { artifact_id: 'models', kind: 'model', status: 'building', built_by_run: RUN, cache_hit: false },
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
  metric: 'precision@',
  parameter: '10_pct',
  rule: 'best_average_value',
  pick: 4,
  k: 3,
  n: 4,
  provisional: true,
  ranking: [
    {
      run_id: RUN,
      metric: 'precision@',
      parameter: '10_pct',
      model_group_id: 4,
      n_splits_evaluated: 3,
      avg_value: 0.343,
      stddev_value: 0.02,
      avg_distance_from_best: 0.018,
      max_regret: 0.03,
    },
    {
      run_id: RUN,
      metric: 'precision@',
      parameter: '10_pct',
      model_group_id: 7,
      n_splits_evaluated: 3,
      avg_value: 0.331,
      stddev_value: 0.018,
      avg_distance_from_best: 0.041,
      max_regret: 0.07,
    },
    {
      run_id: RUN,
      metric: 'precision@',
      parameter: '10_pct',
      model_group_id: 2,
      n_splits_evaluated: 3,
      avg_value: 0.318,
      stddev_value: 0.022,
      avg_distance_from_best: 0.063,
      max_regret: 0.09,
    },
  ],
  curves: [
    { run_id: RUN, model_group_id: 4, metric: 'precision@', parameter: '10_pct', as_of_date: SPLITS[0], raw_value: 0.31, best_value: 0.334, dist_from_best_case: 0.024 },
    { run_id: RUN, model_group_id: 4, metric: 'precision@', parameter: '10_pct', as_of_date: SPLITS[1], raw_value: 0.33, best_value: 0.358, dist_from_best_case: 0.028 },
    { run_id: RUN, model_group_id: 4, metric: 'precision@', parameter: '10_pct', as_of_date: SPLITS[2], raw_value: 0.35, best_value: 0.37, dist_from_best_case: 0.02 },
    { run_id: RUN, model_group_id: 7, metric: 'precision@', parameter: '10_pct', as_of_date: SPLITS[0], raw_value: 0.284, best_value: 0.334, dist_from_best_case: 0.05 },
    { run_id: RUN, model_group_id: 7, metric: 'precision@', parameter: '10_pct', as_of_date: SPLITS[1], raw_value: 0.32, best_value: 0.358, dist_from_best_case: 0.038 },
    { run_id: RUN, model_group_id: 7, metric: 'precision@', parameter: '10_pct', as_of_date: SPLITS[2], raw_value: 0.306, best_value: 0.37, dist_from_best_case: 0.064 },
    { run_id: RUN, model_group_id: 2, metric: 'precision@', parameter: '10_pct', as_of_date: SPLITS[0], raw_value: 0.246, best_value: 0.334, dist_from_best_case: 0.088 },
    { run_id: RUN, model_group_id: 2, metric: 'precision@', parameter: '10_pct', as_of_date: SPLITS[1], raw_value: 0.282, best_value: 0.358, dist_from_best_case: 0.076 },
    { run_id: RUN, model_group_id: 2, metric: 'precision@', parameter: '10_pct', as_of_date: SPLITS[2], raw_value: 0.288, best_value: 0.37, dist_from_best_case: 0.082 },
  ],
}

export const bias: BiasResponse = [
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'restaurant', metric: 'tpr', value: 0.41, ref_group_value: 'restaurant', disparity: null },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'restaurant', metric: 'fpr', value: 0.18, ref_group_value: 'restaurant', disparity: null },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'restaurant', metric: 'ppv', value: 0.34, ref_group_value: 'restaurant', disparity: null },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'grocery store', metric: 'tpr', value: 0.33, ref_group_value: 'restaurant', disparity: 0.08 },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'grocery store', metric: 'fpr', value: 0.15, ref_group_value: 'restaurant', disparity: null },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'grocery store', metric: 'ppv', value: 0.3, ref_group_value: 'restaurant', disparity: null },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'school', metric: 'tpr', value: 0.29, ref_group_value: 'restaurant', disparity: 0.12 },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'school', metric: 'fpr', value: 0.11, ref_group_value: 'restaurant', disparity: null },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'school', metric: 'ppv', value: 0.27, ref_group_value: 'restaurant', disparity: null },
]

/**
 * Bare leaderboard rows (long format: one per model/metric/parameter/as_of_date).
 * model_groups 4 (RF), 7 (DT·d5), 2 (DT·d3) → models 27, 31, 19. The SPA reshapes
 * + ranks these client-side.
 */
export const leaderboard: LeaderboardResponse = [
  // model 27 (group 4) — latest split SPLITS[2]
  { run_id: RUN, model_group_id: 4, model_type: 'sklearn.ensemble.RandomForestClassifier', split_kind: 'test', metric: 'precision@', parameter: '10_pct', as_of_date: SPLITS[2], value: 0.343, value_expected: 0.335, value_std: 0.02, model_id: 27, train_end_time: SPLITS[2] },
  { run_id: RUN, model_group_id: 4, model_type: 'sklearn.ensemble.RandomForestClassifier', split_kind: 'test', metric: 'precision@', parameter: '100_abs', as_of_date: SPLITS[2], value: 0.41, value_expected: 0.4, value_std: 0.03, model_id: 27, train_end_time: SPLITS[2] },
  { run_id: RUN, model_group_id: 4, model_type: 'sklearn.ensemble.RandomForestClassifier', split_kind: 'test', metric: 'auc_roc', parameter: '', as_of_date: SPLITS[2], value: 0.574, value_expected: 0.57, value_std: 0.01, model_id: 27, train_end_time: SPLITS[2] },
  // model 31 (group 7)
  { run_id: RUN, model_group_id: 7, model_type: 'sklearn.tree.DecisionTreeClassifier', split_kind: 'test', metric: 'precision@', parameter: '10_pct', as_of_date: SPLITS[2], value: 0.331, value_expected: 0.325, value_std: 0.018, model_id: 31, train_end_time: SPLITS[2] },
  { run_id: RUN, model_group_id: 7, model_type: 'sklearn.tree.DecisionTreeClassifier', split_kind: 'test', metric: 'precision@', parameter: '100_abs', as_of_date: SPLITS[2], value: 0.39, value_expected: 0.38, value_std: 0.025, model_id: 31, train_end_time: SPLITS[2] },
  { run_id: RUN, model_group_id: 7, model_type: 'sklearn.tree.DecisionTreeClassifier', split_kind: 'test', metric: 'auc_roc', parameter: '', as_of_date: SPLITS[2], value: 0.571, value_expected: 0.568, value_std: 0.012, model_id: 31, train_end_time: SPLITS[2] },
  // model 19 (group 2)
  { run_id: RUN, model_group_id: 2, model_type: 'sklearn.tree.DecisionTreeClassifier', split_kind: 'test', metric: 'precision@', parameter: '10_pct', as_of_date: SPLITS[2], value: 0.318, value_expected: 0.312, value_std: 0.022, model_id: 19, train_end_time: SPLITS[2] },
  { run_id: RUN, model_group_id: 2, model_type: 'sklearn.tree.DecisionTreeClassifier', split_kind: 'test', metric: 'precision@', parameter: '100_abs', as_of_date: SPLITS[2], value: 0.37, value_expected: 0.36, value_std: 0.028, model_id: 19, train_end_time: SPLITS[2] },
  { run_id: RUN, model_group_id: 2, model_type: 'sklearn.tree.DecisionTreeClassifier', split_kind: 'test', metric: 'auc_roc', parameter: '', as_of_date: SPLITS[2], value: 0.567, value_expected: 0.563, value_std: 0.013, model_id: 19, train_end_time: SPLITS[2] },
]

/**
 * Flat evaluation rows (one per model/metric/as_of_date, test split). The SPA
 * groups by metric and averages across models for the overlay series.
 */
export const evaluations: EvaluationsResponse = (() => {
  const rows: EvaluationsResponse = []
  const prec: Record<number, (number | null)[]> = {
    27: [0.31, 0.33, 0.35, null],
    31: [0.3, 0.32, 0.34, null],
    19: [0.29, 0.31, 0.32, null],
  }
  const auc: Record<number, (number | null)[]> = {
    27: [0.561, 0.569, 0.578, null],
    31: [0.558, 0.566, 0.571, null],
    19: [0.55, 0.56, 0.567, null],
  }
  const group: Record<number, number> = { 27: 4, 31: 7, 19: 2 }
  for (const modelId of [27, 31, 19]) {
    SPLITS.forEach((d, i) => {
      const p = prec[modelId][i]
      if (p != null) {
        rows.push({
          run_id: RUN,
          model_id: modelId,
          model_group_id: group[modelId],
          split_kind: 'test',
          as_of_date: d,
          metric: 'precision@',
          parameter: '10_pct',
          value: p,
          num_labeled: 1995,
          num_positive: 480,
        })
      }
      const a = auc[modelId][i]
      if (a != null) {
        rows.push({
          run_id: RUN,
          model_id: modelId,
          model_group_id: group[modelId],
          split_kind: 'test',
          as_of_date: d,
          metric: 'auc_roc',
          parameter: '',
          value: a,
          num_labeled: 1995,
          num_positive: 480,
        })
      }
    })
  }
  return rows
})()

/** Bare prediction_ranks rows for model 27, top 3. */
export const predictions: PredictionsResponse = [
  { model_id: 27, entity_id: '9921', as_of_date: SPLITS[2], split_kind: 'test', score: 0.94, scored_at: '2026-06-21T13:40:00Z', rank_abs: 1, rank_pct: 0.0005 },
  { model_id: 27, entity_id: '4410', as_of_date: SPLITS[2], split_kind: 'test', score: 0.91, scored_at: '2026-06-21T13:40:00Z', rank_abs: 2, rank_pct: 0.001 },
  { model_id: 27, entity_id: '7732', as_of_date: SPLITS[2], split_kind: 'test', score: 0.88, scored_at: '2026-06-21T13:40:00Z', rank_abs: 3, rank_pct: 0.0015 },
]

export const sourcePins: SourcePinsResponse = {
  run_pins: [
    { run_id: RUN, source_name: 'clean.inspections', version_label: 'b15567f4', fingerprint: 'sha256:aa11' },
    { run_id: RUN, source_name: 'featurizer', version_label: 'v0.4.1', fingerprint: '3b60057f' },
    { run_id: RUN, source_name: 'sklearn', version_label: '1.7.x', fingerprint: null },
  ],
  current: [
    { source_name: 'clean.inspections', version_label: 'b15567f4', registered_at: '2026-06-20T00:00:00Z', fingerprint: 'sha256:aa11' },
    { source_name: 'featurizer', version_label: 'v0.4.2', registered_at: '2026-06-21T00:00:00Z', fingerprint: '9c12ab00' },
    { source_name: 'sklearn', version_label: '1.7.x', registered_at: '2026-06-01T00:00:00Z', fingerprint: null },
  ],
}

export const selectedModel: SelectedModelResponse = {
  metric: 'precision@',
  parameter: '10_pct',
  rule: 'best_average_value',
  audition_group: 4,
  audition_model: 27,
  leaderboard_group: 7,
  leaderboard_model: 31,
  diverges: true,
}

/** Per-model detail keyed by model_id (raw routes.py shape). */
const MODEL_DETAILS: Record<number, ModelDetailResponse> = {
  27: {
    model_id: 27,
    feature_importances: [
      { model_id: 27, feature: 'inspections.count_180d', feature_importance: 0.284, rank_abs: 1, rank_pct: 0.99 },
      { model_id: 27, feature: 'inspections.fail_rate_1y', feature_importance: 0.197, rank_abs: 2, rank_pct: 0.98 },
      { model_id: 27, feature: 'inspections.risk_max_180d', feature_importance: 0.121, rank_abs: 3, rank_pct: 0.97 },
      { model_id: 27, feature: 'facilities.facility_type=school', feature_importance: 0.018, rank_abs: 4, rank_pct: 0.5 },
      { model_id: 27, feature: 'facilities.zip_code=60647', feature_importance: 0.011, rank_abs: 5, rank_pct: 0.4 },
    ],
    evaluations: modelEvals(27, [0.31, 0.33, 0.35, null], [0.561, 0.569, 0.578, null]),
  },
  31: {
    model_id: 31,
    feature_importances: [
      { model_id: 31, feature: 'inspections.fail_rate_1y', feature_importance: 0.312, rank_abs: 1, rank_pct: 0.99 },
      { model_id: 31, feature: 'inspections.count_180d', feature_importance: 0.244, rank_abs: 2, rank_pct: 0.98 },
      { model_id: 31, feature: 'inspections.risk_max_180d', feature_importance: 0.103, rank_abs: 3, rank_pct: 0.97 },
      { model_id: 31, feature: 'facilities.facility_type=restaurant', feature_importance: 0.014, rank_abs: 4, rank_pct: 0.45 },
    ],
    evaluations: modelEvals(31, [0.3, 0.32, 0.34, null], [0.558, 0.566, 0.571, null]),
  },
  19: {
    model_id: 19,
    feature_importances: [
      { model_id: 19, feature: 'inspections.count_180d', feature_importance: 0.301, rank_abs: 1, rank_pct: 0.99 },
      { model_id: 19, feature: 'inspections.fail_rate_1y', feature_importance: 0.176, rank_abs: 2, rank_pct: 0.98 },
      { model_id: 19, feature: 'inspections.risk_max_180d', feature_importance: 0.092, rank_abs: 3, rank_pct: 0.97 },
    ],
    evaluations: modelEvals(19, [0.29, 0.31, 0.32, null], [0.55, 0.56, 0.567, null]),
  },
}

/** Build long-format model evaluation rows for the per-split table. */
function modelEvals(
  modelId: number,
  prec: (number | null)[],
  auc: (number | null)[],
): ModelDetailResponse['evaluations'] {
  const rows: ModelDetailResponse['evaluations'] = []
  SPLITS.forEach((d, i) => {
    rows.push({
      model_id: modelId,
      split_kind: 'test',
      as_of_date: d,
      metric: 'precision@',
      parameter: '10_pct',
      value: prec[i],
      value_expected: prec[i],
      value_std: prec[i] == null ? null : 0.02,
      num_labeled: prec[i] == null ? null : 38402 + i * 1000,
      num_positive: prec[i] == null ? null : 9000,
    })
    rows.push({
      model_id: modelId,
      split_kind: 'test',
      as_of_date: d,
      metric: 'auc_roc',
      parameter: '',
      value: auc[i],
      value_expected: auc[i],
      value_std: auc[i] == null ? null : 0.01,
      num_labeled: auc[i] == null ? null : 38402 + i * 1000,
      num_positive: auc[i] == null ? null : 9000,
    })
  })
  return rows
}

export function modelDetail(modelId: number): ModelDetailResponse {
  return MODEL_DETAILS[modelId] ?? MODEL_DETAILS[27]
}

/** Top predictions keyed by model_id (selected-model drives this panel). */
export function predictionsFor(modelId: number): PredictionsResponse {
  if (!Array.isArray(predictions)) return predictions
  return predictions.map((p) => ({ ...p, model_id: modelId }))
}

/** Bias rows keyed by model_id. */
export function biasFor(modelId: number): BiasResponse {
  if (!Array.isArray(bias)) return bias
  return bias.map((r) => ({ ...r, model_id: modelId }))
}
