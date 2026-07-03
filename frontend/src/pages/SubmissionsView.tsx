/*
 * SubmissionsView (/submissions) — submit an experiment + the append-only audit trail
 * (ADR-0024, write surface). The form sends a greenfield experiment config as raw YAML/JSON
 * text to /api/submissions (the server parses it — the same text `triage run` consumes), after
 * a mandatory dry-run against /api/validate-config: the core derives the ADR-0022 experiment
 * hash + split/grid counts and returns path-addressed errors, and Submit stays disabled until
 * the exact text on screen has validated clean. Config sources: pick a committed example
 * (/api/example-configs), upload a .yaml/.json file, or paste. Degrades gracefully with no
 * registry (RegistryGate).
 */
import { useState } from 'react'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type {
  ExampleConfig,
  Profile,
  Submission,
  SubmissionResult,
  ValidateConfigResult,
} from '../api/types'
import { RegistryGate } from '../components/RegistryGate'

const CONFIG_PLACEHOLDER = `# paste a greenfield config (YAML or JSON) — exactly what \`triage run\` consumes
problem_type: classification
cohort_config:
  query: |
    select entity_id from ... where created < '{as_of_date}'
label_config:
  query: |
    select entity_id, outcome from ... -- needs {as_of_date} and {label_timespan}
temporal_config: { }
feature_config: { }
grid_config: { }
`

export function SubmissionsView() {
  const me = useAsync(() => api.me(), [])
  const projects = useAsync(() => api.listProjects(), [])
  const submissions = useAsync(() => api.listSubmissions(), [])

  return (
    <main className="page">
      <div className="exphead">
        <h2>Submissions</h2>
        <p className="desc">
          Submit a greenfield <span className="mono">experiment config</span> to run it (the same
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
  const examples = useAsync(() => api.listExampleConfigs(), [])
  const [slug, setSlug] = useState('')
  const [profile, setProfile] = useState<Profile>('local')
  const [configText, setConfigText] = useState('')
  const [busy, setBusy] = useState(false)
  const [validating, setValidating] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  // The verdict is bound to the exact text it judged: any edit invalidates it, so Submit can
  // never race ahead of what the user last validated.
  const [verdict, setVerdict] = useState<ValidateConfigResult | null>(null)
  const [validatedText, setValidatedText] = useState<string | null>(null)

  const currentVerdict = validatedText === configText ? verdict : null
  const canSubmit = !!slug && !!currentVerdict?.valid && !busy

  function updateText(text: string) {
    setConfigText(text)
    setMsg(null)
  }

  async function validate() {
    setMsg(null)
    setValidating(true)
    try {
      const res = await api.validateConfig({ config_text: configText })
      setVerdict(res)
      setValidatedText(configText)
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : String(err) })
    } finally {
      setValidating(false)
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!canSubmit) return
    setMsg(null)
    setBusy(true)
    try {
      const res: SubmissionResult = await api.createSubmission({
        project_slug: slug,
        config_text: configText,
        profile,
      })
      setMsg({ ok: true, text: summarize(res) })
      setConfigText('')
      setVerdict(null)
      setValidatedText(null)
      onSubmitted()
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : String(err) })
    } finally {
      setBusy(false)
    }
  }

  async function onUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    updateText(await file.text())
    e.target.value = '' // allow re-uploading the same file after edits
  }

  return (
    <form className="form card" onSubmit={submit} style={{ marginBottom: 18 }}>
      <div className="ch">
        <b>Submit experiment</b>
        <span className="src">POST /api/validate-config → POST /api/submissions</span>
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
        {examples.data && examples.data.length ? (
          <label className="field">
            <span>start from an example</span>
            <select
              value=""
              onChange={(e) => {
                const ex = examples.data?.find((x: ExampleConfig) => x.name === e.target.value)
                if (ex) updateText(ex.content)
              }}
            >
              <option value="">pick a committed config…</option>
              {examples.data.map((ex: ExampleConfig) => (
                <option key={ex.name} value={ex.name} title={ex.description}>
                  {ex.name}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <label className="field">
          <span>or upload</span>
          <input type="file" accept=".yaml,.yml,.json" onChange={onUpload} />
        </label>
      </div>
      <label className="field" style={{ marginTop: 10 }}>
        <span>
          experiment config <span className="muted">(YAML or JSON)</span>
        </span>
        <textarea
          value={configText}
          onChange={(e) => updateText(e.target.value)}
          placeholder={CONFIG_PLACEHOLDER}
          rows={12}
          spellCheck={false}
          required
        />
      </label>
      {currentVerdict ? <VerdictPanel verdict={currentVerdict} /> : null}
      <div className="formactions">
        <button
          type="button"
          className="btn"
          onClick={validate}
          disabled={validating || !configText}
        >
          {validating ? 'Validating…' : 'Validate'}
        </button>
        <button
          type="submit"
          className="btn primary"
          disabled={!canSubmit}
          title={
            currentVerdict?.valid
              ? undefined
              : 'Validate the config (clean) before submitting'
          }
        >
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

function VerdictPanel({ verdict }: { verdict: ValidateConfigResult }) {
  return (
    <div className={`banner ${verdict.valid ? '' : 'err'}`} style={{ margin: '10px 0 0' }}>
      {verdict.valid ? (
        <span>
          ✓ valid — experiment{' '}
          <code className="hashchip">{verdict.experiment_hash?.slice(0, 12)}</code>
          {verdict.n_splits != null ? <> · {verdict.n_splits} split(s)</> : null}
          {verdict.n_models != null ? <> · {verdict.n_models} model(s)/split</> : null}
          {verdict.n_feature_groups != null ? (
            <> · {verdict.n_feature_groups} feature group(s)</>
          ) : null}
        </span>
      ) : (
        <div>
          <b>✗ config has {verdict.errors.length} error(s):</b>
          <ul style={{ margin: '4px 0 0 18px' }}>
            {verdict.errors.map((e, i) => (
              <li key={i}>
                <code className="mono">{e.path}</code> — {e.message}
              </li>
            ))}
          </ul>
        </div>
      )}
      {verdict.warnings.length ? (
        <ul className="muted" style={{ margin: '4px 0 0 18px', fontSize: 11 }}>
          {verdict.warnings.map((w, i) => (
            <li key={i}>⚠ {w}</li>
          ))}
        </ul>
      ) : null}
    </div>
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
