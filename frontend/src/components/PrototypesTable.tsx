/*
 * PrototypesTable — the most typical entity of each class at the cut (TP, FP, FN, TN),
 * as `triage postmodel prototypes` persists them (/models/{id}/prototypes, migration
 * 0037): on the latest date and cut persisted, the entities nearest their class centroid
 * in z-scored feature space, each with its largest deviations from that centroid in
 * standard deviations. Descriptive: nothing is fitted. An entity opens its drawer.
 * Classification and regression_ranking models.
 */
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { fmtNum } from '../api/format'
import { prettyFeature } from '../api/transforms'
import type { PrototypeRow } from '../api/types'
import { AsyncPanel } from './AsyncPanel'

const CLASS_LABEL: Record<PrototypeRow['class'], string> = {
  tp: 'TP · flagged, positive',
  fp: 'FP · flagged, negative',
  fn: 'FN · passed over, positive',
  tn: 'TN · passed over, negative',
}

/** How many deviations each prototype lists (10 are persisted). */
const DEVIATIONS = 3

export function PrototypesTable({
  modelId,
  onEntity,
}: {
  modelId: number
  onEntity?: (entityId: number) => void
}) {
  const prototypes = useAsync(() => api.modelPrototypes(modelId), [modelId])
  return (
    <AsyncPanel state={prototypes} what="the prototypes">
      {(rows) => {
        const last = rows.reduce((d, r) => (r.as_of_date > d ? r.as_of_date : d), rows[0].as_of_date)
        const cut = rows.find((r) => r.as_of_date === last)?.parameter
        const shown = rows.filter((r) => r.as_of_date === last && r.parameter === cut)
        return (
          <>
            <table className="compact">
              <thead>
                <tr>
                  <th>class</th>
                  <th>entity</th>
                  <th className="num" title="Euclidean distance to the class centroid, z units">distance</th>
                  <th>largest deviations (σ from the centroid)</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={`${r.class}-${r.rank}`}>
                    <td>
                      {CLASS_LABEL[r.class]}
                      <div className="muted" style={{ fontSize: 9.5 }}>
                        #{r.rank} of {r.class_size}
                      </div>
                    </td>
                    <td className="mono">
                      {onEntity ? (
                        <button type="button" className="seg" onClick={() => onEntity(r.entity_id)}>
                          {r.entity_id}
                        </button>
                      ) : (
                        r.entity_id
                      )}
                    </td>
                    <td className="num">{fmtNum(r.distance, 2)}</td>
                    <td style={{ fontSize: 10.5 }}>
                      {r.deviations.slice(0, DEVIATIONS).map((d) => (
                        <div key={d.feature} title={`${d.feature} = ${fmtNum(d.value, 4)}`}>
                          {prettyFeature(d.feature).pretty}{' '}
                          <span className="mono">
                            {d.deviation >= 0 ? '+' : ''}
                            {fmtNum(d.deviation, 2)}σ
                          </span>
                        </div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="muted" style={{ fontSize: 10.5, marginTop: 4 }}>
              at {last} · cut {cut}
            </div>
          </>
        )
      }}
    </AsyncPanel>
  )
}
