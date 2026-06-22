/*
 * App shell — header + run rail + routed detail (spec §1, §6 routes).
 * Routes: `/` (rail + most-recent run) and `/runs/:id` (detail). The rail is
 * shared across both; selecting a run navigates to /runs/:id.
 */
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useNavigate,
  useParams,
} from 'react-router-dom'
import { api } from './api/client'
import { useAsync } from './hooks/useAsync'
import { RunRail } from './components/RunRail'
import { RunDetail } from './pages/RunDetail'

function Header() {
  return (
    <header className="top">
      <h1>
        triage-pg — Read Dashboard{' '}
        <span className="muted" style={{ fontSize: 13, fontWeight: 400 }}>
          · v1
        </span>
      </h1>
      <p>
        Run-centric master/detail + card-grid hybrid. Read-only over the in-PG views (ADR-0012).
        The Run monitor is tabbed (Pipeline · Derivation · Audition · Bias) and updates live;
        model-specific panels follow an explicit selected model.
      </p>
      <div className="legend">
        <span className="chip tech">
          live: <b>pg_notify → SSE</b> + <b>REST poll</b> · ADR-0021
        </span>
        <span className="chip">runs / artifacts</span>
        <span className="chip">leaderboard</span>
        <span className="chip">evaluations</span>
        <span className="chip">bias_metrics</span>
        <span className="chip">prediction_ranks</span>
        <span className="chip">current_source_pins</span>
        {api.useFixture ? <span className="chip">fixture data (no backend)</span> : null}
      </div>
    </header>
  )
}

/** Shell with the shared rail; `selectedId` drives the rail highlight + routing. */
function Shell({ selectedId, children }: { selectedId?: string; children: React.ReactNode }) {
  const navigate = useNavigate()
  const runs = useAsync(() => api.listRuns(), [])

  return (
    <>
      <Header />
      <div className="app">
        <RunRail
          runs={runs.data ?? []}
          selectedId={selectedId}
          onSelect={(id) => navigate(`/runs/${id}`)}
        />
        {children}
      </div>
      <Footer />
    </>
  )
}

function Footer() {
  return (
    <div className="foot">
      <b>v1 surface:</b> run-centric hybrid · live = <code>pg_notify→SSE + REST poll</code> ·
      Run monitor on tabs (Pipeline · Derivation · Audition · Bias) · model-specific panels
      follow an explicit selected model — default <b>audition</b>, override to
      leaderboard/manual, with a divergence flag.
    </div>
  )
}

/** `/` — redirect to the most-recent run's detail once the rail loads. */
function Home() {
  const runs = useAsync(() => api.listRuns(), [])
  if (runs.loading) {
    return (
      <Shell>
        <main className="detail">
          <div className="banner">Loading runs…</div>
        </main>
      </Shell>
    )
  }
  const first = runs.data?.[0]
  if (first) return <Navigate to={`/runs/${first.run_id}`} replace />
  return (
    <Shell>
      <main className="detail">
        <div className="banner">No runs found.</div>
      </main>
    </Shell>
  )
}

/** `/runs/:id` — the detail view. */
function RunRoute() {
  const { id } = useParams<{ id: string }>()
  if (!id) return <Navigate to="/" replace />
  return (
    <Shell selectedId={id}>
      <RunDetail runId={id} />
    </Shell>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/runs/:id" element={<RunRoute />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
