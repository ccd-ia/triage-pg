/*
 * ErrorRecurrencePanel — which error rules recur across a model group's test dates
 * (migration 0027). Thin read over /model-groups/{id}/error-recurrence, i.e.
 * triage.error_rule_recurrence: rules are matched by signature (features and split
 * directions, no thresholds), so a rule on most dates is a stable failure mode and a
 * candidate for a missing feature.
 *
 * The kinds offered are the group's problem type's (api/errorKinds.ts, the model card's
 * copy): a regression group reads its abs_error and beats_baseline leaves, which carry no
 * top-k cut (parameter '' and `baseline:<group>`). The cut defaults to the one analysed
 * most recently for the kind; the route lists the others, and picking another kind reads
 * that kind's own latest cut. An abs_error rate is a multiple of the population MAE, so
 * its mean is shown as one (2.50×); the other kinds' rates are shares. Counts and means
 * come from the route as they are.
 */
import { useState } from 'react'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { ErrorKind, ProblemType } from '../api/types'
import { isEmpty } from '../api/types'
import { kindsOf } from '../api/errorKinds'

/** A cut as the CLI prints it: an abs_error tree has none. */
function cutText(parameter: string): string {
  return parameter ? `@${parameter}` : 'no cut'
}

export function ErrorRecurrencePanel({
  groupId,
  problemType,
}: {
  groupId: number
  problemType: ProblemType | null
}) {
  const kinds = kindsOf(problemType)
  const [picked, setPicked] = useState<ErrorKind>(kinds[0].kind)
  // a kind the type does not have falls back to the type's first
  const kind = kinds.some((k) => k.kind === picked) ? picked : kinds[0].kind
  const [parameter, setParameter] = useState<string | undefined>(undefined)
  const recKey = `${groupId}#${kind}#${parameter ?? ''}`
  const rec = useAsync(
    async () => ({
      key: recKey,
      body: await api.modelGroupErrorRecurrence(groupId, { parameter, kind }),
    }),
    [recKey],
  )
  // a response for another kind or cut is never drawn under this tab
  const data = rec.data?.key === recKey ? rec.data.body : undefined

  // the kind tabs stay in the empty state: a regression_ranking group with abs_error trees
  // and no fp tree must still reach them
  const tabs = (
    <div style={{ display: 'flex', gap: 6, marginBottom: 6, flexWrap: 'wrap', alignItems: 'center' }}>
      {kinds.map((k) => (
        <button
          key={k.kind}
          type="button"
          title={k.title}
          className={`seg${kind === k.kind ? ' active' : ''}`}
          onClick={() => {
            setPicked(k.kind)
            // a cut is one kind's: the next kind reads its own latest
            setParameter(undefined)
          }}
        >
          {k.label}
        </button>
      ))}
      {data && !isEmpty(data) ? (
        data.parameters.length > 1 ? (
          <select
            className="splitsel"
            style={{ width: 'auto' }}
            aria-label="cut"
            value={data.parameter}
            onChange={(e) => setParameter(e.target.value)}
          >
            {data.parameters.map((p) => (
              <option key={p} value={p}>{cutText(p)}</option>
            ))}
          </select>
        ) : (
          <span className="muted mono" style={{ fontSize: 11 }}>{cutText(data.parameter)}</span>
        )
      ) : null}
    </div>
  )

  if (rec.error) {
    return (
      <>
        {tabs}
        <div className="banner err">Failed to load rule recurrence: {rec.error.message}</div>
      </>
    )
  }
  if (!data) {
    return (
      <>
        {tabs}
        <div className="muted" style={{ fontSize: 11 }}>loading rule recurrence…</div>
      </>
    )
  }
  if (isEmpty(data)) {
    return (
      <>
        {tabs}
        <div className="muted" style={{ fontSize: 11 }}>
          {data.reason} — {data.hint}
        </div>
      </>
    )
  }
  const body = data
  const ratio = body.error_kind === 'abs_error'
  return (
    <>
      {tabs}
      {body.rules.length === 0 ? (
        <div className="muted" style={{ fontSize: 11 }}>
          no {body.error_kind} rules with a signature at {cutText(body.parameter)} — re-run `triage
          postmodel error-tree` on this group's models.
        </div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>rule (example)</th>
              <th className="num">dates</th>
              <th
                className="num"
                title={ratio ? 'each leaf’s mean |e| over the population MAE, averaged' : undefined}
              >
                {ratio ? 'mean |e| / MAE' : 'mean error'}
              </th>
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
                <td className="num mono">
                  {ratio ? `${r.mean_error_rate.toFixed(2)}×` : `${(r.mean_error_rate * 100).toFixed(0)}%`}
                </td>
                <td className="num mono">{r.mean_support.toFixed(0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
