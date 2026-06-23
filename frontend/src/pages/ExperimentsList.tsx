/*
 * ExperimentsList (/experiments) — the experiment index. The left rail lists
 * experiments; selecting one navigates to /experiments/:hash. Shown as the
 * landing page when no specific experiment is selected.
 */
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { StatusBadge } from '../components/StatusBadge'

export function ExperimentsList() {
  const navigate = useNavigate()
  const exps = useAsync(() => api.listExperiments(), [])

  return (
    <main className="page">
      <div className="exphead">
        <h2>Experiments</h2>
        <p className="desc">Each experiment aggregates all of its runs (shared models across re-runs).</p>
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
                  <b>{e.name ?? e.experiment_hash.slice(0, 12)}</b>
                  {e.description ? (
                    <div className="muted" style={{ fontSize: 10.5 }}>{e.description}</div>
                  ) : null}
                </td>
                <td>{e.problem_type ?? '—'}</td>
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
