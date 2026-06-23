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

const ITEMS: Item[] = [
  { to: '/runs', icon: '▤', label: 'Runs' },
  { to: '/experiments', icon: '✦', label: 'Experiments' },
  { to: '/ontology', icon: '◳', label: 'Ontology' },
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
    </nav>
  )
}
