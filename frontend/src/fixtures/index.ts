/*
 * Dev fixtures for the reworked dashboard — sample data matching the REAL API
 * shapes in dashboard-api-contract.md. Lets `npm run dev` render the whole SPA
 * standalone (the API client serves these when import.meta.env.VITE_USE_FIXTURE
 * is set — the default in dev; production builds default to live).
 *
 * Two confirmed bugs are fixed here:
 *  - Bug A: each run gets DISTINCT per-run data keyed by run_id (summaries,
 *    progress, derivation), so clicking between runs actually changes the view.
 *  - Bug B: feature-importance names use the REALISTIC RAW featurizer strings
 *    (e.g. COUNT(inspections.result|interval=P180D)) so the transforms.ts
 *    prettifier is exercised end-to-end.
 *
 * Analysis fixtures are experiment-scoped (the rework's locked decision): runs
 * roll up to an experiment_hash, and audition/bias/leaderboard/evaluations/
 * model-groups aggregate all of an experiment's runs.
 */
import type {
  ArtifactStatusRow,
  EntityProfileResponse,
  EntityScorePoint,
  PredictionRow,
  CohortProfilePoint,
  DerivationResponse,
  ExpAuditionResponse,
  ExpAuditionCurveRow,
  ExpAuditionRankRow,
  ExpBiasResponse,
  ExpEvaluationRow,
  ExpEvaluationsResponse,
  ExpLeaderboardResponse,
  ExpLeaderboardRow,
  ExpSelectedModelResponse,
  ExperimentDetailResponse,
  ExperimentSummary,
  HistogramBin,
  MetricsResponse,
  ModelCardResponse,
  ModelCurveResponse,
  ModelGroupDetailResponse,
  ModelGroupSummaryRow,
  ModelGroupsResponse,
  ModelHistogramResponse,
  ModelPredictionsResponse,
  OntologyResponse,
  ProgressResponse,
  ProjectDerivationResponse,
  RunListItem,
  SourcePinsResponse,
  StatusResponse,
  SummaryResponse,
  ThresholdCurvePoint,
} from '../api/types'

/* ========================================================================== */
/* Identity — two experiments, four runs                                       */
/* ========================================================================== */

/** failed_inspections experiment (auto-name "Quartz-Curie-7"). */
const EXP_FAILED = '81a68920c3f1'
/** active_facilities experiment (auto-name "Cobalt-Turing"). */
const EXP_ACTIVE = '7e4d0000aa11'

const SPLITS = ['2015-07-01', '2016-01-01', '2016-07-01', '2017-01-01']

/** run_id → experiment_hash. The two completed `active` runs share an experiment
 * to exercise the cache-share / experiment-scope story (Bug Q1). */
const RUN_EXP: Record<string, string> = {
  '81a68920-0000-4000-8000-000000000001': EXP_FAILED, // building, deep-grid
  '5a090000-0000-4000-8000-000000000002': EXP_FAILED, // failed (labels error)
  '7e4d0000-0000-4000-8000-000000000004': EXP_ACTIVE, // completed
  'a4ee0000-0000-4000-8000-000000000003': EXP_ACTIVE, // completed (re-run, cache-hits)
}

export const FIXTURE_RUN_ID = '81a68920-0000-4000-8000-000000000001'

