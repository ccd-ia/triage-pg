/*
 * ErrorRecurrencePanel — which error rules recur across a model group's test dates
 * (migration 0027). Thin read over /model-groups/{id}/error-recurrence, i.e.
 * triage.error_rule_recurrence: rules are matched by signature (features and split
 * directions, no thresholds), so a rule on most dates is a stable failure mode and a
 * candidate for a missing feature. The cut defaults to the one analysed most recently;
 * the route lists the others. Counts and means come from the route as they are.
 */
import { useState } from 'react'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { ErrorKind } from '../api/types'
import { isEmpty } from '../api/types'

const KINDS: ErrorKind[] = ['fp', 'fn', 'confusion']

export function ErrorRecurrencePanel({ groupId }: { groupId: number }) {
  const [kind, setKind] = useState<ErrorKind>('fp')
  const [parameter, setParameter] = useState<string | undefined>(undefined)
  const rec = useAsync(
    () => api.modelGroupErrorRecurrence(groupId, { parameter, kind }),
    [groupId, parameter, kind],
  )

  if (rec.error) {
    return <div className="banner err">Failed to load rule recurrence: {rec.error.message}</div>
  }
  if (!rec.data) {
    return <div className="muted" style={{ fontSize: 11 }}>loading rule recurrence…</div>
  }
  if (isEmpty(rec.data)) {
    return (
      <div className="muted" style={{ fontSize: 11 }}>
        {rec.data.reason} — {rec.data.hint}
      </div>
    )
  }
  const body = rec.data
  return (
    <>
      <div style={{ display: 'flex', gap: 6, marginBottom: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        {KINDS.map((k) => (
          <button
            key={k}
            type="button"
            className={`seg${body.error_kind === k ? ' active' : ''}`}
            onClick={() => setKind(k)}
          >
            {k}
          </button>
        ))}
        {body.parameters.length > 1 ? (
          <select
            className="splitsel"
            style={{ width: 'auto' }}
            value={body.parameter}
            onChange={(e) => setParameter(e.target.value)}
          >
            {body.parameters.map((p) => (
              <option key={p} value={p}>@{p}</option>
            ))}
          </select>
        ) : (
          <span className="muted mono" style={{ fontSize: 11 }}>@{body.parameter}</span>
        )}
      </div>
      {body.rules.length === 0 ? (
        <div className="muted" style={{ fontSize: 11 }}>
          no {body.error_kind} rules with a signature at {body.parameter} — re-run `triage postmodel
          error-tree` on this group's models.
        </div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>rule (example)</th>
              <th className="num">dates</th>
              <th className="num">mean error</th>
              <th className="num">mean n</th>
            </tr>
          </thead>
          <tbody>
            {body.rules.map((r) => (
              <tr key={r.signature}>
                <td>
                  <div className="mono" style={{ fontSize: 11 }}>{r.example_rule}</div>
                  <div className="rawsub mono">{r.signature || '(all)'}</div>
                </td>
                <td className="num mono">
                  {r.n_dates}/{r.n_dates_total}
                </td>
                <td className="num mono">{(r.mean_error_rate * 100).toFixed(0)}%</td>
                <td className="num mono">{r.mean_support.toFixed(0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
