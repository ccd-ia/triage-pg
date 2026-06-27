/*
 * ModelSheet — the model card as a right side-sheet (Option 4). Opened from a grid
 * cell, a model-groups row, or a leaderboard row. Composes:
 *   - a SPLIT SELECTOR over the model group's models (which trained model is shown —
 *     answers "which model opens"; surfaces /model-groups/{id}, follow-up #2)
 *   - ScoreDistributionChart (/models/{id}/histogram)
 *   - RayidCurveChart + client k-slider (/models/{id}/curve)
 *   - feature importance (top 20 + "View all" modal), PRETTY + RAW names
 *   - PredictedList (top 20 + "View all" modal); each entity opens the EntityDrawer
 */
import { useMemo, useState } from 'react'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { prettyFeature } from '../api/transforms'
import { useExperiment } from '../hooks/useExperiment'
import { isEmpty, type FeatureImportanceRow } from '../api/types'
import { ScoreDistributionChart } from './ScoreDistributionChart'
import { RayidCurveChart } from './RayidCurveChart'
import { PredictedList } from './PredictedList'
import { predictionHead, predictionRow } from './predictionRows'
import { FullListModal } from './FullListModal'
import { EntityDrawer } from './EntityDrawer'

const INLINE = 20

interface Props {
  modelId: number
  label: string
  modelGroupId?: number | null
  experimentHash?: string
  groupLabelOf?: (gid: number) => string
  onClose: () => void
}

