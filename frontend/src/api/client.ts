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
  EntityProfileResponse,
  ExampleConfig,
  ExpAuditionResponse,
  ExpBiasResponse,
  ExpEvaluationsResponse,
  ExpLeaderboardResponse,
  ExpSelectedModelResponse,
  ExperimentDetailResponse,
  ExperimentSummary,
  Member,
  MetricsResponse,
  ModelCardResponse,
  ModelCurveResponse,
  ModelGroupDetailResponse,
  ModelGroupsResponse,
  ModelHistogramResponse,
  ModelPredictionsResponse,
  OntologyResponse,
  Principal,
  ProgressResponse,
  Project,
  ProjectDerivationResponse,
  RunListItem,
  SourcePinsResponse,
  StatusResponse,
  Submission,
  SubmissionResult,
  SummaryResponse,
  ValidateConfigResult,
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

/** An HTTP error that carries the status + parsed `detail`, so pages can special-case (e.g. a
 *  503 = "registry not configured" for the write surface) instead of showing a raw message. */
export class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

async function _fail(res: Response): Promise<never> {
  let detail = `${res.status} ${res.statusText}`
  try {
    const body = (await res.json()) as { detail?: unknown }
    const d = body?.detail
    if (typeof d === 'string') {
      detail = d
    } else if (d && typeof d === 'object') {
      const obj = d as { message?: string; login_url?: string }
      if (obj.message) detail = obj.message
      // Real auth (ADR-0028): an unauthenticated API call carries the login flow's URL —
      // hand the browser to it (the OIDC round-trip is a navigation, not a fetch).
      if (res.status === 401 && obj.login_url) window.location.assign(obj.login_url)
    }
  } catch {
    // non-JSON body — keep the status line
  }
  throw new ApiError(res.status, detail)
}

// The active project (ADR-0025 switcher) is stored in localStorage and sent as X-Triage-Project on
// every request, so the whole dashboard follows the switcher. Absent ⇒ the app's bound project.
const ACTIVE_PROJECT_KEY = 'triage.activeProject'

export function getActiveProject(): string | null {
  try {
    return localStorage.getItem(ACTIVE_PROJECT_KEY)
  } catch {
    return null
  }
}

export function setActiveProject(slug: string | null): void {
  try {
    if (slug) localStorage.setItem(ACTIVE_PROJECT_KEY, slug)
    else localStorage.removeItem(ACTIVE_PROJECT_KEY)
  } catch {
    // localStorage unavailable (private mode / SSR) — routing just falls back to the default project
  }
}

/** Merge the active-project header into a request's headers. */
function withProject(headers: Record<string, string>): Record<string, string> {
  const slug = getActiveProject()
  return slug ? { ...headers, 'X-Triage-Project': slug } : headers
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: withProject({ Accept: 'application/json' }),
  })
  if (!res.ok) {
    await _fail(res)
  }
  return (await res.json()) as T
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: withProject({ Accept: 'application/json', 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    await _fail(res)
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

  modelGroup(
    id: number,
    metric?: string,
    parameter?: string,
    experimentHash?: string,
  ): Promise<ModelGroupDetailResponse> {
    if (USE_FIXTURE) return fake(fixture.modelGroupDetail(id))
    const q = new URLSearchParams()
    if (metric) q.set('metric', metric)
    if (parameter !== undefined && parameter !== '') q.set('parameter', parameter)
    if (experimentHash) q.set('experiment_hash', experimentHash)
    const qs = q.toString()
    return get<ModelGroupDetailResponse>(`/model-groups/${id}${qs ? `?${qs}` : ''}`)
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

  modelPredictions(
    id: number,
    opts?: { limit?: number; offset?: number },
  ): Promise<ModelPredictionsResponse> {
    if (USE_FIXTURE) return fake(fixture.modelPredictions(id, opts))
    const q = new URLSearchParams()
    if (opts?.limit !== undefined) q.set('limit', String(opts.limit))
    if (opts?.offset !== undefined) q.set('offset', String(opts.offset))
    const qs = q.toString()
    return get<ModelPredictionsResponse>(`/models/${id}/predictions${qs ? `?${qs}` : ''}`)
  },

  entity(id: number, opts?: { experimentHash?: string }): Promise<EntityProfileResponse> {
    if (USE_FIXTURE) return fake(fixture.entityProfile(id, opts?.experimentHash))
    const q = opts?.experimentHash ? `?experiment_hash=${opts.experimentHash}` : ''
    return get<EntityProfileResponse>(`/entities/${id}${q}`)
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

  /* ------------------------ write surface (ADR-0024) --------------------- */

  me(): Promise<Principal> {
    if (USE_FIXTURE) return fake(fixture.principal)
    return get<Principal>('/me')
  },

  listProjects(): Promise<Project[]> {
    if (USE_FIXTURE) return fake([...fixture.projectsStore])
    return get<Project[]>('/projects')
  },

  createProject(body: {
    slug: string
    display_name: string
    database_name?: string
  }): Promise<Project> {
    if (USE_FIXTURE) return fake(fixture.fxCreateProject(body.slug, body.display_name, body.database_name))
    return post<Project>('/projects', body)
  },

  projectMembers(slug: string): Promise<Member[]> {
    if (USE_FIXTURE) return fake([...(fixture.membersStore[slug] ?? [])])
    return get<Member[]>(`/projects/${encodeURIComponent(slug)}/members`)
  },

  listSubmissions(projectSlug?: string): Promise<Submission[]> {
    if (USE_FIXTURE) {
      const all = [...fixture.submissionsStore]
      return fake(projectSlug ? all.filter((s) => s.project_slug === projectSlug) : all)
    }
    const q = projectSlug ? `?project_slug=${encodeURIComponent(projectSlug)}` : ''
    return get<Submission[]>(`/submissions${q}`)
  },

  /** On-request AWS Batch job status for a cloud submission (no background polling). */
  batchStatus(jobId: string): Promise<{ job_id: string; status: string; reason: string | null }> {
    if (USE_FIXTURE) return fake({ job_id: jobId, status: 'SUCCEEDED', reason: null })
    return get(`/batch-status/${encodeURIComponent(jobId)}`)
  },

  /** Dry-run validation of a config (as raw YAML/JSON text or a parsed object). */
  validateConfig(body: {
    config?: Record<string, unknown>
    config_text?: string
  }): Promise<ValidateConfigResult> {
    if (USE_FIXTURE) return fake(fixture.fxValidateConfig(body.config_text))
    return post<ValidateConfigResult>('/validate-config', body)
  },

  /** The committed example configs, for the submit-form picker. */
  listExampleConfigs(): Promise<ExampleConfig[]> {
    if (USE_FIXTURE) return fake([...fixture.exampleConfigs])
    return get<ExampleConfig[]>('/example-configs')
  },

  createSubmission(body: {
    project_slug: string
    config?: Record<string, unknown>
    config_text?: string
    profile: 'local' | 'cloud'
  }): Promise<SubmissionResult> {
    if (USE_FIXTURE) {
      const sub = fixture.fxCreateSubmission(body.project_slug, body.profile)
      return fake({
        submission: sub,
        result:
          body.profile === 'cloud'
            ? { batch_job_id: sub.batch_job_id, status: 'submitted' }
            : {
                experiment_hash: sub.experiment_hash ?? undefined,
                problem_type: 'classification',
                num_runs: 1,
                num_models: 0,
                num_predictions: 0,
                num_evaluations: 0,
              },
      })
    }
    return post<SubmissionResult>('/submissions', body)
  },
}
