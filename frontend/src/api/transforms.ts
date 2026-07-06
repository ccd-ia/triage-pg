/*
 * Client-side reshaping of the RAW API rows (src/triage/dashboard/routes.py)
 * into the shapes the presentational components want.
 *
 * routes.py returns bare view rows (long-format metrics, unranked leaderboard,
 * per-(kind,status) progress counts). The dashboard panels want overlay series,
 * ranked tables, per-stage progress, etc. That reshaping lives here — one place
 * — instead of being smeared across components, so the components stay thin and
 * the API contract stays honest about what the backend actually ships.
 */
import type {
  AuditionData,
  BiasMetricRow,
  CurrentSourcePin,
  Fingerprint,
  EvaluationRow,
  ExpAuditionCurveRow,
  ExpEvaluationRow,
  LeaderboardRow,
  ModelEvaluationRow,
  ProgressResponse,
  RunSourcePin,
  StageKind,
  StageProgress,
  TemporalPlan,
  ThresholdCurvePoint,
} from './types'

/* ----------------------------- metric keys ------------------------------- */

/**
 * Combine a (metric, parameter) pair into a single display key, e.g.
 * ("precision@", "10_pct") -> "precision@10_pct"; ("auc_roc", "") -> "auc_roc".
 * The backend stores metric and parameter in separate columns; the UI wants one
 * label per series/column.
 */
export function metricKey(metric: string, parameter: string | null | undefined): string {
  const p = parameter ?? ''
  if (!p) return metric
  // metric often already ends in the operator (e.g. "precision@"); just append.
  return `${metric}${p}`
}

/* --------------------------- progress -> stages -------------------------- */

const STAGE_ORDER: StageKind[] = ['cohort', 'labels', 'matrices', 'models', 'evaluate']

/** A planned denominator for a stage, read from runs.plan when present. */
function plannedFor(kind: StageKind, plan: TemporalPlan | null): number {
  if (!plan) return 0
  // Tolerate a few likely key spellings in runs.plan without over-fitting.
  const p = plan as Record<string, unknown>
  const candidates: Record<StageKind, string[]> = {
    cohort: ['n_cohorts', 'n_as_of_dates', 'n_splits'],
    labels: ['n_label_sets', 'n_as_of_dates', 'n_splits'],
    matrices: ['n_matrices'],
    models: ['n_models'],
    evaluate: ['n_evaluations', 'n_models'],
  }
  for (const key of candidates[kind]) {
    const v = p[key]
    if (typeof v === 'number') return v
  }
  return 0
}

/**
 * Fold the raw per-(kind,status) counts into one entry per pipeline stage with a
 * done/current/todo status and an N/M (built / planned). "built"/"completed"
 * rows count toward N; any "building"/"running" row makes the stage current; a
 * stage with N>0 and N>=M (when M known) is done; otherwise todo.
 */
/** Stage → the run_progress artifact kind(s) that feed it. run_progress emits the SINGULAR
 *  artifact kinds (matrix/model/feature_group); the pipeline stages are plural — without this
 *  mapping `matrices`/`models` never matched and a completed run showed them as TODO. */
const STAGE_PROGRESS_KINDS: Record<StageKind, string[]> = {
  cohort: ['cohort'],
  labels: ['labels'],
  matrices: ['matrix', 'feature_group'],
  models: ['model'],
  evaluate: ['evaluation', 'evaluate'],
}

