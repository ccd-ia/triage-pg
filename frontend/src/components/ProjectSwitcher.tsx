/*
 * ProjectSwitcher (ADR-0025) — a top-bar dropdown that repoints the whole dashboard at a different
 * project's database. It lists registry projects; picking one stores the slug (sent as
 * X-Triage-Project on every request) and reloads so all panels re-fetch against that project.
 *
 * Degrades to nothing when there's no registry (listProjects 503s) or no projects — the dashboard
 * then runs in single-project mode against its bound database, exactly as before. A full reload on
 * switch is deliberate for v1: the panels are many independent reads, so reloading is the simplest
 * way to guarantee they all follow the switch (a shared active-project context is a later refinement).
 */
import { api, getActiveProject, setActiveProject } from '../api/client'
import { useAsync } from '../hooks/useAsync'

export function ProjectSwitcher() {
  const projects = useAsync(() => api.listProjects(), [])

  // No registry (error) or no projects → single-project mode: render nothing.
  if (projects.error || !projects.data || projects.data.length === 0) return null

  const active = getActiveProject() ?? ''

  return (
    <label className="projswitch" title="Active project (routes the dashboard to its database)">
      <span className="ico">▣</span>
      <select
        value={active}
        onChange={(e) => {
          setActiveProject(e.target.value || null)
          window.location.reload()
        }}
      >
        <option value="">default project</option>
        {projects.data.map((p) => (
          <option key={p.slug} value={p.slug}>
            {p.display_name}
          </option>
        ))}
      </select>
    </label>
  )
}
