# triage-pg read dashboard (SPA)

React + Vite + TypeScript single-page app for the triage-pg **read dashboard**
(read-only, ADR-0012). Built against the fixed JSON API contract in
`../docs/read-dashboard-spec.md` (§5 endpoints over the §3 in-PG views).

## Develop

```bash
npm install
npm run dev      # http://localhost:5173 — renders standalone on the fixture
npm run build    # tsc -b && vite build  -> dist/  (the acceptance gate)
npm run lint     # eslint
```

The backend (FastAPI) is not part of this package. In dev the app renders
against a local **fixture** (`src/fixtures/`, modeled on DirtyDuck run
`81a68920`, 4 splits) so `npm run dev` works with no backend. The dev server
proxies `/api` → `http://localhost:8000` for later integration
(`vite.config.ts`).

### Switching to the real API

The API client (`src/api/client.ts`) defaults to fixture mode whenever
`import.meta.env.DEV` is true and `VITE_USE_FIXTURE` is unset. To hit the real
endpoints, copy `.env.development.example` to `.env.development` and set
`VITE_USE_FIXTURE=0` (then run the FastAPI backend on `:8000`). Production
builds (`npm run build`) default to live mode.

## Layout (spec §6)

- `src/api/types.ts` — the typed §5 JSON contract. Shapes the spec left
  underspecified are marked with `AMBIGUOUS` comments for integration.
- `src/api/client.ts` — typed GET client over base `/api`; fixture fallback.
- `src/fixtures/` — sample data matching the contract (renders standalone).
- `src/hooks/useAsync.ts` — per-panel async read (independent loading/error +
  `reload()` for SSE invalidation).
- `src/hooks/useRunStream.ts` — one `EventSource('/api/runs/:id/stream')` per
  run (scripted heartbeat in fixture mode).
- `src/components/` — `RunRail`, `SummaryStrip`, `RunMonitor` (tabs: Pipeline +
  Derivation via `@xyflow/react`; Audition + Bias), `SelectedModelBar`,
  `ResultCards` (ExperimentSummary, Leaderboard, MetricOverTime, TopPredictions,
  SourcePins), `ModelDetail`.
- `src/pages/RunDetail.tsx` — `/runs/:id`; owns the selected-model state machine
  and SSE-driven panel re-fetch.
- `src/App.tsx` — shell + routes (`/`, `/runs/:id`) via `react-router-dom`.

## State model (spec §6)

Current run + selected model `{source: 'audition' | 'leaderboard' | 'manual'}`.
The active `model_id` is **derived**: from `/selected-model` for the audition /
leaderboard sources, or the clicked Leaderboard row for manual. One
`EventSource` per run; each delta re-fetches the pipeline + derivation always,
and audition / evaluations / bias / selected-model when `kind ∈ {model,
evaluation}`.
