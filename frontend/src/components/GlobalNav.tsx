/*
 * GlobalNav (Option 4) — the left global navigation: Runs · Experiments ·
 * Ontology · Triage-status. Uses NavLink so the active section highlights from
 * the route. The theme toggle lives in the top bar, not here.
 */
import { NavLink } from 'react-router-dom'

interface Item {
  to: string
  icon: string
  label: string
}

// Runs are not a top-level destination: a run only exists inside an experiment, so it is
// reached from the experiment header (sibling runs) — not the global nav. /runs/:id still
// resolves (deep links) and redirects into its experiment.
const ITEMS: Item[] = [
  { to: '/experiments', icon: '✦', label: 'Experiments' },
  { to: '/ontology', icon: '◳', label: 'Ontology' },
  { to: '/monitoring', icon: '∿', label: 'Monitoring' },
  { to: '/status', icon: '◷', label: 'Triage status' },
]

export function GlobalNav() {
  return (
    <nav className="gnav">
      {ITEMS.map((it) => (
        <NavLink key={it.to} to={it.to} className={({ isActive }) => (isActive ? 'on' : undefined)}>
          <span className="ico">{it.icon}</span>
          {it.label}
        </NavLink>
      ))}
      <div className="sep" />
      <NavLink to="/derivation" className={({ isActive }) => (isActive ? 'on' : undefined)}>
        <span className="ico">⌗</span>
        Derivation
      </NavLink>

      {/* Registry / write surface (ADR-0024) — control plane, distinct from the read views. */}
      <div className="sep" />
      <NavLink to="/projects" className={({ isActive }) => (isActive ? 'on' : undefined)}>
        <span className="ico">▣</span>
        Projects
      </NavLink>
      <NavLink to="/submissions" className={({ isActive }) => (isActive ? 'on' : undefined)}>
        <span className="ico">➦</span>
        Submissions
      </NavLink>
    </nav>
  )
}
