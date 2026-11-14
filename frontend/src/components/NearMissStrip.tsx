/*
 * NearMissStrip — do the rows just below the cut differ from the rows inside it?
 * (/models/{id}/near-miss, migration 0037). Three bands of the latest scored date's
 * ranking: the selected top k, the near miss (the next k rows) and the rest, each with its
 * base rate over its labeled rows (positive = outcome > 0) and its mean score. A near-miss
 * base rate close to the selected one says the cut falls inside a block of similar rows;
 * the crosstabs "vs near miss" toggle shows how their features differ.
 * Classification and regression_ranking models.
 */
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { fmtNum, fmtPct } from '../api/format'
import type { NearMissBand } from '../api/types'
import { AsyncPanel } from './AsyncPanel'

const LABEL: Record<NearMissBand['band'], string> = {
  selected: 'selected',
  near_miss: 'near miss',
  rest: 'rest',
}

/** A top-k cut such as '100_abs' or '10_pct' (resolve_k's grammar). */
const CUT = /^\d+(\.\d+)?_(abs|pct)$/

export function NearMissStrip({ modelId, parameter }: { modelId: number; parameter?: string }) {
  const cut = parameter && CUT.test(parameter) ? parameter : undefined
  const bands = useAsync(() => api.modelNearMiss(modelId, cut), [modelId, cut])
  return (
    <AsyncPanel state={bands} what="the near-miss bands">
      {(rows) => {
        const last = rows.reduce((d, r) => (r.as_of_date > d ? r.as_of_date : d), rows[0].as_of_date)
        const shown = rows.filter((r) => r.as_of_date === last)
        return (
          <>
            <div className="bands">
              {shown.map((b) => (
                <div key={b.band} style={{ display: 'contents' }}>
                  <span title={b.first_rank == null ? 'no row' : `ranks ${b.first_rank}–${b.last_rank}`}>
                    {LABEL[b.band]}
                    <span className="muted">
                      {b.first_rank == null ? ' · empty' : ` · ${b.first_rank}–${b.last_rank}`}
                    </span>
                  </span>
                  <div className="track" title={`${b.n_positive} positive of ${b.n_labeled} labeled rows`}>
                    <div className={`fill ${b.band}`} style={{ width: `${(b.base_rate ?? 0) * 100}%` }} />
                  </div>
                  <span className="mono" title={`mean score ${fmtNum(b.mean_score, 4)} over ${b.n} rows`}>
                    {fmtPct(b.base_rate)}
                  </span>
                </div>
              ))}
            </div>
            <div className="muted" style={{ fontSize: 10.5, marginTop: 4 }}>
              base rate of each band&apos;s labeled rows at {last}
              {cut ? ` · cut ${cut}` : ' · cut 100_abs'}; the near miss holds the next k rows
            </div>
          </>
        )
      }}
    </AsyncPanel>
  )
}
