/*
 * AuditionTab (spec §1 tab 3, §3.4) — distance-from-best / regret curves
 * (recharts) + model_group ranking for the active strategy, with a
 * `provisional · k/N splits` badge until the run completes. Empty-state until
 * ≥2 model_groups across ≥2 evaluated splits (§3.7).
 */
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { AuditionResponse } from '../api/types'
import { EmptyPanel } from './EmptyPanel'

const CURVE_COLORS = ['#3fb950', '#d29922', '#f85149', '#58a6ff', '#bc8cff']

export function AuditionTab({ data }: { data: AuditionResponse }) {
  if ('empty' in data && data.empty) {
    return <EmptyPanel reason={data.reason} hint={data.hint} />
  }

  // Build a wide table keyed by as_of_date for the multi-line chart.
  const dates = data.curves[0]?.points.map((p) => p.as_of_date) ?? []
  const chartData = dates.map((d, idx) => {
    const row: Record<string, number | string | null> = { as_of_date: d.slice(0, 7) }
    for (const c of data.curves) {
      row[c.label] = c.points[idx]?.distance_from_best ?? null
    }
    return row
  })

  return (
    <>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 8,
        }}
      >
        <span className={`badge ${data.provisional ? 'b-prov' : 'b-run'}`}>
          {data.provisional ? `provisional · ${data.k}/${data.n} splits` : `final · ${data.n} splits`}
        </span>
        <span className="src">triage.audition (distance-from-best / regret)</span>
      </div>

      <div className="twocol">
        <div>
          <h3 className="k">distance-from-best per model_group, across splits</h3>
          <div style={{ height: 150 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData} margin={{ top: 6, right: 10, bottom: 0, left: -18 }}>
                <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
                <XAxis dataKey="as_of_date" stroke="var(--mut)" tick={{ fontSize: 9 }} />
                <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} />
                <Tooltip
                  contentStyle={{
                    background: 'var(--panel)',
                    border: '1px solid var(--line)',
                    fontSize: 11,
                  }}
                />
                {data.curves.map((c, i) => (
                  <Line
                    key={c.model_group_id}
                    type="monotone"
                    dataKey={c.label}
                    stroke={CURVE_COLORS[i % CURVE_COLORS.length]}
                    strokeWidth={2}
                    dot={false}
                    isAnimationActive={false}
                    connectNulls
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="muted" style={{ fontSize: 10, marginTop: 6 }}>
            lower is better · curve extends as later splits land
          </div>
        </div>

        <div>
          <h3 className="k">strategy: {data.strategy}</h3>
          <table>
            <thead>
              <tr>
                <th>model_group</th>
                <th className="num">avg dist</th>
                <th className="num">max regret</th>
              </tr>
            </thead>
            <tbody>
              {data.ranking.map((r) => (
                <tr key={r.model_group_id}>
                  <td
                    className="mono"
                    style={r.is_pick ? { color: 'var(--acc2)' } : undefined}
                  >
                    {r.label}
                    {r.is_pick ? ' ◄ pick' : ''}
                  </td>
                  <td className="num">{r.avg_distance_from_best.toFixed(3)}</td>
                  <td className="num">{r.max_regret.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {data.provisional ? (
            <div className="provnote">
              ⚠ provisional — the pick can change as later splits land. Final at run completion.
            </div>
          ) : null}
        </div>
      </div>
    </>
  )
}
