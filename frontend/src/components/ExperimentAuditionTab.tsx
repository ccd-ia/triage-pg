/*
 * ExperimentAuditionTab (Option 2 audition view) — the experiment-scoped
 * audition: aggregates ALL runs of the experiment (the rework's locked scope).
 * Shows:
 *   - metric + k selectors (from /metrics)
 *   - metric-over-time per model group + a "best at each point" line
 *   - a regret bar (max_regret per group, lower better)
 *   - the all-8-strategy table with the audition-picked winner highlighted
 * Data: /experiments/{hash}/audition (ranking + curves + strategies + pick).
 */
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { ExpAuditionResponse, MetricCatalogRow } from '../api/types'
import { isEmpty } from '../api/types'
import { expAuditionChart, groupLabel } from '../api/transforms'
import { tooltipFormatter } from '../api/format'
import { EmptyPanel } from './EmptyPanel'

const COLORS = ['#3fb950', '#d29922', '#f85149', '#58a6ff', '#bc8cff', '#56d4dd']

interface Props {
  data: ExpAuditionResponse
  metrics: MetricCatalogRow[]
  metric: string
  parameter: string
  rule: string
  onMetric: (metric: string, parameter: string) => void
  onRule: (rule: string) => void
}

export function ExperimentAuditionTab({
  data,
  metrics,
  metric,
  parameter,
  rule,
  onMetric,
  onRule,
}: Props) {
  if (isEmpty(data)) {
    return <EmptyPanel reason={data.reason} hint={data.hint} />
  }

  const { rows, groups } = expAuditionChart(data.curves)
  const nText = data.n == null ? '?' : `${data.n}`

  // regret bars
  const regret = [...data.ranking]
    .sort((a, b) => a.max_regret - b.max_regret)
    .map((r) => ({ group: groupLabel(r.model_group_id), regret: r.max_regret }))

  const metricValue = `${metric}|${parameter}`

  return (
    <>
      <div className="selectors" style={{ marginBottom: 10 }}>
        <label>
          metric
          <select
            value={metricValue}
            onChange={(e) => {
              const [m, p] = e.target.value.split('|')
              onMetric(m, p ?? '')
            }}
          >
            {metrics.map((m) => (
              <option key={`${m.metric}|${m.parameter}`} value={`${m.metric}|${m.parameter}`}>
                {m.metric}
                {m.parameter}
              </option>
            ))}
          </select>
        </label>
        <label>
          rule
          <select value={rule} onChange={(e) => onRule(e.target.value)}>
            {data.strategies.map((s) => (
              <option key={s.rule} value={s.rule}>
                {s.rule}
              </option>
            ))}
          </select>
        </label>
        <span className={`badge ${data.provisional ? 'b-prov' : 'b-run'}`}>
          {data.provisional ? `provisional · ${data.k}/${nText} splits` : `final · ${nText} splits`}
        </span>
        <span className="src">aggregates all runs · shared models</span>
      </div>

      <div className="twocol">
        <div>
          <h3 className="k">distance-from-best per model group, over time · best at each point</h3>
          <div style={{ height: 160 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={rows} margin={{ top: 6, right: 10, bottom: 0, left: -18 }}>
                <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
                <XAxis dataKey="as_of_date" stroke="var(--mut)" tick={{ fontSize: 9 }} />
                <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} />
                <Tooltip
                  contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)', fontSize: 11 }}
                  formatter={tooltipFormatter(4)}
                />
                {groups.map((g, i) => (
                  <Line
                    key={g.id}
                    type="monotone"
                    dataKey={g.label}
                    stroke={COLORS[i % COLORS.length]}
                    strokeWidth={data.pick === g.id ? 3 : 1.5}
                    dot={false}
                    connectNulls
                    isAnimationActive={false}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="muted" style={{ fontSize: 10, marginTop: 6 }}>
            lower is better · thick line = audition pick for rule “{rule}”
          </div>
        </div>

        <div>
          <h3 className="k">regret by selection strategy (lower better)</h3>
          <div style={{ height: 160 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={regret} layout="vertical" margin={{ top: 4, right: 14, bottom: 0, left: 30 }}>
                <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" horizontal={false} />
                <XAxis type="number" stroke="var(--mut)" tick={{ fontSize: 9 }} />
                <YAxis type="category" dataKey="group" stroke="var(--mut)" tick={{ fontSize: 9 }} width={70} />
                <Tooltip
                  contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)', fontSize: 11 }}
                  formatter={tooltipFormatter(4)}
                />
                <Bar dataKey="regret" fill="var(--acc)" isAnimationActive={false} radius={[0, 3, 3, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      <h3 className="k" style={{ marginTop: 14 }}>
        all 8 strategies · winner picked by audition (metric: {metric}
        {parameter})
      </h3>
      <table>
        <thead>
          <tr>
            <th>selection strategy</th>
            <th>recommended model group</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {data.strategies.map((s) => {
            const isActive = s.rule === rule
            return (
              <tr key={s.rule} className={isActive ? 'winner' : undefined}>
                <td className="mono">{s.rule}</td>
                <td>{s.model_group_id == null ? '—' : groupLabel(s.model_group_id)}</td>
                <td>{isActive ? <span className="tag-win">active rule</span> : null}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </>
  )
}
