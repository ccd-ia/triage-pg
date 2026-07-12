/*
 * OverviewSparklines (Q4) — the 4 per-split profile sparklines shown directly
 * below the experiment summary strip: cohort size · labels · %-labeled [0–1] ·
 * base-rate [0–1]. The two rate charts share the [0–1] scale so they are visually
 * comparable; cohort/labels are count charts with their own auto scale. Splits
 * whose label window has not yet matured are shaded (immature — Q4): we treat a
 * split with n_labeled == 0 (or null base_rate) as immature.
 *
 * Derived from /runs/{id}/summary (cohort_profile + label_base_rate):
 *   %labeled = n_labeled / n_entities.
 */
import { Area, AreaChart, ReferenceArea, ResponsiveContainer, Tooltip, YAxis } from 'recharts'
import type { ExperimentSummary, SummaryResponse } from '../api/types'
import { tooltipFormatter } from '../api/format'

interface Mini {
  title: string
  /** big headline value (latest split) */
  headline: string
  points: { i: number; v: number | null; immature: boolean }[]
  domain: [number, number] | undefined
  color: string
  /** framing-aware context line under the headline (migration 0019). */
  note?: string
  noteTone?: 'muted' | 'warn'
}

/** The %-labeled card reads differently per observation regime (task_framing): an
 * inspections problem EXPECTS <100% (only acted-on entities get outcomes), while an
 * early-warning problem showing <100% deserves a second look. */
function pctLabeledNote(
  exp: ExperimentSummary | null | undefined,
  pct: number | null,
): { note?: string; noteTone?: 'muted' | 'warn' } {
  switch (exp?.task_framing) {
    case 'resource_prioritization':
      return { note: 'selective labels — <100% expected', noteTone: 'muted' }
    case 'early_warning':
      return pct != null && pct < 0.995
        ? { note: 'early-warning: labels should cover the cohort', noteTone: 'warn' }
        : { note: 'outcome observed for the full cohort', noteTone: 'muted' }
    case 'visit_level':
      return { note: 'label per visit event', noteTone: 'muted' }
    default:
      return {}
  }
}

function buildMinis(data: SummaryResponse, exp?: ExperimentSummary | null): Mini[] {
  const cohort = data.cohort_profile
  const labels = data.label_base_rate
  const n = Math.max(cohort.length, labels.length)
  const idx = Array.from({ length: n }, (_, i) => i)

  const cohortPts = idx.map((i) => {
    const c = cohort[i]
    return { i, v: c ? c.n_entities : null, immature: false }
  })
  const labelPts = idx.map((i) => {
    const l = labels[i]
    const immature = !l || l.n_labeled === 0 || l.base_rate == null
    return { i, v: l ? l.n_labeled : null, immature }
  })
  const pctPts = idx.map((i) => {
    const c = cohort[i]
    const l = labels[i]
    const immature = !l || l.n_labeled === 0 || l.base_rate == null
    const v = c && c.n_entities > 0 && l ? l.n_labeled / c.n_entities : null
    return { i, v, immature }
  })
  const ratePts = idx.map((i) => {
    const l = labels[i]
    const immature = !l || l.n_labeled === 0 || l.base_rate == null
    return { i, v: l ? l.base_rate : null, immature }
  })

  const last = (pts: { v: number | null }[]) => {
    for (let i = pts.length - 1; i >= 0; i--) if (pts[i].v != null) return pts[i].v
    return null
  }
  const fmtInt = (v: number | null) => (v == null ? '—' : v.toLocaleString('en-US'))
  const fmtPct = (v: number | null) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
  const fmtRate = (v: number | null) => (v == null ? '—' : v.toFixed(3))

  // Survival labels carry duration/event_observed (ADR-0010): label_base_rate's
  // base_rate is then the observed-event rate — title the card accordingly.
  const survival = exp?.problem_type === 'survival'
  return [
    { title: 'cohort', headline: fmtInt(last(cohortPts)), points: cohortPts, domain: undefined, color: 'var(--acc)' },
    { title: 'labels', headline: fmtInt(last(labelPts)), points: labelPts, domain: undefined, color: 'var(--acc2)' },
    {
      title: '% labeled',
      headline: fmtPct(last(pctPts)),
      points: pctPts,
      domain: [0, 1],
      color: 'var(--ok)',
      ...pctLabeledNote(exp, last(pctPts)),
    },
    {
      title: survival ? 'event rate' : 'base rate',
      headline: fmtRate(last(ratePts)),
      points: ratePts,
      domain: [0, 1],
      color: 'var(--warn)',
      ...(survival ? { note: 'share of labels with the event observed', noteTone: 'muted' as const } : {}),
    },
  ]
}

function MiniChart({ mini }: { mini: Mini }) {
  // Immature shading: shade the contiguous trailing immature region.
  const firstImmature = mini.points.findIndex((p) => p.immature)
  const showShade = firstImmature >= 0
  return (
    <div className="sk">
      <span className="lbl">{mini.title}</span>
      <span className="big">{mini.headline}</span>
      {mini.note ? (
        <span
          className="muted"
          style={{
            display: 'block',
            fontSize: 9.5,
            lineHeight: 1.2,
            color: mini.noteTone === 'warn' ? 'var(--warn)' : undefined,
          }}
        >
          {mini.note}
        </span>
      ) : null}
      <div style={{ height: 40, marginTop: 4 }}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={mini.points} margin={{ top: 4, right: 2, bottom: 0, left: 0 }}>
            <YAxis hide domain={mini.domain ?? ['auto', 'auto']} />
            <Tooltip
              contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)', fontSize: 11 }}
              labelFormatter={() => mini.title}
              formatter={tooltipFormatter(4)}
            />
            {showShade ? (
              <ReferenceArea
                x1={firstImmature}
                x2={mini.points.length - 1}
                fill="var(--warn)"
                fillOpacity={0.1}
              />
            ) : null}
            <Area
              type="monotone"
              dataKey="v"
              stroke={mini.color}
              fill={mini.color}
              fillOpacity={0.14}
              strokeWidth={2}
              dot={false}
              connectNulls
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

export function OverviewSparklines({
  data,
  experiment,
}: {
  data: SummaryResponse
  experiment?: ExperimentSummary | null
}) {
  const minis = buildMinis(data, experiment)
  return (
    <div className="sparks">
      {minis.map((m) => (
        <MiniChart key={m.title} mini={m} />
      ))}
    </div>
  )
}
