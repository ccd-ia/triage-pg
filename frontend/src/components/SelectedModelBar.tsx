/*
 * SelectedModelBar (spec §1, §3.5) — drives the model-scoped panels (Top
 * predictions, Bias, Model detail). Shows the active model, the source segments
 * (audition | leaderboard | manual), and a divergence flag when leaderboard #1
 * ≠ the audition pick. Default = audition pick; run-state fallback
 * pending → provisional → final.
 *
 * `audition`/`leaderboard` segments set the source from /selected-model;
 * `manual` is client state set by clicking a Leaderboard row (handled by the
 * parent). The bar is presentational over the lifted selection state.
 */
import type { SelectedModelResponse, SelectionSource, SelectionState } from '../api/types'

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
  /** True once at least one model has been evaluated (manual is selectable). */
  manualAvailable: boolean
  onSourceChange: (source: SelectionSource) => void
}

export function SelectedModelBar({
  selected,
  source,
  activeLabel,
  manualAvailable,
  onSourceChange,
}: Props) {
  const diverges = selected?.diverges ?? false
  const state: SelectionState = selected?.state ?? 'pending'

  return (
    <>
      <div className="selbar">
        <span>Model-specific panels show:</span>
        <span className="pick">{activeLabel} ▾</span>
        <span className="from">
          from:
          <Seg
            on={source === 'audition'}
            disabled={!selected?.audition_model_id}
            onClick={() => onSourceChange('audition')}
          >
            audition
          </Seg>
          <Seg
            on={source === 'leaderboard'}
            disabled={!selected?.leaderboard_model}
            onClick={() => onSourceChange('leaderboard')}
          >
            leaderboard
          </Seg>
          <Seg on={source === 'manual'} disabled={!manualAvailable} onClick={() => onSourceChange('manual')}>
            manual
          </Seg>
        </span>
        {diverges && selected ? (
          <span className="diverge">
            ⚠ leaderboard #1 ({selected.leaderboard_label}
            {selected.leaderboard_metric ? ` by ${selected.leaderboard_metric}` : ''}) ≠ audition
            pick ({selected.audition_label}) — click to compare
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
