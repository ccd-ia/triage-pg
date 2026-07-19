/*
 * TemporalConfigChart — a temporal_config's cross-validation blocks, mirroring the CLI
 * `analyze-config --plot` viz. Train (blue) and validation (orange) get distinct colors; one lane
 * per split (most recent on top); each matrix's as-of-date span carries per-date markers and a
 * lighter label-window lookahead, so the point-in-time separation reads at a glance. The as-of-date
 * COUNT (how many as-of dates feed each matrix — the training/test frequency) is labelled next to
 * each bar. Theme-aware via CSS vars for text/lines/background; brand colors for the two roles.
 *
 * The SVG is measured to its container width and drawn 1:1 (viewBox width == pixel width) so its
 * text renders at the same size as the surrounding dashboard, not scaled up by a stretched viewBox.
 */
import { useEffect, useRef, useState } from 'react'
import type { TemporalSplit } from '../api/types'

const TRAIN = '#58a6ff'
const VAL = '#f0883e'

function ms(d: string): number {
  return new Date(`${d}T00:00:00Z`).getTime()
}

/** A white-filled, color-ringed as-of-date marker (also used in the legend). */
function AsOfDot({ cx, cy, color }: { cx: number; cy: number; color: string }) {
  return (
    <circle cx={cx} cy={cy} r={3} fill="var(--panel)" stroke={color} strokeWidth={1.3} />
  )
}

export function TemporalConfigChart({ splits }: { splits: TemporalSplit[] }) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [W, setW] = useState(920)

  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      const cw = Math.round(entries[0].contentRect.width)
      if (cw > 0) setW(cw)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const rows = [...splits].reverse() // most recent split on top

  let lo = Infinity
  let hi = -Infinity
  for (const s of splits) {
    lo = Math.min(lo, ms(s.feature_start), ms(s.train.first_as_of))
    hi = Math.max(hi, ms(s.train.label_end), ms(s.validation.label_end))
  }
  const years: number[] = []
  if (Number.isFinite(lo) && Number.isFinite(hi)) {
    for (let y = new Date(lo).getUTCFullYear(); y <= new Date(hi).getUTCFullYear() + 1; y++)
      years.push(y)
  }

  if (!rows.length) return null

  const padL = 22
  const padR = 150 // room for the per-lane as-of-date + label annotations
  const padT = 30
  const rowH = 54
  const H = padT + rows.length * rowH + 22
  const plotW = Math.max(80, W - padL - padR)
  const span = hi - lo || 1
  const x = (t: number) => padL + ((t - lo) / span) * plotW
  const xd = (d: string) => x(ms(d))
  const barH = 11
  const seg = (x0: string, x1: string) => Math.max(1.5, xd(x1) - xd(x0))

  return (
    <div ref={wrapRef}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width={W}
        height={H}
        role="img"
        aria-label="Temporal cross-validation blocks"
      >
        {years.map((y) => {
          const px = x(ms(`${y}-01-01`))
          if (px < padL || px > W - padR) return null
          return (
            <g key={y}>
              <line x1={px} x2={px} y1={padT - 4} y2={H - 14} stroke="var(--line)" />
              <text x={px} y={H - 3} fontSize={10.5} fill="var(--mut)" textAnchor="middle">
                {y}
              </text>
            </g>
          )
        })}

        {/* legend — includes the as-of-date marker so the dots are self-explanatory */}
        <g fontSize={10.5} fill="var(--mut)">
          <rect x={padL} y={6} width={11} height={8} fill={TRAIN} rx={1} />
          <text x={padL + 15} y={13}>train</text>
          <rect x={padL + 52} y={6} width={11} height={8} fill={VAL} rx={1} />
          <text x={padL + 67} y={13}>validation</text>
          <rect x={padL + 140} y={6} width={11} height={8} fill={TRAIN} opacity={0.28} rx={1} />
          <text x={padL + 155} y={13}>label window</text>
          <AsOfDot cx={padL + 236} cy={10} color="var(--mut)" />
          <text x={padL + 244} y={13}>as-of date</text>
        </g>

        {rows.map((s, i) => {
          const yc = padT + i * rowH + rowH / 2
          const yTrain = yc - 8
          const yVal = yc + 8
          const annoX = W - padR + 6
          return (
            <g key={`${s.feature_start}-${i}`}>
              <line
                x1={xd(s.feature_start)}
                x2={xd(s.feature_start)}
                y1={yc - rowH / 2 + 6}
                y2={yc + rowH / 2 - 6}
                stroke="var(--ok)"
                strokeDasharray="2 3"
              />
              <text x={padL} y={yc - rowH / 2 + 4} fontSize={11.5} fill="var(--ink)">
                Split {rows.length - i}
              </text>

              {/* train: as-of span + label window + per-date markers */}
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
                <AsOfDot key={j} cx={xd(d)} cy={yTrain} color={TRAIN} />
              ))}
              <text x={annoX} y={yTrain + 3} fontSize={10} fill="var(--mut)">
                {`${s.train.n_as_of} as-of · label ${s.train.label_timespan}`}
              </text>

              {/* validation: as-of span + label window + per-date markers */}
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
                <AsOfDot key={j} cx={xd(d)} cy={yVal} color={VAL} />
              ))}
              <text x={annoX} y={yVal + 3} fontSize={10} fill="var(--mut)">
                {`${s.validation.n_as_of} as-of · label ${s.validation.label_timespan}`}
              </text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}
