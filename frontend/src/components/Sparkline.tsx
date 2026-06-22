/*
 * Tiny per-split sparkline (recharts) for the experiment-summary card —
 * cohort-size-per-split and base-rate-per-split (spec §3.2, the stability signal).
 */
import { Bar, BarChart, ResponsiveContainer } from 'recharts'

interface Props {
  values: number[]
  warm?: boolean
  height?: number
}

export function Sparkline({ values, warm, height = 34 }: Props) {
  const data = values.map((v, i) => ({ i, v }))
  const color = warm ? 'var(--warn)' : 'var(--acc)'
  return (
    <div style={{ width: values.length * 12 + 8, height }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} barCategoryGap={2}>
          <Bar dataKey="v" fill={color} radius={[1, 1, 0, 0]} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
