/*
 * ScoreDistributionChart — the model score histogram in the ModelSheet
 * (/models/{id}/histogram). Bars are the count per score bin. For a classifier the
 * positives are stacked on the other rows, so the operator sees where the labelled
 * positives concentrate; for the other problem types `n_pos` is null and one series is
 * drawn over the model's own score range.
 */
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { HistogramBin } from '../api/types'
import { fmtNum, tooltipFormatter } from '../api/format'

export function ScoreDistributionChart({ bins }: { bins: HistogramBin[] }) {
  const byClass = bins.some((b) => b.n_pos != null)
  const data = bins.map((b) => ({
    bin: fmtNum(Number(b.lo.toPrecision(3)), 2),
    ...(byClass ? { neg: b.n - (b.n_pos ?? 0), pos: b.n_pos ?? 0 } : { n: b.n }),
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
          {byClass ? (
            <>
              <Bar dataKey="neg" stackId="s" fill="var(--mut)" isAnimationActive={false} />
              <Bar dataKey="pos" stackId="s" fill="var(--acc)" isAnimationActive={false} />
            </>
          ) : (
            <Bar dataKey="n" fill="var(--acc)" isAnimationActive={false} />
          )}
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
