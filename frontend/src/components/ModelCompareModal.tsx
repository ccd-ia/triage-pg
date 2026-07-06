/*
 * ModelCompareModal — any two models side by side (plan P6): Rayid curves, score
 * distributions, windowed means, and the top-k list overlap (Jaccard + Spearman,
 * migration 0016). This is the model-vs-model complement to the audition modal's
 * group-vs-group view; opened from the model sheet's "compare with…" selector.
 */
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { RayidCurveChart } from './RayidCurveChart'
import { ScoreDistributionChart } from './ScoreDistributionChart'

function Side({ modelId, title }: { modelId: number; title: string }) {
  const card = useAsync(() => api.model(modelId), [modelId])
  const curve = useAsync(() => api.modelCurve(modelId), [modelId])
  const histo = useAsync(() => api.modelHistogram(modelId), [modelId])
  return (
    <div style={{ flex: 1, minWidth: 0 }}>
      <h4>{title}</h4>
      {(card.data?.windowed ?? []).slice(0, 2).map((w) => (
        <div key={`${w.metric}${w.parameter}`} className="muted" style={{ fontSize: 10.5 }}>
          {w.metric}
          {w.parameter}: {w.value_mean?.toFixed(3) ?? '—'} over {w.n_as_of_dates} date(s)
        </div>
      ))}
      <div style={{ marginTop: 6 }}>
        {curve.data ? <RayidCurveChart curve={curve.data} initialPct={10} /> : null}
      </div>
      <div style={{ marginTop: 6 }}>
        {histo.data ? <ScoreDistributionChart bins={histo.data} /> : null}
      </div>
    </div>
  )
}

export function ModelCompareModal({
  modelA,
  modelB,
  parameter,
  onClose,
}: {
  modelA: number
  modelB: number
  parameter?: string
  onClose: () => void
}) {
  const overlap = useAsync(
    () => api.modelOverlap(modelA, modelB, parameter),
    [modelA, modelB, parameter],
  )
  return (
    <>
      <div className="sheet-backdrop stacked" onClick={onClose} />
      <div className="modal" role="dialog" aria-label={`compare m${modelA} vs m${modelB}`}>
        <div className="sh">
          <h3>
            compare · m{modelA} vs m{modelB}
          </h3>
          <button type="button" className="close" onClick={onClose} aria-label="close">
            ×
          </button>
        </div>
        {/* do the two models flag the same entities? */}
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', margin: '4px 0 10px' }}>
          {(overlap.data ?? []).map((o) => (
            <span
              key={o.as_of_date}
              className="badge b-aud"
              title={`${o.n_intersection} shared of top-${o.k_a}/${o.k_b}`}
            >
              {o.as_of_date}: jaccard {o.jaccard?.toFixed(2) ?? '—'} · ρ{' '}
              {o.rank_corr?.toFixed(2) ?? '—'}
            </span>
          ))}
          {overlap.data && overlap.data.length === 0 ? (
            <span className="muted" style={{ fontSize: 11 }}>
              no shared prediction dates.
            </span>
          ) : null}
        </div>
        <div style={{ display: 'flex', gap: 16 }}>
          <Side modelId={modelA} title={`model ${modelA}`} />
          <Side modelId={modelB} title={`model ${modelB}`} />
        </div>
      </div>
    </>
  )
}
