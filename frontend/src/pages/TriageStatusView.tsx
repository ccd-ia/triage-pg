/*
 * TriageStatusView (/status) — the project control panel: source pins / drift,
 * engine versions, GC / artifact-status rollup, and run counts by status. Data
 * from /status (current_source_pins + latest plan engine_versions + artifacts
 * grouped by kind×status + run counts).
 */
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'

export function TriageStatusView() {
  const st = useAsync(() => api.status(), [])

  return (
    <main className="page">
      <div className="exphead">
        <h2>Triage status</h2>
        <p className="desc">Source pins, engine versions, artifact GC, and run counts.</p>
      </div>

      {st.loading ? (
        <div className="banner">Loading status…</div>
      ) : st.error ? (
        <div className="banner err">Failed to load status: {st.error.message}</div>
      ) : st.data ? (
        <div className="cards" style={{ gridTemplateColumns: '1fr 1fr' }}>
          {/* run counts */}
          <div className="card">
            <div className="ch">
              <b>Runs by status</b>
              <span className="src">triage.runs</span>
            </div>
            <div className="kv">
              {Object.entries(st.data.runs).map(([k, v]) => (
                <RunStat key={k} label={k} value={v} />
              ))}
            </div>
          </div>

          {/* engine versions */}
          <div className="card">
            <div className="ch">
              <b>Engine versions</b>
              <span className="src">latest plan → engine_versions</span>
            </div>
            <div className="kv">
              {st.data.engine_versions
                ? Object.entries(st.data.engine_versions).map(([k, v]) => (
                    <span key={k} style={{ display: 'contents' }}>
                      <span className="k2">{k}</span>
                      <span className="v2 mono">{v}</span>
                    </span>
                  ))
                : <span className="muted">no engine versions recorded</span>}
            </div>
          </div>

          {/* source pins */}
          <div className="card">
            <div className="ch">
              <b>Source pins (registry head)</b>
              <span className="src">current_source_pins</span>
            </div>
            <table>
              <thead>
                <tr>
                  <th>source</th>
                  <th>version</th>
                  <th>fingerprint</th>
                </tr>
              </thead>
              <tbody>
                {st.data.sources.map((p) => (
                  <tr key={p.source_name}>
                    <td>{p.source_name}</td>
                    <td className="mono">{p.version_label ?? '—'}</td>
                    <td className="mono muted">{p.fingerprint ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* GC / artifact status */}
          <div className="card">
            <div className="ch">
              <b>Artifact status (GC)</b>
              <span className="src">artifacts grouped by kind × status</span>
            </div>
            <table>
              <thead>
                <tr>
                  <th>kind</th>
                  <th>status</th>
                  <th className="num">n</th>
                </tr>
              </thead>
              <tbody>
                {st.data.gc.map((r) => (
                  <tr key={`${r.kind}-${r.status}`}>
                    <td>{r.kind}</td>
                    <td>
                      <span
                        style={{
                          color: r.status === 'collected' ? 'var(--mut)' : 'var(--ok)',
                        }}
                      >
                        {r.status}
                      </span>
                    </td>
                    <td className="num">{r.n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </main>
  )
}

function RunStat({ label, value }: { label: string; value: number }) {
  return (
    <span style={{ display: 'contents' }}>
      <span className="k2">{label}</span>
      <span className="v2">{value}</span>
    </span>
  )
}