export const runs: RunListItem[] = [
  {
    run_id: '81a68920-0000-4000-8000-000000000001',
    experiment_hash: EXP_FAILED,
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
    experiment_hash: EXP_ACTIVE,
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
    experiment_hash: EXP_ACTIVE,
    profile: 'local (in-process)',
    purpose: 'ordinal · re-run (cache hits)',
    status: 'completed',
    started_at: '2026-06-19T16:42:00Z',
    finished_at: '2026-06-19T17:08:00Z',
    triage_version: '0.1.0',
    git_hash: '033113f2',
    batch_job_id: null,
  },
  {
    run_id: '5a090000-0000-4000-8000-000000000002',
    experiment_hash: EXP_FAILED,
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

/* ========================================================================== */
/* Bug A — DISTINCT per-run summaries / progress / derivation                  */
/* ========================================================================== */

/** Per-run knobs so each run renders different cohort/label/progress data. */
const RUN_PROFILE: Record<
  string,
  { cohort: number[]; baseRate: number[]; nLabeled: number[]; matricesBuilt: number; modelsBuilt: number; status: RunListItem['status'] }
> = {
  '81a68920-0000-4000-8000-000000000001': {
    cohort: [1640, 1810, 1995, 2140],
    baseRate: [0.221, 0.236, 0.241, 0.238],
    nLabeled: [1502, 1660, 1830, 1902],
    matricesBuilt: 3,
    modelsBuilt: 3,
    status: 'building',
  },
  '7e4d0000-0000-4000-8000-000000000004': {
    cohort: [980, 1075, 1190, 1260],
    baseRate: [0.142, 0.151, 0.149, 0.155],
    nLabeled: [902, 1001, 1120, 1190],
    matricesBuilt: 4,
    modelsBuilt: 12,
    status: 'completed',
  },
  'a4ee0000-0000-4000-8000-000000000003': {
    cohort: [980, 1075, 1190, 1260],
    baseRate: [0.142, 0.151, 0.149, 0.155],
    nLabeled: [902, 1001, 1120, 1190],
    matricesBuilt: 4,
    modelsBuilt: 12,
    status: 'completed',
  },
  '5a090000-0000-4000-8000-000000000002': {
    cohort: [1600, 0, 0, 0],
    baseRate: [0.22, 0, 0, 0],
    nLabeled: [0, 0, 0, 0],
    matricesBuilt: 0,
    modelsBuilt: 0,
    status: 'failed',
  },
}

function cohortProfile(runId: string): CohortProfilePoint[] {
  const p = RUN_PROFILE[runId] ?? RUN_PROFILE[FIXTURE_RUN_ID]
  return SPLITS.map((d, i) => ({ run_id: runId, as_of_date: d, n_entities: p.cohort[i] }))
}

function labelBaseRate(runId: string): SummaryResponse['label_base_rate'] {
  const p = RUN_PROFILE[runId] ?? RUN_PROFILE[FIXTURE_RUN_ID]
  return SPLITS.map((d, i) => ({
    run_id: runId,
    as_of_date: d,
    label_timespan: '6mo',
    base_rate: p.baseRate[i] || null,
    n_labeled: p.nLabeled[i],
  }))
}

export function summaryFor(runId: string): SummaryResponse {
  const run = runs.find((r) => r.run_id === runId) ?? runs[0]
  const p = RUN_PROFILE[runId] ?? RUN_PROFILE[FIXTURE_RUN_ID]
  const exp = RUN_EXP[runId] ?? EXP_FAILED
  const isFailed = run.status === 'failed'
  return {
    summary: {
      run_id: runId,
      status: run.status,
      profile: run.profile,
      purpose: run.purpose,
      started_at: run.started_at,
      finished_at: run.finished_at,
      duration: run.finished_at ? '32m' : run.status === 'building' ? '2m' : null,
      problem_type: 'classification',
      experiment_hash: exp,
      experiment_config: {
        cohort_name: exp === EXP_FAILED ? 'failed_inspections' : 'active_facilities',
        label_name: exp === EXP_FAILED ? 'failed_inspections · 6mo' : 'active_facilities · 6mo',
      },
      plan: {
        n_splits: 4,
        label_timespan: '6mo',
        history: '5y hist',
        n_models: p.modelsBuilt || 12,
        n_matrices: 8,
        n_evaluations: 12,
        engine_versions: { featurizer: 'v0.4.1 (3b60057f)', sklearn: '1.7.x' },
      },
      n_features: 147,
      n_feature_groups: 6,
      n_model_groups: 12,
      n_models: p.modelsBuilt,
      estimator_types: ['DT', 'RF', 'ET', 'GB'],
      random_seed: 42,
      triage_version: '0.1.0',
      git_hash: run.git_hash,
      batch_job_id: null,
      engine_versions: { featurizer: 'v0.4.1 (3b60057f)', sklearn: '1.7.x' },
    },
    cohort_profile: isFailed ? cohortProfile(runId).slice(0, 1) : cohortProfile(runId),
    label_base_rate: isFailed ? [] : labelBaseRate(runId),
  }
}

export function progressFor(runId: string): ProgressResponse {
  const p = RUN_PROFILE[runId] ?? RUN_PROFILE[FIXTURE_RUN_ID]
  const plan = { n_splits: 4, n_matrices: 8, n_models: 12, n_evaluations: 12, n_as_of_dates: 4 }
  if (p.status === 'failed') {
    return {
      progress: [
        { run_id: runId, kind: 'cohort', status: 'built', n: 1 },
        { run_id: runId, kind: 'labels', status: 'failed', n: 1 },
      ],
      plan,
    }
  }
  if (p.status === 'building') {
    return {
      progress: [
        { run_id: runId, kind: 'cohort', status: 'built', n: 4 },
        { run_id: runId, kind: 'labels', status: 'built', n: 4 },
        { run_id: runId, kind: 'matrices', status: 'built', n: p.matricesBuilt },
        { run_id: runId, kind: 'matrices', status: 'building', n: 8 - p.matricesBuilt },
        { run_id: runId, kind: 'models', status: 'built', n: p.modelsBuilt },
        { run_id: runId, kind: 'models', status: 'building', n: 12 - p.modelsBuilt },
        { run_id: runId, kind: 'evaluate', status: 'built', n: Math.max(0, p.modelsBuilt - 1) },
      ],
      plan,
    }
  }
  return {
    progress: [
      { run_id: runId, kind: 'cohort', status: 'built', n: 4 },
      { run_id: runId, kind: 'labels', status: 'built', n: 4 },
      { run_id: runId, kind: 'matrices', status: 'built', n: 8 },
      { run_id: runId, kind: 'models', status: 'built', n: 12 },
      { run_id: runId, kind: 'evaluate', status: 'built', n: 12 },
    ],
    plan,
  }
}

// Inject the split/matrix_kind fields (migration: derivation nodes carry their temporal
// split) so fixture nodes match the type without hand-editing every literal.
function addSplitFields<T extends { kind: string }>(
  n: T,
  i: number,
): T & { split: string | null; matrix_kind: string | null } {
  const isSplitKind = n.kind === 'matrix' || n.kind === 'model'
  return {
    ...n,
    split: isSplitKind ? SPLITS[i % SPLITS.length] : null,
    matrix_kind: n.kind === 'matrix' ? (i % 2 === 0 ? 'train' : 'test') : null,
  }
}

export function derivationFor(runId: string): DerivationResponse {
  const p = RUN_PROFILE[runId] ?? RUN_PROFILE[FIXTURE_RUN_ID]
  // The re-run (a4ee…003) gets cache_hit nodes; the original builds fresh.
  const isRerun = runId === 'a4ee0000-0000-4000-8000-000000000003'
  const building = p.status === 'building'
  return {
    nodes: [
      { artifact_id: 'src-db', kind: 'source', status: 'built', built_by_run: runId, cache_hit: false },
      { artifact_id: 'src-fz', kind: 'source', status: 'built', built_by_run: runId, cache_hit: false },
      { artifact_id: 'src-tc', kind: 'source', status: 'built', built_by_run: runId, cache_hit: false },
      { artifact_id: 'cohort', kind: 'cohort', status: 'built', built_by_run: isRerun ? 'prev-run' : runId, cache_hit: isRerun },
      { artifact_id: 'labels', kind: 'labels', status: 'built', built_by_run: isRerun ? 'prev-run' : runId, cache_hit: isRerun },
      { artifact_id: 'fg', kind: 'feature_group', status: 'built', built_by_run: isRerun ? 'prev-run' : runId, cache_hit: isRerun },
      { artifact_id: 'mx-123', kind: 'matrix', status: 'built', built_by_run: runId, cache_hit: false },
      { artifact_id: 'mx-4', kind: 'matrix', status: building ? 'building' : 'built', built_by_run: runId, cache_hit: false },
      { artifact_id: 'models', kind: 'model', status: building ? 'building' : 'built', built_by_run: runId, cache_hit: false },
    ].map(addSplitFields),
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
}

export const sourcePins: SourcePinsResponse = {
  run_pins: [
    { run_id: FIXTURE_RUN_ID, source_name: 'clean.inspections', version_label: 'b15567f4', fingerprint: 'sha256:aa11' },
    { run_id: FIXTURE_RUN_ID, source_name: 'featurizer', version_label: 'v0.4.1', fingerprint: '3b60057f' },
    { run_id: FIXTURE_RUN_ID, source_name: 'sklearn', version_label: '1.7.x', fingerprint: null },
  ],
  current: [
    { source_name: 'clean.inspections', version_label: 'b15567f4', registered_at: '2026-06-20T00:00:00Z', fingerprint: 'sha256:aa11' },
    { source_name: 'featurizer', version_label: 'v0.4.2', registered_at: '2026-06-21T00:00:00Z', fingerprint: '9c12ab00' },
    { source_name: 'sklearn', version_label: '1.7.x', registered_at: '2026-06-01T00:00:00Z', fingerprint: null },
  ],
}

/* ========================================================================== */
/* Experiments                                                                 */
/* ========================================================================== */

export const experiments: ExperimentSummary[] = [
  {
    experiment_hash: EXP_FAILED,
    name: 'Quartz-Curie-7',
    description: 'predict facilities likely to fail their next inspection',
    author: 'adolfo',
    problem_type: 'classification',
    created_at: '2026-06-18T11:05:00Z',
    n_runs: 2,
    last_started_at: '2026-06-21T13:27:00Z',
    last_status: 'building',
    last_plan: { n_splits: 4, label_timespan: '6mo' },
    n_model_groups: 12,
    n_models: 48,
    n_splits: 4,
    n_features: 120,
    base_rate: 0.277,
    cohort_size: 14261,
  },
  {
    experiment_hash: EXP_ACTIVE,
    name: 'Cobalt-Turing',
    description: 'active-facilities cohort, ordinal categorical encoding',
    author: 'adolfo',
    problem_type: 'classification',
    created_at: '2026-06-19T16:42:00Z',
    n_runs: 2,
    last_started_at: '2026-06-20T09:10:00Z',
    last_status: 'completed',
    last_plan: { n_splits: 4, label_timespan: '6mo' },
    n_model_groups: 12,
    n_models: 48,
    n_splits: 4,
    n_features: 147,
    base_rate: 0.277,
    cohort_size: 14261,
  },
]

export function experimentFor(hash: string): ExperimentDetailResponse {
  const summary = experiments.find((e) => e.experiment_hash === hash) ?? experiments[0]
  const expRuns = runs.filter((r) => r.experiment_hash === summary.experiment_hash)
  return {
    summary,
    config: {
      cohort_name: summary.experiment_hash === EXP_FAILED ? 'failed_inspections' : 'active_facilities',
      label_name: 'failed_inspections · 6mo',
      problem_type: 'classification',
      temporal_config: { test_durations: '6month', label_timespans: ['6month'] },
      grid_config: { 'sklearn.tree.DecisionTreeClassifier': { max_depth: [3, 5] } },
    },
    runs: expRuns,
    model_reuse: { built: 12, reused: 80 },
    artifact_sharing: { n_total: 23, n_foreign: 0, n_shared: 0, shared_with_hash: null, shared_with_name: null },
  }
}

/* ========================================================================== */
/* Model groups (12 groups; only a handful carry through to evals/audition)    */
/* ========================================================================== */

const FEATURE_LIST = [
  'COUNT(inspections.result|interval=P180D)',
  'AVG(inspections.risk_level|interval=P1Y)',
  'MAX(inspections.violations|interval=P180D)',
  'facilities.facility_type=restaurant',
]

/** model_group_id → (algorithm, depth/label, models in group). */
const GROUPS: { id: number; type: string; label: string }[] = [
  { id: 4, type: 'sklearn.ensemble.RandomForestClassifier', label: 'RF · depth 3' },
  { id: 9, type: 'sklearn.ensemble.GradientBoostingClassifier', label: 'GB · lr .1' },
  { id: 7, type: 'sklearn.tree.DecisionTreeClassifier', label: 'DT · depth 5' },
  { id: 11, type: 'sklearn.ensemble.ExtraTreesClassifier', label: 'ET · depth 3' },
  { id: 2, type: 'sklearn.tree.DecisionTreeClassifier', label: 'DT · depth 3' },
  { id: 5, type: 'sklearn.linear_model.LogisticRegression', label: 'LR · C 1.0' },
]

export function modelGroupsFor(hash: string): ModelGroupsResponse {
  return GROUPS.map(
    (g): ModelGroupSummaryRow => ({
      experiment_hash: hash,
      model_group_id: g.id,
      model_group_hash: `mgh${g.id}`,
      model_type: g.type,
      hyperparameters:
        g.type.includes('Tree') || g.type.includes('Forest') || g.type.includes('ExtraTrees')
          ? { max_depth: g.label.includes('5') ? 5 : 3 }
          : g.type.includes('Gradient')
            ? { learning_rate: 0.1, n_estimators: 250 }
            : { C: 1.0 },
      feature_list: FEATURE_LIST,
      n_models: 4,
      first_train_end: SPLITS[0],
      last_train_end: SPLITS[3],
    }),
  )
}

/* ========================================================================== */
/* Evaluations / leaderboard (experiment-scoped, per model)                    */
/* ========================================================================== */

/** model_id → its model_group_id. */
const MODEL_GROUP: Record<number, number> = { 27: 4, 35: 9, 31: 7, 41: 11, 19: 2, 23: 5 }
/** Per-group precision@10pct over the 4 splits (group 4 RF is the best). */
const GROUP_PREC: Record<number, number[]> = {
  4: [0.555, 0.481, 0.602, 0.591],
  9: [0.512, 0.539, 0.566, 0.572],
  7: [0.578, 0.553, 0.521, 0.498],
  11: [0.488, 0.501, 0.524, 0.531],
  2: [0.47, 0.486, 0.503, 0.511],
  5: [0.441, 0.452, 0.478, 0.486],
}
const GROUP_AUC: Record<number, number[]> = {
  4: [0.572, 0.561, 0.598, 0.594],
  9: [0.55, 0.566, 0.571, 0.578],
  7: [0.568, 0.557, 0.541, 0.522],
  11: [0.531, 0.539, 0.548, 0.551],
  2: [0.51, 0.522, 0.531, 0.538],
  5: [0.49, 0.5, 0.512, 0.519],
}

function evalRows(hash: string): ExpEvaluationRow[] {
  const rows: ExpEvaluationRow[] = []
  for (const [mid, gid] of Object.entries(MODEL_GROUP)) {
    const modelId = Number(mid)
    SPLITS.forEach((d, i) => {
      rows.push({
        experiment_hash: hash,
        model_id: modelId,
        model_group_id: gid,
        split_kind: 'test',
        as_of_date: d,
        metric: 'precision@',
        parameter: '10_pct',
        value: GROUP_PREC[gid][i],
        num_labeled: 1995,
        num_positive: 480,
      })
      rows.push({
        experiment_hash: hash,
        model_id: modelId,
        model_group_id: gid,
        split_kind: 'test',
        as_of_date: d,
        metric: 'auc_roc',
        parameter: '',
        value: GROUP_AUC[gid][i],
        num_labeled: 1995,
        num_positive: 480,
      })
    })
  }
  return rows
}

export function expEvaluationsFor(hash: string): ExpEvaluationsResponse {
  return evalRows(hash)
}

export function expLeaderboardFor(hash: string): ExpLeaderboardResponse {
  const rows: ExpLeaderboardRow[] = []
  for (const [mid, gid] of Object.entries(MODEL_GROUP)) {
    const modelId = Number(mid)
    const g = GROUPS.find((x) => x.id === gid)!
    const i = 2 // latest mature split
    rows.push({
      experiment_hash: hash,
      model_group_id: gid,
      model_type: g.type,
      split_kind: 'test',
      metric: 'precision@',
      parameter: '10_pct',
      as_of_date: SPLITS[i],
      value: GROUP_PREC[gid][i],
      value_expected: GROUP_PREC[gid][i] - 0.005,
      value_std: 0.02,
      model_id: modelId,
      train_end_time: SPLITS[i],
    })
    rows.push({
      experiment_hash: hash,
      model_group_id: gid,
      model_type: g.type,
      split_kind: 'test',
      metric: 'auc_roc',
      parameter: '',
      as_of_date: SPLITS[i],
      value: GROUP_AUC[gid][i],
      value_expected: GROUP_AUC[gid][i] - 0.003,
      value_std: 0.01,
      model_id: modelId,
      train_end_time: SPLITS[i],
    })
  }
  return rows
}

/* ========================================================================== */
/* Audition — 8 strategies, experiment-scoped                                  */
/* ========================================================================== */

const RULES = [
  'best_current_value',
  'best_average_value',
  'lowest_metric_variance',
  'most_frequent_best_dist',
  'best_avg_var_penalized',
  'best_avg_recency_weight',
  'best_average_two_metrics',
  'random_model_group',
]

export function expAuditionFor(
  hash: string,
  _metric?: string,
  _parameter?: string,
  rule?: string,
): ExpAuditionResponse {
  const activeRule = rule ?? 'best_average_value'
  const groups = [4, 9, 7, 11, 2, 5]
  // Distance-from-best per group/split (lower better); group 4 leads on average.
  const ranking: ExpAuditionRankRow[] = groups.map((gid) => {
    const prec = GROUP_PREC[gid]
    const avg = prec.slice(0, 3).reduce((s, v) => s + v, 0) / 3
    const best = 0.602
    return {
      experiment_hash: hash,
      metric: 'precision@',
      parameter: '10_pct',
      model_group_id: gid,
      n_splits_evaluated: 3,
      avg_value: avg,
      stddev_value: 0.02,
      avg_distance_from_best: Number((best - avg).toFixed(3)),
      max_regret: Number((best - Math.min(...prec.slice(0, 3))).toFixed(3)),
    }
  })
  ranking.sort((a, b) => a.avg_distance_from_best - b.avg_distance_from_best)
  const curves: ExpAuditionCurveRow[] = []
  for (const gid of groups) {
    SPLITS.slice(0, 3).forEach((d, i) => {
      const bestAtSplit = Math.max(...groups.map((g) => GROUP_PREC[g][i]))
      curves.push({
        experiment_hash: hash,
        model_group_id: gid,
        metric: 'precision@',
        parameter: '10_pct',
        as_of_date: d,
        raw_value: GROUP_PREC[gid][i],
        best_value: bestAtSplit,
        dist_from_best_case: Number((bestAtSplit - GROUP_PREC[gid][i]).toFixed(3)),
      })
    })
  }
  // Each strategy's pick — most agree on RF (group 4); variance/recency pick GB.
  const strategies = RULES.map((r) => ({
    rule: r,
    model_group_id:
      r === 'lowest_metric_variance' || r === 'best_avg_var_penalized'
        ? 9
        : r === 'random_model_group'
          ? 2
          : 4,
  }))
  const pick = strategies.find((s) => s.rule === activeRule)?.model_group_id ?? 4
  return {
    metric: 'precision@',
    parameter: '10_pct',
    rule: activeRule,
    ranking,
    curves,
    pick,
    k: 3,
    n: 4,
    provisional: true,
    strategies,
  }
}

export function expSelectedModelFor(
  hash: string,
  _metric?: string,
  _parameter?: string,
  rule?: string,
): ExpSelectedModelResponse {
  void hash
  void rule
  return {
    metric: 'precision@',
    parameter: '10_pct',
    rule: rule ?? 'best_average_value',
    audition_group: 4,
    audition_model: 27,
    leaderboard_group: 7,
    leaderboard_model: 31,
    diverges: true,
  }
}

/* ========================================================================== */
/* Bias — experiment-scoped, 3-view source (group/disparity/fairness)          */
/* ========================================================================== */

const BIAS_ROWS: ExpBiasResponse = [
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'restaurant', metric: 'tpr', value: 0.41, ref_group_value: 'restaurant', disparity: 1.0 },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'restaurant', metric: 'fpr', value: 0.18, ref_group_value: 'restaurant', disparity: 1.0 },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'restaurant', metric: 'ppv', value: 0.34, ref_group_value: 'restaurant', disparity: 1.0 },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'grocery store', metric: 'tpr', value: 0.33, ref_group_value: 'restaurant', disparity: 0.80 },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'grocery store', metric: 'fpr', value: 0.15, ref_group_value: 'restaurant', disparity: 0.83 },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'grocery store', metric: 'ppv', value: 0.3, ref_group_value: 'restaurant', disparity: 0.88 },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'school', metric: 'tpr', value: 0.29, ref_group_value: 'restaurant', disparity: 0.71 },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'school', metric: 'fpr', value: 0.11, ref_group_value: 'restaurant', disparity: 0.61 },
  { model_id: 27, split_kind: 'test', as_of_date: SPLITS[2], parameter: '10_pct', attribute_name: 'facility_type', attribute_value: 'school', metric: 'ppv', value: 0.27, ref_group_value: 'restaurant', disparity: 0.79 },
]

