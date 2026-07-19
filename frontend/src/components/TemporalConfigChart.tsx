/*
 * TemporalConfigChart — a temporal_config's cross-validation blocks, mirroring the CLI
 * `analyze-config --plot` viz. Train (blue) and validation (orange) get distinct colors; one lane
 * per split (most recent on top); each matrix's as-of-date span carries per-date markers and a
 * lighter label-window lookahead, so the point-in-time separation reads at a glance. Theme-aware
 * via CSS vars for text/lines/background; brand colors for the two roles.
 */
import { useMemo } from 'react'
import type { TemporalSplit } from '../api/types'

const TRAIN = '#58a6ff'
const VAL = '#f0883e'

function ms(d: string): number {
  return new Date(`${d}T00:00:00Z`).getTime()
}

export function TemporalConfigChart({ splits }: { splits: TemporalSplit[] }) {
  const rows = useMemo(() => [...splits].reverse(), [splits]) // most recent on top

  const { tmin, tmax, years } = useMemo(() => {
    let lo = Infinity
    let hi = -Infinity
    for (const s of splits) {
      lo = Math.min(lo, ms(s.feature_start), ms(s.train.first_as_of))
      hi = Math.max(hi, ms(s.train.label_end), ms(s.validation.label_end))
    }
    const ys: number[] = []
    if (Number.isFinite(lo) && Number.isFinite(hi)) {
      const y0 = new Date(lo).getUTCFullYear()
      const y1 = new Date(hi).getUTCFullYear() + 1
      for (let y = y0; y <= y1; y++) ys.push(y)
    }
    return { tmin: lo, tmax: hi, years: ys }
  }, [splits])

  if (!rows.length) return null

  const W = 760
  const padL = 20
  const padR = 148
  const padT = 34
  const rowH = 60
  const H = padT + rows.length * rowH + 20
  const plotW = W - padL - padR
  const span = tmax - tmin || 1
  const x = (t: number) => padL + ((t - tmin) / span) * plotW
  const xd = (d: string) => x(ms(d))
  const barH = 12

  const seg = (x0: string, x1: string) => Math.max(1.5, xd(x1) - xd(x0))

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width="100%"
      role="img"
      aria-label="Temporal cross-validation blocks"
    >
      {years.map((y) => {
        const px = x(ms(`${y}-01-01`))
        if (px < padL || px > W - padR) return null
        return (
          <g key={y}>
            <line x1={px} x2={px} y1={padT - 6} y2={H - 14} stroke="var(--line)" />
            <text x={px} y={H - 2} fontSize={10} fill="var(--mut)" textAnchor="middle">
              {y}
            </text>
          </g>
        )
      })}

      <g fontSize={10} fill="var(--mut)">
        <rect x={padL} y={8} width={12} height={8} fill={TRAIN} rx={1} />
        <text x={padL + 16} y={15}>train</text>
        <rect x={padL + 58} y={8} width={12} height={8} fill={VAL} rx={1} />
        <text x={padL + 74} y={15}>validation</text>
        <rect x={padL + 158} y={8} width={12} height={8} fill={TRAIN} opacity={0.28} rx={1} />
        <text x={padL + 174} y={15}>label window</text>
      </g>

      {rows.map((s, i) => {
        const yc = padT + i * rowH + rowH / 2
        const yTrain = yc - 9
        const yVal = yc + 9
        return (
          <g key={`${s.feature_start}-${i}`}>
            <line
              x1={xd(s.feature_start)}
              x2={xd(s.feature_start)}
              y1={yc - rowH / 2 + 8}
              y2={yc + rowH / 2 - 8}
              stroke="var(--ok)"
              strokeDasharray="2 3"
            />
            <text x={padL} y={yc - rowH / 2 + 6} fontSize={10} fill="var(--ink)">
              Split {rows.length - i}
            </text>

            <rect
              x={xd(s.train.first_as_of)}
              y={yTrain - barH / 2}
              width={seg(s.train.first_as_of, s.train.last_as_of)}
              height={barH}
              fill={TRAIN}
              rx={2}
            />
            <rect
              x={xd(s.train.last_as_of)}
              y={yTrain - barH / 2}
              width={seg(s.train.last_as_of, s.train.label_end)}
              height={barH}
              fill={TRAIN}
              opacity={0.28}
              rx={2}
            />
            {s.train.as_of_dates.map((d, j) => (
              <circle
                key={j}
                cx={xd(d)}
                cy={yTrain}
                r={2.6}
                fill="var(--panel)"
                stroke={TRAIN}
                strokeWidth={1.2}
              />
            ))}

            <rect
              x={xd(s.validation.first_as_of)}
              y={yVal - barH / 2}
              width={seg(s.validation.first_as_of, s.validation.last_as_of)}
              height={barH}
              fill={VAL}
              rx={2}
            />
            <rect
              x={xd(s.validation.last_as_of)}
              y={yVal - barH / 2}
              width={seg(s.validation.last_as_of, s.validation.label_end)}
              height={barH}
              fill={VAL}
              opacity={0.28}
              rx={2}
            />
            {s.validation.as_of_dates.map((d, j) => (
              <circle
                key={j}
                cx={xd(d)}
                cy={yVal}
                r={2.6}
                fill="var(--panel)"
                stroke={VAL}
                strokeWidth={1.2}
              />
            ))}

            <text x={W - padR + 6} y={yc + 3} fontSize={9} fill="var(--mut)">
              {`train ${s.train.label_timespan} · val ${s.validation.label_timespan}`}
            </text>
          </g>
        )
      })}
    </svg>
  )
}
