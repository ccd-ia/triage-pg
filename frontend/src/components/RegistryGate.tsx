/*
 * RegistryGate — the write surface's loading/error envelope. The registry is optional
 * (ADR-0024): with no TRIAGE_REGISTRY_URL the write routes 503. This turns that into a clear,
 * actionable banner (the API's own detail carries the fix) instead of a raw error, and handles
 * the plain loading / other-error cases so the write pages stay tiny.
 */
import type { ReactNode } from 'react'
import { ApiError } from '../api/client'
import { EmptyPanel } from './EmptyPanel'

export function RegistryGate({
  error,
  loading,
  children,
}: {
  error: Error | undefined
  loading: boolean
  children: ReactNode
}) {
  if (loading) return <div className="banner">Loading…</div>

  if (error instanceof ApiError && error.status === 503) {
    // No registry is a supported read-only deployment (ADR-0024), not a failure —
    // render the same neutral empty state the read views use, hint carried by the API.
    return (
      <EmptyPanel
        reason="Registry not configured — this is a read-only deployment."
        hint={error.detail || 'Set TRIAGE_REGISTRY_URL and run `just alembic-registry upgrade head` to enable the write surface (ADR-0002).'}
      />
    )
  }
  if (error) {
    return <div className="banner err">Failed to load: {error.message}</div>
  }
  return <>{children}</>
}
