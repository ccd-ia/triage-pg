/*
 * ExperimentBiasTab — the 3 Aequitas-style fairness views (Q9) for the selected
 * model, scoped to the experiment (/experiments/{hash}/bias?model_id=):
 *   1. group-metric table       — one row per protected group, metric columns
 *   2. disparity bars           — each group's disparity vs the reference group
 *   3. fairness pass/fail grid  — disparity within [0.8, 1.25] (the 80% rule)
 * Long-format bias rows carry ref_group_value + disparity from the SQL group-bys
 * (ADR-0007); we group them client-side. Empty-state when no protected_groups.
 */
import type { ExpBiasResponse } from '../api/types'
import { isEmpty } from '../api/types'
import { groupBias, type BiasGroupRow } from '../api/transforms'
import { EmptyPanel } from './EmptyPanel'

function fmt(x: number | undefined): string {
  return x == null ? '—' : x.toFixed(2)
}

function metricColumns(rows: BiasGroupRow[]): string[] {
  const seen = new Set<string>()
  for (const r of rows) for (const k of Object.keys(r.metrics)) seen.add(k)
  const preferred = ['tpr', 'fpr', 'ppv', 'fnr', 'fdr', 'precision', 'recall']
  const ordered: string[] = []
  for (const p of preferred) if (seen.has(p)) ordered.push(p)
  for (const k of seen) if (!ordered.includes(k)) ordered.push(k)
  return ordered
}

/** 80%-rule fairness: disparity within [0.8, 1.25] passes. */
function passes(disparity: number | null): boolean {
  if (disparity == null) return true
  return disparity >= 0.8 && disparity <= 1.25
}

export function ExperimentBiasTab({ data, modelLabel }: { data: ExpBiasResponse; modelLabel: string }) {
  if (isEmpty(data)) {
    return <EmptyPanel reason={data.reason} hint={data.hint} />
  }

  const rows = groupBias(data)
  const attr = rows[0]?.attribute_name ?? 'group'
  const cols = metricColumns(rows)
  const ref = rows.find((r) => r.disparity == null || r.disparity === 1)?.attribute_value ?? rows[0]?.ref_group_value

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span className="badge b-aud">3 Aequitas views · ref group: {ref ?? '—'}</span>
        <span className="src">triage.bias_metrics (group-bys, ADR-0007) · {modelLabel}</span>
      </div>

      {/* View 1: group-metric table */}
      <h3 className="k">1 · group-metric table</h3>
      <table>
        <thead>
          <tr>
            <th>group ({attr})</th>
            {cols.map((c) => (
              <th key={c} className="num">
                {c.toUpperCase()}
              </th>
            ))}
            <th className="num">disparity</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.attribute_name}-${r.attribute_value}`}>
              <td>{r.attribute_value}</td>
              {cols.map((c) => (
                <td key={c} className="num">
                  {fmt(r.metrics[c])}
                </td>
              ))}
              <td className="num">{r.disparity == null ? '1.00' : r.disparity.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="twocol" style={{ marginTop: 14 }}>
        {/* View 2: disparity bars vs reference group */}
        <div>
          <h3 className="k">2 · disparity vs reference (1.0 = parity)</h3>
          <div className="disp">
            {rows.map((r) => {
              const d = r.disparity ?? 1
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
                  <span className="num">{d.toFixed(2)}</span>
                </div>
              )
            })}
          </div>
        </div>

        {/* View 3: fairness pass/fail grid (80% rule) */}
        <div>
          <h3 className="k">3 · fairness pass/fail (80% rule)</h3>
          <div className="fairgrid" style={{ gridTemplateColumns: `repeat(${Math.min(3, rows.length)}, 1fr)` }}>
            {rows.map((r) => {
              const ok = passes(r.disparity)
              return (
                <div key={`f-${r.attribute_value}`} className={`fc ${ok ? 'pass' : 'fail'}`}>
                  {r.attribute_value}
                  <div style={{ fontWeight: 700, marginTop: 3 }}>{ok ? 'PASS' : 'FAIL'}</div>
                </div>
              )
            })}
          </div>
          <div className="muted" style={{ fontSize: 10, marginTop: 8 }}>
            passes when disparity ∈ [0.80, 1.25] vs the reference group
          </div>
        </div>
      </div>
    </>
  )
}
