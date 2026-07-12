/*
 * ExperimentHeader (Option 4) — the experiment identity strip: auto-name (or
 * user name) / description / author, plus the sibling runs of this experiment.
 * Clicking a sibling run navigates to /runs/:id (which resolves back to this
 * experiment but anchors run-scoped panels — Pipeline/Derivation — on that run).
 *
 * name/description/author are the cosmetic columns outside the experiment_hash
 * (they must not change identity); the hash is shown as the durable id.
 */
import type { ExperimentDetailResponse, TaskFraming } from '../api/types'
import { StatusBadge } from './StatusBadge'

/** task_framing (migration 0019) — the observation regime, orthogonal to problem_type. */
const FRAMING_TITLE: Record<TaskFraming, string> = {
  early_warning: 'early warning: the outcome is observed for every cohort member — %labeled should approach 100%',
  resource_prioritization:
    'resource prioritization (inspections): outcomes exist only for acted-on entities — %labeled < 100% is expected',
  visit_level: 'visit-level: the label attaches to an event/visit, not the entity period',
}

interface Props {
  data: ExperimentDetailResponse
  /** The run currently anchoring the run-scoped panels (highlighted sibling). */
  activeRunId: string | undefined
  onSelectRun: (runId: string) => void
}

export function ExperimentHeader({ data, activeRunId, onSelectRun }: Props) {
  const s = data.summary
  const name = s.name ?? s.experiment_hash.slice(0, 12)
  // Cross-experiment artifact overlap: ~100% foreign ⇒ this experiment rebuilt nothing and is a
  // duplicate of its dominant lender (e.g. the same config under a stale pre-fix hash).
  const sh = data.artifact_sharing
  const sharePct = sh && sh.n_total > 0 ? sh.n_foreign / sh.n_total : 0
  return (
    <div className="exphead">
      <h2>{name}</h2>
      {s.description ? <p className="desc">{s.description}</p> : null}
      <div className="meta">
        <span className="pill mono">{s.experiment_hash.slice(0, 12)}</span>
        <span className="pill">{s.problem_type ?? '—'}</span>
        {s.task_framing ? (
          <span className="pill" title={FRAMING_TITLE[s.task_framing]}>
            {s.task_framing.replace(/_/g, ' ')}
          </span>
        ) : null}
        <span className="pill">author · {s.author ?? '—'}</span>
        <span className="pill">{s.n_runs} run{s.n_runs === 1 ? '' : 's'}</span>
        {sh && sharePct >= 0.99 && sh.shared_with_name ? (
          <span
            className="pill warn"
            title={`every artifact of this experiment was built by ${sh.shared_with_name} — it rebuilt nothing`}
          >
            ⟲ shares 100% of artifacts with {sh.shared_with_name}
          </span>
        ) : sh && sharePct > 0 && sh.shared_with_name ? (
          <span className="pill" title={`reuses ${sh.n_shared}/${sh.n_total} artifacts from ${sh.shared_with_name}`}>
            ⟲ reuses {Math.round(sharePct * 100)}% (mostly {sh.shared_with_name})
          </span>
        ) : null}
        <div className="siblings">
          {data.runs.map((r) => {
            const replay = r.n_built === 0 && (r.n_reused ?? 0) > 0
            return (
              <button
                key={r.run_id}
                type="button"
                className={`sib${r.run_id === activeRunId ? ' on' : ''}`}
                onClick={() => onSelectRun(r.run_id)}
                title={replay ? `${r.run_id} · replay (built 0 / reused ${r.n_reused})` : (r.purpose ?? r.run_id)}
              >
                <span className="mono">{r.run_id.slice(0, 8)}</span>
                <StatusBadge status={r.status} />
                {replay ? <span className="replay-badge">replay</span> : null}
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}
