/*
 * SelectedModelContextBar (Option 2) — the persistent context bar that shows the
 * selected Experiment ▸ Group ▸ Model and drives the model-scoped panels. The
 * source segments (audition | leaderboard | manual) come from the useExperiment
 * selection; a divergence flag warns when leaderboard #1 ≠ the audition pick.
 *
 * Selection state flows from useExperiment (the page builds the value from
 * /selected-model + the user's source choice); this component reads it and
 * renders the chips + segment buttons, calling back into the context setters.
 */
import type { ExpSelectedModelResponse } from '../api/types'
import { isEmpty } from '../api/types'
import { useExperiment } from '../hooks/useExperiment'

interface Props {
  experimentName: string
  /** /selected-model result (resolves audition/leaderboard ids + divergence). */
  selected: ExpSelectedModelResponse | undefined
  /** Human labels resolved by the page (model_group / model). */
  groupLabel: string | null
  modelLabel: string | null
  /** True once at least one model is evaluated (manual selectable). */
  manualAvailable: boolean
  /** Open the active model's sheet. */
  onOpenModel: () => void
}

export function SelectedModelContextBar({
  experimentName,
  selected,
  groupLabel,
  modelLabel,
  manualAvailable,
  onOpenModel,
}: Props) {
  const { source, modelId, setSource } = useExperiment()
  const data = selected && !isEmpty(selected) ? selected : undefined
  const diverges = data?.diverges ?? false

  return (
    <div className="ctxbar">
      <div className="chip">
        <span className="k">exp</span>
        <span className="v">{experimentName}</span>
      </div>
      <span className="muted">▸</span>
      <div className="chip">
        <span className="k">group</span>
        <span className="v">{groupLabel ?? '—'}</span>
      </div>
      <span className="muted">▸</span>
      <button
        type="button"
        className="chip"
        onClick={onOpenModel}
        disabled={modelId == null}
        style={{ cursor: modelId == null ? 'default' : 'pointer' }}
        title={modelId == null ? 'no model selected' : 'open model sheet'}
      >
        <span className="k">model</span>
        <span className="v">{modelLabel ?? '—'}</span>
      </button>

      <span className="from muted" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        from:
        <button
          type="button"
          className={`seg${source === 'audition' ? ' on' : ''}`}
          disabled={!data?.audition_model}
          onClick={() => setSource('audition')}
        >
          audition
        </button>
        <button
          type="button"
          className={`seg${source === 'leaderboard' ? ' on' : ''}`}
          disabled={!data?.leaderboard_model}
          onClick={() => setSource('leaderboard')}
        >
          leaderboard
        </button>
        <button
          type="button"
          className={`seg${source === 'manual' ? ' on' : ''}`}
          disabled={!manualAvailable}
          onClick={() => setSource('manual')}
        >
          manual
        </button>
      </span>

      {diverges && data ? (
        <span className="diverge">⚠ leaderboard #1 ≠ audition pick — click to compare</span>
      ) : null}
    </div>
  )
}
