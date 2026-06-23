/*
 * OntologyView (/ontology, Q5) — the per-project data profile: each source's
 * volume-over-time (the EDA-style spine). One panel per source from
 * /ontology (sources + volumes), with the relation + knowledge_date_column it is
 * generated from. No hardcoded ontology.* names — the backend derives volumes
 * generically from triage.sources(relation, knowledge_date_column).
 */
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { VolumePoint } from '../api/types'

function SourcePanel({
  name,
  relation,
  kd,
  description,
  series,
}: {
  name: string
  relation: string
  kd: string | null
  description: string | null
  series: VolumePoint[]
}) {
  const data = series.map((p) => ({ period: p.period.slice(0, 7), n: p.n }))
  return (
    <div className="panel">
      <div className="ch" style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <b>{name}</b>
        <span className="src mono">{relation}{kd ? ` · ${kd}` : ''}</span>
      </div>
      {description ? (
        <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>{description}</div>
      ) : null}
      <div style={{ height: 140 }}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 6, right: 10, bottom: 0, left: -14 }}>
            <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
            <XAxis dataKey="period" stroke="var(--mut)" tick={{ fontSize: 9 }} minTickGap={24} />
            <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} />
            <Tooltip
              contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)', fontSize: 11 }}
            />
            <Area
              type="monotone"
              dataKey="n"
              stroke="var(--acc)"
              fill="var(--acc)"
              fillOpacity={0.16}
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

export function OntologyView() {
  const onto = useAsync(() => api.ontology(), [])

  return (
    <main className="page">
      <div className="exphead">
        <h2>Ontology · data profile</h2>
        <p className="desc">Volume over time per source — the EDA spine for this project.</p>
      </div>
      {onto.loading ? (
        <div className="banner">Loading ontology…</div>
      ) : onto.error ? (
        <div className="banner err">Failed to load ontology: {onto.error.message}</div>
      ) : onto.data && onto.data.sources.length ? (
        <div className="cards" style={{ gridTemplateColumns: '1fr 1fr' }}>
          {onto.data.sources.map((s) => (
            <SourcePanel
              key={s.source_name}
              name={s.source_name}
              relation={s.relation}
              kd={s.knowledge_date_column}
              description={s.description}
              series={onto.data!.volumes[s.source_name] ?? []}
            />
          ))}
        </div>
      ) : (
        <div className="banner">No sources registered.</div>
      )}
    </main>
  )
}
