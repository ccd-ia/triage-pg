/*
 * ModelDetail drill-down (spec §1, §6) — for the selected model: feature
 * importances + per-split evaluations, driven by the selected model_id.
 * (Individual-prediction importances deferred to a future version, §9.)
 *
 * Reconciled to routes.py: GET /models/{id} returns {model_id,
 * feature_importances:[{feature, feature_importance, ...}], evaluations:[long
 * format rows]}. No model_group_id / label / per_split — the human label comes
 * from the parent's selection state, and per-split rows are folded client-side.
 */
import type { ModelDetailResponse } from '../api/types'
import { perSplitEvals } from '../api/transforms'

function fmt3(x: number | null | undefined): string {
  return x == null ? '—' : x.toFixed(3)
}

/** Find a metricKey present in the per-split data that matches a wanted prefix. */
function findMetricKey(splits: ReturnType<typeof perSplitEvals>, want: string): string | undefined {
  for (const s of splits) {
    for (const k of Object.keys(s.metrics)) {
      if (k.startsWith(want) || k.includes(want)) return k
    }
  }
  return undefined
}

export function ModelDetail({ data, label }: { data: ModelDetailResponse; label: string }) {
  const splits = perSplitEvals(data.evaluations)
  const precKey = findMetricKey(splits, 'precision@') ?? findMetricKey(splits, 'precision')
  const aucKey = findMetricKey(splits, 'auc')

  return (
    <div className="drill">
      <div className="ph">
        <b>
          ▸ Model detail · {label} (model {data.model_id})
          <span className="driven"> ⟵ selected</span>
        </b>
        <span className="src">feature_importances · evaluations</span>
      </div>
      <div className="twocol">
        <div>
          <h3 className="k">feature importances · triage.feature_importances</h3>
          <table>
            <thead>
              <tr>
                <th>feature</th>
                <th className="num">importance</th>
              </tr>
            </thead>
            <tbody>
              {data.feature_importances.map((f) => {
                const low = f.feature_importance < 0.05
                return (
                  <tr key={f.feature}>
                    <td className={`mono${low ? ' muted' : ''}`}>{f.feature}</td>
                    <td className={`num${low ? ' muted' : ''}`}>{f.feature_importance.toFixed(3)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <div>
          <h3 className="k">per-split evaluations · triage.evaluations</h3>
          <table>
            <thead>
              <tr>
                <th>as_of_date</th>
                <th className="num">{precKey ?? 'p@10%'}</th>
                <th className="num">{aucKey ?? 'auc'}</th>
                <th className="num">n labeled</th>
              </tr>
            </thead>
            <tbody>
              {splits.map((row) => (
                <tr key={row.as_of_date}>
                  <td className="mono">{row.as_of_date}</td>
                  <td className="num">{fmt3(precKey ? row.metrics[precKey] : null)}</td>
                  <td className="num">{fmt3(aucKey ? row.metrics[aucKey] : null)}</td>
                  <td className="num">{row.num_labeled?.toLocaleString('en-US') ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="muted" style={{ fontSize: 10, marginTop: 6 }}>
            no individual-prediction importances in v1 (deferred)
          </div>
        </div>
      </div>
    </div>
  )
}