export function expBiasFor(hash: string, modelId?: number): ExpBiasResponse {
  void hash
  if (!Array.isArray(BIAS_ROWS)) return BIAS_ROWS
  const mid = modelId ?? 27
  return BIAS_ROWS.map((r) => ({ ...r, model_id: mid }))
}

/* ========================================================================== */
/* Model-group + model detail (hierarchy)                                      */
/* ========================================================================== */

export function modelGroupDetail(id: number): ModelGroupDetailResponse {
  const g = GROUPS.find((x) => x.id === id) ?? GROUPS[0]
  const modelId = Object.entries(MODEL_GROUP).find(([, gid]) => gid === g.id)?.[0]
  const mid = modelId ? Number(modelId) : 27
  const allEvals = evalRows(EXP_FAILED).filter((r) => r.model_group_id === g.id)
  return {
    summary: modelGroupsFor(EXP_FAILED).find((m) => m.model_group_id === g.id)!,
    models: SPLITS.map((d, i) => ({
      model_id: mid + i,
      train_end_time: d,
      run_id: FIXTURE_RUN_ID,
      training_label_timespan: '6 mons',
      test_as_of: SPLITS[Math.min(i + 1, SPLITS.length - 1)],
    })),
    metric_over_time: allEvals,
    per_split: allEvals,
  }
}

