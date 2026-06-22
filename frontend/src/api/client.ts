/*
 * Typed API client for the read dashboard (docs/read-dashboard-spec.md §5).
 *
 * Every method is a thin typed GET against base `/api`. The backend is not yet
 * running, so when VITE_USE_FIXTURE is enabled (the dev default, see .env.development)
 * each method resolves the dev fixture instead of fetching. At integration time,
 * unset the flag (or set it to "0") and the same methods hit the real endpoints
 * via the Vite proxy / FastAPI static mount.
 */
import type {
  AuditionResponse,
  BiasResponse,
  DerivationResponse,
  EvaluationsResponse,
  LeaderboardResponse,
  ModelDetailResponse,
  PredictionsResponse,
  ProgressResponse,
  RunListItem,
  SelectedModelResponse,
  SourcePinsResponse,
  SummaryResponse,
} from './types'
import * as fixture from '../fixtures'

const BASE = '/api'

// Default to fixture mode in dev unless explicitly disabled. import.meta.env
// values are strings; treat anything but "0"/"false" as enabled when the var
// is present, and fall back to DEV when it's absent.
const USE_FIXTURE = (() => {
  const v = import.meta.env.VITE_USE_FIXTURE as string | undefined
  if (v === undefined) return import.meta.env.DEV
  return v !== '0' && v.toLowerCase() !== 'false'
})()

/** Simulated latency so loading states are exercised in fixture mode. */
function fake<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), 120))
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { Accept: 'application/json' },
  })
  if (!res.ok) {
    throw new Error(`GET ${path} failed: ${res.status} ${res.statusText}`)
  }
  return (await res.json()) as T
}

export const api = {
  useFixture: USE_FIXTURE,

  listRuns(): Promise<RunListItem[]> {
    if (USE_FIXTURE) return fake(fixture.runs)
    return get<RunListItem[]>('/runs')
  },

  summary(runId: string): Promise<SummaryResponse> {
    if (USE_FIXTURE) return fake(fixture.summary)
    return get<SummaryResponse>(`/runs/${runId}/summary`)
  },

  progress(runId: string): Promise<ProgressResponse> {
    if (USE_FIXTURE) return fake(fixture.progress)
    return get<ProgressResponse>(`/runs/${runId}/progress`)
  },

  derivation(runId: string): Promise<DerivationResponse> {
    if (USE_FIXTURE) return fake(fixture.derivation)
    return get<DerivationResponse>(`/runs/${runId}/derivation`)
  },

  audition(runId: string, metric?: string, strategy?: string): Promise<AuditionResponse> {
    if (USE_FIXTURE) return fake(fixture.audition)
    const q = new URLSearchParams()
    if (metric) q.set('metric', metric)
    if (strategy) q.set('strategy', strategy)
    const qs = q.toString()
    return get<AuditionResponse>(`/runs/${runId}/audition${qs ? `?${qs}` : ''}`)
  },

  bias(runId: string, modelId?: number): Promise<BiasResponse> {
    if (USE_FIXTURE) return fake(modelId ? fixture.biasFor(modelId) : fixture.bias)
    const q = modelId ? `?model_id=${modelId}` : ''
    return get<BiasResponse>(`/runs/${runId}/bias${q}`)
  },

  leaderboard(runId: string): Promise<LeaderboardResponse> {
    if (USE_FIXTURE) return fake(fixture.leaderboard)
    return get<LeaderboardResponse>(`/runs/${runId}/leaderboard`)
  },

  evaluations(runId: string, metric?: string): Promise<EvaluationsResponse> {
    if (USE_FIXTURE) return fake(fixture.evaluations)
    const q = metric ? `?metric=${encodeURIComponent(metric)}` : ''
    return get<EvaluationsResponse>(`/runs/${runId}/evaluations${q}`)
  },

  predictions(runId: string, modelId: number, k = 10): Promise<PredictionsResponse> {
    if (USE_FIXTURE) return fake(fixture.predictionsFor(modelId))
    return get<PredictionsResponse>(`/runs/${runId}/predictions?model_id=${modelId}&k=${k}`)
  },

  sourcePins(runId: string): Promise<SourcePinsResponse> {
    if (USE_FIXTURE) return fake(fixture.sourcePins)
    return get<SourcePinsResponse>(`/runs/${runId}/source-pins`)
  },

  selectedModel(runId: string, metric?: string): Promise<SelectedModelResponse> {
    if (USE_FIXTURE) return fake(fixture.selectedModel)
    const q = metric ? `?metric=${encodeURIComponent(metric)}` : ''
    return get<SelectedModelResponse>(`/runs/${runId}/selected-model${q}`)
  },

  modelDetail(modelId: number): Promise<ModelDetailResponse> {
    if (USE_FIXTURE) return fake(fixture.modelDetail(modelId))
    return get<ModelDetailResponse>(`/models/${modelId}`)
  },

  /** SSE endpoint URL for run_progress deltas (§4). */
  streamUrl(runId: string): string {
    return `${BASE}/runs/${runId}/stream`
  },
}
