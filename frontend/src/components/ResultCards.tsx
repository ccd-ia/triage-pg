/*
 * ResultCards (spec §1, §6) — the result-card grid:
 *  - ExperimentSummaryCard: §3.1 scalars + §3.2 per-split sparklines
 *  - LeaderboardCard: triage.leaderboard (bare rows → ranked client-side)
 *  - MetricOverTimeCard: triage.evaluations (flat rows → overlay series)
 *  - TopPredictionsCard: prediction_ranks (bare rows | empty envelope)
 *  - SourcePinsCard: run pins ⋈ registry head (drift derived client-side)
 *
 * Reconciled to routes.py: the API returns RAW view rows; the reshaping lives in
 * api/transforms.ts, so these cards consume the real response shapes.
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
import type {
  EvaluationsResponse,
  LeaderboardResponse,
  PredictionsResponse,
  SourcePinsResponse,
  SummaryResponse,
} from '../api/types'
import { isEmpty } from '../api/types'
import {
  buildMetricSeries,
  deriveSourcePins,
  rankLeaderboard,
  leaderboardMetricKeys,
  type LeaderboardEntry,
} from '../api/transforms'
import { Sparkline } from './Sparkline'
import { EmptyPanel } from './EmptyPanel'

/* ----------------------------- helpers ----------------------------------- */

function fmt3(x: number | undefined): string {
  return x == null ? '—' : x.toFixed(3)
}
function fmt2(x: number | undefined): string {
  return x == null ? '—' : x.toFixed(2)
}

const SERIES_COLORS = ['#58a6ff', '#bc8cff', '#3fb950', '#d29922', '#f85149']

/* --------------------------- experiment summary -------------------------- */

export function ExperimentSummaryCard({ data }: { data: SummaryResponse }) {
  const s = data.summary
  const cohortVals = data.cohort_profile.map((p) => p.n_entities)
  const baseVals = data.label_base_rate.map((p) => p.base_rate ?? 0)
  const cohortName = s.experiment_config?.cohort_name ?? null
  const labelName = s.experiment_config?.label_name ?? null
  const nSplits = s.plan?.n_splits ?? null
  const labelTimespan = s.plan?.label_timespan ?? data.label_base_rate[0]?.label_timespan ?? ''
  const history = s.plan?.history ?? ''
  const temporal =
    nSplits != null
      ? `${nSplits} splits${labelTimespan ? ` · ${labelTimespan}` : ''}${history ? ` · ${history}` : ''}`
      : '—'
  return (
    <div className="card full">
      <div className="ch">
        <b>Experiment summary</b>
        <span className="src">run_summary · cohort_profile · label_base_rate · source pins</span>
      </div>
      <div className="twocol">
        <div className="kv">
          <span className="k2">problem_type</span>
          <span className="v2">{s.problem_type ?? '—'}</span>
          <span className="k2">cohort</span>
          <span className="v2">{cohortName ?? '—'}</span>
          <span className="k2">label</span>
          <span className="v2">{labelName ?? '—'}</span>
          <span className="k2">temporal</span>
          <span className="v2">{temporal}</span>
          <span className="k2">features</span>
          <span className="v2">{s.n_features ?? '—'}</span>
          <span className="k2">grid</span>
          <span className="v2">
            {s.n_models ?? '—'} models · {(s.estimator_types ?? []).join(',')}
          </span>
        </div>
        <div className="kv">
          <span className="k2">random_seed</span>
          <span className="v2">{s.random_seed ?? '—'}</span>
          <span className="k2">featurizer</span>
          <span className="v2">{s.engine_versions?.featurizer ?? '—'}</span>
          <span className="k2">config hash</span>
          <span className="v2 mono">{s.experiment_hash ?? '—'}</span>
          <span className="k2">profile</span>
          <span className="v2">{s.profile ?? '—'}</span>
          <span className="k2">git hash</span>
          <span className="v2 mono">{s.git_hash ?? '—'}</span>
          <span className="k2">started</span>
          <span className="v2">
            {new Date(s.started_at).toLocaleString('en-US')}
            {s.duration ? ` · ${s.duration}` : ''}
          </span>
        </div>
      </div>
      <div className="splitrow">
        <div className="sp">
          <span className="lbl">
            cohort size / split <span className="muted">(cohort_profile)</span>
          </span>
          <Sparkline values={cohortVals} />
        </div>
        <div className="sp">
          <span className="lbl">
            base rate / split <span className="muted">(label_base_rate · stability)</span>
          </span>
          <Sparkline values={baseVals} warm />
        </div>
      </div>
    </div>
  )
}

/* ------------------------------- leaderboard ----------------------------- */

/**
 * The leaderboard ranks by the first metric present, preferring a precision@
 * style metric when available (the audition default is auc_roc, but the headline
 * leaderboard metric is usually precision@k). The column set is whatever the
 * matview actually returned for this run.
 */
function pickRankMetric(keys: string[]): string | undefined {
  if (keys.length === 0) return undefined
  const pref = keys.find((k) => k.startsWith('precision@')) ?? keys.find((k) => k.includes('precision'))
  return pref ?? keys[0]
}