export function deriveStages(data: ProgressResponse): StageProgress[] {
  const byKind = new Map<string, { built: number; building: number; failed: number; total: number }>()
  for (const r of data.progress) {
    const acc = byKind.get(r.kind) ?? { built: 0, building: 0, failed: 0, total: 0 }
    const st = (r.status ?? '').toLowerCase()
    acc.total += r.n
    if (st === 'built' || st === 'completed' || st === 'done') acc.built += r.n
    else if (st === 'building' || st === 'running' || st === 'current') acc.building += r.n
    else if (st === 'failed' || st === 'error') acc.failed += r.n
    byKind.set(r.kind, acc)
  }

  // Fold the run_progress rows for a stage's underlying artifact kinds into one accumulator.
  const fold = (kinds: string[]) => {
    const acc = { built: 0, building: 0, failed: 0, total: 0 }
    let any = false
    for (const k of kinds) {
      const a = byKind.get(k)
      if (!a) continue
      any = true
      acc.built += a.built
      acc.building += a.building
      acc.failed += a.failed
      acc.total += a.total
    }
    return any ? acc : null
  }

  const result = STAGE_ORDER.map((kind) => {
    const acc = fold(STAGE_PROGRESS_KINDS[kind])
    const m = plannedFor(kind, data.plan)
    if (!acc) {
      return { kind, status: 'todo' as const, n: 0, m }
    }
    let status: StageProgress['status']
    if (acc.building > 0) status = 'current'
    else if (acc.built > 0 && (m === 0 || acc.built >= m)) status = 'done'
    else if (acc.built > 0) status = 'current'
    else status = 'todo'
    return { kind, status, n: acc.built, m: m || acc.total || acc.built }
  })

  // evaluate has no artifact telemetry in run_progress; mirror the models stage — evaluations
  // are produced right after training in the same run, so a done models stage ⇒ evaluate done.
  const models = result.find((s) => s.kind === 'models')
  const evaluate = result.find((s) => s.kind === 'evaluate')
  if (evaluate && evaluate.status === 'todo' && models && models.status !== 'todo') {
    evaluate.status = models.status
    evaluate.n = models.n
    evaluate.m = models.m
  }
  return result
}

/* ----------------------- evaluations -> overlay series ------------------- */

export interface MetricSeriesPoint {
  as_of_date: string
  value: number | null
}

export interface MetricSeries {
  /** Display key, e.g. "precision@10_pct" or "auc_roc". */
  metric: string
  points: MetricSeriesPoint[]
}

/**
 * Group flat evaluation rows into one series per (metric,parameter), averaging
 * across model_groups at each as_of_date (the card overlays metric trends, not
 * per-model lines). Points are sorted by as_of_date; the union of dates across
 * series is used so every series aligns on the X axis.
 */
export function buildMetricSeries(rows: EvaluationRow[]): MetricSeries[] {
  // metricKey -> as_of_date -> {sum, count}
  const acc = new Map<string, Map<string, { sum: number; count: number }>>()
  const allDates = new Set<string>()
  for (const r of rows) {
    if (r.value == null) continue
    const key = metricKey(r.metric, r.parameter)
    allDates.add(r.as_of_date)
    let byDate = acc.get(key)
    if (!byDate) {
      byDate = new Map()
      acc.set(key, byDate)
    }
    const cell = byDate.get(r.as_of_date) ?? { sum: 0, count: 0 }
    cell.sum += r.value
    cell.count += 1
    byDate.set(r.as_of_date, cell)
  }

  const dates = [...allDates].sort()
  return [...acc.entries()].map(([metric, byDate]) => ({
    metric,
    points: dates.map((d) => {
      const cell = byDate.get(d)
      return { as_of_date: d, value: cell ? cell.sum / cell.count : null }
    }),
  }))
}

/* --------------------- leaderboard -> ranked per-model ------------------- */

export interface LeaderboardEntry {
  model_id: number
  model_group_id: number
  model_type: string | null
  /** Display label derived from model_type / ids. */
  label: string
  /** metricKey -> latest value (latest as_of_date wins). */
  metrics: Record<string, number>
  /** 1-based rank by the chosen ranking metric (desc), undefined if unranked. */
  rank?: number
}

/** A short human label for a model row when the API gives no name. */
function modelLabel(model_type: string | null, model_id: number, model_group_id: number): string {
  const base = model_type ? shortType(model_type) : `model ${model_id}`
  return `${base} · g${model_group_id} · m${model_id}`
}

