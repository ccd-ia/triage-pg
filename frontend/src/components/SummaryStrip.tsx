/*
 * SummaryStrip — always-visible thin strip (spec §1/§3.1):
 * problem_type · status · cohort · base rate · #splits · #features · #models · run_id.
 * cohort size + base rate are the latest-split values from the per-split profiles.
 *
 * Reconciled to routes.py: the per-split base-rate array is `label_base_rate`
 * (not `base_rate`); problem_type / n_splits / features / models come off
 * `summary.summary` (run_summary view), n_splits from `summary.plan->n_splits`.
 */
import type { ExperimentSummary, SummaryResponse } from '../api/types'
import { StatusBadge } from './StatusBadge'

function latestCohortSize(s: SummaryResponse): number | null {
  const p = s.cohort_profile
  return p.length ? p[p.length - 1].n_entities : null
}

function latestBaseRate(s: SummaryResponse): number | null {
  const p = s.label_base_rate
  for (let i = p.length - 1; i >= 0; i--) {
    if (p[i].base_rate != null) return p[i].base_rate
  }
  return null
}

function fmtInt(n: number | null | undefined): string {
  return n == null ? '—' : n.toLocaleString('en-US')
}

function fmtPct(x: number | null | undefined): string {
  return x == null ? '—' : `${(x * 100).toFixed(1)}%`
}

/**
 * The always-visible strip. `actuals` (the experiment_summary row, migration 0006)
 * supplies splits/features/models/base-rate/cohort derived from what was BUILT, so the
 * strip is populated even when runs.plan is null (the pre-plan food runs showed "—").
 * Live per-split profiles still win for cohort/base-rate when present.
 */
export function SummaryStrip({
  data,
  actuals,
}: {
  data: SummaryResponse
  actuals?: ExperimentSummary | null
}) {
  const s = data.summary
  const nSplits = (s.plan?.n_splits ?? actuals?.n_splits ?? null) as number | null
  const nFeatures = s.n_features ?? actuals?.n_features ?? null
  const nGroups = actuals?.n_model_groups ?? null
  const nModels = s.n_models ?? actuals?.n_models ?? null
  const cohort = latestCohortSize(data) ?? actuals?.cohort_size ?? null
  const baseRate = latestBaseRate(data) ?? actuals?.base_rate ?? null
  return (
    <div className="strip">
      <Cell label="problem" value={s.problem_type ?? actuals?.problem_type ?? '—'} />
      <Cell label="status" value={<StatusBadge status={s.status} />} />
      <Cell label="cohort" value={fmtInt(cohort)} numeric />
      <Cell label="base rate" value={fmtPct(baseRate)} numeric />
      <Cell label="splits" value={fmtInt(nSplits)} numeric />
      <Cell label="features" value={fmtInt(nFeatures)} numeric />
      <Cell label="groups" value={fmtInt(nGroups)} numeric />
      <Cell label="models" value={fmtInt(nModels)} numeric />
      <Cell label="run" value={s.run_id.slice(0, 8)} mono />
    </div>
  )
}

function Cell({
  label,
  value,
  numeric,
  mono,
}: {
  label: string
  value: React.ReactNode
  numeric?: boolean
  mono?: boolean
}) {
  return (
    <div className="cell">
      <span className="lbl">{label}</span>
      <span className={`val${numeric ? ' num' : ''}${mono ? ' mono' : ''}`}>{value}</span>
    </div>
  )
}
