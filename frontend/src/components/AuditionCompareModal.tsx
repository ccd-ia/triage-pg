/*
 * AuditionCompareModal — opened from the "leaderboard #1 ≠ audition pick" divergence
 * warning. Fetches both model groups (/model-groups/{id}) and lays them side by side so
 * the operator can see *why* the audition rule and the current leaderboard disagree:
 * algorithm, hyperparameters, model count, train-end span, and the latest metric value.
 */
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { fmtNum, tooltipFormatter } from '../api/format'
import { abbrevAlgo } from '../api/transforms'
import type { ModelGroupDetailResponse } from '../api/types'

const shortType = abbrevAlgo
function hyperText(h: Record<string, unknown> | null | undefined): string {
  if (!h) return '—'
  return Object.entries(h)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(' · ')
}
function latestValue(g: ModelGroupDetailResponse | undefined): number | null {
  const rows = (g?.metric_over_time ?? []).filter((r) => r.value != null)
  if (!rows.length) return null
  const last = [...rows].sort((a, b) => a.as_of_date.localeCompare(b.as_of_date)).at(-1)
  return last?.value ?? null
}

function CompareSide({
  gid,
  data,
  pickName,
  name,
  metricCol,
  onOpenGroup,
}: {
  gid: number
  data: ModelGroupDetailResponse | undefined
  pickName: string
  name: string
  metricCol: string
  onOpenGroup?: (gid: number) => void
}) {
  const s = data?.summary
  const chart = (data?.metric_over_time ?? [])
    .filter((r) => r.value != null)
    .map((r) => ({ as_of_date: r.as_of_date.slice(0, 7), value: r.value as number }))
    .sort((x, y) => x.as_of_date.localeCompare(y.as_of_date))
  return (
    <div className="cmp-col">
      <div className="cmp-head">
        <span className="cmp-tag">{pickName}</span>
        <button type="button" className="cmp-name" onClick={onOpenGroup ? () => onOpenGroup(gid) : undefined} disabled={!onOpenGroup}>
          {name} <span className="mono muted">g{gid}</span>
        </button>
      </div>
      <div className="kv">
        <span className="k2">algorithm</span>
        <span className="v2">{shortType(s?.model_type)}</span>
        <span className="k2">hyperparameters</span>
        <span className="v2 mono">{hyperText(s?.hyperparameters)}</span>
        <span className="k2">models</span>
        <span className="v2">{s?.n_models ?? '—'}</span>
        <span className="k2">train-end span</span>
        <span className="v2 mono">{s?.first_train_end?.slice(0, 7) ?? '—'} → {s?.last_train_end?.slice(0, 7) ?? '—'}</span>
        <span className="k2">latest {metricCol}</span>
        <span className="v2"><b>{fmtNum(latestValue(data), 4)}</b></span>
      </div>
      {chart.length ? (
        <div style={{ height: 120, marginTop: 6 }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chart} margin={{ top: 6, right: 8, bottom: 0, left: -16 }}>
              <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
              <XAxis dataKey="as_of_date" stroke="var(--mut)" tick={{ fontSize: 9 }} minTickGap={20} />
              <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} domain={[0, 1]} allowDataOverflow />
              <Tooltip contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)', fontSize: 11 }} formatter={tooltipFormatter(4)} />
              <Line type="monotone" dataKey="value" stroke="var(--acc)" strokeWidth={2} dot={false} isAnimationActive={false} connectNulls />
            </LineChart>
          </ResponsiveContainer>
        </div>
      ) : null}
    </div>
  )
}

interface Props {
  auditionGroup: number
  leaderboardGroup: number
  metric: string
  parameter: string
  rule: string
  experimentHash?: string
  groupLabelOf?: (gid: number) => string
  onOpenGroup?: (gid: number) => void
  onClose: () => void
}

export function AuditionCompareModal({
  auditionGroup,
  leaderboardGroup,
  metric,
  parameter,
  rule,
  experimentHash,
  groupLabelOf,
  onOpenGroup,
  onClose,
}: Props) {
  const a = useAsync(
    () => api.modelGroup(auditionGroup, metric, parameter, experimentHash),
    [auditionGroup, metric, parameter, experimentHash],
  )
  const b = useAsync(
    () => api.modelGroup(leaderboardGroup, metric, parameter, experimentHash),
    [leaderboardGroup, metric, parameter, experimentHash],
  )
  const label = (gid: number) => (groupLabelOf ? groupLabelOf(gid) : `group ${gid}`)
  const metricCol = `${metric}${parameter ?? ''}`
  const same = auditionGroup === leaderboardGroup

  return (
    <>
      <div className="sheet-backdrop stacked" onClick={onClose} />
      <div className="modal" role="dialog" aria-label="audition vs leaderboard">
        <div className="sh">
          <div>
            <h3>audition pick vs leaderboard #1</h3>
            <div className="sub mono">
              {metricCol} · rule {rule}
              {same ? ' · (they agree)' : ' · they diverge'}
            </div>
          </div>
          <button type="button" className="close" onClick={onClose} aria-label="close">×</button>
        </div>
        <div className="modal-body">
          <div className="cmp-grid">
            <CompareSide
              gid={auditionGroup}
              data={a.data}
              pickName={`audition (${rule})`}
              name={label(auditionGroup)}
              metricCol={metricCol}
              onOpenGroup={onOpenGroup}
            />
            <CompareSide
              gid={leaderboardGroup}
              data={b.data}
              pickName="leaderboard #1 (best current)"
              name={label(leaderboardGroup)}
              metricCol={metricCol}
              onOpenGroup={onOpenGroup}
            />
          </div>
          {a.loading || b.loading ? <div className="muted" style={{ fontSize: 11, padding: 8 }}>loading groups…</div> : null}
          <div className="muted" style={{ fontSize: 10.5, marginTop: 8 }}>
            The audition pick optimizes the selection rule over <i>all</i> splits; leaderboard #1 is just the best value at the
            latest split. Divergence means the most-recent winner is not the most-robust choice.
          </div>
        </div>
      </div>
    </>
  )
}
