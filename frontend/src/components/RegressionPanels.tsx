/*
 * RegressionPanels — the model card's regression section (regression and
 * regression_ranking models). Every number is a route's:
 *   - the error strip: MAE, WAPE, bias, point coverage (share of y ≤ ŷ) and the top-1%
 *     error share of the latest test date, one row per date below it
 *     (/models/{id}/residuals, migration 0033). When one forecast value covers more than
 *     half the rows the strip warns: such a forecast ranks almost nothing.
 *   - bias and MAE per target-history band, pooled over the test dates
 *     (/models/{id}/residuals/segments?cut=band).
 *   - forecast against actual as a hexbin on sign·log1p axes, the forecast = outcome line
 *     drawn, as `triage plot forecast-actual` draws it (/models/{id}/forecast-actual).
 *   - the model's group against the experiment's closest baseline group, per segment of
 *     every cut (/models/{id}/baseline, baseline_compare).
 *   - checks on the fitted estimator: leaves fitted on few rows, the linear mean shift by
 *     feature family (/models/{id}/structure).
 * The abs-error rules live in ErrorRulesPanel, which the sheet shows for every type.
 */
import { Bar, BarChart, CartesianGrid, Legend, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { fmtNum, fmtPct, tooltipFormatter } from '../api/format'
import type {
  BaselineComparison,
  ForecastActualPair,
  ResidualDateRow,
  ResidualSegmentRow,
  StructureData,
} from '../api/types'
import { AsyncPanel } from './AsyncPanel'

/** The pairs the hexbin reads: the route keeps the first entities by hash past this. */
const PAIRS_CAP = 20_000
/** The panel warns when one forecast value covers more than this share of a date's rows. */
const MODE_WARNING = 0.5

function ErrorStrip({ modelId, rows }: { modelId: number; rows: ResidualDateRow[] }) {
  const dates = [...rows].sort((a, b) => a.as_of_date.localeCompare(b.as_of_date))
  const last = dates[dates.length - 1]
  const cell = (label: string, value: string, title?: string) => (
    <div className="cell" title={title}>
      <span className="lbl">{label}</span>
      <span className="val">{value}</span>
    </div>
  )
  return (
    <>
      <div className="strip">
        {cell('test date', last.as_of_date, `${last.n} labeled rows`)}
        {cell('MAE', fmtNum(last.mae, 3))}
        {cell('WAPE', fmtPct(last.wape), 'Σ|forecast − outcome| over Σ|outcome|')}
        {cell('bias', fmtNum(last.bias, 3), 'the mean of forecast − outcome: above 0 over-forecasts')}
        {cell('y ≤ ŷ', fmtPct(last.coverage_point), 'point coverage: the share of rows whose outcome is at most the forecast')}
        {cell('top 1% |e|', fmtPct(last.top1pct_error_share), 'the share of the absolute error held by the largest 1% of errors')}
      </div>
      {(last.mode_share ?? 0) > MODE_WARNING ? (
        <div className="banner warn">
          one forecast value, {fmtNum(last.mode_score, 4)}, covers {fmtPct(last.mode_share, 0)} of the
          rows on {last.as_of_date} ({last.n_distinct_scores} distinct values): the model ranks
          almost nothing. `triage postmodel structure {modelId}` shows where it comes from.
        </div>
      ) : null}
      {(last.negative_share ?? 0) > 0 ? (
        <div className="muted" style={{ fontSize: 10.5 }}>
          {fmtPct(last.negative_share)} of the forecasts on {last.as_of_date} are negative
        </div>
      ) : null}
      {dates.length > 1 ? (
        <table className="compact" style={{ marginTop: 6 }}>
          <thead>
            <tr>
              <th>date</th>
              <th className="num">rows</th>
              <th className="num">MAE</th>
              <th className="num">WAPE</th>
              <th className="num">bias</th>
              <th className="num">y ≤ ŷ</th>
            </tr>
          </thead>
          <tbody>
            {dates.map((d) => (
              <tr key={d.as_of_date}>
                <td className="mono">{d.as_of_date}</td>
                <td className="num">{d.n}</td>
                <td className="num">{fmtNum(d.mae, 3)}</td>
                <td className="num">{fmtPct(d.wape)}</td>
                <td className="num">{fmtNum(d.bias, 3)}</td>
                <td className="num">{fmtPct(d.coverage_point)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </>
  )
}

function BandChart({ rows }: { rows: ResidualSegmentRow[] }) {
  const data = [...rows]
    .sort((a, b) => a.segment_order - b.segment_order)
    .map((r) => ({ band: r.segment, bias: r.bias, MAE: r.mae, rows: r.n }))
  return (
    <div style={{ height: 150 }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: -12 }}>
          <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
          <XAxis dataKey="band" stroke="var(--mut)" tick={{ fontSize: 9 }} />
          <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} />
          <Tooltip
            contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)', fontSize: 11 }}
            formatter={tooltipFormatter(3)}
          />
          <Legend wrapperStyle={{ fontSize: 10 }} />
          <ReferenceLine y={0} stroke="var(--mut)" />
          <Bar dataKey="bias" fill="var(--warn)" isAnimationActive={false} />
          <Bar dataKey="MAE" fill="var(--acc)" isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

/** sign(v)·log1p(|v|): the forecast-actual plot's axis unit, defined for a negative forecast. */
const slog = (v: number) => Math.sign(v) * Math.log1p(Math.abs(v))

/** Ticks at 0, 1, 10, 100… in slog units, as the plot draws them. */
function slogTicks(lo: number, hi: number): number[] {
  const top = Math.max(Math.abs(lo), Math.abs(hi), 1)
  const raw = [0]
  for (let p = 1; p <= top * 10; p *= 10) raw.push(p)
  return lo < 0 ? [...raw.slice(1).map((v) => -v).reverse(), ...raw] : raw
}

/** The hexagon (d3-hexbin's pointy-top grid) holding the point (px, py), as grid indices. */
function hexOf(px: number, py: number, dx: number, dy: number): [number, number] {
  const py1 = py / dy
  let pj = Math.round(py1)
  const px1 = px / dx - (pj & 1) / 2
  let pi = Math.round(px1)
  const py2 = py1 - pj
  if (Math.abs(py2) * 3 > 1) {
    const px2 = px1 - pi
    const pi2 = pi + (px1 < pi ? -1 : 1) / 2
    const pj2 = pj + (py1 < pj ? -1 : 1)
    const px3 = px1 - pi2
    const py3 = py1 - pj2
    if (px2 * px2 + py2 * py2 > px3 * px3 + py3 * py3) {
      pi = pi2 + ((pj & 1) ? 1 : -1) / 2
      pj = pj2
    }
  }
  return [pi, pj]
}

function Hexbin({ pairs }: { pairs: ForecastActualPair[] }) {
  const W = 520
  const H = 280
  const M = { left: 40, right: 10, top: 8, bottom: 30 }
  const iw = W - M.left - M.right
  const ih = H - M.top - M.bottom
  const xs = pairs.map((p) => slog(p.forecast))
  const ys = pairs.map((p) => slog(p.outcome))
  // loops, not Math.min(...xs): a spread of 20,000 arguments overflows some engines' stack
  let rawLo = Infinity
  let rawHi = -Infinity
  for (const p of pairs) {
    rawLo = Math.min(rawLo, p.forecast, p.outcome)
    rawHi = Math.max(rawHi, p.forecast, p.outcome)
  }
  let lo = slog(rawLo)
  let hi = slog(rawHi)
  if (hi - lo < 1e-9) {
    lo -= 1
    hi += 1
  }
  const sx = (v: number) => ((v - lo) / (hi - lo)) * iw
  const sy = (v: number) => ih - ((v - lo) / (hi - lo)) * ih
  // 40 hexagons across the x axis, the plot's gridsize
  const r = iw / 40 / Math.sqrt(3)
  const dx = r * Math.sqrt(3)
  const dy = r * 1.5
  const counts = new Map<string, { x: number; y: number; n: number }>()
  for (let i = 0; i < pairs.length; i++) {
    const [pi, pj] = hexOf(sx(xs[i]), sy(ys[i]), dx, dy)
    const key = `${pi},${pj}`
    const hit = counts.get(key)
    if (hit) hit.n += 1
    else counts.set(key, { x: (pi + (pj & 1) / 2) * dx, y: pj * dy, n: 1 })
  }
  let max = 1
  for (const c of counts.values()) max = Math.max(max, c.n)
  const hexPath = [0, 1, 2, 3, 4, 5]
    .map((k) => {
      const a = (Math.PI / 3) * k - Math.PI / 6
      return `${(r * Math.cos(a)).toFixed(2)},${(r * Math.sin(a)).toFixed(2)}`
    })
    .join('L')
  const ticks = slogTicks(rawLo, rawHi).filter(
    (v) => slog(v) >= lo - 1e-9 && slog(v) <= hi + 1e-9,
  )
  return (
    <svg className="hexbin" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="forecast against actual">
      <g transform={`translate(${M.left},${M.top})`}>
        <rect width={iw} height={ih} fill="none" stroke="var(--line2)" />
        {[...counts.values()].map((c) => (
          <path
            key={`${c.x},${c.y}`}
            transform={`translate(${c.x.toFixed(2)},${c.y.toFixed(2)})`}
            d={`M${hexPath}Z`}
            fill="var(--acc)"
            fillOpacity={0.15 + (0.85 * Math.log1p(c.n)) / Math.log1p(max)}
          >
            <title>{`${c.n} row(s)`}</title>
          </path>
        ))}
        <line x1={sx(lo)} y1={sy(lo)} x2={sx(hi)} y2={sy(hi)} stroke="var(--ink)" strokeDasharray="4 3" strokeWidth={1} />
        {ticks.map((v) => (
          <g key={`x${v}`}>
            <text x={sx(slog(v))} y={ih + 12} textAnchor="middle">{fmtNum(v, 0)}</text>
            <text x={-5} y={sy(slog(v)) + 3} textAnchor="end">{fmtNum(v, 0)}</text>
          </g>
        ))}
        <text x={iw / 2} y={ih + 25} textAnchor="middle">forecast (log1p scale)</text>
        <text transform={`translate(${-30},${ih / 2}) rotate(-90)`} textAnchor="middle">
          outcome (log1p scale)
        </text>
      </g>
    </svg>
  )
}

function BaselineTable({ data }: { data: BaselineComparison }) {
  return (
    <>
      <div className="muted" style={{ fontSize: 10.5, marginBottom: 4 }}>
        group {data.model_group_id} against baseline group {data.baseline_group_id}
        {data.baseline_chosen ? ' (the lowest MAE on the shared rows)' : ''}: {data.n_rows} shared
        rows on {data.dates.length} test date(s) · segments won {fmtPct(data.segment_win_share, 0)} ·
        entities won {fmtPct(data.entities.won_share, 0)}
        {data.chance?.p_value != null
          ? ` · wins concentrated beyond chance: p = ${fmtNum(data.chance.p_value, 3)}`
          : ''}
      </div>
      <table className="compact">
        <thead>
          <tr>
            <th>cut</th>
            <th>segment</th>
            <th className="num">rows</th>
            <th className="num">MAE model</th>
            <th className="num">MAE base</th>
            <th className="num">WAPE model</th>
            <th className="num">WAPE base</th>
            <th className="num" title="share of rows where the model is strictly closer">closer</th>
          </tr>
        </thead>
        <tbody>
          {data.segments.map((s) => (
            <tr key={`${s.cut}|${s.segment}`}>
              <td className="mono">{s.cut}</td>
              <td>{s.segment}</td>
              <td className="num">{s.n}</td>
              <td className="num" style={{ color: s.model_wins ? 'var(--ok)' : 'var(--bad)' }}>
                {fmtNum(s.mae_m, 3)}
              </td>
              <td className="num">{fmtNum(s.mae_b, 3)}</td>
              <td className="num">{fmtPct(s.wape_m)}</td>
              <td className="num">{fmtPct(s.wape_b)}</td>
              <td className="num">{fmtPct(s.closer_share, 0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

function Structure({ data }: { data: StructureData }) {
  const support = data.leaf_support
  const shift = data.mean_shift
  return (
    <div style={{ fontSize: 11 }}>
      <div className="muted" style={{ fontSize: 10.5, marginBottom: 4 }}>
        estimator <span className="mono">{data.estimator}</span>
      </div>
      {support ? (
        <div>
          {support.n_trees} tree(s), {support.leaves_per_tree.min}–{support.leaves_per_tree.max}{' '}
          leaves each; <b>{support.small_leaves}</b> leaves fitted on fewer than {support.min_rows}{' '}
          training rows, in {support.trees_with_small_leaf} tree(s)
        </div>
      ) : null}
      {shift ? (
        <>
          <div>
            mean forecast {fmtNum(shift.mean_forecast_train, 3)} on the train matrix,{' '}
            {fmtNum(shift.mean_forecast_test, 3)} on the test matrix: a shift of{' '}
            <b>{fmtNum(shift.shift, 3)}</b>
          </div>
          <table className="compact" style={{ marginTop: 4 }}>
            <thead>
              <tr>
                <th>feature family</th>
                <th className="num">features</th>
                <th className="num">β·Δx̄</th>
                <th className="num">|share|</th>
              </tr>
            </thead>
            <tbody>
              {shift.families.slice(0, 6).map((f) => (
                <tr key={f.family}>
                  <td className="mono">{f.family}</td>
                  <td className="num">{f.n_features}</td>
                  <td className="num">{fmtNum(f.contribution, 3)}</td>
                  <td className="num">{fmtPct(f.share, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
      {data.unavailable.map((u) => (
        <div key={u} className="muted" style={{ fontSize: 10.5 }}>{u}</div>
      ))}
    </div>
  )
}

export function RegressionPanels({ modelId }: { modelId: number }) {
  const residuals = useAsync(() => api.modelResiduals(modelId), [modelId])
  const bands = useAsync(() => api.modelResidualSegments(modelId, 'band'), [modelId])
  const pairs = useAsync(() => api.modelForecastActual(modelId, PAIRS_CAP), [modelId])
  const baseline = useAsync(() => api.modelBaseline(modelId), [modelId])
  const structure = useAsync(() => api.modelStructure(modelId), [modelId])

  return (
    <>
      <section>
        <h4>errors · residual summary</h4>
        <AsyncPanel state={residuals} what="residuals">
          {(rows) => <ErrorStrip modelId={modelId} rows={rows} />}
        </AsyncPanel>
      </section>

      <section>
        <h4>bias and MAE by target-history band</h4>
        <AsyncPanel state={bands} what="the band table">
          {(rows) => (
            <>
              <BandChart rows={rows} />
              <div className="muted" style={{ fontSize: 10.5 }}>
                bands of each entity’s mean label before the date, pooled over the test dates;
                bias above 0 over-forecasts
              </div>
            </>
          )}
        </AsyncPanel>
      </section>

      <section>
        <h4>forecast against actual</h4>
        <AsyncPanel state={pairs} what="the forecast-actual pairs">
          {(data) => (
            <>
              <Hexbin pairs={data.pairs} />
              <div className="muted" style={{ fontSize: 10.5 }}>
                {data.sampled
                  ? `${data.pairs.length} of ${data.n_total} labeled rows (sampled by entity)`
                  : `${data.n_total} labeled rows`}
                ; dashed line: forecast = outcome
              </div>
            </>
          )}
        </AsyncPanel>
      </section>

      <section>
        <h4>against the baseline · per segment</h4>
        <AsyncPanel state={baseline} what="the baseline comparison">
          {(data) => <BaselineTable data={data} />}
        </AsyncPanel>
      </section>

      <section>
        <h4>fitted model structure</h4>
        <AsyncPanel state={structure} what="the model structure">
          {(data) => <Structure data={data} />}
        </AsyncPanel>
      </section>
    </>
  )
}