export function LeaderboardCard({
  data,
  onPick,
  selectedModelId,
  auditionPickGroup,
}: {
  data: LeaderboardResponse
  onPick: (modelId: number) => void
  selectedModelId: number | undefined
  /** model_group_id of the audition pick (from /selected-model), for the badge. */
  auditionPickGroup: number | null
}) {
  // First pass to discover the metric columns, then rank by the chosen metric.
  const probe = rankLeaderboard(data)
  const metricKeys = leaderboardMetricKeys(probe)
  const rankBy = pickRankMetric(metricKeys)
  const entries: LeaderboardEntry[] = rankLeaderboard(data, rankBy)

  if (entries.length === 0) {
    return (
      <div className="card">
        <div className="ch">
          <b>Leaderboard</b>
          <span className="src">triage.leaderboard</span>
        </div>
        <EmptyPanel
          reason="leaderboard not available yet"
          hint="the leaderboard matview is empty until it is REFRESHed after evaluations land."
        />
      </div>
    )
  }

  return (
    <div className="card">
      <div className="ch">
        <b>Leaderboard</b>
        <span className="src">triage.leaderboard</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>model</th>
            {metricKeys.map((k) => (
              <th key={k} className="num">
                {k}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {entries.map((r) => {
            const isPick = auditionPickGroup != null && r.model_group_id === auditionPickGroup
            return (
              <tr
                key={r.model_id}
                className="clickrow"
                onClick={() => onPick(r.model_id)}
                style={r.model_id === selectedModelId ? { background: '#0d1b2a' } : undefined}
              >
                <td className="mono">
                  {r.label}
                  {isPick ? <span className="b-aud badge"> audition</span> : null}
                  {r.rank === 1 && rankBy ? (
                    <span style={{ color: 'var(--acc)' }}> ← #1 by {rankBy}</span>
                  ) : null}
                </td>
                {metricKeys.map((k) => {
                  const v = r.metrics[k]
                  const wide = k.includes('auc') || k.includes('ap') || k.endsWith('roc')
                  return (
                    <td key={k} className="num">
                      {wide ? fmt3(v) : fmt2(v)}
                    </td>
                  )
                })}
              </tr>
            )
          })}
        </tbody>
      </table>
      <div className="drillhint" style={{ marginTop: 7 }}>
        ▸ click a row → sets selector to "manual" → drives panels below
      </div>
    </div>
  )
}

/* ---------------------------- metric over time --------------------------- */

export function MetricOverTimeCard({ data }: { data: EvaluationsResponse }) {
  const series = buildMetricSeries(data)
  const dates = series[0]?.points.map((p) => p.as_of_date) ?? []
  const chartData = dates.map((d, idx) => {
    const row: Record<string, number | string | null> = { as_of_date: d.slice(0, 7) }
    for (const s of series) {
      row[s.metric] = s.points[idx]?.value ?? null
    }
    return row
  })

  if (series.length === 0) {
    return (
      <div className="card">
        <div className="ch">
          <b>Metric over time</b>
          <span className="src">triage.evaluations</span>
        </div>
        <EmptyPanel
          reason="no evaluated splits yet"
          hint="the metric trend fills in as test splits evaluate."
        />
      </div>
    )
  }

  return (
    <div className="card">
      <div className="ch">
        <b>Metric over time</b>
        <span className="src">triage.evaluations</span>
      </div>
      <div style={{ height: 110 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData} margin={{ top: 6, right: 10, bottom: 0, left: -18 }}>
            <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
            <XAxis dataKey="as_of_date" stroke="var(--mut)" tick={{ fontSize: 9 }} />
            <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} domain={['auto', 'auto']} />
            <Tooltip
              contentStyle={{
                background: 'var(--panel)',
                border: '1px solid var(--line)',
                fontSize: 11,
              }}
            />
            {series.map((s, i) => (
              <Line
                key={s.metric}
                type="monotone"
                dataKey={s.metric}
                stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
                strokeWidth={2}
                dot={false}
                connectNulls
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="muted" style={{ fontSize: 10, marginTop: 6 }}>
        updates live per evaluated split
      </div>
    </div>
  )
}

/* --------------------------- top predictions ----------------------------- */

export function TopPredictionsCard({
  data,
  modelLabel,
}: {
  data: PredictionsResponse
  modelLabel: string
}) {
  return (
    <div className="card">
      <div className="ch">
        <b>Top predictions</b>
        <span className="driven">⟵ selected: {modelLabel}</span>
      </div>
      {isEmpty(data) ? (
        <EmptyPanel reason={data.reason} hint={data.hint} />
      ) : (
        <>
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 6 }}>
            <span className="src">prediction_ranks (latest scored_at)</span>
          </div>
          <table>
            <thead>
              <tr>
                <th>rank</th>
                <th>entity</th>
                <th className="num">pct</th>
                <th className="num">score</th>
              </tr>
            </thead>
            <tbody>
              {data.map((p) => (
                <tr key={`${p.model_id}-${p.entity_id}-${p.as_of_date}`}>
                  <td>{p.rank_abs}</td>
                  <td className="mono">{p.entity_id}</td>
                  <td className="num">{p.rank_pct == null ? '—' : `${(p.rank_pct * 100).toFixed(1)}%`}</td>
                  <td className="num">{p.score.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="muted" style={{ fontSize: 10, marginTop: 6 }}>
            append-only; "current" = max(scored_at) · ADR-0006
          </div>
        </>
      )}
    </div>
  )
}

/* ------------------------------ source pins ------------------------------ */

export function SourcePinsCard({ data }: { data: SourcePinsResponse }) {
  const pins = deriveSourcePins(data.run_pins, data.current)
  return (
    <div className="card">
      <div className="ch">
        <b>Source pins / drift</b>
        <span className="src">run_source_pins · current_source_pins</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>source</th>
            <th>pin</th>
            <th>current</th>
            <th>drift</th>
          </tr>
        </thead>
        <tbody>
          {pins.map((p) => (
            <tr key={p.source}>
              <td>{p.source}</td>
              <td className="mono">{p.pin}</td>
              <td className="mono">{p.current ?? '—'}</td>
              <td>
                <span style={{ color: p.drift === 'drift' ? 'var(--bad)' : 'var(--ok)' }}>
                  {p.drift}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
