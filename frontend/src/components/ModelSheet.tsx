/*
 * ModelSheet — the model card as a right side-sheet (Option 4). Opened by
 * clicking a grid cell, a model-groups row, or a leaderboard row. Composes:
 *   - ScoreDistributionChart  (/models/{id}/histogram)
 *   - RayidCurveChart + client k-slider → prec/rec + TP/FP/FN/TN (/models/{id}/curve)
 *   - feature importance with PRETTY + RAW names (Bug B prettifier, /models/{id})
 *   - PredictedList (/models/{id}/predictions?k=)
 * Each sub-read is independent (useAsync); a missing one renders its own state.
 */
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { prettyFeature } from '../api/transforms'
import { useExperiment } from '../hooks/useExperiment'
import { ScoreDistributionChart } from './ScoreDistributionChart'
import { RayidCurveChart } from './RayidCurveChart'
import { PredictedList } from './PredictedList'

interface Props {
  modelId: number
  /** A short human label for the header (e.g. "RF · depth 3 @ 2015-07"). */
  label: string
  onClose: () => void
}

export function ModelSheet({ modelId, label, onClose }: Props) {
  const { choice, k } = useExperiment()
  const card = useAsync(() => api.model(modelId), [modelId])
  const histo = useAsync(() => api.modelHistogram(modelId), [modelId])
  const curve = useAsync(() => api.modelCurve(modelId), [modelId])
  // Predicted-list depth follows the selection k (fraction → top-k absolute is
  // resolved server-side; here we pass a sensible absolute cap derived from k).
  const topK = Math.max(10, Math.round(k * 200))
  const preds = useAsync(() => api.modelPredictions(modelId, topK), [modelId, topK])

  const features = card.data?.feature_importances ?? []
  const maxImp = features.reduce((m, f) => Math.max(m, f.feature_importance), 0) || 1

  return (
    <>
      <div className="sheet-backdrop" onClick={onClose} />
      <aside className="sheet" role="dialog" aria-label={`model ${modelId}`}>
        <div className="sh">
          <div>
            <h3>{label}</h3>
            <div className="sub mono">
              model {modelId}
              {card.data?.model_group_id != null ? ` · group ${card.data.model_group_id}` : ''} ·{' '}
              {choice.metric}
              {choice.parameter}
            </div>
          </div>
          <button type="button" className="close" onClick={onClose} aria-label="close">
            ×
          </button>
        </div>

        <section>
          <h4>score distribution</h4>
          {histo.data ? (
            <ScoreDistributionChart bins={histo.data} />
          ) : (
            <div className="muted" style={{ fontSize: 11 }}>loading histogram…</div>
          )}
        </section>

        <section>
          <h4>Rayid curve · k-slider</h4>
          {curve.data ? (
            <RayidCurveChart curve={curve.data} initialPct={k} />
          ) : (
            <div className="muted" style={{ fontSize: 11 }}>loading curve…</div>
          )}
        </section>

        <section>
          <h4>feature importance · pretty + raw (Bug B fix)</h4>
          {card.loading ? (
            <div className="muted" style={{ fontSize: 11 }}>loading…</div>
          ) : features.length === 0 ? (
            <div className="muted" style={{ fontSize: 11 }}>no feature importances persisted</div>
          ) : (
            <div className="featlist">
              {features.map((f) => {
                const { pretty, raw } = prettyFeature(f.feature)
                return (
                  <div className="featrow" key={f.feature}>
                    <div>
                      <div className="pretty">{pretty}</div>
                      <div className="rawsub">{raw}</div>
                    </div>
                    <div className="imp">{f.feature_importance.toFixed(3)}</div>
                    <div
                      className="bar"
                      style={{ width: `${(f.feature_importance / maxImp) * 100}%` }}
                    />
                  </div>
                )
              })}
            </div>
          )}
        </section>

        <section>
          <h4>top predictions (k = {topK})</h4>
          {preds.data ? (
            <PredictedList data={preds.data} />
          ) : (
            <div className="muted" style={{ fontSize: 11 }}>loading predictions…</div>
          )}
        </section>
      </aside>
    </>
  )
}
