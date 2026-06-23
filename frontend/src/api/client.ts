/*
 * Typed API client for the read dashboard (src/triage/dashboard/routes.py §5).
 *
 * Every method is a thin typed GET against base `/api`, returning the REAL
 * response shapes from routes.py (the source of truth). When VITE_USE_FIXTURE
 * is enabled (the dev default, see .env.development) each method resolves the
 * dev fixture instead of fetching. At integration time, unset the flag (or set
 * it to "0") and the same methods hit the real endpoints via the Vite proxy /
 * FastAPI static mount.
 */
import type {
  DerivationResponse,
  ExpAuditionResponse,
  ExpBiasResponse,
  ExpEvaluationsResponse,
  ExpLeaderboardResponse,
  ExpSelectedModelResponse,
  ExperimentDetailResponse,
  ExperimentSummary,
  MetricsResponse,
  ModelCardResponse,
  ModelCurveResponse,
  ModelGroupDetailResponse,
  ModelGroupsResponse,
  ModelHistogramResponse,
  ModelPredictionsResponse,
  OntologyResponse,
  ProgressResponse,
  ProjectDerivationResponse,
  RunListItem,
  SourcePinsResponse,
  StatusResponse,
  SummaryResponse,
} from './types'
import * as fixture from '../fixtures'

const BASE = '/api'

/** Default audition/selected-model rule (matches routes.py defaults). */
export const DEFAULT_RULE = 'best_average_value'

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

/** Build a `?metric=&parameter=&rule=` query string (omitting empties). */
function metricQuery(metric?: string, parameter?: string, rule?: string): string {
  const q = new URLSearchParams()
  if (metric) q.set('metric', metric)
  if (parameter !== undefined && parameter !== '') q.set('parameter', parameter)
  if (rule) q.set('rule', rule)
  const qs = q.toString()
  return qs ? `?${qs}` : ''
}

