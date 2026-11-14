/*
 * GainsChart — the cumulative-gains curve of a regression_ranking model
 * (/models/{id}/gains, migration 0035): the share of the date's outcome total that the
 * top k of the ranking hold, against k as a share of the scored rows. The diagonal is a
 * random list; the area between them is what the stored AUCC summarizes. Shares are
 * undefined (null) on a date whose labeled outcome is negative somewhere or sums to 0.
 *
 * DecileLiftTable — the score deciles with their mean outcome and its lift over the
 * population's, from the calibration route the sheet already reads (migration 0029):
 * tied scores share a decile, so a block of ties draws fewer than ten rows (the
 * calibration note under the chart names the largest block).
 */
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { fmtNum, tooltipFormatter } from '../api/format'
import type { CalibrationDecile } from '../api/types'
import { AsyncPanel } from './AsyncPanel'

export function GainsChart({ modelId }: { modelId: number }) {
  const gains = useAsync(() => api.modelGains(modelId), [modelId])
  return (
    <AsyncPanel state={gains} what="the gains curve">
      {(data) => {
        const rows = data.rows.map((r) => ({
          k: Number((r.k_pct * 100).toFixed(1)),
          captured: r.captured,
          random: r.k_pct,
          lift: r.lift,
          rows: r.k,
        }))
        const undefinedShares = data.rows.every((r) => r.captured == null)
        return (
          <>
            {undefinedShares ? (
              <div className="banner">
                no share is defined on {data.as_of_date}: the labeled outcome is negative
                somewhere or sums to 0
              </div>
            ) : (
              <div style={{ height: 150 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={rows} margin={{ top: 6, right: 10, bottom: 0, left: -18 }}>
                    <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
                    <XAxis dataKey="k" stroke="var(--mut)" tick={{ fontSize: 9 }} unit="%" />
                    <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} domain={[0, 1]} />
                    <Tooltip
                      contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)', fontSize: 11 }}
                      formatter={tooltipFormatter(3)}
                      labelFormatter={(k) => `top ${k}% of the scored rows`}
                    />
                    <Line dataKey="random" stroke="var(--mut)" strokeDasharray="4 3" dot={false} isAnimationActive={false} />
                    <Line dataKey="captured" stroke="var(--acc)" strokeWidth={2} dot={false} isAnimationActive={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
            <div className="muted" style={{ fontSize: 10.5 }}>
              at {data.as_of_date} · share of the outcome total captured by the top k; dashed:
              a random list · AUCC {fmtNum(data.aucc, 3)}
            </div>
          </>
        )
      }}
    </AsyncPanel>
  )
}

export function DecileLiftTable({ deciles }: { deciles: CalibrationDecile[] }) {
  return (
    <table className="compact">
      <thead>
        <tr>
          <th>decile</th>
          <th className="num">rows</th>
          <th className="num">mean score</th>
          <th className="num">mean outcome</th>
          <th className="num" title="the decile's mean outcome over the population's">lift</th>
        </tr>
      </thead>
      <tbody>
        {deciles.map((d) => (
          <tr key={d.decile}>
            <td className="mono">d{d.decile}</td>
            <td className="num">{d.n}</td>
            <td className="num">{fmtNum(d.avg_score, 3)}</td>
            <td className="num">{fmtNum(d.mean_outcome, 3)}</td>
            <td className="num">{d.lift == null ? '—' : `${fmtNum(d.lift, 2)}×`}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
