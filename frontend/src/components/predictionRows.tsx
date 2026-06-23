/*
 * Render helpers for prediction rows, shared by PredictedList (inline top-N) and the
 * FullListModal "View all" table. Kept in a component-free module so the lists and the
 * modal render identical rows without tripping react-refresh's component-export rule.
 */
import type { ReactNode } from 'react'
import type { PredictionRow } from '../api/types'

export function outcomeCell(outcome: number | null): ReactNode {
  if (outcome == null) return <span className="muted">—</span>
  return outcome > 0 ? <span style={{ color: 'var(--ok)' }}>✓ hit</span> : <span className="muted">miss</span>
}

export function predictionHead(): ReactNode {
  return (
    <tr>
      <th>rank</th>
      <th>entity</th>
      <th className="num">pct</th>
      <th className="num">score</th>
      <th>outcome</th>
    </tr>
  )
}

export function predictionRow(p: PredictionRow, onEntityClick?: (id: number) => void): ReactNode {
  return (
    <tr
      key={`${p.entity_id}-${p.as_of_date}`}
      className={onEntityClick ? 'clickrow' : undefined}
      onClick={onEntityClick ? () => onEntityClick(Number(p.entity_id)) : undefined}
    >
      <td>{p.rank_abs}</td>
      <td className="mono">{p.entity_id}</td>
      <td className="num">{p.rank_pct == null ? '—' : `${(p.rank_pct * 100).toFixed(2)}%`}</td>
      <td className="num">{p.score.toFixed(3)}</td>
      <td>{outcomeCell(p.outcome)}</td>
    </tr>
  )
}
