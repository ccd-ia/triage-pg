/*
 * App shell (Option 4) — top bar (title + theme toggle) · left GLOBAL nav
 * (Runs · Experiments · Ontology · Triage-status · Derivation) · routed content.
 *
 * Routing model (rework plan):
 *   /experiments            — experiment index (landing)
 *   /experiments/:hash      — the experiment detail (analysis is experiment-scoped)
 *   /runs                   — run rail (monitoring), newest run resolves to its experiment
 *   /runs/:id               — resolve the run's experiment_hash → redirect to
 *                             /experiments/:hash?run=:id (run anchors the monitoring panels)
 *   /ontology · /status · /derivation — project-level views
 *
 * The run rail stays primary for monitoring; clicking a run lands on its
 * experiment with that run anchoring Pipeline/Derivation.
 */
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
} from 'react-router-dom'
import { api } from './api/client'
import { useAsync } from './hooks/useAsync'
import { ErrorBoundary } from './components/ErrorBoundary'
import { GlobalNav } from './components/GlobalNav'
import { ThemeToggle } from './components/ThemeToggle'
import { RunRail } from './components/RunRail'
import { ExperimentsList } from './pages/ExperimentsList'
import { ExperimentDetail } from './pages/ExperimentDetail'
import { OntologyView } from './pages/OntologyView'
import { TriageStatusView } from './pages/TriageStatusView'
import { ProjectDerivationView } from './pages/ProjectDerivationView'

function TopBar() {
  return (
    <header className="bar">
      <span className="brand">
        triage<span className="dot">·</span>pg
        {api.useFixture ? (
          <span className="muted" style={{ fontSize: 11, fontWeight: 400, marginLeft: 10 }}>
            fixture data
          </span>
        ) : null}
      </span>
      <ThemeToggle />
    </header>
  )
}

/** Shell: top bar + global nav + content slot. `rail` injects an inner rail. */
function Shell({ rail, children }: { rail?: React.ReactNode; children: React.ReactNode }) {
  return (
    <>
      <TopBar />
      <div className="layout">
        <GlobalNav />
        <div className={`content${rail ? '' : ' norail'}`}>
          {rail}
          {children}
        </div>
      </div>
    </>
  )
}

/* ----------------------------- runs (monitoring) ------------------------- */

/** /runs — the run rail; selecting a run resolves to its experiment. */
function RunsRoute() {
  const navigate = useNavigate()
  const runs = useAsync(() => api.listRuns(), [])
  return (
    <Shell
      rail={
        <RunRail
          runs={runs.data ?? []}
          selectedId={undefined}
          onSelect={(id) => navigate(`/runs/${id}`)}
        />
      }
    >
      <main className="page">
        <div className="exphead">
          <h2>Runs</h2>
          <p className="desc">Pick a run to open its experiment (analysis aggregates all runs of the experiment).</p>
        </div>
        {runs.loading ? <div className="banner">Loading runs…</div> : null}
        {runs.data && runs.data.length === 0 ? <div className="banner">No runs found.</div> : null}
      </main>
    </Shell>
  )
}

/** /runs/:id — resolve experiment_hash via /summary, then redirect. */
function RunResolve() {
  const { id } = useParams<{ id: string }>()
  const summary = useAsync(() => (id ? api.summary(id) : Promise.resolve(undefined)), [id])
  if (!id) return <Navigate to="/runs" replace />
  if (summary.loading) {
    return (
      <Shell>
        <main className="page">
          <div className="banner">Resolving run {id.slice(0, 8)}…</div>
        </main>
      </Shell>
    )
  }
  const hash = summary.data?.summary.experiment_hash
  if (hash) return <Navigate to={`/experiments/${hash}?run=${id}`} replace />
  return (
    <Shell>
      <main className="page">
        <div className="banner err">Run {id.slice(0, 8)} has no experiment_hash.</div>
      </main>
    </Shell>
  )
}

/* ---------------------------- experiments -------------------------------- */

/** /experiments — index with the experiment rail. */
function ExperimentsRoute() {
  const navigate = useNavigate()
  const exps = useAsync(() => api.listExperiments(), [])
  return (
    <Shell rail={<ExperimentRail selectedHash={undefined} onSelect={(h) => navigate(`/experiments/${h}`)} runs={exps} />}>
      <ExperimentsList />
    </Shell>
  )
}

/** /experiments/:hash — the experiment detail with the experiment rail. */
function ExperimentRoute() {
  const { hash } = useParams<{ hash: string }>()
  const navigate = useNavigate()
  const exps = useAsync(() => api.listExperiments(), [])
  if (!hash) return <Navigate to="/experiments" replace />
  return (
    <Shell rail={<ExperimentRail selectedHash={hash} onSelect={(h) => navigate(`/experiments/${h}`)} runs={exps} />}>
      <ExperimentDetail hash={hash} />
    </Shell>
  )
}

/** A small rail listing experiments (left of the experiment pages). */
function ExperimentRail({
  selectedHash,
  onSelect,
  runs,
}: {
  selectedHash: string | undefined
  onSelect: (hash: string) => void
  runs: ReturnType<typeof useAsync<import('./api/types').ExperimentSummary[]>>
}) {
  return (
    <aside className="exprail">
      <h3 className="k">experiments</h3>
      {(runs.data ?? []).map((e) => (
        <button
          key={e.experiment_hash}
          type="button"
          className={`ei${e.experiment_hash === selectedHash ? ' sel' : ''}`}
          onClick={() => onSelect(e.experiment_hash)}
        >
          <span className="nm">{e.name ?? e.experiment_hash.slice(0, 12)}</span>
          <small>
            {e.n_runs} run{e.n_runs === 1 ? '' : 's'} · {e.last_status ?? '—'}
          </small>
        </button>
      ))}
    </aside>
  )
}

/* -------------------------------- routes --------------------------------- */

/** Routes wrapped in an error boundary keyed by pathname — a render error is scoped
 *  to the current view (not a blank app) and clears when you navigate elsewhere. */
function RoutedContent() {
  const location = useLocation()
  return (
    <ErrorBoundary key={location.pathname}>
      <Routes>
        <Route path="/" element={<Navigate to="/experiments" replace />} />
        <Route path="/experiments" element={<ExperimentsRoute />} />
        <Route path="/experiments/:hash" element={<ExperimentRoute />} />
        <Route path="/runs" element={<RunsRoute />} />
        <Route path="/runs/:id" element={<RunResolve />} />
        <Route path="/ontology" element={<Shell><OntologyView /></Shell>} />
        <Route path="/status" element={<Shell><TriageStatusView /></Shell>} />
        <Route path="/derivation" element={<Shell><ProjectDerivationView /></Shell>} />
        <Route path="*" element={<Navigate to="/experiments" replace />} />
      </Routes>
    </ErrorBoundary>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <RoutedContent />
    </BrowserRouter>
  )
}