/** Shorten a sklearn class path to its leaf, e.g. "...RandomForestClassifier" -> "RandomForest". */
function shortType(model_type: string): string {
  const leaf = model_type.split('.').pop() ?? model_type
  return leaf.replace(/Classifier$|Regressor$/, '')
}

/**
 * Reshape bare leaderboard rows into one entry per model with a metricKey->value
 * map (latest as_of_date wins), then rank by `rankBy` descending. `rankBy` is a
 * metricKey; if absent or unknown, entries keep insertion order with no rank.
 * Returns [] for an empty matview (mid-run before REFRESH) — handled gracefully.
 */
export function rankLeaderboard(rows: LeaderboardRow[], rankBy?: string): LeaderboardEntry[] {
  const byModel = new Map<number, LeaderboardEntry>()
  // Track the latest as_of_date seen per (model, metricKey) so later splits win.
  const latestDate = new Map<string, string>()

  for (const r of rows) {
    let e = byModel.get(r.model_id)
    if (!e) {
      e = {
        model_id: r.model_id,
        model_group_id: r.model_group_id,
        model_type: r.model_type,
        label: modelLabel(r.model_type, r.model_id, r.model_group_id),
        metrics: {},
      }
      byModel.set(r.model_id, e)
    }
    if (r.value == null) continue
    const key = metricKey(r.metric, r.parameter)
    const seenKey = `${r.model_id}::${key}`
    const prev = latestDate.get(seenKey)
    if (prev === undefined || r.as_of_date >= prev) {
      e.metrics[key] = r.value
      latestDate.set(seenKey, r.as_of_date)
    }
  }

  const entries = [...byModel.values()]
  if (rankBy) {
    entries.sort((a, b) => (b.metrics[rankBy] ?? -Infinity) - (a.metrics[rankBy] ?? -Infinity))
    let rank = 0
    for (const e of entries) {
      rank += 1
      if (e.metrics[rankBy] !== undefined) e.rank = rank
    }
  }
  return entries
}

/** The set of metricKeys present across leaderboard entries, in stable order. */
export function leaderboardMetricKeys(entries: LeaderboardEntry[]): string[] {
  const seen = new Set<string>()
  for (const e of entries) for (const k of Object.keys(e.metrics)) seen.add(k)
  return [...seen]
}

/* ------------------------- bias -> grouped rows ------------------------- */

export interface BiasGroupRow {
  attribute_name: string
  attribute_value: string
  /** metric name -> value (selection_rate/precision/tpr/fpr/fnr/fdr/for/npv + counts). */
  metrics: Record<string, number>
  /** metric name -> disparity vs the reference group (per-metric, migration 0014). */
  disparities: Record<string, number | null>
  /** metric name -> SQL fairness verdict at τ (null = no verdict for that metric). */
  passes: Record<string, boolean | null>
  /** the τ the verdicts used (null on pre-0014 rows). */
  tau: number | null
  /** representative disparity: selection_rate's when present, else the first seen. */
  disparity: number | null
  ref_group_value: string | null
}

/** Group long-format bias rows into one row per (attribute, value) with metric maps. */
export function groupBias(rows: BiasMetricRow[]): BiasGroupRow[] {
  const byGroup = new Map<string, BiasGroupRow>()
  for (const r of rows) {
    const key = `${r.attribute_name}::${r.attribute_value}`
    let g = byGroup.get(key)
    if (!g) {
      g = {
        attribute_name: r.attribute_name,
        attribute_value: r.attribute_value,
        metrics: {},
        disparities: {},
        passes: {},
        tau: null,
        disparity: null,
        ref_group_value: r.ref_group_value,
      }
      byGroup.set(key, g)
    }
    if (r.value != null) g.metrics[r.metric] = r.value
    g.disparities[r.metric] = r.disparity
    g.passes[r.metric] = r.passes_fairness ?? null
    if (r.tau != null) g.tau = r.tau
    if (r.disparity != null && (g.disparity == null || r.metric === 'selection_rate')) {
      g.disparity = r.disparity
    }
  }
  return [...byGroup.values()]
}

