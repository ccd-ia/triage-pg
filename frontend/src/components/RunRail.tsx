/*
 * RunRail — left rail listing triage.runs (spec §1). Selecting a run navigates
 * to /runs/:id. Status badge + started_at + a derived sub-line.
 *
 * Reconciled to routes.py: GET /runs returns raw triage.runs rows (no `label` /
 * `headline_metric` / `progress_line`). The rail derives a short label from
 * purpose / experiment_hash and a sub-line from profile + status.
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

/** A short human label for a run row from the columns triage.runs provides. */
function runLabel(r: RunListItem): string {
  if (r.purpose) return r.purpose
  if (r.experiment_hash) return r.experiment_hash.slice(0, 12)
  return shortId(r.run_id)
}

/** Sub-line: profile + (for completed runs) the finish, else the live status. */
function runSubLine(r: RunListItem): string {
  const parts: string[] = []
  if (r.profile) parts.push(r.profile)
  if (r.status === 'completed' && r.finished_at) parts.push(`done ${relativeTime(r.finished_at)}`)
  else if (r.status === 'building' || r.status === 'started') parts.push('in progress')
  else if (r.status === 'failed') parts.push('failed')
  return parts.join(' · ')
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
        const sub = runSubLine(r)
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
              {runLabel(r)} · {relativeTime(r.started_at)}
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
