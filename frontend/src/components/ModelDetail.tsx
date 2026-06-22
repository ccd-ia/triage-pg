/*
 * ModelDetail drill-down (spec §1, §6) — for the selected model: feature
 * importances + per-split evaluations, driven by the selected model_id.
 * (Individual-prediction importances deferred to a future version, §9.)
 */
import type { ModelDetailResponse } from '../api/types'

function fmt3(x: number | null): string {
  return x == null ? '—' : x.toFixed(3)
}

export function ModelDetail({ data }: { data: ModelDetailResponse }) {
  return (
    <div className="drill">
      <div className="ph">
        <b>
          ▸ Model detail · {data.label} (model_group {data.model_group_id} → model {data.model_id})
          <span className="driven"> ⟵ selected</span>
        </b>
        <span className="src">model_groups → models · feature_importances · evaluations</span>
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
                const low = f.importance < 0.05
                return (
                  <tr key={f.feature}>
                    <td className={`mono${low ? ' muted' : ''}`}>{f.feature}</td>
                    <td className={`num${low ? ' muted' : ''}`}>{f.importance.toFixed(3)}</td>
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
                <th className="num">p@10%</th>
                <th className="num">auc</th>
                <th className="num">n test</th>
              </tr>
            </thead>
            <tbody>
              {data.per_split.map((row) => {
                const building = row.building
                return (
                  <tr key={row.as_of_date}>
                    <td className={`mono${building ? ' muted' : ''}`}>
                      {row.as_of_date}
                      {building ? ' ◐' : ''}
                    </td>
                    <td className={`num${building ? ' muted' : ''}`}>
                      {fmt3(row.metrics['precision@10_pct'] ?? null)}
                    </td>
                    <td className={`num${building ? ' muted' : ''}`}>
                      {fmt3(row.metrics['auc'] ?? null)}
                    </td>
                    <td className={`num${building ? ' muted' : ''}`}>
                      {building ? 'building' : (row.n_test?.toLocaleString('en-US') ?? '—')}
                    </td>
                  </tr>
                )
              })}
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