/** Raw featurizer feature names per model (Bug B — exercises the prettifier). */
const FEATURE_IMPORTANCES: Record<number, ModelCardResponse['feature_importances']> = {
  27: [
    { model_id: 27, feature: 'COUNT(inspections.result|interval=P180D)', feature_importance: 0.284, rank_abs: 1, rank_pct: 0.99 },
    { model_id: 27, feature: 'RATE(inspections.result|interval=P1Y)', feature_importance: 0.197, rank_abs: 2, rank_pct: 0.98 },
    { model_id: 27, feature: 'MAX(inspections.risk_level|interval=P180D)', feature_importance: 0.121, rank_abs: 3, rank_pct: 0.97 },
    { model_id: 27, feature: 'AVG(inspections.violations|interval=P2Y)', feature_importance: 0.064, rank_abs: 4, rank_pct: 0.8 },
    { model_id: 27, feature: 'facilities.facility_type=school', feature_importance: 0.018, rank_abs: 5, rank_pct: 0.5 },
    { model_id: 27, feature: 'facilities.zip_code=60647', feature_importance: 0.011, rank_abs: 6, rank_pct: 0.4 },
  ],
  31: [
    { model_id: 31, feature: 'RATE(inspections.result|interval=P1Y)', feature_importance: 0.312, rank_abs: 1, rank_pct: 0.99 },
    { model_id: 31, feature: 'COUNT(inspections.result|interval=P180D)', feature_importance: 0.244, rank_abs: 2, rank_pct: 0.98 },
    { model_id: 31, feature: 'MAX(inspections.risk_level|interval=P180D)', feature_importance: 0.103, rank_abs: 3, rank_pct: 0.97 },
    { model_id: 31, feature: 'facilities.facility_type=restaurant', feature_importance: 0.014, rank_abs: 4, rank_pct: 0.45 },
  ],
}

