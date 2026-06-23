/*
 * ModelGroupGrid (Option 3) — a hand-rolled CSS-grid heatmap: rows = model
 * groups, columns = splits (as_of_date), each cell = the group's metric value at
 * that split, heat-shaded, with the BEST value in each column highlighted (the
 * audition intuition, in place). Clicking a cell opens that group/split's model
 * in the ModelSheet. No heatmap lib — pure CSS grid (rework directive).
 *
 * Data: experiment evaluations for the active (metric, parameter); the
 * higher-is-better flag (from the metric catalog) decides the column winner and
 * the heat polarity.
 */
import { useMemo } from 'react'
import type { ExpEvaluationRow } from '../api/types'
import { buildGrid, groupLabel } from '../api/transforms'

interface Props {
  rows: ExpEvaluationRow[]
  metric: string
  parameter: string
  higherIsBetter: boolean
  /** Label per model_group_id (from model-groups summary). */
  labelFor: (groupId: number) => string
  /** Currently selected model_group (highlighted). */
  selectedGroupId: number | null
  /** Click a cell → resolve to a model_id and open its sheet. */
  onPickCell: (groupId: number, asOfDate: string, modelId: number | null) => void
}

/** Map a number in [min,max] to a heat color, polarity by higherIsBetter. */
function heat(value: number, min: number, max: number, higherIsBetter: boolean): string {
  if (max === min) return 'transparent'
  let t = (value - min) / (max - min) // 0..1, 1 = max value
  if (!higherIsBetter) t = 1 - t // invert so "good" is always 1
  // Interpolate from a dim panel tint (bad) to the good-heat green tint.
  const goodA = Math.round(8 + t * 36) // alpha-ish via lightness on green
  return `color-mix(in srgb, var(--heat-good) ${goodA}%, var(--panel2))`
}

export function ModelGroupGrid({
  rows,
  metric,
  parameter,
  higherIsBetter,
  labelFor,
  selectedGroupId,
  onPickCell,
}: Props) {
  const grid = useMemo(
    () => buildGrid(rows, metric, parameter, higherIsBetter),
    [rows, metric, parameter, higherIsBetter],
  )

  // (group, date) → a model_id for the click-through (latest model wins).
  const modelAt = useMemo(() => {
    const m = new Map<string, number>()
    for (const r of rows) {
      if (r.metric !== metric || (r.parameter ?? '') !== parameter) continue
      m.set(`${r.model_group_id}::${r.as_of_date}`, r.model_id)
    }
    return m
  }, [rows, metric, parameter])

  if (grid.rows.length === 0 || grid.dates.length === 0) {
    return (
      <div className="empty">
        <b>no evaluations yet</b>
        <div style={{ marginTop: 6 }}>the grid fills as model groups evaluate across splits.</div>
      </div>
    )
  }

  const cols = `170px repeat(${grid.dates.length}, minmax(64px, 1fr))`

  return (
    <div>
      <div className="grid" style={{ gridTemplateColumns: cols }}>
        <div className="gh">model group ↓ / split →</div>
        {grid.dates.map((d) => (
          <div key={d} className="gh" style={{ textAlign: 'center' }}>
            {d.slice(0, 7)}
          </div>
        ))}

        {grid.rows.map((row) => (
          <Row
            key={row.model_group_id}
            label={labelFor(row.model_group_id) || groupLabel(row.model_group_id)}
            row={row}
            dates={grid.dates}
            min={grid.min}
            max={grid.max}
            higherIsBetter={higherIsBetter}
            selected={selectedGroupId === row.model_group_id}
            onPick={(date) =>
              onPickCell(row.model_group_id, date, modelAt.get(`${row.model_group_id}::${date}`) ?? null)
            }
          />
        ))}
      </div>
      <div className="muted" style={{ fontSize: 10, marginTop: 8 }}>
        best-in-column is outlined (the audition pick, in place) · click a cell → model sheet
      </div>
    </div>
  )
}

function Row({
  label,
  row,
  dates,
  min,
  max,
  higherIsBetter,
  selected,
  onPick,
}: {
  label: string
  row: ReturnType<typeof buildGrid>['rows'][number]
  dates: string[]
  min: number
  max: number
  higherIsBetter: boolean
  selected: boolean
  onPick: (date: string) => void
}) {
  return (
    <>
      <div className="rh" style={selected ? { color: 'var(--acc)' } : undefined}>
        {label}
      </div>
      {dates.map((d) => {
        const cell = row.cells.get(d)
        if (!cell || cell.value == null) {
          return (
            <div key={d} className="cell empty">
              —
            </div>
          )
        }
        return (
          <button
            type="button"
            key={d}
            className={`cell${cell.best ? ' best' : ''}${selected ? ' sel' : ''}`}
            style={{ background: heat(cell.value, min, max, higherIsBetter) }}
            onClick={() => onPick(d)}
            title={`${label} @ ${d.slice(0, 7)} = ${cell.value.toFixed(3)}`}
          >
            {cell.value.toFixed(3)}
          </button>
        )
      })}
    </>
  )
}
