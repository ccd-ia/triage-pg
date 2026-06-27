/*
 * RayidCurveChart — the classic precision/recall-vs-population curve
 * (/models/{id}/curve) with a CLIENT-SIDE k-slider (Q7). The curve series is the
 * SQL-computed source of truth (ADR-0012); dragging the slider is a pure lookup
 * into that series — no server round-trip, no business logic. At the slider's
 * pct it shows prec/rec and the TP/FP/FN/TN confusion counts, and moves a marker
 * on the chart. The initial k can be seeded from the experiment selection.
 */
import { useMemo, useState } from 'react'
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
import { tooltipFormatter } from '../api/format'

interface Props {
  curve: ThresholdCurvePoint[]
  /** Initial population fraction (0..1) for the slider. */
  initialPct?: number
}

export function RayidCurveChart({ curve, initialPct = 0.1 }: Props) {
  const [pct, setPct] = useState(initialPct)

  const chartData = useMemo(
    () =>
      curve.map((p) => ({
        pct: Math.round(p.pct * 100),
        precision: p.prec,
        recall: p.rec,
      })),
    [curve],
  )

  const at = useMemo(() => curveAtPct(curve, pct), [curve, pct])

  if (curve.length === 0) {
    return <div className="muted" style={{ fontSize: 11 }}>no curve (model not scored yet)</div>
  }

  const pctInt = at ? Math.round(at.pct * 100) : Math.round(pct * 100)

  return (
    <div>
      <div style={{ height: 150 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData} margin={{ top: 6, right: 10, bottom: 0, left: -18 }}>
            <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
            <XAxis
              dataKey="pct"
              stroke="var(--mut)"
              tick={{ fontSize: 9 }}
              tickFormatter={(v) => `${v}%`}
            />
            <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} domain={[0, 1]} />
            <Tooltip
              contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)', fontSize: 11 }}
              formatter={tooltipFormatter(4)}
            />
            <ReferenceLine x={pctInt} stroke="var(--acc)" strokeDasharray="4 3" />
            <Line type="monotone" dataKey="precision" stroke="var(--acc)" strokeWidth={2} dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="recall" stroke="var(--acc2)" strokeWidth={2} dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>

      <div className="kslider">
        <span className="muted" style={{ fontSize: 10 }}>k</span>
        <input
          type="range"
          min={0}
          max={100}
          step={1}
          value={Math.round(pct * 100)}
          onChange={(e) => setPct(Number(e.target.value) / 100)}
        />
        <span className="kval">
          {pctInt}% of population{at ? ` · k=${at.k}` : ''}
        </span>
      </div>

      <div className="muted" style={{ fontSize: 10, margin: '2px 0 10px' }}>
        prec <b style={{ color: 'var(--acc)' }}>{at?.prec?.toFixed(3) ?? '—'}</b> · rec{' '}
        <b style={{ color: 'var(--acc2)' }}>{at?.rec?.toFixed(3) ?? '—'}</b> · recomputes list +
        confusion client-side
      </div>

      {at ? (
        <div className="confusion">
          <div className="cm tp">
            <span className="lbl">TP</span>
            <span className="num">{at.tp.toLocaleString('en-US')}</span>
          </div>
          <div className="cm fp">
            <span className="lbl">FP</span>
            <span className="num">{at.fp.toLocaleString('en-US')}</span>
          </div>
          <div className="cm fn">
            <span className="lbl">FN</span>
            <span className="num">{at.fn.toLocaleString('en-US')}</span>
          </div>
          <div className="cm tn">
            <span className="lbl">TN</span>
            <span className="num">{at.tn.toLocaleString('en-US')}</span>
          </div>
        </div>
      ) : null}
    </div>
  )
}
