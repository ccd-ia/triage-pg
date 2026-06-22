/* Status badge atom — maps run/artifact status to the mockup's badge classes. */
import type { RunStatus } from '../api/types'

const CLASS: Record<RunStatus, string> = {
  started: 'b-build',
  building: 'b-build',
  completed: 'b-run',
  failed: 'b-fail',
}

const LABEL: Record<RunStatus, string> = {
  started: 'started',
  building: 'building',
  completed: 'done',
  failed: 'failed',
}

export function StatusBadge({ status }: { status: RunStatus }) {
  return <span className={`badge ${CLASS[status]}`}>{LABEL[status]}</span>
}
