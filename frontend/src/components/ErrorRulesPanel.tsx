/*
 * ErrorRulesPanel — WHERE does this model fail (plan P5). Thin read over
 * triage.error_analysis (persisted by `triage postmodel error-tree`): each row is a
 * leaf of a shallow tree fitted on the model's mistakes — a human-readable rule with
 * its support and error rate. fp = wrong flags within the top-k; fn = missed
 * positives among the passed-over. Diagnostic only, never a score modifier.
 */
import { useState } from 'react'
import type { ErrorRulesResponse } from '../api/types'
import { isEmpty } from '../api/types'

export function ErrorRulesPanel({ data }: { data: ErrorRulesResponse }) {
  const [kind, setKind] = useState<'fp' | 'fn'>('fp')
  if (isEmpty(data)) {
    return (
      <div className="muted" style={{ fontSize: 11 }}>
        {data.reason} — {data.hint}
      </div>
    )
  }
  const rows = data.filter((r) => r.error_kind === kind)
  return (
    <>
      <div style={{ display: 'flex', gap: 6, marginBottom: 6 }}>
        <button
          type="button"
          className={`seg${kind === 'fp' ? ' active' : ''}`}
          onClick={() => setKind('fp')}
        >
          FP · wrong flags
        </button>
        <button
          type="button"
          className={`seg${kind === 'fn' ? ' active' : ''}`}
          onClick={() => setKind('fn')}
        >
          FN · missed need
        </button>
      </div>
      {rows.length === 0 ? (
        <div className="muted" style={{ fontSize: 11 }}>no {kind} rules persisted.</div>
      ) : (
        <div className="featlist">
          {rows.map((r, i) => (
            <div className="featrow" key={`${r.as_of_date}-${r.error_kind}-${i}`}>
              <div>
                <div className="pretty mono" style={{ fontSize: 11 }}>{r.rule}</div>
                <div className="rawsub">
                  {r.as_of_date} · matches {r.n_matched} · {r.n_errors} error(s)
                </div>
              </div>
              <div className="imp">{(r.error_rate * 100).toFixed(0)}%</div>
              <div className="bar" style={{ width: `${r.error_rate * 100}%` }} />
            </div>
          ))}
        </div>
      )}
    </>
  )
}
