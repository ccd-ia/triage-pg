/*
 * MonitoringView (/monitoring) — the ADR-0006 payoff over append-only predictions
 * (ADR-0027, migration 0012). Everything shown IS the in-PG monitoring layer:
 *   volume    — triage.monitoring_volume (the scoring heartbeat per group/day)
 *   drift     — triage.monitoring_score_drift (PSI + KS: pinned reference vs latest window)
 *   outcomes  — triage.monitoring_outcome_tracking (realized metrics as labels arrive)
 * The default drift comparison is first-scored-day (the pinned reference by convention)
 * vs last-scored-day; both windows are visible and the operator's convention rules
 * (docs/monitoring.md).
 */
import { useMemo, useState } from 'react'
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
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { MonitoringVolumeRow } from '../api/types'
import { EmptyPanel } from '../components/EmptyPanel'

function nextDay(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + 1)
  return d.toISOString().slice(0, 10)
}

export function MonitoringView() {
  const volume = useAsync(() => api.monitoringVolume(), [])
  const groups = useMemo(
    () => Array.from(new Set((volume.data ?? []).map((v) => v.model_group_id))).sort((a, b) => a - b),
    [volume.data],
  )
  const [group, setGroup] = useState<number | null>(null)
  const active = group ?? groups[0] ?? null

  const rows: MonitoringVolumeRow[] = useMemo(
    () => (volume.data ?? []).filter((v) => v.model_group_id === active),
    [volume.data, active],
  )
  const days = useMemo(
    () => Array.from(new Set(rows.map((r) => r.scored_on))).sort(),
    [rows],
  )

  // Pinned-reference convention: first scored day = reference, latest = the window under
  // inspection. Both are day-wide scored_at windows.
  const drift = useAsync(() => {
    if (active === null || days.length < 2) return Promise.resolve(null)
    return api.monitoringDrift({
      modelGroupId: active,
      referenceFrom: days[0],
      referenceTo: nextDay(days[0]),
      windowFrom: days[days.length - 1],
      windowTo: nextDay(days[days.length - 1]),
    })
  }, [active, days.join(',')])

  const outcomes = useAsync(
    () => (active === null ? Promise.resolve([]) : api.monitoringOutcomes(active)),
    [active],
  )
  const metrics = useMemo(
    () => Array.from(new Set((outcomes.data ?? []).map((o) => `${o.metric}${o.parameter}`))),
    [outcomes.data],
  )
  const [metricKey, setMetricKey] = useState<string | null>(null)
  const activeMetric = metricKey ?? metrics[0] ?? null
  const outcomeSeries = useMemo(
    () =>
      (outcomes.data ?? [])
        .filter((o) => `${o.metric}${o.parameter}` === activeMetric && o.value != null)
        .map((o) => ({ as_of_date: o.as_of_date, value: o.value, purpose: o.purpose })),
    [outcomes.data, activeMetric],
  )

  const volumeSeries = useMemo(
    () =>
      days.map((day) => ({
        scored_on: day,
        n_predictions: rows.filter((r) => r.scored_on === day).reduce((a, r) => a + r.n_predictions, 0),
        n_entities: rows.filter((r) => r.scored_on === day).reduce((a, r) => a + r.n_entities, 0),
      })),
    [rows, days],
  )

  if (volume.loading) return <main className="page"><div className="banner">Loading monitoring…</div></main>
  if (volume.error) {
    return <main className="page"><div className="banner err">Monitoring failed to load: {volume.error.message}</div></main>
  }
  if (!volume.data || volume.data.length === 0) {
    return (
      <main className="page">
        <div className="exphead"><h2>Monitoring</h2></div>
        <EmptyPanel
          reason="No scoring history yet"
          hint="Monitoring reads the append-only predictions table (ADR-0006). Schedule forward scoring with `triage score <model_id>` (docs/monitoring.md) and this view fills in."
        />
      </main>
    )
  }

  return (
    <main className="page">
      <div className="exphead">
        <h2>Monitoring</h2>
        <p className="desc">
          Score drift, volume, and realized outcomes over the append-only{' '}
          <span className="mono">triage.predictions</span> history (ADR-0006/0027). Reference
          window pinned to the first scored day by convention.
        </p>
      </div>

      <div className="formrow" style={{ marginBottom: 14 }}>
        <label className="field">
          <span>model group</span>
          <select value={active ?? ''} onChange={(e) => setGroup(Number(e.target.value))}>
            {groups.map((g) => (
              <option key={g} value={g}>
                group {g}
              </option>
            ))}
          </select>
        </label>
        {drift.data ? (
          <span style={{ display: 'flex', gap: 8, alignItems: 'flex-end', paddingBottom: 4 }}>
            <DriftChip label="PSI" value={drift.data.psi} warn={0.1} bad={0.25} />
            <DriftChip label="KS" value={drift.data.ks} warn={0.1} bad={0.2} />
            <span className="muted" style={{ fontSize: 11 }}>
              {days[0]} (ref, n={drift.data.n_reference}) → {days[days.length - 1]} (n=
              {drift.data.n_window})
            </span>
          </span>
        ) : days.length < 2 ? (
          <span className="muted" style={{ fontSize: 11, paddingBottom: 8 }}>
            drift needs two scored days — one scoring run so far
          </span>
        ) : null}
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="ch">
          <b>Scoring volume</b>
          <span className="src">triage.monitoring_volume</span>
        </div>
        <ResponsiveContainer width="100%" height={180}>
          <BarChart data={volumeSeries}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="scored_on" fontSize={10} />
            <YAxis fontSize={10} />
            <Tooltip />
            <Bar dataKey="n_predictions" name="predictions" fill="var(--acc, #4a7fb5)" />
          </BarChart>
        </ResponsiveContainer>
      </div>

      <div className="card">
        <div className="ch">
          <b>Realized outcomes over time</b>
          <span className="src">triage.monitoring_outcome_tracking</span>
          {metrics.length > 1 ? (
            <select
              value={activeMetric ?? ''}
              onChange={(e) => setMetricKey(e.target.value)}
              style={{ marginLeft: 10 }}
            >
              {metrics.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          ) : null}
        </div>
        {outcomeSeries.length ? (
          <ResponsiveContainer width="100%" height={180}>
            <LineChart data={outcomeSeries}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="as_of_date" fontSize={10} />
              <YAxis fontSize={10} domain={[0, 1]} />
              <Tooltip />
              <Line type="monotone" dataKey="value" dot />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="banner" style={{ margin: 8 }}>
            No realized evaluations yet — when labels arrive for a scored date, re-run{' '}
            <span className="mono">triage.evaluate_model</span> (docs/monitoring.md) and the
            realized series appears here.
          </div>
        )}
      </div>
    </main>
  )
}

/** PSI/KS chip with the rule-of-thumb thresholds (green &lt; warn &lt; red). */
function DriftChip({
  label,
  value,
  warn,
  bad,
}: {
  label: string
  value: number | null
  warn: number
  bad: number
}) {
  if (value == null) return <span className="badge">{label} —</span>
  const cls = value >= bad ? 'b-err' : value >= warn ? 'b-aud' : 'b-run'
  return (
    <span className={`badge ${cls}`} title={`${label} threshold: investigate ≥ ${warn}, drifted ≥ ${bad}`}>
      {label} {value.toFixed(3)}
    </span>
  )
}