/* ----------------------- the fairness tree (plan P2) --------------------- */

export type Intervention = 'punitive' | 'assistive' | 'representation'

export interface FairnessFocus {
  /** the metric the tree says matters most (highlighted + drives views 2/3). */
  primary: string
  /** the metric family worth reading together (primary first). */
  family: string[]
  rationale: string
}

/**
 * The Aequitas Fairness Tree, operationalized (docs/fairness.md): which disparity
 * family matters for which intervention type. Pure routing — it never hides metrics.
 * Credit: https://datasciencepublicpolicy.org/our-work/tools-guides/aequitas/
 */
export function fairnessFocus(intervention: Intervention, capped: boolean): FairnessFocus {
  if (intervention === 'representation') {
    return {
      primary: 'selection_rate',
      family: ['selection_rate'],
      rationale:
        'Representation: the question is who gets picked at all — compare each group’s selection rate (demographic parity).',
    }
  }
  if (intervention === 'punitive') {
    return capped
      ? {
          primary: 'fdr',
          family: ['fdr', 'fpr'],
          rationale:
            'Punitive + capped list: the harm is a WRONG flag among those acted on — compare false discovery rates (FDR parity).',
        }
      : {
          primary: 'fpr',
          family: ['fpr', 'fdr'],
          rationale:
            'Punitive, acting broadly: the harm is wrongly flagging the innocent — compare false positive rates (FPR parity).',
        }
  }
  return capped
    ? {
        primary: 'fnr',
        family: ['fnr', 'for'],
        rationale:
          'Assistive + capped list: the harm is MISSING someone in need — compare false negative rates (FNR parity).',
      }
    : {
        primary: 'for',
        family: ['for', 'fnr'],
        rationale:
          'Assistive, acting broadly: the harm concentrates among those passed over — compare false omission rates (FOR parity).',
      }
}

/* ----------------------- model evals -> per-split ----------------------- */

export interface PerSplitEval {
  as_of_date: string
  /** metricKey -> value. */
  metrics: Record<string, number | null>
  num_labeled: number | null
}

/** Fold long-format model evaluations (test split) into one row per as_of_date. */
export function perSplitEvals(rows: ModelEvaluationRow[]): PerSplitEval[] {
  const byDate = new Map<string, PerSplitEval>()
  for (const r of rows) {
    if (r.split_kind !== 'test') continue
    let row = byDate.get(r.as_of_date)
    if (!row) {
      row = { as_of_date: r.as_of_date, metrics: {}, num_labeled: r.num_labeled ?? null }
      byDate.set(r.as_of_date, row)
    }
    row.metrics[metricKey(r.metric, r.parameter)] = r.value
    if (r.num_labeled != null) row.num_labeled = r.num_labeled
  }
  return [...byDate.values()].sort((a, b) => a.as_of_date.localeCompare(b.as_of_date))
}

/* --------------------------- source pins -> drift ----------------------- */

export interface SourcePinView {
  source: string
  pin: string
  current: string | null
  /** "stable" when the run pin == registry head, "drift" otherwise, "pinned" when no head. */
  drift: 'stable' | 'drift' | 'pinned'
}

/**
 * Format a source fingerprint (an advisory {row_count, max_knowledge_date} jsonb object,
 * NOT a string) to a short label. Renders nothing as a raw object — guards against React
 * "objects are not valid as a child" crashes (the /status fingerprint regression).
 */
