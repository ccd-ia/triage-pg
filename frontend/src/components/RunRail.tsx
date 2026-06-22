/*
 * RunRail — left rail listing triage.runs (spec §1). Selecting a run navigates
 * to /runs/:id. Status badge + started_at + headline metric / progress line.
 */
import type { RunListItem } from '../api/types'
import { StatusBadge } from './StatusBadge'

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const diffMin = Math.round((Date.now() - then) / 60000)
  if (diffMin < 1) return 'just now'
  if (diffMin < 60) return `${diffMin}m ago`
  const diffH = Math.round(diffMin / 60)
  if (diffH < 24) return `${diffH}h ago`
  return `${Math.round(diffH / 24)}d ago`
}

function shortId(runId: string): string {
  return runId.slice(0, 8)
}

interface Props {
  runs: RunListItem[]
  selectedId: string | undefined
  onSelect: (runId: string) => void
}

export function RunRail({ runs, selectedId, onSelect }: Props) {
  return (
    <aside className="rail">
      <h3 className="k">runs · triage.runs</h3>
      {runs.map((r) => {
        const sub = r.headline_metric ?? r.progress_line ?? ''
        return (
          <button
            type="button"
            key={r.run_id}
            className={`ri${r.run_id === selectedId ? ' sel' : ''}`}
            onClick={() => onSelect(r.run_id)}
          >
            <div className="top">
              <span className="id mono">{shortId(r.run_id)}</span>
              <StatusBadge status={r.status} />
            </div>
            <small>
              {r.label} · {relativeTime(r.started_at)}
              {sub ? (
                <>
                  <br />
                  {sub}
                </>
              ) : null}
            </small>
          </button>
        )
      })}
    </aside>
  )
}