export function modelCard(id: number): ModelCardResponse {
  const importances = FEATURE_IMPORTANCES[id] ?? FEATURE_IMPORTANCES[27]
  const gid = MODEL_GROUP[id] ?? 4
  const prec = GROUP_PREC[gid] ?? GROUP_PREC[4]
  const auc = GROUP_AUC[gid] ?? GROUP_AUC[4]
  const evaluations: ModelCardResponse['evaluations'] = []
  SPLITS.forEach((d, i) => {
    evaluations.push({
      model_id: id, split_kind: 'test', as_of_date: d, metric: 'precision@', parameter: '10_pct',
      value: prec[i], value_expected: prec[i] - 0.005, value_std: 0.02, num_labeled: 1995 + i * 100, num_positive: 480,
    })
    evaluations.push({
      model_id: id, split_kind: 'test', as_of_date: d, metric: 'auc_roc', parameter: '',
      value: auc[i], value_expected: auc[i] - 0.003, value_std: 0.01, num_labeled: 1995 + i * 100, num_positive: 480,
    })
  })
  return { model_id: id, model_group_id: gid, feature_importances: importances.map((f) => ({ ...f, model_id: id })), evaluations }
}

/** A monotone Rayid threshold curve (cumulative TP/FP by rank) over 20 points. */
export function modelCurve(id: number): ModelCurveResponse {
  void id
  const N = 2000
  const POS = 480
  const points: ThresholdCurvePoint[] = []
  for (let i = 1; i <= 20; i++) {
    const pct = i / 20
    const k = Math.round(pct * N)
    // Precision decays as we go deeper; recall climbs.
    const prec = Math.max(0.12, 0.62 - pct * 0.5)
    const tp = Math.min(POS, Math.round(prec * k))
    const fp = k - tp
    const fn = POS - tp
    const tn = N - k - fn
    points.push({
      k,
      pct: Number(pct.toFixed(3)),
      prec: Number(prec.toFixed(3)),
      rec: Number((tp / POS).toFixed(3)),
      tp,
      fp,
      fn,
      tn,
    })
  }
  return points
}

