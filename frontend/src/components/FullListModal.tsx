/*
 * FullListModal — a generic paginated "View all" modal for the side-sheet lists
 * (predictions, feature importances). The sheet shows the top 20 inline; this opens
 * the full list, paged. `loadPage(offset, limit)` is async so the predictions list
 * pages server-side (limit/offset, migration 0006); bounded lists (feature
 * importances) resolve a synchronous slice.
 */
import { useState, type ReactNode } from 'react'
import { useAsync } from '../hooks/useAsync'

interface Props<T> {
  title: string
  total: number
  pageSize?: number
  loadPage: (offset: number, limit: number) => Promise<T[]>
  head: ReactNode
  row: (item: T, index: number) => ReactNode
  onClose: () => void
}

export function FullListModal<T>({
  title,
  total,
  pageSize = 50,
  loadPage,
  head,
  row,
  onClose,
}: Props<T>) {
  const [offset, setOffset] = useState(0)
  // useAsync reads loadPage via a ref, so an inline loadPage prop won't re-trigger;
  // the page re-loads only when offset/pageSize change.
  const page = useAsync(() => loadPage(offset, pageSize), [offset, pageSize])
  const rows = page.data ?? []
  const loading = page.loading

  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(offset + pageSize, total)
  const canPrev = offset > 0
  const canNext = offset + pageSize < total

  return (
    <>
      <div className="sheet-backdrop stacked" onClick={onClose} />
      <div className="modal" role="dialog" aria-label={title}>
        <div className="sh">
          <div>
            <h3>{title}</h3>
            <div className="sub mono">
              {from}–{to} of {total.toLocaleString('en-US')}
            </div>
          </div>
          <button type="button" className="close" onClick={onClose} aria-label="close">
            ×
          </button>
        </div>
        <div className="modal-body">
          <table>
            <thead>{head}</thead>
            <tbody>{rows.map((item, i) => row(item, offset + i))}</tbody>
          </table>
          {loading ? <div className="muted" style={{ fontSize: 11, padding: 8 }}>loading…</div> : null}
        </div>
        <div className="modal-foot">
          <button type="button" className="seg" disabled={!canPrev} onClick={() => setOffset((o) => Math.max(0, o - pageSize))}>
            ‹ prev
          </button>
          <span className="muted" style={{ fontSize: 11 }}>
            {from}–{to} of {total.toLocaleString('en-US')}
          </span>
          <button type="button" className="seg" disabled={!canNext} onClick={() => setOffset((o) => o + pageSize)}>
            next ›
          </button>
        </div>
      </div>
    </>
  )
}