export const api = {
  useFixture: USE_FIXTURE,

  /* ---------------------------- run-scoped ------------------------------- */

  listRuns(): Promise<RunListItem[]> {
    if (USE_FIXTURE) return fake(fixture.runs)
    return get<RunListItem[]>('/runs')
  },

  summary(runId: string): Promise<SummaryResponse> {
    if (USE_FIXTURE) return fake(fixture.summaryFor(runId))
    return get<SummaryResponse>(`/runs/${runId}/summary`)
  },

  progress(runId: string): Promise<ProgressResponse> {
    if (USE_FIXTURE) return fake(fixture.progressFor(runId))
    return get<ProgressResponse>(`/runs/${runId}/progress`)
  },

  derivation(runId: string): Promise<DerivationResponse> {
    if (USE_FIXTURE) return fake(fixture.derivationFor(runId))
    return get<DerivationResponse>(`/runs/${runId}/derivation`)
  },

  sourcePins(runId: string): Promise<SourcePinsResponse> {
    if (USE_FIXTURE) return fake(fixture.sourcePins)
    return get<SourcePinsResponse>(`/runs/${runId}/source-pins`)
  },

  /** SSE endpoint URL for run_progress deltas (§4). */
  streamUrl(runId: string): string {
    return `${BASE}/runs/${runId}/stream`
  },

  /* ------------------------- experiment-scoped --------------------------- */

  listExperiments(): Promise<ExperimentSummary[]> {
    if (USE_FIXTURE) return fake(fixture.experiments)
    return get<ExperimentSummary[]>('/experiments')
  },

  experiment(hash: string): Promise<ExperimentDetailResponse> {
    if (USE_FIXTURE) return fake(fixture.experimentFor(hash))
    return get<ExperimentDetailResponse>(`/experiments/${hash}`)
  },

  expAudition(
    hash: string,
    metric?: string,
    parameter?: string,
    rule?: string,
  ): Promise<ExpAuditionResponse> {
    if (USE_FIXTURE) return fake(fixture.expAuditionFor(hash, metric, parameter, rule))
    return get<ExpAuditionResponse>(`/experiments/${hash}/audition${metricQuery(metric, parameter, rule)}`)
  },

  expBias(hash: string, modelId?: number): Promise<ExpBiasResponse> {
    if (USE_FIXTURE) return fake(fixture.expBiasFor(hash, modelId))
    const q = modelId ? `?model_id=${modelId}` : ''
    return get<ExpBiasResponse>(`/experiments/${hash}/bias${q}`)
  },

  expLeaderboard(hash: string): Promise<ExpLeaderboardResponse> {
    if (USE_FIXTURE) return fake(fixture.expLeaderboardFor(hash))
    return get<ExpLeaderboardResponse>(`/experiments/${hash}/leaderboard`)
  },

  expEvaluations(hash: string, metric?: string): Promise<ExpEvaluationsResponse> {
    if (USE_FIXTURE) return fake(fixture.expEvaluationsFor(hash))
    const q = metric ? `?metric=${encodeURIComponent(metric)}` : ''
    return get<ExpEvaluationsResponse>(`/experiments/${hash}/evaluations${q}`)
  },

  expModelGroups(hash: string): Promise<ModelGroupsResponse> {
    if (USE_FIXTURE) return fake(fixture.modelGroupsFor(hash))
    return get<ModelGroupsResponse>(`/experiments/${hash}/model-groups`)
  },

  expSelectedModel(
    hash: string,
    metric?: string,
    parameter?: string,
    rule?: string,
  ): Promise<ExpSelectedModelResponse> {
    if (USE_FIXTURE) return fake(fixture.expSelectedModelFor(hash, metric, parameter, rule))
    return get<ExpSelectedModelResponse>(
      `/experiments/${hash}/selected-model${metricQuery(metric, parameter, rule)}`,
    )
  },

  ontology(): Promise<OntologyResponse> {
    if (USE_FIXTURE) return fake(fixture.ontology)
    return get<OntologyResponse>('/ontology')
  },

  /* ---------------------------- hierarchy -------------------------------- */

  modelGroup(id: number, metric?: string, parameter?: string): Promise<ModelGroupDetailResponse> {
    if (USE_FIXTURE) return fake(fixture.modelGroupDetail(id))
    return get<ModelGroupDetailResponse>(`/model-groups/${id}${metricQuery(metric, parameter)}`)
  },

  model(id: number): Promise<ModelCardResponse> {
    if (USE_FIXTURE) return fake(fixture.modelCard(id))
    return get<ModelCardResponse>(`/models/${id}`)
  },

  modelCurve(id: number): Promise<ModelCurveResponse> {
    if (USE_FIXTURE) return fake(fixture.modelCurve(id))
    return get<ModelCurveResponse>(`/models/${id}/curve`)
  },

  modelHistogram(id: number, bins?: number): Promise<ModelHistogramResponse> {
    if (USE_FIXTURE) return fake(fixture.modelHistogram(id))
    const q = bins ? `?bins=${bins}` : ''
    return get<ModelHistogramResponse>(`/models/${id}/histogram${q}`)
  },

  modelPredictions(id: number, k?: number): Promise<ModelPredictionsResponse> {
    if (USE_FIXTURE) return fake(fixture.modelPredictions(id, k))
    const q = k !== undefined ? `?k=${k}` : ''
    return get<ModelPredictionsResponse>(`/models/${id}/predictions${q}`)
  },

  /* --------------------------- project-level ----------------------------- */

  metrics(): Promise<MetricsResponse> {
    if (USE_FIXTURE) return fake(fixture.metrics)
    return get<MetricsResponse>('/metrics')
  },

  status(): Promise<StatusResponse> {
    if (USE_FIXTURE) return fake(fixture.status)
    return get<StatusResponse>('/status')
  },

  projectDerivation(): Promise<ProjectDerivationResponse> {
    if (USE_FIXTURE) return fake(fixture.projectDerivation)
    return get<ProjectDerivationResponse>('/derivation')
  },
}