export function ModelSheet({ modelId, label, modelGroupId, experimentHash, groupLabelOf, onClose }: Props) {
  const { choice, k } = useExperiment()
  // The trained model actually shown — starts at the opened model, swappable via the
  // split selector. Reset when the opened model changes (adjust-state-during-render
  // pattern, so there's no setState-in-effect cascade).
  const [activeId, setActiveId] = useState(modelId)
  const [openedId, setOpenedId] = useState(modelId)
  if (modelId !== openedId) {
    setOpenedId(modelId)
    setActiveId(modelId)
  }

  // Sub-sheets/modals
  const [entityId, setEntityId] = useState<number | null>(null)
  const [showAllPreds, setShowAllPreds] = useState(false)
  const [showAllFeats, setShowAllFeats] = useState(false)

  const group = useAsync(
    () =>
      modelGroupId != null
        ? api.modelGroup(modelGroupId, undefined, undefined, experimentHash)
        : Promise.resolve(undefined),
    [modelGroupId, experimentHash],
  )
  const card = useAsync(() => api.model(activeId), [activeId])
  const histo = useAsync(() => api.modelHistogram(activeId), [activeId])
  const curve = useAsync(() => api.modelCurve(activeId), [activeId])
  const preds = useAsync(() => api.modelPredictions(activeId, { limit: INLINE }), [activeId])

  const features = useMemo(() => card.data?.feature_importances ?? [], [card.data])
  const maxImp = features.reduce((m, f) => Math.max(m, f.feature_importance), 0) || 1
  const topFeatures = features.slice(0, INLINE)

  const models = group.data?.models ?? []
  const activeModel = models.find((m) => m.model_id === activeId)

  // Dynamic header: the group name + the ACTIVE model's test period, so it stays correct
  // when you change splits (the static `label` is only the originally-opened split).
  const groupName =
    groupLabelOf && modelGroupId != null
      ? groupLabelOf(modelGroupId)
      : label.split(' @ ')[0].split(' · m')[0]
  const activePeriod = activeModel?.test_as_of ?? activeModel?.train_end_time ?? null
  const header = activePeriod ? `${groupName} @ ${activePeriod.slice(0, 7)}` : label

  return (
    <>
      <div className="sheet-backdrop" onClick={onClose} />
      <aside className="sheet" role="dialog" aria-label={`model ${activeId}`}>
        <div className="sh">
          <div>
            <h3>{header}</h3>
            <div className="sub mono">
              model {activeId}
              {card.data?.model_group_id != null ? ` · group ${card.data.model_group_id}` : ''} ·{' '}
              {choice.metric}
              {choice.parameter}
            </div>
          </div>
          <button type="button" className="close" onClick={onClose} aria-label="close">×</button>
        </div>

        {models.length > 1 ? (
          <section>
            <h4>split (model group · {models.length} models)</h4>
            <select
              className="splitsel"
              value={activeId}
              onChange={(e) => setActiveId(Number(e.target.value))}
            >
              {models.map((m) => (
                <option key={m.model_id} value={m.model_id}>
                  {m.train_end_time ? `train ≤ ${m.train_end_time}` : `model ${m.model_id}`}
                  {m.test_as_of ? ` · test ${m.test_as_of}` : ''} · m{m.model_id}
                </option>
              ))}
            </select>
            {activeModel ? (
              <div className="muted" style={{ fontSize: 10.5, marginTop: 4 }}>
                trained through <b>{activeModel.train_end_time ?? '—'}</b>
                {activeModel.test_as_of ? (
                  <> · scored at <b>{activeModel.test_as_of}</b></>
                ) : null}
                {activeModel.training_label_timespan ? (
                  <> · label window {activeModel.training_label_timespan}</>
                ) : null}
              </div>
            ) : null}
          </section>
        ) : null}

        <section>
          <h4>score distribution</h4>
          {histo.data ? <ScoreDistributionChart bins={histo.data} /> : <div className="muted" style={{ fontSize: 11 }}>loading histogram…</div>}
        </section>

        <section>
          <h4>Rayid curve · k-slider</h4>
          {curve.data ? <RayidCurveChart curve={curve.data} initialPct={k} /> : <div className="muted" style={{ fontSize: 11 }}>loading curve…</div>}
        </section>

        <section>
          <h4>feature importance · pretty + raw</h4>
          {card.loading ? (
            <div className="muted" style={{ fontSize: 11 }}>loading…</div>
          ) : features.length === 0 ? (
            <div className="muted" style={{ fontSize: 11 }}>no feature importances persisted</div>
          ) : (
            <>
              {/* What the number means depends on the estimator: Gini impurity for trees,
                  |coefficient| for linear models (β + odds-ratio shown for logistic). */}
              {(() => {
                const kind = features[0]?.importance_kind
                if (kind === 'gini')
                  return <div className="muted" style={{ fontSize: 10.5, marginBottom: 6 }}>Gini importance (mean impurity decrease) — unsigned.</div>
                if (kind === 'coef' || kind === 'abs_coef')
                  return <div className="muted" style={{ fontSize: 10.5, marginBottom: 6 }}>|β| on scaled features; <b>β</b> = signed coefficient, <b>OR</b> = odds-ratio exp(β).</div>
                return null
              })()}
              <div className="featlist">
                {topFeatures.map((f) => {
                  const { pretty, raw } = prettyFeature(f.feature)
                  const isCoef = f.importance_kind === 'coef' || f.importance_kind === 'abs_coef'
                  return (
                    <div className="featrow" key={f.feature}>
                      <div>
                        <div className="pretty">{pretty}</div>
                        <div className="rawsub">{raw}</div>
                        {isCoef && f.signed_value != null ? (
                          <div className="rawsub">
                            β {f.signed_value.toFixed(4)}
                            {f.odds_ratio != null ? ` · OR ${f.odds_ratio.toFixed(4)}` : ''}
                          </div>
                        ) : null}
                      </div>
                      <div className="imp">{f.feature_importance.toFixed(3)}</div>
                      <div className="bar" style={{ width: `${(f.feature_importance / maxImp) * 100}%` }} />
                    </div>
                  )
                })}
              </div>
              {features.length > INLINE ? (
                <button type="button" className="seg" style={{ marginTop: 8 }} onClick={() => setShowAllFeats(true)}>
                  View all {features.length} features →
                </button>
              ) : null}
            </>
          )}
        </section>

        <section>
          <h4>top predictions</h4>
          {preds.data ? (
            <PredictedList
              data={preds.data}
              onEntityClick={(id) => setEntityId(id)}
              onViewAll={() => setShowAllPreds(true)}
            />
          ) : (
            <div className="muted" style={{ fontSize: 11 }}>loading predictions…</div>
          )}
        </section>
      </aside>

      {showAllPreds && preds.data && !isEmpty(preds.data) ? (
        <FullListModal
          title={`predictions · ${label}`}
          total={preds.data.total}
          loadPage={async (offset, limit) => {
            const page = await api.modelPredictions(activeId, { offset, limit })
            return isEmpty(page) ? [] : page.rows
          }}
          head={predictionHead()}
          row={(p) => predictionRow(p, (id) => setEntityId(id))}
          onClose={() => setShowAllPreds(false)}
        />
      ) : null}

      {showAllFeats ? (
        <FullListModal<FeatureImportanceRow>
          title={`feature importance · ${label}`}
          total={features.length}
          loadPage={(offset, limit) => Promise.resolve(features.slice(offset, offset + limit))}
          head={<tr><th>feature</th><th className="num">importance</th><th className="num">rank</th></tr>}
          row={(f) => {
            const { pretty, raw } = prettyFeature(f.feature)
            return (
              <tr key={f.feature}>
                <td><div className="pretty">{pretty}</div><div className="rawsub">{raw}</div></td>
                <td className="num">{f.feature_importance.toFixed(4)}</td>
                <td className="num">{f.rank_abs ?? '—'}</td>
              </tr>
            )
          }}
          onClose={() => setShowAllFeats(false)}
        />
      ) : null}

      {entityId != null ? (
        <EntityDrawer
          entityId={entityId}
          experimentHash={experimentHash}
          defaultGroupId={modelGroupId ?? card.data?.model_group_id ?? null}
          groupLabelOf={groupLabelOf}
          onClose={() => setEntityId(null)}
        />
      ) : null}
    </>
  )
}
