/*
 * Live progress via a single EventSource per run (spec §4 / §6).
 *
 * One `EventSource('/api/runs/:id/stream')` for the whole detail view; each
 * delta is delivered to the caller, which decides which panels to re-fetch
 * (pipeline always; audition/bias/metric when kind ∈ {model, evaluation}).
 *
 * In fixture mode there is no backend, so instead of opening a (failing)
 * EventSource we emit a small scripted sequence of deltas on a timer to
 * exercise the live re-fetch wiring. Real integration just drops the fixture
 * branch and uses the EventSource.
 */
import { useEffect, useRef } from 'react'
import type { ProgressDelta } from '../api/types'
import { api } from '../api/client'

type DeltaHandler = (delta: ProgressDelta) => void

const FIXTURE_SCRIPT: ProgressDelta['kind'][] = ['matrix', 'model', 'evaluation']

export function useRunStream(runId: string | undefined, onDelta: DeltaHandler): void {
  // Keep the latest handler without resubscribing on every render.
  const handlerRef = useRef(onDelta)
  useEffect(() => {
    handlerRef.current = onDelta
  })

  useEffect(() => {
    if (!runId) return

    if (api.useFixture) {
      // Scripted heartbeat: cycle through delta kinds so panels re-fetch live.
      let i = 0
      const id = window.setInterval(() => {
        const kind = FIXTURE_SCRIPT[i % FIXTURE_SCRIPT.length]
        i += 1
        handlerRef.current({ run_id: runId, kind, status: 'built' })
      }, 4000)
      return () => window.clearInterval(id)
    }

    const es = new EventSource(api.streamUrl(runId))
    es.onmessage = (ev: MessageEvent<string>) => {
      try {
        const delta = JSON.parse(ev.data) as ProgressDelta
        handlerRef.current(delta)
      } catch {
        // Malformed event: surface to the console, keep the stream alive
        // (a single bad frame should not tear down live progress).
        console.error('useRunStream: unparseable SSE payload', ev.data)
      }
    }
    es.onerror = () => {
      // EventSource auto-reconnects; log once for visibility.
      console.warn('useRunStream: SSE connection error (auto-retrying)')
    }
    return () => es.close()
  }, [runId])
}
