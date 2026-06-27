/*
 * Shared number formatting for the dashboard.
 *
 * Charts (recharts) default to raw float precision in tooltips (e.g.
 * 0.271301054909386); every chart should round to a readable number of decimals.
 * `fmtNum` rounds to <= `digits` decimals (default 4) and adds thousands
 * separators; integers render without a decimal point. `tooltipFormatter` is a
 * drop-in for a recharts `<Tooltip formatter={...} />`.
 */

/** Round to <= `digits` decimals (default 4), thousands-separated. null/NaN -> "—". */
export function fmtNum(v: unknown, digits = 4): string {
  if (v == null || v === '') return '—'
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return String(v)
  return n.toLocaleString('en-US', { maximumFractionDigits: digits })
}

/** A 0..1 ratio as a percentage with <= `digits` decimals (default 1). */
export function fmtPct(x: unknown, digits = 1): string {
  if (x == null || x === '') return '—'
  const n = typeof x === 'number' ? x : Number(x)
  if (!Number.isFinite(n)) return String(x)
  return `${(n * 100).toLocaleString('en-US', { maximumFractionDigits: digits })}%`
}

/** recharts Tooltip `formatter`: shows each value at <= `digits` decimals.
 *  Param is `unknown` so the function stays assignable to recharts' Formatter type
 *  (whose value can be number | string | array | undefined). */
export function tooltipFormatter(digits = 4) {
  return (value: unknown): string => fmtNum(value, digits)
}
