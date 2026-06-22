/*
 * EmptyPanel — the §3.7 empty-state pattern. Endpoints return
 * {empty:true, reason, hint} (200) when their source is empty; the SPA renders
 * this in place of the panel body.
 */
export function EmptyPanel({ reason, hint }: { reason: string; hint: string }) {
  return (
    <div className="empty">
      <b>{reason}</b>
      <div style={{ marginTop: 6 }}>{hint}</div>
    </div>
  )
}
