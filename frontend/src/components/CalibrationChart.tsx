/*
 * CalibrationChart — reliability deciles on the model card (migration 0029's
 * monitoring_calibration). Bars: mean predicted score per decile; dots: what the
 * decile's rows realized — the positive rate for a classifier, the mean outcome for
 * the other problem types. A well-calibrated model keeps the dots on the bars.
 * Tied scores share a decile, so a model that gives most rows one score draws fewer
 * than ten bars.
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
import type { CalibrationDecile, ProblemType } from '../api/types'

export function CalibrationChart({
  deciles,
  problemType,
}: {
  deciles: CalibrationDecile[]
  problemType: ProblemType
}) {
  const classifier = problemType === 'classification'
  const data = deciles.map((d) => ({
    decile: `d${d.decile}`,
    score: d.avg_score ?? 0,
    [classifier ? 'realized' : 'outcome']: (classifier ? d.realized_rate : d.mean_outcome) ?? 0,
    n: d.n,
  }))
  return (
    <ResponsiveContainer width="100%" height={140}>
      <ComposedChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -22 }}>
        <XAxis dataKey="decile" tick={{ fontSize: 9 }} />
        <YAxis domain={classifier ? [0, 1] : [0, 'auto']} tick={{ fontSize: 9 }} />
        <Tooltip formatter={tooltipFormatter(3)} />
        <Bar dataKey="score" fill="var(--accent-soft, #c7d2fe)" radius={[2, 2, 0, 0]} />
        <Line
          dataKey={classifier ? 'realized' : 'outcome'}
          stroke="var(--accent, #4f46e5)"
          strokeWidth={2}
          dot={{ r: 2.5 }}
        />
      </ComposedChart>
    </ResponsiveContainer>
  )
}
