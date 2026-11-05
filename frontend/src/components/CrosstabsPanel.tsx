/*
 * CrosstabsPanel — what distinguishes the selected top-k from the rest (plan P5).
 * Thin read over triage.crosstabs (persisted by `triage postmodel crosstabs`, the
 * ADR-0011 pattern), in one of the two rankings the route serves:
 *
 *   - mean ratio (?stat=mean, the default): rows ranked by |log ratio| of means. The ratio
 *     column shows the direction: >1 over-represented in the list, <1 under-represented.
 *   - nonzero rate (?stat=nonzero_rate): rows ranked by |list rate − rest rate|, each
 *     feature drawn as two bars, the share of the list and the share of the rest with a
 *     nonzero value. A feature nonzero on almost all of the list and almost none of the
 *     rest is the near-total-separation leakage signature; missing values (imputed to
 *     zero) concentrated on one side show here too, where a ratio of two rates near zero
 *     would mislead.
 *
 * The ranking and every rate come from the route; the panel only lays them out.
 */
import { useState } from 'react'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { CrosstabRow, CrosstabStat } from '../api/types'
import { isEmpty } from '../api/types'
import { prettyFeature } from '../api/transforms'

const STATS: { stat: CrosstabStat; label: string; title: string }[] = [
  { stat: 'mean', label: 'mean ratio', title: 'selected mean ÷ rest mean, ranked by |log ratio|' },
  {
    stat: 'nonzero_rate',
    label: 'nonzero rate',
    title: 'share of the list and of the rest with a nonzero value, ranked by their difference',
  },
]

function pct(v: number | null): string {
  return v == null ? '—' : `${(v * 100).toFixed(0)}%`
}

function RatioTable({ rows }: { rows: CrosstabRow[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th>feature</th>
          <th className="num">selected</th>
          <th className="num">rest</th>
          <th className="num">ratio</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const { pretty, raw } = prettyFeature(r.feature)
          const over = (r.ratio ?? 1) >= 1
          return (
            <tr key={`${r.as_of_date}-${r.feature}`}>
              <td>
                <div className="pretty">{pretty}</div>
                <div className="rawsub">{raw}</div>
              </td>
              <td className="num">{r.selected_value?.toFixed(2) ?? '—'}</td>
              <td className="num">{r.rest_value?.toFixed(2) ?? '—'}</td>
              <td className="num" style={{ color: over ? 'var(--ok, #15803d)' : 'var(--warn, #b45309)' }}>
                {r.ratio == null ? '—' : `${r.ratio.toFixed(2)}×`}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function NonzeroBars({ rows }: { rows: CrosstabRow[] }) {
  return (
    <div className="featlist">
      {rows.map((r) => {
        const { pretty, raw } = prettyFeature(r.feature)
        return (
          <div className="featrow" key={`${r.as_of_date}-${r.parameter}-${r.feature}`}>
            <div>
              <div className="pretty">{pretty}</div>
              <div className="rawsub">
                {raw} · {r.as_of_date} @{r.parameter}
              </div>
            </div>
            <div />
            <div className="nzbars">
              <span>list</span>
              <div className="track" title={`nonzero in ${pct(r.selected_value)} of the selected top-k`}>
                <div className="fill list" style={{ width: `${Math.min(1, r.selected_value ?? 0) * 100}%` }} />
              </div>
              <span className="val">{pct(r.selected_value)}</span>
              <span>rest</span>
              <div className="track" title={`nonzero in ${pct(r.rest_value)} of the rest`}>
                <div className="fill rest" style={{ width: `${Math.min(1, r.rest_value ?? 0) * 100}%` }} />
              </div>
              <span className="val">{pct(r.rest_value)}</span>
            </div>
          </div>
        )
      })}
    </div>
  )
}

export function CrosstabsPanel({ modelId }: { modelId: number }) {
  const [stat, setStat] = useState<CrosstabStat>('mean')
  // the statistic travels with its rows, so a stale response never draws in the other layout
  const res = useAsync(
    async () => ({ stat, rows: await api.modelCrosstabs(modelId, stat) }),
    [modelId, stat],
  )

  return (
    <>
      <div style={{ display: 'flex', gap: 6, marginBottom: 6, alignItems: 'center' }}>
        {STATS.map((s) => (
          <button
            key={s.stat}
            type="button"
            title={s.title}
            aria-pressed={stat === s.stat}
            className={`seg${stat === s.stat ? ' active' : ''}`}
            onClick={() => setStat(s.stat)}
          >
            {s.label}
          </button>
        ))}
      </div>
      {res.error ? (
        <div className="muted" style={{ fontSize: 11 }}>
          failed to load crosstabs: {res.error.message}
        </div>
      ) : !res.data ? (
        <div className="muted" style={{ fontSize: 11 }}>loading crosstabs…</div>
      ) : isEmpty(res.data.rows) ? (
        <div className="muted" style={{ fontSize: 11 }}>
          {res.data.rows.reason} — {res.data.rows.hint}
        </div>
      ) : res.data.stat === 'nonzero_rate' ? (
        <>
          <div className="muted" style={{ fontSize: 10, margin: '0 0 4px' }}>
            share of rows with a nonzero value: <b style={{ color: 'var(--acc)' }}>list</b> = the
            selected top-k, <b>rest</b> = everyone else. Near 100% against near 0% is the
            leakage signature; check how the feature is known.
          </div>
          <NonzeroBars rows={res.data.rows} />
        </>
      ) : (
        <RatioTable rows={res.data.rows} />
      )}
    </>
  )
}
