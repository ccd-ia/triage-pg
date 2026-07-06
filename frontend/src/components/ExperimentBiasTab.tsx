/*
 * ExperimentBiasTab — the Aequitas-style fairness views (Q9) for the selected
 * model, scoped to the experiment (/experiments/{hash}/bias?model_id=):
 *   0. fairness-tree wizard      — routes attention to the metric that matters
 *   1. group-metric table        — one row per protected group; the full 0014 set
 *                                  (selection_rate/precision/tpr/fpr/fnr/fdr/for/npv),
 *                                  focus-family columns highlighted
 *   2. disparity bars            — the FOCUS metric's disparity vs the reference
 *   3. fairness pass/fail grid   — the SQL verdict (passes_fairness at τ, migration
 *                                  0014); falls back to the τ=0.8 rule on pre-0014 rows
 * Long-format bias rows carry per-metric disparity + verdict from the SQL group-bys
 * (ADR-0007); we group them client-side. Empty-state when no protected_groups.
 */
import { useState } from 'react'
import type { BiasConfigEcho, ExpBiasResponse } from '../api/types'
import { isEmpty } from '../api/types'
import {
  fairnessFocus,
  groupBias,
  type BiasGroupRow,
  type FairnessFocus,
} from '../api/transforms'
import { EmptyPanel } from './EmptyPanel'
import { FairnessTreeWizard } from './FairnessTreeWizard'

function fmt(x: number | undefined): string {
  return x == null ? '—' : x.toFixed(2)
}

/** Exactly what the SQL emits (migration 0014), in reading order; unknown extras last. */
const CANONICAL = ['selection_rate', 'precision', 'tpr', 'fpr', 'fnr', 'fdr', 'for', 'npv']

function metricColumns(rows: BiasGroupRow[]): string[] {
  const seen = new Set<string>()
  for (const r of rows) for (const k of Object.keys(r.metrics)) seen.add(k)
  seen.delete('group_size')
  seen.delete('num_selected')
  const ordered = CANONICAL.filter((c) => seen.has(c))
  for (const k of seen) if (!ordered.includes(k)) ordered.push(k)
  return ordered
}

/** SQL verdict when present (0014); else the τ fallback rule on the raw disparity. */
function passes(row: BiasGroupRow, metric: string, tau: number): boolean {
  const verdict = row.passes[metric]
  if (verdict != null) return verdict
  const d = row.disparities[metric]
  if (d == null) return true // no disparity -> no verdict; don't render a false FAIL
  return d >= tau && d <= 1 / tau
}

export function ExperimentBiasTab({
  data,
  modelLabel,
  biasConfig,
}: {
  data: ExpBiasResponse
  modelLabel: string
  biasConfig?: BiasConfigEcho | null
}) {
  const seeded = biasConfig?.intervention ?? null
  const [focus, setFocus] = useState<FairnessFocus>(() =>
    fairnessFocus(seeded ?? 'punitive', true),
  )
  if (isEmpty(data)) {
    return <EmptyPanel reason={data.reason} hint={data.hint} />
  }

  const rows = groupBias(data)
  const attr = rows[0]?.attribute_name ?? 'group'
  const cols = metricColumns(rows)
  const ref =
    rows.find((r) => r.disparity == null || r.disparity === 1)?.attribute_value ??
    rows[0]?.ref_group_value
  const tau = rows.find((r) => r.tau != null)?.tau ?? biasConfig?.tau ?? 0.8
  const focusMetric = cols.includes(focus.primary) ? focus.primary : (cols[0] ?? 'selection_rate')

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span className="badge b-aud">fairness views · ref group: {ref ?? '—'}</span>
        <span className="src">triage.bias_metrics (group-bys, ADR-0007/0014) · {modelLabel}</span>
      </div>

      {/* View 0: the fairness tree routes attention (never hides) */}
      <FairnessTreeWizard intervention={seeded} onFocus={setFocus} />

      {/* View 1: group-metric table, focus family highlighted */}
      <h3 className="k">1 · group-metric table</h3>
      <table>
        <thead>
          <tr>
            <th>group ({attr})</th>
            {cols.map((c) => (
              <th
                key={c}
                className="num"
                style={focus.family.includes(c) ? { background: 'var(--chip, #eef2ff)' } : undefined}
                title={focus.family.includes(c) ? focus.rationale : undefined}
              >
                {c === focusMetric ? '▸ ' : ''}
                {c.toUpperCase()}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.attribute_name}-${r.attribute_value}`}>
              <td>{r.attribute_value}</td>
              {cols.map((c) => (
                <td
                  key={c}
                  className="num"
                  style={focus.family.includes(c) ? { background: 'var(--chip, #eef2ff)' } : undefined}
                >
                  {fmt(r.metrics[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>

      <div className="twocol" style={{ marginTop: 14 }}>
        {/* View 2: the focus metric's disparity vs reference group */}
        <div>
          <h3 className="k">
            2 · {focusMetric.toUpperCase()} disparity vs reference (1.0 = parity)
          </h3>
          <div className="disp">
            {rows.map((r) => {
              const d = r.disparities[focusMetric] ?? 1
              const under = d < 1
              // map ratio (0..2) onto a bar centered at 0.5 of the track
              const width = Math.min(50, Math.abs(d - 1) * 50)
              return (
                <div className="dr" key={`d-${r.attribute_value}`}>
                  <span className="mono">{r.attribute_value}</span>
                  <span className="track">
                    <span className="mid" />
                    <span
                      className={`fill${under ? ' under' : ''}`}
                      style={under ? { right: '50%', left: 'auto', width: `${width}%` } : { left: '50%', width: `${width}%` }}
                    />
                  </span>
                  <span className="num">
                    {r.disparities[focusMetric] == null ? '—' : d.toFixed(2)}
                  </span>
                </div>
              )
            })}
          </div>
        </div>

        {/* View 3: SQL fairness verdict at τ for the focus metric */}
        <div>
          <h3 className="k">
            3 · fairness pass/fail — {focusMetric.toUpperCase()} at τ={tau}
          </h3>
          <div className="fairgrid" style={{ gridTemplateColumns: `repeat(${Math.min(3, rows.length)}, 1fr)` }}>
            {rows.map((r) => {
              const ok = passes(r, focusMetric, tau)
              return (
                <div key={`f-${r.attribute_value}`} className={`fc ${ok ? 'pass' : 'fail'}`}>
                  {r.attribute_value}
                  <div style={{ fontWeight: 700, marginTop: 3 }}>{ok ? 'PASS' : 'FAIL'}</div>
                </div>
              )
            })}
          </div>
          <div className="muted" style={{ fontSize: 10, marginTop: 8 }}>
            passes when disparity ∈ [{tau}, {(1 / tau).toFixed(2)}] vs the reference group
            (SQL verdict, migration 0014; τ from bias_config)
          </div>
        </div>
      </div>
    </>
  )
}
