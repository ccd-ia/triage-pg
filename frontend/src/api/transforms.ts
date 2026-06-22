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
  EvaluationRow,
  LeaderboardRow,
  ModelEvaluationRow,
  ProgressResponse,
  RunSourcePin,
  StageKind,
  StageProgress,
  TemporalPlan,
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

  return STAGE_ORDER.map((kind) => {
    const acc = byKind.get(kind)
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
  /** metric name -> value (e.g. tpr/fpr/ppv/fdr...). */
  metrics: Record<string, number>
  /** disparity vs the reference group, if any metric row carries it. */
  disparity: number | null
  ref_group_value: string | null
}

/** Group long-format bias rows into one row per (attribute, value) with a metric map. */
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
        disparity: null,
        ref_group_value: r.ref_group_value,
      }
      byGroup.set(key, g)
    }
    if (r.value != null) g.metrics[r.metric] = r.value
    if (r.disparity != null) g.disparity = r.disparity
  }
  return [...byGroup.values()]
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
    const pin = p.version_label ?? p.fingerprint ?? '—'
    if (!c) return { source: p.source_name, pin, current: null, drift: 'pinned' as const }
    const cur = c.version_label ?? c.fingerprint ?? null
    const same =
      (p.version_label != null && p.version_label === c.version_label) ||
      (p.fingerprint != null && p.fingerprint === c.fingerprint)
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

/** Is the given ranking row the rule's pick? (compares to AuditionData.pick). */
export function isAuditionPick(data: AuditionData, model_group_id: number): boolean {
  return data.pick != null && data.pick === model_group_id
}
