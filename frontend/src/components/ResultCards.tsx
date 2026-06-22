/*
 * ResultCards (spec §1, §6) — the result-card grid:
 *  - ExperimentSummaryCard: §3.1 scalars + §3.2 per-split sparklines
 *  - LeaderboardCard: triage.leaderboard; clicking a row sets manual selection
 *  - MetricOverTimeCard: triage.evaluations (recharts overlay)
 *  - TopPredictionsCard: prediction_ranks, driven by the selected model
 *  - SourcePinsCard: current_source_pins
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
import { Sparkline } from './Sparkline'
import { EmptyPanel } from './EmptyPanel'

/* ----------------------------- helpers ----------------------------------- */

function fmt3(x: number | undefined): string {
  return x == null ? '—' : x.toFixed(3)
}
function fmt2(x: number | undefined): string {
  return x == null ? '—' : x.toFixed(2)
}

const SERIES_COLORS = ['#58a6ff', '#bc8cff', '#3fb950']

/* --------------------------- experiment summary -------------------------- */

export function ExperimentSummaryCard({ data }: { data: SummaryResponse }) {
  const s = data.summary
  const cohortVals = data.cohort_profile.map((p) => p.n_entities)
  const baseVals = data.base_rate.map((p) => p.base_rate ?? 0)
  return (
    <div className="card full">
      <div className="ch">
        <b>Experiment summary</b>
        <span className="src">run_summary · cohort_profile · label_base_rate · source pins</span>
      </div>
      <div className="twocol">
        <div className="kv">
          <span className="k2">problem_type</span>
          <span className="v2">{s.problem_type}</span>
          <span className="k2">cohort</span>
          <span className="v2">{s.cohort_name ?? '—'}</span>
          <span className="k2">label</span>
          <span className="v2">{s.label_name ?? '—'}</span>
          <span className="k2">temporal</span>
          <span className="v2">
            {s.temporal
              ? `${s.temporal.n_splits} splits · ${s.temporal.label_timespan ?? ''} · ${
                  s.temporal.history ?? ''
                }`
              : '—'}
          </span>
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
          <span className="v2 mono">{s.experiment_hash}</span>
          <span className="k2">profile</span>
          <span className="v2">{s.profile}</span>
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

const LB_METRICS: { key: string; label: string }[] = [
  { key: 'precision@10_pct', label: 'p@10%' },
  { key: 'precision@100_abs', label: 'p@100' },
  { key: 'auc', label: 'auc' },
  { key: 'ap', label: 'ap' },
]

export function LeaderboardCard({
  data,
  onPick,
  selectedModelId,
}: {
  data: LeaderboardResponse
  onPick: (modelId: number) => void
  selectedModelId: number | undefined
}) {
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
            {LB_METRICS.map((m) => (
              <th key={m.key} className="num">
                {m.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.rows.map((r) => (
            <tr
              key={r.model_id}
              className="clickrow"
              onClick={() => onPick(r.model_id)}
              style={r.model_id === selectedModelId ? { background: '#0d1b2a' } : undefined}
            >
              <td className="mono">
                {r.label}
                {r.is_audition_pick ? <span className="b-aud badge"> audition</span> : null}
                {r.rank_metric ? (
                  <span style={{ color: 'var(--acc)' }}> ← #1 by {r.rank_metric}</span>
                ) : null}
              </td>
              {LB_METRICS.map((m) => (
                <td key={m.key} className="num">
                  {m.key === 'auc' || m.key === 'ap' ? fmt3(r.metrics[m.key]) : fmt2(r.metrics[m.key])}
                </td>
              ))}
            </tr>
          ))}
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
  const dates = data.series[0]?.points.map((p) => p.as_of_date) ?? []
  const chartData = dates.map((d, idx) => {
    const row: Record<string, number | string | null> = { as_of_date: d.slice(0, 7) }
    for (const s of data.series) {
      row[s.metric] = s.points[idx]?.value ?? null
    }
    return row
  })
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
            {data.series.map((s, i) => (
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
      {'empty' in data && data.empty ? (
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
                <th>attribute</th>
                <th className="num">score</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((p) => (
                <tr key={p.rank}>
                  <td>{p.rank}</td>
                  <td className="mono">{p.entity_id}</td>
                  <td>{p.attribute ?? '—'}</td>
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
  return (
    <div className="card">
      <div className="ch">
        <b>Source pins / drift</b>
        <span className="src">triage.current_source_pins</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>source</th>
            <th>pin</th>
            <th className="num">rows</th>
            <th>drift</th>
          </tr>
        </thead>
        <tbody>
          {data.pins.map((p) => (
            <tr key={p.source}>
              <td>{p.source}</td>
              <td className="mono">{p.pin}</td>
              <td className="num">{p.rows == null ? '—' : p.rows.toLocaleString('en-US')}</td>
              <td>
                <span style={{ color: 'var(--ok)' }}>{p.drift}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
