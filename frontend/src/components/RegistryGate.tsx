/*
 * RegistryGate — the write surface's loading/error envelope. The registry is optional
 * (ADR-0024): with no TRIAGE_REGISTRY_URL the write routes 503. This turns that into a clear,
 * actionable banner (the API's own detail carries the fix) instead of a raw error, and handles
 * the plain loading / other-error cases so the write pages stay tiny.
 */
import type { ReactNode } from 'react'
import { ApiError } from '../api/client'

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
    return (
      <div className="banner err">
        <b>Registry not configured.</b>
        <div style={{ marginTop: 6, fontWeight: 400 }}>{error.detail}</div>
      </div>
    )
  }
  if (error) {
    return <div className="banner err">Failed to load: {error.message}</div>
  }
  return <>{children}</>
}
