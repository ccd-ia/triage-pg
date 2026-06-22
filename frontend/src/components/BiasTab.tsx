/*
 * BiasTab (spec §1 tab 4, §3.7) — fairness group-bys for the selected model;
 * live (fills per evaluated model/split). §3.7 empty-state when the experiment
 * has no protected_groups config.
 *
 * Reconciled to routes.py: GET /bias returns a BARE array of long-format rows
 * (one per attribute/value/metric), OR the empty envelope. The component groups
 * rows by (attribute, value) and renders whichever metrics are present.
 */
import type { BiasResponse } from '../api/types'
import { isEmpty } from '../api/types'
import { groupBias, type BiasGroupRow } from '../api/transforms'
import { EmptyPanel } from './EmptyPanel'

function fmt(x: number | undefined): string {
  return x == null ? '—' : x.toFixed(2)
}

/** Stable metric-column order: the common fairness metrics first, then the rest. */
function metricColumns(rows: BiasGroupRow[]): string[] {
  const seen = new Set<string>()
  for (const r of rows) for (const k of Object.keys(r.metrics)) seen.add(k)
  const preferred = ['tpr', 'fpr', 'ppv', 'fnr', 'fdr', 'precision', 'recall']
  const ordered: string[] = []
  for (const p of preferred) if (seen.has(p)) ordered.push(p)
  for (const k of seen) if (!ordered.includes(k)) ordered.push(k)
  return ordered
}

export function BiasTab({ data, modelLabel }: { data: BiasResponse; modelLabel: string }) {
  if (isEmpty(data)) {
    return <EmptyPanel reason={data.reason} hint={data.hint} />
  }

  const rows = groupBias(data)
  const attr = rows[0]?.attribute_name ?? 'group'
  const cols = metricColumns(rows)

  return (
    <>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 8,
        }}
      >
        <span className="badge b-prov">live · fills per evaluated model/split</span>
        <span className="src">triage.bias_metrics (group-bys, ADR-0007)</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>group ({attr})</th>
            {cols.map((c) => (
              <th key={c} className="num">
                {c.toUpperCase()}
              </th>
            ))}
            <th className="num">Δ disparity</th>
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
              <td className="num">
                {r.disparity != null ? (
                  <span style={{ color: 'var(--bad)' }}>{r.disparity.toFixed(2)}</span>
                ) : (
                  '—'
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="muted" style={{ fontSize: 10.5, marginTop: 8 }}>
        For the selected model ({modelLabel}).
      </div>
    </>
  )
}
