/*
 * AsyncPanel — the loading / error / empty-state frame of one model-sheet panel. A route
 * answers its rows, the §3.7 empty-state envelope (rendered as EmptyPanel, whose hint names
 * the CLI command that fills it), or an HTTP error: a 422 carries the refusal naming the
 * command that applies to the model's problem type.
 */
import type { ReactNode } from 'react'
import type { AsyncState } from '../hooks/useAsync'
import { isEmpty, type EmptyState } from '../api/types'
import { EmptyPanel } from './EmptyPanel'

export function AsyncPanel<T>({
  state,
  what,
  children,
}: {
  state: AsyncState<T | EmptyState>
  what: string
  children: (data: T) => ReactNode
}) {
  if (state.error) {
    return (
      <div className="muted" style={{ fontSize: 11 }}>
        {what} unavailable: {state.error.message}
      </div>
    )
  }
  if (state.data === undefined) {
    return <div className="muted" style={{ fontSize: 11 }}>loading {what}…</div>
  }
  if (isEmpty(state.data)) {
    return <EmptyPanel reason={state.data.reason} hint={state.data.hint} />
  }
  return <>{children(state.data as T)}</>
}
