/*
 * ScoreDistributionChart — the model score histogram in the ModelSheet
 * (/models/{id}/histogram). Bars are total count per score bin, with the
 * positive-label share overlaid so the operator sees where the labelled
 * positives concentrate.
 */
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { HistogramBin } from '../api/types'
import { tooltipFormatter } from '../api/format'

export function ScoreDistributionChart({ bins }: { bins: HistogramBin[] }) {
  const data = bins.map((b) => ({
    bin: b.lo.toFixed(2),
    neg: b.n - b.n_pos,
    pos: b.n_pos,
  }))
  return (
    <div style={{ height: 130 }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: -20 }}>
          <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
          <XAxis dataKey="bin" stroke="var(--mut)" tick={{ fontSize: 9 }} />
          <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} />
          <Tooltip
            contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)', fontSize: 11 }}
            formatter={tooltipFormatter(4)}
          />
          <Bar dataKey="neg" stackId="s" fill="var(--mut)" isAnimationActive={false} />
          <Bar dataKey="pos" stackId="s" fill="var(--acc)" isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
