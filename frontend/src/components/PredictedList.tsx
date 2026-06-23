/*
 * PredictedList — the top-k predicted entities for a model
 * (/models/{id}/predictions?k=), joined to their realized outcome. Append-only
 * predictions: "current" = the latest scored_at (ADR-0006). Outcome is shown as
 * a hit/miss chip when the label is known (null until the label matures).
 */
import type { ModelPredictionsResponse } from '../api/types'
import { isEmpty } from '../api/types'
import { EmptyPanel } from './EmptyPanel'

export function PredictedList({ data }: { data: ModelPredictionsResponse }) {
  if (isEmpty(data)) {
    return <EmptyPanel reason={data.reason} hint={data.hint} />
  }
  return (
    <table>
      <thead>
        <tr>
          <th>rank</th>
          <th>entity</th>
          <th className="num">pct</th>
          <th className="num">score</th>
          <th>outcome</th>
        </tr>
      </thead>
      <tbody>
        {data.map((p) => (
          <tr key={`${p.entity_id}-${p.as_of_date}`}>
            <td>{p.rank_abs}</td>
            <td className="mono">{p.entity_id}</td>
            <td className="num">{p.rank_pct == null ? '—' : `${(p.rank_pct * 100).toFixed(2)}%`}</td>
            <td className="num">{p.score.toFixed(3)}</td>
            <td>
              {p.outcome == null ? (
                <span className="muted">—</span>
              ) : p.outcome > 0 ? (
                <span style={{ color: 'var(--ok)' }}>✓ hit</span>
              ) : (
                <span className="muted">miss</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
