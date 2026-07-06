/*
 * CrosstabsPanel — what distinguishes the selected top-k from the rest (plan P5).
 * Thin read over triage.crosstabs (persisted by `triage postmodel crosstabs`, the
 * ADR-0011 pattern); rows arrive pre-ranked by |log ratio| of means. Bars show the
 * ratio direction: >1 over-represented in the list, <1 under-represented.
 */
import type { CrosstabsResponse } from '../api/types'
import { isEmpty } from '../api/types'
import { prettyFeature } from '../api/transforms'

export function CrosstabsPanel({ data }: { data: CrosstabsResponse }) {
  if (isEmpty(data)) {
    return (
      <div className="muted" style={{ fontSize: 11 }}>
        {data.reason} — {data.hint}
      </div>
    )
  }
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
        {data.map((r) => {
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
