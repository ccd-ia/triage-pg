/*
 * OntologyView (/ontology) — the per-project data profile. One panel per registered
 * source: a humanized title (the source's description), the source_name/relation
 * demoted to a mono tag + a role badge (entity/event), a stats row (total rows ·
 * knowledge-date range · distinct entities, from source_profile, migration 0006), and
 * the volume-over-time spine. No hardcoded ontology.* names — all generic over
 * triage.sources(relation, knowledge_date_column).
 */
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { OntologySourceRow, SourceProfile, VolumePoint } from '../api/types'

function fmtInt(n: number | null | undefined): string {
  return n == null ? '—' : n.toLocaleString('en-US')
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="cell">
      <span className="lbl">{label}</span>
      <span className="val num">{value}</span>
    </div>
  )
}

function SourcePanel({
  source,
  profile,
  series,
}: {
  source: OntologySourceRow
  profile: SourceProfile | undefined
  series: VolumePoint[]
}) {
  const data = series.map((p) => ({ period: p.period.slice(0, 7), n: p.n }))
  // Humanize: the description is the headline; source_name/relation are the provenance tag.
  const title = source.description || source.relation
  const range =
    profile?.first_date && profile?.last_date
      ? `${profile.first_date} → ${profile.last_date}`
      : '—'
  return (
    <div className="panel">
      <div className="ch" style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8, gap: 8 }}>
        <b>{title}</b>
        <span style={{ display: 'flex', gap: 6, alignItems: 'center', flexShrink: 0 }}>
          {source.role ? (
            <span className={`badge ${source.role === 'entity' ? 'b-aud' : 'b-prov'}`}>
              {source.role}
            </span>
          ) : null}
          <span className="src mono">
            {source.source_name}
            {source.knowledge_date_column ? ` · ${source.knowledge_date_column}` : ''}
          </span>
        </span>
      </div>
      <div className="strip" style={{ marginBottom: 10 }}>
        <Stat label="rows" value={fmtInt(profile?.total_rows)} />
        <Stat label="entities" value={fmtInt(profile?.n_distinct_entities)} />
        <div className="cell">
          <span className="lbl">range</span>
          <span className="val mono" style={{ fontSize: 10.5 }}>{range}</span>
        </div>
      </div>
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
        <p className="desc">Each registered source — its volume over time, row counts, and date range.</p>
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
              source={s}
              profile={onto.data!.profile?.[s.source_name]}
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
