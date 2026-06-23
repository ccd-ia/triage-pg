/*
 * useExperiment — the selection state that drives the model-scoped panels and
 * the persistent SelectedModelContextBar (Option 2). It is intentionally tiny:
 * the user's *choices* (the source segment + a manually-picked model) live in
 * React state; the concrete audition/leaderboard model ids are RESOLVED from
 * /experiments/{hash}/selected-model by the page and fed back in here.
 *
 * Shape (rework plan): {source: audition|leaderboard|manual, model_id, metric, k}.
 * Defaults: source=audition, metric/parameter/rule from the contract defaults.
 * This file is JSX-free on purpose so it exports only hooks/context (no
 * component), which keeps react-refresh/only-export-components happy; the
 * Provider component lives next to the page that owns it.
 */
import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import type { SelectionSource } from '../api/types'

/** The (metric, parameter, rule) the analysis panels are computed for. */
export interface MetricChoice {
  metric: string
  parameter: string
  rule: string
}

export const DEFAULT_METRIC: MetricChoice = {
  metric: 'precision@',
  parameter: '10_pct',
  rule: 'best_average_value',
}

/** What the page resolves and feeds back (the concrete ids behind each source). */
export interface ResolvedSelection {
  auditionModel: number | null
  auditionGroup: number | null
  leaderboardModel: number | null
  leaderboardGroup: number | null
}

export interface ExperimentSelection {
  /** Which provenance drives the model-scoped panels. */
  source: SelectionSource
  /** The active model_id (resolved: manual pick, or audition/leaderboard). */
  modelId: number | null
  /** The active model_group_id (for grid highlighting / group sheet). */
  modelGroupId: number | null
  /** Active metric/parameter/rule for the analysis panels. */
  choice: MetricChoice
  /** Top-k fraction for the predicted list (0..1); the Rayid slider is local. */
  k: number
  setSource: (s: SelectionSource) => void
  setMetric: (c: Partial<MetricChoice>) => void
  setK: (k: number) => void
  /** Pick a concrete model (sets source=manual). */
  pickModel: (modelId: number, groupId?: number | null) => void
}

const ExperimentContext = createContext<ExperimentSelection | null>(null)

export { ExperimentContext }

/**
 * Build the selection value. `resolved` comes from /selected-model; the active
 * modelId/groupId derive from the chosen source + manual pick. A page wraps its
 * subtree in <ExperimentProvider value={useExperimentState(resolved)}> (the
 * provider is a thin component defined alongside the page).
 */
export function useExperimentState(resolved: ResolvedSelection): ExperimentSelection {
  const [source, setSourceRaw] = useState<SelectionSource>('audition')
  const [manual, setManual] = useState<{ modelId: number; groupId: number | null } | null>(null)
  const [choice, setChoice] = useState<MetricChoice>(DEFAULT_METRIC)
  const [k, setK] = useState(0.1)

  const setSource = useCallback((s: SelectionSource) => setSourceRaw(s), [])
  const setMetric = useCallback(
    (c: Partial<MetricChoice>) => setChoice((prev) => ({ ...prev, ...c })),
    [],
  )
  const pickModel = useCallback((modelId: number, groupId?: number | null) => {
    setManual({ modelId, groupId: groupId ?? null })
    setSourceRaw('manual')
  }, [])

  const { modelId, modelGroupId } = useMemo(() => {
    if (source === 'manual' && manual) {
      return { modelId: manual.modelId, modelGroupId: manual.groupId }
    }
    if (source === 'leaderboard') {
      return { modelId: resolved.leaderboardModel, modelGroupId: resolved.leaderboardGroup }
    }
    return { modelId: resolved.auditionModel, modelGroupId: resolved.auditionGroup }
  }, [source, manual, resolved])

  return useMemo(
    () => ({ source, modelId, modelGroupId, choice, k, setSource, setMetric, setK, pickModel }),
    [source, modelId, modelGroupId, choice, k, setSource, setMetric, pickModel],
  )
}

/** Read the experiment selection. Throws if used outside an ExperimentProvider. */
export function useExperiment(): ExperimentSelection {
  const ctx = useContext(ExperimentContext)
  if (!ctx) throw new Error('useExperiment must be used within an ExperimentProvider')
  return ctx
}