export function fmtFingerprint(fp: Fingerprint): string | null {
  if (fp == null) return null
  if (typeof fp === 'string') return fp
  const rc = (fp as { row_count?: number }).row_count
  const kd = (fp as { max_knowledge_date?: string | null }).max_knowledge_date
  if (rc != null || kd != null) {
    return [rc != null ? `${rc} rows` : null, kd ?? null].filter(Boolean).join(' · ')
  }
  return JSON.stringify(fp)
}

/**
 * Join the run's frozen pins against the registry's current head per source;
 * drift = the two version_labels (or fingerprints) differ.
 */
export function deriveSourcePins(
  runPins: RunSourcePin[],
  current: CurrentSourcePin[],
): SourcePinView[] {
  const head = new Map<string, CurrentSourcePin>()
  for (const c of current) head.set(c.source_name, c)
  return runPins.map((p) => {
    const c = head.get(p.source_name)
    const pin = p.version_label ?? fmtFingerprint(p.fingerprint) ?? '—'
    if (!c) return { source: p.source_name, pin, current: null, drift: 'pinned' as const }
    const cur = c.version_label ?? fmtFingerprint(c.fingerprint) ?? null
    const same =
      (p.version_label != null && p.version_label === c.version_label) ||
      (p.fingerprint != null && fmtFingerprint(p.fingerprint) === fmtFingerprint(c.fingerprint))
    return {
      source: p.source_name,
      pin,
      current: cur,
      drift: same ? ('stable' as const) : ('drift' as const),
    }
  })
}

/* ------------------------ audition pick label --------------------------- */

/** model_group label for the audition table when the API gives only ids. */
export function groupLabel(model_group_id: number): string {
  return `group ${model_group_id}`
}

/** Short algorithm code (RF, DT, GB, ET, LR…) from a full estimator path/class name. */
const ALGO_ABBREV: Record<string, string> = {
  RandomForestClassifier: 'RF',
  RandomForestRegressor: 'RF',
  DecisionTreeClassifier: 'DT',
  DecisionTreeRegressor: 'DT',
  GradientBoostingClassifier: 'GB',
  GradientBoostingRegressor: 'GB',
  ExtraTreesClassifier: 'ET',
  ExtraTreesRegressor: 'ET',
  LogisticRegression: 'LR',
  ScaledLogisticRegression: 'sLR',
  LinearRegression: 'OLS',
}
export function abbrevAlgo(modelType: string | null | undefined): string {
  if (!modelType) return '—'
  const leaf = modelType.split('.').pop() ?? modelType
  return ALGO_ABBREV[leaf] ?? leaf.replace(/Classifier$|Regressor$/, '')
}

/** A short, DISTINGUISHING hyperparameter suffix for a group label — algorithm-aware so each
 *  estimator family shows the parameter(s) that actually vary it (trees: depth; logistic: C +
 *  penalty; otherwise the first hyperparameter). Keeps the grid/labels narrow but unambiguous. */
export function hyperSuffix(
  modelType: string | null | undefined,
  hp: Record<string, unknown> | null | undefined,
): string {
  if (!hp || Object.keys(hp).length === 0) return ''
  const leaf = (modelType ?? '').split('.').pop() ?? ''
  if (/Logistic/i.test(leaf)) {
    const parts: string[] = []
    if (hp.C != null) parts.push(`C=${String(hp.C)}`)
    if (hp.penalty != null) parts.push(String(hp.penalty))
    if (parts.length) return parts.join(' · ')
  }
  if (hp.max_depth != null) {
    const n = hp.n_estimators != null ? ` · n=${String(hp.n_estimators)}` : ''
    return `depth ${String(hp.max_depth)}${n}`
  }
  const [k, v] = Object.entries(hp)[0]
  return `${k}=${String(v)}`
}

/** Is the given ranking row the rule's pick? (compares to AuditionData.pick). */
export function isAuditionPick(data: AuditionData, model_group_id: number): boolean {
  return data.pick != null && data.pick === model_group_id
}

/* ------------------------ feature names (Bug B) ------------------------- */

