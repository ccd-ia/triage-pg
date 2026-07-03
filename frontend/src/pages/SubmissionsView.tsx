/*
 * SubmissionsView (/submissions) — submit an experiment + the append-only audit trail
 * (ADR-0024, write surface). The form POSTs a greenfield experiment_config to
 * /api/submissions, which runs it via the profile execution seam (in-process locally, one AWS
 * Batch job in cloud — the SAME path as `triage run`) and records the submission. Degrades
 * gracefully with no registry (RegistryGate).
 */
import { useState } from 'react'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { Profile, Submission, SubmissionResult } from '../api/types'
import { RegistryGate } from '../components/RegistryGate'

const CONFIG_PLACEHOLDER = `{
  "problem_type": "classification",
  "cohort_config": { "name": "...", "query": "select entity_id ..." },
  "label_config": { "name": "...", "query": "select entity_id, outcome ..." },
  "temporal_config": { },
  "feature_config": { "target": "...", "entities": [] },
  "grid_config": { }
}`

export function SubmissionsView() {
  const me = useAsync(() => api.me(), [])
  const projects = useAsync(() => api.listProjects(), [])
  const submissions = useAsync(() => api.listSubmissions(), [])

  return (
    <main className="page">
      <div className="exphead">
        <h2>Submissions</h2>
        <p className="desc">
          Submit a greenfield <span className="mono">experiment_config</span> to run it (the same
          path as <span className="mono">triage run</span>) and record it in the append-only audit
          trail (<span className="mono">registry.submissions</span>).
        </p>
      </div>

      <RegistryGate
        error={me.error ?? projects.error ?? submissions.error}
        loading={me.loading || projects.loading || submissions.loading}
      >
        <SubmitForm
          projects={(projects.data ?? []).map((p) => p.slug)}
          onSubmitted={() => submissions.reload()}
        />
        {submissions.data && submissions.data.length ? (
          <SubmissionTable rows={submissions.data} />
        ) : (
          <div className="banner">No submissions yet.</div>
        )}
      </RegistryGate>
    </main>
  )
}

function SubmitForm({
  projects,
  onSubmitted,
}: {
  projects: string[]
  onSubmitted: () => void
}) {
  const [slug, setSlug] = useState('')
  const [profile, setProfile] = useState<Profile>('local')
  const [config, setConfig] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setMsg(null)
    if (!slug) {
      setMsg({ ok: false, text: 'Pick a project first.' })
      return
    }
    let parsed: Record<string, unknown>
    try {
      parsed = JSON.parse(config)
    } catch (err) {
      setMsg({ ok: false, text: `Config is not valid JSON: ${err instanceof Error ? err.message : err}` })
      return
    }
    setBusy(true)
    try {
      const res: SubmissionResult = await api.createSubmission({
        project_slug: slug,
        config: parsed,
        profile,
      })
      setMsg({ ok: true, text: summarize(res) })
      setConfig('')
      onSubmitted()
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : String(err) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="form card" onSubmit={submit} style={{ marginBottom: 18 }}>
      <div className="ch">
        <b>Submit experiment</b>
        <span className="src">POST /api/submissions</span>
      </div>
      <div className="formrow">
        <label className="field">
          <span>project</span>
          <select value={slug} onChange={(e) => setSlug(e.target.value)} required>
            <option value="" disabled>
              choose a project…
            </option>
            {projects.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>profile</span>
          <select value={profile} onChange={(e) => setProfile(e.target.value as Profile)}>
            <option value="local">local (in-process)</option>
            <option value="cloud">cloud (AWS Batch)</option>
          </select>
        </label>
      </div>
      <label className="field" style={{ marginTop: 10 }}>
        <span>
          experiment_config <span className="muted">(JSON)</span>
        </span>
        <textarea
          value={config}
          onChange={(e) => setConfig(e.target.value)}
          placeholder={CONFIG_PLACEHOLDER}
          rows={10}
          spellCheck={false}
          required
        />
      </label>
      <div className="formactions">
        <button type="submit" className="btn primary" disabled={busy}>
          {busy ? 'Submitting…' : 'Submit'}
        </button>
        {profile === 'local' ? (
          <span className="muted" style={{ fontSize: 11 }}>
            local runs synchronously — this may take a while.
          </span>
        ) : null}
        {msg ? <span className={`formmsg ${msg.ok ? 'ok' : 'err'}`}>{msg.text}</span> : null}
      </div>
    </form>
  )
}

function summarize(res: SubmissionResult): string {
  const r = res.result
  if (r.batch_job_id) return `Submitted to AWS Batch (job ${r.batch_job_id}).`
  const h = r.experiment_hash ? r.experiment_hash.slice(0, 12) : '—'
  return `Completed experiment ${h}: ${r.num_runs ?? 0} run(s), ${r.num_models ?? 0} model(s), ${r.num_evaluations ?? 0} eval(s).`
}

function SubmissionTable({ rows }: { rows: Submission[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th>submitted</th>
          <th>project</th>
          <th>by</th>
          <th>experiment</th>
          <th>profile</th>
          <th>batch job</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((s) => (
          <tr key={s.submission_id}>
            <td className="muted">{s.submitted_at.replace('T', ' ').slice(0, 16)}</td>
            <td>
              <code className="hashchip">{s.project_slug}</code>
            </td>
            <td className="muted">{s.submitted_by_email ?? '—'}</td>
            <td className="mono">{s.experiment_hash ? s.experiment_hash.slice(0, 12) : '— (pending)'}</td>
            <td>
              <span className={`badge ${s.profile === 'cloud' ? 'b-aud' : 'b-run'}`}>{s.profile}</span>
            </td>
            <td className="mono muted">{s.batch_job_id ?? '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
