/*
 * ProjectsView (/projects) — the registry control plane (ADR-0002/0024, write surface).
 * Lists registry.projects; admins can create one (POST /api/projects). Selecting a project
 * loads its members. The whole page degrades gracefully when no registry is configured
 * (the write routes 503) — see RegistryGate.
 */
import { useState } from 'react'
import { api, ApiError } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { Member, Project } from '../api/types'
import { RegistryGate } from '../components/RegistryGate'

export function ProjectsView() {
  const me = useAsync(() => api.me(), [])
  const projects = useAsync(() => api.listProjects(), [])

  return (
    <main className="page">
      <div className="exphead">
        <h2>Projects</h2>
        <p className="desc">
          The registry control plane (<span className="mono">registry.projects</span>, ADR-0002):
          each project is one isolated database. Admins create projects; the creator becomes owner.
        </p>
      </div>

      <RegistryGate error={me.error ?? projects.error} loading={me.loading || projects.loading}>
        {me.data?.is_admin ? (
          <NewProjectForm onCreated={() => projects.reload()} />
        ) : (
          <div className="banner" style={{ marginBottom: 16 }}>
            You are <b>{me.data?.email}</b> (not an admin) — project creation is admin-only.
          </div>
        )}

        {projects.data && projects.data.length ? (
          <ProjectTable projects={projects.data} />
        ) : (
          <div className="banner">No projects registered yet.</div>
        )}
      </RegistryGate>
    </main>
  )
}

function NewProjectForm({ onCreated }: { onCreated: () => void }) {
  const [slug, setSlug] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [dbName, setDbName] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setMsg(null)
    try {
      const p = await api.createProject({
        slug: slug.trim(),
        display_name: displayName.trim(),
        database_name: dbName.trim() || undefined,
      })
      setMsg({ ok: true, text: `Created project “${p.slug}” → database ${p.database_name}.` })
      setSlug('')
      setDisplayName('')
      setDbName('')
      onCreated()
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : String(err) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="form card" onSubmit={submit} style={{ marginBottom: 18 }}>
      <div className="ch">
        <b>New project</b>
        <span className="src">POST /api/projects · admin</span>
      </div>
      <div className="formrow">
        <label className="field">
          <span>slug</span>
          <input
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="donorschoose"
            required
            pattern="[a-z0-9][a-z0-9_-]*"
            title="url-safe lowercase — [a-z0-9][a-z0-9_-]*"
          />
        </label>
        <label className="field">
          <span>display name</span>
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="DonorsChoose EWS"
            required
          />
        </label>
        <label className="field">
          <span>
            database <span className="muted">(defaults to slug)</span>
          </span>
          <input value={dbName} onChange={(e) => setDbName(e.target.value)} placeholder="donors" />
        </label>
      </div>
      <div className="formactions">
        <button type="submit" className="btn primary" disabled={busy}>
          {busy ? 'Creating…' : 'Create project'}
        </button>
        {msg ? <span className={`formmsg ${msg.ok ? 'ok' : 'err'}`}>{msg.text}</span> : null}
      </div>
    </form>
  )
}

function ProjectTable({ projects }: { projects: Project[] }) {
  const [openSlug, setOpenSlug] = useState<string | null>(null)
  return (
    <table>
      <thead>
        <tr>
          <th>project</th>
          <th>database</th>
          <th>status</th>
          <th>created</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {projects.map((p) => (
          <ProjectRow
            key={p.project_id}
            project={p}
            open={openSlug === p.slug}
            onToggle={() => setOpenSlug(openSlug === p.slug ? null : p.slug)}
          />
        ))}
      </tbody>
    </table>
  )
}

function ProjectRow({
  project: p,
  open,
  onToggle,
}: {
  project: Project
  open: boolean
  onToggle: () => void
}) {
  const members = useAsync<Member[]>(() => (open ? api.projectMembers(p.slug) : Promise.resolve([])), [open, p.slug])
  return (
    <>
      <tr className="clickrow" onClick={onToggle}>
        <td>
          <b>{p.display_name}</b>{' '}
          <code className="hashchip" title={p.project_id}>
            {p.slug}
          </code>
        </td>
        <td className="mono">{p.database_name}</td>
        <td>
          <span className={`badge ${p.status === 'active' ? 'b-run' : 'b-build'}`}>{p.status}</span>
        </td>
        <td className="muted">{p.created_at.slice(0, 10)}</td>
        <td className="muted" style={{ textAlign: 'right' }}>
          {open ? '▾ members' : '▸ members'}
        </td>
      </tr>
      {open ? (
        <tr>
          <td colSpan={5} style={{ background: 'var(--panel2)' }}>
            {members.loading ? (
              <span className="muted" style={{ fontSize: 11 }}>
                loading members…
              </span>
            ) : members.data && members.data.length ? (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                {members.data.map((m) => (
                  <span key={m.user_id} className="rolechip">
                    {m.email} <em>{m.role}</em>
                  </span>
                ))}
              </div>
            ) : (
              <span className="muted" style={{ fontSize: 11 }}>
                no members.
              </span>
            )}
          </td>
        </tr>
      ) : null}
    </>
  )
}

// Re-exported so callers can `instanceof`-check without importing from the client directly.
export { ApiError }