/** A prettified featurizer feature name: a human label + the raw source. */
export interface PrettyFeature {
  pretty: string
  raw: string
}

/** Turn an ISO-8601 duration (P180D / P1Y / P6M / P2W) into a short label. */
function prettyInterval(iso: string): string {
  // PnYnMnD or PnW. Render the first non-zero component compactly.
  const m = iso.match(/^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?$/i)
  if (!m) return iso
  const [, y, mo, w, d] = m
  if (y) return `${y}y`
  if (mo) return `${mo}mo`
  if (w) return `${w}w`
  if (d) return `${d}d`
  return iso
}

/**
 * Prettify a raw featurizer feature string (Bug B). Handles the canonical DFS
 * shape `AGG(table.column|interval=Pxxx)` →
 * `table · agg(column) · <interval>`, plus the simpler categorical one-hot
 * `table.col=value` → `table · col = value`. Anything it does not recognize is
 * passed through verbatim (so the raw subline always stays truthful).
 */
export function prettyFeature(raw: string): PrettyFeature {
  // AGG(table.column|interval=P180D[,more]) — the DFS primitive shape.
  const agg = raw.match(/^([A-Za-z_]+)\(([^)|]+)(?:\|([^)]*))?\)$/)
  if (agg) {
    const [, fn, target, opts] = agg
    const parts: string[] = []
    // target is usually `table.column`; keep both around the agg call.
    const dot = target.indexOf('.')
    const table = dot >= 0 ? target.slice(0, dot) : null
    const column = dot >= 0 ? target.slice(dot + 1) : target
    if (table) parts.push(table)
    parts.push(`${fn.toLowerCase()}(${column})`)
    if (opts) {
      const interval = opts.match(/interval=([A-Za-z0-9]+)/i)
      if (interval) parts.push(prettyInterval(interval[1]))
    }
    return { pretty: parts.join(' · '), raw }
  }
  // Categorical one-hot: table.col=value
  const cat = raw.match(/^([A-Za-z_]+)\.([A-Za-z0-9_]+)=(.+)$/)
  if (cat) {
    const [, table, col, value] = cat
    return { pretty: `${table} · ${col} = ${value}`, raw }
  }
  // table.column (no agg) — light prettify with a middot.
  const plain = raw.match(/^([A-Za-z_]+)\.([A-Za-z0-9_]+)$/)
  if (plain) {
    const [, table, col] = plain
    return { pretty: `${table} · ${col}`, raw }
  }
  return { pretty: raw, raw }
}

/* -------------------- experiment audition (8 strategies) ----------------- */

/** Distance-from-best curve shaped wide for recharts (one row per as_of_date). */
export interface ExpCurvePoint {
  as_of_date: string
  [groupLabel: string]: number | string | null
}

/** Fold experiment-scoped audition distances into wide chart rows + group ids. */
export function expAuditionChart(
  curves: ExpAuditionCurveRow[],
): { rows: ExpCurvePoint[]; groups: { id: number; label: string }[] } {
  const byGroup = new Map<number, Map<string, number | null>>()
  const allDates = new Set<string>()
  for (const c of curves) {
    allDates.add(c.as_of_date)
    let g = byGroup.get(c.model_group_id)
    if (!g) {
      g = new Map()
      byGroup.set(c.model_group_id, g)
    }
    g.set(c.as_of_date, c.dist_from_best_case)
  }
  const dates = [...allDates].sort()
  const groups = [...byGroup.keys()].map((id) => ({ id, label: groupLabel(id) }))
  const rows: ExpCurvePoint[] = dates.map((d) => {
    const row: ExpCurvePoint = { as_of_date: d.slice(0, 7) }
    for (const { id, label } of groups) {
      row[label] = byGroup.get(id)?.get(d) ?? null
    }
    return row
  })
  return { rows, groups }
}

/* ------------------------- model-group grid (Option 3) ------------------- */

