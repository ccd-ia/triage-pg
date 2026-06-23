/*
 * ExperimentHeader (Option 4) — the experiment identity strip: auto-name (or
 * user name) / description / author, plus the sibling runs of this experiment.
 * Clicking a sibling run navigates to /runs/:id (which resolves back to this
 * experiment but anchors run-scoped panels — Pipeline/Derivation — on that run).
 *
 * name/description/author are the cosmetic columns outside the experiment_hash
 * (they must not change identity); the hash is shown as the durable id.
 */
import type { ExperimentDetailResponse } from '../api/types'
import { StatusBadge } from './StatusBadge'

interface Props {
  data: ExperimentDetailResponse
  /** The run currently anchoring the run-scoped panels (highlighted sibling). */
  activeRunId: string | undefined
  onSelectRun: (runId: string) => void
}

export function ExperimentHeader({ data, activeRunId, onSelectRun }: Props) {
  const s = data.summary
  const name = s.name ?? s.experiment_hash.slice(0, 12)
  return (
    <div className="exphead">
      <h2>{name}</h2>
      {s.description ? <p className="desc">{s.description}</p> : null}
      <div className="meta">
        <span className="pill mono">{s.experiment_hash.slice(0, 12)}</span>
        <span className="pill">{s.problem_type ?? '—'}</span>
        <span className="pill">author · {s.author ?? '—'}</span>
        <span className="pill">{s.n_runs} run{s.n_runs === 1 ? '' : 's'}</span>
        <div className="siblings">
          {data.runs.map((r) => (
            <button
              key={r.run_id}
              type="button"
              className={`sib${r.run_id === activeRunId ? ' on' : ''}`}
              onClick={() => onSelectRun(r.run_id)}
              title={r.purpose ?? r.run_id}
            >
              <span className="mono">{r.run_id.slice(0, 8)}</span>
              <StatusBadge status={r.status} />
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
