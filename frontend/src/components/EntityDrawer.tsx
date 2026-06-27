/*
 * EntityDrawer — the full entity profile (click an entity in the predicted list).
 * Shows (a) the entity-grain attributes (role='entity' source row), (b) its label /
 * outcome history (full as_of grid; NULL = no matured label), and (c) its score +
 * rank trajectory across as_of_dates, defaulting to the model group it was opened
 * from with a toggle to overlay other model groups.
 * Data: GET /entities/{id}?experiment_hash= (migrations 0006 / 0008).
 *
 * Attribute rendering is shape-aware: a PostGIS point (migration 0008 emits
 * {lon,lat,geojson,kind:'geo'}) renders as readable coords + an OpenStreetMap link;
 * a daterange string ("[lo,hi)") renders as "lo → hi" + duration + a span bar.
 */
import { useMemo, useState } from 'react'
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { tooltipFormatter } from '../api/format'
import type { EntityScorePoint } from '../api/types'

const LINE_COLORS = ['#58a6ff', '#bc8cff', '#3fb950', '#d29922', '#f85149', '#39c5cf', '#db61a2', '#a371f7']

function fmtVal(v: unknown): string {
  if (v == null) return '—'
  const s = typeof v === 'object' ? JSON.stringify(v) : String(v)
  return s.length > 48 ? `${s.slice(0, 45)}…` : s
}

/** A PostGIS geo attribute as emitted by entity_attributes() (migration 0008). */
interface GeoVal {
  kind: 'geo'
  lon: number
  lat: number
  geojson?: unknown
}
function isGeo(v: unknown): v is GeoVal {
  return (
    typeof v === 'object' &&
    v !== null &&
    (v as { kind?: unknown }).kind === 'geo' &&
    typeof (v as GeoVal).lat === 'number' &&
    typeof (v as GeoVal).lon === 'number'
  )
}

/** Parse a PostgreSQL daterange literal like "[2014-01-14,2017-02-24)" → [lo, hi]. */
function parseDateRange(s: string): [string, string] | null {
  const m = /^[[(]\s*("?)([0-9]{4}-[0-9]{2}-[0-9]{2})\1\s*,\s*("?)([0-9]{4}-[0-9]{2}-[0-9]{2})\3\s*[)\]]$/.exec(s)
  return m ? [m[2], m[4]] : null
}

function fmtDuration(lo: string, hi: string): string {
  const a = new Date(lo)
  const b = new Date(hi)
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return ''
  let months = (b.getFullYear() - a.getFullYear()) * 12 + (b.getMonth() - a.getMonth())
  if (b.getDate() < a.getDate()) months -= 1
  if (months < 0) return ''
  const y = Math.floor(months / 12)
  const mo = months % 12
  return [y ? `${y}y` : '', mo ? `${mo}mo` : ''].filter(Boolean).join(' ') || '<1mo'
}

function GeoValue({ geo }: { geo: GeoVal }) {
  const url = `https://www.openstreetmap.org/?mlat=${geo.lat}&mlon=${geo.lon}#map=16/${geo.lat}/${geo.lon}`
  return (
    <span className="v2">
      <span className="mono">
        {geo.lat.toFixed(5)}, {geo.lon.toFixed(5)}
      </span>{' '}
      <a className="geolink" href={url} target="_blank" rel="noreferrer" title="open in OpenStreetMap">
        map ↗
      </a>
    </span>
  )
}

function DateRangeValue({ lo, hi }: { lo: string; hi: string }) {
  const dur = fmtDuration(lo, hi)
  return (
    <span className="v2">
      <span className="mono">{lo} → {hi}</span>
      <span className="rangebar" aria-hidden />
      {dur ? <span className="muted" style={{ fontSize: 10 }}>{dur}</span> : null}
    </span>
  )
}