export function modelHistogram(id: number): ModelHistogramResponse {
  void id
  const bins: HistogramBin[] = []
  // Bimodal-ish score distribution over 12 bins.
  const counts = [310, 280, 230, 180, 140, 110, 90, 78, 66, 58, 50, 44]
  const posCounts = [12, 18, 26, 34, 42, 50, 56, 58, 56, 52, 46, 40]
  for (let i = 0; i < counts.length; i++) {
    const lo = i / counts.length
    bins.push({
      bin: i,
      lo: Number(lo.toFixed(3)),
      hi: Number(((i + 1) / counts.length).toFixed(3)),
      n: counts[i],
      n_pos: posCounts[i],
    })
  }
  return bins
}

export function modelPredictions(
  id: number,
  opts?: { limit?: number; offset?: number },
): ModelPredictionsResponse {
  const total = 200
  const all: PredictionRow[] = []
  for (let i = 0; i < total; i++) {
    all.push({
      entity_id: 9921 - i * 37,
      as_of_date: SPLITS[2],
      score: Number(Math.max(0.01, 0.96 - i * 0.0045).toFixed(3)),
      rank_abs: i + 1,
      rank_pct: Number(((i + 1) / total).toFixed(4)),
      outcome: i % 3 === 0 ? 1 : 0,
    })
  }
  void id
  const offset = opts?.offset ?? 0
  const limit = opts?.limit ?? 20
  return { rows: all.slice(offset, offset + limit), total }
}

