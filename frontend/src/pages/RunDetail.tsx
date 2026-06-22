/*
 * RunDetail — the /runs/:id detail view. Owns:
 *  - per-panel reads (useAsync), independent loading/error (spec §6)
 *  - the selected-model state machine {source, model_id} (default from
 *    /selected-model; manual = a clicked Leaderboard row)
 *  - one EventSource per run; deltas re-fetch the affected panels
 *    (pipeline always; audition/bias/metric on kind ∈ {model, evaluation}).
 *
 * Reconciled to routes.py: /selected-model returns bigint ids and no labels (so
 * labels are resolved here from the leaderboard reshape), the active source-model
 * field is `audition_model` / `leaderboard_model`, audition + selected-model take
 * a `rule` (default best_average_value), and the run-state (pending/provisional/
 * final) is derived client-side from run status + audition provisionality.
 */
import { useCallback, useMemo, useState } from 'react'
import { api, DEFAULT_RULE } from '../api/client'
import type {
  ProgressDelta,
  SelectionSource,
  SelectionState,
} from '../api/types'
import { isEmpty } from '../api/types'
import { rankLeaderboard, type LeaderboardEntry } from '../api/transforms'
import { useAsync } from '../hooks/useAsync'
import { useRunStream } from '../hooks/useRunStream'
import { SummaryStrip } from '../components/SummaryStrip'
import { RunMonitor } from '../components/RunMonitor'
import { SelectedModelBar } from '../components/SelectedModelBar'
import { ModelDetail } from '../components/ModelDetail'
import {
  ExperimentSummaryCard,
  LeaderboardCard,
  MetricOverTimeCard,
  SourcePinsCard,
  TopPredictionsCard,
} from '../components/ResultCards'

/** User-chosen selection: the source segment + the manual pick (if any). */
interface Selection {
  source: SelectionSource
  manualModelId: number | undefined
}

