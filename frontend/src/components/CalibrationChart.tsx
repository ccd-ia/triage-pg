/*
 * CalibrationChart — reliability deciles (migration 0012's monitoring_calibration,
 * finally surfaced on the model card — plan P4). Bars: mean predicted score per
 * decile; dots: the realized outcome rate. A well-calibrated model keeps the dots
 * on the bars; over-confidence shows dots sitting below.
 */
import {
  Bar,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { tooltipFormatter } from '../api/format'
import type { CalibrationDecile } from '../api/types'

export function CalibrationChart({ deciles }: { deciles: CalibrationDecile[] }) {
  const data = deciles.map((d) => ({
    decile: `d${d.decile}`,
    score: d.avg_score ?? 0,
    realized: d.realized_rate ?? 0,
    n: d.n,
  }))
  return (
    <ResponsiveContainer width="100%" height={140}>
      <ComposedChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -22 }}>
        <XAxis dataKey="decile" tick={{ fontSize: 9 }} />
        <YAxis domain={[0, 1]} tick={{ fontSize: 9 }} />
        <Tooltip formatter={tooltipFormatter(3)} />
        <Bar dataKey="score" fill="var(--accent-soft, #c7d2fe)" radius={[2, 2, 0, 0]} />
        <Line dataKey="realized" stroke="var(--accent, #4f46e5)" strokeWidth={2} dot={{ r: 2.5 }} />
      </ComposedChart>
    </ResponsiveContainer>
  )
}
