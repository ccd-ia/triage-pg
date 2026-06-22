/*
 * SelectedModelBar (spec §1, §3.5) — drives the model-scoped panels (Top
 * predictions, Bias, Model detail). Shows the active model, the source segments
 * (audition | leaderboard | manual), and a divergence flag when leaderboard #1
 * ≠ the audition pick. Default = audition pick; run-state fallback
 * pending → provisional → final.
 *
 * Reconciled to routes.py: GET /selected-model returns {metric, parameter,
 * rule, audition_group, audition_model, leaderboard_group, leaderboard_model,
 * diverges} OR the empty envelope — bigint ids, NO human labels and NO `state`.
 * The provenance label (audition|leaderboard|manual) is SPA client state; the
 * run-state and the human model labels are resolved by the parent and passed in.
 */
import type { SelectedModelResponse, SelectionSource, SelectionState } from '../api/types'
import { isEmpty } from '../api/types'

const STATE_NOTE: Record<SelectionState, string> = {
  pending: 'pending — no evaluated models yet',
  provisional: 'provisional — audition pick over k<N splits',
  final: 'final — at run completion',
}

interface Props {
  selected: SelectedModelResponse | undefined
  source: SelectionSource
  /** Human label of the currently active model (resolves manual too). */
  activeLabel: string
  /** Labels for the audition / leaderboard picks (resolved by the parent). */
  auditionLabel: string | null
  leaderboardLabel: string | null
  /** Client-derived run-state for the strip note. */
  state: SelectionState
  /** True once at least one model has been evaluated (manual is selectable). */
  manualAvailable: boolean
  onSourceChange: (source: SelectionSource) => void
}

export function SelectedModelBar({
  selected,
  source,
  activeLabel,
  auditionLabel,
  leaderboardLabel,
  state,
  manualAvailable,
  onSourceChange,
}: Props) {
  const data = selected && !isEmpty(selected) ? selected : undefined
  const diverges = data?.diverges ?? false

  return (
    <>
      <div className="selbar">
        <span>Model-specific panels show:</span>
        <span className="pick">{activeLabel} ▾</span>
        <span className="from">
          from:
          <Seg
            on={source === 'audition'}
            disabled={!data?.audition_model}
            onClick={() => onSourceChange('audition')}
          >
            audition
          </Seg>
          <Seg
            on={source === 'leaderboard'}
            disabled={!data?.leaderboard_model}
            onClick={() => onSourceChange('leaderboard')}
          >
            leaderboard
          </Seg>
          <Seg on={source === 'manual'} disabled={!manualAvailable} onClick={() => onSourceChange('manual')}>
            manual
          </Seg>
        </span>
        {diverges && data ? (
          <span className="diverge">
            ⚠ leaderboard #1 ({leaderboardLabel ?? `model ${data.leaderboard_model}`}) ≠ audition
            pick ({auditionLabel ?? `model ${data.audition_model}`}) — click to compare
          </span>
        ) : null}
      </div>
      <div className="muted" style={{ fontSize: 10, marginTop: -8 }}>
        run-state: <span className="mono">{state}</span> · {STATE_NOTE[state]}
      </div>
    </>
  )
}

function Seg({
  on,
  disabled,
  onClick,
  children,
}: {
  on: boolean
  disabled?: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      className={`seg${on ? ' on' : ''}`}
      disabled={disabled}
      onClick={onClick}
    >
      {children}
    </button>
  )
}
