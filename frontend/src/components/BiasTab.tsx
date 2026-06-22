/*
 * BiasTab (spec §1 tab 4, §3.7) — TPR/FPR/PPV group-bys for the selected model;
 * live (fills per evaluated model/split). §3-B empty-state when the experiment
 * has no protected_groups config.
 */
import type { BiasResponse } from '../api/types'
import { EmptyPanel } from './EmptyPanel'

function fmt(x: number): string {
  return x.toFixed(2)
}

export function BiasTab({ data, modelLabel }: { data: BiasResponse; modelLabel: string }) {
  if ('empty' in data && data.empty) {
    return <EmptyPanel reason={data.reason} hint={data.hint} />
  }

  const attr = data.rows[0]?.group_attribute ?? 'group'
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
            <th className="num">TPR</th>
            <th className="num">FPR</th>
            <th className="num">PPV</th>
            <th className="num">n</th>
          </tr>
        </thead>
        <tbody>
          {data.rows.map((r) => (
            <tr key={r.group_value}>
              <td>{r.group_value}</td>
              <td className="num">
                {fmt(r.tpr)}
                {r.disparity != null ? (
                  <span style={{ color: 'var(--bad)' }}> Δ{r.disparity.toFixed(2)}</span>
                ) : null}
              </td>
              <td className="num">{fmt(r.fpr)}</td>
              <td className="num">{fmt(r.ppv)}</td>
              <td className="num">{r.n.toLocaleString('en-US')}</td>
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
