/*
 * ModelGroupSheet — a side-sheet for ONE model group (/model-groups/{id}), surfaced
 * from the Model Groups table (the "open group" affordance) and reused by the audition
 * compare view. Shows the group's identity (algorithm + hyperparameters + features),
 * its per-split models (each opens the model sheet), and its metric-over-time line.
 */
import { useMemo, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { tooltipFormatter } from '../api/format'
import { abbrevAlgo } from '../api/transforms'
import type { ExpEvaluationRow, MetricCatalogRow } from '../api/types'

const shortType = abbrevAlgo

function hyperText(h: Record<string, unknown> | null): string {
  if (!h) return '—'
  return Object.entries(h)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(' · ')
}

interface Props {
  groupId: number
  label?: string
  metric?: string
  parameter?: string
  /** Scope the group's models/evals to one experiment (a group can be shared across experiments). */
  experimentHash?: string
  /** Metric catalog so the panel can switch the metric-over-time series. */
  metrics?: MetricCatalogRow[]
  /** Open a specific model of this group in the model sheet. */
  onOpenModel?: (modelId: number, asOf?: string) => void
  onClose: () => void
}

export function ModelGroupSheet({
  groupId,
  label,
  metric,
  parameter,
  experimentHash,
  metrics,
  onOpenModel,
  onClose,
}: Props) {
  // The metric-over-time series is switchable (seeded from the experiment's current metric).
  const [sel, setSel] = useState({ metric: metric ?? 'auc_roc', parameter: parameter ?? '' })
  const group = useAsync(
    () => api.modelGroup(groupId, sel.metric, sel.parameter, experimentHash),
    [groupId, sel.metric, sel.parameter, experimentHash],
  )
  const s = group.data?.summary
  const models = group.data?.models ?? []
  // group aggregates (triage.audition, migration 0013) for the selected metric —
  // the avg ± σ band the trajectory is judged against (plan P6).
  const agg = (group.data?.audition ?? []).find(
    (a) => a.metric === sel.metric && a.parameter === sel.parameter,
  )

  // metric_over_time → recharts rows keyed by as_of (one value series).
  const chartData = useMemo(() => {
    const rows = (group.data?.metric_over_time ?? []) as ExpEvaluationRow[]
    return rows
      .filter((r) => r.value != null)
      .map((r) => ({ as_of_date: r.as_of_date.slice(0, 7), value: r.value as number }))
      .sort((a, b) => a.as_of_date.localeCompare(b.as_of_date))
  }, [group.data])

  const heading = label ?? (s ? `${shortType(s.model_type)} · g${groupId}` : `group ${groupId}`)

  return (
    <>
      <div className="sheet-backdrop stacked" onClick={onClose} />
      <aside className="sheet stacked" role="dialog" aria-label={`model group ${groupId}`}>
        <div className="sh">
          <div>
            <h3>{heading}</h3>
            <div className="sub mono">
              group {groupId}
              {s?.model_group_hash ? ` · ${s.model_group_hash.slice(0, 10)}` : ''}
            </div>
          </div>
          <button type="button" className="close" onClick={onClose} aria-label="close">×</button>
        </div>

        {group.loading ? (
          <div className="muted" style={{ fontSize: 11, padding: 8 }}>loading model group…</div>
        ) : group.error ? (
          <div className="banner err">
            {group.error.message.includes('404')
              ? `model group ${groupId} not found`
              : `Failed to load model group: ${group.error.message}`}
          </div>
        ) : s ? (
          <>
            <section>
              <h4>identity</h4>
              <div className="kv">
                <span className="k2">algorithm</span>
                <span className="v2">{shortType(s.model_type)}</span>
                <span className="k2">hyperparameters</span>
                <span className="v2 mono">{hyperText(s.hyperparameters)}</span>
                <span className="k2">features</span>
                <span className="v2">{s.feature_list?.length ?? '—'}</span>
                <span className="k2">models</span>
                <span className="v2">{s.n_models}</span>
                <span className="k2">train-end span</span>
                <span className="v2 mono">
                  {s.first_train_end?.slice(0, 7) ?? '—'} → {s.last_train_end?.slice(0, 7) ?? '—'}
                </span>
              </div>
              {agg ? (
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
                  <span className="badge b-run" title={`${agg.n_splits_evaluated} split(s)`}>
                    {sel.metric}
                    {sel.parameter} avg {agg.avg_value?.toFixed(3) ?? '—'} ±{' '}
                    {agg.stddev_value?.toFixed(3) ?? '—'}
                  </span>
                  <span className="badge b-aud" title="worst distance-from-best across splits">
                    max regret {agg.max_regret?.toFixed(3) ?? '—'}
                  </span>
                  {agg.max_regret_next_time != null ? (
                    <span className="badge b-aud" title="regret realized at the NEXT split (DSSG)">
                      next-time {agg.max_regret_next_time.toFixed(3)}
                    </span>
                  ) : null}
                </div>
              ) : null}
            </section>

            <section>
              <h4>models (per split)</h4>
              {models.length ? (
                <table>
                  <thead>
                    <tr><th>train ≤</th><th>test</th><th className="num">fit</th><th className="num">model</th></tr>
                  </thead>
                  <tbody>
                    {models.map((m) => (
                      <tr
                        key={m.model_id}
                        className={onOpenModel ? 'clickrow' : undefined}
                        onClick={onOpenModel ? () => onOpenModel(m.model_id, m.test_as_of ?? undefined) : undefined}
                      >
                        <td className="mono">{m.train_end_time ?? '—'}</td>
                        <td className="mono muted">{m.test_as_of ?? '—'}</td>
                        <td className="num mono muted">
                          {m.train_duration_ms != null ? `${(m.train_duration_ms / 1000).toFixed(1)}s` : '—'}
                        </td>
                        <td className="num mono">m{m.model_id}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <div className="muted" style={{ fontSize: 11 }}>no models in this group.</div>
              )}
            </section>

            <section>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <h4 style={{ margin: 0 }}>metric over time · {sel.metric}{sel.parameter}</h4>
                {metrics && metrics.length ? (
                  <select
                    className="splitsel"
                    style={{ width: 'auto' }}
                    value={`${sel.metric}|${sel.parameter}`}
                    onChange={(e) => {
                      const [m, p] = e.target.value.split('|')
                      setSel({ metric: m, parameter: p ?? '' })
                    }}
                  >
                    {metrics.map((mc) => (
                      <option key={`${mc.metric}|${mc.parameter}`} value={`${mc.metric}|${mc.parameter ?? ''}`}>
                        {mc.metric}{mc.parameter}
                      </option>
                    ))}
                  </select>
                ) : null}
              </div>
              {chartData.length ? (
                <div style={{ height: 160 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={chartData} margin={{ top: 6, right: 10, bottom: 0, left: -14 }}>
                      <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
                      <XAxis dataKey="as_of_date" stroke="var(--mut)" tick={{ fontSize: 9 }} minTickGap={20} />
                      <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} domain={[0, 1]} allowDataOverflow />
                      <Tooltip
                        contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)', fontSize: 11 }}
                        formatter={tooltipFormatter(4)}
                      />
                      {/* the group's own avg ± σ band — a split outside it is the outlier
                          the model sheet's z-chip flags (plan P6) */}
                      {agg?.avg_value != null && agg?.stddev_value != null ? (
                        <ReferenceArea
                          y1={agg.avg_value - agg.stddev_value}
                          y2={agg.avg_value + agg.stddev_value}
                          fill="var(--acc)"
                          fillOpacity={0.08}
                        />
                      ) : null}
                      {agg?.avg_value != null ? (
                        <ReferenceLine
                          y={agg.avg_value}
                          stroke="var(--acc)"
                          strokeDasharray="4 4"
                          strokeOpacity={0.6}
                        />
                      ) : null}
                      <Line type="monotone" dataKey="value" stroke="var(--acc)" strokeWidth={2} dot={{ r: 2.5 }} isAnimationActive={false} connectNulls />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <div className="muted" style={{ fontSize: 11 }}>no evaluations yet for this metric.</div>
              )}
            </section>
          </>
        ) : (
          <div className="muted" style={{ fontSize: 11, padding: 8 }}>model group not found.</div>
        )}
      </aside>
    </>
  )
}
