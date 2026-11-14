/*
 * ExperimentAuditionTab (Option 2 audition view) — the experiment-scoped
 * audition: aggregates ALL runs of the experiment (the rework's locked scope).
 * Shows:
 *   - metric + k selectors (from /metrics)
 *   - metric-over-time per model group + a "best at each point" line
 *   - a regret bar (max_regret per group, lower better)
 *   - the all-8-strategy table with the audition-picked winner highlighted
 *   - when the groups scored different shares of the labeled rows, a "common rows" toggle:
 *     it registers the rows every group scored as a Subset and evaluates the groups on it
 *     (POST /experiments/{hash}/common-rows, a write), then shows the audition of that
 *     Subset (?subset_hash=) and each group's MAE on its own rows against the common ones
 *     (GET /experiments/{hash}/common-rows). What `triage audition --common-rows` does.
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
import { useState } from 'react'
import { ApiError, api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { CommonRowsRow, CommonRowsSubset, ExpAuditionResponse, MetricCatalogRow } from '../api/types'
import { coverageDiffers, isEmpty } from '../api/types'
import { expAuditionChart, groupLabel } from '../api/transforms'
import { fmtNum, fmtPct, tooltipFormatter } from '../api/format'
import { EmptyPanel } from './EmptyPanel'

const COLORS = ['#3fb950', '#d29922', '#f85149', '#58a6ff', '#bc8cff', '#56d4dd']

interface Props {
  /** The experiment, for the common-rows routes. */
  hash: string
  data: ExpAuditionResponse
  metrics: MetricCatalogRow[]
  metric: string
  parameter: string
  rule: string
  onMetric: (metric: string, parameter: string) => void
  onRule: (rule: string) => void
}

/** Each group's MAE on its own rows and on the common rows, on the latest test date. */
function CommonRowsTable({ rows }: { rows: CommonRowsRow[] }) {
  const last = rows.reduce((d, r) => (r.as_of_date > d ? r.as_of_date : d), rows[0].as_of_date)
  return (
    <table style={{ marginTop: 6 }}>
      <thead>
        <tr>
          <th>model group</th>
          <th className="num">labeled</th>
          <th className="num">scored</th>
          <th className="num">common</th>
          <th className="num">MAE own rows</th>
          <th className="num">MAE common rows</th>
          <th className="num">WAPE common</th>
        </tr>
      </thead>
      <tbody>
        {rows
          .filter((r) => r.as_of_date === last)
          .map((r) => (
            <tr key={r.model_group_id}>
              <td>{groupLabel(r.model_group_id)}</td>
              <td className="num">{r.n_labeled}</td>
              <td className="num">{r.n_scored_own}</td>
              <td className="num">{r.n_common}</td>
              <td className="num">{fmtNum(r.mae_own, 3)}</td>
              <td className="num">{fmtNum(r.mae_common, 3)}</td>
              <td className="num">{fmtPct(r.wape_common)}</td>
            </tr>
          ))}
      </tbody>
      <caption className="muted" style={{ captionSide: 'bottom', fontSize: 10, textAlign: 'left' }}>
        at {last}
      </caption>
    </table>
  )
}

export function ExperimentAuditionTab({
  hash,
  data: ownRows,
  metrics,
  metric,
  parameter,
  rule,
  onMetric,
  onRule,
}: Props) {
  // The common-rows Subset, once the write route registered it; the toggle shows its
  // audition in place of each group's own rows.
  const [subset, setSubset] = useState<CommonRowsSubset | null>(null)
  const [commonOn, setCommonOn] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const onCommon = commonOn && subset != null
  const common = useAsync(
    () =>
      onCommon
        ? api.expAudition(hash, metric, parameter, rule, subset.subset_hash)
        : Promise.resolve(undefined),
    [hash, metric, parameter, rule, onCommon, subset?.subset_hash],
  )
  const commonRows = useAsync(
    () => (onCommon ? api.expCommonRows(hash) : Promise.resolve(undefined)),
    [hash, onCommon],
  )

  const toggleCommon = async () => {
    if (commonOn) {
      setCommonOn(false)
      return
    }
    setBusy(true)
    setFailure(null)
    try {
      // POST refills the Subset of the same groups, so a second toggle stays current
      setSubset(await api.materializeCommonRows(hash))
      setCommonOn(true)
    } catch (e) {
      setFailure(e instanceof ApiError ? e.detail : String(e))
    } finally {
      setBusy(false)
    }
  }

  const metricValue = `${metric}|${parameter}`
  // the metric catalog is the project's: an experiment of another problem type may have no
  // row on the selected metric, and the selector must stay reachable from that empty state
  const metricSelect = (
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
  )

  if (isEmpty(ownRows)) {
    return (
      <>
        <div className="selectors" style={{ marginBottom: 10 }}>
          {metricSelect}
        </div>
        <EmptyPanel reason={ownRows.reason} hint={ownRows.hint} />
      </>
    )
  }
  const data = onCommon && common.data && !isEmpty(common.data) ? common.data : ownRows
  const differs = coverageDiffers(ownRows.ranking.map((r) => r.coverage))

  const { rows, groups } = expAuditionChart(data.curves)
  const nText = data.n == null ? '?' : `${data.n}`

  // regret bars
  const regret = [...data.ranking]
    .sort((a, b) => a.max_regret - b.max_regret)
    .map((r) => ({ group: groupLabel(r.model_group_id), regret: r.max_regret }))

  return (
    <>
      <div className="selectors" style={{ marginBottom: 10 }}>
        {metricSelect}
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
          {differs || commonOn ? (
            <div className="muted" style={{ fontSize: 10, marginTop: 4 }}>
              the groups scored different shares of the labeled rows (
              {ownRows.ranking
                .map((r) => `${groupLabel(r.model_group_id)} ${fmtPct(r.coverage, 0)}`)
                .join(', ')}
              ),{' '}
              {onCommon
                ? `so this audition compares them on the ${subset.n_rows} rows all of them scored (${subset.n_dates} test dates)`
                : 'so each is evaluated on its own rows'}{' '}
              <button
                type="button"
                className={`commonrows${onCommon ? ' active' : ''}`}
                aria-pressed={onCommon}
                disabled={busy}
                title="register the rows every group scored as a Subset, evaluate the groups on it, and select on it (writes evaluations)"
                onClick={() => void toggleCommon()}
              >
                {busy ? 'evaluating on common rows…' : onCommon ? 'common rows · on' : 'common rows'}
              </button>
              {failure ? (
                <div className="banner err" style={{ marginTop: 4 }}>
                  {failure} — the CLI does the same: `triage audition {hash.slice(0, 12)} --common-rows`
                </div>
              ) : null}
              {onCommon && common.data && isEmpty(common.data) ? (
                <div className="muted">{common.data.reason}</div>
              ) : null}
            </div>
          ) : null}
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
        {parameter}{onCommon ? ' · on common rows' : ''})
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

      {onCommon ? (
        <>
          <h3 className="k" style={{ marginTop: 14 }}>own rows against common rows</h3>
          {commonRows.error ? (
            <div className="muted" style={{ fontSize: 11 }}>
              common rows unavailable: {commonRows.error.message}
            </div>
          ) : !commonRows.data ? (
            <div className="muted" style={{ fontSize: 11 }}>loading the common rows…</div>
          ) : isEmpty(commonRows.data) ? (
            <EmptyPanel reason={commonRows.data.reason} hint={commonRows.data.hint} />
          ) : (
            <CommonRowsTable rows={commonRows.data} />
          )}
        </>
      ) : null}
    </>
  )
}
