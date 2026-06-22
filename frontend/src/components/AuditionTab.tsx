/*
 * AuditionTab (spec §1 tab 3, §3.4) — distance-from-best / regret curves
 * (recharts) + model_group ranking for the active rule, with a
 * `provisional · k/N splits` badge until the run completes. Empty-state until
 * ≥2 model_groups across ≥2 evaluated splits (§3.7).
 *
 * Reconciled to routes.py: the query param / field is `rule` (not `strategy`);
 * ranking/curve rows carry only ids (no human labels) and the pick is the
 * top-level `pick` (a model_group_id), not per-row `is_pick`; the curve value is
 * `dist_from_best_case`. `n` (planned splits) may be null.
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
import { isEmpty } from '../api/types'
import { groupLabel, isAuditionPick } from '../api/transforms'
import { EmptyPanel } from './EmptyPanel'

const CURVE_COLORS = ['#3fb950', '#d29922', '#f85149', '#58a6ff', '#bc8cff']

export function AuditionTab({ data }: { data: AuditionResponse }) {
  if (isEmpty(data)) {
    return <EmptyPanel reason={data.reason} hint={data.hint} />
  }

  // Group curve rows by model_group, then build a wide table keyed by as_of_date.
  const byGroup = new Map<number, { label: string; points: Map<string, number | null> }>()
  const allDates = new Set<string>()
  for (const c of data.curves) {
    allDates.add(c.as_of_date)
    let g = byGroup.get(c.model_group_id)
    if (!g) {
      g = { label: groupLabel(c.model_group_id), points: new Map() }
      byGroup.set(c.model_group_id, g)
    }
    g.points.set(c.as_of_date, c.dist_from_best_case)
  }
  const dates = [...allDates].sort()
  const groups = [...byGroup.entries()].map(([id, g]) => ({ id, label: g.label, points: g.points }))
  const chartData = dates.map((d) => {
    const row: Record<string, number | string | null> = { as_of_date: d.slice(0, 7) }
    for (const g of groups) {
      row[g.label] = g.points.get(d) ?? null
    }
    return row
  })

  const nText = data.n == null ? '?' : `${data.n}`

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
          {data.provisional ? `provisional · ${data.k}/${nText} splits` : `final · ${nText} splits`}
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
                {groups.map((g, i) => (
                  <Line
                    key={g.id}
                    type="monotone"
                    dataKey={g.label}
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
          <h3 className="k">rule: {data.rule}</h3>
          <table>
            <thead>
              <tr>
                <th>model_group</th>
                <th className="num">avg dist</th>
                <th className="num">max regret</th>
              </tr>
            </thead>
            <tbody>
              {data.ranking.map((r) => {
                const pick = isAuditionPick(data, r.model_group_id)
                return (
                  <tr key={r.model_group_id}>
                    <td className="mono" style={pick ? { color: 'var(--acc2)' } : undefined}>
                      {groupLabel(r.model_group_id)}
                      {pick ? ' ◄ pick' : ''}
                    </td>
                    <td className="num">{r.avg_distance_from_best.toFixed(3)}</td>
                    <td className="num">{r.max_regret.toFixed(2)}</td>
                  </tr>
                )
              })}
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
