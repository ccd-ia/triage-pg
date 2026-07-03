/*
 * ExperimentDetail — the experiment-scoped detail page (refactor of RunDetail).
 *
 * Scope split (the rework's locked decision):
 *   - ANALYSIS is experiment-scoped: audition / bias / leaderboard / evaluations
 *     / model-groups / selected-model aggregate ALL runs of the experiment
 *     (fetched by experiment_hash). This is a correctness requirement — the
 *     derivation DAG cache-shares models across runs, so run-scoped analysis is
 *     empty on a re-run.
 *   - MONITORING stays run-scoped: Pipeline / Derivation / source-pins anchor on
 *     a single "active run" (default = the experiment's newest run, overridable
 *     by clicking a sibling run in the header → ?run=<id>).
 *
 * Selection: useExperimentState builds the {source, model, metric, k} value from
 * /selected-model + the user's source choice; ExperimentProvider exposes it to
 * the context bar, grid, and ModelSheet. SSE (useRunStream on the active run)
 * re-fetches run panels always, and experiment panels on kind ∈ {model,evaluation}.
 */
import { useCallback, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, DEFAULT_RULE } from '../api/client'
import type {
  ExpEvaluationRow,
  ModelGroupSummaryRow,
  ProgressDelta,
} from '../api/types'
import { isEmpty } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { useRunStream } from '../hooks/useRunStream'
import {
  ExperimentContext,
  useExperimentState,
  type ExperimentSelection,
  type ResolvedSelection,
} from '../hooks/useExperiment'
import { groupLabel, abbrevAlgo, hyperSuffix } from '../api/transforms'
import { ExperimentHeader } from '../components/ExperimentHeader'
import { SummaryStrip } from '../components/SummaryStrip'
import { OverviewSparklines } from '../components/OverviewSparklines'
import { SelectedModelContextBar } from '../components/SelectedModelContextBar'
import { ModelGroupGrid } from '../components/ModelGroupGrid'
import { ModelGroupsTable } from '../components/ModelGroupsTable'
import { ModelSheet } from '../components/ModelSheet'
import { ModelGroupSheet } from '../components/ModelGroupSheet'
import { ExperimentAuditionTab } from '../components/ExperimentAuditionTab'
import { ExperimentBiasTab } from '../components/ExperimentBiasTab'
import { PipelineGraph } from '../components/PipelineGraph'
import { DerivationGraph } from '../components/DerivationGraph'
import { ConfigPanel } from '../components/ConfigPanel'
import { EntitySearch } from '../components/EntitySearch'
import { EntityDrawer } from '../components/EntityDrawer'
import { AuditionCompareModal } from '../components/AuditionCompareModal'

type Tab = 'overview' | 'pipeline' | 'derivation' | 'audition' | 'bias' | 'groups' | 'config'

/** Thin provider component (kept here so the hook file stays JSX-free). */
function ExperimentProvider({
  value,
  children,
}: {
  value: ExperimentSelection
  children: React.ReactNode
}) {
  return <ExperimentContext.Provider value={value}>{children}</ExperimentContext.Provider>
}

