/*
 * OntologyView (/ontology) — the per-project data profile. One panel per registered
 * source: a humanized title (the source's description), the source_name/relation
 * demoted to a mono tag + a role badge (entity/event), a stats row (total rows ·
 * knowledge-date range · distinct entities, from source_profile, migration 0006), and
 * the volume-over-time spine. No hardcoded ontology.* names — all generic over
 * triage.sources(relation, knowledge_date_column).
 */
import { useMemo, useState } from 'react'
import { Area, AreaChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { tooltipFormatter } from '../api/format'
import type { OntologySourceRow, SourceProfile, TypeVolumePoint, VolumePoint } from '../api/types'

const TYPE_COLORS = ['#58a6ff', '#bc8cff', '#3fb950', '#d29922', '#f85149', '#39c5cf', '#db61a2', '#a371f7', '#e3b341', '#7ee787']

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
  byType,
}: {
  source: OntologySourceRow
  profile: SourceProfile | undefined
  series: VolumePoint[]
  byType: TypeVolumePoint[]
}) {
  const hasTypes = byType.length > 0 && !!source.type_column
  const [view, setView] = useState<'total' | 'type'>('total')
  const data = series.map((p) => ({ period: p.period.slice(0, 7), n: p.n }))

  // Pivot the per-type series → rows keyed by period, one column per type value (stacked areas).
  // High-cardinality types (e.g. ~100 facility_type values) would swamp the legend/chart, so keep
  // the top-N by total volume and bucket the long tail into "other".
  const TOP_N = 10
  const { types, typeData, nTypes } = useMemo(() => {
    const totals = new Map<string, number>()
    for (const p of byType) {
      const t = p.type_value ?? 'unknown'
      totals.set(t, (totals.get(t) ?? 0) + p.n)
    }
    const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([t]) => t)
    const keep = new Set(ranked.slice(0, TOP_N))
    const hasOther = ranked.length > TOP_N
    const byPeriod = new Map<string, Record<string, number | string>>()
    for (const p of byType) {
      if (!p.period) continue
      const k = p.period.slice(0, 7)
      const row = byPeriod.get(k) ?? { period: k }
      const t = p.type_value ?? 'unknown'
      const bucket = keep.has(t) ? t : 'other'
      row[bucket] = ((row[bucket] as number) ?? 0) + p.n
      byPeriod.set(k, row)
    }
    const rows = [...byPeriod.values()].sort((a, b) => String(a.period).localeCompare(String(b.period)))
    const ts = [...ranked.slice(0, TOP_N), ...(hasOther ? ['other'] : [])]
    return { types: ts, typeData: rows, nTypes: totals.size }
  }, [byType])

  const title = source.description || source.relation
  const range =
    profile?.first_date && profile?.last_date
      ? `${profile.first_date} → ${profile.last_date}`
      : '—'
  const showType = view === 'type' && hasTypes

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
      {hasTypes ? (
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 6, marginBottom: 4 }}>
          <span className="muted" style={{ fontSize: 10 }}>
            {showType && nTypes > TOP_N ? `top ${TOP_N} of ${nTypes} ${source.type_column} · rest as "other"` : ''}
          </span>
          <span style={{ display: 'flex', gap: 6 }}>
            <button type="button" className={`seg${view === 'total' ? ' on' : ''}`} onClick={() => setView('total')}>total</button>
            <button type="button" className={`seg${view === 'type' ? ' on' : ''}`} onClick={() => setView('type')}>
              by {source.type_column}
            </button>
          </span>
        </div>
      ) : null}
      <div style={{ height: showType ? 180 : 140 }}>
        <ResponsiveContainer width="100%" height="100%">
          {showType ? (
            <AreaChart data={typeData} margin={{ top: 6, right: 10, bottom: 0, left: -14 }}>
              <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
              <XAxis dataKey="period" stroke="var(--mut)" tick={{ fontSize: 9 }} minTickGap={24} />
              <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} />
              <Tooltip
                contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)', fontSize: 11 }}
                formatter={tooltipFormatter(0)}
              />
              <Legend wrapperStyle={{ fontSize: 9 }} />
              {types.map((t, i) => {
                const color = t === 'other' ? 'var(--mut)' : TYPE_COLORS[i % TYPE_COLORS.length]
                return (
                <Area
                  key={t}
                  type="monotone"
                  dataKey={t}
                  stackId="s"
                  stroke={color}
                  fill={color}
                  fillOpacity={0.35}
                  strokeWidth={1.25}
                  dot={false}
                  isAnimationActive={false}
                />
                )
              })}
            </AreaChart>
          ) : (
            <AreaChart data={data} margin={{ top: 6, right: 10, bottom: 0, left: -14 }}>
              <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
              <XAxis dataKey="period" stroke="var(--mut)" tick={{ fontSize: 9 }} minTickGap={24} />
              <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} />
              <Tooltip
                contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)', fontSize: 11 }}
                formatter={tooltipFormatter(0)}
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
          )}
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
              byType={onto.data!.volumes_by_type?.[s.source_name] ?? []}
            />
          ))}
        </div>
      ) : (
        <div className="banner">No sources registered.</div>
      )}
    </main>
  )
}