export function entityProfile(id: number, experimentHash?: string): EntityProfileResponse {
  const groups = [1, 2, 3]
  const score_history: EntityScorePoint[] = []
  for (const g of groups) {
    SPLITS.forEach((d, i) => {
      score_history.push({
        model_group_id: g,
        model_id: g * 10 + i,
        experiment_hash: experimentHash ?? EXP_ACTIVE,
        as_of_date: d,
        score: Number((0.2 + g * 0.05 + i * 0.02).toFixed(4)),
        rank_abs: 5000 + g * 300 + i * 200,
        rank_pct: Number((0.4 + g * 0.02).toFixed(3)),
        model_type: 'sklearn.tree.DecisionTreeClassifier',
        hyperparameters: { max_depth: 3 + g },
        train_end_time: d,
      })
    })
  }
  return {
    entity_id: id,
    attributes: {
      entity_id: id,
      facility: 'east of edens',
      facility_type: 'restaurant',
      zip_code: '60646',
      address: '6350 n cicero ave',
    },
    label_history: SPLITS.map((d, i) => ({
      as_of_date: d,
      label_timespan: '6 mons',
      outcome: i % 2,
    })),
    score_history,
  }
}

/* ========================================================================== */
/* Project-level — metrics, ontology, status, derivation                       */
/* ========================================================================== */

export const metrics: MetricsResponse = [
  { metric: 'precision@', parameter: '10_pct', higher_is_better: true },
  { metric: 'precision@', parameter: '5_pct', higher_is_better: true },
  { metric: 'precision@', parameter: '20_pct', higher_is_better: true },
  { metric: 'recall@', parameter: '10_pct', higher_is_better: true },
  { metric: 'auc_roc', parameter: '', higher_is_better: true },
]

function volumeSeries(base: number, months: number): { period: string; n: number }[] {
  const out: { period: string; n: number }[] = []
  let cur = base
  for (let i = 0; i < months; i++) {
    cur = Math.round(cur * (0.97 + Math.random() * 0.08))
    const d = new Date(Date.UTC(2014 + Math.floor(i / 12), i % 12, 1))
    out.push({ period: d.toISOString().slice(0, 10), n: cur })
  }
  return out
}

export const ontology: OntologyResponse = {
  sources: [
    { source_name: 'inspections', relation: 'clean.inspections', knowledge_date_column: 'inspection_date', description: 'health inspections with results + violations', role: 'event' },
    { source_name: 'facilities', relation: 'clean.facilities', knowledge_date_column: 'license_start_date', description: 'licensed food facilities', role: 'entity' },
    { source_name: 'complaints', relation: 'clean.complaints', knowledge_date_column: 'received_date', description: 'public complaints by facility', role: 'event' },
  ],
  volumes: {
    inspections: volumeSeries(420, 36),
    facilities: volumeSeries(90, 36),
    complaints: volumeSeries(150, 36),
  },
  profile: {
    inspections: { total_rows: 74191, first_date: '2014-01-02', last_date: '2017-12-29', n_distinct_entities: 18909 },
    facilities: { total_rows: 22169, first_date: '2014-01-02', last_date: '2017-12-29', n_distinct_entities: 22169 },
    complaints: { total_rows: 5400, first_date: '2014-01-05', last_date: '2017-12-20', n_distinct_entities: 3120 },
  },
}

export const status: StatusResponse = {
  sources: sourcePins.current,
  engine_versions: { featurizer: 'v0.4.1 (3b60057f)', sklearn: '1.7.x', triage: '0.1.0' },
  gc: ((): ArtifactStatusRow[] => [
    { kind: 'matrix', status: 'built', n: 16 },
    { kind: 'matrix', status: 'collected', n: 4 },
    { kind: 'model', status: 'built', n: 24 },
    { kind: 'cohort', status: 'built', n: 2 },
    { kind: 'labels', status: 'built', n: 2 },
    { kind: 'feature_group', status: 'built', n: 2 },
  ])(),
  runs: { completed: 2, building: 1, failed: 1 },
  db: {
    server_version: '16.14',
    db_size: '728 MB',
    connections: 6,
    max_connections: 100,
    max_parallel_workers: 8,
    uptime: '4 days 10:28:36',
    reachable: true,
  },
  execution: {
    profile: 'local',
    purpose: 'experiment',
    status: 'completed',
    started_at: '2026-06-23T15:40:20Z',
    finished_at: '2026-06-23T15:40:26Z',
    duration_s: 6,
    triage_version: '0.1.0',
    git_hash: 'abc1234',
    batch_job_id: null,
  },
  compute: { cpu_count: 10, profile: 'local' },
  source_drift: [
    { source_name: 'inspections', run_version: 'dirtyduck-v1', head_version: 'dirtyduck-v1', drift: false },
    { source_name: 'facilities', run_version: 'dirtyduck-v1', head_version: 'dirtyduck-v2', drift: true },
  ],
}

