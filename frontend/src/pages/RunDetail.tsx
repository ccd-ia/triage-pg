/*
 * RunDetail — the /runs/:id detail view. Owns:
 *  - per-panel reads (useAsync), independent loading/error (spec §6)
 *  - the selected-model state machine {source, model_id} (default from
 *    /selected-model; manual = a clicked Leaderboard row)
 *  - one EventSource per run; deltas re-fetch the affected panels
 *    (pipeline always; audition/bias/metric on kind ∈ {model, evaluation}).
 */
import { useCallback, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { ProgressDelta, SelectionSource } from '../api/types'
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
  const audition = useAsync(() => api.audition(runId), [runId])
  const leaderboard = useAsync(() => api.leaderboard(runId), [runId])
  const evaluations = useAsync(() => api.evaluations(runId), [runId])
  const sourcePins = useAsync(() => api.sourcePins(runId), [runId])
  const selectedModel = useAsync(() => api.selectedModel(runId), [runId])

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

  const sm = selectedModel.data
  const activeModelId =
    selection.source === 'manual'
      ? selection.manualModelId
      : selection.source === 'leaderboard'
        ? (sm?.leaderboard_model ?? undefined)
        : (sm?.audition_model_id ?? undefined)

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

  /* ---- active model label (for the bar + model-scoped cards) ---- */
  const activeLabel = useMemo(() => {
    const fromDetail = modelDetail.data?.label
    if (fromDetail) return fromDetail
    const lbRow = leaderboard.data?.rows.find((r) => r.model_id === activeModelId)
    if (lbRow) return lbRow.label
    if (sm && selection.source === 'leaderboard') return sm.leaderboard_label ?? '—'
    if (sm) return sm.audition_label ?? '—'
    return '—'
  }, [modelDetail.data, leaderboard.data, activeModelId, sm, selection.source])

  const live = summary.data?.summary.status === 'building'

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
        manualAvailable={!!leaderboard.data?.rows.length}
        onSourceChange={onSourceChange}
      />

      <div className="cards">
        {summary.data ? <ExperimentSummaryCard data={summary.data} /> : null}
        {leaderboard.data ? (
          <LeaderboardCard
            data={leaderboard.data}
            onPick={onManualPick}
            selectedModelId={activeModelId}
          />
        ) : null}
        {evaluations.data ? <MetricOverTimeCard data={evaluations.data} /> : null}
        {predictions.data ? (
          <TopPredictionsCard data={predictions.data} modelLabel={activeLabel} />
        ) : null}
        {sourcePins.data ? <SourcePinsCard data={sourcePins.data} /> : null}
      </div>

      {modelDetail.data ? <ModelDetail data={modelDetail.data} /> : null}
    </main>
  )
}
