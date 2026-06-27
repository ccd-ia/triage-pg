/*
 * TriageStatusView (/status) — the project control panel: source pins / drift,
 * engine versions, GC / artifact-status rollup, and run counts by status. Data
 * from /status (current_source_pins + latest plan engine_versions + artifacts
 * grouped by kind×status + run counts).
 */
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { fmtFingerprint } from '../api/transforms'

export function TriageStatusView() {
  const st = useAsync(() => api.status(), [])

  return (
    <main className="page">
      <div className="exphead">
        <h2>Triage status</h2>
        <p className="desc">Database health, execution mode + compute, source pins / drift, engine versions, artifact GC, and run counts.</p>
      </div>

      {st.loading ? (
        <div className="banner">Loading status…</div>
      ) : st.error ? (
        <div className="banner err">Failed to load status: {st.error.message}</div>
      ) : st.data ? (
        <div className="cards" style={{ gridTemplateColumns: '1fr 1fr' }}>
          {/* database health */}
          <div className="card">
            <div className="ch">
              <b>Database</b>
              <span className="src">live · pg catalogs</span>
            </div>
            <div className="kv">
              <KV label="reachable" value={<span style={{ color: 'var(--ok)' }}>● up</span>} />
              <KV label="version" value={st.data.db.server_version} mono />
              <KV label="size" value={st.data.db.db_size} />
              <KV label="connections" value={`${st.data.db.connections} / ${st.data.db.max_connections}`} />
              <KV label="parallel workers" value={`${st.data.db.max_parallel_workers}`} />
              <KV label="uptime" value={st.data.db.uptime} />
            </div>
          </div>

          {/* execution + compute (latest run) */}
          <div className="card">
            <div className="ch">
              <b>Execution · latest run</b>
              <span className="src">runs.profile · plan.compute</span>
            </div>
            <div className="kv">
              <KV
                label="profile"
                value={
                  <span className={`badge ${st.data.execution.profile === 'cloud' ? 'b-aud' : 'b-run'}`}>
                    {st.data.execution.profile === 'cloud' ? 'cloud · AWS Batch' : 'local · in-process'}
                  </span>
                }
              />
              <KV label="status" value={st.data.execution.status ?? '—'} />
              <KV
                label="duration"
                value={st.data.execution.duration_s != null ? `${st.data.execution.duration_s}s` : '—'}
              />
              <KV
                label="CPUs"
                value={st.data.compute?.cpu_count != null ? `${st.data.compute.cpu_count}` : 'not recorded'}
              />
              <KV label="batch job" value={st.data.execution.batch_job_id ?? '— (local)'} mono />
              <KV label="triage" value={st.data.execution.triage_version ?? '—'} mono />
              <KV label="git" value={st.data.execution.git_hash ?? '—'} mono />
            </div>
          </div>

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

          {/* experiments overview */}
          <div className="card">
            <div className="ch">
              <b>Experiments</b>
              <span className="src">experiment_summary</span>
            </div>
            {st.data.experiments && st.data.experiments.length ? (
              <table>
                <thead>
                  <tr>
                    <th>experiment</th>
                    <th className="num">runs</th>
                    <th className="num">models</th>
                    <th>status</th>
                  </tr>
                </thead>
                <tbody>
                  {st.data.experiments.map((e) => (
                    <tr key={e.experiment_hash}>
                      <td>
                        {e.name ?? e.experiment_hash.slice(0, 12)}{' '}
                        <code className="mono muted" style={{ fontSize: 10 }}>{e.experiment_hash.slice(0, 8)}</code>
                      </td>
                      <td className="num">{e.n_runs}</td>
                      <td className="num">{e.n_models ?? '—'}</td>
                      <td className="muted">{e.last_status ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="muted" style={{ fontSize: 11 }}>no experiments yet.</div>
            )}
          </div>

          {/* artifact storage paths */}
          <div className="card">
            <div className="ch">
              <b>Artifact storage</b>
              <span className="src">matrices · models (parent dir)</span>
            </div>
            {st.data.artifact_paths && st.data.artifact_paths.length ? (
              <table>
                <thead>
                  <tr>
                    <th>kind</th>
                    <th>directory</th>
                    <th className="num">n</th>
                  </tr>
                </thead>
                <tbody>
                  {st.data.artifact_paths.map((a) => (
                    <tr key={`${a.kind}-${a.dir}`}>
                      <td>{a.kind}</td>
                      <td className="mono" style={{ fontSize: 10.5 }}>{a.dir || '·'}/</td>
                      <td className="num">{a.n}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="muted" style={{ fontSize: 11 }}>no stored artifacts yet (paths are relative to the project storage root).</div>
            )}
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
                    <td className="mono muted">{fmtFingerprint(p.fingerprint) ?? '—'}</td>
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

          {/* source-pin drift (latest run vs registry head) */}
          <div className="card">
            <div className="ch">
              <b>Source pin drift</b>
              <span className="src">latest run vs registry head</span>
            </div>
            {st.data.source_drift.length ? (
              <table>
                <thead>
                  <tr>
                    <th>source</th>
                    <th>run pin</th>
                    <th>head</th>
                    <th>drift</th>
                  </tr>
                </thead>
                <tbody>
                  {st.data.source_drift.map((d) => (
                    <tr key={d.source_name}>
                      <td>{d.source_name}</td>
                      <td className="mono">{d.run_version ?? '—'}</td>
                      <td className="mono">{d.head_version ?? '—'}</td>
                      <td>
                        {d.drift ? (
                          <span className="badge b-build">drifted</span>
                        ) : (
                          <span style={{ color: 'var(--ok)' }}>in sync</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="muted" style={{ fontSize: 11 }}>no run with frozen pins yet.</div>
            )}
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

function KV({ label, value, mono }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <span style={{ display: 'contents' }}>
      <span className="k2">{label}</span>
      <span className={`v2${mono ? ' mono' : ''}`}>{value}</span>
    </span>
  )
}
