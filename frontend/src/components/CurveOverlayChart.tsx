/*
 * CurveOverlayChart — several threshold curves on one chart: one precision line and one
 * dashed base-rate line per series, both in the series' colour. The model sheet overlays
 * populations (everyone and up to four Subsets, each ranked among its own members); the
 * model-group sheet overlays its (model, test date) curves. Every point is a row the route
 * returned (a thinned curve, ?points=101); the hover readout is a lookup into those rows
 * (curveAtPct, as the k-slider does) and the legend prints the route's base rate. Nothing
 * here computes a metric (ADR-0012).
 */
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { ThresholdCurvePoint } from '../api/types'
import { curveAtPct } from '../api/transforms'

export interface OverlaySeries {
  key: string
  label: string
  /** Any CSS colour: a categorical slot (var(--series-N)) or a step of the date ramp. */
  color: string
  points: ThresholdCurvePoint[]
  baseRate: number | null
  /** Extra legend text, e.g. the population size. */
  note?: string
}

interface Props {
  series: OverlaySeries[]
  height?: number
  /** Legend text for a series with no rows (a Subset without members at the date). */
  emptyNote?: string
}

const BASE_DASH = '4 3'

/** Legend swatch: the solid precision stroke over the dashed base-rate stroke. */
function Swatch({ color }: { color: string }) {
  return (
    <svg width="24" height="11" aria-hidden="true" style={{ flex: 'none' }}>
      <line x1="0" y1="3" x2="24" y2="3" stroke={color} strokeWidth="2" />
      <line x1="0" y1="8.5" x2="24" y2="8.5" stroke={color} strokeWidth="1.25" strokeDasharray={BASE_DASH} />
    </svg>
  )
}

function Readout({ share, series }: { share: number; series: OverlaySeries[] }) {
  return (
    <div className="ovtip">
      <div className="muted">top {Math.round(share)}% of each population · precision</div>
      {series.map((s) => {
        const at = curveAtPct(s.points, share / 100)
        if (!at) return null
        return (
          <div className="ovtiprow" key={s.key}>
            <span className="ovtipsw" style={{ background: s.color }} />
            <span>{s.label}</span>
            <b>{at.prec?.toFixed(3) ?? '—'}</b>
            <span className="muted">k={at.k.toLocaleString('en-US')}</span>
          </div>
        )
      })}
    </div>
  )
}

export function CurveOverlayChart({
  series,
  height = 220,
  emptyNote = 'no scored rows at this date',
}: Props) {
  const drawn = series.filter((s) => s.points.length > 0)
  return (
    <div>
      <div style={{ height }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart margin={{ top: 6, right: 10, bottom: 0, left: -18 }}>
            <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
            <XAxis
              type="number"
              dataKey="share"
              domain={[0, 100]}
              ticks={[0, 20, 40, 60, 80, 100]}
              stroke="var(--mut)"
              tick={{ fontSize: 9 }}
              tickFormatter={(v) => `${v}%`}
            />
            <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} domain={[0, 1]} />
            <Tooltip
              cursor={{ stroke: 'var(--mut)', strokeDasharray: '2 3' }}
              isAnimationActive={false}
              content={({ active, label }) =>
                active && typeof label === 'number' ? <Readout share={label} series={drawn} /> : null
              }
            />
            {drawn.map((s) =>
              s.baseRate != null ? (
                <ReferenceLine
                  key={`base-${s.key}`}
                  y={s.baseRate}
                  stroke={s.color}
                  strokeWidth={1.25}
                  strokeDasharray={BASE_DASH}
                />
              ) : null,
            )}
            {drawn.map((s) => (
              <Line
                key={s.key}
                data={s.points.map((p) => ({ share: p.pct * 100, precision: p.prec }))}
                dataKey="precision"
                name={s.label}
                type="linear"
                stroke={s.color}
                strokeWidth={2}
                dot={false}
                activeDot={false}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="muted" style={{ fontSize: 10, margin: '2px 0 4px' }}>
        x: share of each population, ranked by score · solid: precision · dashed: base rate
        (the precision a random ranking expects)
      </div>
      <div className="ovlegend">
        {series.map((s) => (
          <span className="ovitem" key={s.key}>
            <Swatch color={s.color} />
            <span>{s.label}</span>
            <span className="muted">
              {s.points.length === 0
                ? emptyNote
                : `base rate ${s.baseRate?.toFixed(3) ?? '—'}${s.note ? ` · ${s.note}` : ''}`}
            </span>
          </span>
        ))}
      </div>
    </div>
  )
}