export function RunDetail({ runId }: { runId: string }) {
  /* ---- panel reads (independent) ---- */
  const summary = useAsync(() => api.summary(runId), [runId])
  const progress = useAsync(() => api.progress(runId), [runId])
  const derivation = useAsync(() => api.derivation(runId), [runId])
  const audition = useAsync(() => api.audition(runId, undefined, undefined, DEFAULT_RULE), [runId])
  const leaderboard = useAsync(() => api.leaderboard(runId), [runId])
  const evaluations = useAsync(() => api.evaluations(runId), [runId])
  const sourcePins = useAsync(() => api.sourcePins(runId), [runId])
  const selectedModel = useAsync(
    () => api.selectedModel(runId, undefined, undefined, DEFAULT_RULE),
    [runId],
  )

  /* ---- selection state machine ---- */
  // Only the user's *choices* are stored: the source segment, and the concrete
  // model_id when they pick a Leaderboard row (manual). The active model_id for
  // the audition/leaderboard sources is DERIVED from /selected-model, so no
  // effect mirrors async data into state (avoids cascading renders).
  const [selection, setSelection] = useState<Selection>({ source: 'audition', manualModelId: undefined })

  const onSourceChange = useCallback((source: SelectionSource) => {
    setSelection((prev) => ({ source, manualModelId: prev.manualModelId }))
  }, [])

  const onManualPick = useCallback((modelId: number) => {
    setSelection({ source: 'manual', manualModelId: modelId })
  }, [])

  // selected-model may be the empty envelope (no evaluated models yet).
  const sm = selectedModel.data && !isEmpty(selectedModel.data) ? selectedModel.data : undefined
  const activeModelId =
    selection.source === 'manual'
      ? selection.manualModelId
      : selection.source === 'leaderboard'
        ? (sm?.leaderboard_model ?? undefined)
        : (sm?.audition_model ?? undefined)

  /* ---- model-scoped reads (depend on the selected model) ---- */
  const bias = useAsync(
    () => (activeModelId ? api.bias(runId, activeModelId) : Promise.resolve(undefined)),
    [runId, activeModelId],
  )
  const predictions = useAsync(
    () => (activeModelId ? api.predictions(runId, activeModelId) : Promise.resolve(undefined)),
    [runId, activeModelId],
  )
  const modelDetail = useAsync(
    () => (activeModelId ? api.modelDetail(activeModelId) : Promise.resolve(undefined)),
    [activeModelId],
  )

  /* ---- leaderboard reshape (rows → ranked entries) used for labels ---- */
  const lbEntries: LeaderboardEntry[] = useMemo(
    () => (leaderboard.data ? rankLeaderboard(leaderboard.data) : []),
    [leaderboard.data],
  )
  const lbById = useMemo(() => {
    const m = new Map<number, LeaderboardEntry>()
    for (const e of lbEntries) m.set(e.model_id, e)
    return m
  }, [lbEntries])

  /* ---- model labels (the API gives only ids; resolve from the leaderboard) ---- */
  const labelFor = useCallback(
    (modelId: number | null | undefined): string | null => {
      if (modelId == null) return null
      return lbById.get(modelId)?.label ?? `model ${modelId}`
    },
    [lbById],
  )
  const auditionLabel = labelFor(sm?.audition_model)
  const leaderboardLabel = labelFor(sm?.leaderboard_model)
  const activeLabel = labelFor(activeModelId) ?? '—'

  /* ---- run-state (pending → provisional → final), derived client-side ---- */
  const runStatus = summary.data?.summary.status
  const auditionProvisional = audition.data && !isEmpty(audition.data) ? audition.data.provisional : true
  const state: SelectionState = useMemo(() => {
    if (!activeModelId) return 'pending'
    if (runStatus === 'completed') return 'final'
    return auditionProvisional ? 'provisional' : 'final'
  }, [activeModelId, runStatus, auditionProvisional])

  const live = runStatus === 'building' || runStatus === 'started'

  /* ---- SSE: re-fetch affected panels on each delta (spec §4/§6) ----
   * Plain function (re-created each render); useRunStream reads the latest via a
   * ref, so it need not be memoized. */
  const onDelta = (delta: ProgressDelta) => {
    // Pipeline + derivation always reflect structural progress.
    progress.reload()
    derivation.reload()
    if (delta.kind === 'model' || delta.kind === 'evaluation') {
      audition.reload()
      evaluations.reload()
      bias.reload()
      selectedModel.reload()
    }
    if (delta.kind === 'run') {
      // run-level transition: refresh summary, leaderboard, pins.
      summary.reload()
      leaderboard.reload()
      sourcePins.reload()
    }
  }
  useRunStream(runId, onDelta)

  if (summary.error) {
    return (
      <main className="detail">
        <div className="banner err">Failed to load run {runId}: {summary.error.message}</div>
      </main>
    )
  }

  const auditionPickGroup = sm?.audition_group ?? null

  return (
    <main className="detail">
      {summary.data ? (
        <SummaryStrip data={summary.data} />
      ) : (
        <div className="banner">Loading run summary…</div>
      )}

      <RunMonitor
        progress={progress.data}
        derivation={derivation.data}
        audition={audition.data}
        bias={bias.data}
        selectedModelLabel={activeLabel}
        live={!!live}
      />

      <SelectedModelBar
        selected={selectedModel.data}
        source={selection.source}
        activeLabel={activeLabel}
        auditionLabel={auditionLabel}
        leaderboardLabel={leaderboardLabel}
        state={state}
        manualAvailable={lbEntries.length > 0}
        onSourceChange={onSourceChange}
      />

      <div className="cards">
        {summary.data ? <ExperimentSummaryCard data={summary.data} /> : null}
        {leaderboard.data ? (
          <LeaderboardCard
            data={leaderboard.data}
            onPick={onManualPick}
            selectedModelId={activeModelId}
            auditionPickGroup={auditionPickGroup}
          />
        ) : null}
        {evaluations.data ? <MetricOverTimeCard data={evaluations.data} /> : null}
        {predictions.data ? (
          <TopPredictionsCard data={predictions.data} modelLabel={activeLabel} />
        ) : null}
        {sourcePins.data ? <SourcePinsCard data={sourcePins.data} /> : null}
      </div>

      {modelDetail.data ? <ModelDetail data={modelDetail.data} label={activeLabel} /> : null}
    </main>
  )
}
