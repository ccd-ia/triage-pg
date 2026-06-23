/*
 * PredictedList — the top predicted entities for a model (/models/{id}/predictions),
 * joined to their realized outcome. Append-only predictions: "current" = the latest
 * scored_at (ADR-0006). Shows the top page inline; a "View all" opens the full paginated
 * list. Each entity row is clickable → the entity profile drawer. Row rendering lives in
 * predictionRows so the inline list and the modal stay identical.
 */
import type { ModelPredictionsResponse } from '../api/types'
import { isEmpty } from '../api/types'
import { EmptyPanel } from './EmptyPanel'
import { predictionHead, predictionRow } from './predictionRows'

export function PredictedList({
  data,
  onEntityClick,
  onViewAll,
}: {
  data: ModelPredictionsResponse
  onEntityClick?: (id: number) => void
  onViewAll?: () => void
}) {
  if (isEmpty(data)) {
    return <EmptyPanel reason={data.reason} hint={data.hint} />
  }
  const more = data.total - data.rows.length
  return (
    <>
      <table>
        <thead>{predictionHead()}</thead>
        <tbody>{data.rows.map((p) => predictionRow(p, onEntityClick))}</tbody>
      </table>
      {more > 0 && onViewAll ? (
        <button type="button" className="seg" style={{ marginTop: 8 }} onClick={onViewAll}>
          View all {data.total.toLocaleString('en-US')} →
        </button>
      ) : null}
    </>
  )
}
