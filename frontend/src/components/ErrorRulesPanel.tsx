/*
 * ErrorRulesPanel — WHERE does this model fail (plan P5). Thin read over
 * triage.error_analysis (persisted by `triage postmodel error-tree`): each row is a
 * leaf of a shallow tree fitted at the top-k cut — a human-readable rule with its
 * support and error rate. fp = wrong flags within the top-k; fn = missed positives
 * among the passed-over; confusion = TP/FP/FN/TN over every labeled row (migration
 * 0027). The kind tab fetches that kind from the route, so a long fp list cannot crowd
 * the other kinds out of the route's row limit.
 *
 * The "suspiciously right" chip reads triage.error_rules_suspicious through the same
 * route (?suspicious=true): high-support leaves with almost no error, each with its
 * lift over the model's precision@. Which leaves qualify is decided in SQL; the panel
 * only shows the count and the rows. Diagnostic only, never a score modifier.
 */
import { useState } from 'react'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { ErrorKind } from '../api/types'
import { isEmpty } from '../api/types'

const KINDS: { kind: ErrorKind; label: string; title: string }[] = [
  { kind: 'fp', label: 'FP · wrong flags', title: 'within the top-k: where the model over-selects' },
  { kind: 'fn', label: 'FN · missed need', title: 'among the passed-over: where the model misses positives' },
  {
    kind: 'confusion',
    label: 'Confusion · all four',
    title: 'every labeled row, TP/FP/FN/TN: the first splits mostly redraw the cut itself',
  },
]

/** " · tp 39 · fp 1" — the leaf's class counts, in the route's key order ('' when none). */
function countsSuffix(counts: Record<string, number> | null): string {
  return Object.entries(counts ?? {})
    .map(([c, n]) => ` · ${c} ${n}`)
    .join('')
}

export function ErrorRulesPanel({ modelId }: { modelId: number }) {
  const [kind, setKind] = useState<ErrorKind>('fp')
  const [showSuspicious, setShowSuspicious] = useState(false)
  const rules = useAsync(() => api.modelErrorRules(modelId, kind), [modelId, kind])
  const suspicious = useAsync(() => api.modelSuspiciousRules(modelId), [modelId])
  const nSuspicious = suspicious.data?.length ?? 0

  return (
    <>
      <div style={{ display: 'flex', gap: 6, marginBottom: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        {KINDS.map((k) => (
          <button
            key={k.kind}
            type="button"
            title={k.title}
            className={`seg${!showSuspicious && kind === k.kind ? ' active' : ''}`}
            onClick={() => {
              setKind(k.kind)
              setShowSuspicious(false)
            }}
          >
            {k.label}
          </button>
        ))}
        <button
          type="button"
          className={`badge ${nSuspicious > 0 ? 'b-build' : 'b-aud'}`}
          style={{ cursor: nSuspicious > 0 ? 'pointer' : 'default', opacity: nSuspicious > 0 ? 1 : 0.6 }}
          disabled={nSuspicious === 0}
          aria-pressed={showSuspicious}
          title={
            suspicious.error
              ? `failed to load: ${suspicious.error.message}`
              : 'high-support leaves with almost no error — a leakage signature as often as a real signal'
          }
          onClick={() => setShowSuspicious((v) => !v)}
        >
          suspiciously right · {suspicious.error ? 'unavailable' : suspicious.data ? nSuspicious : '…'}
        </button>
      </div>

      {showSuspicious ? (
        <div className="featlist">
          {(suspicious.data ?? []).map((r) => (
            <div
              className="featrow"
              key={`${r.split_kind}-${r.parameter}-${r.as_of_date}-${r.error_kind}-${r.rule_id}`}
            >
              <div>
                <div className="pretty mono" style={{ fontSize: 11 }}>{r.rule}</div>
                <div className="rawsub">
                  {r.as_of_date} · {r.error_kind} · @{r.parameter} · matches {r.n_matched}
                  {r.tp_share != null ? ` · TP share ${(r.tp_share * 100).toFixed(0)}%` : ''}
                </div>
              </div>
              <div className="imp" title="1 − error rate over the model's precision@ at this date and cut">
                {r.lift != null ? `${r.lift.toFixed(2)}× lift` : 'lift —'}
              </div>
              <div className="bar" style={{ width: `${(r.tp_share ?? 0) * 100}%` }} />
            </div>
          ))}
        </div>
      ) : !rules.data ? (
        <div className="muted" style={{ fontSize: 11 }}>
          {rules.error ? `failed to load error rules: ${rules.error.message}` : 'loading error rules…'}
        </div>
      ) : isEmpty(rules.data) ? (
        <div className="muted" style={{ fontSize: 11 }}>
          {rules.data.reason} — {rules.data.hint}
        </div>
      ) : (
        <div className="featlist">
          {rules.data.map((r, i) => (
            <div className="featrow" key={`${r.as_of_date}-${r.error_kind}-${i}`}>
              <div>
                <div className="pretty mono" style={{ fontSize: 11 }}>{r.rule}</div>
                <div className="rawsub">
                  {r.as_of_date} · matches {r.n_matched} · {r.n_errors} error(s)
                  {countsSuffix(r.class_counts)}
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