export const projectDerivation: ProjectDerivationResponse = {
  nodes: [
    { artifact_id: 'src-db', kind: 'source', status: 'built', built_by_run: null, n_experiments: 2, n_runs: 4 },
    { artifact_id: 'src-fz', kind: 'source', status: 'built', built_by_run: null, n_experiments: 2, n_runs: 4 },
    { artifact_id: 'cohort-fi', kind: 'cohort', status: 'built', built_by_run: '81a68920…01', n_experiments: 1, n_runs: 2 },
    { artifact_id: 'cohort-af', kind: 'cohort', status: 'built', built_by_run: '7e4d…04', n_experiments: 1, n_runs: 2 },
    { artifact_id: 'fg-shared', kind: 'feature_group', status: 'built', built_by_run: '7e4d…04', n_experiments: 2, n_runs: 3 },
    { artifact_id: 'mx-fi', kind: 'matrix', status: 'built', built_by_run: '81a68920…01', n_experiments: 1, n_runs: 1 },
    { artifact_id: 'mx-af', kind: 'matrix', status: 'built', built_by_run: '7e4d…04', n_experiments: 1, n_runs: 2 },
    { artifact_id: 'models-fi', kind: 'model', status: 'building', built_by_run: '81a68920…01', n_experiments: 1, n_runs: 1 },
    { artifact_id: 'models-af', kind: 'model', status: 'built', built_by_run: '7e4d…04', n_experiments: 1, n_runs: 2 },
  ].map(addSplitFields),
  edges: [
    { parent_id: 'src-db', artifact_id: 'cohort-fi' },
    { parent_id: 'src-db', artifact_id: 'cohort-af' },
    { parent_id: 'src-fz', artifact_id: 'fg-shared' },
    { parent_id: 'cohort-fi', artifact_id: 'mx-fi' },
    { parent_id: 'cohort-af', artifact_id: 'mx-af' },
    { parent_id: 'fg-shared', artifact_id: 'mx-fi' },
    { parent_id: 'fg-shared', artifact_id: 'mx-af' },
    { parent_id: 'mx-fi', artifact_id: 'models-fi' },
    { parent_id: 'mx-af', artifact_id: 'models-af' },
  ],
}

/* -------------------------- write surface (ADR-0024) ---------------------- */
// A tiny in-memory control plane so the write pages work in `npm run dev` (fixture mode):
// lists read these arrays, create/submit mutate them. Not persisted across reloads.
import type { Member, Principal, Project, Submission } from '../api/types'

export const principal: Principal = {
  user_id: '00000000-0000-0000-0000-000000000001',
  email: 'dev@localhost',
  display_name: 'Dev User',
  is_admin: true,
}

export const projectsStore: Project[] = [
  {
    project_id: '00000000-0000-0000-0000-0000000000a1',
    slug: 'food',
    display_name: 'Food Inspections',
    database_name: 'food',
    status: 'active',
    created_at: '2026-06-20T12:00:00Z',
    archived_at: null,
  },
]

export const membersStore: Record<string, Member[]> = {
  food: [
    {
      project_id: '00000000-0000-0000-0000-0000000000a1',
      user_id: principal.user_id,
      role: 'owner',
      added_at: '2026-06-20T12:00:00Z',
      email: principal.email,
      display_name: principal.display_name,
    },
  ],
}

export const submissionsStore: Submission[] = [
  {
    submission_id: '00000000-0000-0000-0000-0000000000b1',
    project_id: '00000000-0000-0000-0000-0000000000a1',
    project_slug: 'food',
    submitted_by: principal.user_id,
    submitted_by_email: principal.email,
    experiment_hash: 'b9e38fd8f366aa22e8e3f761b446eb3a',
    profile: 'local',
    batch_job_id: null,
    submitted_at: '2026-06-20T12:05:00Z',
  },
]

let _seq = 2
function _uuid(tag: string): string {
  return `00000000-0000-0000-0000-0000000000${tag}${(_seq++).toString(16).padStart(1, '0')}`
}

/** Append a project + make `principal` its owner (fixture-mode create). */
export function fxCreateProject(slug: string, displayName: string, dbName?: string): Project {
  const p: Project = {
    project_id: _uuid('a'),
    slug,
    display_name: displayName,
    database_name: dbName || slug,
    status: 'active',
    created_at: new Date().toISOString(),
    archived_at: null,
  }
  projectsStore.unshift(p)
  membersStore[slug] = [
    {
      project_id: p.project_id,
      user_id: principal.user_id,
      role: 'owner',
      added_at: p.created_at,
      email: principal.email,
      display_name: principal.display_name,
    },
  ]
  return p
}

/** Append a submission row (fixture-mode submit). */
export function fxCreateSubmission(projectSlug: string, profile: 'local' | 'cloud'): Submission {
  const project = projectsStore.find((p) => p.slug === projectSlug)
  const s: Submission = {
    submission_id: _uuid('b'),
    project_id: project?.project_id ?? _uuid('a'),
    project_slug: projectSlug,
    submitted_by: principal.user_id,
    submitted_by_email: principal.email,
    experiment_hash: profile === 'cloud' ? null : `fixturehash${_seq.toString(16)}`.padEnd(32, '0'),
    profile,
    batch_job_id: profile === 'cloud' ? `job-${_seq}` : null,
    submitted_at: new Date().toISOString(),
  }
  submissionsStore.unshift(s)
  return s
}
