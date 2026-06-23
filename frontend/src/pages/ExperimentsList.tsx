/*
 * ExperimentsList (/experiments) — the experiment index. Selecting a row navigates
 * to /experiments/:hash. Each row carries the experiment's stable id (short
 * experiment_hash, beside the friendly name) plus context columns — author, model
 * groups, models, base rate — derived from the experiment_summary actuals (migration
 * 0006), so the list distinguishes experiments without drilling in.
 */
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { StatusBadge } from '../components/StatusBadge'

function fmtInt(n: number | null | undefined): string {
  return n == null ? '—' : n.toLocaleString('en-US')
}

function fmtPct(x: number | null | undefined): string {
  return x == null ? '—' : `${(x * 100).toFixed(1)}%`
}

export function ExperimentsList() {
  const navigate = useNavigate()
  const exps = useAsync(() => api.listExperiments(), [])

  return (
    <main className="page">
      <div className="exphead">
        <h2>Experiments</h2>
        <p className="desc">
          Each experiment is one config (stable <span className="mono">experiment_hash</span>);
          its runs are re-executions that share models across re-runs.
        </p>
      </div>
      {exps.loading ? (
        <div className="banner">Loading experiments…</div>
      ) : exps.error ? (
        <div className="banner err">Failed to load experiments: {exps.error.message}</div>
      ) : exps.data && exps.data.length ? (
        <table>
          <thead>
            <tr>
              <th>experiment</th>
              <th>problem</th>
              <th>author</th>
              <th className="num">groups</th>
              <th className="num">models</th>
              <th className="num">base rate</th>
              <th className="num">runs</th>
              <th>last run</th>
              <th>status</th>
            </tr>
          </thead>
          <tbody>
            {exps.data.map((e) => (
              <tr
                key={e.experiment_hash}
                className="clickrow"
                onClick={() => navigate(`/experiments/${e.experiment_hash}`)}
              >
                <td>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <b>{e.name ?? e.experiment_hash.slice(0, 12)}</b>
                    <code className="hashchip" title={e.experiment_hash}>
                      {e.experiment_hash.slice(0, 12)}
                    </code>
                  </div>
                  {e.description ? (
                    <div className="muted" style={{ fontSize: 10.5 }}>{e.description}</div>
                  ) : null}
                </td>
                <td>{e.problem_type ?? '—'}</td>
                <td className="muted">{e.author ?? '—'}</td>
                <td className="num">{fmtInt(e.n_model_groups)}</td>
                <td className="num">{fmtInt(e.n_models)}</td>
                <td className="num">{fmtPct(e.base_rate)}</td>
                <td className="num">{e.n_runs}</td>
                <td className="muted">{e.last_started_at?.slice(0, 10) ?? '—'}</td>
                <td>{e.last_status ? <StatusBadge status={e.last_status} /> : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="banner">No experiments found.</div>
      )}
    </main>
  )
}