export interface GridCell {
  model_group_id: number
  as_of_date: string
  value: number | null
  /** True when this is the best value in its column (split). */
  best: boolean
}

export interface GridData {
  /** Distinct as_of_date columns, sorted. */
  dates: string[]
  /** One row per model_group, with a value per date. */
  rows: { model_group_id: number; cells: Map<string, GridCell> }[]
  /** Global min/max across non-null values, for the heat scale. */
  min: number
  max: number
}

/**
 * Build the Option-3 model-group × split grid from experiment evaluations for a
 * single (metric, parameter). One row per model_group, one column per
 * as_of_date; the best value in each column is flagged. `higherIsBetter`
 * decides which extreme wins a column and anchors the heat scale.
 */
export function buildGrid(
  rows: ExpEvaluationRow[],
  metric: string,
  parameter: string,
  higherIsBetter: boolean,
): GridData {
  const dates = new Set<string>()
  // model_group_id -> as_of_date -> value (latest model in the group wins a cell)
  const byGroup = new Map<number, Map<string, number>>()
  let min = Infinity
  let max = -Infinity
  for (const r of rows) {
    if (r.metric !== metric || (r.parameter ?? '') !== parameter) continue
    if (r.value == null) continue
    dates.add(r.as_of_date)
    let g = byGroup.get(r.model_group_id)
    if (!g) {
      g = new Map()
      byGroup.set(r.model_group_id, g)
    }
    // Multiple models per group/date can exist; keep the better one.
    const prev = g.get(r.as_of_date)
    if (prev === undefined || (higherIsBetter ? r.value > prev : r.value < prev)) {
      g.set(r.as_of_date, r.value)
    }
    if (r.value < min) min = r.value
    if (r.value > max) max = r.value
  }
  const sortedDates = [...dates].sort()
  // Best per column.
  const colBest = new Map<string, number>()
  for (const d of sortedDates) {
    let best: number | undefined
    for (const g of byGroup.values()) {
      const v = g.get(d)
      if (v === undefined) continue
      if (best === undefined || (higherIsBetter ? v > best : v < best)) best = v
    }
    if (best !== undefined) colBest.set(d, best)
  }
  const gridRows = [...byGroup.entries()].map(([gid, byDate]) => {
    const cells = new Map<string, GridCell>()
    for (const d of sortedDates) {
      const v = byDate.get(d) ?? null
      cells.set(d, {
        model_group_id: gid,
        as_of_date: d,
        value: v,
        best: v != null && colBest.get(d) === v,
      })
    }
    return { model_group_id: gid, cells }
  })
  // Sort rows by their average value (best groups on top).
  gridRows.sort((a, b) => {
    const avg = (cells: Map<string, GridCell>) => {
      const vals = [...cells.values()].map((c) => c.value).filter((v): v is number => v != null)
      return vals.length ? vals.reduce((s, v) => s + v, 0) / vals.length : higherIsBetter ? -Infinity : Infinity
    }
    return higherIsBetter ? avg(b.cells) - avg(a.cells) : avg(a.cells) - avg(b.cells)
  })
  return {
    dates: sortedDates,
    rows: gridRows,
    min: min === Infinity ? 0 : min,
    max: max === -Infinity ? 1 : max,
  }
}

/* ----------------------- Rayid curve (client k-slider) ------------------- */

/**
 * Pick the curve point at (or just past) a target population fraction `pct`
 * (0..1). The series is the SQL-computed source of truth (ADR-0012); the slider
 * is a pure lookup — no recompute. Returns the closest point by |pct - target|.
 */
export function curveAtPct(curve: ThresholdCurvePoint[], target: number): ThresholdCurvePoint | null {
  if (curve.length === 0) return null
  let best = curve[0]
  let bestDist = Math.abs(curve[0].pct - target)
  for (const p of curve) {
    const d = Math.abs(p.pct - target)
    if (d < bestDist) {
      best = p
      bestDist = d
    }
  }
  return best
}