export function ExperimentDetail({ hash }: { hash: string }) {
  const [params, setParams] = useSearchParams()
  const [tab, setTab] = useState<Tab>('overview')
  const [sheetModel, setSheetModel] = useState<{ id: number; label: string; groupId: number | null } | null>(null)
  const [groupPanel, setGroupPanel] = useState<number | null>(null)
  const [searchEntity, setSearchEntity] = useState<number | null>(null)
  const [compareOpen, setCompareOpen] = useState(false)

  /* ---------- experiment-scoped reads ---------- */
  const experiment = useAsync(() => api.experiment(hash), [hash])
  const metricsCat = useAsync(() => api.metrics(), [])
  const modelGroups = useAsync(() => api.expModelGroups(hash), [hash])
  const evaluations = useAsync(() => api.expEvaluations(hash), [hash])
  const leaderboard = useAsync(() => api.expLeaderboard(hash), [hash])

  /* ---------- metric/rule selection (experiment analysis) ---------- */
  // The metric/rule for audition + selected-model. Kept in local state and
  // mirrored into the context via setMetric below; defaults from the contract.
  const [metric, setMetric] = useState({ metric: 'precision@', parameter: '10_pct' })
  const [rule, setRule] = useState(DEFAULT_RULE)

  // Regression/survival experiments have no 'precision@' rows (rmse/c_index instead): the
  // EFFECTIVE metric is the selection when the data-driven catalog carries it, else the
  // catalog's first entry — derived, not state, so the analysis panels are never empty for a
  // non-classification problem_type and no effect/setState cascade is needed.
  const effectiveMetric = useMemo(() => {
    const cat = metricsCat.data
    if (!cat || cat.length === 0) return metric
    const exists = cat.some(
      (x) => x.metric === metric.metric && (x.parameter ?? '') === metric.parameter,
    )
    return exists ? metric : { metric: cat[0].metric, parameter: cat[0].parameter ?? '' }
  }, [metricsCat.data, metric])

  const audition = useAsync(
    () => api.expAudition(hash, effectiveMetric.metric, effectiveMetric.parameter, rule),
    [hash, effectiveMetric.metric, effectiveMetric.parameter, rule],
  )
  const selectedModel = useAsync(
    () => api.expSelectedModel(hash, effectiveMetric.metric, effectiveMetric.parameter, rule),
    [hash, effectiveMetric.metric, effectiveMetric.parameter, rule],
  )

  /* ---------- resolved selection for the context/sheet ---------- */
  const sm = selectedModel.data && !isEmpty(selectedModel.data) ? selectedModel.data : undefined
  const resolved: ResolvedSelection = useMemo(
    () => ({
      auditionModel: sm?.audition_model ?? null,
      auditionGroup: sm?.audition_group ?? null,
      leaderboardModel: sm?.leaderboard_model ?? null,
      leaderboardGroup: sm?.leaderboard_group ?? null,
    }),
    [sm],
  )
  const selection = useExperimentState(resolved)

  /* ---------- active run (run-scoped monitoring) ---------- */
  // Default to the experiment's newest run; ?run=<id> overrides (sibling click).
  const runIdParam = params.get('run') ?? undefined
  const activeRunId = runIdParam ?? experiment.data?.runs[0]?.run_id

  const summary = useAsync(
    () => (activeRunId ? api.summary(activeRunId) : Promise.resolve(undefined)),
    [activeRunId],
  )
  const progress = useAsync(
    () => (activeRunId ? api.progress(activeRunId) : Promise.resolve(undefined)),
    [activeRunId],
  )
  const derivation = useAsync(
    () => (activeRunId ? api.derivation(activeRunId) : Promise.resolve(undefined)),
    [activeRunId],
  )

  const onSelectRun = useCallback(
    (runId: string) => {
      const next = new URLSearchParams(params)
      next.set('run', runId)
      setParams(next, { replace: true })
    },
    [params, setParams],
  )

  /* ---------- model-group labels (id → "RF · depth 3") ---------- */
  const groupLabelOf = useCallback(
    (gid: number): string => {
      const g = modelGroups.data?.find((m) => m.model_group_id === gid)
      if (!g) return groupLabel(gid)
      const leaf = abbrevAlgo(g.model_type)
      if (!leaf || leaf === '—') return groupLabel(gid)
      const suffix = hyperSuffix(g.model_type, g.hyperparameters)
      return suffix ? `${leaf} · ${suffix}` : leaf
    },
    [modelGroups.data],
  )

  /* ---------- model labels for the context bar ---------- */
  const groupLabelForBar = selection.modelGroupId != null ? groupLabelOf(selection.modelGroupId) : null
  const modelLabelForBar = selection.modelId != null ? `${groupLabelForBar ?? 'model'} · m${selection.modelId}` : null

  /* ---------- open a model sheet ---------- */
  const openModel = useCallback(
    (modelId: number, gid: number | null, asOf?: string) => {
      const gl = gid != null ? groupLabelOf(gid) : 'model'
      const label = asOf ? `${gl} @ ${asOf.slice(0, 7)}` : `${gl} · m${modelId}`
      setSheetModel({ id: modelId, label, groupId: gid })
    },
    [groupLabelOf],
  )

  /* ---------- grid cell click ---------- */
  const onPickCell = useCallback(
    (gid: number, asOfDate: string, modelId: number | null) => {
      selection.pickModel(modelId ?? 0, gid)
      if (modelId != null) openModel(modelId, gid, asOfDate)
    },
    [selection, openModel],
  )

  /* ---------- model-groups row click ---------- */
  const onPickGroup = useCallback(
    (g: ModelGroupSummaryRow) => {
      // Deterministic: open the group's model at the LATEST split (max as_of_date),
      // ties broken by model_id — never the arbitrary first eval row. The split selector
      // in the sheet lets the user step to earlier splits.
      const rows = (evaluations.data ?? []).filter((r) => r.model_group_id === g.model_group_id)
      const latest = rows.reduce<ExpEvaluationRow | null>((best, r) => {
        if (!best) return r
        if (r.as_of_date > best.as_of_date) return r
        if (r.as_of_date === best.as_of_date && r.model_id > best.model_id) return r
        return best
      }, null)
      const modelId = latest?.model_id ?? null
      selection.pickModel(modelId ?? 0, g.model_group_id)
      if (modelId != null) openModel(modelId, g.model_group_id, latest?.as_of_date)
    },
    [evaluations.data, selection, openModel],
  )

  /* ---------- open the context-bar's active model ---------- */
  const onOpenActiveModel = useCallback(() => {
    if (selection.modelId != null) openModel(selection.modelId, selection.modelGroupId)
  }, [selection, openModel])

  /* ---------- metric / rule changes feed both local state + context ---------- */
  const onMetric = useCallback(
    (m: string, p: string) => {
      setMetric({ metric: m, parameter: p })
      selection.setMetric({ metric: m, parameter: p })
    },
    [selection],
  )
  const onRule = useCallback(
    (r: string) => {
      setRule(r)
      selection.setMetric({ rule: r })
    },
    [selection],
  )

  /* ---------- SSE: re-fetch on deltas (active run) ---------- */
  const onDelta = (delta: ProgressDelta) => {
    progress.reload()
    derivation.reload()
    if (delta.kind === 'model' || delta.kind === 'evaluation') {
      audition.reload()
      evaluations.reload()
      leaderboard.reload()
      selectedModel.reload()
      modelGroups.reload()
    }
    if (delta.kind === 'run') {
      summary.reload()
      experiment.reload()
    }
  }
  useRunStream(activeRunId, onDelta)

  /* ---------- metric catalog → higher_is_better for the grid ---------- */
  const higherIsBetter = useMemo(() => {
    const m = metricsCat.data?.find(
      (x) =>
        x.metric === effectiveMetric.metric &&
        (x.parameter ?? '') === effectiveMetric.parameter,
    )
    return m?.higher_is_better ?? true
  }, [metricsCat.data, effectiveMetric])

  if (experiment.error) {
    return (
      <main className="page">
        <div className="banner err">Failed to load experiment {hash}: {experiment.error.message}</div>
      </main>
    )
  }

  const expName = experiment.data?.summary.name ?? hash.slice(0, 12)
  const evalRows: ExpEvaluationRow[] = evaluations.data ?? []
  const manualAvailable = (modelGroups.data?.length ?? 0) > 0

  return (
    <ExperimentProvider value={selection}>
      <main className="page">
        {experiment.data ? (
          <ExperimentHeader data={experiment.data} activeRunId={activeRunId} onSelectRun={onSelectRun} />
        ) : (
          <div className="banner">Loading experiment…</div>
        )}

        {summary.data ? (
          <SummaryStrip data={summary.data} actuals={experiment.data?.summary} />
        ) : null}
        {summary.data ? <OverviewSparklines data={summary.data} /> : null}

        <SelectedModelContextBar
          experimentName={expName}
          selected={selectedModel.data}
          groupLabel={groupLabelForBar}
          modelLabel={modelLabelForBar}
          manualAvailable={manualAvailable}
          onOpenModel={onOpenActiveModel}
          onCompare={() => setCompareOpen(true)}
        />

        <div className="toolbar">
          <EntitySearch onOpen={setSearchEntity} />
        </div>

        {/* sub-tabs */}
        <div className="subtabs">
          <TabBtn id="overview" tab={tab} set={setTab}>Overview</TabBtn>
          <TabBtn id="pipeline" tab={tab} set={setTab}>Pipeline</TabBtn>
          <TabBtn id="derivation" tab={tab} set={setTab}>Derivation</TabBtn>
          <TabBtn id="audition" tab={tab} set={setTab}>Audition</TabBtn>
          <TabBtn id="bias" tab={tab} set={setTab}>Bias</TabBtn>
          <TabBtn id="groups" tab={tab} set={setTab}>Model Groups</TabBtn>
          <TabBtn id="config" tab={tab} set={setTab}>Config</TabBtn>
        </div>

        <section className="panel">
          {tab === 'overview' && (
            <ModelGroupGrid
              rows={evalRows}
              metric={effectiveMetric.metric}
              parameter={effectiveMetric.parameter}
              higherIsBetter={higherIsBetter}
              labelFor={groupLabelOf}
              selectedGroupId={selection.modelGroupId}
              onPickCell={onPickCell}
            />
          )}
          {tab === 'pipeline' &&
            (progress.data ? (
              <PipelineGraph data={progress.data} derivation={derivation.data} />
            ) : (
              <Loading what="pipeline" />
            ))}
          {tab === 'derivation' &&
            (derivation.data ? <DerivationGraph data={derivation.data} /> : <Loading what="derivation" />)}
          {tab === 'audition' &&
            (audition.data ? (
              <ExperimentAuditionTab
                data={audition.data}
                metrics={metricsCat.data ?? []}
                metric={effectiveMetric.metric}
                parameter={effectiveMetric.parameter}
                rule={rule}
                onMetric={onMetric}
                onRule={onRule}
              />
            ) : (
              <Loading what="audition" />
            ))}
          {tab === 'bias' && <BiasPanel hash={hash} modelId={selection.modelId} label={modelLabelForBar ?? '—'} />}
          {tab === 'groups' &&
            (modelGroups.data ? (
              <ModelGroupsTable
                groups={modelGroups.data}
                selectedGroupId={selection.modelGroupId}
                onPickGroup={onPickGroup}
                onOpenGroupPanel={(g) => setGroupPanel(g.model_group_id)}
              />
            ) : (
              <Loading what="model groups" />
            ))}
          {tab === 'config' &&
            (experiment.data ? (
              <ConfigPanel
                config={experiment.data.config}
                attempt={summary.data?.summary.plan?.attempt ?? null}
                summary={experiment.data.summary}
                modelReuse={experiment.data.model_reuse}
                runs={experiment.data.runs}
                activeRunId={activeRunId}
                onSelectRun={onSelectRun}
              />
            ) : (
              <Loading what="config" />
            ))}
        </section>

        {sheetModel ? (
          <ModelSheet
            modelId={sheetModel.id}
            label={sheetModel.label}
            modelGroupId={sheetModel.groupId}
            experimentHash={hash}
            groupLabelOf={groupLabelOf}
            onClose={() => setSheetModel(null)}
          />
        ) : null}

        {groupPanel != null ? (
          <ModelGroupSheet
            groupId={groupPanel}
            label={groupLabelOf(groupPanel)}
            metric={effectiveMetric.metric}
            parameter={effectiveMetric.parameter}
            experimentHash={hash}
            metrics={metricsCat.data ?? []}
            onOpenModel={(mid, asOf) => openModel(mid, groupPanel, asOf)}
            onClose={() => setGroupPanel(null)}
          />
        ) : null}

        {searchEntity != null ? (
          <EntityDrawer
            entityId={searchEntity}
            experimentHash={hash}
            defaultGroupId={selection.modelGroupId}
            groupLabelOf={groupLabelOf}
            onClose={() => setSearchEntity(null)}
          />
        ) : null}

        {compareOpen && sm && sm.audition_group != null && sm.leaderboard_group != null ? (
          <AuditionCompareModal
            auditionGroup={sm.audition_group}
            leaderboardGroup={sm.leaderboard_group}
            metric={effectiveMetric.metric}
            parameter={effectiveMetric.parameter}
            rule={rule}
            experimentHash={hash}
            groupLabelOf={groupLabelOf}
            onOpenGroup={(gid) => {
              setCompareOpen(false)
              setGroupPanel(gid)
            }}
            onClose={() => setCompareOpen(false)}
          />
        ) : null}
      </main>
    </ExperimentProvider>
  )
}

/** Bias panel — its own read because it depends on the selected model_id. */
function BiasPanel({ hash, modelId, label }: { hash: string; modelId: number | null; label: string }) {
  const bias = useAsync(
    () => (modelId ? api.expBias(hash, modelId) : api.expBias(hash)),
    [hash, modelId],
  )
  if (!bias.data) return <Loading what="bias" />
  return <ExperimentBiasTab data={bias.data} modelLabel={label} />
}

function TabBtn({
  id,
  tab,
  set,
  children,
}: {
  id: Tab
  tab: Tab
  set: (t: Tab) => void
  children: React.ReactNode
}) {
  return (
    <button type="button" className={`tabbtn${tab === id ? ' active' : ''}`} onClick={() => set(id)}>
      {children}
    </button>
  )
}

function Loading({ what }: { what: string }) {
  return <div className="muted" style={{ padding: '14px 4px' }}>Loading {what}…</div>
}