function AttrValue({ v }: { v: unknown }) {
  if (isGeo(v)) return <GeoValue geo={v} />
  if (typeof v === 'string') {
    const r = parseDateRange(v)
    if (r) return <DateRangeValue lo={r[0]} hi={r[1]} />
  }
  return <span className="v2 mono">{fmtVal(v)}</span>
}

type Field = 'score' | 'rank_pct'

export function EntityDrawer({
  entityId,
  experimentHash,
  defaultGroupId,
  groupLabelOf,
  onClose,
}: {
  entityId: number
  experimentHash?: string
  defaultGroupId?: number | null
  groupLabelOf?: (gid: number) => string
  onClose: () => void
}) {
  const profile = useAsync(() => api.entity(entityId, { experimentHash }), [entityId, experimentHash])
  const [field, setField] = useState<Field>('score')
  const [showAll, setShowAll] = useState(false)

  const score_history: EntityScorePoint[] = useMemo(
    () => profile.data?.score_history ?? [],
    [profile.data],
  )

  const groups = useMemo(() => {
    const set = new Set(score_history.map((p) => p.model_group_id))
    return [...set].sort((a, b) => a - b)
  }, [score_history])

  // Which model-group series to draw: the opened group by default, all when toggled.
  const visibleGroups = useMemo(() => {
    if (showAll || groups.length <= 1) return groups
    if (defaultGroupId != null && groups.includes(defaultGroupId)) return [defaultGroupId]
    return groups.slice(0, 1)
  }, [showAll, groups, defaultGroupId])

  // Pivot to recharts rows keyed by as_of_date with one column per visible group.
  const chartData = useMemo(() => {
    const byDate = new Map<string, Record<string, number | string>>()
    for (const p of score_history) {
      if (!visibleGroups.includes(p.model_group_id)) continue
      const d = p.as_of_date.slice(0, 7)
      const row = byDate.get(d) ?? { as_of_date: d }
      row[`g${p.model_group_id}`] = field === 'score' ? p.score : (p.rank_pct ?? 0)
      byDate.set(d, row)
    }
    return [...byDate.values()].sort((a, b) => String(a.as_of_date).localeCompare(String(b.as_of_date)))
  }, [score_history, visibleGroups, field])

  const labelFor = (gid: number) => (groupLabelOf ? groupLabelOf(gid) : `group ${gid}`)
  const attrs = profile.data?.attributes ?? null
  // The model group the drawer was opened from — keep it visible so the trajectory's
  // context ("which model is this?") is never lost when overlaying others.
  const primaryGroup = defaultGroupId != null && groups.includes(defaultGroupId) ? defaultGroupId : null

  return (
    <>
      <div className="sheet-backdrop stacked" onClick={onClose} />
      <aside className="sheet stacked" role="dialog" aria-label={`entity ${entityId}`}>
        <div className="sh">
          <div>
            <h3>entity {entityId}</h3>
            <div className="sub mono">
              {experimentHash ? `${experimentHash.slice(0, 12)} · ` : ''}full profile
              {primaryGroup != null ? ` · group ${labelFor(primaryGroup)}` : ''}
            </div>
          </div>
          <button type="button" className="close" onClick={onClose} aria-label="close">×</button>
        </div>

        {profile.loading ? (
          <div className="muted" style={{ fontSize: 11, padding: 8 }}>loading entity…</div>
        ) : profile.error ? (
          <div className="banner err">
            {profile.error.message.includes('404')
              ? `entity ${entityId} not found in this project`
              : `Failed to load entity: ${profile.error.message}`}
          </div>
        ) : (
          <>
            <section>
              <h4>attributes</h4>
              {attrs ? (
                <div className="kv">
                  {Object.entries(attrs).map(([k, v]) => (
                    <span key={k} style={{ display: 'contents' }}>
                      <span className="k2">{k}</span>
                      <AttrValue v={v} />
                    </span>
                  ))}
                </div>
              ) : (
                <div className="muted" style={{ fontSize: 11 }}>no entity-grain source registered (set a source role='entity').</div>
              )}
            </section>

            <section>
              <h4>label history</h4>
              {profile.data && profile.data.label_history.length ? (
                <table>
                  <thead>
                    <tr><th>as_of</th><th>timespan</th><th>outcome</th></tr>
                  </thead>
                  <tbody>
                    {profile.data.label_history.map((l) => (
                      <tr key={`${l.as_of_date}-${l.label_timespan}`}>
                        <td className="mono">{l.as_of_date}</td>
                        <td className="muted">{l.label_timespan}</td>
                        <td>
                          {l.outcome == null ? (
                            <span className="muted" title="no matured label at this as_of (not in cohort window / no event)">—</span>
                          ) : l.outcome > 0 ? (
                            <span style={{ color: 'var(--ok)' }}>1</span>
                          ) : (
                            <span className="muted">0</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <div className="muted" style={{ fontSize: 11 }}>no labels for this entity.</div>
              )}
            </section>

            <section>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <h4 style={{ margin: 0 }}>trajectory over time</h4>
                <div style={{ display: 'flex', gap: 6 }}>
                  <button type="button" className={`seg${field === 'score' ? ' on' : ''}`} onClick={() => setField('score')}>score</button>
                  <button type="button" className={`seg${field === 'rank_pct' ? ' on' : ''}`} onClick={() => setField('rank_pct')}>rank&nbsp;pct</button>
                </div>
              </div>
              {primaryGroup != null ? (
                <div className="muted" style={{ fontSize: 10.5, marginTop: 2 }}>
                  model group <b style={{ color: 'var(--acc)' }}>{labelFor(primaryGroup)}</b>
                  {showAll ? ' · others muted (hover a line for its value)' : ''}
                </div>
              ) : null}
              {groups.length > 1 ? (
                <label className="muted" style={{ fontSize: 11, display: 'flex', gap: 6, alignItems: 'center', margin: '6px 0' }}>
                  <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
                  overlay all {groups.length} model groups
                </label>
              ) : null}
              {chartData.length ? (
                <div style={{ height: 200 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={chartData} margin={{ top: 6, right: 10, bottom: 0, left: -12 }}>
                      <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
                      <XAxis dataKey="as_of_date" stroke="var(--mut)" tick={{ fontSize: 9 }} minTickGap={20} />
                      {/* score is a probability and rank_pct a fraction — both are fixed 0..1 so the
                          shape is comparable across entities (no misleading auto-zoom). */}
                      <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} domain={[0, 1]} allowDataOverflow />
                      <Tooltip
                        contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)', fontSize: 11 }}
                        formatter={tooltipFormatter(4)}
                      />
                      {/* With 23 overlaid groups a legend is unreadable; show it only for a small set. */}
                      {visibleGroups.length > 1 && visibleGroups.length <= 6 ? <Legend wrapperStyle={{ fontSize: 10 }} /> : null}
                      {visibleGroups.map((gid, i) => {
                        const isPrimary = gid === defaultGroupId
                        // In overlay mode, the opened group is the accent line and every other group
                        // collapses to a faint grey envelope — the chosen model stays legible.
                        const stroke = isPrimary ? 'var(--acc)' : showAll ? 'var(--mut)' : LINE_COLORS[i % LINE_COLORS.length]
                        return (
                          <Line
                            key={gid}
                            type="monotone"
                            dataKey={`g${gid}`}
                            name={labelFor(gid)}
                            stroke={stroke}
                            strokeWidth={isPrimary ? 2.5 : 1.25}
                            strokeOpacity={showAll && !isPrimary ? 0.3 : 1}
                            dot={false}
                            isAnimationActive={false}
                            connectNulls
                          />
                        )
                      })}
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <div className="muted" style={{ fontSize: 11 }}>no predictions for this entity.</div>
              )}
            </section>
          </>
        )}
      </aside>
    </>
  )
}
